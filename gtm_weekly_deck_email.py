"""
DSN GTM monthly report runner.

Pulls both views live from HubSpot for the current year to date, buckets by
calendar month, builds the editable PowerPoint deck (via the deck builder),
and emails it as a .pptx attachment through Resend.

Only sends on the LAST FRIDAY of the month. cron-job.org keeps firing every
Friday at 10am ET; this script decides whether today is the send day. To send
on any other day (local testing or a manual run), set FORCE_SEND=1.

Runner needs: pip install requests python-pptx python-dotenv
(no LibreOffice; python-pptx writes the .pptx directly).

Env vars (GitHub Actions secrets / local .env):
  HUBSPOT_TOKEN, RESEND_API_KEY, EMAIL_FROM, EMAIL_TO
  FORCE_SEND (optional: "1" to bypass the last-Friday gate)
"""

import os
import base64
import calendar
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

REP_MODE = "distinct"   # "distinct" = owners who closed YTD; else "fixed"
FIXED_REPS = {"New Logo Sales (Platform)": 4,
              "Atlas / Cloud Upgrades": 4,
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
def is_last_friday(d):
    """True if d is a Friday and the next Friday falls in a different month."""
    return d.weekday() == 4 and (d + dt.timedelta(days=7)).month != d.month


def build_window(today=None):
    """Year-to-date months. Returns (year, month_numbers, labels, start, end)."""
    today = today or dt.date.today()
    year = today.year
    months = list(range(1, today.month + 1))          # 1..current month
    labels = [calendar.month_abbr[m] for m in months]  # Jan, Feb, ... portable
    start = dt.date(year, 1, 1)
    return year, months, labels, start, today


def parse_hs_date(value):
    """HubSpot returns ISO 8601 strings (sometimes epoch ms). Handle both."""
    s = str(value)
    if s.isdigit():
        return dt.datetime.utcfromtimestamp(int(s) / 1000).date()
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()


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


def get_acquisitions(year, months, start, end):
    lo, hi = ms_bounds(start, end)
    n = len(months)
    idx = {m: i for i, m in enumerate(months)}
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
        acq = [0] * n
        arr = [0.0] * n
        owners = set()
        for d in search(filters, ["hs_arr", "closedate", "hubspot_owner_id"]):
            p = d["properties"]
            cd = parse_hs_date(p["closedate"])
            if cd.year != year or cd.month not in idx:
                continue
            i = idx[cd.month]
            acq[i] += 1
            arr[i] += float(p.get("hs_arr") or 0)
            if p.get("hubspot_owner_id"):
                owners.add(p["hubspot_owner_id"])
        reps = FIXED_REPS.get(seg["name"], len(owners)) if REP_MODE == "fixed" \
            else len(owners)
        segments.append({"name": seg["name"], "reps": reps,
                         "acq": acq, "arr": arr})
    return segments


def get_creation(year, months, start, end):
    lo, hi = ms_bounds(start, end)
    n = len(months)
    idx = {m: i for i, m in enumerate(months)}
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
        counts = [0] * n
        for d in search(filters, ["createdate"]):
            cd = parse_hs_date(d["properties"]["createdate"])
            if cd.year == year and cd.month in idx:
                counts[idx[cd.month]] += 1
        rows.append({"name": st["name"], "counts": counts})
    return rows


# ---- send -------------------------------------------------------------
def send(path, through_label):
    with open(path, "rb") as fh:
        content = base64.b64encode(fh.read()).decode()
    body = (f"<p style='font-family:Arial'>DSN GTM monthly through "
            f"<b>{through_label}</b>. Editable PowerPoint attached: acquisitions "
            f"and productivity by month, plus deal creation by pipeline.</p>")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                 "Content-Type": "application/json"},
        json={"from": EMAIL_FROM, "to": [EMAIL_TO],
              "subject": f"DSN GTM Monthly (through {through_label})",
              "html": body,
              "attachments": [{"filename": f"dsn-gtm-monthly-{through_label}.pptx",
                               "content": content}]},
        timeout=30,
    )
    r.raise_for_status()
    print("Sent:", r.json().get("id"))


def main():
    today = dt.date.today()
    forced = os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes")
    if not forced and not is_last_friday(today):
        print(f"{today} is not the last Friday of the month; skipping send. "
              f"Set FORCE_SEND=1 to override.")
        return

    year, months, labels, start, end = build_window(today)
    through_label = f"{calendar.month_abbr[months[-1]]} {year}"
    acq = get_acquisitions(year, months, start, end)
    create = get_creation(year, months, start, end)

    path = os.path.join(tempfile.gettempdir(), "dsn-gtm-monthly.pptx")
    build_deck(labels, acq, create, path,
               period_label=f"{year} YTD", by_word="Month")
    send(path, through_label)


if __name__ == "__main__":
    main()
