"""Shortlist + applied-logging: react to GitHub issue events.

- Checking a checkbox on an alert issue (`- [x] ... <!--radar:ID-->`) means
  "save this for later" — it does NOT mean applied. It's recorded to
  state/shortlist.json, gives a small ranking boost, and nothing is written
  to Notion yet.
- Actual applications are detected automatically by email_watch.py (matches
  confirmation emails against the shortlist and promotes them to
  state/applied.json + Notion), or logged explicitly via comment commands:
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
from .notion_sync import sync_applied
from .score import update_feedback_from_applied, update_feedback_from_shortlist

CHECKED = re.compile(r"^- \[[xX]\] .*?<!--radar:([a-f0-9]{16})-->", re.M)
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
        criteria=profile().get("culture_criteria", ""), company=name), max_tokens=500)
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


def _record_shortlist(job: dict, shortlist: list, fb: dict) -> bool:
    if any(s["id"] == job["id"] for s in shortlist):
        return False
    shortlist.append({
        "id": job["id"], "company": job["company"], "title": job["title"],
        "url": job.get("url", ""), "locations": job.get("locations", []),
        "score": job.get("score"), "source": job.get("source"),
        "shortlisted_at": int(time.time()),
    })
    update_feedback_from_shortlist(fb, job["company"], job["title"])
    return True


def record_applied(job: dict, applied: list, fb: dict, via: str) -> bool:
    if any(a["id"] == job["id"] for a in applied):
        return False
    applied.append({
        "id": job["id"], "company": job["company"], "title": job["title"],
        "url": job.get("url", ""), "locations": job.get("locations", []),
        "score": job.get("score"), "source": job.get("source"),
        "applied_at": int(time.time()), "via": via, "notion_synced": False,
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

    jobs = state.jobs()
    applied = state.applied()
    shortlist = state.shortlist()
    fb = state.feedback()
    changed = 0

    body = ""
    if "comment" in event:
        body = event["comment"].get("body", "") or ""
    elif "issue" in event:
        # only trust checkbox state on our own alert issues
        labels = [l["name"] for l in event["issue"].get("labels", [])]
        if "radar-alerts" in labels:
            body = event["issue"].get("body", "") or ""

    if "comment" in event:
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
            # if this was previously shortlisted, remove it — it's confirmed now
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
                changed += _record_shortlist(job, shortlist, fb)

    # sync_applied only ever pushes confirmed applications (state/applied.json)
    # to Notion — shortlisting never writes to Notion.
    synced = sync_applied(applied)
    if changed or synced:
        state.save("applied.json", applied)
        state.save("shortlist.json", shortlist)
        state.save("feedback.json", fb)
        print(f"applied: recorded {changed} change(s), synced {synced} to Notion")
    else:
        print("applied: nothing new")


def main() -> None:
    path = env("GITHUB_EVENT_PATH")
    if not path:
        raise SystemExit("GITHUB_EVENT_PATH not set (this entrypoint runs in Actions)")
    handle_event(path)
