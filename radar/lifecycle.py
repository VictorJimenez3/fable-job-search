"""Deterministic posting lifecycle state.

The crawler keeps terminal postings in ``state/jobs*.json`` for analysis, but
removes them from active dashboards, boards, feeds, and alert delivery.  This
module owns the small state machine so the new-grad lane, internship lane,
quality pass, and web surfaces agree on what "open", "expired", and "filled"
mean.

Only definitive dead-page evidence or a conservative source-gap timeout can
close a posting.  A transient network failure is never enough.  Every change
gets an auditable reason and an event entry for future posting-timeline
analysis.
"""
from __future__ import annotations

import re
from collections.abc import MutableMapping

from .config import env

OPEN = "open"
EXPIRED = "expired"
FILLED = "filled"
TERMINAL_STATUSES = frozenset({EXPIRED, FILLED})
VALID_STATUSES = frozenset({OPEN, EXPIRED, FILLED})

_FILLED_RE = re.compile(
    r"\b(?:position|role|job|opening|vacancy)\s+(?:has\s+)?been\s+filled\b|"
    r"\b(?:position|role|job|opening)\s+is\s+filled\b|"
    r"\bwe\s+have\s+filled\s+this\s+(?:position|role)\b",
    re.I,
)
_EXPIRED_RE = re.compile(
    r"\b(?:posting|job|position|role|opening)\s+(?:has\s+)?(?:expired|closed)\b|"
    r"\bno\s+longer\s+(?:accepting\s+applications|available|open)\b|"
    r"\bjob\s+(?:not\s+found|is\s+no\s+longer\s+available)\b",
    re.I,
)

LIFECYCLE_FIELDS = (
    "posting_status", "posting_status_changed_at", "posting_status_reason",
    "closed_at", "last_closed_at", "last_seen_at", "lifecycle_checked_at",
    "lifecycle_events",
)


def _get(target, key: str, default=None):
    if isinstance(target, MutableMapping):
        return target.get(key, default)
    return getattr(target, key, default)


def _set(target, key: str, value) -> None:
    if isinstance(target, MutableMapping):
        target[key] = value
    else:
        setattr(target, key, value)


def _pop(target, key: str) -> None:
    if isinstance(target, MutableMapping):
        target.pop(key, None)
    elif hasattr(target, key):
        setattr(target, key, None)


def _reason_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:240]


def status_from_dead_text(text: str | None) -> str:
    """Classify a definitive dead page without using an AI provider."""
    text = text or ""
    if _FILLED_RE.search(text):
        return FILLED
    return EXPIRED


def status_from_reason(reason: str | None) -> str:
    """Classify a human/archive reason, defaulting safely to expired."""
    return FILLED if _FILLED_RE.search(reason or "") or re.search(r"\bfilled\b", reason or "", re.I) else EXPIRED


def status_of(target) -> str:
    raw = str(_get(target, "posting_status", "") or "").strip().lower()
    if raw in TERMINAL_STATUSES:
        return raw
    if _get(target, "manual_archived") or _get(target, "closed_at"):
        reason = _get(target, "posting_status_reason") or _get(target, "archive_reason")
        return status_from_reason(reason)
    quality = _get(target, "quality", {}) or {}
    if quality.get("live") is False:
        return str(quality.get("dead_status") or EXPIRED)
    if raw == OPEN:
        return OPEN
    return OPEN


def is_terminal(target) -> bool:
    return status_of(target) in TERMINAL_STATUSES or bool(_get(target, "closed_at"))


def _append_reason(target, line: str) -> None:
    reasons = _get(target, "score_reasons")
    if reasons is None:
        reasons = []
        _set(target, "score_reasons", reasons)
    if line not in reasons:
        reasons.append(line)


def _append_event(target, status: str, when: int, reason: str) -> None:
    events = _get(target, "lifecycle_events")
    if not isinstance(events, list):
        events = []
    events.append({"status": status, "at": int(when), "reason": reason})
    # Enough history for timeline features without allowing a malformed page
    # or repeated reopen/close cycle to grow a record without bound.
    _set(target, "lifecycle_events", events[-24:])


def set_status(target, status: str, when: int, reason: str) -> bool:
    """Set a lifecycle status on either a dict record or a ``Job`` object.

    Returns whether the visible status changed.  Reapplying the same evidence
    is idempotent, while the score reason remains present for auditability.
    """
    status = str(status or EXPIRED).strip().lower()
    if status not in VALID_STATUSES:
        status = EXPIRED
    when = int(when)
    reason = _reason_text(reason) or "posting lifecycle check"
    old = status_of(target)
    changed = old != status
    _set(target, "posting_status", status)
    _set(target, "posting_status_reason", reason)
    _set(target, "lifecycle_checked_at", when)
    if changed or not _get(target, "posting_status_changed_at"):
        _set(target, "posting_status_changed_at", when)

    if status in TERMINAL_STATUSES:
        if not _get(target, "closed_at"):
            _set(target, "closed_at", when)
        _set(target, "alert_ok", False)
    elif old in TERMINAL_STATUSES:
        previous_closed = _get(target, "closed_at")
        if previous_closed:
            _set(target, "last_closed_at", previous_closed)
        _pop(target, "closed_at")

    line = f"posting lifecycle: {status} — {reason}"
    _append_reason(target, line)
    # Preserve the long-standing quality/posting audit token for downstream
    # reports and compatibility with stored reason-ledger consumers.
    if "posting gone (link checked)" in reason:
        _append_reason(target, "posting gone (link checked)")
    if changed:
        _append_event(target, status, when, reason)
    return changed


def mark_terminal(target, status: str, when: int, reason: str) -> bool:
    """Record ``expired``/``filled`` evidence and suppress active delivery."""
    status = str(status or EXPIRED).strip().lower()
    if status not in TERMINAL_STATUSES:
        status = EXPIRED
    return set_status(target, status, when, reason)


def touch(target, when: int, source: str = "crawl") -> bool:
    """Mark a posting as seen and reopen an auto-closed role if it reappears."""
    _set(target, "last_seen_at", int(when))
    _set(target, "lifecycle_checked_at", int(when))
    if _get(target, "manual_archived"):
        return False
    if is_terminal(target):
        return set_status(target, OPEN, when, f"posting reappeared in {source}")
    return False


def normalize_record(record: dict, when: int) -> None:
    """Backfill lifecycle fields on pre-lifecycle records without reopening them."""
    raw = str(record.get("posting_status") or "").strip().lower()
    quality = record.get("quality") or {}
    if (raw not in VALID_STATUSES or raw == OPEN) and (
            record.get("manual_archived") or record.get("closed_at") or
            quality.get("live") is False):
        status = str(quality.get("dead_status") or status_from_reason(
            record.get("archive_reason") or record.get("posting_status_reason") or
            " ".join(record.get("score_reasons") or [])))
        checked = int(record.get("closed_at") or quality.get("checked_at") or when)
        set_status(record, status, checked, record.get("archive_reason") or
                   "legacy closed posting")
    elif raw not in VALID_STATUSES:
        record["posting_status"] = OPEN
    record.setdefault("last_seen_at", record.get("first_seen"))
    if is_terminal(record):
        record["alert_ok"] = False


def reconcile(jobs: dict, when: int, seen_ids: set[str] | None = None,
              allow_source_gap_expiry: bool = True) -> dict:
    """Normalize records and expire roles absent from feeds for long enough.

    The default 45-day active age plus 14-day source-gap grace is intentionally
    conservative: a single broken source or transient outage cannot delete a
    useful role.  Definitive dead-link evidence is handled immediately by
    ``mark_terminal``.
    """
    seen_ids = seen_ids or set()
    active_days = max(1, int(env("RADAR_LIFECYCLE_ACTIVE_DAYS", "45")))
    grace_days = max(1, int(env("RADAR_LIFECYCLE_UNSEEN_GRACE_DAYS", "14")))
    stats = {"normalized": 0, "expired": 0, "filled": 0, "reopened": 0}
    for jid, record in jobs.items():
        normalize_record(record, when)
        stats["normalized"] += 1
        if jid in seen_ids:
            if not _get(record, "manual_archived"):
                # A terminal transition stamped during this crawl is evidence
                # from the liveness fetch; let it stand until the next crawl.
                if not (is_terminal(record) and
                        _get(record, "posting_status_changed_at") == when):
                    if touch(record, when, "monitored source"):
                        stats["reopened"] += 1
            continue
        if is_terminal(record) or _get(record, "manual_archived"):
            continue
        first = int(record.get("posted_at") or record.get("first_seen") or when)
        last_seen = int(record.get("last_seen_at") or record.get("first_seen") or first)
        old_enough = when - first >= active_days * 86400
        source_gap = when - last_seen >= grace_days * 86400
        if allow_source_gap_expiry and old_enough and source_gap:
            days = max(1, (when - last_seen) // 86400)
            mark_terminal(record, EXPIRED, when,
                          f"not seen in monitored sources for {days} days")
            stats[EXPIRED] += 1
    return stats


def source_run_healthy(aggregator_stats: dict, ats_stats: dict) -> bool:
    """Return whether at least one monitored source completed successfully.

    An all-source outage must not look like a source gap and expire the whole
    retained dataset. Aggregator stats use an integer result count on success;
    ATS stats expose a successful-company count.
    """
    aggregator_ok = any(isinstance(value, (int, float))
                        for value in (aggregator_stats or {}).values())
    ats_ok = int((ats_stats or {}).get("ok", 0) or 0) > 0
    return aggregator_ok or ats_ok


def history_cutoff(when: int) -> int:
    """Unix cutoff for retained terminal/open state (default: two years)."""
    days = max(365, int(env("RADAR_HISTORY_DAYS", "730")))
    return int(when) - days * 86400


def merge_record_metadata(record: dict, old: dict | None) -> None:
    """Carry lifecycle metadata across a manual placeholder upgrade."""
    if not old:
        return
    old_status = status_of(old)
    new_status = status_of(record)
    if old.get("manual_archived") or (old_status in TERMINAL_STATUSES and not record.get("posting_status_changed_at")):
        for key in LIFECYCLE_FIELDS:
            if key in old and (key not in record or key == "posting_status" or
                               not record.get(key)):
                record[key] = old[key]
    if old.get("manual_archived"):
        for key in ("manual_archived", "archived_at", "archived_by", "archive_reason"):
            if key in old:
                record[key] = old[key]
        if new_status == OPEN:
            record["posting_status"] = old_status
            record["closed_at"] = old.get("closed_at")
            record["alert_ok"] = False


def lifecycle_reason(target) -> str:
    return (_reason_text(_get(target, "posting_status_reason")) or
            _reason_text(_get(target, "archive_reason")) or
            "posting removed from active views")
