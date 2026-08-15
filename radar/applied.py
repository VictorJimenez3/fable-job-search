"""Application tracking: react to GitHub issue events.

- Checking a checkbox on an alert issue (`- [x] ... <!--radar:ID-->`) tracks
  the job: it is recorded to applied.json with stage="saved" and a Notion page
  is created immediately with the not-yet-applied status. Victor flips the
  status to Applied in Notion himself when he actually applies.
- An `applied <url>` comment (or, once credentials exist, email confirmation
  detection) records stage="applied" — promoting an already-saved entry and
  patching its Notion page rather than duplicating it.
- Comment commands:
    applied <url or id>   log a confirmed application immediately
    skip <company or id>  negative feedback (downranks similar roles)
    feedback <id> <up|down> <reason>  structured owner taste feedback
    untrack <id or url>   remove from the in-house pipeline and archive Notion
    track <ats> <token> [Company Name]   manually add a company to the registry
"""
from __future__ import annotations

import json
import re
import time

from . import state
from .config import env, profile_id
from .identity import canonical_url
from .models import norm
from .notion_sync import sync_applied, sync_from_notion
from .score import update_feedback_from_applied

CHECKED = re.compile(r"^- \[[xX]\] .*?<!--radar:([a-f0-9]{16})-->", re.M)
CMD_SAVE = re.compile(r"^save\s+(\S+)", re.I | re.M)
CMD_APPLIED = re.compile(r"^applied\s+(\S+)", re.I | re.M)
CMD_SKIP = re.compile(r"^skip\s+(.+?)\s*$", re.I | re.M)
CMD_FEEDBACK = re.compile(
    r"^feedback\s+([a-f0-9]{16})\s+(up|down)\s+"
    r"(company|role|both|eligibility|location|other)\s*$", re.I | re.M)
CMD_UNTRACK = re.compile(r"^untrack\s+(\S+)", re.I | re.M)
CMD_TRACK = re.compile(r"^track\s+(\w+)\s+(\S+)(?:\s+(.+))?", re.I | re.M)
CMD_CULTURE = re.compile(r"^culture\s+(.+?)\s*$", re.I | re.M)

# Keep this ordering in one place for issue commands, email detection, and
# tracker deduplication. Terminal states intentionally remain terminal.
TRACKER_STAGE_ORDER = {
    "saved": 0, "applied": 1, "oa": 2, "interview": 3,
    "offered": 8, "signed": 9, "rejected": 9, "closed": 9,
    "not_pursuing": 9,
}


def _stage_rank(stage: str | None) -> int:
    return TRACKER_STAGE_ORDER.get(str(stage or "applied").lower(), 1)


def _tracker_key(entry: dict) -> tuple[str, str]:
    url = canonical_url(entry.get("url"))
    return ("url", url) if url else ("id", str(entry.get("id", "")))


def _tracker_winner_key(index: int, entry: dict) -> tuple:
    return (
        1 if not entry.get("notion_archived") else 0,
        1 if not entry.get("tracker_removed_at") else 0,
        _stage_rank(entry.get("stage")),
        int(entry.get("stage_changed_at") or 0),
        int(entry.get("responded_at") or 0),
        1 if entry.get("notion_page") else 0,
        1 if entry.get("notion_synced") else 0,
        int(entry.get("applied_at") or 0),
        -index,
    )


def _merge_tracker_entry(winner: dict, loser: dict) -> dict:
    """Merge duplicate tracker rows without discarding owner or Notion state."""
    merged = dict(winner)
    for key, value in loser.items():
        if key in {"id", "url", "stage", "notion_page", "notion_synced"}:
            continue
        if key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value

    locations = list(merged.get("locations") or [])
    for location in loser.get("locations") or []:
        if location not in locations:
            locations.append(location)
    if locations:
        merged["locations"] = locations

    if (loser.get("score") or 0) > (merged.get("score") or 0):
        merged["score"] = loser["score"]

    # A surviving row can inherit the only Notion page. If both rows had a
    # page, the non-surviving page is queued for soft-archival by notion_sync.
    winner_page = merged.get("notion_page") or ""
    loser_page = loser.get("notion_page") or ""
    duplicate_pages = list(merged.get("notion_duplicate_pages") or [])
    duplicate_pages.extend(loser.get("notion_duplicate_pages") or [])
    if winner_page and loser_page and winner_page != loser_page:
        duplicate_pages.append(loser_page)
    elif not winner_page and loser_page:
        merged["notion_page"] = loser_page
        merged["notion_synced"] = bool(loser.get("notion_synced"))
        winner_page = loser_page
    merged["notion_duplicate_pages"] = list(dict.fromkeys(
        page for page in duplicate_pages if page and page != winner_page
    ))

    if not merged.get("notion_stage") and loser.get("notion_stage"):
        merged["notion_stage"] = loser["notion_stage"]
    if not merged.get("notion_synced") and loser.get("notion_synced") and winner_page:
        merged["notion_synced"] = True

    merged_ids = list(merged.get("tracker_merged_ids") or [])
    loser_id = loser.get("id")
    if loser_id and loser_id != merged.get("id") and loser_id not in merged_ids:
        merged_ids.append(loser_id)
    if merged_ids:
        merged["tracker_merged_ids"] = merged_ids
    return merged


def deduplicate_entries(applied: list[dict]) -> int:
    """Collapse tracker entries that refer to the same canonical posting URL."""
    groups: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    for index, entry in enumerate(applied):
        key = _tracker_key(entry)
        if key == ("id", ""):
            key = ("index", str(index))
        groups.setdefault(key, []).append((index, entry))

    output: list[tuple[int, dict]] = []
    merged_count = 0
    for members in groups.values():
        if len(members) == 1:
            output.append(members[0])
            continue
        winner_index, winner = max(members, key=lambda pair: _tracker_winner_key(*pair))
        merged = dict(winner)
        for index, entry in members:
            if index == winner_index:
                continue
            merged = _merge_tracker_entry(merged, entry)
            merged_count += 1
        output.append((min(index for index, _ in members), merged))

    output.sort(key=lambda pair: pair[0])
    applied[:] = [entry for _, entry in output]
    return merged_count


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
    d.update(name=name, source="est.", generated_at=int(time.time()))
    d["fit"] = culture.fit_score(d)
    dossiers[norm(name)] = d
    culture.save(dossiers)
    return True


def record_applied(job: dict, applied: list, fb: dict, via: str, stage: str = "applied") -> bool:
    job_url = canonical_url(job.get("url"))
    existing = next((a for a in applied
                     if a.get("id") == job.get("id")
                     or (job_url and canonical_url(a.get("url")) == job_url)), None)
    now = int(time.time())
    if existing:
        existing.setdefault("profile", job.get("profile") or profile_id())
        for key in ("company", "title", "url", "locations", "sector", "score", "source"):
            if existing.get(key) in (None, "", [], {}) and job.get(key) not in (None, "", [], {}):
                existing[key] = job[key]
        current = existing.get("stage", "applied")
        if _stage_rank(stage) > _stage_rank(current):
            existing["stage"] = stage
            existing["status"] = stage
            existing["via"] = via
            existing["stage_changed_at"] = now
            if stage == "applied":
                existing["applied_at"] = now
            if stage in {"oa", "interview", "rejected", "closed"}:
                existing.setdefault("responded_at", now)
            if existing.get("notion_archived"):
                # Re-tracking a previously archived posting gets a fresh page;
                # the old page remains recoverable in Notion trash.
                existing["notion_archived"] = False
                existing["notion_synced"] = False
                existing.pop("notion_page", None)
            existing.pop("tracker_removed_at", None)
            existing.pop("notion_deleted_at", None)
            return True
        return False
    entry = {
        "id": job["id"], "company": job["company"], "title": job["title"],
        "url": job.get("url", ""), "locations": job.get("locations", []),
        "sector": job.get("sector", ""),
        "score": job.get("score"), "source": job.get("source"),
        "profile": job.get("profile") or profile_id(),
        "applied_at": now, "via": via, "stage": stage, "status": stage,
        "stage_changed_at": now,
        "notion_synced": False,
    }
    if stage in {"oa", "interview", "rejected", "closed"}:
        entry["responded_at"] = now
    applied.append(entry)
    update_feedback_from_applied(fb, job["company"], job["title"])
    return True


def remove_tracking(ref: str, applied: list, untracked: set[str]) -> bool:
    """Remove one local tracker entry and archive its Notion page if present."""
    ref_url = canonical_url(ref)
    entry = next((a for a in applied if a.get("id") == ref
                  or (ref_url and canonical_url(a.get("url")) == ref_url)), None)
    if not entry:
        return False
    page = entry.get("notion_page")
    token = env("NOTION_TOKEN")
    if page and token:
        from .notion_sync import archive_page, page_id_from_url
        page_id = page_id_from_url(page)
        if page_id:
            try:
                archive_page(token, page_id)
            except Exception as exc:
                print(f"tracker: could not archive Notion page for {entry.get('company')}: {exc}")
    applied.remove(entry)
    untracked.add(entry["id"])
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
    untracked = set(state.load("untracked.json", []))
    fb = state.feedback()
    changed = 0
    taste_changed = False

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
                if job and jid not in untracked:
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
                untracked.discard(job["id"])
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
            untracked.discard(job["id"])
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
        from . import taste
        for m in CMD_FEEDBACK.finditer(body):
            job = jobs.get(m.group(1))
            if job and taste.record_feedback(fb, job, m.group(2), m.group(3)):
                changed += 1
                taste_changed = True
        for m in CMD_UNTRACK.finditer(body):
            if remove_tracking(m.group(1).strip(), applied, untracked):
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
            if job and jid not in untracked:
                changed += record_applied(job, applied, fb, via="issue-checkbox", stage="saved")
                shortlist[:] = [s for s in shortlist if s["id"] != job["id"]]

    pulled = sync_from_notion(applied)
    synced = sync_applied(applied)
    if changed or pulled or synced:
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("feedback.json", fb)
        if taste_changed:
            taste.write_report(fb)
    state.save("untracked.json", sorted(untracked))
    if changed or pulled or synced:
        print(f"applied: recorded {changed} change(s), pulled {pulled}, "
              f"synced {synced} to Notion")
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
    changes); this idempotent sweep guarantees nothing Victor checked is ever
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
    untracked = set(state.load("untracked.json", []))
    fb = state.feedback()

    checked: set[str] = set()
    page = 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{github_repo()}/issues",
                         params={"labels": "radar-alerts", "state": "all",
                                 "per_page": 100, "page": page},
                         headers=headers, timeout=30)
        r.raise_for_status()
        issues = r.json()
        if not issues:
            break
        for issue in issues:
            issue_labels = {label.get("name") for label in issue.get("labels", [])}
            issue_body = issue.get("body") or ""
            is_internship = "radar-internships" in issue_labels or "radar-profile: internship" in issue_body
            if (profile_id() == "internship") != is_internship:
                continue
            checked |= set(CHECKED.findall(issue_body))
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
        if job and jid not in untracked:
            changed += record_applied(job, applied, fb, via="reconcile", stage="saved")
            shortlist[:] = [s for s in shortlist if s["id"] != jid]
    pulled = sync_from_notion(applied)
    synced = sync_applied(applied)
    if changed or pulled or synced:
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("feedback.json", fb)
    state.save("untracked.json", sorted(untracked))
    print(f"reconcile: {len(checked)} checked boxes across all issues, "
          f"{changed} newly tracked, {pulled} stage change(s) pulled, "
          f"{synced} synced to Notion")
    return 0


def main() -> None:
    path = env("GITHUB_EVENT_PATH")
    if not path:
        raise SystemExit("GITHUB_EVENT_PATH not set (this entrypoint runs in Actions)")
    handle_event(path)
