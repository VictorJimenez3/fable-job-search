"""Google Sheets application tracker adapter (OAuth refresh-token flow).

The default tracker remains Notion. Set ``TRACKER_BACKEND=google_sheets`` and
the four documented Google secrets to use a Sheet instead. The adapter uses
only HTTPS APIs and ``requests`` so it adds no heavyweight Google dependency.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from urllib.parse import quote

import requests

from .config import env, profile

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
HEADERS = ["Job Radar ID", "Company", "Title", "Stage", "Job URL", "Location",
           "Applied At", "Updated At", "Source", "Board"]
STAGES = {"saved", "applied", "oa", "interview", "rejected", "closed"}


def configured() -> bool:
    return all(env(name) for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                                      "GOOGLE_REFRESH_TOKEN", "GOOGLE_SHEET_ID"))


def _access_token() -> str:
    response = requests.post(TOKEN_URL, timeout=20, data={
        "client_id": env("GOOGLE_CLIENT_ID"),
        "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": env("GOOGLE_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    })
    response.raise_for_status()
    return response.json()["access_token"]


def _tab() -> str:
    return env("GOOGLE_SHEET_TAB", "Applications")


def _range_url(cell_range: str, suffix: str = "") -> str:
    sheet = env("GOOGLE_SHEET_ID")
    named_range = quote(f"{_tab()}!{cell_range}", safe="")
    return f"{SHEETS_API}/{sheet}/values/{named_range}{suffix}"


def _values(token: str) -> list[list[str]]:
    response = requests.get(_range_url("A:J"), timeout=30,
                            headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    return response.json().get("values") or []


def _put(token: str, cell_range: str, rows: list[list[str]]) -> None:
    response = requests.put(_range_url(cell_range), timeout=30,
                            params={"valueInputOption": "RAW"},
                            headers={"Authorization": f"Bearer {token}"},
                            json={"range": f"{_tab()}!{cell_range}",
                                  "majorDimension": "ROWS", "values": rows})
    response.raise_for_status()


def _append(token: str, rows: list[list[str]]) -> None:
    response = requests.post(_range_url("A:J", ":append"), timeout=30,
                             params={"valueInputOption": "RAW",
                                     "insertDataOption": "INSERT_ROWS"},
                             headers={"Authorization": f"Bearer {token}"},
                             json={"majorDimension": "ROWS", "values": rows})
    response.raise_for_status()


def _iso(epoch: int | None) -> str:
    return (datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
            if epoch else "")


def _row(entry: dict, now: int) -> list[str]:
    return [str(entry.get("id", "")), str(entry.get("company", "")),
            str(entry.get("title", "")), str(entry.get("stage") or "applied"),
            str(entry.get("url", "")), str((entry.get("locations") or [""])[0]),
            _iso(entry.get("applied_at")), _iso(now), str(entry.get("source", "")),
            str(profile().get("profile_mode", "main"))]


def sync_applied(applied: list) -> int:
    """Create/update tracker rows by stable Job Radar ID."""
    if not configured():
        if applied:
            print("google-sheets: selected but OAuth secrets/Sheet ID are incomplete")
        return 0
    try:
        token = _access_token()
        rows = _values(token)
        if not rows:
            _put(token, "A1:J1", [HEADERS])
            rows = [HEADERS]
        by_id = {str(row[0]): i + 1 for i, row in enumerate(rows[1:], start=1) if row}
        changed, appends = 0, []
        now = int(time.time())
        for entry in applied:
            jid = str(entry.get("id", ""))
            if not jid:
                continue
            desired = _row(entry, now)
            row_number = by_id.get(jid)
            if row_number is None:
                appends.append(desired)
                changed += 1
            else:
                current = (rows[row_number - 1] + [""] * len(HEADERS))[:len(HEADERS)]
                # Updated At is intentionally ignored in equality checks.
                if current[:7] + current[8:] != desired[:7] + desired[8:]:
                    _put(token, f"A{row_number}:J{row_number}", [desired])
                    changed += 1
            entry["sheet_synced"] = True
            entry["sheet_stage"] = entry.get("stage") or "applied"
        if appends:
            _append(token, appends)
        return changed
    except Exception as exc:
        print(f"google-sheets: sync failed; local entries remain queued: {exc}")
        return 0


def sync_from_sheet(applied: list) -> int:
    """Pull stage edits from the configured Sheet, matched only by radar ID."""
    if not configured() or not applied:
        return 0
    try:
        rows = _values(_access_token())
    except Exception as exc:
        print(f"google-sheets: readback failed; local stages unchanged: {exc}")
        return 0
    stages = {str(row[0]): str(row[3]).strip().lower()
              for row in rows[1:] if len(row) >= 4 and str(row[3]).strip().lower() in STAGES}
    now, changed = int(time.time()), 0
    for entry in applied:
        stage = stages.get(str(entry.get("id", "")))
        if not stage:
            continue
        old = entry.get("stage") or "applied"
        entry["sheet_stage"] = stage
        entry["sheet_read_at"] = now
        if stage != old:
            entry["stage"] = stage
            entry["status"] = stage
            if stage in {"oa", "interview", "rejected", "closed"} and not entry.get("responded_at"):
                entry["responded_at"] = now
            changed += 1
    return changed
