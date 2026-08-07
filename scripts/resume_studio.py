#!/usr/bin/env python3
"""Victor-first local Resume Studio.

This is deliberately a local companion, not a hosted CV service.  It reads the
radar's public job snapshot and Victor's ignored ``CV/`` directory, then can
ask the installed first-party Codex and Claude Code CLIs to work on a private
resume draft using their existing local authentication.

The service has two modes:

* ``strict`` selects only existing, human-approved source bullets and runs
  deterministic layout checks against the canonical one-page resume format.
* ``dream`` runs independent frontier drafts, a synthesis pass, and a fixed
  reviewer pass.  The reviewer returns observations only; this module computes
  the final score from the immutable rubric below.

Run with::

    .venv/bin/python scripts/resume_studio.py

Then open http://127.0.0.1:4317/ .  All generated material stays below the
ignored ``CV/.resume_studio/`` directory.
"""

from __future__ import annotations

import argparse
import copy
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import requests

SCRIPT_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_REPO_ROOT))

from radar.resume_match import (MATCH_VERSION, build_evidence_graph,
                                evidence_context, job_match_hash,
                                posting_eligibility_blocks, score_resume_match)


RUBRIC_VERSION = "resume-review-v4"
RUBRIC_WEIGHTS = {
    "target_fit": 30,
    "evidence": 25,
    "clarity": 15,
    "portfolio": 20,
    "layout": 10,
}
REVIEW_CRITERIA = ("factual", "target_fit", "evidence", "clarity", "portfolio")
STATUS_MULTIPLIER = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
MAX_POSTING_CHARS = 12000
MAX_PROMPT_CHARS = 42000
RUN_TIMEOUT_SECONDS = 12 * 60
CANONICAL_TEMPLATE = "resume.tex"
BODY_MARKER = "%-----------EXPERIENCE-----------"
MAX_STYLE_REDUCTION_PERCENT = 5.0
MAX_DENSITY_GAP_PT = 24.0
MAX_RIGHT_SLACK_PT = 38.0
MAX_LINE_EDIT_PASSES = 2
MIN_TOTAL_BULLETS = 22
MAX_TOTAL_BULLETS = 26
PROTECTED_QUALIFIERS = (
    "proof of concept",
    "prototype",
    "synthetic",
    "simulation",
    "simulated",
    "demo",
)
PORTFOLIO_CAPS = {
    "experiences": {"entries": 3, "bullets": 6},
    "projects": {"entries": 4, "bullets": 3},
    "leadership": {"entries": 2, "bullets": 1},
}
EXPERIENCE_BULLET_CAPS = (6, 4, 4)
PORTFOLIO_FLOORS = {
    "experiences": {"entries": 3, "bullets": 3},
    "projects": {"entries": 4, "bullets": 2},
    "leadership": {"entries": 1, "bullets": 1},
}
_EVIDENCE_GRAPH_CACHE: Dict[str, Dict[str, Any]] = {}
_CURRENT_SCORE_CACHE: Dict[str, Any] = {}
FORBIDDEN_CONTENT_COMMANDS = re.compile(
    r"\\(?:documentclass|usepackage|geometry|fontsize|newcommand|renewcommand|"
    r"setlength|addtolength|scalebox|resizebox|vspace|hspace|small|footnotesize|"
    r"scriptsize|tiny|normalsize|large|Large|LARGE|huge|Huge|section|subsection|"
    r"begin|end|newpage|clearpage|pagebreak)\b",
    flags=re.I,
)


def repo_root() -> Path:
    configured = os.environ.get("RADAR_ROOT")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parent.parent


def cv_root(root: Optional[Path] = None) -> Path:
    configured = os.environ.get("CV_ROOT")
    return Path(configured).expanduser().resolve() if configured else (root or repo_root()) / "CV"


def studio_root(root: Optional[Path] = None) -> Path:
    return cv_root(root) / ".resume_studio"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resume calibration can run several private cases concurrently.  A fixed
    # ``file.json.tmp`` name lets otherwise unrelated workers replace or
    # delete one another's temporary file.
    tmp = path.with_name(
        ".%s.%s.%s.tmp" % (path.name, os.getpid(), threading.get_ident())
    )
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return default


def load_jobs(root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    path = (root or repo_root()) / "state" / "jobs.json"
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def current_scored_jobs(root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Read-only v8 score projection for the local Studio.

    Production state is rebuilt by the crawler.  The Studio can be opened
    between crawler runs, so it projects stale records through the current
    deterministic equation in memory instead of displaying/sorting v7 scores.
    """
    base = (root or repo_root()).resolve()
    jobs_path = base / "state" / "jobs.json"
    feedback_path = base / "state" / "feedback.json"
    culture_path = base / "state" / "culture.json"
    company_research_path = base / "state" / "company_research.json"
    profile_path = base / "profile.yaml"
    signature = tuple(
        path.stat().st_mtime_ns if path.exists() else 0
        for path in (jobs_path, feedback_path, culture_path, company_research_path, profile_path)
    )
    key = str(base)
    cached = _CURRENT_SCORE_CACHE.get(key)
    if isinstance(cached, dict) and cached.get("signature") == signature:
        return cached["jobs"]

    from radar import posting, quality
    from radar.models import Job
    import radar.score as score_mod
    from radar.score import (RULES_VERSION, early_career_possible,
                             explicit_new_grad, gates, score,
                             source_new_grad)

    records = copy.deepcopy(load_jobs(base))
    feedback = read_json(feedback_path, {}) or {}
    score_mod._CULTURE_CACHE = None
    score_mod._CULTURE_MATCH_CACHE = {}
    score_mod._COMPANY_RESEARCH_CACHE = None
    now = int(time.time())
    for rec in records.values():
        if rec.get("score_version") == RULES_VERSION and rec.get("rules_v") == RULES_VERSION:
            continue
        job = Job(
            company=rec.get("company", ""), title=rec.get("title", ""),
            url=rec.get("url", ""), source=rec.get("source", ""),
            locations=rec.get("locations", []), salary=rec.get("salary", ""),
            remote=bool(rec.get("remote")), posted_at=rec.get("posted_at"),
            ats=rec.get("ats", ""), sector=rec.get("sector", ""),
        )
        keep, alert_eligible, gate_reasons = gates(job)
        score(job, feedback, now)
        rec.update({
            "score": job.score,
            "score_raw": job.score_raw,
            "score_calibrated": job.score_calibrated,
            "score_dimensions": job.score_dimensions,
            "score_reasons": job.score_reasons + gate_reasons,
            "alert_ok": bool(keep and alert_eligible),
            "explicit_new_grad": explicit_new_grad(job.title) or source_new_grad(job),
            "rules_v": RULES_VERSION,
            "score_version": RULES_VERSION,
            "early_career_possible": early_career_possible(job, rec.get("posting")),
        })
        if rec.get("quality"):
            quality.reapply(rec)
        if rec.get("posting"):
            posting.reapply(rec)
    _CURRENT_SCORE_CACHE[key] = {"signature": signature, "jobs": records}
    return records


def evidence_graph(root: Optional[Path] = None, refresh_public: bool = False) -> Dict[str, Any]:
    base = (root or repo_root()).resolve()
    key = str(base)
    if refresh_public or key not in _EVIDENCE_GRAPH_CACHE:
        graph = build_evidence_graph(
            cv_root(base), studio_root(base), source_catalog(base), refresh_public=refresh_public
        )
        _EVIDENCE_GRAPH_CACHE[key] = graph
        serializable = {name: value for name, value in graph.items() if not name.startswith("_runtime_")}
        write_json(studio_root(base) / "evidence_graph.json", serializable)
    return _EVIDENCE_GRAPH_CACHE[key]


def _match_cache_path(root: Optional[Path] = None) -> Path:
    return studio_root(root or repo_root()) / "resume_matches.json"


def load_match_cache(root: Optional[Path] = None) -> Dict[str, Any]:
    value = read_json(_match_cache_path(root), {})
    return value if isinstance(value, dict) else {}


def resume_match_for_job(
    job: Dict[str, Any],
    root: Optional[Path] = None,
    posting_text: str = "",
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base = root or repo_root()
    graph = evidence_graph(base)
    matches = cache if cache is not None else load_match_cache(base)
    cache_key = str(job.get("id") or "")
    digest = job_match_hash(job, posting_text)
    existing = matches.get(cache_key)
    if (
        isinstance(existing, dict)
        and existing.get("version") == MATCH_VERSION
        and existing.get("graph_hash") == graph.get("hash")
        and existing.get("job_hash") == digest
    ):
        return existing
    result = score_resume_match(job, graph, posting_text=posting_text)
    result["job_hash"] = digest
    result["scored_at"] = int(time.time())
    matches[cache_key] = result
    if cache is None:
        write_json(_match_cache_path(base), matches)
    return result


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _braced_argument(source: str, start: int) -> Tuple[str, int]:
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source) or source[start] != "{":
        raise ValueError("expected a braced LaTeX argument")
    depth = 0
    index = start
    while index < len(source):
        char = source[index]
        escaped = index > 0 and source[index - 1] == "\\"
        if char == "{" and not escaped:
            depth += 1
        elif char == "}" and not escaped:
            depth -= 1
            if depth == 0:
                return source[start + 1 : index].strip(), index + 1
        index += 1
    raise ValueError("unclosed LaTeX argument")


def _macro_calls(source: str, macro: str, arg_count: int) -> List[Tuple[int, List[str], int]]:
    calls: List[Tuple[int, List[str], int]] = []
    needle = "\\" + macro
    cursor = 0
    while True:
        start = source.find(needle, cursor)
        if start < 0:
            return calls
        boundary = start + len(needle)
        if boundary < len(source) and source[boundary].isalpha():
            cursor = boundary
            continue
        args: List[str] = []
        position = boundary
        try:
            for _ in range(arg_count):
                argument, position = _braced_argument(source, position)
                args.append(argument)
        except ValueError:
            cursor = boundary
            continue
        calls.append((start, args, position))
        cursor = position


def _section_at(source: str, position: int) -> str:
    section = ""
    for match in re.finditer(r"\\section\{([^{}]+)\}", source):
        if match.start() > position:
            break
        section = match.group(1).strip()
    return section


def _plain_heading(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^\\(?:large|normalsize|small)\s*", "", value)
    return value.strip()


def _slug(value: str) -> str:
    value = re.sub(r"\\[A-Za-z]+", " ", value)
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:48] or "entry"


def _resume_tokens(value: str) -> set:
    plain = _latex_plain(value).lower()
    tokens = re.findall(r"[a-z0-9]+", plain)
    ignored = {"a", "an", "and", "the", "to", "for", "of", "with", "via", "in", "on"}
    return {token for token in tokens if token not in ignored}


def _resume_text_similarity(left: str, right: str) -> float:
    left_tokens = _resume_tokens(left)
    right_tokens = _resume_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _same_resume_bullet(left: str, right: str) -> bool:
    # Resume variants routinely differ only by an article, tense, or an
    # abbreviated unit (``2.4s`` versus ``2.4 seconds``).  At 0.80 overlap the
    # shared technical object, mechanism, and proof are already the same
    # interview story; keeping both is repetition, not breadth.
    return _resume_text_similarity(left, right) >= 0.80


def _resume_numeric_anchors(value: str) -> set:
    plain = _latex_plain(value).lower().replace(",", "")
    return set(re.findall(r"\$?\d+(?:\.\d+)?\+?%?", plain))


def _same_entry_resume_bullet(left: str, right: str) -> bool:
    """Detect two phrasings of the same evidence inside one entry."""
    if _same_resume_bullet(left, right):
        return True
    shared_numbers = _resume_numeric_anchors(left) & _resume_numeric_anchors(right)
    if not shared_numbers:
        return False
    generic = {
        "built", "engineered", "developed", "designed", "implemented",
        "led", "using", "across", "real", "time", "data", "system",
    }
    shared_terms = {
        term for term in (_resume_tokens(left) & _resume_tokens(right)) - generic
        if re.search(r"[a-z]", term)
    }
    return bool(shared_terms)


def _missing_protected_qualifiers(source: str, candidate: str) -> List[str]:
    """Keep scope-limiting facts when an enhanced bullet is rewritten."""
    source_plain = _latex_plain(source).lower()
    candidate_plain = _latex_plain(candidate).lower()
    missing = []
    for qualifier in PROTECTED_QUALIFIERS:
        if qualifier in source_plain and qualifier not in candidate_plain:
            missing.append(qualifier)
    # POC is a common compact spelling of proof of concept. Either spelling
    # preserves the same limitation and should satisfy the guard.
    if "proof of concept" in missing and re.search(r"\bpoc\b", candidate_plain):
        missing.remove("proof of concept")
    if re.search(r"\bpoc\b", source_plain) and not (
        re.search(r"\bpoc\b", candidate_plain) or "proof of concept" in candidate_plain
    ):
        missing.append("POC")
    return missing


def _entry_bullets(source: str, after: int) -> List[str]:
    start = source.find("\\resumeItemListStart", after)
    if start < 0:
        return []
    next_section = source.find("\\section{", after)
    next_heading = min(
        [position for position in (
            source.find("\\resumeSubheading", after),
            source.find("\\resumeCompanySubheading", after),
            source.find("\\resumeProjectHeading", after),
        ) if position >= 0] or [len(source)]
    )
    if start > min(next_section if next_section >= 0 else len(source), next_heading):
        return []
    end = source.find("\\resumeItemListEnd", start)
    if end < 0:
        return []
    block = source[start:end]
    return [args[0] for _, args, _ in _macro_calls(block, "resumeItem", 1)]


def source_catalog(root: Optional[Path] = None) -> Dict[str, Any]:
    """Build a source-addressable bank from Victor's existing LaTeX only."""
    base = cv_root(root)
    entries: Dict[str, Dict[str, Any]] = {}
    # The canonical resume is the curated baseline and cv_full is the broad
    # responsibility bank.  Target-specific historical resumes are outputs,
    # not independent evidence sources; ingesting them created duplicate roles,
    # projects, and stale claims in generated portfolios.
    order = ("resume.tex", "cv_full.tex")
    keys: Dict[Tuple[str, str], str] = {}

    def merge_entry(kind: str, company: str, role: str, dates: str, location: str, bullets: List[str], source_name: str) -> None:
        key = (re.sub(r"\s+", " ", company.lower()), re.sub(r"\s+", " ", role.lower()))
        entry_id = keys.get(key)
        if entry_id is None:
            entry_id = "%s:%s" % (kind, _slug(company + "-" + role))
            suffix = 2
            original = entry_id
            while entry_id in entries:
                entry_id = "%s-%s" % (original, suffix)
                suffix += 1
            keys[key] = entry_id
            entries[entry_id] = {
                "id": entry_id,
                "kind": kind,
                "company": company,
                "role": role,
                "dates": dates,
                "location": location,
                "sources": [],
                "bullets": [],
            }
        entry = entries[entry_id]
        if source_name not in entry["sources"]:
            entry["sources"].append(source_name)
        known = [item["text"] for item in entry["bullets"]]
        for bullet in bullets:
            if any(_same_resume_bullet(bullet, existing) for existing in known):
                continue
            bullet_id = "%s:b%s" % (entry_id, len(entry["bullets"]) + 1)
            entry["bullets"].append({"id": bullet_id, "text": bullet, "source": source_name})
            known.append(bullet)

    project_keys: Dict[str, str] = {}
    for source_name in order:
        path = base / source_name
        if not path.exists():
            continue
        source = path.read_text()
        heading_calls = _macro_calls(source, "resumeSubheading", 4) + _macro_calls(source, "resumeCompanySubheading", 4)
        for start, args, after in sorted(heading_calls, key=lambda item: item[0]):
            section = _section_at(source, start).lower()
            if "education" in section:
                continue
            macro_is_company = source.startswith("\\resumeCompanySubheading", start)
            if macro_is_company:
                role, dates, company, location = args
            else:
                company, dates, role, location = args
            kind = "leadership" if "leadership" in section or "extracurricular" in section else "experience"
            merge_entry(
                kind,
                _plain_heading(company),
                _plain_heading(role),
                _plain_heading(dates),
                _plain_heading(location),
                _entry_bullets(source, after),
                source_name,
            )
        for start, args, after in _macro_calls(source, "resumeProjectHeading", 2):
            if "project" not in _section_at(source, start).lower():
                continue
            heading = _plain_heading(args[0])
            bullets = _entry_bullets(source, after)
            key = re.sub(r"\s+", " ", heading.lower())
            entry_id = project_keys.get(key)
            if entry_id is None:
                for candidate_id, candidate in entries.items():
                    if candidate.get("kind") != "project":
                        continue
                    heading_match = _resume_text_similarity(heading, str(candidate.get("heading") or "")) >= 0.72
                    bullet_match = any(
                        _same_resume_bullet(incoming, existing.get("text", ""))
                        for incoming in bullets
                        for existing in candidate.get("bullets", [])
                    )
                    if heading_match or bullet_match:
                        entry_id = candidate_id
                        project_keys[key] = entry_id
                        break
            if entry_id is None:
                entry_id = "project:%s" % _slug(heading)
                suffix = 2
                original = entry_id
                while entry_id in entries:
                    entry_id = "%s-%s" % (original, suffix)
                    suffix += 1
                project_keys[key] = entry_id
                entries[entry_id] = {
                    "id": entry_id,
                    "kind": "project",
                    "heading": heading,
                    "sources": [],
                    "bullets": [],
                }
            entry = entries[entry_id]
            if source_name not in entry["sources"]:
                entry["sources"].append(source_name)
            known = [item["text"] for item in entry["bullets"]]
            for bullet in bullets:
                if any(_same_resume_bullet(bullet, existing) for existing in known):
                    continue
                bullet_id = "%s:b%s" % (entry_id, len(entry["bullets"]) + 1)
                entry["bullets"].append({"id": bullet_id, "text": bullet, "source": source_name})
                known.append(bullet)
    return {"template": CANONICAL_TEMPLATE, "entries": entries}


def catalog_for_prompt(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    packed = []
    for entry in catalog.get("entries", {}).values():
        item = {key: value for key, value in entry.items() if key not in {"sources"}}
        packed.append(item)
    return packed


def job_matches(job: Dict[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(
        [
            str(job.get("company", "")),
            str(job.get("title", "")),
            str(job.get("sector", "")),
            str(job.get("description", "")),
            " ".join(job.get("locations") or []),
        ]
    ).lower()
    return all(part in haystack for part in query.lower().split())


def job_summary(job: Dict[str, Any], match: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    summary = {
        "id": job.get("id", ""),
        "company": job.get("company", ""),
        "title": job.get("title", ""),
        "url": job.get("url", ""),
        "locations": job.get("locations", []),
        "sector": job.get("sector", ""),
        "score": job.get("score", 0),
        "alert_ok": bool(job.get("alert_ok")),
        "early_career_possible": bool(job.get("early_career_possible")),
        "posted_at": job.get("posted_at"),
        "description_available": bool(job.get("description")),
    }
    if isinstance(match, dict):
        summary["resume_match"] = {
            "score": match.get("score"),
            "confidence": match.get("confidence", "low"),
            "version": match.get("version", MATCH_VERSION),
        }
    return summary


def list_jobs(
    root: Optional[Path] = None,
    query: str = "",
    limit: int = 200,
    sort_by: str = "best",
) -> List[Dict[str, Any]]:
    base = root or repo_root()
    jobs = [
        job for job in current_scored_jobs(base).values()
        if not job.get("closed_at") and job_matches(job, query)
    ]
    cached = load_match_cache(base)
    graph = evidence_graph(base)
    matches: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        cached_match = cached.get(str(job.get("id") or ""))
        if (
            isinstance(cached_match, dict)
            and cached_match.get("version") == MATCH_VERSION
            and cached_match.get("graph_hash") == graph.get("hash")
        ):
            matches[str(job.get("id") or "")] = cached_match
        elif sort_by == "resume_match":
            matches[str(job.get("id") or "")] = score_resume_match(job, graph)

    if sort_by == "newest":
        jobs.sort(key=lambda job: (int(job.get("posted_at") or 0), int(job.get("score") or 0)), reverse=True)
    elif sort_by == "resume_match":
        jobs.sort(
            key=lambda job: (
                int((matches.get(str(job.get("id") or "")) or {}).get("score") or -1),
                bool(job.get("alert_ok")),
                int(job.get("score") or 0),
                int(job.get("posted_at") or 0),
            ),
            reverse=True,
        )
    else:
        jobs.sort(
            key=lambda job: (
                bool(job.get("alert_ok")),
                int(job.get("score") or 0),
                int(job.get("posted_at") or 0),
            ),
            reverse=True,
        )
    return [
        job_summary(job, matches.get(str(job.get("id") or "")))
        for job in jobs[: max(1, min(limit, 500))]
    ]


def fetch_job_description(job: Dict[str, Any]) -> str:
    existing = clean_text(str(job.get("description") or ""))
    if len(existing) >= 300:
        return existing[:MAX_POSTING_CHARS]
    url = str(job.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return existing
    # Workday/Oracle/Eightfold pages are JavaScript shells.  Reuse Radar's
    # first-party ATS readers so tailoring and full-match analysis receive the
    # real posting rather than a title-only approximation.
    try:
        from radar import quality
        if quality.spa_kind(job):
            _, spa_text = quality.fetch_posting_spa(job)
            spa_text = clean_text(spa_text)
            if len(spa_text) >= 300:
                return spa_text[:MAX_POSTING_CHARS]
    except Exception:
        pass
    try:
        response = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "JobRadar-ResumeStudio/1.0 (local owner tool)"},
        )
        text = clean_text(response.text)
        if response.ok and len(text) >= 300:
            return text[:MAX_POSTING_CHARS]
    except requests.RequestException:
        pass
    return existing


def provider_commands() -> Dict[str, Optional[str]]:
    return {"codex": shutil.which("codex"), "claude": shutil.which("claude")}


def subscription_environment(run_dir: Path) -> Dict[str, str]:
    """Prefer cached first-party subscription sessions over API keys.

    The local CLIs own their credential stores.  Removing these variables is a
    cost/privacy guard: a stray shell API key must not silently turn this into
    billable API traffic.
    """
    env = dict(os.environ)
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
    ):
        env.pop(name, None)
    env["RESUME_STUDIO_RUN_DIR"] = str(run_dir)
    return env


def extract_json(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("result"), str):
            nested = extract_json(value["result"])
            if nested is not None:
                return nested
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    for candidate in (text, re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("result"), str):
                    nested = extract_json(parsed["result"])
                    if nested is not None:
                        return nested
                return parsed
        except ValueError:
            continue
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def response_data(raw: str) -> Dict[str, Any]:
    parsed = extract_json(raw)
    if parsed is None:
        return {"parse_error": "provider did not return a JSON object", "raw_excerpt": raw[-4000:]}
    return parsed


def useful_provider_data(data: Dict[str, Any], label: str) -> bool:
    if not isinstance(data, dict) or data.get("is_error") or data.get("api_error_status"):
        return False
    if label.startswith("review"):
        criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else data
        return any(
            isinstance(value, dict) and str(value.get("status", "")).lower() in STATUS_MULTIPLIER
            for value in criteria.values()
        )
    return bool(data.get("experiences")) and bool(data.get("projects"))


def plan_schema(enhance: bool) -> Dict[str, Any]:
    bullet_properties: Dict[str, Any] = {
        "source_id": {"type": "string"},
        "priority": {"type": "integer", "minimum": 1, "maximum": 100},
    }
    # Codex strict structured outputs require every declared object property in
    # ``required``.  Priority is therefore explicit in both modes; validation
    # still clamps it defensively for other providers.
    bullet_required = ["source_id", "priority"]
    if enhance:
        bullet_properties["text"] = {"type": "string"}
        bullet_properties["evidence_ids"] = {
            "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8
        }
        bullet_properties["candidate_rationale"] = {"type": "string"}
        bullet_required.extend(["text", "evidence_ids", "candidate_rationale"])
    bullet = {
        "type": "object",
        "properties": bullet_properties,
        "required": bullet_required,
        "additionalProperties": False,
    }

    def selection(min_bullets: int, max_bullets: int) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "bullets": {
                    "type": "array",
                    "items": bullet,
                    "minItems": min_bullets,
                    "maxItems": max_bullets,
                },
                "why": {"type": "string"},
            },
            "required": ["source_id", "bullets", "why"],
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {
            "positioning_thesis": {"type": "string"},
            "selected_evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"source": {"type": "string"}, "why": {"type": "string"}},
                    "required": ["source", "why"],
                    "additionalProperties": False,
                },
            },
            "excluded_evidence": {"type": "array", "items": {"type": "string"}},
            "experiences": {
                "type": "array",
                "items": selection(3, 6),
                "minItems": 3,
                "maxItems": 3,
            },
            "projects": {
                "type": "array",
                "items": selection(2, 3),
                "minItems": 4,
                "maxItems": 5,
            },
            "leadership": {
                "type": "array",
                "items": selection(1, 1),
                "minItems": 1,
                "maxItems": 2,
            },
            "revision_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "positioning_thesis",
            "selected_evidence",
            "excluded_evidence",
            "experiences",
            "projects",
            "leadership",
            "revision_notes",
        ],
        "additionalProperties": False,
    }


def review_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "criteria": {
                "type": "object",
                "properties": {
                    name: {
                        "type": "object",
                        "properties": {"status": {"type": "string"}, "reason": {"type": "string"}},
                        "required": ["status", "reason"],
                        "additionalProperties": False,
                    }
                    for name in REVIEW_CRITERIA
                },
                "required": list(REVIEW_CRITERIA),
                "additionalProperties": False,
            },
            "unsupported_claims": {"type": "array", "items": {"type": "string"}},
            "missing_evidence": {"type": "array", "items": {"type": "string"}},
            "revision_priorities": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["criteria", "unsupported_claims", "missing_evidence", "revision_priorities"],
        "additionalProperties": False,
    }


def reviewed_plan_schema(enhance: bool) -> Dict[str, Any]:
    """Return one structured contract for critique plus the corrected plan.

    The old pipeline paid for an adversarial review but only displayed its
    complaints.  Requiring a complete corrected plan makes that same call do
    useful final-edit work without adding a third frontier-model pass.
    """
    schema = review_schema()
    schema["properties"]["final_plan"] = plan_schema(enhance)
    schema["required"].append("final_plan")
    return schema


def provider_data_from_files(stdout_path: Path, stderr_path: Path, label: str) -> Optional[Dict[str, Any]]:
    """Recover a final response even if a CLI lingers after emitting it."""
    for path in (stdout_path, stderr_path):
        try:
            raw = path.read_text(errors="replace")
        except OSError:
            continue
        if not raw.strip():
            continue
        candidates = [raw] + list(reversed(raw.splitlines()))
        for candidate in candidates:
            parsed = extract_json(candidate)
            if not isinstance(parsed, dict):
                continue
            data = response_data(json.dumps(parsed, ensure_ascii=False))
            if useful_provider_data(data, label):
                return data
    return None


def provider_usage_tokens(stderr_path: Path) -> Optional[int]:
    """Read the Codex CLI's emitted token total when it reports one."""
    try:
        raw = stderr_path.read_text(errors="replace")
    except OSError:
        return None
    matches = re.findall(r"tokens used\s*[\r\n]+\s*([\d,]+)", raw, flags=re.I)
    return int(matches[-1].replace(",", "")) if matches else None


def run_provider(
    provider: str,
    prompt: str,
    run_dir: Path,
    label: str,
    timeout: int = RUN_TIMEOUT_SECONDS,
    schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    commands = provider_commands()
    executable = commands.get(provider)
    if not executable:
        return {"provider": provider, "ok": False, "error": "CLI not installed"}
    prompt_path = run_dir / ("prompt_" + label + "_" + provider + ".txt")
    stdout_path = run_dir / ("stdout_" + label + "_" + provider + ".txt")
    stderr_path = run_dir / ("stderr_" + label + "_" + provider + ".txt")
    prompt_path.write_text(prompt)
    schema = schema or (review_schema() if label.startswith("review") else plan_schema(False))
    schema_path = run_dir / ("schema_" + label + "_" + provider + ".json")
    write_json(schema_path, schema)
    if provider == "codex":
        effort_variable = (
            "RESUME_STUDIO_REVIEW_CODEX_EFFORT"
            if label.startswith("review")
            else "RESUME_STUDIO_CODEX_EFFORT"
        )
        default_effort = "medium" if label.startswith("review") else "high"
        configured_effort = os.environ.get(effort_variable, default_effort).strip().lower()
        if configured_effort not in {"low", "medium", "high", "max"}:
            configured_effort = "high"
        args = [
            executable,
            "exec",
            "-c",
            "model_reasoning_effort=" + configured_effort,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-schema",
            str(schema_path),
            "-o",
            str(stdout_path),
            "-",
        ]
    else:
        args = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            "--add-dir",
            str(cv_root(repo_root())),
        ]
    started = time.time()
    stdout_path.touch()
    try:
        with stderr_path.open("w") as err, stdout_path.open("w") as out:
            proc = subprocess.Popen(
                args,
                cwd=str(repo_root()),
                env=subscription_environment(run_dir),
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                text=True,
            )
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            timed_out = False
            while proc.poll() is None:
                if time.time() - started >= timeout:
                    timed_out = True
                    proc.kill()
                    proc.wait(timeout=5)
                    break
                time.sleep(0.25)
        data = provider_data_from_files(stdout_path, stderr_path, label)
        if data is not None:
            return {
                "provider": provider,
                "ok": True,
                "elapsed_seconds": round(time.time() - started, 1),
                "data": data,
                "usage_tokens": provider_usage_tokens(stderr_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }
        if timed_out:
            return {
                "provider": provider,
                "ok": False,
                "error": "timed out after %ss" % timeout,
                "elapsed_seconds": round(time.time() - started, 1),
                "usage_tokens": provider_usage_tokens(stderr_path),
                "stderr_path": str(stderr_path),
            }
        if proc.returncode != 0:
            return {
                "provider": provider,
                "ok": False,
                "error": "CLI exited with code %s" % proc.returncode,
                "elapsed_seconds": round(time.time() - started, 1),
                "usage_tokens": provider_usage_tokens(stderr_path),
                "stderr_path": str(stderr_path),
            }
        data = response_data(stdout_path.read_text() if stdout_path.exists() else "")
        return {
            "provider": provider,
            "ok": useful_provider_data(data, label),
            "elapsed_seconds": round(time.time() - started, 1),
            "data": data,
            "usage_tokens": provider_usage_tokens(stderr_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    except OSError as exc:
        return {"provider": provider, "ok": False, "error": str(exc)}


def job_context(job: Dict[str, Any]) -> Dict[str, Any]:
    context = dict(job)
    context["posting_text"] = fetch_job_description(job)
    context["local_sources"] = [
        "CV/cv_full.tex",
        "CV/tldp_resume.tex",
        "CV/RESUME_TAILORING_PLAYBOOK.md",
        "CV/RESUME_BULLET_METHODOLOGY.md",
        "CV/RESUME_NOTES.md",
        "CV/JJ_RESUME_CONTEXT.md",
        "CV/experiences/",
    ]
    return context


def resume_methodology_context(root: Optional[Path] = None) -> str:
    """Inline the two governing methods so model calls stay self-contained."""
    cv = cv_root(root or repo_root())
    parts = []
    for name in ("RESUME_TAILORING_PLAYBOOK.md", "RESUME_BULLET_METHODOLOGY.md"):
        try:
            parts.append("# " + name + "\n" + (cv / name).read_text())
        except OSError:
            continue
    return "\n\n".join(parts)


def base_prompt(
    context: Dict[str, Any],
    role: str,
    catalog: Dict[str, Any],
    enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
) -> str:
    role_guardrails = """
Victor-specific guardrails:
- The master CV is a responsibility/evidence bank, not a keyword dump.
- Never invent metrics, users, adoption, production status, scope, accuracy,
  dates, technologies, or business outcomes.
- Every selected claim must be traceable to CV/ source material.
- Projects may be selected for prestige or award value even when their stack is
  not a direct keyword match, but explain that tradeoff.
- Prefer one distinct job per bullet: leadership, technical artifact, result,
  operating scope, communication, or prestige.
- CV/resume.tex is the immutable visual template. You are selecting content,
  not designing a resume. The harness, not you, renders the LaTeX.
- Never read CV/.resume_studio/ or use earlier generated resumes/reports as
  evidence. They are outputs, not authority.
- Use the complete CV/RESUME_TAILORING_PLAYBOOK.md and
  CV/RESUME_BULLET_METHODOLOGY.md. Use CV/cv_full.tex as the responsibility
  bank and the experience dossiers as higher-authority factual sources.
- CV/resume.tex is authoritative for the immutable contact, education, skills,
  employer-heading metadata, and dates that the renderer copies. Treat older
  conflicting metadata in cv_full.tex or target-specific resumes as stale;
  those files authorize bullet evidence, not replacement template metadata.
- Preserve qualifiers such as prototype, synthetic, simulation, or demo when
  they distinguish the work from production or real-user deployment.
- Employer entries are rendered company-first, then role/title. TLDP is one
  target program, not a generic style.
- Return a rich but ranked candidate pool: all 3 established experiences with
  3-6 bullets each, 4-5 projects with 2-3 bullets each, and 1-2 leadership
  entries with one bullet each. Every bullet must introduce a distinct,
  defensible interview thread; lower-ranked candidates are useful packing
  alternatives, not permission to repeat the same story.
- Victor's immutable human references contain 24-26 meaningful bullets and
  reach the bottom of the page. The deterministic packer targets that same
  evidence density while capping the final page at 3 experiences, 4 projects,
  2 leadership entries, and 26 bullets. A large blank bottom region is a hard
  failure. Fill space with distinct evidence, never filler wording.
- Assign priority 1-100 to every bullet based on target relevance, proof,
  distinctiveness, and interview value. Do not inflate every priority.
- Return source IDs from the supplied catalog. Never return a LaTeX document,
  preamble, section command, margin, font size, spacing command, or page break.
""".strip()
    if enhance:
        role_guardrails += (
            "\n- Enhancement mode may tighten a selected bullet's wording, but every bullet must retain its source_id, "
            "remain source-grounded, use only inline \\textbf/\\emph emphasis, and follow the methodology. "
            "Cite every fact-bearing source in evidence_ids and explain why the candidate improves the benchmark. "
            "Public GitHub/Devpost nodes corroborate breadth but cannot authorize a claim by themselves."
        )
    else:
        role_guardrails += (
            "\n- Source-only mode is selection, not rewriting. Choose source IDs only; the harness will copy every heading and bullet verbatim."
        )
    if "johnson" in str(context.get("company", "")).lower():
        role_guardrails += "\n- For Johnson & Johnson, TICC is not a priority; exclude it unless the posting makes it clearly relevant."
    else:
        role_guardrails += "\n- Evaluate TICC and other leadership evidence against this target; do not apply Johnson & Johnson-specific exclusions."
    context_text = json.dumps(context, indent=2, ensure_ascii=False)
    catalog_text = json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)
    graph_text = json.dumps(
        evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [],
        indent=2,
        ensure_ascii=False,
    )
    return (
        "You are the "
        + role
        + " in Victor Jimenez's private resume studio.\n"
        + role_guardrails
        + "\n\nThis request is self-contained. Do not inspect the filesystem, run commands, or read prior outputs; "
        "the governing methodology and authorized evidence are supplied below. Return only the structured "
        "selection requested by the schema. Order entries and bullets strongest-first.\n\n"
        "Governing methodology:\n"
        + resume_methodology_context(repo_root())[:MAX_PROMPT_CHARS]
        + "\n\n"
        "Job context:\n"
        + context_text[:MAX_PROMPT_CHARS]
        + "\n\nSource-addressable evidence catalog:\n"
        + catalog_text[:MAX_PROMPT_CHARS]
        + "\n\nTarget-ranked evidence graph nodes (authority and claim_allowed are binding):\n"
        + graph_text[:MAX_PROMPT_CHARS]
    )


def synthesis_prompt(
    context: Dict[str, Any], drafts: List[Dict[str, Any]], catalog: Dict[str, Any], enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
) -> str:
    packed = []
    for draft in drafts:
        data = draft.get("data") or {}
        packed.append(
            {
                "provider": draft.get("provider"),
                "positioning_thesis": data.get("positioning_thesis", ""),
                "selected_evidence": data.get("selected_evidence", []),
                "excluded_evidence": data.get("excluded_evidence", []),
                "experiences": data.get("experiences", []),
                "projects": data.get("projects", []),
                "leadership": data.get("leadership", []),
                "revision_notes": data.get("revision_notes", []),
            }
        )
    return (
        "You are the senior resume evidence editor synthesizing competing plans for Victor.\n"
        "This request is self-contained. Do not inspect the filesystem or run commands; use the supplied source IDs "
        "and evidence only. Do not edit files and do not return a LaTeX document. "
        "CV/resume.tex remains immutable and the harness renders it. Return a rich, strongest-first ranked pool sized "
        "to match the full human-authored reference page; each bullet must earn its place through target fit, proof, "
        "and a distinct interview story. "
        + ("You may tighten bullet text but must retain its source_id and facts. " if enhance else "Select source IDs verbatim; do not rewrite bullets. ")
        + "Choose the stronger defensible plan rather than averaging it.\n\n"
        "Job context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nEvidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nTarget-ranked evidence graph:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [], indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nCompeting drafts:\n"
        + json.dumps(packed, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
    )


def reviewer_prompt(
    context: Dict[str, Any], tex: str,
    plan: Optional[Dict[str, Any]] = None,
    graph_context: Optional[List[Dict[str, Any]]] = None,
    catalog: Optional[Dict[str, Any]] = None,
) -> str:
    return (
        "You are an adversarial final resume editor. This is a fresh review: do not "
        "trust the generation agents, their explanations, or any score they may "
        "have claimed. This request is self-contained: do not inspect the filesystem, "
        "run commands, or read any prior generated resume/report. Use only the target, "
        "plan, catalog, and authorized evidence supplied below. Inspect the proposed content "
        "plan, correct it, and return JSON only. "
        "Do not assign a numeric score; the harness calculates it. final_plan must be a "
        "complete, strongest-first replacement plan. Keep all three established experiences, "
        "four or five complementary projects, and one or two leadership entries so the "
        "deterministic packer can produce the same full-page evidence density as Victor's "
        "human references. You may reorder, replace, or rewrite bullets using authorized "
        "source IDs. Remove overlap and unsupported implications; do not solve criticism by "
        "making the page sparse. Then grade the FINAL plan you return, not the input draft. "
        "unsupported_claims must describe only claims still present in final_plan.\n\n"
        "Authority rule: CV/resume.tex is canonical for the immutable contact, "
        "education, skills, employer-heading metadata, and dates copied by the "
        "renderer. Do not mark those fields unsupported merely because stale "
        "metadata differs in cv_full.tex or a historical target resume. Experience "
        "dossiers remain higher authority for bullet facts, and prototype/synthetic "
        "qualifiers must remain explicit when relevant.\n\n"
        "For each criterion return pass, partial, or fail plus a short reason:\n"
        "factual: every claim is source-grounded and interview-safe\n"
        "target_fit: the portfolio answers this role's actual needs\n"
        "evidence: bullets contain concrete objects, scope, outcomes, or stakes\n"
        "clarity: a recruiter can understand the argument in a six-second skim\n"
        "portfolio: selected items are complementary and exclusions are sensible\n"
        "A final plan with 22-26 one-line, distinct bullets is intentionally benchmarked to "
        "Victor's human references. Do not call it unclear merely because of bullet count; "
        "penalize actual repetition, weak hierarchy, or hard-to-parse writing.\n"
        "Also return unsupported_claims (array), missing_evidence (array), and "
        "revision_priorities (array). Never make the test easier because the "
        "draft is polished.\n\n"
        "Fixed rubric version: "
        + RUBRIC_VERSION
        + "\nTarget context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nBullet provenance plan:\n"
        + json.dumps(plan or {}, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nSource-addressable evidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog or {}), indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nAuthorized evidence context:\n"
        + json.dumps(graph_context or [], indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
    )


def line_editor_prompt(
    context: Dict[str, Any], plan: Dict[str, Any], layout: Dict[str, Any],
    graph: Dict[str, Any],
) -> str:
    return (
        "You are Victor's final one-line resume editor. This request is self-contained; do not inspect the filesystem "
        "or run commands. Preserve every selected entry, bullet source_id, "
        "evidence_id, fact, priority, and section order. Change only bullet text. For wrapped lines, cut filler "
        "and compress clauses without losing the technical object or proof. A concise line may end early; never "
        "expand a bullet merely to approach the right margin. Do not pad, invent, change layout, or return LaTeX beyond inline textbf/emph. Return the complete "
        "structured plan under the same schema.\n\nTarget:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nCurrent plan:\n"
        + json.dumps(plan, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nRendered bullet geometry:\n"
        + json.dumps((layout.get("horizontal") or {}).get("bullets", []), indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nRelevant evidence:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")), indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
    )


def _plan_source_signature(plan: Dict[str, Any]) -> List[Tuple[str, Tuple[str, ...]]]:
    return [
        (str(entry.get("source_id")), tuple(str(bullet.get("source_id")) for bullet in entry.get("bullets", [])))
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
    ]


def _normalize_model_fragment(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return (
        value.strip()
        .replace("\u2011", "-")
        .replace("\u2013", "--")
        .replace("\u2014", "---")
        # Models occasionally emit a math-only multiplication command in
        # prose. The intended glyph is unambiguous and plain ``x`` is safer in
        # both LaTeX and ATS text extraction.
        .replace("\\times{}", "x")
        .replace("\\times", "x")
    )


def _unsupported_inline_commands(value: str) -> List[str]:
    allowed = {"textbf", "emph"}
    return sorted(
        {
            match.group(1)
            for match in re.finditer(r"\\([A-Za-z]+)", value)
            if match.group(1) not in allowed
        }
    )


def validate_plan(
    plan: Dict[str, Any],
    catalog: Dict[str, Any],
    enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    entries = catalog.get("entries", {})
    evidence_ids = {str(node.get("id")) for node in (graph or {}).get("nodes", [])}
    claim_authorities = {
        str(node.get("id")) for node in (graph or {}).get("nodes", [])
        if node.get("claim_allowed")
    }
    errors: List[str] = []
    normalized = dict(plan)
    validation_warnings: List[str] = []
    used_entries = set()
    used_bullets = set()
    for kind, minimum, maximum in (("experiences", 3, 3), ("projects", 4, 5), ("leadership", 1, 2)):
        selections = plan.get(kind)
        if not isinstance(selections, list):
            errors.append("%s must be a list" % kind)
            normalized[kind] = []
            continue
        if len(selections) < minimum or len(selections) > maximum:
            errors.append("%s must contain %s-%s entries" % (kind, minimum, maximum))
        normalized_selections = []
        expected_kind = {"experiences": "experience", "projects": "project", "leadership": "leadership"}[kind]
        for selection in selections:
            if not isinstance(selection, dict):
                errors.append("%s selection is not an object" % kind)
                continue
            entry_id = str(selection.get("source_id") or "")
            entry = entries.get(entry_id)
            if not entry or entry.get("kind") != expected_kind:
                errors.append("unknown %s source_id: %s" % (expected_kind, entry_id))
                continue
            if entry_id in used_entries:
                errors.append("duplicate entry: %s" % entry_id)
                continue
            used_entries.add(entry_id)
            bullet_bank = {item["id"]: item["text"] for item in entry.get("bullets", [])}
            selected_bullets = []
            for bullet in selection.get("bullets") or []:
                if not isinstance(bullet, dict):
                    errors.append("invalid bullet selection for %s" % entry_id)
                    continue
                bullet_id = str(bullet.get("source_id") or "")
                if bullet_id not in bullet_bank:
                    # Providers occasionally hallucinate a neighboring bullet
                    # number.  There is no safe text to render, so discard it
                    # while preserving a machine-readable warning; a plan
                    # still fails below if every bullet for an entry is bad.
                    validation_warnings.append(
                        "dropped unknown bullet %s for %s" % (bullet_id, entry_id)
                    )
                    continue
                if bullet_id in used_bullets:
                    errors.append("duplicate bullet: %s" % bullet_id)
                    continue
                used_bullets.add(bullet_id)
                text = _normalize_model_fragment(bullet.get("text")) if enhance else bullet_bank[bullet_id]
                if enhance and not text:
                    errors.append("enhanced bullet %s has no text" % bullet_id)
                    continue
                if enhance:
                    missing_qualifiers = _missing_protected_qualifiers(
                        bullet_bank[bullet_id], text
                    )
                    if missing_qualifiers:
                        # The source-addressed wording is already authorized.
                        # Revert this one bullet instead of discarding an
                        # otherwise useful plan or allowing scope inflation.
                        validation_warnings.append(
                            "reverted enhanced bullet %s after it dropped protected qualifier(s): %s"
                            % (bullet_id, ", ".join(missing_qualifiers))
                        )
                        text = bullet_bank[bullet_id]
                if FORBIDDEN_CONTENT_COMMANDS.search(text):
                    errors.append("bullet %s contains a forbidden layout command" % bullet_id)
                    continue
                if enhance:
                    unsupported_commands = _unsupported_inline_commands(text)
                    if unsupported_commands:
                        errors.append(
                            "bullet %s contains unsupported inline command(s): %s"
                            % (bullet_id, ", ".join(unsupported_commands))
                        )
                        continue
                cited = [str(value) for value in (bullet.get("evidence_ids") or []) if str(value)]
                if enhance and graph is not None:
                    unknown = [value for value in cited if value not in evidence_ids]
                    if unknown:
                        validation_warnings.append(
                            "dropped unknown evidence for %s: %s"
                            % (bullet_id, ", ".join(unknown))
                        )
                        cited = [value for value in cited if value in evidence_ids]
                    if not cited:
                        errors.append("enhanced bullet %s has no evidence_ids" % bullet_id)
                        continue
                    if not set(cited) & claim_authorities:
                        errors.append("bullet %s has no claim-authorizing evidence" % bullet_id)
                        continue
                selected_bullets.append({
                    "source_id": bullet_id,
                    "text": text,
                    "evidence_ids": cited or [bullet_id],
                    "priority": max(1, min(100, int(bullet.get("priority") or 50))),
                    "candidate_rationale": str(bullet.get("candidate_rationale") or ""),
                })
            if not selected_bullets:
                errors.append("entry %s has no valid bullets" % entry_id)
                continue
            normalized_selections.append(
                {
                    "source_id": entry_id,
                    "bullets": selected_bullets,
                    "why": str(selection.get("why") or ""),
                }
            )
        normalized[kind] = normalized_selections
    if validation_warnings:
        normalized["validation_warnings"] = validation_warnings
    return normalized, errors


def _render_bullets(bullets: List[Dict[str, str]]) -> List[str]:
    lines = ["        \\resumeItemListStart"]
    for bullet in bullets:
        lines.extend(["            \\resumeItem{", "                " + bullet["text"], "            }"])
    lines.append("        \\resumeItemListEnd")
    return lines


def expand_candidate_portfolio(
    plan: Dict[str, Any], catalog: Dict[str, Any], enhance: bool
) -> Dict[str, Any]:
    """Build a balanced, source-safe packing pool from selected entries.

    The human references use 24-26 meaningful bullets. Providers rank the
    target narrative. When they return fewer than the 22-bullet acceptance
    floor, this pass adds authorized source-text alternatives one per entry per
    round. A complete model plan passes through unchanged so omitted evidence
    is not silently reintroduced. This avoids both the old 30-bullet dump and
    the later 16-20-bullet sparse page.
    """
    expanded = copy.deepcopy(plan)
    source_order = {
        entry_id: index
        for index, entry_id in enumerate((catalog.get("entries") or {}).keys())
    }
    expanded["experiences"] = sorted(
        expanded.get("experiences", []),
        key=lambda entry: source_order.get(str(entry.get("source_id")), 10**6),
    )
    selected_total = sum(
        len(entry.get("bullets", []))
        for section in ("experiences", "projects", "leadership")
        for entry in expanded.get(section, [])
    )
    if selected_total >= MIN_TOTAL_BULLETS:
        return expanded
    entries = catalog.get("entries", {})
    explicit_exclusions = "\n".join(str(value) for value in plan.get("excluded_evidence", []))
    queues: List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]] = []
    for section in ("experiences", "projects", "leadership"):
        for entry_index, selection in enumerate(expanded.get(section, [])):
            maximum = (
                EXPERIENCE_BULLET_CAPS[min(entry_index, len(EXPERIENCE_BULLET_CAPS) - 1)]
                if section == "experiences"
                else PORTFOLIO_CAPS[section]["bullets"]
            )
            entry = entries.get(selection.get("source_id")) or {}
            selected = {str(item.get("source_id")) for item in selection.get("bullets", [])}
            backups = []
            for source in entry.get("bullets", []):
                source_id = str(source.get("id") or "")
                if not source_id or source_id in selected or source_id in explicit_exclusions:
                    continue
                text = str(source.get("text") or "")
                backups.append({
                    "source_id": source_id,
                    "text": text,
                    "evidence_ids": [source_id],
                    "priority": max(20, min(55, round(_bullet_value({"text": text, "priority": 38})))),
                    "candidate_rationale": (
                        "Authorized source-text backup added by the deterministic overflow pool"
                        if enhance else ""
                    ),
                })
            backups.sort(key=_bullet_value, reverse=True)
            capacity = max(0, maximum - len(selection.get("bullets", [])))
            queues.append((section, selection, backups[:capacity]))

    # Round-robin filling preserves the references' portfolio breadth instead
    # of exhausting one experience before projects receive any alternatives.
    while selected_total < MAX_TOTAL_BULLETS:
        added = False
        for _, selection, backups in queues:
            if selected_total >= MAX_TOTAL_BULLETS:
                break
            if not backups:
                continue
            selection.setdefault("bullets", []).append(backups.pop(0))
            selected_total += 1
            added = True
        if not added:
            break
    return expanded


def curate_candidate_portfolio(
    candidate_plan: Dict[str, Any], catalog: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Apply the human-reference limits before compile-time packing."""
    curated = copy.deepcopy(candidate_plan)
    source_order = {
        entry_id: index
        for index, entry_id in enumerate(((catalog or {}).get("entries") or {}).keys())
    }
    if catalog:
        curated["experiences"] = sorted(
            curated.get("experiences", []),
            key=lambda entry: source_order.get(str(entry.get("source_id")), 10**6),
        )
    for section in ("experiences", "projects", "leadership"):
        cap = PORTFOLIO_CAPS[section]
        ranked_entries = []
        for original_index, entry in enumerate(curated.get(section, [])):
            bullet_cap = (
                EXPERIENCE_BULLET_CAPS[min(original_index, len(EXPERIENCE_BULLET_CAPS) - 1)]
                if section == "experiences"
                else cap["bullets"]
            )
            ranked_bullets = sorted(
                entry.get("bullets", []), key=_bullet_value, reverse=True
            )
            distinct = []
            local_seen: List[str] = []
            for bullet in ranked_bullets:
                text = str(bullet.get("text") or "")
                if any(_same_entry_resume_bullet(text, existing) for existing in local_seen):
                    continue
                distinct.append(bullet)
                local_seen.append(text)
                if len(distinct) >= bullet_cap:
                    break
            if not distinct:
                continue
            entry["bullets"] = distinct
            top_values = [_bullet_value(bullet) for bullet in distinct[:2]]
            entry_score = sum(top_values) / len(top_values)
            ranked_entries.append((entry_score, -original_index, entry))
        winners = sorted(ranked_entries, reverse=True, key=lambda item: (item[0], item[1]))[: cap["entries"]]
        winner_ids = {id(item[2]) for item in winners}
        curated[section] = [
            entry for entry in curated.get(section, []) if id(entry) in winner_ids
        ]
        if section == "experiences" and catalog:
            curated[section].sort(
                key=lambda entry: source_order.get(str(entry.get("source_id")), 10**6)
            )

    seen_texts: List[str] = []
    for section in ("experiences", "projects", "leadership"):
        retained_entries = []
        for entry in curated.get(section, []):
            retained = []
            for bullet in entry.get("bullets", []):
                text = str(bullet.get("text") or "")
                if any(_same_resume_bullet(text, existing) for existing in seen_texts):
                    continue
                retained.append(bullet)
                seen_texts.append(text)
            if retained:
                entry["bullets"] = retained
                retained_entries.append(entry)
        curated[section] = retained_entries

    # A final global cap protects against future schema expansion. Section and
    # per-entry floors in _removal_actions preserve the balanced shape of both
    # immutable human references.
    while sum(
        len(entry.get("bullets", []))
        for section in ("experiences", "projects", "leadership")
        for entry in curated.get(section, [])
    ) > MAX_TOTAL_BULLETS:
        actions = _removal_actions(curated)
        bullet_actions = [action for action in actions if action[3] is not None]
        if not bullet_actions:
            break
        _apply_removal(curated, min(bullet_actions, key=lambda item: item[0]))
    return curated


def portfolio_metrics(plan: Dict[str, Any]) -> Dict[str, Any]:
    counts = {
        section: {
            "entries": len(plan.get(section, [])),
            "bullets": sum(len(entry.get("bullets", [])) for entry in plan.get(section, [])),
        }
        for section in ("experiences", "projects", "leadership")
    }
    bullets = [
        bullet
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
        for bullet in entry.get("bullets", [])
    ]
    duplicates = []
    for index, bullet in enumerate(bullets):
        for other in bullets[index + 1 :]:
            if _same_resume_bullet(str(bullet.get("text") or ""), str(other.get("text") or "")):
                duplicates.append([bullet.get("source_id"), other.get("source_id")])
    violations = []
    for section, floor in PORTFOLIO_FLOORS.items():
        if counts[section]["entries"] < floor["entries"]:
            violations.append(
                "%s needs at least %s entries"
                % (section, floor["entries"])
            )
    for section, cap in PORTFOLIO_CAPS.items():
        if counts[section]["entries"] > cap["entries"]:
            violations.append("%s has too many entries" % section)
        for entry_index, entry in enumerate(plan.get(section, [])):
            bullet_cap = (
                EXPERIENCE_BULLET_CAPS[min(entry_index, len(EXPERIENCE_BULLET_CAPS) - 1)]
                if section == "experiences"
                else cap["bullets"]
            )
            if len(entry.get("bullets", [])) > bullet_cap:
                violations.append("%s exceeds the per-entry bullet cap" % entry.get("source_id"))
            floor = PORTFOLIO_FLOORS[section]["bullets"]
            if len(entry.get("bullets", [])) < floor:
                violations.append(
                    "%s needs at least %s distinct bullets"
                    % (entry.get("source_id"), floor)
                )
    if len(bullets) > MAX_TOTAL_BULLETS:
        violations.append("resume exceeds %s bullets" % MAX_TOTAL_BULLETS)
    if len(bullets) < MIN_TOTAL_BULLETS:
        violations.append("resume needs at least %s distinct bullets" % MIN_TOTAL_BULLETS)
    if duplicates:
        violations.append("resume contains duplicate or near-duplicate bullets")
    return {
        "pass": not violations,
        "counts": counts,
        "total_bullets": len(bullets),
        "min_total_bullets": MIN_TOTAL_BULLETS,
        "max_total_bullets": MAX_TOTAL_BULLETS,
        "duplicates": duplicates,
        "violations": violations,
    }


def merge_edited_bullets(candidate_plan: Dict[str, Any], edited_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Apply line edits to the rich pool without discarding excluded backups."""
    merged = copy.deepcopy(candidate_plan)
    edited = {
        str(bullet.get("source_id")): bullet
        for section in ("experiences", "projects", "leadership")
        for entry in edited_plan.get(section, [])
        for bullet in entry.get("bullets", [])
    }
    for section in ("experiences", "projects", "leadership"):
        for entry in merged.get(section, []):
            for index, bullet in enumerate(entry.get("bullets", [])):
                replacement = edited.get(str(bullet.get("source_id")))
                if replacement:
                    entry["bullets"][index] = copy.deepcopy(replacement)
    return merged


def restore_wrapped_source_text(
    plan: Dict[str, Any], layout: Dict[str, Any], catalog: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """Prefer an approved source line when an enhanced rewrite wraps.

    This deterministic fallback preserves facts and avoids another costly
    model call when the source bank already contains a tighter version.
    """
    wrapped = {
        str(item.get("source_id"))
        for item in (layout.get("horizontal") or {}).get("bullets", [])
        if item.get("wraps") is True
    }
    if not wrapped:
        return plan, []
    source_text = {
        str(bullet.get("id")): str(bullet.get("text") or "")
        for entry in (catalog.get("entries") or {}).values()
        for bullet in entry.get("bullets", [])
    }
    restored = copy.deepcopy(plan)
    restored_ids = []
    for section in ("experiences", "projects", "leadership"):
        for entry in restored.get(section, []):
            for bullet in entry.get("bullets", []):
                source_id = str(bullet.get("source_id") or "")
                approved = source_text.get(source_id, "")
                if source_id in wrapped and approved and bullet.get("text") != approved:
                    bullet["text"] = approved
                    restored_ids.append(source_id)
    return restored, restored_ids


def render_plan(plan: Dict[str, Any], catalog: Dict[str, Any], root: Optional[Path] = None) -> str:
    """Render content through Victor's exact resume.tex preamble and macros."""
    template_path = cv_root(root) / CANONICAL_TEMPLATE
    template = template_path.read_text()
    if BODY_MARKER not in template:
        raise ValueError("CV/resume.tex is missing the experience marker")
    prefix = template.split(BODY_MARKER, 1)[0].rstrip()
    entries = catalog["entries"]
    lines = [prefix, "", BODY_MARKER, "\\section{Experience}", "\\resumeSubHeadingListStart", ""]
    for selection in plan["experiences"]:
        entry = entries[selection["source_id"]]
        lines.extend(
            [
                "    \\resumeSubheading",
                "    {\\large %s}{%s}" % (entry["company"], entry["dates"]),
                "    {%s}{%s}" % (entry["role"], entry["location"]),
            ]
        )
        lines.extend(_render_bullets(selection["bullets"]))
        lines.append("")
    lines.extend(["\\resumeSubHeadingListEnd", "", "%-----------PROJECTS-----------", "\\section{Projects}", "\\resumeSubHeadingListStart", ""])
    for selection in plan["projects"]:
        entry = entries[selection["source_id"]]
        lines.extend(["    \\resumeProjectHeading", "        {\\large %s}{}" % entry["heading"]])
        lines.extend(_render_bullets(selection["bullets"]))
        lines.append("")
    lines.extend(["\\resumeSubHeadingListEnd", ""])
    if plan.get("leadership"):
        lines.extend(["%-----------LEADERSHIP-----------", "\\section{Leadership \\& Extracurriculars}", "\\resumeSubHeadingListStart", ""])
        for selection in plan["leadership"]:
            entry = entries[selection["source_id"]]
            lines.extend(
                [
                    "    \\resumeSubheading",
                    "    {\\large %s}{%s}" % (entry["company"], entry["dates"]),
                    "    {%s}{%s}" % (entry["role"], entry["location"]),
                ]
            )
            lines.extend(_render_bullets(selection["bullets"]))
            lines.append("")
        lines.extend(["\\resumeSubHeadingListEnd", ""])
    lines.extend(["\\end{document}", ""])
    return "\n".join(lines)


def _bullet_value(bullet: Dict[str, Any]) -> float:
    text = _latex_plain(str(bullet.get("text") or ""))
    priority = float(bullet.get("priority") or 50)
    # The model's priority is target-aware; deterministic signals are only
    # tie-breakers. Larger generic bonuses previously overruled an explicitly
    # higher-ranked biomedical Pandas/SQL bullet in favor of a less relevant
    # FastAPI bullet merely because it contained more stock technical terms.
    metric = 2 if re.search(r"\b\d[\d,.]*\+?%?|\$\d|\b(?:won|selected|grant|fellowship)\b", text, re.I) else 0
    technical = 1 if re.search(r"\b(architected|engineered|built|implemented|orchestrated|designed|pipeline|system|model|api|cloud|database)\b", text, re.I) else 0
    return priority + metric + technical


def _removal_actions(plan: Dict[str, Any]) -> List[Tuple[float, str, int, Optional[int]]]:
    actions: List[Tuple[float, str, int, Optional[int]]] = []
    for section in ("experiences", "projects", "leadership"):
        entries = plan.get(section, [])
        minimum_entries = PORTFOLIO_FLOORS[section]["entries"]
        minimum_bullets = PORTFOLIO_FLOORS[section]["bullets"]
        for entry_index, entry in enumerate(entries):
            bullets = entry.get("bullets", [])
            if len(bullets) > minimum_bullets:
                for bullet_index, bullet in enumerate(bullets):
                    actions.append((_bullet_value(bullet), section, entry_index, bullet_index))
            if len(entries) > minimum_entries:
                # Removing an entry saves its heading too, so compare value per
                # vertical unit rather than raw total value.
                density = sum(_bullet_value(bullet) for bullet in bullets) / max(1, len(bullets) + 1)
                actions.append((density, section, entry_index, None))
    return actions


def _apply_removal(
    plan: Dict[str, Any], action: Tuple[float, str, int, Optional[int]]
) -> Dict[str, Any]:
    _, section, entry_index, bullet_index = action
    if bullet_index is None:
        removed = plan[section].pop(entry_index)
        return {"kind": "entry", "section": section, "index": entry_index, "value": removed}
    entry = plan[section][entry_index]
    removed = entry["bullets"].pop(bullet_index)
    return {
        "kind": "bullet", "section": section, "entry_id": entry.get("source_id"),
        "index": bullet_index, "value": removed,
    }


def _undo_removal(plan: Dict[str, Any], undo: Dict[str, Any]) -> None:
    if undo["kind"] == "entry":
        plan[undo["section"]].insert(undo["index"], copy.deepcopy(undo["value"]))
        return
    entry = next(
        item for item in plan[undo["section"]]
        if item.get("source_id") == undo.get("entry_id")
    )
    entry["bullets"].insert(undo["index"], copy.deepcopy(undo["value"]))


def _compile_plan_attempt(
    plan: Dict[str, Any], catalog: Dict[str, Any], run_dir: Path, attempt: int
) -> Tuple[str, Dict[str, Any]]:
    attempt_dir = run_dir / "layout_search" / ("attempt-%02d" % attempt)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    tex = render_plan(plan, catalog, repo_root())
    (attempt_dir / "resume.tex").write_text(tex)
    compiled = compile_resume(attempt_dir)
    layout = pdf_layout(attempt_dir, compiled, plan=plan, run_capacity_test=False)
    return tex, layout


def pack_plan_to_page(
    candidate_plan: Dict[str, Any], catalog: Dict[str, Any], run_dir: Path
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Compile-search the highest-value one-page subset of a rich plan."""
    plan = curate_candidate_portfolio(candidate_plan, catalog)
    removed: List[Dict[str, Any]] = []
    attempts = []
    attempt_number = 0
    while attempt_number < 50:
        attempt_number += 1
        _, layout = _compile_plan_attempt(plan, catalog, run_dir, attempt_number)
        attempts.append({
            "attempt": attempt_number, "pages": layout.get("pages"),
            "overfull": layout.get("overfull"),
            "density_gap_pt": layout.get("density_gap_pt"),
            "density_pass": layout.get("density_pass"),
            "bullets": sum(len(entry.get("bullets", [])) for section in ("experiences", "projects", "leadership") for entry in plan.get(section, [])),
        })
        if not layout.get("compiled"):
            raise RuntimeError(
                "candidate LaTeX failed to compile; packing cannot treat a syntax error as page overflow"
            )
        if layout.get("compiled") and layout.get("pages") == 1 and not layout.get("overfull"):
            break
        actions = _removal_actions(plan)
        if not actions:
            break
        removed.append(_apply_removal(plan, min(actions, key=lambda item: item[0])))

    if not (
        layout.get("compiled")
        and layout.get("pages") == 1
        and not layout.get("overfull")
        and layout.get("density_pass")
    ):
        raise RuntimeError(
            "candidate portfolio could not meet the immutable one-page density reference"
        )

    kept_ids = {
        bullet.get("source_id")
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
        for bullet in entry.get("bullets", [])
    }
    all_ids = {
        bullet.get("source_id")
        for section in ("experiences", "projects", "leadership")
        for entry in candidate_plan.get(section, [])
        for bullet in entry.get("bullets", [])
    }
    return plan, {
        "strategy": "human-reference-density portfolio with compile-measured overflow removal",
        "attempts": attempts,
        "kept_bullets": len(kept_ids),
        "excluded_bullet_ids": sorted(value for value in all_ids - kept_ids if value),
        "style_change_percent": 0.0,
    }


def template_style_guard(tex: str, root: Optional[Path] = None) -> Dict[str, Any]:
    template = (cv_root(root) / CANONICAL_TEMPLATE).read_text()
    template_prefix = template.split(BODY_MARKER, 1)[0].rstrip()
    generated_prefix = tex.split(BODY_MARKER, 1)[0].rstrip() if BODY_MARKER in tex else ""
    body = tex.split(BODY_MARKER, 1)[1] if BODY_MARKER in tex else tex
    renderer_commands = {"\\section", "\\begin", "\\end", "\\large"}
    forbidden = sorted(
        set(match.group(0) for match in FORBIDDEN_CONTENT_COMMANDS.finditer(body)) - renderer_commands
    )
    identical = generated_prefix == template_prefix
    passed = identical and not forbidden
    return {
        "passed": passed,
        "canonical_template": "CV/" + CANONICAL_TEMPLATE,
        "identical_preamble_header_education_skills": identical,
        "font_size_reduction_percent": 0.0 if identical else None,
        "font_size_increase_percent": 0.0 if identical else None,
        "allowed_max_reduction_percent": MAX_STYLE_REDUCTION_PERCENT,
        "forbidden_layout_commands": forbidden,
    }


def pdf_content_bottom(pdf: Path) -> Optional[float]:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return None
    try:
        raw = subprocess.check_output([pdftotext, "-bbox", str(pdf), "-"], timeout=30)
        root = ET.fromstring(raw)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return None
    bottom = None
    for element in root.iter():
        if not element.tag.endswith("word"):
            continue
        text_value = "".join(element.itertext()).strip()
        try:
            y_max = float(element.attrib.get("yMax", "0"))
            x_min = float(element.attrib.get("xMin", "0"))
            x_max = float(element.attrib.get("xMax", "0"))
        except ValueError:
            continue
        if text_value.isdigit() and y_max > 740 and 280 < (x_min + x_max) / 2 < 330:
            continue
        bottom = y_max if bottom is None else max(bottom, y_max)
    return round(bottom, 2) if bottom is not None else None


def _latex_plain(value: str) -> str:
    value = re.sub(r"\\(?:textbf|emph|underline)\{([^{}]*)\}", r"\1", value or "")
    value = re.sub(r"\\[A-Za-z]+", " ", value)
    value = value.replace("\\&", "&").replace("\\%", "%").replace("\\$", "$")
    return re.sub(r"\s+", " ", value).strip()


def pdf_line_geometry(pdf: Path) -> Dict[str, Any]:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return {"page_width": None, "lines": []}
    try:
        raw = subprocess.check_output([pdftotext, "-bbox", str(pdf), "-"], timeout=30)
        root = ET.fromstring(raw)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return {"page_width": None, "lines": []}
    page = next((element for element in root.iter() if element.tag.endswith("page")), None)
    try:
        page_width = float(page.attrib.get("width")) if page is not None else None
    except (TypeError, ValueError):
        page_width = None
    words = []
    for element in root.iter():
        if not element.tag.endswith("word"):
            continue
        try:
            words.append({
                "text": "".join(element.itertext()).strip(),
                "x_min": float(element.attrib.get("xMin", "0")),
                "x_max": float(element.attrib.get("xMax", "0")),
                "y_min": float(element.attrib.get("yMin", "0")),
                "y_max": float(element.attrib.get("yMax", "0")),
            })
        except ValueError:
            continue
    groups: List[List[Dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: ((item["y_min"] + item["y_max"]) / 2, item["x_min"])):
        center = (word["y_min"] + word["y_max"]) / 2
        if groups:
            previous_center = sum((item["y_min"] + item["y_max"]) / 2 for item in groups[-1]) / len(groups[-1])
        else:
            previous_center = -999
        if groups and abs(center - previous_center) <= 2.0:
            groups[-1].append(word)
        else:
            groups.append([word])
    lines = []
    for group in groups:
        ordered = sorted(group, key=lambda item: item["x_min"])
        lines.append({
            "text": " ".join(item["text"] for item in ordered if item["text"] != "•"),
            "x_min": min(item["x_min"] for item in ordered),
            "x_max": max(item["x_max"] for item in ordered),
            "y_min": min(item["y_min"] for item in ordered),
            "y_max": max(item["y_max"] for item in ordered),
        })
    return {"page_width": page_width, "lines": lines}


def bullet_layout_metrics(plan: Dict[str, Any], pdf: Path) -> Dict[str, Any]:
    geometry = pdf_line_geometry(pdf)
    page_width = geometry.get("page_width") or 612.0
    right_edge = page_width - 36.0
    results = []
    for section in ("experiences", "projects", "leadership"):
        for entry in plan.get(section, []):
            for bullet in entry.get("bullets", []):
                plain = _latex_plain(str(bullet.get("text") or ""))
                tokens = re.findall(r"[a-z0-9]+", plain.lower())
                anchor = " ".join(tokens[:2])
                ending = tokens[-1] if tokens else ""
                matched = None
                best_overlap = -1.0
                for line in geometry.get("lines", []):
                    line_tokens = re.findall(r"[a-z0-9]+", str(line.get("text") or "").lower())
                    line_text = " ".join(line_tokens)
                    if not anchor or anchor not in line_text:
                        continue
                    overlap = len(set(tokens) & set(line_tokens)) / max(1, len(set(tokens)))
                    if overlap > best_overlap:
                        matched = line
                        best_overlap = overlap
                if matched:
                    line_tokens = re.findall(r"[a-z0-9]+", str(matched.get("text") or "").lower())
                    wraps = bool(ending and ending not in line_tokens)
                    slack = round(right_edge - float(matched.get("x_max") or 0), 2)
                    results.append({
                        "source_id": bullet.get("source_id"),
                        "text": plain,
                        "wraps": wraps,
                        "right_slack_pt": slack,
                        "horizontal_pass": not wraps,
                    })
                else:
                    results.append({
                        "source_id": bullet.get("source_id"), "text": plain,
                        "wraps": None, "right_slack_pt": None, "horizontal_pass": False,
                        "warning": "bullet line not found in PDF geometry",
                    })
    measurable = [item for item in results if item.get("right_slack_pt") is not None]
    return {
        "max_right_slack_pt": MAX_RIGHT_SLACK_PT,
        "measured": len(measurable),
        "bullets": results,
        "wrap_count": sum(item.get("wraps") is True for item in results),
        "underfilled_line_count": sum(item.get("right_slack_pt", -1) > MAX_RIGHT_SLACK_PT for item in measurable),
        "pass": bool(results) and all(item.get("horizontal_pass") for item in results),
    }


def vertical_capacity_test(run_dir: Path, tex: str) -> Dict[str, Any]:
    qa_dir = run_dir / "qa_vertical_capacity"
    qa_dir.mkdir(exist_ok=True)
    sentinel = (
        "\\resumeItem{\\textbf{Additional verified technical evidence} "
        "with concrete implementation scope and measurable outcome}\n"
    )
    marker = "\\resumeItemListEnd"
    body_start = tex.find(BODY_MARKER)
    insertion = tex.find(marker, body_start if body_start >= 0 else 0)
    if insertion < 0:
        return {"pass": False, "warning": "could not insert QA bullet"}
    qa_tex = tex[:insertion] + sentinel + tex[insertion:]
    (qa_dir / "resume.tex").write_text(qa_tex)
    compiled = compile_resume(qa_dir)
    layout = pdf_layout(qa_dir, compiled, plan=None, run_capacity_test=False)
    return {
        "pass": layout.get("pages") is not None and layout.get("pages") > 1,
        "qa_pages": layout.get("pages"),
        "sentinel": "one standard one-line bullet",
        "warning": (
            "one more bullet still fits" if layout.get("pages") == 1
            else "; ".join(layout.get("warnings") or []) if not layout.get("compiled") else ""
        ),
    }


def render_preview(run_dir: Path) -> Optional[str]:
    pdftoppm = shutil.which("pdftoppm")
    pdf = run_dir / "resume.pdf"
    if not pdftoppm or not pdf.exists():
        return None
    prefix = run_dir / "resume-preview"
    try:
        subprocess.run(
            [pdftoppm, "-png", "-r", "144", "-singlefile", str(pdf), str(prefix)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return str(prefix.with_suffix(".png")) if prefix.with_suffix(".png").exists() else None


def compile_resume(run_dir: Path) -> Dict[str, Any]:
    tex = run_dir / "resume.tex"
    if not tex.exists():
        return {"compiled": False, "error": "resume.tex was not produced"}
    tectonic = shutil.which("tectonic")
    if not tectonic:
        return {"compiled": False, "error": "tectonic is not installed"}
    try:
        proc = subprocess.run(
            [tectonic, "-X", "compile", "--outdir", str(run_dir), "resume.tex"],
            cwd=str(run_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
        )
        (run_dir / "tectonic.stdout.txt").write_text(proc.stdout or "")
        pdf = run_dir / "resume.pdf"
        if proc.returncode != 0 or not pdf.exists():
            return {"compiled": False, "error": "LaTeX compilation failed", "exit_code": proc.returncode}
        return {"compiled": True, "pdf": str(pdf), "exit_code": proc.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"compiled": False, "error": str(exc)}


def pdf_layout(
    run_dir: Path,
    compile_info: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    run_capacity_test: bool = True,
) -> Dict[str, Any]:
    result = {
        "compiled": bool(compile_info.get("compiled")),
        "pages": None,
        "text_extractable": False,
        "overfull": False,
        "content_bottom_pt": None,
        "reference_content_bottom_pt": None,
        "density_gap_pt": None,
        "density_pass": False,
        "horizontal": {"pass": False, "bullets": []},
        "vertical_capacity": {"pass": False},
        "warnings": [],
    }
    if not result["compiled"]:
        result["warnings"].append(compile_info.get("error", "compile failed"))
        return result
    pdf = run_dir / "resume.pdf"
    pdfinfo = shutil.which("pdfinfo")
    pdftotext = shutil.which("pdftotext")
    if pdfinfo:
        try:
            info = subprocess.check_output([pdfinfo, str(pdf)], text=True, timeout=20)
            match = re.search(r"^Pages:\s*(\d+)", info, flags=re.M)
            result["pages"] = int(match.group(1)) if match else None
        except (OSError, subprocess.SubprocessError):
            result["warnings"].append("pdfinfo failed")
    if pdftotext:
        try:
            text = subprocess.check_output([pdftotext, str(pdf), "-"], text=True, timeout=20)
            result["text_extractable"] = len(text.strip()) > 100
            (run_dir / "resume.txt").write_text(text)
        except (OSError, subprocess.SubprocessError):
            result["warnings"].append("pdftotext failed")
    result["content_bottom_pt"] = pdf_content_bottom(pdf)
    reference_pdf = cv_root(repo_root()) / "resume.pdf"
    result["reference_content_bottom_pt"] = pdf_content_bottom(reference_pdf) if reference_pdf.exists() else None
    if result["content_bottom_pt"] is not None and result["reference_content_bottom_pt"] is not None:
        result["density_gap_pt"] = round(result["reference_content_bottom_pt"] - result["content_bottom_pt"], 2)
        result["density_pass"] = result["density_gap_pt"] <= MAX_DENSITY_GAP_PT
    else:
        result["warnings"].append("page-density measurement unavailable")
    logs = ""
    for log in run_dir.glob("*.log"):
        try:
            logs += log.read_text(errors="replace")
        except OSError:
            pass
    result["overfull"] = bool(re.search(r"Overfull \\hbox|Output loop---|Fatal error", logs))
    if plan is not None:
        result["horizontal"] = bullet_layout_metrics(plan, pdf)
        result["portfolio"] = portfolio_metrics(plan)
        if not result["horizontal"].get("pass"):
            result["warnings"].append(
                "one-line bullet check failed: %s wrap(s)"
                % result["horizontal"].get("wrap_count", 0)
            )
        if run_capacity_test:
            try:
                result["vertical_capacity"] = vertical_capacity_test(run_dir, (run_dir / "resume.tex").read_text())
            except OSError as exc:
                result["vertical_capacity"] = {"pass": False, "warning": str(exc)}
    if result["overfull"]:
        result["warnings"].append("LaTeX log contains an overfull box or fatal layout warning")
    if result["pages"] != 1:
        result["warnings"].append("strict one-page target is not met")
    if not result["text_extractable"]:
        result["warnings"].append("PDF text is not extractable")
    if not result["density_pass"]:
        result["warnings"].append("page has extreme unused bottom space relative to CV/resume.pdf")
    return result


def deterministic_review(job: Dict[str, Any], tex: str, layout: Dict[str, Any]) -> Dict[str, Any]:
    warnings = list(layout.get("warnings", []))
    style = template_style_guard(tex, repo_root())
    if not style.get("passed"):
        warnings.append("generated resume changed or bypassed the canonical CV/resume.tex formatting")
    company = str(job.get("company", "")).lower()
    if "Victor Jimenez" not in tex or "vmj@njit.edu" not in tex:
        warnings.append("canonical owner name/contact header is missing")
    if "johnson" in company and "ticc" in tex.lower():
        warnings.append("TICC appears in a Johnson & Johnson draft; review relevance")
    layout_gate = (
        layout.get("compiled")
        and layout.get("pages") == 1
        and not layout.get("overfull")
        and layout.get("density_pass")
        and (layout.get("horizontal") or {}).get("pass")
        and style.get("passed")
    )
    portfolio = layout.get("portfolio") or portfolio_metrics({})
    portfolio_gate = bool(portfolio.get("pass"))
    eligibility_blocks = posting_eligibility_blocks(str(job.get("posting_text") or ""))
    if eligibility_blocks:
        eligibility = {"status": "fail", "reason": "; ".join(eligibility_blocks)}
    elif job.get("alert_ok"):
        eligibility = {"status": "pass", "reason": "Radar verifies the role as new-grad/eligible"}
    elif job.get("early_career_possible"):
        eligibility = {"status": "partial", "reason": "Early-career possible; posting eligibility needs confirmation"}
    else:
        eligibility = {"status": "fail", "reason": "Radar does not currently verify new-grad eligibility"}
    return {
        "rubric_version": RUBRIC_VERSION,
        "hard_fail": not layout_gate,
        "warnings": warnings,
        "layout": layout,
        "style": style,
        "gates": {
            "layout": {"status": "pass" if layout_gate else "fail", "reason": "; ".join(warnings) or "all rendered layout checks passed"},
            "portfolio": {
                "status": "pass" if portfolio_gate else "fail",
                "reason": "; ".join(portfolio.get("violations") or []) or "portfolio is compact and nonredundant",
            },
            "eligibility": eligibility,
        },
    }


def score_review(agent_review: Dict[str, Any], deterministic: Dict[str, Any]) -> Dict[str, Any]:
    data = agent_review.get("data") or {}
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else data
    scores = {}
    for name, weight in RUBRIC_WEIGHTS.items():
        if name == "layout":
            layout = deterministic.get("layout") or {}
            style = deterministic.get("style") or {}
            status = (
                "pass"
                if layout.get("compiled")
                and layout.get("pages") == 1
                and not layout.get("overfull")
                and layout.get("density_pass")
                and (layout.get("horizontal") or {}).get("pass")
                and style.get("passed")
                else "fail"
            )
        else:
            raw = criteria.get(name, {}) if isinstance(criteria, dict) else {}
            if isinstance(raw, str):
                status = raw.lower()
            elif isinstance(raw, dict):
                status = str(raw.get("status", "fail")).lower()
            else:
                status = "fail"
            if status not in STATUS_MULTIPLIER:
                status = "fail"
        scores[name] = {
            "status": status,
            "points": round(weight * STATUS_MULTIPLIER[status], 1),
            "weight": weight,
        }
    unsupported = data.get("unsupported_claims", [])
    if not isinstance(unsupported, list):
        unsupported = [str(unsupported)]
    factual_raw = criteria.get("factual", {}) if isinstance(criteria, dict) else {}
    factual_status = (
        str(factual_raw.get("status", "fail")).lower()
        if isinstance(factual_raw, dict) else str(factual_raw).lower()
    )
    if factual_status not in STATUS_MULTIPLIER:
        factual_status = "fail"
    deterministic_gates = deterministic.get("gates") or {}
    gates = {
        "factual": {
            "status": "fail" if unsupported else factual_status,
            "reason": "unsupported claims reported" if unsupported else (
                factual_raw.get("reason", "") if isinstance(factual_raw, dict) else "reviewer factual verdict"
            ),
        },
        "eligibility": deterministic_gates.get("eligibility", {"status": "fail", "reason": "eligibility unavailable"}),
        "layout": deterministic_gates.get("layout", {"status": "fail", "reason": "layout unavailable"}),
        "portfolio": deterministic_gates.get("portfolio", {"status": "pass", "reason": "portfolio gate unavailable"}),
    }
    hard_fail = any(gate.get("status") == "fail" for gate in gates.values())
    total = round(sum(item["points"] for item in scores.values()), 1)
    return {
        "rubric_version": RUBRIC_VERSION,
        "craft_score": total,
        "score": total,
        "ready": total >= 80 and not hard_fail,
        "hard_fail": hard_fail,
        "criteria": scores,
        "gates": gates,
        "unsupported_claims": unsupported,
        "missing_evidence": data.get("missing_evidence", []),
        "revision_priorities": data.get("revision_priorities", []),
        "reviewer": agent_review.get("provider"),
        "deterministic": deterministic,
    }


def make_report(run_dir: Path, payload: Dict[str, Any]) -> None:
    write_json(run_dir / "report.json", payload)
    status_path = run_dir / "status.json"
    current = read_json(status_path, {}) or {}
    current["report"] = payload
    write_json(status_path, current)


def _select_valid_plan(
    candidates: List[Dict[str, Any]], catalog: Dict[str, Any], enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], List[str], Optional[Dict[str, Any]]]:
    all_errors: List[str] = []
    for candidate in candidates:
        if not candidate.get("ok"):
            continue
        normalized, errors = validate_plan(candidate.get("data") or {}, catalog, enhance, graph=graph)
        if not errors:
            return normalized, [], candidate
        all_errors.extend(["%s: %s" % (candidate.get("provider", "provider"), error) for error in errors])
    return None, all_errors, None


def run_tailoring(run_dir: Path, job: Dict[str, Any], update, enhance: bool) -> None:
    update("context", "Fetching the posting and preparing the private CV context")
    context = job_context(job)
    write_json(run_dir / "job_context.json", context)
    catalog = source_catalog(repo_root())
    write_json(run_dir / "evidence_catalog.json", catalog_for_prompt(catalog))
    graph = evidence_graph(repo_root())
    graph_context = evidence_context(graph, context, str(context.get("posting_text") or ""))
    write_json(run_dir / "evidence_graph_context.json", graph_context)
    match = resume_match_for_job(job, repo_root(), posting_text=str(context.get("posting_text") or ""))
    context["resume_match"] = match
    write_json(run_dir / "job_context.json", context)
    mode_label = "enhanced" if enhance else "source-only"
    prompt = base_prompt(context, "an independent resume evidence strategist", catalog, enhance, graph=graph)
    schema = plan_schema(enhance)
    available = [name for name, path in provider_commands().items() if path]
    if not available:
        raise RuntimeError("Neither codex nor claude is installed")
    update("drafting", "Building independent %s evidence plans: %s" % (mode_label, ", ".join(available)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(available)) as pool:
        futures = {
            pool.submit(run_provider, provider, prompt, run_dir, "draft", RUN_TIMEOUT_SECONDS, schema): provider
            for provider in available
        }
        drafts = [future.result() for future in concurrent.futures.as_completed(futures)]
    for draft in drafts:
        write_json(run_dir / (draft.get("provider", "unknown") + "_draft.json"), draft)
    successful = [draft for draft in drafts if draft.get("ok")]
    if not successful:
        raise RuntimeError("Frontier agents returned no usable evidence plan; inspect *_draft.json")
    update("synthesis", "Selecting the strongest source-grounded evidence plan")
    synthesizer = successful[0].get("provider") or ("codex" if provider_commands().get("codex") else "claude")
    if len(successful) == 1:
        synthesis = {
            "provider": synthesizer,
            "ok": True,
            "skipped": True,
            "reason": "Only one usable frontier provider was available; reused its structured plan instead of asking it to synthesize itself.",
            "data": successful[0].get("data") or {},
        }
    else:
        synthesis = run_provider(
            synthesizer,
            synthesis_prompt(context, successful, catalog, enhance, graph=graph),
            run_dir,
            "synthesis",
            timeout=4 * 60,
            schema=schema,
        )
    write_json(run_dir / "synthesis.json", synthesis)
    candidates = [synthesis] + successful
    candidate_plan, plan_errors, chosen_record = _select_valid_plan(candidates, catalog, enhance, graph=graph)
    if candidate_plan is None:
        write_json(run_dir / "plan_errors.json", plan_errors)
        raise RuntimeError("No provider returned a valid source-addressed plan; inspect plan_errors.json")
    candidate_plan = expand_candidate_portfolio(candidate_plan, catalog, enhance)
    write_json(run_dir / "candidate_plan.json", candidate_plan)
    update("packing", "Packing a full, meaningful portfolio against the human reference page")
    plan, packing = pack_plan_to_page(candidate_plan, catalog, run_dir)
    write_json(run_dir / "content_plan.json", plan)
    write_json(run_dir / "layout_packing.json", packing)
    chosen = render_plan(plan, catalog, repo_root())
    (run_dir / "resume.tex").write_text(chosen)
    update("rendering", "Compiling through the immutable CV/resume.tex template")
    compiled = compile_resume(run_dir)
    layout = pdf_layout(run_dir, compiled, plan=plan)
    line_edits: List[Dict[str, Any]] = []
    editable_pool = candidate_plan
    for line_round in range(1, MAX_LINE_EDIT_PASSES + 1):
        space_pass = (layout.get("horizontal") or {}).get("pass")
        if not enhance or space_pass:
            break
        update(
            "line_editing",
            "Optimizing rendered bullets against the actual margins (pass %s/%s)"
            % (line_round, MAX_LINE_EDIT_PASSES),
        )
        label = "line_edit" if line_round == 1 else "line_edit_%s" % line_round
        line_edit = run_provider(
            synthesizer,
            line_editor_prompt(context, plan, layout, graph),
            run_dir,
            label,
            timeout=6 * 60,
            schema=schema,
        )
        line_edits.append(line_edit)
        write_json(run_dir / (label + ".json"), line_edit)
        if not line_edit.get("ok"):
            break
        edited, edit_errors = validate_plan(line_edit.get("data") or {}, catalog, True, graph=graph)
        if edit_errors or _plan_source_signature(edited) != _plan_source_signature(plan):
            write_json(
                run_dir / (label + "_errors.json"),
                edit_errors or ["line editor changed selected source IDs"],
            )
            break
        # Width expansion can change vertical fit. Preserve the rich excluded
        # pool, merge only the accepted text edits, and rerun the same packer.
        editable_pool = merge_edited_bullets(editable_pool, edited)
        plan, line_packing = pack_plan_to_page(
            editable_pool, catalog, run_dir / (label + "_pack")
        )
        packing[label + "_pack"] = line_packing
        write_json(run_dir / "content_plan.json", plan)
        write_json(run_dir / "layout_packing.json", packing)
        chosen = render_plan(plan, catalog, repo_root())
        (run_dir / "resume.tex").write_text(chosen)
        compiled = compile_resume(run_dir)
        layout = pdf_layout(run_dir, compiled, plan=plan)
    preview = render_preview(run_dir)
    deterministic = deterministic_review(context, chosen, layout)
    update("reviewing", "Running the adversarial final editor")
    reviewer = "claude" if synthesizer == "codex" and provider_commands().get("claude") else synthesizer
    review = run_provider(
        reviewer,
        reviewer_prompt(
            context, chosen, plan=plan, graph_context=graph_context, catalog=catalog
        ),
        run_dir,
        "review",
        timeout=8 * 60,
        schema=reviewed_plan_schema(enhance),
    )
    if not review.get("ok") and reviewer != synthesizer:
        reviewer = synthesizer
        review = run_provider(
            reviewer,
            reviewer_prompt(
                context, chosen, plan=plan, graph_context=graph_context, catalog=catalog
            ),
            run_dir,
            "review_fallback",
            timeout=8 * 60,
            schema=reviewed_plan_schema(enhance),
        )
    write_json(run_dir / "review_agent.json", review)
    review_plan_applied = False
    review_plan_errors: List[str] = []
    if review.get("ok"):
        reviewed_raw = (review.get("data") or {}).get("final_plan")
        if isinstance(reviewed_raw, dict):
            reviewed_plan, review_plan_errors = validate_plan(
                reviewed_raw, catalog, enhance, graph=graph
            )
            if not review_plan_errors:
                update("finalizing", "Applying the adversarial content corrections and repacking")
                reviewed_pool = expand_candidate_portfolio(reviewed_plan, catalog, enhance)
                plan, review_packing = pack_plan_to_page(
                    reviewed_pool, catalog, run_dir / "review_pack"
                )
                packing["review_pack"] = review_packing
                write_json(run_dir / "content_plan.json", plan)
                write_json(run_dir / "layout_packing.json", packing)
                chosen = render_plan(plan, catalog, repo_root())
                (run_dir / "resume.tex").write_text(chosen)
                compiled = compile_resume(run_dir)
                layout = pdf_layout(run_dir, compiled, plan=plan)
                plan, restored_source_ids = restore_wrapped_source_text(
                    plan, layout, catalog
                )
                if restored_source_ids:
                    packing["review_source_reversions"] = restored_source_ids
                    write_json(run_dir / "content_plan.json", plan)
                    write_json(run_dir / "layout_packing.json", packing)
                    chosen = render_plan(plan, catalog, repo_root())
                    (run_dir / "resume.tex").write_text(chosen)
                    compiled = compile_resume(run_dir)
                    layout = pdf_layout(run_dir, compiled, plan=plan)
                preview = render_preview(run_dir)
                deterministic = deterministic_review(context, chosen, layout)
                review_plan_applied = True
        else:
            review_plan_errors = ["adversarial final editor returned no final_plan"]
    if review_plan_errors:
        write_json(run_dir / "review_plan_errors.json", review_plan_errors)
        review_data = review.setdefault("data", {})
        unresolved = review_data.setdefault("unsupported_claims", [])
        unresolved.append(
            "Adversarial final plan was not applied: " + "; ".join(review_plan_errors)
        )
    scored = score_review(review, deterministic)
    synthesis_data = plan
    provider_records = [
        {"provider": d.get("provider"), "ok": d.get("ok"), "called": True, "elapsed_seconds": d.get("elapsed_seconds"), "usage_tokens": d.get("usage_tokens")}
        for d in drafts
    ] + [
        {"provider": "synthesis/" + synthesizer, "ok": synthesis.get("ok"), "called": not synthesis.get("skipped"), "skipped": bool(synthesis.get("skipped")), "usage_tokens": synthesis.get("usage_tokens")},
    ] + [
        {"provider": "line-edit-%s/%s" % (index, synthesizer), "ok": item.get("ok"), "called": True, "usage_tokens": item.get("usage_tokens")}
        for index, item in enumerate(line_edits, 1)
    ] + [
        {"provider": "review/" + reviewer, "ok": review.get("ok"), "called": True, "usage_tokens": review.get("usage_tokens")},
    ]
    codex_records = [item for item in provider_records if item.get("called") and item.get("provider", "").split("/")[-1] == "codex"]
    known_codex = [item["usage_tokens"] for item in codex_records if item.get("usage_tokens") is not None]
    report = {
        "mode": "enhanced" if enhance else "source-only",
        "job": job_summary(job),
        "resume_match": match,
        "positioning_thesis": synthesis_data.get("positioning_thesis", ""),
        "selected_evidence": synthesis_data.get("selected_evidence", []),
        "excluded_evidence": synthesis_data.get("excluded_evidence", []),
        "revision_notes": synthesis_data.get("revision_notes", []),
        "validation_warnings": synthesis_data.get("validation_warnings", []),
        "content_plan": {
            "experiences": synthesis_data.get("experiences", []),
            "projects": synthesis_data.get("projects", []),
            "leadership": synthesis_data.get("leadership", []),
        },
        "layout_packing": packing,
        "format_contract": {
            "template": "CV/resume.tex",
            "model_can_write_latex_document": False,
            "font_size_reduction_percent": 0.0,
            "font_size_increase_percent": 0.0,
            "allowed_max_reduction_percent": MAX_STYLE_REDUCTION_PERCENT,
        },
        "providers": provider_records,
        "usage": {
            "codex_tokens": sum(known_codex),
            "codex_calls": len(codex_records),
            "complete": len(known_codex) == len(codex_records),
            "known_calls": len(known_codex),
        },
        "review": scored,
        "review_plan_applied": review_plan_applied,
        "review_plan_errors": review_plan_errors,
        "artifacts": [
            "resume.tex",
            "resume.pdf",
            "resume.txt",
            "resume-preview.png" if preview else None,
            "report.json",
            "job_context.json",
            "evidence_catalog.json",
            "evidence_graph_context.json",
            "candidate_plan.json",
            "content_plan.json",
            "layout_packing.json",
            *[("line_edit.json" if index == 1 else "line_edit_%s.json" % index) for index in range(1, len(line_edits) + 1)],
            "review_agent.json",
        ],
    }
    report["artifacts"] = [artifact for artifact in report["artifacts"] if artifact]
    make_report(run_dir, report)
    update(
        "complete",
        "%s tailored draft and adversarial final edit are ready" % mode_label.capitalize(),
        report=report,
    )


def run_strict(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=False)


def run_dream(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=True)


class RunManager:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or repo_root()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.lock = threading.Lock()

    def start(self, job: Dict[str, Any], mode: str) -> Dict[str, Any]:
        if mode not in {"strict", "dream"}:
            raise ValueError("mode must be strict or dream")
        run_id = uuid.uuid4().hex[:12]
        run_dir = studio_root(self.root) / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        status = {
            "run_id": run_id,
            "mode": mode,
            "status": "queued",
            "step": "queued",
            "message": "Queued",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "job": job_summary(job),
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "status.json", status)
        self.executor.submit(self._worker, run_id, run_dir, job, mode)
        return status

    def update(self, run_id: str, run_dir: Path, status: str, step: str, message: str, **extra) -> None:
        path = run_dir / "status.json"
        value = read_json(path, {}) or {}
        value.update({"run_id": run_id, "status": status, "step": step, "message": message, "updated_at": now_iso()})
        value.update(extra)
        write_json(path, value)

    def _worker(self, run_id: str, run_dir: Path, job: Dict[str, Any], mode: str) -> None:
        def update(step: str, message: str, **extra: Any) -> None:
            status = "complete" if step == "complete" else "running"
            self.update(run_id, run_dir, status, step, message, **extra)

        try:
            if mode == "strict":
                run_strict(run_dir, job, update)
            else:
                run_dream(run_dir, job, update)
        except Exception as exc:  # keep failure inspectable in the local UI
            trace = traceback.format_exc()
            (run_dir / "error.log").write_text(trace)
            self.update(run_id, run_dir, "failed", "error", str(exc), error_log="error.log")

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = studio_root(self.root) / "runs" / run_id / "status.json"
        return read_json(path)


UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resume Studio · Job Radar</title>
<style>
:root{color-scheme:dark;--bg:#0e1117;--panel:#161b22;--line:#30363d;--muted:#8b949e;--text:#f0f6fc;--accent:#58a6ff;--good:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1250px;margin:0 auto;padding:28px 20px 70px}h1{margin:0 0 6px;font-size:28px}h2{font-size:18px;margin:0 0 12px}.sub{color:var(--muted);margin:0 0 20px}.grid{display:grid;grid-template-columns:410px 1fr;gap:18px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}.jobs{max-height:650px;overflow:auto}.job{width:100%;text-align:left;background:transparent;color:var(--text);border:1px solid transparent;border-radius:8px;padding:10px;margin:4px 0;cursor:pointer}.job:hover,.job.selected{background:#1f2937;border-color:#3b82f6}.job strong{display:block}.job small{color:var(--muted)}input,select,button{font:inherit}input,select{background:#0d1117;border:1px solid var(--line);border-radius:6px;color:var(--text);padding:9px}input{width:100%;margin-bottom:8px}.toolbar{display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:10px}.toolbar select{min-width:145px}button{background:#238636;border:1px solid #2ea043;color:#fff;border-radius:6px;padding:9px 12px;cursor:pointer;margin:4px 6px 4px 0}button.secondary{background:#21262d;border-color:var(--line)}button:disabled{opacity:.5;cursor:wait}.selected-card{border:1px solid var(--accent);border-radius:8px;padding:13px;margin:10px 0 16px}.meta{color:var(--muted);font-size:13px}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;margin:3px 4px 0 0;font-size:12px}.match-card{margin:10px 0;padding:10px;border-radius:7px;background:#0d1117;border:1px solid var(--line)}.match-card strong{font-size:18px}.status{border-left:3px solid var(--accent);padding:10px 12px;background:#111827;white-space:pre-wrap}.status.complete{border-color:var(--good)}.status.failed{border-color:var(--bad)}.status.running{border-color:var(--warn)}a{color:var(--accent)}pre{white-space:pre-wrap;max-height:360px;overflow:auto;background:#0d1117;border:1px solid var(--line);padding:12px;border-radius:6px;font-size:12px}.score{font-size:27px;margin:4px 0}.preview{display:block;width:100%;max-width:760px;margin:14px auto;border:1px solid var(--line);background:#fff}.hidden{display:none}@media(max-width:850px){.grid{grid-template-columns:1fr}.jobs{max-height:360px}}
</style></head><body><main>
<h1>Resume Studio</h1><p class="sub">Victor-first local workspace · CV and frontier sessions never leave this Mac through Job Radar.</p>
<div class="grid"><section class="panel"><h2>Choose a role</h2><input id="search" placeholder="Search company, title, sector…" autocomplete="off"><div class="toolbar"><select id="sort" aria-label="Sort roles"><option value="best">Best Radar score</option><option value="newest">Newest</option><option value="resume_match">Resume Match</option></select><button id="refreshEvidence" class="secondary" title="Refresh GitHub and Devpost evidence">Refresh evidence</button></div><div id="jobs" class="jobs">Loading roles…</div></section>
<section class="panel"><h2>Application workspace</h2><div id="empty" class="sub">Select a company and role. Both modes preserve CV/resume.tex exactly.</div><div id="workspace" class="hidden"><div id="selected" class="selected-card"></div><div id="match" class="match-card"></div><button id="analyzeMatch" class="secondary">Analyze full posting match</button><button id="strict">Tailor with my existing bullets</button><button id="dream">Tailor with reviewable enhancements</button><div id="status" class="status hidden"></div><div id="report" class="hidden"></div></div></section></div>
<script>
let selected=null, pollTimer=null;
const $=id=>document.getElementById(id);
async function loadJobs(){const q=encodeURIComponent($('search').value),sort=encodeURIComponent($('sort').value);$('jobs').innerHTML='<p class="sub">Scoring roles…</p>';const r=await fetch('/api/jobs?query='+q+'&sort='+sort);const data=await r.json();$('jobs').innerHTML=data.jobs.map(j=>{const m=j.resume_match?` · resume ${j.resume_match.score} (${j.resume_match.confidence})`:'';return `<button class="job" data-id="${j.id}"><strong>${esc(j.company)} · ${esc(j.title)}</strong><small>${esc((j.locations||[]).join(', '))} · Radar ${j.score}${m} ${j.alert_ok?'· alert':''}</small></button>`}).join('')||'<p class="sub">No matching roles.</p>';document.querySelectorAll('.job').forEach(b=>b.onclick=()=>choose(b.dataset.id));}
function renderMatch(match){if(!match){$('match').innerHTML='<span class="meta">Resume Match not analyzed.</span>';return;}const gaps=(match.missing_requirements||[]).join(', ')||'none detected';$('match').innerHTML=`<strong>${match.score}/100 Resume Match</strong> <span class="badge">${esc(match.confidence||'low')} confidence</span><div class="meta">${esc((match.reasons||[]).join(' · '))}</div><div class="meta">Gaps: ${esc(gaps)}</div>`;}
async function choose(id){const r=await fetch('/api/job?id='+encodeURIComponent(id));selected=await r.json();$('empty').classList.add('hidden');$('workspace').classList.remove('hidden');$('selected').innerHTML=`<strong>${esc(selected.company)} · ${esc(selected.title)}</strong><div class="meta">${esc((selected.locations||[]).join(', '))} · Radar ${selected.score} · <a href="${esc(selected.url)}" target="_blank" rel="noreferrer">open posting</a></div><div>${(selected.alert_ok?'<span class="badge">alert eligible</span>':'<span class="badge">dashboard role</span>')} ${(selected.early_career_possible?'<span class="badge">early-career possible</span>':'')}</div>`;renderMatch(selected.resume_match);document.querySelectorAll('.job').forEach(b=>b.classList.toggle('selected',b.dataset.id===id));$('status').classList.add('hidden');$('report').classList.add('hidden');}
async function analyzeMatch(){if(!selected)return;$('analyzeMatch').disabled=true;$('match').innerHTML='<span class="meta">Fetching the posting and matching the full evidence graph…</span>';const r=await fetch('/api/match',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:selected.id})});const data=await r.json();$('analyzeMatch').disabled=false;if(!r.ok){$('match').textContent=data.error||'Match analysis failed';return;}selected.resume_match=data.resume_match;renderMatch(data.resume_match);}
async function refreshEvidence(){const button=$('refreshEvidence');button.disabled=true;button.textContent='Refreshing…';const r=await fetch('/api/evidence/refresh',{method:'POST',headers:{'content-type':'application/json'},body:'{}'});const data=await r.json();button.disabled=false;button.textContent='Refresh evidence';if(!r.ok){alert(data.error||'Evidence refresh failed');return;}await loadJobs();if(selected)await choose(selected.id);}
async function start(mode){if(!selected)return;['strict','dream'].forEach(x=>$(x).disabled=true);$('status').className='status running';$('status').textContent='Starting '+mode+'…';$('status').classList.remove('hidden');$('report').classList.add('hidden');const r=await fetch('/api/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:selected.id,mode})});const data=await r.json();if(!r.ok){$('status').className='status failed';$('status').textContent=data.error||'Could not start run';['strict','dream'].forEach(x=>$(x).disabled=false);return;}poll(data.run_id);}
async function poll(id){const r=await fetch('/api/run?id='+encodeURIComponent(id));const data=await r.json();$('status').textContent=data.message||data.status;$('status').className='status '+data.status;if(data.status==='complete'||data.status==='failed'){['strict','dream'].forEach(x=>$(x).disabled=false);renderReport(data);return;}pollTimer=setTimeout(()=>poll(id),1500);}
function renderReport(status){const report=status.report;if(!report){return;}$('report').classList.remove('hidden');const review=report.review||{},gates=review.gates||{};let html='<h3>Result</h3>';if(review.craft_score!==undefined)html+=`<div class="score">${review.craft_score}/100 craft</div><div>${review.ready?'Ready for human review':'Needs revision or fact verification'}</div><p>${Object.entries(gates).map(([name,gate])=>`<span class="badge">${esc(name)}: ${esc(gate.status)}</span>`).join(' ')}</p>`;if(report.resume_match)html+=`<p><strong>Resume Match:</strong> ${report.resume_match.score}/100 <span class="badge">${esc(report.resume_match.confidence)}</span></p>`;if(report.positioning_thesis)html+=`<p><strong>Thesis:</strong> ${esc(report.positioning_thesis)}</p>`;if(report.format_contract)html+=`<p class="meta"><strong>Format:</strong> CV/resume.tex locked · 0% font-size change · company first</p>`;const layout=review.deterministic?.layout||{};if(layout.horizontal)html+=`<p class="meta"><strong>Space QA:</strong> ${layout.horizontal.measured||0} bullets measured · ${layout.horizontal.wrap_count||0} wraps · ${layout.horizontal.underfilled_line_count||0} lines with more than ~10 characters left · one-more-bullet ${layout.vertical_capacity?.pass?'overflows':'still fits'}</p>`;if(report.usage)html+=`<p class="meta"><strong>Codex usage:</strong> ${Number(report.usage.codex_tokens||0).toLocaleString()} tokens across ${report.usage.codex_calls||0} calls${report.usage.complete?'':' (some call totals unavailable)'}</p>`;html+=`<p><a href="/runs/${status.run_id}/resume.pdf" target="_blank">Open PDF</a> · <a href="/runs/${status.run_id}/resume.tex" target="_blank">Open LaTeX</a> · <a href="/runs/${status.run_id}/content_plan.json" target="_blank">Open source plan</a> · <a href="/runs/${status.run_id}/report.json" target="_blank">Open report</a></p>`;if((report.artifacts||[]).includes('resume-preview.png'))html+=`<img class="preview" src="/runs/${status.run_id}/resume-preview.png" alt="Rendered resume preview">`;html+='<pre>'+esc(JSON.stringify(review,null,2))+'</pre>';$('report').innerHTML=html;}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
$('search').oninput=()=>{clearTimeout(window.searchTimer);window.searchTimer=setTimeout(loadJobs,250)};$('sort').onchange=loadJobs;$('analyzeMatch').onclick=analyzeMatch;$('refreshEvidence').onclick=refreshEvidence;$('strict').onclick=()=>start('strict');$('dream').onclick=()=>start('dream');loadJobs();
</script></main></body></html>"""


class StudioHandler(BaseHTTPRequestHandler):
    manager: RunManager = RunManager()

    def log_message(self, format: str, *args) -> None:  # keep terminal output useful
        print("resume-studio:", format % args)

    def send_json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_bytes(self, raw: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if parsed.path == "/":
            return self.send_bytes(UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if parsed.path == "/api/health":
            graph = evidence_graph(repo_root())
            return self.send_json({
                "ok": True,
                "providers": {k: bool(v) for k, v in provider_commands().items()},
                "cv_root": str(cv_root(repo_root())),
                "evidence_graph": {"version": graph.get("version"), "nodes": len(graph.get("nodes", [])), "hash": graph.get("hash")},
            })
        if parsed.path == "/api/jobs":
            params = parse_qs(parsed.query)
            query = params.get("query", [""])[0]
            sort_by = params.get("sort", ["best"])[0]
            if sort_by not in {"best", "newest", "resume_match"}:
                sort_by = "best"
            return self.send_json({"jobs": list_jobs(repo_root(), query=query, sort_by=sort_by), "sort": sort_by})
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            job = current_scored_jobs(repo_root()).get(job_id)
            if not job:
                return self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            value = dict(job)
            value["resume_match"] = resume_match_for_job(job, repo_root())
            return self.send_json(value)
        if parsed.path == "/api/run":
            run_id = parse_qs(parsed.query).get("id", [""])[0]
            status = self.manager.get(run_id)
            if not status:
                return self.send_json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return self.send_json(status)
        if parsed.path.startswith("/runs/"):
            parts = parsed.path.split("/")
            if len(parts) != 4 or not re.fullmatch(r"[a-f0-9]{12}", parts[2]):
                return self.send_json({"error": "invalid artifact path"}, HTTPStatus.BAD_REQUEST)
            run_dir = studio_root(repo_root()) / "runs" / parts[2]
            target = (run_dir / parts[3]).resolve()
            if run_dir.resolve() not in target.parents or not target.is_file():
                return self.send_json({"error": "artifact not found"}, HTTPStatus.NOT_FOUND)
            content_type = "application/pdf" if target.suffix == ".pdf" else "image/png" if target.suffix == ".png" else "application/json" if target.suffix == ".json" else "text/plain; charset=utf-8"
            return self.send_bytes(target.read_bytes(), content_type)
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/run", "/api/match", "/api/evidence/refresh"}:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/evidence/refresh":
                graph = evidence_graph(repo_root(), refresh_public=True)
                return self.send_json({
                    "ok": True,
                    "version": graph.get("version"),
                    "nodes": len(graph.get("nodes", [])),
                    "hash": graph.get("hash"),
                    "public_refresh_errors": graph.get("public_refresh_errors", []),
                })
            job_id = str(body.get("job_id") or "")
            job = current_scored_jobs(repo_root()).get(job_id)
            if not job:
                return self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            if parsed.path == "/api/match":
                posting_text = fetch_job_description(job)
                match = resume_match_for_job(job, repo_root(), posting_text=posting_text)
                return self.send_json({"job_id": job_id, "resume_match": match})
            mode = str(body.get("mode") or "")
            status = self.manager.start(job, mode)
            return self.send_json(status, HTTPStatus.ACCEPTED)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run Victor's local Resume Studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4317)
    args = parser.parse_args(list(argv) if argv is not None else None)
    server = ThreadingHTTPServer((args.host, args.port), StudioHandler)
    print("Resume Studio: http://%s:%s/" % (args.host, args.port))
    print("Private CV root: %s" % cv_root(repo_root()))
    print("Providers: %s" % ", ".join(name for name, path in provider_commands().items() if path) or "none")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nResume Studio stopped")
    finally:
        server.server_close()
        StudioHandler.manager.executor.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
