"""Consolidated views: the master-board issue and the daily-best issue.

Why: individual alert issues are notification surfaces, while this master board
is ONE stable issue holding every open, alert-worthy role — body plus bot comments
as extra pages, rewritten in place each crawl. Its checkboxes carry the same
``<!--radar:ID-->`` markers, so the existing applied-sync workflow tracks
them to Notion (comment-checkbox edits included). The daily-best issue is a
fresh top-10 each evening; because it's assigned to Victor, GitHub's own
notification email doubles as the requested daily "best of" email with zero
mail credentials.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import requests

from . import lifecycle, state
from .alerts import (
    API,
    LABEL,
    _dedupe_alert_rows,
    _existing_marker_ids,
    _expand_marker_ids,
    _headers,
    _load_job_aliases,
    _posting_marker,
    _resolve_job_alias,
    format_line,
    lane_label,
)
from .config import env, github_owner, github_repo, profile, profile_id
from .score import ranking_key

MASTER_TITLE = "📌 Job Radar — master board (every open role, one place)"
MASTER_LABEL = "radar-master"
DAILY_LABEL = "radar-daily"
BATCH_LABEL = "radar-email-batch"
PAGE_LIMIT = 55000  # per body/comment, under GitHub's 65536 cap
REQUEST_TIMEOUT = (5, 20)
SPRING_WINTER_2027 = re.compile(r"\b(?:spring|winter)\s*2027\b", re.I)


def _spring_winter_2027_internship(record: dict) -> bool:
    """Return true only when a role has explicit Spring/Winter 2027 evidence."""
    evidence = record.get("internship_eligibility") or {}
    options = evidence.get("term_options") or evidence.get("terms") or []
    if isinstance(options, str):
        options = [options]
    values = list(options) if isinstance(options, (list, tuple, set)) else []
    actual_start = evidence.get("term_start") or record.get("term_start")
    if actual_start:
        values.append(actual_start)
    # Evidence v2 makes parsed employer terms authoritative. A stale curator
    # source_term must not make a Summer-only role enter the Spring/Winter
    # email batch after rescoring.
    authoritative = evidence.get("evidence_version", 1) >= 2 and bool(values)
    if not authoritative:
        values.append(evidence.get("source_term") or record.get("source_term"))
    for value in values:
        if isinstance(value, str) and value.startswith("2027-01"):
            return True
    if authoritative:
        return False
        if isinstance(value, str) and SPRING_WINTER_2027.search(value):
            return True
    title = record.get("title") or ""
    return bool(SPRING_WINTER_2027.search(title))


def master_label() -> str:
    return "radar-internship-master" if profile_id() == "internship" else MASTER_LABEL


def batch_label() -> str:
    return "radar-internship-email" if profile_id() == "internship" else BATCH_LABEL


def email_enabled() -> bool:
    defaults = {"new_grad_email": True, "internship_email": True}
    preferences = state.load_shared("notification_preferences.json", defaults)
    key = "internship_email" if profile_id() == "internship" else "new_grad_email"
    return bool(preferences.get(key, defaults[key]))

MASTER_HEADER = (
    "Every alert-worthy open role the radar currently knows, best first — "
    "**{count} roles**, refreshed {stamp}. Extra pages live in the comments "
    "below.\n\n"
    "☑️ Checking a box here works exactly like an alert issue: the job "
    "lands in the in-house Pipeline and is mirrored to Notion as not-applied; "
    "flip its status in Notion "
    "when you apply. Already-tracked jobs show up pre-checked.\n")


def _open_rows(jobs_state: dict, now: int, max_age_days: int = 30) -> list[dict]:
    thr = profile()["thresholds"]["alert"]
    rows = [r for r in jobs_state.values()
            if not lifecycle.is_terminal(r)
            and r.get("alert_ok") and r.get("score", 0) >= thr
            and now - r.get("first_seen", now) <= max_age_days * 86400]
    rows.sort(key=ranking_key)
    return rows


def _paginate(lines: list[str], limit: int = PAGE_LIMIT) -> list[str]:
    pages, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit and cur:
            pages.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    pages.append("\n".join(cur))
    return pages


def update_master_board(jobs_state: dict, applied: list) -> str | None:
    """Refresh only the master-board pages whose contents actually changed.

    The master board is intentionally comprehensive, but it must not turn
    every crawl into N sequential writes for N historical comment pages.  The
    current comments are the source of truth; comparing their rendered text
    preserves checkbox semantics and makes a no-change crawl one GET + one
    header patch instead of dozens of API calls.
    """
    token = env("GITHUB_TOKEN")
    if not token:
        print("board: GITHUB_TOKEN not set — skipping master board")
        return None
    repo = github_repo()
    now = int(time.time())
    from .culture import load as culture_load
    culture_map = culture_load()
    applied_ids = {a["id"] for a in applied}

    lines = []
    for r in _open_rows(jobs_state, now):
        line = format_line(r, culture_map)
        if r["id"] in applied_ids:
            line = line.replace("- [ ]", "- [x]", 1)
        lines.append(line)
    pages = _paginate(lines)
    stamp = datetime.now(UTC).strftime("%a %b %d, %H:%M UTC")
    body = MASTER_HEADER.format(count=len(lines), stamp=stamp) + "\n" + pages[0]

    r = requests.get(f"{API}/repos/{repo}/issues",
                     params={"labels": master_label(), "state": "open", "per_page": 5},
                     headers=_headers(), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    found = r.json()
    if found:
        issue = found[0]
        # The refreshed timestamp lives in the first page, so it is the one
        # page intentionally updated every run.  Extra pages below are diffed.
        requests.patch(f"{API}/repos/{repo}/issues/{issue['number']}",
                       headers=_headers(), timeout=REQUEST_TIMEOUT,
                       json={"body": body, "assignees": []}).raise_for_status()
    else:
        r = requests.post(f"{API}/repos/{repo}/issues", headers=_headers(), timeout=REQUEST_TIMEOUT,
                          json={"title": MASTER_TITLE, "body": body,
                                "labels": [LABEL, lane_label(), master_label()], "assignees": []})
        r.raise_for_status()
        issue = r.json()

    # pages 2..n live in the bot's own comments, edited in place
    cr = requests.get(issue["comments_url"], params={"per_page": 100},
                      headers=_headers(), timeout=REQUEST_TIMEOUT)
    cr.raise_for_status()
    own = [c for c in cr.json() if c["user"]["login"].endswith("[bot]")]
    extra_pages = pages[1:]
    updates: list[tuple[str, str, dict]] = []
    unchanged = 0
    for i, page in enumerate(extra_pages):
        text = f"**Page {i + 2}**\n\n{page}"
        if i < len(own):
            if (own[i].get("body") or "") == text:
                unchanged += 1
                continue
            updates.append(("patch", f"{API}/repos/{repo}/issues/comments/{own[i]['id']}", {"body": text}))
        else:
            updates.append(("post", issue["comments_url"], {"body": text}))
    unused = "_(page currently unused)_"
    for c in own[len(extra_pages):]:
        if (c.get("body") or "") != unused:
            updates.append(("patch", f"{API}/repos/{repo}/issues/comments/{c['id']}", {"body": unused}))

    def write(change: tuple[str, str, dict]) -> None:
        method, url, payload = change
        response = (requests.patch if method == "patch" else requests.post)(
            url, headers=_headers(), timeout=REQUEST_TIMEOUT, json=payload)
        response.raise_for_status()

    # Comment pages are independent and GitHub's issue endpoint tolerates
    # modest concurrency.  Four workers keeps the board fast without a burst
    # that would compete with the radar's other API traffic.
    if updates:
        with ThreadPoolExecutor(max_workers=min(4, len(updates)), thread_name_prefix="master-board") as pool:
            futures = [pool.submit(write, change) for change in updates]
            for future in as_completed(futures):
                future.result()
    print(f"board: {len(lines)} role(s), {len(pages)} page(s), "
          f"{len(updates)} comment write(s), {unchanged} unchanged")
    return issue["html_url"]


def post_daily_best(jobs_state: dict, top_n: int = 10) -> str | None:
    """Create today's 🏆 best-of issue (idempotent; closes yesterday's)."""
    if not email_enabled():
        print(f"daily-best: {profile_id()} email is disabled")
        return None
    token = env("GITHUB_TOKEN")
    if not token:
        print("board: GITHUB_TOKEN not set — skipping daily best")
        return None
    repo = github_repo()
    now = int(time.time())
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    title = f"🏆 Best of {today}"

    rows = [r for r in jobs_state.values()
            if not lifecycle.is_terminal(r)
            and r.get("alert_ok") and now - r.get("first_seen", 0) <= 86400]
    rows.sort(key=ranking_key)
    rows = rows[:top_n]
    if not rows:
        print("daily-best: no new alert-worthy roles in the last 24h")
        return None

    r = requests.get(f"{API}/repos/{repo}/issues",
                     params={"labels": DAILY_LABEL, "state": "open", "per_page": 20},
                     headers=_headers(), timeout=20)
    r.raise_for_status()
    open_dailies = r.json()
    if any(i["title"] == title for i in open_dailies):
        print("daily-best: today's issue already exists")
        return next(i["html_url"] for i in open_dailies if i["title"] == title)
    for old in open_dailies:  # yesterday's digest has served its purpose
        requests.patch(f"{API}/repos/{repo}/issues/{old['number']}",
                       headers=_headers(), timeout=20,
                       json={"state": "closed"}).raise_for_status()

    from .culture import load as culture_load
    culture_map = culture_load()
    body = (f"The {len(rows)} strongest roles the radar found in the last 24 hours. "
            "Check a box to track it in Notion.\n\n"
            + "\n".join(format_line(x, culture_map) for x in rows) + "\n")
    r = requests.post(f"{API}/repos/{repo}/issues", headers=_headers(), timeout=20,
                      json={"title": title, "body": body,
                            "labels": [LABEL, DAILY_LABEL],
                            "assignees": [github_owner()]})
    r.raise_for_status()
    return r.json()["html_url"]


def email_batch_rows(alert_history: list[dict], sent_ids: set[str], now: int,
                     limit: int = 15, jobs_state: dict | None = None) -> list[dict]:
    """Choose unsent current alerts: importance first, then freshness.

    ``alert_history`` is an event log and therefore deliberately keeps the
    score/verdict from the crawl that emitted an alert.  The jobs snapshot is
    the authority for what is still alertable; use the history row only for
    the alert timestamp and the first-sighting fallback.  This prevents a
    later rescore or lifecycle transition from leaking a stale role into an
    email batch.  Duplicate history rows (including alias remaps) collapse to
    their newest event before selection.
    """
    aliases = _load_job_aliases()
    sent_ids = _expand_marker_ids(set(sent_ids), aliases)
    latest: dict[str, dict] = {}
    for index, record in enumerate(alert_history):
        raw_id = str(record.get("id") or "")
        if not raw_id:
            continue
        job_id = _resolve_job_alias(raw_id, aliases)
        normalized = {**record, "id": job_id, "_history_index": index}
        previous = latest.get(job_id)
        if previous is None or (
                int(record.get("alerted_at", 0) or 0), index
        ) >= (
                int(previous.get("alerted_at", 0) or 0),
                int(previous.get("_history_index", -1)),
        ):
            # Keep the index private to this selection pass so equal-timestamp
            # duplicate records consistently prefer the later history entry.
            latest[job_id] = normalized

    rows = []
    threshold = int(profile()["thresholds"]["alert"])
    for history_row in latest.values():
        job_id = str(history_row.get("id") or "")
        if (job_id in sent_ids
                or not (0 < now - history_row.get("alerted_at", 0) <= 14 * 86400)):
            continue
        current = (jobs_state or {}).get(job_id)
        if jobs_state is not None and (current is None or lifecycle.is_terminal(current)):
            continue
        if jobs_state is None and lifecycle.is_terminal(history_row):
            continue
        if jobs_state is not None:
            # Compact state omits false defaults, so an absent alert_ok is an
            # explicit demotion rather than an unknown value.
            if not current.get("alert_ok") or int(current.get("score", 0) or 0) < threshold:
                continue
            row = dict(current)
            row["alerted_at"] = history_row.get("alerted_at", 0)
        else:
            # Fresh installs may have an alert history before a jobs snapshot
            # exists. Preserve that compatibility while rejecting explicit
            # stale verdicts and below-threshold records.
            if history_row.get("alert_ok") is False:
                continue
            if "score" in history_row and int(history_row.get("score", 0) or 0) < threshold:
                continue
            row = dict(history_row)
        if profile_id() == "internship" and not _spring_winter_2027_internship(row):
            continue
        rows.append(row)
    # A provider can expose one requisition at multiple application/detail
    # URLs. Treat those as one delivery item while keeping distinct
    # requisitions separate through the provider-aware posting key.
    sent_postings = {
        _posting_marker(row) for row in rows if row.get("id") in sent_ids
    }
    rows = _dedupe_alert_rows(rows)
    rows = [row for row in rows
            if row.get("id") not in sent_ids
            and _posting_marker(row) not in sent_postings]
    rows.sort(key=lambda row: ranking_key(row, timestamp_key="alerted_at"))
    for row in rows:
        row.pop("_history_index", None)
    return rows[:limit]


def _batch_marker_labels() -> str:
    """Labels that identify one lane's batch issues to GitHub."""
    return ",".join((LABEL, lane_label(), batch_label()))


def _remember_batch_receipts(notification: dict, alert_history: list[dict],
                             sent_ids: set[str], delivered: list[dict]) -> None:
    """Keep a deterministic, bounded local cache of recent batch receipts.

    GitHub issue markers are the durable receipt.  The local list is only a
    fast path, so discard IDs no longer present in the active history window
    and order the remainder by the latest alert event rather than by the
    nondeterministic iteration order of a set.
    """
    history_times: dict[str, int] = {}
    for record in alert_history:
        job_id = str(record.get("id") or "")
        if not job_id:
            continue
        stamp = int(record.get("alerted_at", 0) or 0)
        history_times[job_id] = max(history_times.get(job_id, stamp), stamp)
    for row in delivered:
        job_id = str(row.get("id") or "")
        if job_id:
            sent_ids.add(job_id)
    recent = [job_id for job_id in sent_ids if job_id in history_times]
    recent.sort(key=lambda job_id: (history_times[job_id], job_id))
    notification["email_batch_sent_ids"] = recent[-1000:]


def _jobs_snapshot() -> tuple[dict, bool]:
    """Return jobs plus whether a persisted snapshot exists.

    An empty JSON object is a valid authoritative snapshot; only a missing
    snapshot may use alert history as the fresh-install fallback.
    """
    jobs = state.jobs()
    path_fn = getattr(state, "_path", None)
    if path_fn is None:
        return jobs, bool(jobs)
    try:
        primary = path_fn("jobs.json")
        history = path_fn("jobs_history.json")
        return jobs, bool(primary.exists() or history.exists())
    except (AttributeError, OSError):
        return jobs, bool(jobs)


def post_email_batch(alert_history: list[dict], limit: int | None = None) -> str | None:
    """Send a periodic multi-job GitHub email and remember delivered rows."""
    if not email_enabled():
        print(f"email-batch: {profile_id()} email is disabled")
        return None
    token = env("GITHUB_TOKEN")
    if not token:
        print("email-batch: GITHUB_TOKEN not set — skipping")
        return None
    now = int(time.time())
    limit = int(env("RADAR_EMAIL_BATCH_SIZE", "15")) if limit is None else limit
    notification = state.load("notification_state.json", {})
    sent_ids = _expand_marker_ids(set(notification.get("email_batch_sent_ids", [])))
    jobs_state, snapshot_exists = _jobs_snapshot()
    # A caller can supply a standalone alert history before the first jobs
    # snapshot exists (used by tests and by a fresh install). Once state has
    # rows, missing/terminal IDs are treated as no longer deliverable.
    pending = email_batch_rows(alert_history, sent_ids, now, 1000,
                               jobs_state if snapshot_exists else None)
    if not pending:
        print("email-batch: no unsent alerts")
        return None
    min_batch = int(env("RADAR_EMAIL_BATCH_MIN", "3"))
    max_wait_hours = float(env("RADAR_EMAIL_BATCH_MAX_WAIT_HOURS", "12"))
    urgent_score = int(env("RADAR_EMAIL_BATCH_URGENT_SCORE", "92"))
    oldest_age = now - min(row.get("alerted_at", now) for row in pending)
    urgent = max(row.get("score", 0) for row in pending) >= urgent_score
    if len(pending) < min_batch and not urgent and oldest_age < max_wait_hours * 3600:
        print(f"email-batch: holding {len(pending)} role(s); waiting for "
              f"{min_batch} roles or {max_wait_hours:g}h")
        return None
    repo = github_repo()
    # The issue body is the durable delivery receipt.  This closes the crash
    # window between GitHub accepting the batch and the local JSON save, and
    # also protects against a concurrent workflow starting from stale state.
    marker_since = min(int(row.get("alerted_at", now) or now) for row in pending)
    existing_ids = _existing_marker_ids(
        repo, since=marker_since, labels=_batch_marker_labels())
    if existing_ids:
        recovered = [row for row in pending if row.get("id") in existing_ids]
        if recovered:
            _remember_batch_receipts(notification, alert_history, sent_ids, recovered)
            try:
                state.save("notification_state.json", notification)
            except Exception as exc:
                print(f"email-batch: could not cache recovered receipts: {exc}")
        pending = [row for row in pending if row.get("id") not in existing_ids]
    if not pending:
        print("email-batch: all pending alerts already have durable batch markers")
        return None
    # A marker can remove enough rows to cross the normal batch threshold.
    # Reapply the same policy to the rows that still need delivery.
    oldest_age = now - min(row.get("alerted_at", now) for row in pending)
    urgent = max(row.get("score", 0) for row in pending) >= urgent_score
    if len(pending) < min_batch and not urgent and oldest_age < max_wait_hours * 3600:
        print(f"email-batch: holding {len(pending)} role(s); waiting for "
              f"{min_batch} roles or {max_wait_hours:g}h")
        return None
    rows = pending[:limit]
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    internship_lane = profile_id() == "internship"
    title_prefix = "Internship" if internship_lane else "Job Radar"
    title = f"📬 {title_prefix} batch — {stamp} ({len(rows)} roles)"
    from .culture import load as culture_load
    role_label = "internship roles" if internship_lane else "roles"
    body = (f"The radar found **{len(rows)}** new {role_label} since the last batch. "
            "They are ordered by score, then recency. Check a box to track one.\n\n"
            + "\n".join(format_line(row, culture_load()) for row in rows) + "\n")
    response = requests.post(f"{API}/repos/{repo}/issues", headers=_headers(), timeout=20,
                             json={"title": title, "body": body,
                                   "labels": [LABEL, lane_label(), batch_label()],
                                   "assignees": [github_owner()]})
    response.raise_for_status()
    _remember_batch_receipts(notification, alert_history, sent_ids, rows)
    notification["last_email_batch_at"] = now
    state.save("notification_state.json", notification)
    return response.json()["html_url"]
