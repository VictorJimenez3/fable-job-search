"""Application tracking: react to GitHub issue events.

- Checking a checkbox on an alert issue (`- [x] ... <!--radar:ID-->`) tracks
  the job: it is recorded to applied.json with stage="saved" and a Notion page
  is created immediately with the not-yet-applied status. The candidate flips the
  status to Applied in Notion himself when he actually applies.
- An `applied <url>` comment (or, once credentials exist, email confirmation
  detection) records stage="applied" — promoting an already-saved entry and
  patching its Notion page rather than duplicating it.
- Comment commands:
    applied <url or id>   log a confirmed application immediately
    skip <company or id>  negative feedback (downranks similar roles)
    track <ats> <token> [Company Name]   manually add a company to the registry
"""
from __future__ import annotations

import json
import re
import time

from . import state
from .config import env
from .models import norm
from .notion_sync import sync_applied, sync_from_notion
from .score import update_feedback_from_applied

CHECKED = re.compile(r"^- \[[xX]\] .*?<!--radar:([a-f0-9]{16})-->", re.M)
CMD_SAVE = re.compile(r"^save\s+(\S+)", re.I | re.M)
CMD_APPLIED = re.compile(r"^applied\s+(\S+)", re.I | re.M)
CMD_SKIP = re.compile(r"^skip\s+(.+?)\s*$", re.I | re.M)
CMD_TRACK = re.compile(r"^track\s+(\w+)\s+(\S+)(?:\s+(.+))?", re.I | re.M)
CMD_CULTURE = re.compile(r"^culture\s+(.+?)\s*$", re.I | re.M)


def _reply(event: dict, body: str) -> None:
    """Post a comment reply on the issue this event belongs to."""
    import requests
    from .config import github_repo
    token = env("GITHUB_TOKEN")
    number = (event.get("issue") or {}).get("number")
    if not token or not number:
        print(f"reply (not posted):\n{body[:400]}")
        return
    requests.post(f"https://api.github.com/repos/{github_repo()}/issues/{number}/comments",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/vnd.github+json"},
                  json={"body": body}, timeout=20).raise_for_status()


def culture_generate_one(name: str, dossiers: dict) -> bool:
    """On-demand dossier for the `culture` command; no-op without an LLM."""
    import json as _json

    from . import culture, llm
    from .config import profile
    if not llm.available():
        return False
    text = llm.complete(culture._GEN_PROMPT.format(
        criteria=profile().get("culture_criteria", ""), company=name),
        max_tokens=350, json_mode=True, task="company_research")
    if not text:
        return False
    try:
        row = _json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, _json.JSONDecodeError):
        return False
    d = {f: row.get(f, "") for f in culture.FIELDS}
    d.update(name=name, source="est.", generated_at=int(time.time()),
             profile_mode=culture.PROFILE_MODE)
    d["fit"] = culture.fit_score(d)
    dossiers[norm(name)] = d
    culture.save(dossiers)
    return True


def record_applied(job: dict, applied: list, fb: dict, via: str, stage: str = "applied") -> bool:
    existing = next((a for a in applied if a["id"] == job["id"]), None)
    if existing:
        # An applied signal promotes a saved entry; anything else is a dupe.
        if stage == "applied" and existing.get("stage", "applied") != "applied":
            existing["stage"] = "applied"
            existing["via"] = via
            existing["applied_at"] = int(time.time())
            return True
        return False
    applied.append({
        "id": job["id"], "company": job["company"], "title": job["title"],
        "url": job.get("url", ""), "locations": job.get("locations", []),
        "score": job.get("score"), "source": job.get("source"),
        "applied_at": int(time.time()), "via": via, "stage": stage,
        "notion_synced": False,
    })
    update_feedback_from_applied(fb, job["company"], job["title"])
    return True


def handle_event(event_path: str) -> None:
    with open(event_path) as f:
        event = json.load(f)

    sender = (event.get("sender") or {}).get("login", "")
    if sender.endswith("[bot]"):
        print("applied: bot event, ignoring")
        return
    # This repo is public: anyone on GitHub can comment or open issues.
    # Only the repo owner may drive the tracker — GitHub's login is the auth.
    from .config import github_owner
    if sender.lower() != github_owner().lower():
        print(f"applied: event from {sender!r} is not the repo owner — ignoring")
        return

    jobs = state.jobs()
    applied = state.applied()
    shortlist = state.shortlist()
    fb = state.feedback()
    changed = 0

    labels = [l["name"] for l in (event.get("issue") or {}).get("labels", [])]
    # tokenless platform path: a freshly opened issue whose body carries
    # commands (save <id>, applied <url>, skip <company>). The owner check
    # above is the gate; the issue is auto-closed when processed.
    opened_cmd = "comment" not in event and event.get("action") == "opened"
    body = ""
    if "comment" in event:
        body = event["comment"].get("body", "") or ""
        # master-board pages live in bot comments; a checkbox ticked there
        # arrives as an issue_comment edit — track it like a body checkbox
        if "radar-alerts" in labels:
            for jid in CHECKED.findall(body):
                job = jobs.get(jid)
                if job:
                    changed += record_applied(job, applied, fb,
                                              via="issue-checkbox", stage="saved")
                    shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]
    elif opened_cmd:
        body = (event.get("issue") or {}).get("body", "") or ""
    elif "issue" in event:
        # only trust checkbox state on our own alert issues
        if "radar-alerts" in labels:
            body = event["issue"].get("body", "") or ""

    if "comment" in event or opened_cmd:
        for m in CMD_SAVE.finditer(body):
            ref = m.group(1).strip()
            job = jobs.get(ref) or next((j for j in jobs.values() if j.get("url") == ref), None)
            if job:
                changed += record_applied(job, applied, fb, via="issue-command", stage="saved")
                shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]
        for m in CMD_APPLIED.finditer(body):
            ref = m.group(1).strip()
            job = jobs.get(ref)
            if job is None:  # try URL match
                job = next((j for j in jobs.values() if j.get("url") == ref), None)
            if job is None:  # unknown job: log a minimal record from the URL
                job = {"id": f"manual{int(time.time()) % 10 ** 10:010x}"[:16].ljust(16, "0"),
                       "company": ref.split("/")[2] if ref.startswith("http") else ref,
                       "title": "Manually logged application", "url": ref if ref.startswith("http") else "",
                       "locations": [], "score": None, "source": "manual"}
            changed += record_applied(job, applied, fb, via="comment")
            # Clear any old shortlist entry for this now-confirmed application.
            shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]
        for m in CMD_SKIP.finditer(body):
            ref = m.group(1).strip()
            job = jobs.get(ref) if re.fullmatch(r"[a-f0-9]{16}", ref) else None
            comp = norm(job["company"]) if job else norm(ref)
            if comp and comp not in fb["negative_companies"]:
                fb["negative_companies"].append(comp)
                changed += 1
        for m in CMD_CULTURE.finditer(body):
            from . import culture
            name = m.group(1).strip()
            dossiers = culture.load()
            culture.sync_seed(dossiers)
            d = culture.dossier_for(name, dossiers)
            if d is None and culture_generate_one(name, dossiers):
                d = culture.dossier_for(name, dossiers)
            _reply(event, culture.render_dossier_reply(d) if d else
                   f"No dossier for **{name}** yet — it'll be generated on the next "
                   "enrichment pass (needs an LLM provider configured).")
        for m in CMD_TRACK.finditer(body):
            ats, token, name = m.group(1).lower(), m.group(2), (m.group(3) or m.group(2)).strip()
            registry = state.companies()
            k = f"{ats}:{token}"
            if k not in registry:
                registry[k] = {"name": name, "ats": ats, "token": token, "extra": {},
                               "sector": "", "status": "new", "origin": "manual",
                               "first_seen": int(time.time()), "failures": 0, "last_ok": 0}
                state.save("companies.json", registry)
                changed += 1
    else:
        for jid in CHECKED.findall(body):
            job = jobs.get(jid)
            if job:
                changed += record_applied(job, applied, fb, via="issue-checkbox", stage="saved")
                shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]

    pulled = sync_from_notion(applied)
    synced = sync_applied(applied)
    if changed or pulled or synced:
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save_feedback(fb)
        print(f"applied: recorded {changed} change(s), pulled {pulled}, "
              f"synced {synced} to tracker")
    else:
        print("applied: nothing new")
    if opened_cmd:
        _close_command_issue(event)


def _close_command_issue(event: dict) -> None:
    """Tidy up a processed tokenless command issue (best-effort)."""
    import requests
    from .config import github_repo
    token = env("GITHUB_TOKEN")
    number = (event.get("issue") or {}).get("number")
    if not token or not number:
        return
    try:
        requests.patch(f"https://api.github.com/repos/{github_repo()}/issues/{number}",
                       headers={"Authorization": f"Bearer {token}",
                                "Accept": "application/vnd.github+json"},
                       json={"state": "closed", "state_reason": "completed"},
                       timeout=20).raise_for_status()
    except Exception as e:
        print(f"applied: could not close command issue: {e}")


def reconcile_checkboxes() -> int:
    """Sweep every radar issue (weekly, daily, master — bodies and comments)
    for checked boxes and make sure each one is tracked in applied.json and
    Notion. Event-driven sync can miss ticks (deploys, outages, the semantics
    changes); this idempotent sweep guarantees nothing the owner checked is ever
    silently lost. Runs on a schedule and on demand."""
    import requests
    from .config import github_repo
    token = env("GITHUB_TOKEN")
    if not token:
        print("reconcile: GITHUB_TOKEN not set")
        return 1
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    jobs = state.jobs()
    hist = {a["id"]: a for a in state.load("alert_history.json", [])}
    applied = state.applied()
    shortlist = state.shortlist()
    fb = state.feedback()

    checked: set[str] = set()
    page = 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{github_repo()}/issues",
                         params={"labels": "radar-cheme", "state": "all",
                                 "per_page": 100, "page": page},
                         headers=headers, timeout=30)
        r.raise_for_status()
        issues = r.json()
        if not issues:
            break
        for issue in issues:
            checked |= set(CHECKED.findall(issue.get("body") or ""))
            if issue.get("comments"):
                cr = requests.get(issue["comments_url"], params={"per_page": 100},
                                  headers=headers, timeout=30)
                cr.raise_for_status()
                for c in cr.json():
                    checked |= set(CHECKED.findall(c.get("body") or ""))
        page += 1

    changed = 0
    for jid in checked:
        job = jobs.get(jid) or hist.get(jid)
        if job:
            changed += record_applied(job, applied, fb, via="reconcile", stage="saved")
            shortlist[:] = [s for s in shortlist if s["id"] != jid]
    pulled = sync_from_notion(applied)
    synced = sync_applied(applied)
    if changed or pulled or synced:
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save_feedback(fb)
    print(f"reconcile: {len(checked)} checked boxes across all issues, "
          f"{changed} newly tracked, {pulled} stage change(s) pulled, "
          f"{synced} synced to tracker")
    return 0


def main() -> None:
    path = env("GITHUB_EVENT_PATH")
    if not path:
        raise SystemExit("GITHUB_EVENT_PATH not set (this entrypoint runs in Actions)")
    handle_event(path)
