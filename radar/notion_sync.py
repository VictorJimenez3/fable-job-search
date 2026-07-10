"""Write applied jobs into the user's Notion Applications database.

The database is resolved by *search*, not a hardcoded ID: Notion's search API
only returns objects actually shared with the integration, so whichever
database has "application" in its title and is connected to "Job Radar" is
found automatically. This means renaming or recreating the tracker (e.g. a
"fresh slate" database) never requires a code change — the resolution is
cached in state/notion_db.json and self-heals by re-searching if the cached
ID stops resolving.

Schema is read live from whatever database is found, and only the properties
that actually exist are populated — so a differently-shaped fresh database
degrades gracefully instead of crashing (Company + a status/select "stage"
property are the only two considered required).

Requires NOTION_TOKEN (internal-integration secret the user creates and
shares the database with). Without it, applied entries queue in
state/applied.json with notion_synced=false and sync on a later run.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from . import state
from .config import env, profile
from .score import role_bucket

PAGES_API = "https://api.notion.com/v1/pages"
SEARCH_API = "https://api.notion.com/v1/search"
DB_API = "https://api.notion.com/v1/databases/{}"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}


def _db_title(db_json: dict) -> str:
    return "".join(t.get("plain_text", "") for t in db_json.get("title", [])) or "(untitled)"


def _parse_db(db_json: dict) -> dict:
    props = db_json.get("properties", {})
    return {"id": db_json["id"], "title": _db_title(db_json),
            "properties": {name: meta["type"] for name, meta in props.items()}}


def resolve_database(token: str, name_hint: str = "application") -> dict:
    """Find the connected Applications database. Cached; self-heals on rename."""
    cached = state.load("notion_db.json", {})
    if cached.get("id"):
        r = requests.get(DB_API.format(cached["id"]), headers=_headers(token), timeout=20)
        if r.status_code == 200:
            return _parse_db(r.json())
        # cached ID no longer resolves (renamed/recreated database) — fall through and re-search

    r = requests.post(SEARCH_API, headers=_headers(token),
                      json={"filter": {"value": "database", "property": "object"}, "page_size": 25},
                      timeout=20)
    r.raise_for_status()
    candidates = [res for res in r.json().get("results", [])
                  if name_hint in _db_title(res).lower()]
    if not candidates:
        raise RuntimeError(
            f"no database with '{name_hint}' in its title is shared with the Job Radar "
            "integration. Fix: open the database in Notion -> '...' menu -> Connections "
            "-> connect 'Job Radar'.")
    candidates.sort(key=lambda c: c.get("last_edited_time", ""), reverse=True)
    info = _parse_db(candidates[0])
    state.save("notion_db.json", {"id": info["id"], "title": info["title"],
                                   "resolved_at": int(time.time())})
    return info


def _position_options(title: str) -> list[dict]:
    pmap = profile()["notion"]["position_map"]
    bucket = role_bucket(title) or "swe"
    name = pmap.get(bucket) or pmap["swe"]
    return [{"name": name}]


def build_payload(entry: dict, schema: dict) -> dict:
    """Only populate properties that actually exist in the target database."""
    n = profile()["notion"]
    props = schema["properties"]
    payload_props: dict = {}

    if props.get("Company") == "title":
        payload_props["Company"] = {"title": [{"text": {"content": entry["company"][:200]}}]}
    if props.get("Stage") == "status":
        payload_props["Stage"] = {"status": {"name": n["stage_applied"]}}
    elif props.get("Status") == "status":
        payload_props["Status"] = {"status": {"name": n["stage_applied"]}}
    if props.get("Position") == "multi_select":
        payload_props["Position"] = {"multi_select": _position_options(entry["title"])}
    if props.get("Apply date") == "date":
        payload_props["Apply date"] = {"date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}}
    if props.get("Text") == "rich_text":
        payload_props["Text"] = {"rich_text": [{"text": {"content":
            f"{entry['title'][:150]} · via JobRadar (score {entry.get('score', '?')}, "
            f"source {entry.get('source', '?')})"}}]}
    if entry.get("url") and props.get("Job URL") == "url":
        payload_props["Job URL"] = {"url": entry["url"][:1900]}
    loc = (entry.get("locations") or [""])[0]
    if loc and props.get("Location") == "rich_text":
        payload_props["Location"] = {"rich_text": [{"text": {"content": loc[:200]}}]}

    return {"parent": {"database_id": schema["id"]}, "properties": payload_props}


def verify_connection() -> None:
    """Read-only check: does NOTION_TOKEN exist and can it find + read an
    Applications database? Prints a clear diagnosis and exits nonzero on
    failure. Creates nothing — safe to run anytime."""
    token = env("NOTION_TOKEN")
    if not token:
        print("FAIL: NOTION_TOKEN secret is not set on this repo.")
        raise SystemExit(1)
    try:
        schema = resolve_database(token)
    except RuntimeError as e:
        print(f"FAIL: {e}")
        raise SystemExit(1)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print("FAIL: token is invalid or was revoked. Fix: generate a new secret at "
                  "notion.so/my-integrations and update the NOTION_TOKEN repo secret.")
        else:
            print(f"FAIL: unexpected error resolving database: {e}")
        raise SystemExit(1)

    props = schema["properties"]
    has_stage = props.get("Stage") == "status" or props.get("Status") == "status"
    missing_required = [n for n, ok in [("Company (title)", props.get("Company") == "title"),
                                        ("Stage/Status (status)", has_stage)] if not ok]
    print(f"OK: found database '{schema['title']}' ({schema['id']}).")
    print(f"    properties: {', '.join(sorted(props)) or '(none)'}")
    if missing_required:
        print(f"FAIL: missing required propert{'y' if len(missing_required)==1 else 'ies'}: "
              f"{', '.join(missing_required)}. Applied-logging needs at least a title "
              "column and a status column.")
        raise SystemExit(1)
    optional_present = [n for n in ("Position", "Apply date", "Text", "Job URL", "Location")
                        if n in props]
    optional_missing = [n for n in ("Position", "Apply date", "Text", "Job URL", "Location")
                        if n not in props]
    print(f"    will populate: Company, Stage" + (f", {', '.join(optional_present)}" if optional_present else ""))
    if optional_missing:
        print(f"    skipping (not in this database): {', '.join(optional_missing)}")
    print("Applied-logging is armed.")


def sync_applied(applied: list) -> int:
    """Push all unsynced applied entries to Notion. Returns count synced."""
    token = env("NOTION_TOKEN")
    if not token:
        pending = sum(1 for a in applied if not a.get("notion_synced"))
        if pending:
            print(f"notion: NOTION_TOKEN not set — {pending} applied entries queued locally")
        return 0
    pending = [a for a in applied if not a.get("notion_synced")]
    if not pending:
        return 0
    try:
        schema = resolve_database(token)
    except Exception as e:
        print(f"notion: could not resolve Applications database, leaving entries queued: {e}")
        return 0

    headers = _headers(token)
    synced = 0
    for entry in pending:
        try:
            r = requests.post(PAGES_API, headers=headers, json=build_payload(entry, schema), timeout=20)
            r.raise_for_status()
            entry["notion_synced"] = True
            entry["notion_page"] = r.json().get("url", "")
            synced += 1
        except Exception as e:
            print(f"notion: failed to sync {entry.get('company')}: {e}")
    return synced
