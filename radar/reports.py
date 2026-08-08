"""Community posting reports and the small-scale owner review queue."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import state
from .config import DOCS_DIR, env, github_owner, github_repo


REPORT_THRESHOLD = 3
REPORT_TYPES = {"expired", "filled", "duplicate", "wrong", "other"}
REPORT_RE = re.compile(r"radar-report:\s*([a-f0-9]{16})", re.I)
TYPE_RE = re.compile(r"report-type:\s*(expired|filled|duplicate|wrong|other)", re.I)


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def record_report(reports: dict, job: dict, reporter: str, report_type: str,
                  issue_number: int | None = None, issue_url: str = "") -> bool:
    reporter = str(reporter or "").strip()
    report_type = str(report_type or "").lower().strip()
    job_id = str(job.get("id") or "").strip()
    if not reporter or not job_id or report_type not in REPORT_TYPES:
        return False
    entry = reports.setdefault(job_id, {
        "job_id": job_id,
        "company": str(job.get("company", ""))[:200],
        "title": str(job.get("title", ""))[:240],
        "url": str(job.get("url", ""))[:2000],
        "reports": [],
    })
    existing = next((r for r in entry["reports"] if r.get("github_user", "").lower() == reporter.lower()), None)
    now = int(time.time())
    if existing:
        if existing.get("report_type") == report_type:
            return False
        existing.update({"report_type": report_type, "updated_at": now,
                         "issue_number": issue_number, "issue_url": issue_url})
        return True
    entry["reports"].append({
        "github_user": reporter,
        "report_type": report_type,
        "created_at": now,
        "issue_number": issue_number,
        "issue_url": issue_url,
    })
    entry["reports"] = entry["reports"][-100:]
    return True


def distinct_reporters(entry: dict) -> int:
    return len({str(r.get("github_user", "")).lower() for r in entry.get("reports", []) if r.get("github_user")})


def _notify(entry: dict, issue_number: int | None) -> bool:
    if distinct_reporters(entry) < REPORT_THRESHOLD or entry.get("notified_at"):
        return False
    token = env("GITHUB_TOKEN")
    if not token or not issue_number:
        return False
    import requests
    body = (
        f"@{github_owner()} this posting has reports from **{distinct_reporters(entry)} "
        f"distinct GitHub users** and needs review.\n\n"
        f"**{entry.get('company')} — {entry.get('title')}**\n"
        f"Report types: {', '.join(sorted({r.get('report_type', 'other') for r in entry.get('reports', [])}))}\n"
        f"Posting: {entry.get('url', '')}\n\n"
        "Use the owner dashboard action to archive it if it is stale; the crawler history is preserved."
    )
    response = requests.post(
        f"https://api.github.com/repos/{github_repo()}/issues/{issue_number}/comments",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "job-radar-report-sync"},
        json={"body": body}, timeout=20,
    )
    if response.ok:
        entry["notified_at"] = int(time.time())
        return True
    print(f"reports: owner notification failed ({response.status_code})")
    return False


def render_report(reports: dict) -> str:
    rows = sorted(reports.values(), key=lambda e: (-distinct_reporters(e), e.get("company", "")))
    lines = [
        "# Community posting reports",
        "",
        "> Reports are keyed by posting and deduplicated by GitHub login. Three distinct reporters move a posting into the owner review queue.",
        "> Owner archive is recoverable: the job remains in historical state with `manual_archived: true`.",
        "",
        "| reporters | posting | report types | owner review |",
        "| ---: | --- | --- | --- |",
    ]
    for entry in rows:
        names = sorted({str(r.get("github_user")) for r in entry.get("reports", [])})
        types = sorted({str(r.get("report_type")) for r in entry.get("reports", [])})
        review = "✅ notified" if entry.get("notified_at") else ("⚠️ review" if len(names) >= REPORT_THRESHOLD else "watch")
        lines.append(
            f"| {len(names)} | {_md(entry.get('company'))} — {_md(entry.get('title'))} | "
            f"{', '.join(types)} | {review} |"
        )
        lines.append(f"|  | reporters: {', '.join('@' + _md(n) for n in names)} |  |  |")
    if not rows:
        lines.append("| 0 | No reports yet |  |  |")
    lines.append("")
    return "\n".join(lines)


def write_report(reports: dict, path: Path | None = None) -> Path:
    destination = path or (DOCS_DIR / "REPORTS.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(reports))
    return destination


def handle_event(event_path: str) -> int:
    with open(event_path) as f:
        event = json.load(f)
    issue = event.get("issue") or {}
    body = issue.get("body") or ""
    match = REPORT_RE.search(body)
    type_match = TYPE_RE.search(body)
    if not match or not type_match:
        return 0
    jobs = state.jobs()
    history = {row.get("id"): row for row in state.load("alert_history.json", [])}
    job = jobs.get(match.group(1)) or history.get(match.group(1))
    if not job:
        print(f"reports: job {match.group(1)!r} not found")
        return 0
    reports = state.load("reports.json", {})
    changed = record_report(
        reports, job, (issue.get("user") or {}).get("login", ""),
        type_match.group(1), issue.get("number"), issue.get("html_url", ""),
    )
    if not changed:
        print("reports: duplicate report")
        return 0
    entry = reports[match.group(1)]
    _notify(entry, issue.get("number"))
    state.save("reports.json", reports)
    write_report(reports)
    print(f"reports: recorded {job.get('company')} — {distinct_reporters(entry)} distinct reporter(s)")
    return 0
