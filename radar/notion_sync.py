"""Write applied jobs into the user's existing Notion 'Applications' database.

Schema discovered from the live tracker (see profile.yaml `notion`):
  Company (title) · Position (multi_select) · Stage (status) · Job URL (url)
  Location (rich_text) · Apply date (date) · Text (rich_text)

Requires NOTION_TOKEN (internal-integration secret the user creates and
shares the database with). Without it, applied entries queue in
state/applied.json with notion_synced=false and sync on a later run.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from .config import env, profile
from .score import role_bucket

API = "https://api.notion.com/v1/pages"


def _position_options(title: str) -> list[dict]:
    pmap = profile()["notion"]["position_map"]
    bucket = role_bucket(title) or "swe"
    name = pmap.get(bucket) or pmap["swe"]
    return [{"name": name}]


def build_payload(entry: dict) -> dict:
    n = profile()["notion"]
    props = {
        "Company": {"title": [{"text": {"content": entry["company"][:200]}}]},
        "Stage": {"status": {"name": n["stage_applied"]}},
        "Position": {"multi_select": _position_options(entry["title"])},
        "Apply date": {"date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}},
        "Text": {"rich_text": [{"text": {"content":
            f"{entry['title'][:150]} · via JobRadar (score {entry.get('score', '?')}, "
            f"source {entry.get('source', '?')})"}}]},
    }
    if entry.get("url"):
        props["Job URL"] = {"url": entry["url"][:1900]}
    loc = (entry.get("locations") or [""])[0]
    if loc:
        props["Location"] = {"rich_text": [{"text": {"content": loc[:200]}}]}
    return {"parent": {"database_id": n["database_id"]}, "properties": props}


def verify_connection() -> None:
    """Read-only check: does NOTION_TOKEN exist and can it see the Applications
    database? Prints a clear diagnosis and exits nonzero on failure. Creates
    nothing — safe to run anytime."""
    token = env("NOTION_TOKEN")
    if not token:
        print("FAIL: NOTION_TOKEN secret is not set on this repo.")
        raise SystemExit(1)
    db_id = profile()["notion"]["database_id"]
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
    r = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=headers, timeout=20)
    if r.status_code == 200:
        title = "".join(t.get("plain_text", "") for t in r.json().get("title", []))
        print(f"OK: token is valid and can see database '{title}'. Applied-logging is armed.")
        return
    if r.status_code == 404:
        print("FAIL: token is valid but the integration is not connected to the "
              "Applications database. Fix: open the database in Notion -> '...' menu "
              "-> Connections -> connect 'Job Radar'.")
    elif r.status_code == 401:
        print("FAIL: token is invalid or was revoked. Fix: generate a new secret at "
              "notion.so/my-integrations and update the NOTION_TOKEN repo secret.")
    else:
        print(f"FAIL: unexpected response {r.status_code}: {r.text[:300]}")
    raise SystemExit(1)


def sync_applied(applied: list) -> int:
    """Push all unsynced applied entries to Notion. Returns count synced."""
    token = env("NOTION_TOKEN")
    if not token:
        pending = sum(1 for a in applied if not a.get("notion_synced"))
        if pending:
            print(f"notion: NOTION_TOKEN not set — {pending} applied entries queued locally")
        return 0
    headers = {"Authorization": f"Bearer {token}",
               "Notion-Version": "2022-06-28",
               "Content-Type": "application/json"}
    synced = 0
    for entry in applied:
        if entry.get("notion_synced"):
            continue
        try:
            r = requests.post(API, headers=headers, json=build_payload(entry), timeout=20)
            r.raise_for_status()
            entry["notion_synced"] = True
            entry["notion_page"] = r.json().get("url", "")
            synced += 1
        except Exception as e:
            print(f"notion: failed to sync {entry.get('company')}: {e}")
    return synced
