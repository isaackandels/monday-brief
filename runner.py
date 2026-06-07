#!/usr/bin/env python3
"""Monday Pipeline Brief — orchestrator (BUILD sections 1-8).

Read-only against HubSpot. Clocks staleness off real engagement records, not
summary fields, so deals worked through notes/tasks/stage moves drop off and
deals coasting on automation stay.

Run:
    python runner.py            # build + send
    python runner.py --dry-run  # build + write HTML, do not send
"""
import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

import hubspot_client as hs
import render
import emailer

DAY_MS = 86_400_000


def _load_dotenv():
    """Load a local .env into os.environ (without overriding existing vars).

    Lightweight, no dependency. GitHub Actions injects real env vars, so the
    .env file simply won't exist there and this is a no-op.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()
STALE_DAYS = int(os.environ.get("STALE_DAYS", "45"))


# ----------------------------------------------------------------------
# timestamp helpers
# ----------------------------------------------------------------------
def to_ms(val):
    """Parse a HubSpot timestamp (epoch-ms int/str or ISO 8601) to epoch ms."""
    if val in (None, ""):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if s.isdigit():
        return int(s)
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def ms_to_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


# ----------------------------------------------------------------------
# engagement pull — the staleness clock (BUILD section 5b)
# ----------------------------------------------------------------------
def last_real_touch_ms(client, deal_ids):
    """Return {deal_id: latest_real_touch_ms} across engagements + stage moves.

    Real touch = notes, calls, emails, meetings, COMPLETED tasks, and manual
    (non-automation) stage changes. Open/future tasks are NOT a touch; they are
    handled by the next-step gate via notes_next_activity_date.
    """
    touch = {did: None for did in deal_ids}

    def bump(did, ms):
        if ms is None:
            return
        if touch.get(did) is None or ms > touch[did]:
            touch[did] = ms

    for etype in hs.ENGAGEMENT_TYPES:
        try:
            assoc = client.batch_associations("deals", etype, deal_ids)
        except Exception as e:  # noqa: BLE001 — scope/availability issues shouldn't kill the run
            print(f"  warn: association pull for {etype} failed ({e}); skipping")
            continue

        # map engagement id -> list of deal ids it belongs to
        eng_to_deals = {}
        for did, tos in assoc.items():
            for t in tos:
                eng_to_deals.setdefault(str(t.get("toObjectId") or t.get("id")), []).append(did)
        if not eng_to_deals:
            continue

        props = ["hs_timestamp", "hs_createdate"]
        if etype == "tasks":
            props += ["hs_task_status", "hs_task_completion_date"]
        try:
            records = client.batch_read(etype, list(eng_to_deals), props)
        except Exception as e:  # noqa: BLE001
            print(f"  warn: batch read for {etype} failed ({e}); skipping")
            continue

        for eid, p in records.items():
            if etype == "tasks":
                if (p.get("hs_task_status") or "").upper() != "COMPLETED":
                    continue  # open task -> next step, not a touch
                ts = to_ms(p.get("hs_task_completion_date")) or to_ms(p.get("hs_timestamp"))
            else:
                ts = to_ms(p.get("hs_timestamp")) or to_ms(p.get("hs_createdate"))
            for did in eng_to_deals.get(eid, []):
                bump(did, ts)

    # manual stage changes (per-deal property history)
    for did in deal_ids:
        try:
            stage_ts = client.deal_stage_history(did)
        except Exception as e:  # noqa: BLE001
            stage_ts = None
            print(f"  warn: stage history for {did} failed ({e})")
        bump(did, to_ms(stage_ts))

    return touch


# ----------------------------------------------------------------------
# enrichment (BUILD section 5)
# ----------------------------------------------------------------------
def _pick_primary(to_list):
    if not to_list:
        return None
    for t in to_list:
        for at in t.get("associationTypes", []):
            if "primary" in (at.get("label") or "").lower():
                return str(t.get("toObjectId") or t.get("id"))
    first = to_list[0]
    return str(first.get("toObjectId") or first.get("id"))


def enrich(client, deals):
    deal_ids = [d["id"] for d in deals]

    # companies
    comp_assoc = client.batch_associations("deals", "companies", deal_ids)
    deal_to_company = {did: _pick_primary(tos) for did, tos in comp_assoc.items()}
    company_ids = [c for c in deal_to_company.values() if c]
    companies = client.batch_read("companies", set(company_ids), hs.COMPANY_PROPERTIES) if company_ids else {}

    # contacts
    cont_assoc = client.batch_associations("deals", "contacts", deal_ids)
    deal_to_contact = {did: _pick_primary(tos) for did, tos in cont_assoc.items()}
    contact_ids = [c for c in deal_to_contact.values() if c]
    contacts = client.batch_read("contacts", set(contact_ids), hs.CONTACT_ENRICH_PROPERTIES) if contact_ids else {}

    for d in deals:
        cid = deal_to_company.get(d["id"])
        d["company"] = companies.get(cid) if cid else None
        pid = deal_to_contact.get(d["id"])
        d["contact"] = contacts.get(pid) if pid else None


# ----------------------------------------------------------------------
# main pipeline
# ----------------------------------------------------------------------
def build(dry_run=False):
    now = datetime.now(timezone.utc)
    run_ms = int(now.timestamp() * 1000)
    run_date = now.date()
    cutoff_ms = run_ms - STALE_DAYS * DAY_MS
    print(f"run_date={run_date}  STALE_DAYS={STALE_DAYS}  cutoff={ms_to_date(cutoff_ms)}")

    client = hs.HubSpotClient()

    # 2. deal search (first-pass over-select)
    raw_deals = client.search_deals(cutoff_ms)
    print(f"deal search (first pass): {len(raw_deals)}")

    deals = []
    for r in raw_deals:
        p = r.get("properties", {})
        deals.append({
            "id": r["id"],
            "name": p.get("dealname") or "",
            "owner": p.get("hubspot_owner_id"),
            "pipeline": p.get("pipeline"),
            "dtype": p.get("existing_customer_deal_type") or "",
            "arr": p.get("hs_arr") or "",
            "stage": p.get("dealstage"),
            "notes_last_contacted": to_ms(p.get("notes_last_contacted")),
            "next_activity": to_ms(p.get("notes_next_activity_date")),
        })

    # 3. next-step gate: drop deals with a future next activity
    pre = len(deals)
    deals = [d for d in deals if not (d["next_activity"] and d["next_activity"] > run_ms)]
    print(f"after next-step gate: {len(deals)}  (dropped {pre - len(deals)} with a future next step)")

    # 5b. engagement pull -> last_real_touch (floored at notes_last_contacted)
    touches = last_real_touch_ms(client, [d["id"] for d in deals])
    for d in deals:
        candidates = [t for t in (touches.get(d["id"]), d["notes_last_contacted"]) if t]
        d["last_real_touch_ms"] = max(candidates) if candidates else None

    # 6. staleness gate: keep only deals with no real touch in STALE_DAYS+
    kept = []
    for d in deals:
        lrt = d["last_real_touch_ms"]
        if lrt is None or lrt <= cutoff_ms:
            base = lrt if lrt is not None else d["notes_last_contacted"]
            if base is not None:
                d["last_real_touch"] = ms_to_date(base)
                d["stale_days"] = (run_date - d["last_real_touch"]).days
            else:
                d["last_real_touch"] = None
                d["stale_days"] = STALE_DAYS
            kept.append(d)
    deals = kept
    print(f"after staleness gate: {len(deals)}  (dropped {len(touches) - len(deals)} worked recently)")

    # 5. enrichment on the final kept set
    if deals:
        enrich(client, deals)

    # per-rep summary (smoke check)
    print("per-rep stale deals:")
    for oid, name in render.REPS.items():
        n = sum(1 for d in deals if d["owner"] == oid)
        print(f"  {name}: {n}")

    # 7. render
    html_desktop = render.render(deals, run_date)
    html_email = render.render_email(deals, run_date)

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "dsn-pipeline-brief-live.html"), "w", encoding="utf-8") as f:
        f.write(html_desktop)
    with open(os.path.join(here, "dsn-pipeline-brief-email.html"), "w", encoding="utf-8") as f:
        f.write(html_email)
    print("wrote dsn-pipeline-brief-live.html and dsn-pipeline-brief-email.html")

    total_arr = sum(render._arr_int(d) for d in deals)
    subject = (f"Monday Pipeline Brief — {len(deals)} stale deals, "
               f"${total_arr/1000:.0f}K at risk — {render.fmt_date(run_date)}")

    # 8. send
    if dry_run:
        print(f"[dry-run] would send: {subject!r}")
    else:
        emailer.send_brief(html_email, subject)

    return deals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build HTML but do not send")
    args = ap.parse_args()
    try:
        build(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
