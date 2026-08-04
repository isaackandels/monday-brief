"""
DSN Daily Outreach Email
Pulls contacts worth reaching out to from HubSpot, has Claude rank them,
and emails the list every weekday morning. Runs on GitHub Actions cron.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
FROM_EMAIL = os.environ.get("BRIEF_FROM_EMAIL", "onboarding@resend.dev")
TO_EMAIL = os.environ.get("BRIEF_TO_EMAIL", "isaacs@dsn.com")

PORTAL_ID = "47651487"

STALE_DEAL_DAYS = 14          # per DSN pipeline hygiene SLA
WARM_SIGNAL_DAYS = 7          # site visit within this window = warm, any stage
MAX_CANDIDATES_PER_QUERY = 60 # pulled per HubSpot query
MAX_STALE_DEALS = 30          # top open deals by ARR with no recent activity
MAX_ENRICH = 40               # shortlist that gets full history + Claude ranking
HISTORY_PER_CONTACT = 3       # recent emails/calls/meetings/notes per contact
TOP_PICKS = 12                # contacts in the final email

CLAUDE_MODEL = "claude-sonnet-4-6"

HS_BASE = "https://api.hubapi.com"
HS_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}

REPS = {
    "403415850": "Kim Cannon",
    "12742684": "Tami Ferro",
    "530210063": "Eric Wong",
    "79812036": "Jonny Harris",
    "1494239239": "Michelle Simpson",
}
MARKETING = {
    "750674104": "Isaac Shapot",
    "82160145": "Bruno Ferraz",
}
OWNERS = {**REPS, **MARKETING}

SENT_LOG = "sent_log.json"   # committed back by the workflow
SKIP_RECENT_RUNS = 2         # weekly cadence: don't repeat a contact within 2 sends

# Closed/NA-stage exclusions for New Customer, Existing Customer, and DSO
CLOSED_OR_NA_STAGES = [
    # New Customer
    "closedwon", "952722998", "closedlost",
    # Existing Customer
    "952805189", "952785055", "952805190", "1119649101",
    # DSO
    "1000760833", "1209999671", "952781340",
]
STALE_DEAL_PIPELINES = ["default", "647046539", "646383620"]

CONTACT_PROPS = [
    "firstname", "lastname", "email", "phone", "jobtitle", "company",
    "contact_status", "new_customer_journey_stage",
    "existing_customer_journey_phase", "notes_last_contacted",
    "createdate", "hubspot_owner_id", "hs_analytics_last_timestamp",
]


# ---------------------------------------------------------------------------
# HubSpot pulls
# ---------------------------------------------------------------------------

def hs_search(object_type, payload):
    r = requests.post(
        f"{HS_BASE}/crm/v3/objects/{object_type}/search",
        headers=HS_HEADERS, json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("results", [])


def pull_journey_contacts(prop, stages, source_label):
    """MQLs and Leads on a journey stage property, excluding Trash."""
    payload = {
        "filterGroups": [{
            "filters": [
                {"propertyName": prop, "operator": "IN", "values": stages},
                {"propertyName": "contact_status", "operator": "NOT_IN",
                 "values": ["Trash"]},
            ]
        }],
        "properties": CONTACT_PROPS,
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
        "limit": min(MAX_CANDIDATES_PER_QUERY, 100),
    }
    results = hs_search("contacts", payload)
    return [contact_record(c, source_label) for c in results]


def pull_warm_signals():
    """Contacts with a website visit in the last WARM_SIGNAL_DAYS, any journey
    stage. Closes the gap where an old lead warming back up never surfaces
    because journey queries sort newest-created first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=WARM_SIGNAL_DAYS)
    payload = {
        "filterGroups": [{
            "filters": [
                {"propertyName": "hs_analytics_last_timestamp",
                 "operator": "GT",
                 "value": str(int(cutoff.timestamp() * 1000))},
                {"propertyName": "contact_status", "operator": "NOT_IN",
                 "values": ["Trash"]},
            ]
        }],
        "properties": CONTACT_PROPS,
        "sorts": [{"propertyName": "hs_analytics_last_timestamp",
                   "direction": "DESCENDING"}],
        "limit": 30,
    }
    return [contact_record(c, "warm_signal") for c in hs_search("contacts", payload)]


def pull_stale_deal_contacts():
    """Open deals in New + Existing Customer pipelines with no activity in
    STALE_DEAL_DAYS, then their associated contacts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DEAL_DAYS)
    cutoff_ms = str(int(cutoff.timestamp() * 1000))

    payload = {
        "filterGroups": [{
            "filters": [
                {"propertyName": "pipeline", "operator": "IN",
                 "values": STALE_DEAL_PIPELINES},
                {"propertyName": "dealstage", "operator": "NOT_IN",
                 "values": CLOSED_OR_NA_STAGES},
                {"propertyName": "notes_last_updated", "operator": "LT",
                 "value": cutoff_ms},
            ]
        }],
        "properties": ["dealname", "dealstage", "pipeline", "hs_arr",
                       "hubspot_owner_id", "notes_last_updated"],
        "sorts": [{"propertyName": "hs_arr", "direction": "DESCENDING"}],
        "limit": MAX_STALE_DEALS,
    }
    deals = hs_search("deals", payload)

    candidates = []
    for deal in deals:
        contact_ids = deal_contact_ids(deal["id"])
        if not contact_ids:
            continue
        contacts = batch_read_contacts(contact_ids[:2])  # primary contacts
        for c in contacts:
            rec = contact_record(c, "stale_deal")
            arr = deal["properties"].get("hs_arr")
            rec["deal"] = {
                "name": deal["properties"].get("dealname"),
                "arr": arr,
                "days_quiet": days_since(deal["properties"].get("notes_last_updated")),
                "link": f"https://app.hubspot.com/contacts/{PORTAL_ID}/record/0-3/{deal['id']}",
            }
            candidates.append(rec)
    return candidates


def deal_contact_ids(deal_id):
    r = requests.get(
        f"{HS_BASE}/crm/v4/objects/deals/{deal_id}/associations/contacts",
        headers=HS_HEADERS, timeout=30,
    )
    r.raise_for_status()
    return [str(a["toObjectId"]) for a in r.json().get("results", [])]


def batch_read_contacts(ids):
    r = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts/batch/read",
        headers=HS_HEADERS, timeout=30,
        json={"inputs": [{"id": i} for i in ids], "properties": CONTACT_PROPS},
    )
    r.raise_for_status()
    return r.json().get("results", [])


# ---------------------------------------------------------------------------
# Engagement history (emails, calls, meetings, notes)
# ---------------------------------------------------------------------------

ENGAGEMENT_TYPES = {
    "emails": ["hs_timestamp", "hs_email_direction", "hs_email_subject",
               "hs_email_text"],
    "calls": ["hs_timestamp", "hs_call_direction", "hs_call_title",
              "hs_call_body"],
    "meetings": ["hs_timestamp", "hs_meeting_title", "hs_meeting_outcome"],
    "notes": ["hs_timestamp", "hs_note_body"],
    "tasks": ["hs_timestamp", "hs_task_status", "hs_task_subject"],
}

AUTO_REPLY_PREFIXES = ("automatic reply", "out of office", "auto:",
                       "autoreply", "auto-reply", "ooo:")


def batch_associations(contact_ids, to_type):
    """Map contact id -> associated engagement ids, one API call per type."""
    r = requests.post(
        f"{HS_BASE}/crm/v4/associations/contacts/{to_type}/batch/read",
        headers=HS_HEADERS, timeout=30,
        json={"inputs": [{"id": i} for i in contact_ids]},
    )
    r.raise_for_status()
    mapping = {}
    for row in r.json().get("results", []):
        cid = str(row["from"]["id"])
        mapping[cid] = [str(t["toObjectId"]) for t in row.get("to", [])]
    return mapping


def batch_read_objects(object_type, ids, props):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        r = requests.post(
            f"{HS_BASE}/crm/v3/objects/{object_type}/batch/read",
            headers=HS_HEADERS, timeout=30,
            json={"inputs": [{"id": x} for x in ids[i:i + 100]],
                  "properties": props},
        )
        r.raise_for_status()
        for obj in r.json().get("results", []):
            out[str(obj["id"])] = obj["properties"]
    return out


def clean(text, limit=280):
    return " ".join(str(text).split())[:limit] if text else ""


def engagement_summary(etype, p):
    when = days_since(p.get("hs_timestamp"))
    if etype == "emails":
        subject = clean(p.get("hs_email_subject"), 120)
        out = {"type": "email", "direction": p.get("hs_email_direction"),
               "days_ago": when, "subject": subject,
               "snippet": clean(p.get("hs_email_text"))}
        if subject.lower().startswith(AUTO_REPLY_PREFIXES):
            out["auto_reply"] = True
        return out
    if etype == "calls":
        return {"type": "call", "direction": p.get("hs_call_direction"),
                "days_ago": when,
                "subject": clean(p.get("hs_call_title"), 120),
                "snippet": clean(p.get("hs_call_body"))}
    if etype == "meetings":
        return {"type": "meeting", "days_ago": when,
                "subject": clean(p.get("hs_meeting_title"), 120),
                "snippet": clean(p.get("hs_meeting_outcome"))}
    if etype == "tasks":
        return {"type": "task", "days_ago": when,
                "status": p.get("hs_task_status"),
                "subject": clean(p.get("hs_task_subject"), 120)}
    return {"type": "note", "days_ago": when,
            "snippet": clean(p.get("hs_note_body"))}


def enrich_history(candidates):
    """Attach recent emails/calls/meetings/notes so Claude ranks on what was
    actually said, alongside the metadata. Missing scopes skip that type and
    log it; the run continues."""
    ids = [c["id"] for c in candidates]
    per_contact = {i: [] for i in ids}
    for etype, props in ENGAGEMENT_TYPES.items():
        try:
            assoc = batch_associations(ids, etype)
            wanted = {oid for oids in assoc.values() for oid in oids[:10]}
            objects = batch_read_objects(etype, wanted, props)
            for cid, oids in assoc.items():
                for oid in oids[:10]:
                    if oid in objects:
                        per_contact[cid].append(
                            engagement_summary(etype, objects[oid]))
        except requests.HTTPError as e:
            print(f"Skipping {etype} history ({e}). Check private app scopes.",
                  file=sys.stderr)
    for c in candidates:
        full = sorted(
            per_contact.get(c["id"], []),
            key=lambda h: h["days_ago"] if h["days_ago"] is not None else 9999,
        )
        c["_all"] = full
        # Claude sees recent past conversation only; tasks and future items
        # are handled by the hold rules below.
        c["history"] = [
            h for h in full
            if h["type"] != "task"
            and h["days_ago"] is not None and h["days_ago"] >= 0
        ][:HISTORY_PER_CONTACT]


def apply_hold_rules(candidates):
    """Deterministic skips the metadata can't see. A future-dated open task or
    a future meeting means the wait is deliberate (e.g. 'asked for a fall
    check-in', demo already booked): drop the contact. An overdue open task
    keeps the contact in, flagged so the reason reads as a nudge to clear it
    rather than a fresh touch."""
    kept = []
    for c in candidates:
        hold = False
        for h in c.pop("_all", []):
            future = h.get("days_ago") is not None and h["days_ago"] < 0
            if h["type"] == "meeting" and future:
                hold = True
            elif h["type"] == "task" and h.get("status") != "COMPLETED":
                if future:
                    hold = True
                else:
                    c["overdue_task"] = h.get("subject") or "open follow-up task"
        if not hold:
            kept.append(c)
    return kept


def load_sent_log():
    try:
        with open(SENT_LOG) as f:
            runs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), []
    recent = runs[-SKIP_RECENT_RUNS:]
    return {i for r in recent for i in r.get("ids", [])}, runs


def save_sent_log(runs, picks):
    runs.append({"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                 "ids": [c["id"] for c in picks]})
    with open(SENT_LOG, "w") as f:
        json.dump(runs[-20:], f, indent=1)


def one_per_company(picks):
    seen, out = set(), []
    for c in picks:
        key = (c.get("company") or c["id"]).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def trim_for_enrichment(candidates):
    """Cap the shortlist that gets history pulled. Stale deals and MQLs always
    make the cut; leads fill remaining slots by recency."""
    priority = [c for c in candidates
                if c["source"] in ("stale_deal", "new_mql", "existing_mql", "warm_signal")]
    leads = sorted(
        (c for c in candidates if c["source"] in ("new_lead", "existing_lead")),
        key=lambda c: c.get("created_days_ago") or 9999,
    )
    return (priority + leads)[:MAX_ENRICH]


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

def days_since(ms_or_iso):
    if not ms_or_iso:
        return None
    try:
        if str(ms_or_iso).isdigit():
            dt = datetime.fromtimestamp(int(ms_or_iso) / 1000, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ms_or_iso).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except (ValueError, OSError):
        return None


def contact_record(c, source):
    p = c["properties"]
    name = " ".join(x for x in [p.get("firstname"), p.get("lastname")] if x)
    return {
        "id": c["id"],
        "source": source,
        "name": name or p.get("email") or f"Contact {c['id']}",
        "email": p.get("email"),
        "phone": p.get("phone"),
        "title": p.get("jobtitle"),
        "company": p.get("company"),
        "status": p.get("contact_status"),
        "new_journey": p.get("new_customer_journey_stage"),
        "existing_journey": p.get("existing_customer_journey_phase"),
        "owner": OWNERS.get(str(p.get("hubspot_owner_id")), "Unassigned"),
        "lane": "rep" if str(p.get("hubspot_owner_id")) in REPS else "marketing",
        "days_since_contacted": days_since(p.get("notes_last_contacted")),
        "days_since_site_visit": days_since(p.get("hs_analytics_last_timestamp")),
        "created_days_ago": days_since(p.get("createdate")),
        "link": f"https://app.hubspot.com/contacts/{PORTAL_ID}/record/0-1/{c['id']}",
    }


def dedupe(candidates):
    seen, out = set(), []
    for c in candidates:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Claude ranking
# ---------------------------------------------------------------------------

RANK_INSTRUCTIONS = """You are a sales ops analyst for DSN Software, a vertical \
SaaS for oral surgery, perio, and endo practices. From the candidate JSON below, \
pick the {n} contacts most worth a call or email TODAY and return them ranked.

Candidate sources:
- new_mql / new_lead: prospect journey (net-new business)
- existing_mql / existing_lead: existing customers, cloud migration journey
- stale_deal: contact on an open deal with no activity for 14+ days (deal info attached)\n- warm_signal: visited the website in the last 7 days, any stage; recency of intent is the whole point

Each candidate may include "history": their most recent emails, calls, and \
meetings (direction, days_ago, subject, snippet). Read it before ranking. \
Surface unanswered INCOMING_EMAIL messages first, but an entry marked \
auto_reply (or with an out-of-office subject) is NOT engagement; treat that \
thread as unanswered outbound. Skip anyone whose last exchange shows the \
ball is in their court for a stated reason. In each reason, reference the \
actual last conversation so the owner can pick the thread back up in one line.

A candidate with "overdue_task" already has an open follow-up task past its \
due date; include them and write the reason as clearing that task (e.g. \
"Tami's follow-up task on this came due Friday"), never as a fresh cold touch.

Each candidate has a "lane": rep (owned by the sales team) or marketing \
(owned by Isaac/Bruno or unowned; first touches are marketing's job). Pick a \
healthy mix across both lanes. Pick at most ONE contact per practice/company.

Beyond history, prioritize: fresh MQLs nobody has touched, high-ARR stale \
deals going cold, recent website activity, and long gaps since last contact \
on warm records. Mix sources; don't let one section crowd out the rest.

Return ONLY a JSON object, no markdown fences, in this shape:
{{"headline": "one sentence on today's list",
  "picks": [{{"id": "<candidate id>", "reason": "<one short specific sentence why today>"}}]}}

Candidates:
{candidates}"""


def claude_rank(candidates):
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2000,
        "messages": [{
            "role": "user",
            "content": RANK_INSTRUCTIONS.format(
                n=TOP_PICKS, candidates=json.dumps(candidates, default=str)
            ),
        }],
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=body, timeout=120,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"])
    text = text.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(text)
    by_id = {c["id"]: c for c in candidates}
    picks = []
    for p in parsed.get("picks", []):
        c = by_id.get(str(p.get("id")))
        if c:
            c["reason"] = p.get("reason", "")
            picks.append(c)
    return parsed.get("headline", ""), picks[:TOP_PICKS]


def fallback_rank(candidates):
    """Rules-only ordering if the Claude call fails: fresh MQLs first,
    then stale deals by ARR."""
    def key(c):
        if c["source"] in ("new_mql", "existing_mql"):
            return (0, c.get("created_days_ago") or 999)
        if c["source"] == "stale_deal":
            arr = float(c.get("deal", {}).get("arr") or 0)
            return (1, -arr)
        return (2, c.get("days_since_contacted") or 0)
    ranked = sorted(candidates, key=key)[:TOP_PICKS]
    for c in ranked:
        c["reason"] = ""
    return "Claude ranking unavailable today; rules-based order.", ranked


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

SOURCE_LABELS = {
    "new_mql": "New MQL",
    "new_lead": "New-business Lead",
    "existing_mql": "Existing Customer MQL",
    "existing_lead": "Existing Customer Lead",
    "stale_deal": "Deal going quiet",
    "warm_signal": "Warm signal (recent site visit)",
}


def build_html(headline, picks, counts):
    groups = []
    for rep in REPS.values():
        rep_picks = [c for c in picks if c.get("owner") == rep]
        if rep_picks:
            groups.append((rep, rep_picks))
    mkt = [c for c in picks if c.get("lane", "rep") == "marketing"]
    if mkt:
        groups.append(("Marketing / first touch (Isaac + Bruno)", mkt))

    sections = []
    for title, group_picks in groups:
        rows = [pick_row(i, c) for i, c in enumerate(group_picks, 1)]
        sections.append(
            f'<h3 style="color:#0b1f4d;margin:20px 0 4px;font-size:14px;'
            f'text-transform:uppercase;letter-spacing:0.5px;">{title}</h3>'
            f'<table style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>')

    count_txt = " &middot; ".join(f"{v} {SOURCE_LABELS.get(k, k)}s" for k, v in counts.items() if v)
    today = datetime.now(timezone.utc).strftime("%A, %B %-d")
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#1a1a1a;">
      <h2 style="color:#0b1f4d;margin-bottom:4px;">Morning Outreach List</h2>
      <div style="color:#777;font-size:13px;margin-bottom:12px;">{today} &middot; candidate pool: {count_txt}</div>
      <p style="font-size:14px;">{headline}</p>
      {''.join(sections)}
      <div style="color:#999;font-size:12px;margin-top:16px;">Generated automatically from HubSpot. Contacts with a future-dated task or booked meeting are held out on purpose.</div>
    </div>"""


def pick_row(i, c):
    badge = SOURCE_LABELS.get(c["source"], c["source"])
    deal_line = ""
    if c.get("deal"):
        d = c["deal"]
        arr = f"${float(d['arr']):,.0f} ARR" if d.get("arr") else "no ARR set"
        deal_line = (
            f'<div style="font-size:13px;color:#555;margin-top:2px;">'
            f'Deal: <a href="{d["link"]}" style="color:#1a3c8f;">{d["name"]}</a>'
            f' &middot; {arr} &middot; quiet {d["days_quiet"]} days</div>'
        )
    task_line = ""
    if c.get("overdue_task"):
        task_line = (
            f'<div style="font-size:13px;color:#b3541e;margin-top:2px;">'
            f'&#9888; Overdue task: {c["overdue_task"]}</div>'
        )
    contacted = c.get("days_since_contacted")
    contacted_txt = (
        f"last contacted {contacted}d ago" if contacted is not None
        else "no contact logged"
    )
    reason = (
        f'<div style="font-size:13px;color:#1a3c8f;margin-top:4px;">{c["reason"]}</div>'
        if c.get("reason") else ""
    )
    return f"""
    <tr><td style="padding:12px 0;border-bottom:1px solid #e6e6e6;">
      <div style="font-size:15px;">
        <strong>{i}. <a href="{c['link']}" style="color:#0b1f4d;">{c['name']}</a></strong>
        <span style="background:#eef2fb;color:#1a3c8f;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px;">{badge}</span>
      </div>
      <div style="font-size:13px;color:#555;margin-top:2px;">
        {c.get('title') or ''}{' &middot; ' if c.get('title') and c.get('company') else ''}{c.get('company') or ''}
        &middot; owner: {c['owner']} &middot; {contacted_txt}
      </div>
      {deal_line}
      {task_line}
      {reason}
    </td></tr>"""


def send_email(html, subject):
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"},
        json={"from": FROM_EMAIL, "to": [TO_EMAIL],
              "subject": subject, "html": html},
        timeout=30,
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    candidates = []
    candidates += pull_journey_contacts(
        "new_customer_journey_stage", ["MQL"], "new_mql")
    candidates += pull_journey_contacts(
        "new_customer_journey_stage", ["Lead"], "new_lead")
    candidates += pull_journey_contacts(
        "existing_customer_journey_phase", ["MQL"], "existing_mql")
    candidates += pull_journey_contacts(
        "existing_customer_journey_phase", ["Lead"], "existing_lead")
    candidates += pull_warm_signals()
    candidates += pull_stale_deal_contacts()
    candidates = dedupe(candidates)

    recent_ids, runs = load_sent_log()
    candidates = [c for c in candidates if c["id"] not in recent_ids]

    if not candidates:
        send_email(
            "<p>No outreach candidates found today. Check journey stage "
            "values if this seems wrong.</p>",
            "Morning Outreach: nothing in the queue")
        return

    counts = {}
    for c in candidates:
        counts[c["source"]] = counts.get(c["source"], 0) + 1

    shortlist = trim_for_enrichment(candidates)
    enrich_history(shortlist)
    shortlist = apply_hold_rules(shortlist)

    if ANTHROPIC_API_KEY:
        try:
            headline, picks = claude_rank(shortlist)
        except Exception as e:
            print(f"Claude ranking failed, using fallback: {e}", file=sys.stderr)
            headline, picks = fallback_rank(shortlist)
    else:
        headline, picks = fallback_rank(shortlist)
    picks = one_per_company(picks)

    html = build_html(headline, picks, counts)
    today = datetime.now(timezone.utc).strftime("%b %-d")
    send_email(html, f"Morning Outreach: {len(picks)} contacts to hit today ({today})")
    save_sent_log(runs, picks)
    print(f"Sent {len(picks)} picks from {len(candidates)} candidates.")


if __name__ == "__main__":
    main()
