"""
DSN GTM weekly report runner.

Pulls both views live from HubSpot for the trailing 9 weeks, builds the
editable PowerPoint deck (via deck_builder), and emails it as a .pptx
attachment through Resend. Same chain as the Monday stale-deal brief:
cron-job.org -> GitHub Actions -> this script -> HubSpot -> Resend.

Runner needs only: pip install requests python-pptx
(no LibreOffice; python-pptx writes the .pptx directly).

Env vars (GitHub Actions secrets):
  HUBSPOT_TOKEN, RESEND_API_KEY, EMAIL_FROM, EMAIL_TO
"""

import os
import base64
import tempfile
import datetime as dt
import requests

# Load a local .env file when testing on your computer.
# In the cloud this does nothing (GitHub provides the secrets), so it is safe.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from gtm_weekly_deck_builder import build_deck, k  # shared renderer

WEEKS = 9
REP_MODE = "distinct"   # "distinct" = owners who closed in the window; else "fixed"
FIXED_REPS = {"New Logo Sales (Platform)": 3,
              "Atlas / Cloud Upgrades": 3,
              "Strategic Accounts (DSOs)": 1}

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
EMAIL_FROM = os.environ.get("EMAIL_FROM", "DSN GTM <reports@yourdomain.com>")
EMAIL_TO = os.environ.get("EMAIL_TO", "isaacs@dsn.com")

SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/deals/search"
HEADERS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}",
           "Content-Type": "application/json"}

# View 1: closed-won acquisitions (validated filters, reconciles to revenue report)
ACQ_SEGMENTS = [
    {"name": "New Logo Sales (Platform)", "pipeline": "default",
     "stages": ["closedwon", "952722998"], "deal_type": None},
    {"name": "Atlas / Cloud Upgrades", "pipeline": "647046539",
     "stages": ["952805189", "952785055"], "deal_type": "Cloud Conversion"},
    {"name": "Strategic Accounts (DSOs)", "pipeline": "646383620",
     "stages": ["1000760833", "1209999671"], "deal_type": None},
]

# View 2: deal creation, New Customer + Cloud Conversion/Atlas only (no add-ons)
CREATE_STREAMS = [
    {"name": "New Customer", "pipeline": "default",
     "deal_type": None, "na": None},
    {"name": "Cloud Conversion / Atlas", "pipeline": "647046539",
     "deal_type": "Cloud Conversion", "na": "1119649101"},
]


# ---- dates ------------------------------------------------------------
def week_ending_friday(d):
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def build_window(today=None):
    today = today or dt.date.today()
    last_friday = today - dt.timedelta(days=(today.weekday() - 4) % 7)
    cols = [last_friday - dt.timedelta(days=7 * (WEEKS - 1 - i))
            for i in range(WEEKS)]
    return cols, cols[0] - dt.timedelta(days=6), cols[-1]


def to_date(val):
    """Parse a HubSpot timestamp (epoch-ms int/str or ISO 8601) to a date."""
    s = str(val).strip()
    if s.isdigit():
        return dt.datetime.fromtimestamp(int(s) / 1000, tz=dt.timezone.utc).date()
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.date()


def to_friday(ms):
    return week_ending_friday(to_date(ms))


def ms_bounds(start, end):
    lo = int(dt.datetime(start.year, start.month, start.day).timestamp() * 1000)
    hi = int((dt.datetime(end.year, end.month, end.day)
              + dt.timedelta(days=1)).timestamp() * 1000)
    return lo, hi


# ---- hubspot ----------------------------------------------------------
def search(filters, props):
    out, after = [], None
    while True:
        payload = {"filterGroups": [{"filters": filters}],
                   "properties": props, "limit": 100}
        if after:
            payload["after"] = after
        r = requests.post(SEARCH_URL, headers=HEADERS, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out


def get_acquisitions(columns, start, end):
    lo, hi = ms_bounds(start, end)
    idx = {f: i for i, f in enumerate(columns)}
    valid = set(columns)
    segments = []
    for seg in ACQ_SEGMENTS:
        filters = [
            {"propertyName": "pipeline", "operator": "EQ", "value": seg["pipeline"]},
            {"propertyName": "dealstage", "operator": "IN", "values": seg["stages"]},
            {"propertyName": "closedate", "operator": "BETWEEN",
             "value": lo, "highValue": hi},
        ]
        if seg["deal_type"]:
            filters.append({"propertyName": "existing_customer_deal_type",
                            "operator": "EQ", "value": seg["deal_type"]})
        acq = [0] * WEEKS
        arr = [0.0] * WEEKS
        owners = set()
        for d in search(filters, ["hs_arr", "closedate", "hubspot_owner_id"]):
            p = d["properties"]
            f = to_friday(p["closedate"])
            if f not in valid:
                continue
            i = idx[f]
            acq[i] += 1
            arr[i] += float(p.get("hs_arr") or 0)
            if p.get("hubspot_owner_id"):
                owners.add(p["hubspot_owner_id"])
        reps = FIXED_REPS.get(seg["name"], len(owners)) if REP_MODE == "fixed" \
            else len(owners)
        segments.append({"name": seg["name"], "reps": reps,
                         "acq": acq, "arr": arr})
    return segments


def get_creation(columns, start, end):
    lo, hi = ms_bounds(start, end)
    idx = {f: i for i, f in enumerate(columns)}
    valid = set(columns)
    rows = []
    for st in CREATE_STREAMS:
        filters = [
            {"propertyName": "pipeline", "operator": "EQ", "value": st["pipeline"]},
            {"propertyName": "createdate", "operator": "BETWEEN",
             "value": lo, "highValue": hi},
        ]
        if st["deal_type"]:
            filters.append({"propertyName": "existing_customer_deal_type",
                            "operator": "EQ", "value": st["deal_type"]})
        if st["na"]:
            filters.append({"propertyName": "dealstage", "operator": "NEQ",
                            "value": st["na"]})
        counts = [0] * WEEKS
        for d in search(filters, ["createdate"]):
            f = to_friday(d["properties"]["createdate"])
            if f in valid:
                counts[idx[f]] += 1
        rows.append({"name": st["name"], "counts": counts})
    return rows


# ---- send -------------------------------------------------------------
def send(path, columns):
    # %-d is not portable to Windows; format day without leading zero manually
    through = columns[-1].strftime("%b ") + str(columns[-1].day)
    with open(path, "rb") as fh:
        content = base64.b64encode(fh.read()).decode()
    body = (f"<p style='font-family:Arial'>DSN GTM weekly through "
            f"<b>{through}</b>. Editable PowerPoint attached: acquisitions and "
            f"productivity by week, plus deal creation by pipeline.</p>")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"},
        json={"from": EMAIL_FROM, "to": [EMAIL_TO],
              "subject": f"DSN GTM Weekly (through {through})",
              "html": body,
              "attachments": [{"filename": f"dsn-gtm-weekly-{through}.pptx",
                               "content": content}]},
        timeout=30,
    )
    r.raise_for_status()
    print("Sent:", r.json().get("id"))


def main():
    cols, start, end = build_window()
    labels = [c.strftime("%b ") + str(c.day) for c in cols]
    acq = get_acquisitions(cols, start, end)
    create = get_creation(cols, start, end)
    path = os.path.join(tempfile.gettempdir(), "dsn-gtm-weekly.pptx")
    build_deck(labels, acq, create, path)
    send(path, cols)


if __name__ == "__main__":
    main()
