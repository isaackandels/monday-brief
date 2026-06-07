#!/usr/bin/env python3
"""HubSpot REST client for the Monday Pipeline Brief.

Read-only. Search + pagination + v4 batch associations + engagement pull.
Token comes from the HUBSPOT_TOKEN env var, or a local .hubspot_token file
(gitignored) for local verification runs. Never hardcode the token.
"""
import os
import time

import requests

BASE = "https://api.hubapi.com"

# Reference IDs (from BUILD spec section 3)
REP_OWNER_IDS = ["403415850", "12742684", "530210063", "79812036", "1494239239"]
PIPELINE_NEW = "default"
PIPELINE_EXISTING = "647046539"
CLOSED_NEW = ["closedwon", "952722998", "closedlost", "1338074758"]
CLOSED_EXISTING = ["952805189", "952785055", "952805190", "1119649101"]

DEAL_PROPERTIES = [
    "dealname", "hubspot_owner_id", "pipeline", "dealstage",
    "existing_customer_deal_type", "notes_last_contacted",
    "notes_next_activity_date", "hs_arr",
]
CONTACT_LEAD_PROPERTIES = [
    "firstname", "lastname", "jobtitle", "hs_lead_status", "lifecyclestage",
    "hs_latest_source", "createdate", "num_contacted_notes", "hubspot_owner_id",
]
COMPANY_PROPERTIES = [
    "name", "current_pms", "practice_type", "number_of_locations", "former_pms",
]
CONTACT_ENRICH_PROPERTIES = ["firstname", "lastname", "jobtitle", "email"]

# Engagement object types pulled for the staleness clock (section 5b)
ENGAGEMENT_TYPES = ["notes", "calls", "emails", "meetings", "tasks"]

# Property-history sourceTypes that indicate an automated (non-human) change.
AUTOMATION_SOURCES = {
    "AUTOMATION_PLATFORM", "WORKFLOW", "IMPORT", "MIGRATION", "BATCH",
    "INTEGRATION", "API",
}


def _load_token(token=None):
    if token:
        return token
    tok = os.environ.get("HUBSPOT_TOKEN")
    if tok:
        return tok.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".hubspot_token"), ".hubspot_token"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                val = f.read().strip()
            if val and val != "PASTE_YOUR_HUBSPOT_SERVICE_KEY_HERE":
                return val
    raise RuntimeError(
        "No HubSpot token. Set HUBSPOT_TOKEN or put the key in a local "
        ".hubspot_token file (gitignored)."
    )


class HubSpotClient:
    def __init__(self, token=None, max_retries=5):
        self.token = _load_token(token)
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })

    # ---- low-level HTTP with 429 / 5xx backoff --------------------------
    def _request(self, method, path, **kwargs):
        url = path if path.startswith("http") else BASE + path
        for attempt in range(self.max_retries):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 30))
                continue
            if not resp.ok:
                raise requests.HTTPError(
                    f"{resp.status_code} {method} {url}: {resp.text[:500]}"
                )
            return resp.json() if resp.text else {}
        raise requests.HTTPError(f"Exhausted retries for {method} {url}")

    def _post(self, path, payload):
        return self._request("POST", path, json=payload)

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    # ---- search with pagination ----------------------------------------
    def _search_all(self, object_type, payload):
        results = []
        payload = dict(payload)
        while True:
            data = self._post(f"/crm/v3/objects/{object_type}/search", payload)
            results.extend(data.get("results", []))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
            payload["after"] = after
        return results

    def search_deals(self, cutoff_ms):
        """Two OR'd filter groups (section 3). Over-selects on purpose."""
        cutoff = str(cutoff_ms)
        payload = {
            "filterGroups": [
                {"filters": [
                    {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_NEW},
                    {"propertyName": "hubspot_owner_id", "operator": "IN", "values": REP_OWNER_IDS},
                    {"propertyName": "dealstage", "operator": "NOT_IN", "values": CLOSED_NEW},
                    {"propertyName": "notes_last_contacted", "operator": "LTE", "value": cutoff},
                ]},
                {"filters": [
                    {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_EXISTING},
                    {"propertyName": "hubspot_owner_id", "operator": "IN", "values": REP_OWNER_IDS},
                    {"propertyName": "existing_customer_deal_type", "operator": "IN",
                     "values": ["Cloud Conversion", "Atlas Conversion"]},
                    {"propertyName": "dealstage", "operator": "NOT_IN", "values": CLOSED_EXISTING},
                    {"propertyName": "notes_last_contacted", "operator": "LTE", "value": cutoff},
                ]},
            ],
            "properties": DEAL_PROPERTIES,
            "sorts": [{"propertyName": "notes_last_contacted", "direction": "ASCENDING"}],
            "limit": 100,
        }
        return self._search_all("deals", payload)

    def search_cold_leads(self):
        """Lead/MQL contacts owned by the 5 reps with 0-1 logged contacts."""
        payload = {
            "filterGroups": [
                {"filters": [
                    {"propertyName": "hubspot_owner_id", "operator": "IN", "values": REP_OWNER_IDS},
                    {"propertyName": "lifecyclestage", "operator": "IN",
                     "values": ["lead", "marketingqualifiedlead"]},
                    {"propertyName": "num_contacted_notes", "operator": "LTE", "value": "1"},
                ]},
            ],
            "properties": CONTACT_LEAD_PROPERTIES,
            "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
            "limit": 100,
        }
        return self._search_all("contacts", payload)

    # ---- v4 batch associations -----------------------------------------
    def batch_associations(self, from_type, to_type, ids):
        """Return {fromId: [{"id":..., "types":[...]}]} across all ids."""
        out = {}
        for chunk in _chunks(ids, 100):
            payload = {"inputs": [{"id": str(i)} for i in chunk]}
            data = self._post(
                f"/crm/v4/associations/{from_type}/{to_type}/batch/read", payload
            )
            for row in data.get("results", []):
                src = str(row.get("from", {}).get("id"))
                out[src] = row.get("to", [])
        return out

    def batch_read(self, object_type, ids, properties):
        """Return {id: {prop: value}} for the given object ids."""
        out = {}
        ids = [str(i) for i in ids]
        for chunk in _chunks(ids, 100):
            payload = {
                "properties": properties,
                "inputs": [{"id": i} for i in chunk],
            }
            data = self._post(f"/crm/v3/objects/{object_type}/batch/read", payload)
            for row in data.get("results", []):
                out[str(row["id"])] = row.get("properties", {})
        return out

    def deal_stage_history(self, deal_id):
        """Latest *manual* dealstage change timestamp (ISO) from property history.

        Workflow/automation stage moves are excluded so they do not reset the
        staleness clock (BUILD section 5b).
        """
        data = self._get(
            f"/crm/v3/objects/deals/{deal_id}",
            params={"propertiesWithHistory": "dealstage"},
        )
        history = data.get("propertiesWithHistory", {}).get("dealstage", [])
        stamps = [
            h.get("timestamp") for h in history
            if h.get("timestamp") and h.get("sourceType") not in AUTOMATION_SOURCES
        ]
        return max(stamps) if stamps else None


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


if __name__ == "__main__":
    # Self-test: confirm the deal search returns real records (section 3).
    import sys
    from datetime import datetime, timezone, timedelta

    STALE_DAYS = 45
    now = datetime.now(timezone.utc)
    cutoff_ms = int((now - timedelta(days=STALE_DAYS)).timestamp() * 1000)

    client = HubSpotClient()
    print(f"cutoff_ms = {cutoff_ms}  ({(now - timedelta(days=STALE_DAYS)).date()})")
    deals = client.search_deals(cutoff_ms)
    print(f"deal search returned {len(deals)} records\n")

    if not deals:
        print("WARNING: zero deals returned — check token scopes / filters.")
        sys.exit(1)

    by_owner = {}
    for d in deals:
        p = d.get("properties", {})
        by_owner.setdefault(p.get("hubspot_owner_id"), 0)
        by_owner[p.get("hubspot_owner_id")] += 1

    print("first 5 records:")
    for d in deals[:5]:
        p = d.get("properties", {})
        print(f"  id={d['id']}  owner={p.get('hubspot_owner_id')}  "
              f"pipeline={p.get('pipeline')}  stage={p.get('dealstage')}  "
              f"last_contacted={p.get('notes_last_contacted')}  "
              f"name={(p.get('dealname') or '')[:50]!r}")

    print("\nper-owner counts (first-pass, before engagement filter):")
    for oid, n in sorted(by_owner.items(), key=lambda x: -x[1]):
        print(f"  {oid}: {n}")
