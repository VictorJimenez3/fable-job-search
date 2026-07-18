"""Write tracked jobs into the user's Notion Applications database.

Every entry in state/applied.json carries a stage: "saved" (checkbox on an
alert — the candidate is tracking it, hasn't applied yet) or "applied" (an explicit
`applied` comment or, later, email confirmation). Saved entries get a Notion
page immediately with the not-yet-applied status; the candidate flips the status in
Notion when he applies. If the radar itself later learns an entry was applied,
sync patches the existing page's status instead of creating a duplicate.

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

import re
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
            "properties": {name: meta["type"] for name, meta in props.items()},
            # status options can't be created via the API, so record what the
            # live database actually offers and validate against it
            "status_options": {name: [o.get("name", "") for o in
                                      meta.get("status", {}).get("options", [])]
                               for name, meta in props.items()
                               if meta.get("type") == "status"}}


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
    bucket = role_bucket(title) or "default"
    name = pmap.get(bucket) or pmap.get("default") or "Engineering Internship"
    return [{"name": name}]


def _stage_property(schema: dict) -> str | None:
    for name in ("Stage", "Status"):
        if schema["properties"].get(name) == "status":
            return name
    return None


def _stage_option(entry: dict, schema: dict, prop: str) -> str | None:
    """Status name for this entry, or None to omit the property (the database
    then applies its own default status, typically 'Not started')."""
    n = profile()["notion"]
    if entry.get("stage", "applied") == "applied":
        return n["stage_applied"]
    want = n.get("stage_saved", "Not started")
    options = schema.get("status_options", {}).get(prop)
    return want if options is None or want in options else None


def build_payload(entry: dict, schema: dict) -> dict:
    """Only populate properties that actually exist in the target database."""
    props = schema["properties"]
    payload_props: dict = {}

    if props.get("Company") == "title":
        payload_props["Company"] = {"title": [{"text": {"content": entry["company"][:200]}}]}
    stage_prop = _stage_property(schema)
    if stage_prop:
        name = _stage_option(entry, schema, stage_prop)
        if name:
            payload_props[stage_prop] = {"status": {"name": name}}
    if props.get("Position") == "multi_select":
        payload_props["Position"] = {"multi_select": _position_options(entry["title"])}
    if props.get("Apply date") == "date" and entry.get("stage", "applied") == "applied":
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


def archive_page(token: str, page_id: str) -> None:
    """Archive (soft-delete, reversible from Notion's trash) a page by ID."""
    r = requests.patch(f"https://api.notion.com/v1/pages/{page_id}",
                       headers=_headers(token), json={"archived": True}, timeout=20)
    r.raise_for_status()


PAGE_ID_RE = re.compile(r"([0-9a-f]{32})\s*$", re.I)


def page_id_from_url(url: str) -> str | None:
    m = PAGE_ID_RE.search(url or "")
    if not m:
        return None
    h = m.group(1)
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def stage_status_name(stage_key: str) -> str | None:
    """Notion Stage option name for an internal stage key, or None if unmapped."""
    n = profile()["notion"]
    return {
        "saved": n.get("stage_saved"),
        "applied": n.get("stage_applied"),
        "oa": n.get("stage_oa", "OA"),
        "interview": n.get("stage_interview", "Interview"),
        "rejected": n.get("stage_rejected", "Rejected"),
        "closed": n.get("stage_closed", "CLOSED"),
    }.get(stage_key)


# response-bearing stages record a Response date; only "applied" sets Apply date
_RESPONSE_STAGES = {"oa", "interview", "rejected", "closed"}


def _needs_stage_patch(entry: dict) -> bool:
    """A synced page whose Notion stage no longer matches the entry's stage."""
    return (bool(entry.get("notion_synced"))
            and entry.get("stage") is not None
            and entry.get("stage") != entry.get("notion_stage")
            and stage_status_name(entry.get("stage")) is not None)


def sync_applied(applied: list) -> int:
    """Push unsynced entries to Notion (and patch stage changes). Returns count."""
    token = env("NOTION_TOKEN")
    if not token:
        pending = sum(1 for a in applied if not a.get("notion_synced"))
        if pending:
            print(f"notion: NOTION_TOKEN not set — {pending} tracked entries queued locally")
        return 0
    pending = [a for a in applied if not a.get("notion_synced")]
    promotions = [a for a in applied if _needs_stage_patch(a)]
    if not pending and not promotions:
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
            entry["notion_stage"] = entry.get("stage", "applied")
            entry["notion_page"] = r.json().get("url", "")
            synced += 1
        except Exception as e:
            print(f"notion: failed to sync {entry.get('company')}: {e}")

    stage_prop = _stage_property(schema)
    options = schema.get("status_options", {}).get(stage_prop) if stage_prop else None
    for entry in promotions:
        page_id = page_id_from_url(entry.get("notion_page", ""))
        target = entry.get("stage")
        name = stage_status_name(target)
        if not page_id or not stage_prop or not name:
            continue
        if options is not None and name not in options:
            print(f"notion: '{name}' not an option in your Stage column; skipping "
                  f"{entry.get('company')}")
            continue
        props: dict = {stage_prop: {"status": {"name": name}}}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if target == "applied" and schema["properties"].get("Apply date") == "date":
            props["Apply date"] = {"date": {"start": today}}
        if target in _RESPONSE_STAGES and schema["properties"].get("Response date") == "date":
            resp = entry.get("responded_at")
            when = (datetime.fromtimestamp(resp, timezone.utc).strftime("%Y-%m-%d")
                    if resp else today)
            props["Response date"] = {"date": {"start": when}}
        try:
            r = requests.patch(f"{PAGES_API}/{page_id}", headers=headers,
                               json={"properties": props}, timeout=20)
            r.raise_for_status()
            entry["notion_stage"] = target
            synced += 1
        except Exception as e:
            print(f"notion: failed to set {entry.get('company')} -> {name}: {e}")
    return synced
