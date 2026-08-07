#!/usr/bin/env python3
"""Victor-first local Resume Studio.

This is deliberately a local companion, not a hosted CV service.  It reads the
radar's public job snapshot and Victor's ignored ``CV/`` directory, then can
ask the installed first-party Codex and Claude Code CLIs to work on a private
resume draft using their existing local authentication.

The service has two modes:

* ``strict`` selects only existing, human-approved source bullets and runs
  deterministic layout checks against the canonical one-page resume format.
* ``dream``/``unrestricted`` run independent frontier drafts, a synthesis pass,
  and a fixed reviewer pass. The reviewer returns an applied corrected plan;
  this module computes the final score from the immutable rubric below.

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
# A line that ends only a few points before the text block is technically
# single-line in the PDF, but it is one font/rendering change away from
# wrapping. Treat that as a failed draft so the model gets a real chance to
# shorten it or the deterministic source fallback can restore a safer line.
MIN_RIGHT_SLACK_PT = 12.0
MAX_LINE_EDIT_PASSES = 2
MIN_TOTAL_BULLETS = 22
MAX_TOTAL_BULLETS = 26
MAX_AUTHORITY_CONTEXT_CHARS = 16000
MAX_METHODOLOGY_CONTEXT_CHARS = 12000
MAX_CONTEXT_PROMPT_CHARS = 12000
MAX_CATALOG_PROMPT_CHARS = 18000
MAX_GRAPH_PROMPT_CHARS = 28000
MAX_TARGET_KEYWORDS = 24
MAX_WORKSHOP_TEXT_CHARS = 900
MAX_WORKSHOP_REQUEST_CHARS = 3000
MAX_WORKSHOP_REVISIONS = 100
TAILOR_MODE_ALIASES = {
    "strict": "used", "source": "used", "source-only": "used", "used": "used",
    "dream": "ai", "enhanced": "ai", "ai": "ai", "ai-enhanced": "ai",
    "free": "unrestricted", "unrestricted": "unrestricted",
}
FORBIDDEN_RESUME_TERM_RE = re.compile(r"\bticc\b", re.I)
PROTECTED_QUALIFIERS = (
    "proof of concept",
    "prototype",
    "synthetic",
    "simulation",
    "simulated",
    "demo",
)
TARGET_KEYWORD_TERMS = (
    "machine learning", "deep learning", "computer vision", "software engineering",
    "object-oriented", "distributed systems", "data structures", "algorithms",
    "cloud computing", "natural language processing", "large language models",
    "generative ai", "retrieval augmented generation", "statistical analysis",
    "experimental design", "version control", "unit testing", "continuous integration",
    "linux", "bash", "slurm", "gpu", "cuda", "c++", "c#", "python", "java", "sql",
    "pytorch", "tensorflow", "scikit-learn", "numpy", "pandas", "fastapi", "flask",
    "docker", "kubernetes", "aws", "gcp", "google cloud", "alloydb", "postgresql",
    "postgres", "pgvector", "mongodb", "sqlite", "git", "github", "rest api", "api",
    "rag", "llm", "gemini", "agentic", "inference", "training", "quantization",
    "optimization", "hpc", "real-time", "multimodal", "data pipeline", "microservices",
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


def normalize_tailor_mode(mode: str) -> str:
    normalized = TAILOR_MODE_ALIASES.get(str(mode or "").strip().lower())
    if not normalized:
        raise ValueError("mode must be used, ai, or unrestricted")
    return normalized


def tailor_mode_label(mode: str) -> str:
    return {
        "used": "Used bullets",
        "ai": "AI tailor",
        "unrestricted": "Unrestricted AI tailor",
    }.get(normalize_tailor_mode(mode), "AI tailor")


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
    from radar.score import (RULES_VERSION, apply_company_concentration,
                             early_career_possible, explicit_new_grad, gates,
                             score, source_new_grad)

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
    apply_company_concentration(records)
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


def resume_pdf_filename(job: Dict[str, Any]) -> str:
    """Return the human-readable filename shown for a generated resume.

    Runs already have unique directories, but ``resume.pdf`` made it
    impossible to identify an artifact after downloading or opening several
    drafts.  Keep the company in the filename while leaving the run directory
    as the collision boundary.
    """
    company = re.sub(r"[^a-z0-9]+", "_", str(job.get("company") or "resume").lower()).strip("_")
    return "%s_resume_ai.pdf" % (company[:64] or "resume")


def run_pdf_path(run_dir: Path) -> Path:
    """Find the generated PDF for both new named runs and legacy runs."""
    status = read_json(run_dir / "status.json", {}) or {}
    configured = str(status.get("pdf_filename") or "").strip()
    if configured:
        return run_dir / Path(configured).name
    named = sorted(run_dir.glob("*_resume_ai.pdf"))
    if named:
        return named[0]
    return run_dir / "resume.pdf"


def run_preview_path(run_dir: Path) -> Path:
    pdf = run_pdf_path(run_dir)
    return run_dir / (pdf.stem + "-preview.png")


def _workshop_run_dir(root: Optional[Path], run_id: str) -> Optional[Path]:
    if not re.fullmatch(r"[a-f0-9]{12}", str(run_id or "")):
        return None
    directory = studio_root(root) / "runs" / str(run_id)
    return directory if directory.is_dir() else None


def workshop_artifact_url(run_id: str, revision_id: str, filename: str) -> str:
    return "/workshop/%s/%s/%s" % (
        quote(str(run_id), safe=""),
        quote(str(revision_id), safe=""),
        quote(Path(filename).name, safe=""),
    )


def _download_filename(value: Any) -> str:
    """Return a safe, human-readable filename for inline PDF previews."""
    name = Path(str(value or "resume.pdf")).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return safe or "resume.pdf"


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
        if _contains_forbidden_resume_term(company) or _contains_forbidden_resume_term(role):
            return
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
            if _contains_forbidden_resume_term(bullet):
                continue
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
            if _contains_forbidden_resume_term(heading):
                continue
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
                if _contains_forbidden_resume_term(bullet):
                    continue
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


def _library_dir(root: Optional[Path], source: str, entry_id: str) -> Optional[Path]:
    base = studio_root(root)
    if source == "run" and re.fullmatch(r"[a-f0-9]{12}", entry_id or ""):
        return base / "runs" / entry_id
    if source == "experiment" and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", entry_id or ""):
        return base / "architecture_experiments" / entry_id
    return None


def logical_pdf_filename(job: Dict[str, Any], physical: Path) -> str:
    """Give legacy ``resume.pdf`` runs the same identifiable public name."""
    if physical.name == "resume.pdf":
        return resume_pdf_filename(job)
    return physical.name


def artifact_target(directory: Path, filename: str) -> Optional[Path]:
    """Resolve a public artifact name, including the legacy PDF alias."""
    target = (directory / Path(filename).name).resolve()
    if directory.resolve() not in target.parents:
        return None
    if target.is_file():
        return target
    if target.suffix == ".pdf" and target.name.endswith("_resume_ai.pdf"):
        legacy = directory / "resume.pdf"
        if legacy.is_file():
            return legacy
    if target.suffix == ".png" and target.name.endswith("_resume_ai-preview.png"):
        legacy = directory / "resume-preview.png"
        if legacy.is_file():
            return legacy
    return None


def _library_entry(root: Optional[Path], source: str, entry_id: str, directory: Path) -> Dict[str, Any]:
    status = read_json(directory / "status.json", {}) or {}
    report = read_json(directory / "report.json", {}) or status.get("report") or {}
    if not isinstance(report, dict):
        report = {}
    job = read_json(directory / "job.json", {}) or report.get("job") or status.get("job") or {}
    if not isinstance(job, dict):
        job = {}
    pdf = run_pdf_path(directory)
    preview = run_preview_path(directory)
    review = report.get("review") if isinstance(report.get("review"), dict) else {}
    resume_match = report.get("resume_match") if isinstance(report.get("resume_match"), dict) else None
    context = read_json(directory / "job_context.json", {}) or {}
    if not isinstance(context, dict):
        context = {}
    mode = str(status.get("mode") or report.get("mode") or "unknown")
    mode = {"strict": "used", "source-only": "used", "dream": "ai", "enhanced": "ai"}.get(mode, mode)
    public_pdf_name = logical_pdf_filename(job, pdf)
    public_preview_name = public_pdf_name[:-4] + "-preview.png" if public_pdf_name.endswith(".pdf") else preview.name
    created_at = str(status.get("created_at") or report.get("created_at") or "")
    if not created_at:
        try:
            created_at = dt.datetime.fromtimestamp(directory.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds")
        except OSError:
            created_at = ""
    display_status = str(status.get("status") or ("complete" if report or pdf.exists() else "unknown"))
    if display_status in {"queued", "running"}:
        stamp = str(status.get("updated_at") or status.get("created_at") or "")
        try:
            age = time.time() - dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, OverflowError):
            age = 0
        if age > 30 * 60:
            display_status = "interrupted"
    artifacts = []
    for name in (public_pdf_name, public_preview_name, "job.json", "job_context.json", "report.json", "content_plan.json", "candidate_plan.json", "layout_packing.json", "resume.tex", "resume.txt", "workshop.json"):
        if (directory / name).is_file():
            artifacts.append(name)
    # Keep legacy physical artifacts discoverable while presenting an
    # identifiable filename to the user. The artifact route resolves the alias.
    if pdf.is_file() and public_pdf_name not in artifacts:
        artifacts.append(public_pdf_name)
    if preview.is_file() and public_preview_name not in artifacts:
        artifacts.append(public_preview_name)
    return {
        "entry_id": entry_id,
        "source": source,
        "legacy": source != "run",
        "run_id": entry_id if source == "run" else "",
        "status": display_status,
        "step": str(status.get("step") or ""),
        "message": str(status.get("message") or ""),
        "mode": mode,
        "created_at": created_at,
        "updated_at": str(status.get("updated_at") or created_at),
        "job": job_summary(job, resume_match),
        "pdf_filename": public_pdf_name,
        "preview_filename": public_preview_name if preview.is_file() else "",
        "has_pdf": pdf.is_file(),
        "has_posting_snapshot": bool(str(context.get("posting_text") or job.get("description") or "").strip()),
        "has_workshop": source == "run" and (directory / "content_plan.json").is_file(),
        "craft_score": review.get("craft_score"),
        "ready": review.get("ready"),
        "review_plan_applied": report.get("review_plan_applied"),
        "validation_warnings": report.get("validation_warnings") or [],
        "artifacts": artifacts,
        "urls": {
            "pdf": "/artifacts/%s/%s/%s" % (quote(source, safe=""), quote(entry_id, safe=""), quote(public_pdf_name, safe="")),
            "preview": "/artifacts/%s/%s/%s" % (quote(source, safe=""), quote(entry_id, safe=""), quote(public_preview_name, safe="")) if preview.is_file() else "",
            "posting": "/api/posting?source=%s&id=%s" % (quote(source, safe=""), quote(entry_id, safe="")),
            "workshop": "/api/workshop?id=%s" % quote(entry_id, safe="") if source == "run" and (directory / "content_plan.json").is_file() else "",
        },
    }


def resume_library(root: Optional[Path] = None, query: str = "", job_id: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """Index completed, running, and legacy private resume artifacts.

    The index is derived from per-run metadata rather than a single mutable
    pointer.  Selecting a different posting therefore cannot hide or delete a
    prior result, and older runs remain visible after the UI is restarted.
    """
    base = studio_root(root)
    candidates: List[Tuple[str, str, Path]] = []
    runs = base / "runs"
    if runs.is_dir():
        for directory in runs.iterdir():
            if directory.is_dir() and (directory / "status.json").is_file():
                candidates.append(("run", directory.name, directory))
    experiments = base / "architecture_experiments"
    if experiments.is_dir():
        for directory in experiments.iterdir():
            if directory.is_dir() and (directory / "report.json").is_file():
                candidates.append(("experiment", directory.name, directory))
    entries = [_library_entry(root, source, entry_id, directory) for source, entry_id, directory in candidates]
    needle = " ".join(str(query or "").lower().split())
    filtered = []
    for entry in entries:
        job = entry["job"]
        haystack = " ".join([str(job.get("company") or ""), str(job.get("title") or ""), str(job.get("sector") or "")]).lower()
        if needle and not all(part in haystack for part in needle.split()):
            continue
        if job_id and str(job.get("id") or "") != job_id:
            continue
        filtered.append(entry)
    filtered.sort(key=lambda item: (item.get("created_at") or "", item.get("entry_id") or ""), reverse=True)
    return filtered[:max(1, min(int(limit or 200), 500))]


def studio_usage(root: Optional[Path] = None) -> Dict[str, Any]:
    """Aggregate observed provider usage from durable run reports.

    Codex CLI does not expose a user's Plus weekly allowance to this local
    process. We therefore report measured calls/tokens and only calculate a
    percentage when the owner explicitly supplies CODEX_WEEKLY_LIMIT_TOKENS.
    """
    base = studio_root(root)
    now = dt.datetime.now(dt.timezone.utc)
    week_start = (now - dt.timedelta(days=now.weekday())).date().isoformat()
    totals = {"codex_tokens": 0, "codex_calls": 0, "claude_tokens": 0, "claude_calls": 0}
    runs = 0
    completed = 0
    for report_path in (sorted((base / "runs").glob("*/report.json")) if (base / "runs").is_dir() else []):
        report = read_json(report_path, {}) or {}
        if not isinstance(report, dict):
            continue
        status = read_json(report_path.parent / "status.json", {}) or {}
        stamp = str(status.get("updated_at") or status.get("created_at") or "")
        try:
            updated = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            updated = dt.datetime.fromtimestamp(report_path.stat().st_mtime, dt.timezone.utc)
        if updated.date().isoformat() < week_start:
            continue
        runs += 1
        if status.get("status") == "complete":
            completed += 1
        usage = report.get("usage") if isinstance(report.get("usage"), dict) else {}
        codex_tokens = int(usage.get("codex_tokens") or 0)
        codex_calls = int(usage.get("codex_calls") or 0)
        totals["codex_tokens"] += codex_tokens
        totals["codex_calls"] += codex_calls
        for provider in report.get("providers") or []:
            if not isinstance(provider, dict) or not provider.get("called"):
                continue
            name = str(provider.get("provider") or "").split("/")[-1]
            if name == "claude":
                totals["claude_calls"] += 1
                totals["claude_tokens"] += int(provider.get("usage_tokens") or 0)
    configured = os.environ.get("CODEX_WEEKLY_LIMIT_TOKENS", "").strip()
    limit = int(configured) if configured.isdigit() and int(configured) > 0 else None
    return {
        "week_start": week_start,
        **totals,
        "runs": runs,
        "completed_runs": completed,
        "weekly_limit_tokens": limit,
        "percent_of_limit": round(100 * totals["codex_tokens"] / limit, 1) if limit else None,
        "quota_status": "configured" if limit else "unavailable_from_codex_cli",
        "note": (
            "Observed local run usage; Codex Plus weekly allowance is not exposed to the local CLI."
            if not limit else "Observed local run usage compared with CODEX_WEEKLY_LIMIT_TOKENS."
        ),
    }


def posting_snapshot(root: Optional[Path], source: str, entry_id: str) -> Optional[Dict[str, Any]]:
    directory = _library_dir(root, source, entry_id)
    if directory is None or not directory.is_dir():
        return None
    entry = _library_entry(root, source, entry_id, directory)
    context = read_json(directory / "job_context.json", {}) or {}
    if not isinstance(context, dict):
        context = {}
    job = read_json(directory / "job.json", {}) or entry.get("job") or {}
    if not isinstance(job, dict):
        job = entry.get("job") or {}
    posting_text = str(context.get("posting_text") or job.get("description") or "").strip()
    return {
        "entry_id": entry_id,
        "source": source,
        "job": job_summary(job),
        "posting_text": posting_text,
        "captured_at": entry.get("created_at"),
        "available": bool(posting_text),
    }


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
    if label.startswith("workshop"):
        return bool(data.get("suggestions") or str(data.get("reply") or "").strip())
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
        bullet_properties["source_ids"] = {
            "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8
        }
        bullet_properties["text"] = {"type": "string"}
        bullet_properties["evidence_ids"] = {
            "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8
        }
        bullet_properties["candidate_rationale"] = {"type": "string"}
        bullet_required.extend(["source_ids", "text", "evidence_ids", "candidate_rationale"])
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


def workshop_schema() -> Dict[str, Any]:
    """Schema for a small, interactive workshop call.

    Workshop calls return suggestions rather than silently mutating a resume.
    The owner chooses which candidate becomes a durable revision in the UI.
    """
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_id": {"type": "string"},
                        "text": {"type": "string"},
                        "rationale": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    },
                    "required": ["line_id", "text", "rationale", "evidence_ids"],
                    "additionalProperties": False,
                },
                "maxItems": 5,
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["reply", "suggestions", "warnings"],
        "additionalProperties": False,
    }


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


def _prompt_excerpt(value: str, limit: int) -> str:
    """Keep prompt authority bounded while retaining document conclusions."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, round(limit * 0.72))
    tail = max(1, limit - head)
    return text[:head] + "\n\n[...middle of source document omitted... ]\n\n" + text[-tail:]


def resume_authority_context(root: Optional[Path] = None) -> str:
    """Inline the CV documents that govern judgment, not just raw bullets.

    The evidence graph indexes every Markdown file, but the frontier calls also
    need the owner's method and J&J source-of-truth rules in a compact,
    deterministic block. This avoids asking a model to infer the methodology
    from whichever 120 graph nodes happen to rank for a posting.
    """
    cv = cv_root(root or repo_root())
    names = (
        "RESUME_NOTES.md",
        "JJ_RESUME_CONTEXT.md",
        "experiences/JJ_SOURCE_OF_TRUTH.md",
        "experiences/JJ_AI_Data_Science_Intern.md",
        "experiences/JJ_BULLET_ITERATION_LOG.md",
        "Victor_Jimenez_Knowledge_Base_v2.md",
    )
    parts: List[str] = []
    remaining = MAX_AUTHORITY_CONTEXT_CHARS
    for name in names:
        if remaining <= 0:
            break
        try:
            text = (cv / name).read_text(errors="replace")
        except OSError:
            continue
        excerpt_limit = min(7000, remaining)
        excerpt = _prompt_excerpt(text, excerpt_limit)
        parts.append("# " + name + "\n" + excerpt)
        remaining -= len(excerpt) + len(name) + 4
    return "\n\n".join(parts)


def _keyword_pattern(term: str) -> str:
    escaped = re.escape(str(term or "").lower())
    escaped = escaped.replace(r"\ ", r"\s+")
    escaped = escaped.replace(r"\-", r"[-\s]+")
    return r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])"


def _keyword_present(term: str, text: str) -> bool:
    return bool(re.search(_keyword_pattern(term), str(text or "").lower()))


def target_keyword_strategy(
    context: Dict[str, Any], catalog: Dict[str, Any], root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return exact, source-grounded ATS targets for a posting.

    Capability clusters are useful for ranking but are too vague for ATS
    feedback. This compact strategy names exact technical phrases found in the
    captured posting, marks whether Victor can defend each one, and gives the
    model the source IDs it may use. Unsupported terms remain visible as gaps;
    they are never turned into invented resume claims.
    """
    posting = str(context.get("posting_text") or "")
    if len(posting.strip()) < 300:
        return {
            "posting_available": False,
            "reason": "No full posting text was captured; exact ATS targeting is unavailable until the posting is fetched.",
            "terms": [],
            "required_terms": [],
            "preferred_terms": [],
        }
    cv = cv_root(root or repo_root())
    source_texts: List[Tuple[str, str]] = []
    for filename in ("resume.tex", "cv_full.tex"):
        try:
            source_texts.append(("CV/" + filename, _latex_plain((cv / filename).read_text(errors="replace"))))
        except OSError:
            continue
    for entry in (catalog.get("entries") or {}).values():
        for bullet in entry.get("bullets", []):
            source_texts.append((str(bullet.get("id") or ""), _latex_plain(str(bullet.get("text") or ""))))

    sentences = [part.strip() for part in re.split(r"[\n.!?;]+", posting) if part.strip()]
    terms: List[Dict[str, Any]] = []
    for term in sorted(TARGET_KEYWORD_TERMS, key=lambda value: (-len(value), value)):
        if not _keyword_present(term, posting):
            continue
        matching_sources = [source_id for source_id, text in source_texts if _keyword_present(term, text)]
        required = any(
            _keyword_present(term, sentence)
            and re.search(r"\b(required|must|minimum|qualifications?|you will)\b", sentence, re.I)
            for sentence in sentences
        )
        preferred = any(
            _keyword_present(term, sentence)
            and re.search(r"\b(preferred|nice to have|bonus|ideally)\b", sentence, re.I)
            for sentence in sentences
        )
        terms.append({
            "term": term,
            "required": bool(required),
            "preferred": bool(preferred),
            "supported": bool(matching_sources),
            "source_ids": matching_sources[:6],
        })
        if len(terms) >= MAX_TARGET_KEYWORDS:
            break
    required_terms = [item["term"] for item in terms if item["required"]]
    preferred_terms = [item["term"] for item in terms if item["preferred"]]
    return {
        "posting_available": True,
        "reason": "Exact terms extracted from the captured posting and checked against the authorized CV corpus.",
        "terms": terms,
        "required_terms": required_terms,
        "preferred_terms": preferred_terms,
    }


def resume_methodology_context(root: Optional[Path] = None) -> str:
    """Inline the two governing methods so model calls stay self-contained."""
    cv = cv_root(root or repo_root())
    parts = []
    for name in ("RESUME_TAILORING_PLAYBOOK.md", "RESUME_BULLET_METHODOLOGY.md"):
        try:
            parts.append("# " + name + "\n" + (cv / name).read_text(errors="replace"))
        except OSError:
            continue
    return _prompt_excerpt("\n\n".join(parts), MAX_METHODOLOGY_CONTEXT_CHARS)


def base_prompt(
    context: Dict[str, Any],
    role: str,
    catalog: Dict[str, Any],
    enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
    unrestricted: bool = False,
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
- The target keyword strategy below is an ATS aid, not a license to keyword
  stuff. Use exact supported terms when they naturally describe an authorized
  artifact; put unsupported requirements in the gap list rather than inventing
  them. Skills may carry a supported term, but the strongest terms should also
  appear in meaningful experience/project lines when evidence allows.
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
- Projects are deliberately replaceable. Choose the 4-5 strongest projects
  from the complete source catalog for this target; do not preserve a base-CV
  project merely because it appeared in CV/resume.tex. Reword supported
  bullets around the posting's exact terms and explain project swaps in
  revision_notes.
""".strip()
    if enhance:
        role_guardrails += (
            "\n- Enhancement mode is a real drafting pass, not a synonym swap. You may substantially rewrite, combine, "
            "reorder, or replace a weak selected bullet with a stronger line grounded in one or more authorized "
            "source bullets. Keep the selected bullet's primary source_id, put every supporting source in source_ids, "
            "use only inline \\textbf/\\emph emphasis, and follow the methodology. Cite every fact-bearing source in "
            "evidence_ids and explain why the candidate improves the benchmark. "
            "Public GitHub/Devpost nodes corroborate breadth but cannot authorize a claim by themselves."
        )
        if unrestricted:
            role_guardrails += (
                "\n- Unrestricted AI mode is the creative workshop pass: depart from the exact wording and ordering of "
                "the base resume, synthesize across the full authorized CV evidence bank, choose different projects, "
                "and write original bullets that make a sharper argument for this posting. Do not merely substitute "
                "keywords. This freedom changes wording and selection, not factuality: every fact still needs an "
                "authorized source, protected prototype/simulation qualifiers stay intact, and the final page must "
                "remain honest, readable, and reviewable."
            )
    else:
        role_guardrails += (
            "\n- Source-only mode is selection, not rewriting. Choose source IDs only; the harness will copy every heading and bullet verbatim."
        )
    role_guardrails += "\n- TICC is permanently excluded from every resume; never select, rewrite, or mention that activity, even for a TLDP target."
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
        "CV authority dossier (read this before choosing or rewriting evidence):\n"
        + resume_authority_context(repo_root())
        + "\n\n"
        "Job context:\n"
        + context_text[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nSource-addressable evidence catalog:\n"
        + catalog_text[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nTarget-ranked evidence graph nodes (authority and claim_allowed are binding):\n"
        + graph_text[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
    )


def synthesis_prompt(
    context: Dict[str, Any], drafts: List[Dict[str, Any]], catalog: Dict[str, Any], enhance: bool,
    graph: Optional[Dict[str, Any]] = None, unrestricted: bool = False,
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
        + ("You may substantially rewrite or synthesize bullet text from the authorized source bank; preserve the primary source_id, add all supporting source IDs, and retain every scope-limiting qualifier. " if enhance else "Select source IDs verbatim; do not rewrite bullets. ")
        + ("This is the unrestricted creative pass: write genuinely original, role-specific bullets and make decisive project swaps when the evidence supports them; do not collapse back to base-resume phrasing. " if unrestricted else "")
        + "Choose the stronger defensible plan rather than averaging it.\n\n"
        "Job context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nEvidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nTarget-ranked evidence graph:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nCV authority dossier:\n"
        + resume_authority_context(repo_root())
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
        + "\n\nCompeting drafts:\n"
        + json.dumps(packed, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
    )


def reviewer_prompt(
    context: Dict[str, Any], tex: str,
    plan: Optional[Dict[str, Any]] = None,
    graph_context: Optional[List[Dict[str, Any]]] = None,
    catalog: Optional[Dict[str, Any]] = None,
    unrestricted: bool = False,
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
        "human references. You may reorder, replace, or substantially rewrite bullets using one or more authorized "
        "source IDs; source_ids must list every source bullet used to synthesize a line. Remove overlap and unsupported implications; do not solve criticism by "
        "making the page sparse. Then grade the FINAL plan you return, not the input draft. "
        "Projects are replaceable: correct a weak project choice for the target instead of preserving the base portfolio. "
        + ("This is an unrestricted creative pass; preserve factual boundaries but prefer a fresh, specific argument over safe base-CV wording. " if unrestricted else "")
        + "Use the exact ATS keyword strategy to improve supported keyword coverage, while recording unsupported requirements as missing evidence. "
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
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nBullet provenance plan:\n"
        + json.dumps(plan or {}, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nSource-addressable evidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog or {}), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nAuthorized evidence context:\n"
        + json.dumps(graph_context or [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nCV authority dossier:\n"
        + resume_authority_context(repo_root())
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
    )


def line_editor_prompt(
    context: Dict[str, Any], plan: Dict[str, Any], layout: Dict[str, Any],
    graph: Dict[str, Any],
) -> str:
    return (
        "You are Victor's final one-line resume editor. This request is self-contained; do not inspect the filesystem "
        "or run commands. Preserve every selected entry, bullet source_id, "
        "evidence_id, fact, priority, and section order. Change only bullet text. For wrapped or near-wrap lines "
        "(less than the stated safe right slack), cut filler and compress clauses without losing the technical object, "
        "supported ATS term, or proof. A concise line may end early; never "
        "expand a bullet merely to approach the right margin. Do not pad, invent, change layout, or return LaTeX beyond inline textbf/emph. Return the complete "
        "structured plan under the same schema.\n\nTarget:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nCurrent plan:\n"
        + json.dumps(plan, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nRendered bullet geometry:\n"
        + json.dumps((layout.get("horizontal") or {}).get("bullets", []), indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nRelevant evidence:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")), indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
    )


def workshop_ai_prompt(
    context: Dict[str, Any], plan: Dict[str, Any], catalog: Dict[str, Any],
    request: str, line_id: str = "", graph: Optional[Dict[str, Any]] = None,
) -> str:
    current_line = _workshop_line(plan, line_id) if line_id else None
    all_sources = {
        str(item.get("id")): str(item.get("text") or "")
        for entry in catalog.get("entries", {}).values()
        for item in entry.get("bullets", [])
    }
    allowed = []
    if current_line:
        for source_id in current_line.get("source_ids") or [current_line.get("source_id")]:
            if source_id in all_sources:
                allowed.append({"source_id": source_id, "text": all_sources[source_id]})
    if not allowed:
        allowed = [
            {"source_id": item["source_id"], "text": item["text"]}
            for item in workshop_lines(plan, catalog)
        ][:80]
    target = {
        "line_id": line_id,
        "current_line": current_line.get("text") if current_line else "",
        "authorized_source_lines": allowed,
    }
    return (
        "You are the writing partner inside Victor Jimenez's resume workshop. "
        "Return JSON matching the supplied schema. A suggestion is not applied automatically. "
        "Write a complete, meaningful resume line: lead with the strongest action and technical object, "
        "keep concrete mechanism/scope/result, and remove filler. You may substantially rewrite the line or "
        "synthesize the authorized source lines. Never invent a metric, user, adoption, deployment, accuracy, "
        "technology, date, or outcome. Preserve prototype, POC, synthetic, simulation, demo, and other scope "
        "limits. Keep inline emphasis only (\\textbf and \\emph), and do not return LaTeX layout commands. "
        "Return 2-4 genuinely different candidates when revising a line. Every candidate must use the requested "
        "line_id and list the evidence IDs that authorize it. For a general conversation, answer in reply and "
        "only include suggestions when a concrete line change is warranted.\n\n"
        "Request:\n" + str(request or "").strip()[:MAX_WORKSHOP_REQUEST_CHARS]
        + "\n\nTarget line and authorized source material:\n"
        + json.dumps(target, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nTarget posting:\n"
        + json.dumps({
            "company": context.get("company"), "title": context.get("title"),
            "posting_text": str(context.get("posting_text") or "")[:MAX_POSTING_CHARS],
        }, indent=2, ensure_ascii=False)
        + "\n\nMethodology:\n"
        + resume_methodology_context(repo_root())[:MAX_PROMPT_CHARS]
        + "\n\nCV authority dossier:\n"
        + resume_authority_context(repo_root())
        + "\n\nAuthorized evidence context:\n"
        + json.dumps(
            evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [],
            indent=2, ensure_ascii=False,
        )[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
    )


def workshop_ai(
    root: Optional[Path], run_id: str, request: str, line_id: str = "",
    provider: str = "",
) -> Dict[str, Any]:
    run_dir = _workshop_run_dir(root, run_id)
    if run_dir is None:
        raise ValueError("run not found")
    request = str(request or "").strip()
    if not request:
        raise ValueError("Tell the workshop what you want changed")
    if len(request) > MAX_WORKSHOP_REQUEST_CHARS:
        raise ValueError("Workshop request is too long")
    catalog = source_catalog(root or repo_root())
    state = _workshop_state(run_dir, catalog, root=root or repo_root())
    line = _workshop_line(state["plan"], line_id) if line_id else None
    if line_id and line is None:
        raise ValueError("resume line not found")
    context = read_json(run_dir / "job_context.json", {}) or {}
    graph = evidence_graph(root or repo_root())
    selected = provider or ("codex" if provider_commands().get("codex") else "claude")
    if not provider_commands().get(selected):
        raise ValueError("Provider is not installed: %s" % selected)
    label = "workshop_%s" % uuid.uuid4().hex[:8]
    result = run_provider(
        selected,
        workshop_ai_prompt(context, state["plan"], catalog, request, line_id=line_id, graph=graph),
        run_dir,
        label,
        timeout=5 * 60,
        schema=workshop_schema(),
    )
    write_json(run_dir / (label + ".json"), result)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "AI workshop call failed")
    data = result.get("data") or {}
    valid_line_ids = {
        str(item.get("line_id")) for item in workshop_lines(state["plan"], catalog)
    }
    suggestions = []
    warnings = list(data.get("warnings") or [])
    for suggestion in data.get("suggestions") or []:
        if not isinstance(suggestion, dict):
            continue
        suggestion_line_id = str(suggestion.get("line_id") or line_id)
        if suggestion_line_id not in valid_line_ids:
            warnings.append("Dropped a suggestion for an unknown line")
            continue
        try:
            normalized, _ = _workshop_validate_text(
                str(suggestion.get("text") or ""),
                str((_workshop_line(state["plan"], suggestion_line_id) or {}).get("text") or ""),
                "ai",
            )
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        suggestions.append({
            "line_id": suggestion_line_id,
            "text": normalized,
            "rationale": str(suggestion.get("rationale") or ""),
            "evidence_ids": [str(item) for item in (suggestion.get("evidence_ids") or []) if str(item)],
        })
    message = {
        "message_id": uuid.uuid4().hex[:10],
        "created_at": now_iso(),
        "kind": "ai",
        "provider": selected,
        "line_id": line_id,
        "request": request,
        "reply": str(data.get("reply") or ""),
        "suggestions": suggestions,
    }
    state.setdefault("messages", []).append(message)
    state.setdefault("provider_calls", []).append({
        "created_at": message["created_at"], "provider": selected,
        "label": label, "usage_tokens": result.get("usage_tokens"),
        "elapsed_seconds": result.get("elapsed_seconds"),
    })
    state["updated_at"] = now_iso()
    write_json(_workshop_state_path(run_dir), state)
    return {
        "run_id": run_id,
        "provider": selected,
        "reply": message["reply"],
        "suggestions": suggestions,
        "warnings": warnings,
        "usage_tokens": result.get("usage_tokens"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "workshop": _workshop_view(run_dir, catalog),
    }


def _plan_source_signature(plan: Dict[str, Any]) -> List[Tuple[str, Tuple[str, ...]]]:
    return [
        (str(entry.get("source_id")), tuple(str(bullet.get("source_id")) for bullet in entry.get("bullets", [])))
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
    ]


def _normalize_model_fragment(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = (
        value.strip()
        .replace("\u2011", "-")
        .replace("\u2013", "--")
        .replace("\u2014", "---")
        # Models occasionally emit a math-only multiplication command in
        # prose. The intended glyph is unambiguous and plain ``x`` is safer in
        # both LaTeX and ATS text extraction.
        .replace("\\times{}", "x")
        .replace("\\times", "x")
        # A few older CV source lines use the invalid-but-common ``texttimes``
        # spelling. Treat it as the same plain-text multiplication glyph before
        # inline-command validation so a source-grounded candidate is not
        # rejected for a formatting artifact in the evidence bank.
        .replace("\\texttimes{}", "x")
        .replace("\\texttimes", "x")
    )
    # Providers sometimes preserve the words but drop a LaTeX escape from a
    # copied currency, percentage, ampersand, hash, or underscore. These are
    # ordinary resume prose characters here, never math/layout syntax.
    for character in ("$", "%", "&", "#", "_"):
        normalized = re.sub(r"(?<!\\)" + re.escape(character), "\\\\" + character, normalized)
    return normalized


def _contains_forbidden_resume_term(value: Any) -> bool:
    return bool(FORBIDDEN_RESUME_TERM_RE.search(str(value or "")))


def _project_heading(value: Any) -> str:
    """Use the Studio's compact pipe separator for project metadata."""
    heading = str(value or "")
    return re.sub(r"\s*(?:---|—)\s*", " | ", heading)


def _assert_resume_exclusions(tex: str) -> None:
    if _contains_forbidden_resume_term(tex):
        raise ValueError("Generated resume contains a permanently excluded resume term")


def _unsupported_inline_commands(value: str) -> List[str]:
    allowed = {"textbf", "emph"}
    return sorted(
        {
            match.group(1)
            for match in re.finditer(r"\\([A-Za-z]+)", value)
            if match.group(1) not in allowed
        }
    )


def _merge_duplicate_entry_selections(
    selections: List[Any], validation_warnings: List[str]
) -> List[Any]:
    """Combine repeated entry blocks without duplicating their bullets.

    Reviewers sometimes split one source entry into two blocks to express two
    different reasons. Rendering both would repeat the heading; dropping the
    latter would discard valid evidence. Merge the blocks before validation so
    the ordinary source/evidence checks still govern every retained bullet.
    """
    merged: List[Any] = []
    by_source: Dict[str, Dict[str, Any]] = {}
    for selection in selections:
        if not isinstance(selection, dict):
            merged.append(selection)
            continue
        entry_id = str(selection.get("source_id") or "")
        if not entry_id or entry_id not in by_source:
            cloned = copy.deepcopy(selection)
            merged.append(cloned)
            if entry_id:
                by_source[entry_id] = cloned
            continue
        target = by_source[entry_id]
        known = {
            str(item.get("source_id") or "")
            for item in target.get("bullets") or []
            if isinstance(item, dict)
        }
        for bullet in selection.get("bullets") or []:
            bullet_id = str(bullet.get("source_id") or "") if isinstance(bullet, dict) else ""
            if bullet_id and bullet_id in known:
                continue
            target.setdefault("bullets", []).append(copy.deepcopy(bullet))
            if bullet_id:
                known.add(bullet_id)
        extra_why = str(selection.get("why") or "").strip()
        current_why = str(target.get("why") or "").strip()
        if extra_why and extra_why not in current_why:
            target["why"] = "; ".join(value for value in (current_why, extra_why) if value)
        validation_warnings.append("merged duplicate entry: %s" % entry_id)
    return merged


def validate_plan(
    plan: Dict[str, Any],
    catalog: Dict[str, Any],
    enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    entries = catalog.get("entries", {})
    all_bullet_bank = {
        str(item.get("id")): str(item.get("text") or "")
        for entry in entries.values()
        for item in entry.get("bullets", [])
        if item.get("id")
    }
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
        selections = _merge_duplicate_entry_selections(selections, validation_warnings)
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
                # Reviewers occasionally repeat an entry while returning a
                # complete corrected plan.  The source address is still
                # unambiguous, so discard only the duplicate and preserve the
                # rest of the safe plan with an auditable warning.
                validation_warnings.append("dropped duplicate entry: %s" % entry_id)
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
                if _contains_forbidden_resume_term(text):
                    errors.append("bullet %s contains a permanently excluded resume term" % bullet_id)
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
                supporting_ids = [bullet_id]
                if enhance:
                    raw_supporting_ids = bullet.get("source_ids") or [bullet_id]
                    if not isinstance(raw_supporting_ids, list):
                        raw_supporting_ids = [raw_supporting_ids]
                    supporting_ids = []
                    for supporting_id in raw_supporting_ids:
                        normalized_id = str(supporting_id or "")
                        if normalized_id in all_bullet_bank and normalized_id not in supporting_ids:
                            supporting_ids.append(normalized_id)
                        elif normalized_id:
                            validation_warnings.append(
                                "dropped unknown supporting source %s for %s"
                                % (normalized_id, bullet_id)
                            )
                    if bullet_id not in supporting_ids:
                        supporting_ids.insert(0, bullet_id)
                selected_bullets.append({
                    "source_id": bullet_id,
                    "source_ids": supporting_ids,
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


def content_change_report(
    plan: Dict[str, Any], catalog: Dict[str, Any], tex: str,
    keyword_strategy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Make tailoring changes inspectable instead of forcing PDF comparison."""
    entries = catalog.get("entries") or {}
    source_bullets = {
        str(bullet.get("id")): _latex_plain(str(bullet.get("text") or ""))
        for entry in entries.values()
        for bullet in entry.get("bullets", [])
    }
    base_project_ids = {
        str(entry.get("id"))
        for entry in entries.values()
        if entry.get("kind") == "project" and "resume.tex" in (entry.get("sources") or [])
    }
    selected_project_ids = [
        str(entry.get("source_id")) for entry in plan.get("projects", [])
    ]
    rewritten = []
    selected_bullet_ids = []
    for section in ("experiences", "projects", "leadership"):
        for entry in plan.get(section, []):
            for bullet in entry.get("bullets", []):
                source_id = str(bullet.get("source_id") or "")
                selected_bullet_ids.append(source_id)
                final_text = _latex_plain(str(bullet.get("text") or ""))
                source_text = source_bullets.get(source_id, "")
                supporting = [str(value) for value in (bullet.get("source_ids") or []) if str(value)]
                if final_text != source_text or len(supporting) > 1:
                    rewritten.append({
                        "section": section,
                        "source_id": source_id,
                        "source_text": source_text,
                        "final_text": final_text,
                        "source_ids": supporting or [source_id],
                        "rationale": str(bullet.get("candidate_rationale") or ""),
                    })

    keyword_terms = []
    rendered_text = _latex_plain(tex)
    for item in (keyword_strategy or {}).get("terms", []):
        term = str(item.get("term") or "")
        if not term:
            continue
        supported = bool(item.get("supported"))
        rendered = _keyword_present(term, rendered_text)
        status = "covered" if supported and rendered else "missing" if supported else "unsupported"
        keyword_terms.append({
            "term": term,
            "required": bool(item.get("required")),
            "preferred": bool(item.get("preferred")),
            "supported": supported,
            "rendered": rendered,
            "status": status,
            "source_ids": list(item.get("source_ids") or [])[:6],
        })
    supported_terms = [item for item in keyword_terms if item["supported"]]
    covered_terms = [item for item in supported_terms if item["rendered"]]
    keyword_coverage = {
        "posting_available": bool((keyword_strategy or {}).get("posting_available")),
        "reason": str((keyword_strategy or {}).get("reason") or ""),
        "supported_count": len(supported_terms),
        "covered_count": len(covered_terms),
        "exact_coverage_percent": round(100 * len(covered_terms) / max(1, len(supported_terms))),
        "required_terms": list((keyword_strategy or {}).get("required_terms") or []),
        "preferred_terms": list((keyword_strategy or {}).get("preferred_terms") or []),
        "terms": keyword_terms,
    }
    selected_project_set = set(selected_project_ids)
    swapped_in = [entry_id for entry_id in selected_project_ids if entry_id not in base_project_ids]
    swapped_out = [entry_id for entry_id in sorted(base_project_ids) if entry_id not in selected_project_set]
    return {
        "changed_bullet_count": len(rewritten),
        "rewritten_bullets": rewritten,
        "selected_bullet_count": len(selected_bullet_ids),
        "project_swaps": {
            "swapped_in": [_project_heading(entries.get(entry_id, {}).get("heading") or entry_id) for entry_id in swapped_in],
            "swapped_out": [_project_heading(entries.get(entry_id, {}).get("heading") or entry_id) for entry_id in swapped_out],
        },
        "keyword_coverage": keyword_coverage,
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
    unsafe = {
        str(item.get("source_id"))
        for item in (layout.get("horizontal") or {}).get("bullets", [])
        if item.get("wraps") is True or item.get("near_wrap") is True
    }
    if not unsafe:
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
                if source_id in unsafe and approved and bullet.get("text") != approved:
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
        lines.extend(["    \\resumeProjectHeading", "        {\\large %s}{}" % _project_heading(entry["heading"])])
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
    tex = "\n".join(lines)
    _assert_resume_exclusions(tex)
    return tex


def _replace_macro_call(source: str, macro: str, index: int, args: List[str]) -> str:
    calls = _macro_calls(source, macro, len(args))
    if index >= len(calls):
        return source
    start, _, after = calls[index]
    replacement = "\\%s%s" % (macro, "".join("{%s}" % value for value in args))
    return source[:start] + replacement + source[after:]


def front_matter_catalog(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Expose editable education/skills values without exposing LaTeX layout."""
    template = (cv_root(root or repo_root()) / CANONICAL_TEMPLATE)
    try:
        source = template.read_text()
    except OSError:
        return []
    if BODY_MARKER not in source:
        return []
    prefix = source.split(BODY_MARKER, 1)[0]
    result: List[Dict[str, Any]] = []
    headings = _macro_calls(prefix, "resumeSubheading", 4)
    if headings:
        _, args, _ = headings[0]
        labels = ("School", "Location", "Degree", "Dates")
        for key, label, text in zip(("school", "location", "degree", "dates"), labels, args):
            result.append({
                "line_id": "front:education:" + key,
                "section": "education",
                "entry_id": "front:education",
                "entry_label": "Education",
                "role": label,
                "text": text,
                "source_text": text,
                "template": "education",
                "template_index": 0,
                "argument_index": labels.index(label),
            })
    before_skills = prefix.lower().find("%-----------skills-----------")
    all_items = _macro_calls(prefix, "resumeItem", 1)
    skills = [
        (template_index, call)
        for template_index, call in enumerate(all_items)
        if call[0] > before_skills
    ]
    for index, (template_index, (_, args, _)) in enumerate(skills):
        result.append({
            "line_id": "front:skills:%s" % index,
            "section": "technical skills",
            "entry_id": "front:skills",
            "entry_label": "Technical Skills",
            "role": "Skill line %s" % (index + 1),
            "text": args[0],
            "source_text": args[0],
            "template": "skills",
            # _replace_macro_call indexes every parsed call, including macro
            # definitions in the preamble. Keep the absolute call index.
            "template_index": template_index,
            "argument_index": 0,
        })
    return result


def render_workshop_plan(
    plan: Dict[str, Any], catalog: Dict[str, Any], root: Optional[Path] = None,
) -> str:
    """Render body plus owner-editable education/skills values."""
    tex = render_plan(plan, catalog, root)
    front = {
        str(item.get("line_id")): str(item.get("text") or "")
        for item in plan.get("front_matter", [])
        if item.get("line_id")
    }
    if not front:
        _assert_resume_exclusions(tex)
        return tex
    marker = BODY_MARKER
    prefix, body = tex.split(marker, 1)
    headings = _macro_calls(prefix, "resumeSubheading", 4)
    education_keys = (
        "front:education:school", "front:education:location",
        "front:education:degree", "front:education:dates",
    )
    if headings and all(key in front for key in education_keys):
        prefix = _replace_macro_call(
            prefix, "resumeSubheading", 0,
            [front[key] for key in education_keys],
        )
    # resumeItem index 0 is the Education GPA; the next four are skills.
    for item in plan.get("front_matter", []):
        line_id = str(item.get("line_id") or "")
        if not line_id.startswith("front:skills:"):
            continue
        try:
            index = int(str(item.get("template_index")))
        except (TypeError, ValueError):
            continue
        prefix = _replace_macro_call(prefix, "resumeItem", index, [front[line_id]])
    rendered = prefix + marker + body
    _assert_resume_exclusions(rendered)
    return rendered


def _workshop_plan(plan: Dict[str, Any], root: Optional[Path] = None) -> Dict[str, Any]:
    """Add stable line identities without changing the render contract."""
    value = copy.deepcopy(plan)
    existing_front = value.get("front_matter")
    canonical_front = front_matter_catalog(root or repo_root())
    if isinstance(existing_front, list):
        existing_by_id = {
            str(item.get("line_id") or ""): item
            for item in existing_front
            if isinstance(item, dict) and item.get("line_id")
        }
        for item in canonical_front:
            prior = existing_by_id.get(str(item.get("line_id") or ""))
            if not prior:
                continue
            item["text"] = str(prior.get("text") or item.get("text") or "")
            item["revision_note"] = str(prior.get("revision_note") or "")
    value["front_matter"] = canonical_front
    for section in ("experiences", "projects", "leadership"):
        for entry in value.get(section, []):
            for bullet in entry.get("bullets", []):
                source_id = str(bullet.get("source_id") or "")
                bullet["line_id"] = str(bullet.get("line_id") or source_id)
                bullet["source_ids"] = list(
                    dict.fromkeys(
                        str(item)
                        for item in (bullet.get("source_ids") or [source_id])
                        if str(item)
                    )
                )
                if source_id and source_id not in bullet["source_ids"]:
                    bullet["source_ids"].insert(0, source_id)
                bullet.setdefault("evidence_ids", [source_id] if source_id else [])
                bullet.setdefault("revision_note", "")
    return value


def _workshop_line(plan: Dict[str, Any], line_id: str) -> Optional[Dict[str, Any]]:
    for item in plan.get("front_matter", []):
        if str(item.get("line_id") or "") == str(line_id):
            return item
    for section in ("experiences", "projects", "leadership"):
        for entry in plan.get(section, []):
            for bullet in entry.get("bullets", []):
                if str(bullet.get("line_id") or bullet.get("source_id")) == str(line_id):
                    return bullet
    return None


def workshop_lines(plan: Dict[str, Any], catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = catalog.get("entries", {})
    lines: List[Dict[str, Any]] = []
    for item in plan.get("front_matter", []):
        line_id = str(item.get("line_id") or "")
        lines.append({
            "line_id": line_id,
            "section": str(item.get("section") or ""),
            "entry_id": str(item.get("entry_id") or ""),
            "entry_label": str(item.get("entry_label") or ""),
            "role": str(item.get("role") or ""),
            "text": str(item.get("text") or ""),
            "source_text": str(item.get("source_text") or item.get("text") or ""),
            "source_id": line_id,
            "source_ids": [line_id] if line_id else [],
            "evidence_ids": [],
            "priority": None,
            "candidate_rationale": "Canonical education/skills line",
            "revision_note": str(item.get("revision_note") or ""),
        })
    for section in ("experiences", "projects", "leadership"):
        for entry in plan.get(section, []):
            source_entry = entries.get(str(entry.get("source_id"))) or {}
            source_bullets = {
                str(item.get("id")): str(item.get("text") or "")
                for item in source_entry.get("bullets", [])
            }
            for bullet in entry.get("bullets", []):
                source_id = str(bullet.get("source_id") or "")
                lines.append({
                    "line_id": str(bullet.get("line_id") or source_id),
                    "section": section,
                    "entry_id": str(entry.get("source_id") or ""),
                    "entry_label": _project_heading(source_entry.get("heading")) if section == "projects" else str(
                        source_entry.get("company")
                        or source_entry.get("heading")
                        or entry.get("source_id")
                    ),
                    "role": str(source_entry.get("role") or ""),
                    "text": str(bullet.get("text") or ""),
                    "source_text": source_bullets.get(source_id, str(bullet.get("text") or "")),
                    "source_id": source_id,
                    "source_ids": list(bullet.get("source_ids") or [source_id]),
                    "evidence_ids": list(bullet.get("evidence_ids") or [source_id]),
                    "priority": bullet.get("priority"),
                    "candidate_rationale": str(bullet.get("candidate_rationale") or ""),
                    "revision_note": str(bullet.get("revision_note") or ""),
                })
    return lines


def _workshop_state_path(run_dir: Path) -> Path:
    return run_dir / "workshop.json"


def _workshop_state(
    run_dir: Path, catalog: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    current = read_json(_workshop_state_path(run_dir), {}) or {}
    plan = current.get("plan") if isinstance(current.get("plan"), dict) else None
    if plan is None:
        plan = read_json(run_dir / "content_plan.json", {}) or {}
    if not isinstance(plan, dict) or not plan.get("experiences"):
        raise RuntimeError("This run has no completed content plan yet")
    base = root or (run_dir.parents[3] if len(run_dir.parents) > 3 else repo_root())
    value = {
        "version": 1,
        "run_id": str(current.get("run_id") or run_dir.name),
        "created_at": str(current.get("created_at") or now_iso()),
        "updated_at": now_iso(),
        "plan": _workshop_plan(plan, base),
        "revisions": list(current.get("revisions") or []),
        "messages": list(current.get("messages") or []),
        "last_render": current.get("last_render"),
        "provider_calls": list(current.get("provider_calls") or []),
    }
    if not current or current.get("plan") != value["plan"]:
        write_json(_workshop_state_path(run_dir), value)
    return value


def _workshop_view(run_dir: Path, catalog: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    catalog = catalog or source_catalog(repo_root())
    base = run_dir.parents[3] if len(run_dir.parents) > 3 else repo_root()
    state = _workshop_state(run_dir, catalog, root=base)
    status = read_json(run_dir / "status.json", {}) or {}
    report = read_json(run_dir / "report.json", {}) or {}
    job = read_json(run_dir / "job.json", {}) or (
        report.get("job") if isinstance(report, dict) else None
    ) or status.get("job") or {}
    original_pdf_name = logical_pdf_filename(job, run_pdf_path(run_dir))
    original_preview_name = (
        original_pdf_name[:-4] + "-preview.png"
        if original_pdf_name.endswith(".pdf") else run_preview_path(run_dir).name
    )
    render = state.get("last_render") if isinstance(state.get("last_render"), dict) else {}
    if render.get("revision_id") and render.get("pdf_filename"):
        render = dict(render)
        render["pdf_url"] = workshop_artifact_url(
            run_dir.name, render["revision_id"], render["pdf_filename"]
        )
        if render.get("preview_filename"):
            render["preview_url"] = workshop_artifact_url(
                run_dir.name, render["revision_id"], render["preview_filename"]
            )
    revisions = [
        {
            "revision_id": item.get("revision_id"),
            "created_at": item.get("created_at"),
            "kind": item.get("kind"),
            "label": item.get("label"),
            "line_id": item.get("line_id"),
            "provider": item.get("provider"),
        }
        for item in state.get("revisions", [])
        if isinstance(item, dict)
    ]
    return {
        "run_id": run_dir.name,
        "job": job_summary(job),
        "status": status,
        "mode": status.get("mode"),
        "pdf_filename": original_pdf_name,
        "preview_filename": original_preview_name,
        "plan": state["plan"],
        "lines": workshop_lines(state["plan"], catalog),
        "revisions": list(reversed(revisions)),
        "messages": state.get("messages", [])[-30:],
        "last_render": render,
        "providers": {name: bool(path) for name, path in provider_commands().items()},
        "original_pdf_url": (
            "/artifacts/run/%s/%s"
            % (quote(run_dir.name, safe=""), quote(original_pdf_name, safe=""))
            if run_pdf_path(run_dir).is_file() else ""
        ),
        "original_preview_url": (
            "/artifacts/run/%s/%s"
            % (quote(run_dir.name, safe=""), quote(original_preview_name, safe=""))
            if run_preview_path(run_dir).is_file() else ""
        ),
    }


def _workshop_record_revision(
    state: Dict[str, Any], plan: Dict[str, Any], kind: str, label: str,
    line_id: str = "", instruction: str = "", provider: str = "",
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    revision = {
        "revision_id": uuid.uuid4().hex[:10],
        "created_at": now_iso(),
        "kind": kind,
        "label": label,
        "line_id": line_id,
        "instruction": instruction[:MAX_WORKSHOP_REQUEST_CHARS],
        "provider": provider,
        "plan": _workshop_plan(plan, root or repo_root()),
    }
    revisions = list(state.get("revisions") or [])
    revisions.append(revision)
    state["revisions"] = revisions[-MAX_WORKSHOP_REVISIONS:]
    state["plan"] = revision["plan"]
    state["updated_at"] = now_iso()
    return revision


def _workshop_validate_text(text: str, original: str, origin: str) -> Tuple[str, List[str]]:
    normalized = _normalize_model_fragment(text)
    if not normalized:
        raise ValueError("A resume line cannot be empty")
    if _contains_forbidden_resume_term(normalized):
        raise ValueError("This resume term is permanently excluded")
    if len(_latex_plain(normalized)) > MAX_WORKSHOP_TEXT_CHARS:
        raise ValueError("Resume line is too long; keep the technical proof and cut filler")
    if FORBIDDEN_CONTENT_COMMANDS.search(normalized):
        raise ValueError("Line contains a layout command; edit wording only")
    unsupported = _unsupported_inline_commands(normalized)
    if unsupported:
        raise ValueError("Unsupported inline command(s): %s" % ", ".join(unsupported))
    warnings = []
    missing = _missing_protected_qualifiers(original, normalized)
    if missing and origin != "manual":
        raise ValueError("AI suggestion dropped protected qualifier(s): %s" % ", ".join(missing))
    if missing:
        warnings.append("Manual edit removed protected qualifier(s): %s" % ", ".join(missing))
    return normalized, warnings


def _workshop_render(
    run_dir: Path, plan: Dict[str, Any], revision: Dict[str, Any],
    root: Optional[Path] = None, catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    revision_dir = run_dir / "workshop" / "revisions" / str(revision["revision_id"])
    revision_dir.mkdir(parents=True, exist_ok=True)
    status = read_json(run_dir / "status.json", {}) or {}
    report = read_json(run_dir / "report.json", {}) or {}
    job = read_json(run_dir / "job.json", {}) or (
        report.get("job") if isinstance(report, dict) else None
    ) or status.get("job") or {}
    pdf_filename = "%s_workshop_%s.pdf" % (
        re.sub(r"[^a-z0-9]+", "_", str(job.get("company") or "resume").lower()).strip("_")[:64] or "resume",
        revision["revision_id"],
    )
    write_json(revision_dir / "status.json", {"pdf_filename": pdf_filename})
    base = root or repo_root()
    (revision_dir / "resume.tex").write_text(render_workshop_plan(plan, catalog or source_catalog(base), base))
    compiled = compile_resume(revision_dir)
    layout = pdf_layout(revision_dir, compiled, plan=plan)
    preview = render_preview(revision_dir)
    render = {
        "revision_id": revision["revision_id"],
        "pdf_filename": pdf_filename,
        "preview_filename": run_preview_path(revision_dir).name if preview else "",
        "compiled": bool(compiled.get("compiled")),
        "layout": layout,
    }
    write_json(revision_dir / "render.json", render)
    return render


def workshop_apply_edit(
    root: Optional[Path], run_id: str, line_id: str, text: str,
    origin: str = "manual", label: str = "Line edit", provider: str = "",
    instruction: str = "",
) -> Dict[str, Any]:
    run_dir = _workshop_run_dir(root, run_id)
    if run_dir is None:
        raise ValueError("run not found")
    catalog = source_catalog(root or repo_root())
    state = _workshop_state(run_dir, catalog, root=root or repo_root())
    line = _workshop_line(state["plan"], line_id)
    if line is None:
        raise ValueError("resume line not found")
    normalized, warnings = _workshop_validate_text(str(text or ""), str(line.get("text") or ""), origin)
    updated = copy.deepcopy(state["plan"])
    target = _workshop_line(updated, line_id)
    target["text"] = normalized
    target["revision_note"] = label
    revision = _workshop_record_revision(
        state, updated, "line_edit" if origin == "manual" else "ai_apply", label,
        line_id=line_id, instruction=instruction, provider=provider, root=root or repo_root(),
    )
    render = _workshop_render(run_dir, state["plan"], revision, root=root or repo_root(), catalog=catalog)
    state["last_render"] = render
    write_json(_workshop_state_path(run_dir), state)
    result = _workshop_view(run_dir, catalog)
    result["warnings"] = warnings
    return result


def workshop_revert(root: Optional[Path], run_id: str, revision_id: str) -> Dict[str, Any]:
    run_dir = _workshop_run_dir(root, run_id)
    if run_dir is None:
        raise ValueError("run not found")
    catalog = source_catalog(root or repo_root())
    state = _workshop_state(run_dir, catalog, root=root or repo_root())
    prior = next(
        (item for item in state.get("revisions", []) if str(item.get("revision_id")) == str(revision_id)),
        None,
    )
    if not isinstance(prior, dict) or not isinstance(prior.get("plan"), dict):
        raise ValueError("revision not found")
    revision = _workshop_record_revision(
        state, prior["plan"], "revert", "Reverted to %s" % revision_id,
        root=root or repo_root(),
    )
    render = _workshop_render(run_dir, state["plan"], revision, root=root or repo_root(), catalog=catalog)
    state["last_render"] = render
    write_json(_workshop_state_path(run_dir), state)
    return _workshop_view(run_dir, catalog)


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
    value = (value or "").replace("\\texttimes{}", "x").replace("\\texttimes", "x")
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
                        "near_wrap": slack < MIN_RIGHT_SLACK_PT,
                        "horizontal_pass": not wraps and slack >= MIN_RIGHT_SLACK_PT,
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
        "min_right_slack_pt": MIN_RIGHT_SLACK_PT,
        "measured": len(measurable),
        "bullets": results,
        "wrap_count": sum(item.get("wraps") is True for item in results),
        "near_wrap_count": sum(item.get("near_wrap") is True for item in results),
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
    pdf = run_pdf_path(run_dir)
    if not pdftoppm or not pdf.exists():
        return None
    prefix = run_preview_path(run_dir).with_suffix("")
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
        compiled_pdf = run_dir / "resume.pdf"
        pdf = run_pdf_path(run_dir)
        if proc.returncode != 0 or not compiled_pdf.exists():
            return {"compiled": False, "error": "LaTeX compilation failed", "exit_code": proc.returncode}
        if pdf != compiled_pdf:
            if pdf.exists():
                pdf.unlink()
            compiled_pdf.replace(pdf)
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
    pdf = run_pdf_path(run_dir)
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
                "one-line bullet check failed: %s wrap(s), %s near-wrap(s)"
                % (
                    result["horizontal"].get("wrap_count", 0),
                    result["horizontal"].get("near_wrap_count", 0),
                )
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
    if _contains_forbidden_resume_term(tex):
        warnings.append("a permanently excluded resume term appears in the draft")
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


def run_tailoring(
    run_dir: Path, job: Dict[str, Any], update, enhance: bool,
    unrestricted: bool = False,
) -> None:
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
    context["target_keywords"] = target_keyword_strategy(context, catalog, repo_root())
    write_json(run_dir / "job_context.json", context)
    mode_label = "unrestricted" if unrestricted else "enhanced" if enhance else "source-only"
    prompt = base_prompt(
        context, "an independent resume evidence strategist", catalog, enhance,
        graph=graph, unrestricted=unrestricted,
    )
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
            synthesis_prompt(
                context, successful, catalog, enhance, graph=graph,
                unrestricted=unrestricted,
            ),
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
            context, chosen, plan=plan, graph_context=graph_context, catalog=catalog,
            unrestricted=unrestricted,
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
                context, chosen, plan=plan, graph_context=graph_context, catalog=catalog,
                unrestricted=unrestricted,
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
    # The adversarial editor is allowed to change wording and project choices,
    # so its corrected plan can introduce a new near-wrap after the earlier
    # line-edit pass. Give the final plan the same measured repair opportunity
    # before rejecting the run.
    if enhance and not (layout.get("horizontal") or {}).get("pass"):
        for final_round in range(1, MAX_LINE_EDIT_PASSES + 1):
            update(
                "final_line_editing",
                "Repairing final-editor near-wraps against the rendered PDF (pass %s/%s)"
                % (final_round, MAX_LINE_EDIT_PASSES),
            )
            label = "final_line_edit" if final_round == 1 else "final_line_edit_%s" % final_round
            final_edit = run_provider(
                synthesizer,
                line_editor_prompt(context, plan, layout, graph),
                run_dir,
                label,
                timeout=6 * 60,
                schema=plan_schema(True),
            )
            line_edits.append(final_edit)
            write_json(run_dir / (label + ".json"), final_edit)
            if not final_edit.get("ok"):
                break
            edited, edit_errors = validate_plan(
                final_edit.get("data") or {}, catalog, True, graph=graph
            )
            if edit_errors or _plan_source_signature(edited) != _plan_source_signature(plan):
                write_json(
                    run_dir / (label + "_errors.json"),
                    edit_errors or ["final line editor changed selected source IDs"],
                )
                break
            plan, final_packing = pack_plan_to_page(
                merge_edited_bullets(plan, edited), catalog, run_dir / (label + "_pack")
            )
            packing[label + "_pack"] = final_packing
            write_json(run_dir / "content_plan.json", plan)
            write_json(run_dir / "layout_packing.json", packing)
            chosen = render_plan(plan, catalog, repo_root())
            (run_dir / "resume.tex").write_text(chosen)
            compiled = compile_resume(run_dir)
            layout = pdf_layout(run_dir, compiled, plan=plan)
            plan, restored_source_ids = restore_wrapped_source_text(plan, layout, catalog)
            if restored_source_ids:
                packing[label + "_source_reversions"] = restored_source_ids
                write_json(run_dir / "content_plan.json", plan)
                write_json(run_dir / "layout_packing.json", packing)
                chosen = render_plan(plan, catalog, repo_root())
                (run_dir / "resume.tex").write_text(chosen)
                compiled = compile_resume(run_dir)
                layout = pdf_layout(run_dir, compiled, plan=plan)
            if (layout.get("horizontal") or {}).get("pass"):
                break
        preview = render_preview(run_dir)
        deterministic = deterministic_review(context, chosen, layout)
    final_horizontal = layout.get("horizontal") or {}
    if not final_horizontal.get("pass"):
        rejection = {
            "reason": "Final resume rejected by the hard one-line bullet gate",
            "safe_right_slack_pt": MIN_RIGHT_SLACK_PT,
            "wrap_count": final_horizontal.get("wrap_count", 0),
            "near_wrap_count": final_horizontal.get("near_wrap_count", 0),
            "bullets": [
                item for item in final_horizontal.get("bullets", [])
                if item.get("wraps") is True or item.get("near_wrap") is True or not item.get("horizontal_pass")
            ],
        }
        write_json(run_dir / "layout_rejection.json", rejection)
        raise RuntimeError(
            "final resume rejected: %s wrap(s), %s near-wrap(s); see layout_rejection.json"
            % (rejection["wrap_count"], rejection["near_wrap_count"])
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
        "mode": mode_label,
        "pdf_filename": run_pdf_path(run_dir).name,
        "preview_filename": run_preview_path(run_dir).name if preview else "",
        "job": job_summary(job),
        "resume_match": match,
        "positioning_thesis": synthesis_data.get("positioning_thesis", ""),
        "selected_evidence": synthesis_data.get("selected_evidence", []),
        "excluded_evidence": synthesis_data.get("excluded_evidence", []),
        "revision_notes": synthesis_data.get("revision_notes", []),
        "validation_warnings": synthesis_data.get("validation_warnings", []),
        "content_changes": content_change_report(
            plan, catalog, chosen, context.get("target_keywords")
        ),
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
            run_pdf_path(run_dir).name,
            "resume.txt",
            run_preview_path(run_dir).name if preview else None,
            "job.json",
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
    # The first completed draft is immediately addressable in the workshop;
    # later edits create separate revision artifacts and never overwrite this
    # original generated PDF.
    _workshop_state(run_dir, catalog)
    update(
        "complete",
        "%s tailored draft and adversarial final edit are ready" % mode_label.capitalize(),
        report=report,
    )


def run_strict(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=False)


def run_dream(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=True)


def run_unrestricted(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=True, unrestricted=True)


class RunManager:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or repo_root()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.lock = threading.Lock()

    def start(self, job: Dict[str, Any], mode: str) -> Dict[str, Any]:
        mode = normalize_tailor_mode(mode)
        run_id = uuid.uuid4().hex[:12]
        run_dir = studio_root(self.root) / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        pdf_filename = resume_pdf_filename(job)
        status = {
            "run_id": run_id,
            "mode": mode,
            "status": "queued",
            "step": "queued",
            "message": "Queued",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "job": job_summary(job),
            "pdf_filename": pdf_filename,
            "preview_filename": Path(pdf_filename).stem + "-preview.png",
            "run_dir": str(run_dir),
        }
        # Keep the historical posting record attached to the run even if the
        # radar later removes or updates the live job.  This is private ignored
        # state, not a second source of truth for the public radar database.
        write_json(run_dir / "job.json", copy.deepcopy(job))
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
            if mode == "used":
                run_strict(run_dir, job, update)
            elif mode == "ai":
                run_dream(run_dir, job, update)
            else:
                run_unrestricted(run_dir, job, update)
        except Exception as exc:  # keep failure inspectable in the local UI
            trace = traceback.format_exc()
            (run_dir / "error.log").write_text(trace)
            self.update(run_id, run_dir, "failed", "error", str(exc), error_log="error.log")

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = studio_root(self.root) / "runs" / run_id / "status.json"
        value = read_json(path)
        if not isinstance(value, dict):
            return value
        job = read_json(path.parent / "job.json", {}) or value.get("job") or {}
        physical = run_pdf_path(path.parent)
        value = copy.deepcopy(value)
        value["pdf_filename"] = logical_pdf_filename(job, physical)
        preview = run_preview_path(path.parent)
        value["preview_filename"] = (
            value["pdf_filename"][:-4] + "-preview.png"
            if value["pdf_filename"].endswith(".pdf") and preview.is_file()
            else value.get("preview_filename", "")
        )
        return value


UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resume Studio · Job Radar</title>
<style>
:root{color-scheme:dark;--bg:#0e1117;--panel:#161b22;--line:#30363d;--muted:#8b949e;--text:#f0f6fc;--accent:#58a6ff;--good:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1250px;margin:0 auto;padding:28px 20px 70px}h1{margin:0 0 6px;font-size:28px}h2{font-size:18px;margin:0 0 12px}.sub{color:var(--muted);margin:0 0 20px}.grid{display:grid;grid-template-columns:410px 1fr;gap:18px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}.jobs{max-height:650px;overflow:auto}.job{width:100%;text-align:left;background:transparent;color:var(--text);border:1px solid transparent;border-radius:8px;padding:10px;margin:4px 0;cursor:pointer}.job:hover,.job.selected{background:#1f2937;border-color:#3b82f6}.job strong{display:block}.job small{color:var(--muted)}input,select,button{font:inherit}input,select{background:#0d1117;border:1px solid var(--line);border-radius:6px;color:var(--text);padding:9px}input{width:100%;margin-bottom:8px}.toolbar{display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:10px}.toolbar select{min-width:145px}button{background:#238636;border:1px solid #2ea043;color:#fff;border-radius:6px;padding:9px 12px;cursor:pointer;margin:4px 6px 4px 0}button.secondary{background:#21262d;border-color:var(--line)}button:disabled{opacity:.5;cursor:wait}.selected-card{border:1px solid var(--accent);border-radius:8px;padding:13px;margin:10px 0 16px}.meta{color:var(--muted);font-size:13px}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;margin:3px 4px 0 0;font-size:12px}.match-card{margin:10px 0;padding:10px;border-radius:7px;background:#0d1117;border:1px solid var(--line)}.match-card strong{font-size:18px}.status{border-left:3px solid var(--accent);padding:10px 12px;background:#111827;white-space:pre-wrap}.status.complete{border-color:var(--good)}.status.failed{border-color:var(--bad)}.status.running{border-color:var(--warn)}a{color:var(--accent)}pre{white-space:pre-wrap;max-height:360px;overflow:auto;background:#0d1117;border:1px solid var(--line);padding:12px;border-radius:6px;font-size:12px}.score{font-size:27px;margin:4px 0}.preview{display:block;width:100%;max-width:760px;margin:14px auto;border:1px solid var(--line);background:#fff}.hidden{display:none}@media(max-width:850px){.grid{grid-template-columns:1fr}.jobs{max-height:360px}}
<style>
.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:22px}.eyebrow{color:var(--accent);font-size:11px;letter-spacing:.12em;font-weight:700}.hero h1{margin-top:4px}.hero-actions{display:flex;gap:8px;flex-wrap:wrap}.hero-actions button{margin:0}.hero-actions button.active{background:var(--accent);border-color:var(--accent);color:#08111d}.grid{grid-template-columns:360px minmax(0,1fr)}.panel{box-shadow:0 12px 35px rgba(0,0,0,.14)}.panel-top,.workspace-heading,.section-title,.library-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.count{color:var(--muted);font-size:12px;padding-top:4px}.hint{color:var(--muted);margin-top:-5px}.notice{background:#10243a;border:1px solid #1f4f7a;border-radius:7px;padding:10px 12px;margin:0 0 14px;color:#c7e5ff}.action-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}.action-card{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px}.action-card h3{font-size:14px;margin:0 0 4px}.action-card p{color:var(--muted);font-size:12px;min-height:36px;margin:0 0 8px}.action-card button{margin:0;width:100%}.section-title{margin-top:20px}.section-title h3{margin:0;font-size:15px}.empty{padding:38px 12px;text-align:center;color:var(--muted)}.library-view{margin-top:18px}.library-toolbar{display:grid;grid-template-columns:minmax(0,1fr) 180px;gap:8px;margin:14px 0}.library-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}.resume-card{background:#0d1117;border:1px solid var(--line);border-radius:9px;padding:12px;min-width:0}.resume-card:hover{border-color:#4b6e91}.card-top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}.card-top strong{display:block}.card-top small{display:block;color:var(--muted);margin-top:3px}.thumb{display:block;width:100%;height:230px;object-fit:contain;object-position:top center;background:#fff;border:1px solid var(--line);margin:10px 0;border-radius:5px}.thumb-placeholder{height:70px;display:flex;align-items:center;justify-content:center;border:1px dashed var(--line);border-radius:5px;margin:10px 0;color:var(--muted)}.card-meta{color:var(--muted);font-size:12px;line-height:1.55}.card-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.card-actions a,.card-actions button{font-size:12px;margin:0;padding:6px 8px}.card-actions button{background:#21262d;border:1px solid var(--line);color:var(--text);border-radius:6px;cursor:pointer}.posting-snapshot{margin-top:10px}.posting-snapshot pre{max-height:230px;margin:6px 0}.legacy{color:var(--warn)}.hidden{display:none!important}.workshop-layout{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);gap:14px}.workshop-lines{max-height:calc(100vh - 220px);overflow:auto;padding-right:3px}.workshop-entry{border-top:1px solid var(--line);padding:12px 0}.workshop-entry:first-child{border-top:0;padding-top:0}.workshop-entry h4{margin:0 0 8px;font-size:14px}.workshop-line{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0}.workshop-line:focus-within{border-color:var(--accent)}.line-meta{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:11px;margin-bottom:6px}.line-text{width:100%;min-height:58px;resize:vertical;line-height:1.4;background:#111827;border:1px solid #263241;border-radius:5px;color:var(--text);padding:8px}.line-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.line-actions button{font-size:12px;margin:0;padding:6px 8px}.source-note{color:var(--muted);font-size:11px;margin:7px 0 0}.workshop-side{position:sticky;top:16px;align-self:start}.workshop-preview{width:100%;max-height:560px;object-fit:contain;object-position:top;background:#fff;border:1px solid var(--line);border-radius:5px}.chat-box textarea{width:100%;min-height:82px;resize:vertical;background:#0d1117;border:1px solid var(--line);border-radius:6px;color:var(--text);padding:9px}.chat-row{display:flex;gap:8px;margin-top:8px}.chat-row select{flex:0 0 120px}.chat-row button{margin:0;flex:1}.ai-reply{background:#10243a;border:1px solid #1f4f7a;border-radius:7px;padding:10px;margin-top:10px;white-space:pre-wrap}.suggestion{border:1px solid var(--line);border-radius:7px;padding:9px;margin:8px 0}.suggestion .text{font-size:13px}.history-list{max-height:180px;overflow:auto}.history-row{display:flex;justify-content:space-between;gap:8px;align-items:center;border-top:1px solid var(--line);padding:7px 0;font-size:12px}.history-row button{font-size:11px;margin:0;padding:4px 7px;background:#21262d;border-color:var(--line)}@media(max-width:850px){.hero{display:block}.hero-actions{margin-top:12px}.action-grid{grid-template-columns:1fr}.library-toolbar{grid-template-columns:1fr}.workshop-layout{grid-template-columns:1fr}.workshop-side{position:static}}
 .usage-strip{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 18px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#111827}.usage-strip strong{color:var(--text)}.queue-strip{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 14px;padding:10px 12px;border:1px solid #3b2e13;border-radius:8px;background:#1b1710}.queue-strip button{margin:0}.mode-tag{color:var(--accent);font-weight:600}.rationale{border-left:3px solid var(--accent);padding:10px 12px;background:#10243a;border-radius:6px;margin:10px 0}.rationale p{margin:5px 0}.rationale ul{margin:6px 0 0 18px;padding:0}.report-details{margin-top:10px}.report-details summary{cursor:pointer;color:var(--accent)}.workshop-preview-frame{width:100%;height:560px;border:1px solid var(--line);border-radius:5px;background:#fff}.preview-fallback{padding:12px;background:#111827;border-radius:6px}.action-card.featured{border-color:var(--accent);box-shadow:0 0 0 1px rgba(88,166,255,.12)}.action-card .micro{min-height:0;margin:4px 0 8px;font-size:11px;color:#b5c7d8}.button-row{display:flex;gap:8px;flex-wrap:wrap}.button-row button{margin:0}.report-meter{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0}.report-meter div{padding:8px;background:#0d1117;border:1px solid var(--line);border-radius:6px}.report-meter strong{display:block;font-size:17px}@media(max-width:850px){.report-meter{grid-template-columns:1fr}}
</style></head><body><main>
<header class="hero"><div><div class="eyebrow">PRIVATE RESUME WORKSPACE</div><h1>Resume Studio</h1><p class="sub">Create, compare, and revisit role-specific resumes without losing a run.</p></div><div class="hero-actions"><button id="tailorTab" class="active">New tailoring</button><button id="libraryTab" class="secondary">Resume bank <span id="libraryCount">0</span></button></div></header>
<div id="usageStrip" class="usage-strip"><strong>Usage</strong><span class="meta">Loading observed local Codex usage…</span></div>
<div id="queueStrip" class="queue-strip hidden"><span><strong>Tailor queue</strong> <span id="queueSummary" class="meta"></span></span><button id="queueOpen" class="secondary">Open bank</button></div>
<div id="tailorView" class="grid"><section class="panel"><div class="panel-top"><h2>Postings</h2><span id="jobCount" class="count"></span></div><p class="hint">Choose a role. Saved resumes stay in the bank when you switch.</p><input id="search" placeholder="Search company, title, sector…" autocomplete="off"><div class="toolbar"><select id="sort" aria-label="Sort roles"><option value="best">Best Radar score</option><option value="newest">Newest</option><option value="resume_match">Resume Match</option></select><button id="refreshEvidence" class="secondary" title="Refresh GitHub and Devpost evidence">Refresh evidence</button></div><div id="jobs" class="jobs">Loading roles…</div></section>
<section class="panel"><div id="empty" class="empty">Select a posting to see its match, saved resumes, and tailoring actions.</div><div id="workspace" class="hidden"><div class="workspace-heading"><div id="selected" class="selected-card"></div><button id="selectedLibrary" class="secondary">View resume bank</button></div><div class="notice">Switching postings never deletes a generated resume. Every run is saved with its posting snapshot. Queue several roles; each gets its own durable draft, posting snapshot, and editor history.</div><div id="match" class="match-card"></div><div class="button-row"><button id="analyzeMatch" class="secondary">Analyze full posting match</button><button id="showScoreReasons" class="secondary">Explain Radar score</button></div><div class="action-grid"><div class="action-card"><h3>Used bullets</h3><p>Approved wording and selections only. Your clean comparison baseline.</p><p class="micro">Lowest creative variance · still queues a complete draft</p><button id="strict">Queue used-bullets tailor</button></div><div class="action-card featured"><h3>AI tailor</h3><p>Role-specific rewrites, project swaps, ATS coverage, and a review pass.</p><p class="micro">Evidence-grounded original wording</p><button id="dream">Queue AI tailor</button></div><div class="action-card"><h3>Unrestricted AI tailor</h3><p>Freer synthesis across your CV evidence bank for a sharper, more original argument.</p><p class="micro">Still factual and layout-safe · human-review flag stays visible</p><button id="unrestricted">Queue unrestricted tailor</button></div></div><div id="scoreReasons" class="rationale hidden"></div><div id="status" class="status hidden"></div><div id="report" class="hidden"></div><div class="section-title"><h3>Saved for this posting</h3><button id="allSaved" class="secondary">See all saved resumes</button></div><div id="selectedResumes"></div></div></section></div>
<section id="libraryView" class="panel library-view hidden"><div class="library-head"><div><h2>Resume bank</h2><p class="sub">Every generated run and legacy experiment, paired with the posting it used. Nothing is replaced when you queue another tailor.</p></div><button id="backToTailor" class="secondary">Back to tailoring</button></div><div class="library-toolbar"><input id="librarySearch" placeholder="Filter saved resumes by company or role…" autocomplete="off"><select id="libraryMode" aria-label="Filter resume mode"><option value="all">All modes</option><option value="unrestricted">Unrestricted AI</option><option value="ai">AI tailor</option><option value="used">Used bullets</option></select></div><div id="libraryCards" class="library-grid"></div></section>
<section id="workshopView" class="panel library-view hidden"><div class="library-head"><div><div class="eyebrow">DRAFT WORKSHOP</div><h2 id="workshopTitle">Resume workshop</h2><p id="workshopSubtitle" class="sub">Edit one line at a time. Every save creates a new PDF revision.</p></div><div><button id="workshopBack" class="secondary">Back to bank</button><button id="workshopTailor" class="secondary">Back to posting</button></div></div><div class="notice">The original generated PDF stays untouched. Header, education, and technical skills remain the canonical base; experience, projects, and leadership lines are editable here.</div><div class="workshop-layout"><section class="panel"><div class="section-title"><h3>Editable resume lines</h3><span id="workshopLineCount" class="count"></span></div><div id="workshopLines" class="workshop-lines">Loading workshop…</div></section><aside class="workshop-side"><section class="panel"><div class="section-title"><h3>Preview</h3><span id="workshopSaveStatus" class="meta"></span></div><div id="workshopPreview"></div></section><section class="panel chat-box"><h3>Ask the writing partner</h3><p class="hint">Give it a goal or ask about a specific line. It returns candidates; you choose what to apply.</p><textarea id="workshopRequest" placeholder="e.g. Make the J&J bullets feel more like an AI platform I architected, keep the technical proof, and cut generic wording."></textarea><div class="chat-row"><select id="workshopProvider" aria-label="AI provider"></select><button id="workshopAsk">Ask for candidates</button></div><div id="workshopAiResult"></div></section><section class="panel"><div class="section-title"><h3>Revision history</h3><span class="meta">revert creates a new revision</span></div><div id="workshopHistory" class="history-list"></div></section></aside></div></section>
<script>
let selected=null,activeRunId=null,libraryEntries=[],runTimers=new Map(),jobsCache=[],workshopState=null,workshopSuggestions=[];
const $=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtDate(value){if(!value)return 'date unavailable';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString([], {dateStyle:'medium',timeStyle:'short'});}
function modeLabel(mode){return ({used:'Used bullets',strict:'Used bullets','source-only':'Used bullets',ai:'AI tailor',dream:'AI tailor',enhanced:'AI tailor',unrestricted:'Unrestricted AI tailor'})[mode]||'Tailor';}
function artifactUrl(source,id,name){return '/artifacts/'+encodeURIComponent(source)+'/'+encodeURIComponent(id)+'/'+encodeURIComponent(name);}
function runArtifact(id,name){return artifactUrl('run',id,name);}
function renderUsage(usage){if(!usage)return;$('usageStrip').innerHTML=`<strong>Usage</strong><span><strong>${Number(usage.codex_tokens||0).toLocaleString()}</strong> observed Codex tokens · ${usage.codex_calls||0} calls this week · ${usage.runs||0} saved runs</span><span class="meta">${usage.weekly_limit_tokens?`${usage.percent_of_limit}% of configured limit`:'Plus weekly allowance is not exposed by the local CLI'}</span>`;}
function renderQueue(){const active=libraryEntries.filter(entry=>entry.status==='queued'||entry.status==='running');const queued=active.filter(entry=>entry.status==='queued').length,running=active.filter(entry=>entry.status==='running').length;const strip=$('queueStrip');if(!active.length){strip.classList.add('hidden');return;}strip.classList.remove('hidden');$('queueSummary').textContent=`${queued} queued · ${running} running · ${active.length} total`;}
function savedFor(job){return libraryEntries.filter(entry=>String(entry.job?.id||'')===String(job?.id||''));}
function renderJobRows(jobs){
  $('jobCount').textContent=jobs.length+' shown';
  $('jobs').innerHTML=jobs.map(job=>{const count=savedFor(job).length;const match=job.resume_match?` · match ${job.resume_match.score}`:'';return `<button class="job ${selected&&selected.id===job.id?'selected':''}" data-id="${esc(job.id)}"><strong>${esc(job.company)} · ${esc(job.title)}</strong><small>${esc((job.locations||[]).join(', '))} · Radar ${job.score}${match}${count?` · ${count} saved`:''}</small></button>`;}).join('')||'<p class="sub">No matching roles.</p>';
  document.querySelectorAll('.job').forEach(button=>button.onclick=()=>choose(button.dataset.id));
}
async function loadJobs(){
  const q=encodeURIComponent($('search').value),sort=encodeURIComponent($('sort').value);$('jobs').innerHTML='<p class="sub">Scoring roles…</p>';
  try{const r=await fetch('/api/jobs?query='+q+'&sort='+sort);const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not load postings');jobsCache=data.jobs||[];renderJobRows(jobsCache);}catch(error){$('jobs').innerHTML='<p class="sub">'+esc(error.message)+'</p>';}
}
function renderMatch(match){if(!match){$('match').innerHTML='<span class="meta">Resume Match not analyzed yet. Full-posting analysis is optional.</span>';return;}const gaps=(match.missing_requirements||[]).join(', ')||'none detected';const reasons=(match.reasons||[]).map(reason=>`<li>${esc(reason)}</li>`).join('');$('match').innerHTML=`<strong>${match.score}/100 Resume Match</strong> <span class="badge">${esc(match.confidence||'low')} confidence</span><details class="report-details"><summary>Why this match score</summary><ul>${reasons||'<li>Evidence graph has not returned detailed reasons yet.</li>'}</ul></details><div class="meta">Gaps: ${esc(gaps)}</div>`;}
function renderCard(entry,compact=false){
  const job=entry.job||{},report=entry.artifacts.includes('report.json')?artifactUrl(entry.source,entry.entry_id,'report.json'):'';const preview=entry.urls.preview?`<img class="thumb" loading="lazy" src="${entry.urls.preview}" alt="${esc(job.company)} resume preview">`:'<div class="thumb-placeholder">Preview appears after the run finishes</div>';const pdf=entry.has_pdf?`<a href="${entry.urls.pdf}" target="_blank" rel="noreferrer">Preview PDF</a>`:'<span class="meta">PDF pending</span>';const posting=entry.has_posting_snapshot?`<button data-view-posting data-posting="${entry.urls.posting}">View posting snapshot</button>`:'<span class="meta">No saved posting text</span>';const workshop=entry.has_workshop?`<button data-open-workshop data-run="${esc(entry.run_id)}">Open workshop</button>`:'';const warning=entry.legacy?'<span class="legacy">legacy experiment</span>':'';return `<article class="resume-card"><div class="card-top"><div><strong>${esc(job.company||'Unknown company')}</strong><small>${esc(job.title||'Untitled role')}</small></div><span class="badge">${modeLabel(entry.mode)}</span></div>${compact?'':preview}<div class="card-meta">${fmtDate(entry.created_at)} · ${esc(entry.status)}${warning?' · '+warning:''}${entry.craft_score!==null&&entry.craft_score!==undefined?' · ':''}${entry.craft_score!==null&&entry.craft_score!==undefined?`Craft ${esc(entry.craft_score)}/100`:''}${job.resume_match?` · Match ${esc(job.resume_match.score)}/100`:''}</div><div class="card-actions">${pdf}${workshop}${report?`<a href="${report}" target="_blank" rel="noreferrer">Report</a>`:''}${posting}</div><div class="posting-snapshot hidden"></div></article>`;
}
function renderSelectedResumes(){
  if(!selected)return;const entries=savedFor(selected);$('selectedResumes').innerHTML=entries.length?entries.map(entry=>renderCard(entry,true)).join(''):'<p class="sub">No saved resume for this posting yet. Create one above; it will remain here and in the bank.</p>';
}
function renderLibrary(){
  const query=$('librarySearch').value.toLowerCase().trim(),mode=$('libraryMode').value;const entries=libraryEntries.filter(entry=>{const job=entry.job||{},hay=(String(job.company||'')+' '+String(job.title||'')).toLowerCase();return (!query||query.split(/\s+/).every(part=>hay.includes(part)))&&(mode==='all'||entry.mode===mode);});$('libraryCount').textContent=String(libraryEntries.length);$('libraryCards').innerHTML=entries.length?entries.map(entry=>renderCard(entry)).join(''):'<p class="sub">No saved resumes match this filter.</p>';
}
async function loadLibrary(){try{const r=await fetch('/api/library?limit=500');const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not load resume bank');libraryEntries=data.resumes||[];renderQueue();renderLibrary();renderSelectedResumes();renderJobRows(jobsCache);}catch(error){$('libraryCards').innerHTML='<p class="sub">'+esc(error.message)+'</p>';}}
async function loadUsage(){try{const r=await fetch('/api/usage');const data=await r.json();if(r.ok)renderUsage(data);}catch(error){$('usageStrip').innerHTML='<strong>Usage</strong><span class="meta">Usage ledger unavailable: '+esc(error.message)+'</span>';}}
function workshopLineElement(lineId){return Array.from(document.querySelectorAll('[data-line-id]')).find(node=>node.dataset.lineId===lineId);}
function plainLine(value){return String(value??'').replace(/\\textbf\{([^{}]*)\}/g,'$1').replace(/\\emph\{([^{}]*)\}/g,'$1').replace(/\\(?:large|normalsize|small|scshape)\s*/g,'').replace(/\\(?:%|&|\$)/g,m=>m==='\\%'?'%':m==='\\&'?'&':'$').replace(/\\times\{\}/g,'x');}
function renderWorkshop(){
  if(!workshopState)return;const job=workshopState.job||{};$('workshopTitle').textContent=(job.company||'Resume')+' · workshop';$('workshopSubtitle').textContent=(job.title||'Tailored draft')+' · edit wording, ask for alternatives, and keep every revision';
  const lines=workshopState.lines||[];$('workshopLineCount').textContent=lines.length+' editable lines';const groups=[];lines.forEach(line=>{let group=groups.find(item=>item.entry_id===line.entry_id);if(!group){group={entry_id:line.entry_id,label:line.entry_label,role:line.role,section:line.section,lines:[]};groups.push(group);}group.lines.push(line);});
  $('workshopLines').innerHTML=groups.map(group=>`<div class="workshop-entry"><h4>${esc(group.label)}${group.role?' · '+esc(group.role):''}</h4>${group.lines.map(line=>`<div class="workshop-line"><div class="line-meta"><span>${esc(line.section)} · ${esc(line.line_id)}</span><span>${line.source_ids?.length>1?'synthesized from '+line.source_ids.length+' sources':'source-grounded'}</span></div><textarea class="line-text" data-line-id="${esc(line.line_id)}">${esc(plainLine(line.text))}</textarea><div class="line-actions"><button data-save-line="${esc(line.line_id)}">Save line</button><button class="secondary" data-ask-line="${esc(line.line_id)}">Ask AI about this</button></div>${line.source_text&&line.source_text!==line.text?`<p class="source-note">Base source: ${esc(plainLine(line.source_text))}</p>`:''}</div>`).join('')}</div>`).join('')||'<p class="sub">No editable lines in this draft.</p>';
  const providers=workshopState.providers||{};const choices=Object.keys(providers).filter(name=>providers[name]);$('workshopProvider').innerHTML=choices.length?choices.map(name=>`<option value="${esc(name)}">${esc(name==='codex'?'Codex CLI':name==='claude'?'Claude CLI':name)}</option>`).join(''):'<option value="">No local AI CLI</option>';$('workshopAsk').disabled=!choices.length;
  const render=workshopState.last_render||{};$('workshopSaveStatus').textContent=render.revision_id?'revision '+render.revision_id:'original generated draft';const previewUrl=render.preview_url||workshopState.original_preview_url;const pdfUrl=render.pdf_url||workshopState.original_pdf_url;$('workshopPreview').innerHTML=previewUrl?`<img class="workshop-preview" src="${previewUrl}" alt="Rendered resume preview"><iframe class="workshop-preview-frame" src="${pdfUrl}" title="Rendered resume PDF"></iframe><p><a href="${pdfUrl}" target="_blank" rel="noreferrer">Open ${render.revision_id?'workshop':'original'} PDF</a></p>`:pdfUrl?`<div class="preview-fallback"><strong>PDF ready</strong><p class="meta">The browser could not create a thumbnail, but the document is still available.</p><a href="${pdfUrl}" target="_blank" rel="noreferrer">Open resume PDF</a></div>`:'<p class="sub">Preview is not available yet.</p>';
  $('workshopHistory').innerHTML=(workshopState.revisions||[]).map(revision=>`<div class="history-row"><span>${esc(revision.label||revision.kind||'revision')}<br><small>${fmtDate(revision.created_at)}${revision.provider?' · '+esc(revision.provider):''}</small></span><button data-revert-revision="${esc(revision.revision_id)}">Revert</button></div>`).join('')||'<p class="meta">Your first save will appear here.</p>';
  document.querySelectorAll('[data-save-line]').forEach(button=>button.onclick=()=>saveWorkshopLine(button.dataset.saveLine));document.querySelectorAll('[data-ask-line]').forEach(button=>button.onclick=()=>askWorkshop(button.dataset.askLine));document.querySelectorAll('[data-revert-revision]').forEach(button=>button.onclick=()=>revertWorkshop(button.dataset.revertRevision));
}
async function openWorkshop(runId){try{const r=await fetch('/api/workshop?id='+encodeURIComponent(runId));const data=await r.json();if(!r.ok)throw new Error(data.error||'Workshop unavailable');workshopState=data;activeRunId=runId;showView('workshop');renderWorkshop();}catch(error){alert(error.message);}}
async function saveWorkshopLine(lineId){const field=workshopLineElement(lineId);if(!field)return;const button=document.querySelector(`[data-save-line="${CSS.escape(lineId)}"]`);if(button)button.disabled=true;const current=(workshopState.lines||[]).find(line=>line.line_id===lineId);const displayedOriginal=plainLine(current?.text||'').trim();const edited=field.value.trim();const payloadText=edited===displayedOriginal?(current?.text||edited):edited;try{const r=await fetch('/api/workshop/edit',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({run_id:workshopState.run_id,line_id:lineId,text:payloadText,origin:'manual',label:'Manual line edit'})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not save line');workshopState=data;renderWorkshop();}catch(error){alert(error.message);}finally{if(button)button.disabled=false;}}
function renderWorkshopAiResult(data){workshopSuggestions=data.suggestions||[];let html=data.reply?`<div class="ai-reply">${esc(data.reply)}</div>`:'';if(data.warnings?.length)html+=`<p class="meta">${esc(data.warnings.join(' · '))}</p>`;html+=workshopSuggestions.map((suggestion,index)=>`<div class="suggestion"><div class="text">${esc(plainLine(suggestion.text))}</div><p class="source-note">${esc(suggestion.rationale||'Authorized-source candidate')}</p><button data-apply-suggestion="${index}">Apply this candidate</button></div>`).join('');$('workshopAiResult').innerHTML=html||'<p class="meta">No candidate came back. Try a more specific request.</p>';document.querySelectorAll('[data-apply-suggestion]').forEach(button=>button.onclick=()=>applyWorkshopSuggestion(Number(button.dataset.applySuggestion)));}
async function askWorkshop(lineId=''){const request=$('workshopRequest').value.trim()||'Rewrite this line with stronger technical substance, clearer ownership, and a concrete consequence while staying concise and factual.';const button=$('workshopAsk');button.disabled=true;$('workshopAiResult').innerHTML='<p class="meta">Asking the local '+$('workshopProvider').value+' lane…</p>';try{const r=await fetch('/api/workshop/ai',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({run_id:workshopState.run_id,line_id:lineId,request,provider:$('workshopProvider').value})});const data=await r.json();if(!r.ok)throw new Error(data.error||'AI workshop call failed');renderWorkshopAiResult(data);}catch(error){$('workshopAiResult').innerHTML='<p class="meta">'+esc(error.message)+'</p>';}finally{button.disabled=false;}}
async function applyWorkshopSuggestion(index){const suggestion=workshopSuggestions[index];if(!suggestion)return;try{const r=await fetch('/api/workshop/edit',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({run_id:workshopState.run_id,line_id:suggestion.line_id,text:suggestion.text,origin:'ai',label:'Applied AI candidate',provider:$('workshopProvider').value,instruction:$('workshopRequest').value})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not apply candidate');workshopState=data;$('workshopAiResult').innerHTML='<p class="meta">Applied and rendered a new revision.</p>';renderWorkshop();}catch(error){$('workshopAiResult').innerHTML='<p class="meta">'+esc(error.message)+'</p>';}}
async function revertWorkshop(revisionId){if(!confirm('Create a new revision from this older version?'))return;try{const r=await fetch('/api/workshop/revert',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({run_id:workshopState.run_id,revision_id:revisionId})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not revert');workshopState=data;renderWorkshop();}catch(error){alert(error.message);}}
async function choose(id){
  const r=await fetch('/api/job?id='+encodeURIComponent(id));const data=await r.json();if(!r.ok){$('match').textContent=data.error||'Posting could not be loaded';return;}selected=data;$('empty').classList.add('hidden');$('workspace').classList.remove('hidden');$('selected').innerHTML=`<strong>${esc(selected.company)} · ${esc(selected.title)}</strong><div class="meta">${esc((selected.locations||[]).join(', '))} · Radar ${selected.score} · <a href="${esc(selected.url)}" target="_blank" rel="noreferrer">open live posting</a></div><div>${(selected.alert_ok?'<span class="badge">alert eligible</span>':'<span class="badge">dashboard role</span>')} ${(selected.early_career_possible?'<span class="badge">early-career possible</span>':'')}</div>`;renderMatch(selected.resume_match);document.querySelectorAll('.job').forEach(b=>b.classList.toggle('selected',b.dataset.id===id));renderSelectedResumes();showView('tailor');}
async function analyzeMatch(){if(!selected)return;const button=$('analyzeMatch');button.disabled=true;$('match').innerHTML='<span class="meta">Fetching the posting and matching the full evidence graph…</span>';try{const r=await fetch('/api/match',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:selected.id})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Match analysis failed');selected.resume_match=data.resume_match;renderMatch(data.resume_match);}catch(error){$('match').textContent=error.message;}finally{button.disabled=false;}}
async function refreshEvidence(){const button=$('refreshEvidence');button.disabled=true;button.textContent='Refreshing…';try{const r=await fetch('/api/evidence/refresh',{method:'POST',headers:{'content-type':'application/json'},body:'{}'});const data=await r.json();if(!r.ok)throw new Error(data.error||'Evidence refresh failed');await loadJobs();if(selected)await choose(selected.id);}catch(error){alert(error.message);}finally{button.disabled=false;button.textContent='Refresh evidence';}}
function setTailorButtons(disabled){['strict','dream','unrestricted'].forEach(id=>$(id).disabled=disabled);}
async function start(mode){if(!selected)return;const buttons=['strict','dream','unrestricted'];buttons.forEach(id=>$(id).disabled=true);$('status').className='status running';$('status').textContent='Queueing '+modeLabel(mode)+' for '+selected.company+'…';$('status').classList.remove('hidden');$('report').classList.add('hidden');try{const r=await fetch('/api/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:selected.id,mode})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not queue run');activeRunId=data.run_id;$('status').textContent=selected.company+': queued · run '+data.run_id;await loadLibrary();watchRun(data.run_id);}catch(error){$('status').className='status failed';$('status').textContent=error.message;}finally{buttons.forEach(id=>$(id).disabled=false);}}
function watchRun(id){if(runTimers.has(id))return;const tick=async()=>{try{const r=await fetch('/api/run?id='+encodeURIComponent(id));const data=await r.json();if(!r.ok)throw new Error(data.error||'Run status unavailable');if(id===activeRunId){$('status').textContent=(data.job?.company?data.job.company+': ':'')+(data.message||data.status);$('status').className='status '+data.status;}if(data.status==='complete'||data.status==='failed'){runTimers.delete(id);if(id===activeRunId){setTailorButtons(false);if(data.status==='complete')renderReport(data);else $('report').classList.add('hidden');}await loadLibrary();return;}const timer=setTimeout(()=>{runTimers.delete(id);tick();},1500);runTimers.set(id,timer);}catch(error){runTimers.delete(id);if(id===activeRunId){$('status').className='status failed';$('status').textContent=error.message;setTailorButtons(false);}}};tick();}
function renderReport(status){const report=status.report||{};const review=report.review||{},gates=review.gates||{},job=status.job||report.job||{},pdfName=status.pdf_filename||report.pdf_filename||'resume.pdf',previewName=status.preview_filename||report.preview_filename||'';$('report').classList.remove('hidden');let html=`<div class="section-title"><h3>Saved result</h3><span class="badge">${modeLabel(status.mode||report.mode)}</span></div><p class="meta">${esc(job.company||'')} · ${esc(job.title||'')} · ${fmtDate(status.updated_at||status.created_at)}</p>`;if(review.craft_score!==undefined)html+=`<div class="score">${review.craft_score}/100 craft</div><div>${review.ready?'Ready for human review':'Needs revision or fact verification'}</div><p>${Object.entries(gates).map(([name,gate])=>`<span class="badge">${esc(name)}: ${esc(gate.status)}</span>`).join(' ')}</p>`;if(report.resume_match)html+=`<p><strong>Resume Match:</strong> ${report.resume_match.score}/100 <span class="badge">${esc(report.resume_match.confidence)}</span></p>`;if(report.positioning_thesis)html+=`<p><strong>Thesis:</strong> ${esc(report.positioning_thesis)}</p>`;if(status.mode==='ai'||status.mode==='unrestricted'||report.mode==='enhanced'||report.mode==='unrestricted')html+=`<p class="meta">AI tailoring may synthesize authorized source lines; unrestricted drafts are intentionally more original. Edit, compare, or revert in the workshop.</p>`;if(report.format_contract)html+=`<p class="meta"><strong>Format:</strong> CV/resume.tex locked · 0% font-size change · company first</p>`;const layout=review.deterministic?.layout||{};if(layout.horizontal)html+=`<p class="meta"><strong>Space QA:</strong> ${layout.horizontal.measured||0} bullets measured · ${layout.horizontal.wrap_count||0} wraps · ${layout.horizontal.near_wrap_count||0} near-wraps · ${layout.horizontal.underfilled_line_count||0} roomy lines · one-more-bullet ${layout.vertical_capacity?.pass?'overflows':'still fits'}</p>`;if(report.usage)html+=`<p class="meta"><strong>Codex usage:</strong> ${Number(report.usage.codex_tokens||0).toLocaleString()} tokens across ${report.usage.codex_calls||0} calls${report.usage.complete?'':' (some call totals unavailable)'}</p>`;html+=`<p><a href="${runArtifact(status.run_id,pdfName)}" target="_blank" rel="noreferrer">Preview PDF</a> · <button class="secondary" data-open-workshop="${esc(status.run_id)}">Open workshop</button> · <a href="/api/posting?source=run&id=${encodeURIComponent(status.run_id)}" target="_blank" rel="noreferrer">Posting snapshot</a> · <a href="${runArtifact(status.run_id,'content_plan.json')}" target="_blank" rel="noreferrer">Source plan</a> · <a href="${runArtifact(status.run_id,'report.json')}" target="_blank" rel="noreferrer">Full report</a></p>`;if(previewName)html+=`<img class="preview" src="${runArtifact(status.run_id,previewName)}" alt="Rendered resume preview">`;html+='<pre>'+esc(JSON.stringify(review,null,2))+'</pre>';$('report').innerHTML=html;document.querySelectorAll('[data-open-workshop]').forEach(button=>button.onclick=()=>openWorkshop(button.dataset.openWorkshop||status.run_id));}
function showView(view){const bank=view==='library',workshop=view==='workshop';$('tailorView').classList.toggle('hidden',bank||workshop);$('libraryView').classList.toggle('hidden',!bank);$('workshopView').classList.toggle('hidden',!workshop);$('tailorTab').classList.toggle('active',!bank&&!workshop);$('libraryTab').classList.toggle('active',bank);if(bank)renderLibrary();if(workshop)renderWorkshop();}
document.addEventListener('click',async event=>{const open=event.target.closest('[data-open-workshop]');if(open){event.preventDefault();return openWorkshop(open.dataset.openWorkshop||open.dataset.run);}const button=event.target.closest('[data-view-posting]');if(!button)return;const card=button.closest('.resume-card'),panel=card.querySelector('.posting-snapshot');if(!panel.classList.contains('hidden')){panel.classList.add('hidden');button.textContent='View posting snapshot';return;}button.disabled=true;try{const r=await fetch(button.dataset.posting),data=await r.json();if(!r.ok)throw new Error(data.error||'Posting snapshot unavailable');panel.innerHTML=`<strong>Saved posting snapshot</strong><pre>${esc(data.posting_text||'Only posting metadata was available for this run.')}</pre>`;panel.classList.remove('hidden');button.textContent='Hide posting snapshot';}catch(error){panel.textContent=error.message;panel.classList.remove('hidden');}finally{button.disabled=false;}});
function explainRadarReason(reason){const text=String(reason||'');if(text.startsWith('raw utility'))return 'Calibration: '+text;const labels=[['base utility','Baseline role utility'],['role:','Role family fit'],['sector:','Sector fit'],['new-grad/early-career priority','Verified early-career signal'],['early-career possible','Plausible first-role signal'],['new-grad evidence absent','No explicit early-career evidence'],['company tier','Company quality'],['explicit goal company','Personal goal-company preference'],['company concentration','Company diversity adjustment'],['compensation','Compensation'],['posted','Freshness'],['remote','Remote access'],['Resume Match','Resume Match']];const label=(labels.find(item=>text.startsWith(item[0]))||[])[1]||'Scoring input';return label+': '+text;}
$('search').oninput=()=>{clearTimeout(window.searchTimer);window.searchTimer=setTimeout(loadJobs,250)};$('sort').onchange=loadJobs;$('librarySearch').oninput=renderLibrary;$('libraryMode').onchange=renderLibrary;$('analyzeMatch').onclick=analyzeMatch;$('refreshEvidence').onclick=refreshEvidence;$('strict').onclick=()=>start('used');$('dream').onclick=()=>start('ai');$('unrestricted').onclick=()=>start('unrestricted');$('showScoreReasons').onclick=()=>{if(!selected)return;const reasons=selected.score_reasons||[];$('scoreReasons').classList.remove('hidden');$('scoreReasons').innerHTML='<strong>Why Radar gave this role '+esc(selected.score)+'/100</strong><p>Radar is deterministic job fit. Resume Match is a separate CV/evidence alignment score. 90+ is strong; the company-diversity adjustment only nudges weaker duplicates.</p><ul>'+reasons.map(reason=>'<li>'+esc(explainRadarReason(reason))+'</li>').join('')+'</ul>';};$('queueOpen').onclick=()=>showView('library');$('tailorTab').onclick=()=>showView('tailor');$('libraryTab').onclick=()=>showView('library');$('selectedLibrary').onclick=()=>showView('library');$('allSaved').onclick=()=>showView('library');$('backToTailor').onclick=()=>showView('tailor');$('workshopBack').onclick=()=>showView('library');$('workshopTailor').onclick=()=>showView('tailor');$('workshopAsk').onclick=()=>askWorkshop('');Promise.all([loadJobs(),loadLibrary(),loadUsage()]);
</script></main></body></html>"""


UI_HTML = UI_HTML.replace(
    "function showView(view){",
    """const baseRenderReport=renderReport;
renderReport=function(status){
  baseRenderReport(status);
  const report=status.report||{},changes=report.content_changes||{},swaps=changes.project_swaps||{},ats=changes.keyword_coverage||{};
  if(!report.content_changes)return;
  let extra=`<div class=\"match-card\"><strong>What changed</strong><div class=\"meta\">${changes.changed_bullet_count||0} bullets rewritten · ${swaps.swapped_in?.length||0} projects swapped in · ${swaps.swapped_out?.length||0} base projects swapped out</div>`;
  if(swaps.swapped_in?.length)extra+=`<div class=\"meta\"><strong>Added:</strong> ${esc(swaps.swapped_in.join(' · '))}</div>`;
  if(swaps.swapped_out?.length)extra+=`<div class=\"meta\"><strong>Removed:</strong> ${esc(swaps.swapped_out.join(' · '))}</div>`;
  const rewrites=changes.rewritten_bullets||[];
  if(rewrites.length)extra+=`<details><summary>Show ${rewrites.length} rewritten lines</summary>${rewrites.map(item=>`<div class=\"meta\"><strong>${esc(item.source_id||'line')}</strong><br><s>${esc(item.source_text||'')}</s><br>→ ${esc(item.final_text||'')}</div>`).join('')}</details>`;
  extra+='</div>';
  const plan=report.content_plan||{},selections=[...(plan.experiences||[]),...(plan.projects||[]),...(plan.leadership||[])];
  const reasons=selections.map(item=>item.why).filter(Boolean).slice(0,8);
  let rationale=`<div class=\"rationale\"><strong>Why this draft was chosen</strong>`;
  if(report.positioning_thesis)rationale+=`<p>${esc(report.positioning_thesis)}</p>`;
  if(reasons.length)rationale+=`<details class=\"report-details\"><summary>Show selection reasoning</summary><ul>${reasons.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul></details>`;
  if((report.revision_notes||[]).length)rationale+=`<details class=\"report-details\"><summary>Show editorial notes</summary><ul>${report.revision_notes.slice(0,8).map(note=>`<li>${esc(note)}</li>`).join('')}</ul></details>`;
  rationale+='</div>';extra+=rationale;
  if(ats.posting_available){
    const missing=(ats.terms||[]).filter(item=>item.supported&&!item.rendered).map(item=>item.term);
    const unsupported=(ats.terms||[]).filter(item=>!item.supported).map(item=>item.term);
    extra+=`<p><strong>ATS terms:</strong> ${ats.covered_count||0}/${ats.supported_count||0} supported exact terms rendered (${ats.exact_coverage_percent||0}%)</p>`;
    if(missing.length)extra+=`<p class=\"meta\"><strong>Supported but missing:</strong> ${esc(missing.join(', '))}</p>`;
    if(unsupported.length)extra+=`<p class=\"meta\"><strong>Not supported by your evidence:</strong> ${esc(unsupported.join(', '))}</p>`;
  }
  else if(ats.reason)extra+=`<p class=\"meta\"><strong>ATS:</strong> ${esc(ats.reason)}</p>`;
  const anchor=$(\'report\').querySelector(\'pre\');if(anchor)anchor.insertAdjacentHTML(\'beforebegin\',extra);else $(\'report\').insertAdjacentHTML(\'beforeend\',extra);
};
function showView(view){""",
)

UI_HTML = UI_HTML.replace(
    "${layout.horizontal.underfilled_line_count||0} underfilled lines · one-more-bullet",
    "${layout.horizontal.near_wrap_count||0} near-wraps · ${layout.horizontal.underfilled_line_count||0} roomy lines · one-more-bullet",
)
UI_HTML = UI_HTML.replace(
    "</head>",
    "<style>.workshop-preview{display:none!important}</style></head>",
)
UI_HTML = UI_HTML.replace(
    "The original generated PDF stays untouched. Header, education, and technical skills remain the canonical base; experience, projects, and leadership lines are editable here.",
    "The original generated PDF stays untouched. The template shell remains canonical, but every visible resume line—education, skills, experience, projects, and leadership—is editable here.",
)


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

    def send_bytes(
        self, raw: bytes, content_type: str, status: int = HTTPStatus.OK,
        download_name: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if download_name:
            self.send_header(
                "Content-Disposition",
                'inline; filename="%s"' % _download_filename(download_name),
            )
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
        if parsed.path == "/api/library":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", [200])[0] or 200)
            except (TypeError, ValueError):
                limit = 200
            return self.send_json({
                "resumes": resume_library(
                    repo_root(),
                    query=params.get("query", [""])[0],
                    job_id=params.get("job_id", [""])[0],
                    limit=limit,
                ),
            })
        if parsed.path == "/api/usage":
            return self.send_json(studio_usage(repo_root()))
        if parsed.path == "/api/posting":
            params = parse_qs(parsed.query)
            source = params.get("source", [""])[0]
            entry_id = params.get("id", [""])[0]
            snapshot = posting_snapshot(repo_root(), source, entry_id)
            if snapshot is None:
                return self.send_json({"error": "posting snapshot not found"}, HTTPStatus.NOT_FOUND)
            return self.send_json(snapshot)
        if parsed.path == "/api/run":
            run_id = parse_qs(parsed.query).get("id", [""])[0]
            status = self.manager.get(run_id)
            if not status:
                return self.send_json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return self.send_json(status)
        if parsed.path == "/api/workshop":
            run_id = parse_qs(parsed.query).get("id", [""])[0]
            run_dir = _workshop_run_dir(repo_root(), run_id)
            if run_dir is None:
                return self.send_json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            try:
                return self.send_json(_workshop_view(run_dir))
            except RuntimeError as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        if parsed.path.startswith("/runs/"):
            parts = parsed.path.split("/")
            if len(parts) != 4 or not re.fullmatch(r"[a-f0-9]{12}", parts[2]):
                return self.send_json({"error": "invalid artifact path"}, HTTPStatus.BAD_REQUEST)
            run_dir = studio_root(repo_root()) / "runs" / parts[2]
            target = artifact_target(run_dir, parts[3])
            if target is None:
                return self.send_json({"error": "artifact not found"}, HTTPStatus.NOT_FOUND)
            content_type = "application/pdf" if target.suffix == ".pdf" else "image/png" if target.suffix == ".png" else "application/json" if target.suffix == ".json" else "text/plain; charset=utf-8"
            return self.send_bytes(
                target.read_bytes(), content_type,
                download_name=Path(parts[3]).name if target.suffix == ".pdf" else "",
            )
        if parsed.path.startswith("/workshop/"):
            parts = parsed.path.split("/")
            if len(parts) != 5 or not re.fullmatch(r"[a-f0-9]{12}", parts[2]) or not re.fullmatch(r"[a-f0-9]{10}", parts[3]):
                return self.send_json({"error": "invalid workshop artifact path"}, HTTPStatus.BAD_REQUEST)
            if Path(parts[4]).name != parts[4]:
                return self.send_json({"error": "invalid workshop artifact path"}, HTTPStatus.BAD_REQUEST)
            target_dir = studio_root(repo_root()) / "runs" / parts[2] / "workshop" / "revisions" / parts[3]
            target = (target_dir / parts[4]).resolve()
            if target_dir.resolve() not in target.parents or not target.is_file():
                return self.send_json({"error": "workshop artifact not found"}, HTTPStatus.NOT_FOUND)
            content_type = "application/pdf" if target.suffix == ".pdf" else "image/png" if target.suffix == ".png" else "application/json" if target.suffix == ".json" else "text/plain; charset=utf-8"
            return self.send_bytes(
                target.read_bytes(), content_type,
                download_name=Path(parts[4]).name if target.suffix == ".pdf" else "",
            )
        if parsed.path.startswith("/artifacts/"):
            parts = parsed.path.split("/")
            if len(parts) != 5:
                return self.send_json({"error": "invalid artifact path"}, HTTPStatus.BAD_REQUEST)
            source, entry_id, filename = parts[2], parts[3], parts[4]
            directory = _library_dir(repo_root(), source, entry_id)
            if directory is None or Path(filename).name != filename:
                return self.send_json({"error": "invalid artifact path"}, HTTPStatus.BAD_REQUEST)
            target = artifact_target(directory, filename)
            if target is None:
                return self.send_json({"error": "artifact not found"}, HTTPStatus.NOT_FOUND)
            content_type = "application/pdf" if target.suffix == ".pdf" else "image/png" if target.suffix == ".png" else "application/json" if target.suffix == ".json" else "text/plain; charset=utf-8"
            return self.send_bytes(
                target.read_bytes(), content_type,
                download_name=Path(filename).name if target.suffix == ".pdf" else "",
            )
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {
            "/api/run", "/api/match", "/api/evidence/refresh",
            "/api/workshop/edit", "/api/workshop/ai", "/api/workshop/revert",
        }:
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
            if parsed.path == "/api/workshop/edit":
                result = workshop_apply_edit(
                    repo_root(), str(body.get("run_id") or ""),
                    str(body.get("line_id") or ""), str(body.get("text") or ""),
                    origin=str(body.get("origin") or "manual"),
                    label=str(body.get("label") or "Line edit"),
                    provider=str(body.get("provider") or ""),
                    instruction=str(body.get("instruction") or ""),
                )
                return self.send_json(result)
            if parsed.path == "/api/workshop/ai":
                result = workshop_ai(
                    repo_root(), str(body.get("run_id") or ""),
                    str(body.get("request") or ""),
                    line_id=str(body.get("line_id") or ""),
                    provider=str(body.get("provider") or ""),
                )
                return self.send_json(result)
            if parsed.path == "/api/workshop/revert":
                result = workshop_revert(
                    repo_root(), str(body.get("run_id") or ""),
                    str(body.get("revision_id") or ""),
                )
                return self.send_json(result)
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
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
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
