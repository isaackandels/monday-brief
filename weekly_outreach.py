"""
DSN Daily Outreach Email
Pulls contacts worth reaching out to from HubSpot, has Claude rank them,
and emails the list every weekday morning. Runs on GitHub Actions cron.
"""

import json
import os
import re
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
SKIP_RECENT_RUNS = 0         # 0 = no rotation: repeats allowed when still warranted;
                             # sent_log.json is kept as a record of each send only

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
    "hs_sequences_is_enrolled",
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


def hs_search_all(object_type, payload, max_pages=3):
    """Follow search pagination; up to max_pages x 100 results."""
    results, after = [], None
    for _ in range(max_pages):
        body = {**payload, "after": after} if after else payload
        r = requests.post(
            f"{HS_BASE}/crm/v3/objects/{object_type}/search",
            headers=HS_HEADERS, json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        results += data.get("results", [])
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return results


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


def batch_associations(from_type, from_ids, to_type):
    """Map object id -> associated object ids, one API call per pair."""
    r = requests.post(
        f"{HS_BASE}/crm/v4/associations/{from_type}/{to_type}/batch/read",
        headers=HS_HEADERS, timeout=30,
        json={"inputs": [{"id": i} for i in from_ids]},
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
            assoc = batch_associations("contacts", ids, etype)
            # Association order is NOT chronological, so pull wide (60 per
            # contact) and let the date sort below pick what's recent.
            wanted = {oid for oids in assoc.values() for oid in oids[:60]}
            objects = batch_read_objects(etype, wanted, props)
            for cid, oids in assoc.items():
                for oid in oids[:60]:
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
        if c.get("in_sequence"):
            hold = True  # already being worked by a sequence
        dsc = c.get("days_since_contacted")
        if dsc is not None and dsc <= 1:
            hold = True  # touched today/yesterday; actively being worked
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
    if SKIP_RECENT_RUNS <= 0:
        return set(), runs
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


def resolve_task_subjects(tasks):
    """Map task id -> (label, record link) by walking associations in
    priority order: contact, then deal, then company. Tasks live at all
    three levels; a task with no contact still gets a meaningful name."""
    resolved = {}
    remaining = [t["id"] for t in tasks]
    chains = [
        ("contacts", ["firstname", "lastname", "email"], "0-1",
         lambda p: " ".join(
             x for x in [p.get("firstname"), p.get("lastname")] if x)
         or p.get("email")),
        ("deals", ["dealname"], "0-3", lambda p: p.get("dealname")),
        ("companies", ["name"], "0-2", lambda p: p.get("name")),
    ]
    for obj_type, props, type_id, namer in chains:
        if not remaining:
            break
        try:
            assoc = batch_associations("tasks", remaining, obj_type)
            wanted = {ids[0] for ids in assoc.values() if ids}
            objects = batch_read_objects(obj_type, wanted, props) if wanted else {}
        except requests.HTTPError as e:
            print(f"Skipping task-{obj_type} resolution ({e}); "
                  f"check private app scopes.", file=sys.stderr)
            continue
        still = []
        for tid in remaining:
            ids = assoc.get(tid) or []
            name = namer(objects[ids[0]]) if ids and ids[0] in objects else None
            if name:
                resolved[tid] = (
                    name,
                    f"https://app.hubspot.com/contacts/{PORTAL_ID}/record/{type_id}/{ids[0]}")
            else:
                still.append(tid)
        remaining = still
    return resolved


def pull_overdue_tasks():
    """Every open task past its due date for the five reps plus Isaac and
    Bruno, grouped by owner, oldest first, with the associated contact
    resolved so each row links to a person instead of a task id."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    base = [
        {"propertyName": "hubspot_owner_id", "operator": "IN",
         "values": list(OWNERS)},
        {"propertyName": "hs_task_status", "operator": "NEQ",
         "value": "COMPLETED"},
    ]
    payload = {
        # Filter groups are OR'd: past-due tasks, plus open tasks that
        # never got a due date (otherwise invisible to any date filter).
        "filterGroups": [
            {"filters": base + [{"propertyName": "hs_timestamp",
                                 "operator": "LT", "value": now_ms}]},
            {"filters": base + [{"propertyName": "hs_timestamp",
                                 "operator": "NOT_HAS_PROPERTY"}]},
        ],
        "properties": ["hs_task_subject", "hs_task_status", "hs_timestamp",
                       "hubspot_owner_id"],
        "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}],
        "limit": 100,
    }
    tasks = hs_search_all("tasks", payload, max_pages=5)
    if not tasks:
        return {}
    resolved = resolve_task_subjects(tasks)

    per_owner = {}
    for t in tasks:
        p = t["properties"]
        owner = OWNERS.get(str(p.get("hubspot_owner_id")))
        if not owner:
            continue
        entry = {
            "subject": clean(p.get("hs_task_subject"), 140) or "Untitled task",
            "days_overdue": days_since(p.get("hs_timestamp")),
            "link": f"https://app.hubspot.com/contacts/{PORTAL_ID}/record/0-27/{t['id']}",
        }
        who = resolved.get(t["id"])
        if who:
            entry["contact"] = who[0]
            entry["link"] = who[1]
        per_owner.setdefault(owner, []).append(entry)
    for tasks_ in per_owner.values():
        tasks_.sort(key=lambda t: (t["days_overdue"] is None,
                                   -(t["days_overdue"] or 0)))
    return per_owner


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
        "in_sequence": str(p.get("hs_sequences_is_enrolled")).lower() == "true",
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

Recommend a concrete channel in every reason. Whenever two or more outbound \
emails have gone unanswered or a thread has stalled, the recommendation is a \
PHONE CALL, named explicitly ("Michelle should call Mark today"), never \
"reach out" and never another email. Recommend email only to continue a live \
thread.

Calibrate urgency to the real gap. If the most recent event is an inbound \
reply within the last 2 days, the ball just arrived: frame it as answering a \
live conversation ("she replied yesterday asking about X; respond today and \
keep momentum"), and never imply the contact was neglected.

If history shows months of unanswered outbound (a completed sequence plus \
manual follow-ups with no reply or engagement), do NOT pick them and never \
frame that as an open loop; silence after that many touches is an answer.

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
        "max_tokens": 8000,
        "messages": [{
            "role": "user",
            "content": RANK_INSTRUCTIONS.format(
                n=TOP_PICKS, candidates=json.dumps(candidates, default=str)
            ) + "\n\nBegin your reply with { immediately. No preamble.",
        }],
    }
    last_err = None
    for attempt in range(2):
        try:
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
            data = r.json()
            text = "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                blocks = [b.get("type") for b in data.get("content", [])]
                raise ValueError(
                    f"no JSON in response (stop_reason="
                    f"{data.get('stop_reason')}, blocks={blocks}, "
                    f"text_len={len(text)})")
            parsed = json.loads(m.group(0))
            break
        except (requests.RequestException, ValueError,
                json.JSONDecodeError) as e:
            last_err = e
            print(f"Claude attempt {attempt + 1} failed: {e}", file=sys.stderr)
    else:
        raise last_err

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
        c["reason"] = fallback_reason(c)
    return ("AI ranking was unavailable for this run; the list below is "
            "ordered by rules (fresh MQLs, then biggest quiet deals)."), ranked


def fallback_reason(c):
    dsc = c.get("days_since_contacted")
    touch = f"last contacted {dsc}d ago" if dsc is not None else "no contact ever logged"
    if c.get("deal"):
        d = c["deal"]
        arr = f"${float(d['arr']):,.0f} ARR" if d.get("arr") else "open"
        return f"{arr} deal quiet {d['days_quiet']} days; {touch}."
    if c["source"] in ("new_mql", "existing_mql"):
        age = c.get("created_days_ago")
        age_txt = f"MQL for {age}d" if age is not None else "MQL"
        return f"{age_txt}; {touch}."
    if c["source"] == "warm_signal":
        v = c.get("days_since_site_visit")
        return f"Visited the site {v}d ago; {touch}."
    return f"In the funnel; {touch}."


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


NAVY = "#0B1F4D"
ACCENT = "#4F7DF9"
INK = "#1F2430"
MUTED = "#6B7280"
LINE = "#E4E8F0"
ORANGE = "#C2410C"

FONT = "'Segoe UI',-apple-system,Helvetica,Arial,sans-serif"


def build_html(headline, picks, counts, overdue=None, used_fallback=False):
    overdue = overdue or {}
    banner = ""
    if used_fallback:
        banner = (f'<tr><td style="padding:14px 32px 0;">'
                  f'<div style="background:#FDF0E7;border-left:4px solid {ORANGE};'
                  f'padding:10px 14px;font-size:13px;color:{ORANGE};font-weight:600;">'
                  f'AI ranking failed on this run &mdash; reasons below are basic facts, '
                  f'not judgment. Check the GitHub Actions log for the error.</div></td></tr>')
    groups = [(rep, [c for c in picks if c.get("owner") == rep])
              for rep in REPS.values()]
    groups.append(("Marketing / First Touch",
                   [c for c in picks if c.get("lane", "rep") == "marketing"]))

    pick_sections = "".join(section_block(t, g) for t, g in groups)
    overdue_section = overdue_block(overdue)

    pool = " &nbsp;&middot;&nbsp; ".join(
        f"<strong style=\"color:{NAVY};\">{v}</strong> {SOURCE_LABELS.get(k, k).lower()}s"
        for k, v in counts.items() if v)
    today = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")

    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4F6FA;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:640px;max-width:640px;background:#FFFFFF;font-family:{FONT};color:{INK};">

  <tr><td style="background:{NAVY};padding:26px 32px 22px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:{ACCENT};font-weight:600;">DSN Software</div>
    <div style="font-size:24px;font-weight:700;color:#FFFFFF;margin-top:4px;">Weekly Outreach Brief</div>
    <div style="font-size:13px;color:#B8C4DE;margin-top:4px;">{today}</div>
  </td></tr>

  {banner}
  <tr><td style="padding:20px 32px 0;">
    <div style="font-size:15px;line-height:1.55;color:{INK};">{headline}</div>
    <div style="font-size:12px;color:{MUTED};margin-top:12px;padding-bottom:16px;border-bottom:1px solid {LINE};">Candidate pool &nbsp;&rarr;&nbsp; {pool}</div>
  </td></tr>

  {pick_sections}
  {overdue_section}

  <tr><td style="padding:22px 32px 26px;">
    <div style="font-size:11px;color:#9AA3B2;border-top:1px solid {LINE};padding-top:14px;line-height:1.6;">
      Every name and task above links to its HubSpot record &mdash; click through to act. Contacts with a future-dated task, a booked meeting, an active sequence enrollment, or a touch in the last day are held out on purpose. A contact may repeat week to week if they still warrant outreach.
    </div>
  </td></tr>

</table>
</td></tr>
</table>"""


def section_block(title, group_picks):
    if group_picks:
        rows = "".join(pick_row(i, c) for i, c in enumerate(group_picks, 1))
    else:
        rows = (f'<tr><td style="padding:12px 0 14px;border-bottom:1px solid {LINE};'
                f'font-size:13px;color:{MUTED};font-style:italic;">'
                f'No picks this week &mdash; nothing urgent surfaced, or everything is on a deliberate hold.</td></tr>')
    return f"""
  <tr><td style="padding:22px 32px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="4" style="background:{ACCENT};font-size:0;line-height:0;">&nbsp;</td>
      <td style="padding-left:10px;font-size:13px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:{NAVY};">{title}</td>
    </tr></table>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
  </td></tr>"""


def pick_row(i, c):
    badge = SOURCE_LABELS.get(c["source"], c["source"])
    contacted = c.get("days_since_contacted")
    contacted_txt = (f"last contacted {contacted}d ago" if contacted is not None
                     else "no contact logged")
    meta_bits = [b for b in [c.get("title"), c.get("company")] if b]
    meta = " &middot; ".join(meta_bits + [contacted_txt])

    deal_line = ""
    if c.get("deal"):
        d = c["deal"]
        arr = f"${float(d['arr']):,.0f} ARR" if d.get("arr") else "no ARR set"
        deal_line = (f'<div style="font-size:13px;color:{MUTED};margin-top:3px;">'
                     f'<a href="{d["link"]}" style="color:{ACCENT};text-decoration:underline;font-weight:600;">{d["name"]}</a>'
                     f' &nbsp;{arr} &nbsp;&middot;&nbsp; quiet {d["days_quiet"]} days</div>')

    task_line = ""
    if c.get("overdue_task"):
        task_line = (f'<div style="font-size:12.5px;color:{ORANGE};margin-top:4px;font-weight:600;">'
                     f'&#9888;&nbsp; Overdue task: {c["overdue_task"]}</div>')

    reason = ""
    if c.get("reason"):
        reason = (f'<div style="font-size:13.5px;color:{INK};margin-top:6px;line-height:1.5;'
                  f'background:#F5F8FF;padding:8px 12px;">{c["reason"]}</div>')

    return f"""
    <tr><td style="padding:16px 0 14px;border-bottom:1px solid {LINE};">
      <div style="font-size:15.5px;">
        <a href="{c['link']}" style="color:{NAVY};font-weight:700;text-decoration:underline;">{i}. {c['name']}</a>
        &nbsp;<span style="font-size:10.5px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;color:{ACCENT};background:#EAF0FE;padding:2px 7px;">{badge}</span>
      </div>
      <div style="font-size:13px;color:{MUTED};margin-top:3px;">{meta}</div>
      {deal_line}
      {task_line}
      {reason}
    </td></tr>"""


def overdue_block(overdue):
    order = list(REPS.values()) + list(MARKETING.values())
    people = [(p, overdue[p]) for p in order if overdue.get(p)]
    if not people:
        return ""
    blocks = []
    for person, tasks in people:
        rows = []
        for t in tasks:
            if t.get("days_overdue") is not None:
                age = (f'<span style="color:{ORANGE};font-weight:700;">'
                       f'{t["days_overdue"]}d overdue</span>')
            else:
                age = (f'<span style="color:{MUTED};font-weight:600;">'
                       f'no due date</span>')
            who = (f'<div style="font-size:12px;color:{MUTED};margin-top:2px;">'
                   f'{t["contact"]}</div>') if t.get("contact") else ""
            subject = t["subject"][:96] + ("&hellip;" if len(t["subject"]) > 96 else "")
            rows.append(
                f'<tr>'
                f'<td style="padding:8px 12px 8px 0;border-bottom:1px solid #F0E7DF;font-size:13px;line-height:1.4;">'
                f'<a href="{t["link"]}" style="color:{ACCENT};text-decoration:underline;font-weight:600;">{subject}</a>'
                f'{who}</td>'
                f'<td width="96" align="right" valign="top" style="padding:8px 0;border-bottom:1px solid #F0E7DF;'
                f'font-size:12.5px;white-space:nowrap;">{age}</td>'
                f'</tr>')
        blocks.append(
            f'<div style="font-size:13px;font-weight:700;color:{NAVY};margin:14px 0 2px;">{person}'
            f' &nbsp;<span style="font-weight:400;color:{MUTED};">({len(tasks)})</span></div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>')
    return f"""
  <tr><td style="padding:26px 32px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="4" style="background:{ORANGE};font-size:0;line-height:0;">&nbsp;</td>
      <td style="padding-left:10px;font-size:13px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:{NAVY};">Overdue Tasks</td>
    </tr></table>
    {''.join(blocks)}
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

    used_fallback = False
    if ANTHROPIC_API_KEY:
        try:
            headline, picks = claude_rank(shortlist)
        except Exception as e:
            print(f"Claude ranking failed, using fallback: {e}", file=sys.stderr)
            headline, picks = fallback_rank(shortlist)
            used_fallback = True
    else:
        headline, picks = fallback_rank(shortlist)
        used_fallback = True
    picks = one_per_company(picks)

    overdue = pull_overdue_tasks()

    html = build_html(headline, picks, counts, overdue, used_fallback)
    today = datetime.now(timezone.utc).strftime("%b %-d")
    send_email(html, f"Morning Outreach: {len(picks)} contacts to hit today ({today})")
    save_sent_log(runs, picks)
    print(f"Sent {len(picks)} picks from {len(candidates)} candidates.")


if __name__ == "__main__":
    main()
