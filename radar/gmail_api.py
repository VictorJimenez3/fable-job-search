"""Read-only incremental Gmail API adapter with an IMAP-like surface."""

from __future__ import annotations

import base64
import email
import re
from typing import Any

import requests

from . import state
from .config import env

OAUTH_REFRESH_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
CURSOR_FILE = "email_watch_api.json"


class GmailAPIError(RuntimeError):
    status_code: int | None = None


def configured() -> bool:
    return all(
        env(name)
        for name in (
            "GMAIL_REFRESH_TOKEN",
            "GOOGLE_AUTH_CLIENT_ID",
            "GOOGLE_AUTH_CLIENT_SECRET",
        )
    )


class GmailAPIConnection:
    """Enough of IMAP's read-only contract for ``email_watch.run``.

    Gmail history IDs avoid rescanning the same lookback window. The cursor is
    committed only after the caller finishes processing every returned MIME
    message, so a failed run cannot acknowledge unseen mail.
    """

    def __init__(self, session: requests.Session | None = None):
        if not configured():
            raise GmailAPIError("Gmail API refresh credentials are not configured")
        self.http = session or requests.Session()
        self.access_token = self._refresh_access_token()
        self.pending_history_id = ""
        self.can_commit = True

    def _refresh_access_token(self) -> str:
        response = self.http.post(
            OAUTH_REFRESH_URL,
            data={
                "client_id": env("GOOGLE_AUTH_CLIENT_ID"),
                "client_secret": env("GOOGLE_AUTH_CLIENT_SECRET"),
                "refresh_token": env("GMAIL_REFRESH_TOKEN"),
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        if response.status_code >= 400:
            raise GmailAPIError(f"Google token refresh returned {response.status_code}")
        token = str((response.json() or {}).get("access_token") or "")
        if not token:
            raise GmailAPIError("Google token refresh returned no access token")
        return token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        response = self.http.get(
            f"{GMAIL_API}/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=30,
        )
        if response.status_code >= 400:
            error = GmailAPIError(f"Gmail API returned {response.status_code}")
            error.status_code = response.status_code
            raise error
        return response.json() or {}

    def select(self, *_args, **_kwargs):
        profile = self._get("profile")
        self.pending_history_id = str(profile.get("historyId") or "")
        return "OK", [str(profile.get("messagesTotal") or 0).encode()]

    def _initial_ids(self, lookback_days: int) -> list[str]:
        query = (
            f"in:anywhere newer_than:{lookback_days}d "
            "(application OR interview OR assessment OR unfortunately OR recruiter)"
        )
        return self._paged_ids("messages", {"q": query, "includeSpamTrash": "false"})

    def _history_ids(self, history_id: str) -> list[str]:
        return self._paged_ids(
            "history",
            {"startHistoryId": history_id, "historyTypes": "messageAdded"},
            history=True,
        )

    def _paged_ids(self, path: str, params: dict[str, Any], *, history: bool = False) -> list[str]:
        maximum = max(25, min(1000, int(env("RADAR_GMAIL_MAX_MESSAGES", "500"))))
        ids: list[str] = []
        page_token = ""
        while len(ids) < maximum:
            current = dict(params)
            current["maxResults"] = min(500, maximum - len(ids))
            if page_token:
                current["pageToken"] = page_token
            data = self._get(path, current)
            if history:
                for event in data.get("history") or []:
                    ids.extend(
                        str(item.get("message", {}).get("id") or "")
                        for item in event.get("messagesAdded") or []
                    )
                self.pending_history_id = str(data.get("historyId") or self.pending_history_id)
            else:
                ids.extend(str(item.get("id") or "") for item in data.get("messages") or [])
            ids = list(dict.fromkeys(value for value in ids if value))
            page_token = str(data.get("nextPageToken") or "")
            if not page_token:
                break
        if page_token:
            # Do not advance the history cursor when the bounded batch did not
            # cover the whole change set. Seen Message-IDs keep retries cheap.
            self.can_commit = False
        return ids[:maximum]

    def search(self, _charset, *criteria):
        cursor = state.load(CURSOR_FILE, {})
        raw_criteria = " ".join(str(value) for value in criteria)
        match = re.search(r"newer_than:(\d+)d", raw_criteria)
        configured_lookback = int(env("RADAR_EMAIL_LOOKBACK_DAYS", "21"))
        lookback = max(1, min(90, int(match.group(1)) if match else configured_lookback))
        history_id = str(cursor.get("history_id") or "")
        try:
            ids = self._history_ids(history_id) if history_id else self._initial_ids(lookback)
        except GmailAPIError as exc:
            # Gmail expires old history cursors. A bounded query is the
            # documented recovery path; it is still idempotent by Message-ID.
            if getattr(exc, "status_code", None) != 404:
                raise
            ids = self._initial_ids(lookback)
        return "OK", [b" ".join(value.encode() for value in ids)]

    def fetch(self, uid: bytes, _spec: str):
        message_id = uid.decode() if isinstance(uid, bytes) else str(uid)
        data = self._get(f"messages/{message_id}", {"format": "raw"})
        raw = str(data.get("raw") or "")
        if not raw:
            return "NO", [None]
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        # Parse once here as a validation boundary, while returning the byte
        # contract expected by the existing email watcher.
        email.message_from_bytes(decoded)
        return "OK", [(b"gmail-api", decoded)]

    def commit(self) -> None:
        if self.can_commit and self.pending_history_id:
            state.save(CURSOR_FILE, {"history_id": self.pending_history_id})

    def logout(self):
        self.http.close()
        return "OK", [b""]
