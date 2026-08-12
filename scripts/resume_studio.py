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
  and separate critique lanes. Codex may apply critique in bounded revision
  rounds; the reviewer never mutates or self-grades the plan, and the module
  reports separate quality gates instead of a composite craft score.

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
from difflib import SequenceMatcher
import html
import itertools
import json
import os
import re
import shutil
import signal
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
from radar.evidence_review import (BLOCKING_STATUSES, REVIEW_STATUSES,
                                   load_reviews, review_path, review_summary)


RUBRIC_VERSION = "resume-gates-v1"
CODEX_LUNA_MODEL = "gpt-5.6-luna"
REVIEW_CRITERIA = (
    "factual", "target_fit", "evidence", "distinctiveness", "clarity", "privacy",
)
STATUS_MULTIPLIER = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
MAX_POSTING_CHARS = 12000
MAX_PROMPT_CHARS = 42000
RUN_TIMEOUT_SECONDS = 12 * 60
CANONICAL_TEMPLATE = "immutable/VictorJimenezResume.tex"
CANONICAL_PDF = "immutable/VictorJimenezResume.pdf"
CANONICAL_PAGE_FOOTER = r"\fancyfoot[C]{\footnotesize\thepage}"
GENERATED_ONE_PAGE_FOOTER = r"\fancyfoot[C]{}"
CANONICAL_RESUME_FILES = (
    "immutable/VictorJimenezResume.tex",
    "immutable/VictorJimenezResume.pdf",
    "immutable/og_resume.tex",
    "immutable/og_resume.pdf",
    "immutable/tldp_resume.tex",
    "immutable/tldp_resume.pdf",
)
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
MAX_SPACE_EXPANSION_CANDIDATES = 4
MIN_MEANINGFUL_BULLET_CHARS = 48
# A rewrite this close to its authorized source is normally presentation
# churn, not a hiring-value improvement.  Keep the source wording unless the
# candidate adds a meaningful technical/role signal or changes a concrete
# proof point.
LOW_VALUE_REWRITE_SIMILARITY = 0.82
LOW_VALUE_REWRITE_OVERLAP = 0.78
GENERIC_REWRITE_TOKENS = {
    "a", "an", "and", "across", "built", "build", "created", "create",
    "developed", "develop", "designed", "design", "established", "establish",
    "for", "from", "in", "into", "led", "lead", "made", "make", "on",
    "the", "to", "used", "using", "via", "with",
}
# These are provider-output safety limits, not portfolio requirements.  The
# page packer decides how much evidence the target can honestly carry.
MAX_CANDIDATE_BULLETS = 60
MAX_RENDERED_BULLETS = 40
MIN_TOTAL_BULLETS = 0
MAX_TOTAL_BULLETS = MAX_RENDERED_BULLETS
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
    # Provider safety ceilings.  None of these are required for a valid plan.
    "experiences": {"entries": 8, "bullets": 10},
    "projects": {"entries": 12, "bullets": 8},
    "leadership": {"entries": 4, "bullets": 4},
}
EXPERIENCE_BULLET_CAPS = (10, 10, 10, 10, 10, 10, 10, 10)
PORTFOLIO_FLOORS = {
    "experiences": {"entries": 0, "bullets": 0},
    "projects": {"entries": 0, "bullets": 0},
    "leadership": {"entries": 0, "bullets": 0},
}
PORTFOLIO_SIGNAL_PATTERNS = {
    "ai_orchestration": r"\b(?:agentic|agents?|gemini|adk|llm|large language)\b",
    "retrieval_search": r"\b(?:rag|lightrag|retrieval|vector search|knowledge[- ]graph|pgvector)\b",
    "backend_api": r"\b(?:fastapi|flask|rest endpoints?|apis?|web app|backend|websocket)\b",
    "cloud_data": r"\b(?:google cloud|alloydb|sql|pandas|sqlite|database|schema|data pipeline|firebase|cache)\b",
    "ml_modeling": r"\b(?:machine learning|deep learning|pytorch|scikit|xgboost|model training|classification|clustering|linear[- ]algebra)\b",
    "computer_vision_multimodal": r"\b(?:computer vision|emotions?|facial|gaze|multimodal|transcript|audio)\b",
    "security_access": r"\b(?:jwt|secure|security|encrypted|tenseal|ckks|role[- ]based|access control|authentication)\b",
    "algorithms_validation": r"\b(?:algorithm|roc[- ]auc|recall|validation|resampling|brownian|qubit|quantum|stratified)\b",
    "systems_reliability": r"\b(?:resilient|dropout|real[- ]time|streaming|ingestion|hpc|slurm|bash|gpu|hardware)\b",
    "external_validation": r"\b(?:won|selected|hackmit|hackhers|hacknjit|hackru|hacknyu|1st place|fellowship|competitors?)\b",
    "people_leadership": r"\b(?:led|leadership|students|team|interviews?|community|stakeholders?)\b",
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


def canonical_resume_lock(root: Optional[Path] = None) -> Dict[str, Any]:
    """Describe the immutable files that Resume Studio is never allowed to write."""
    cv = cv_root(root).resolve()
    return {
        "locked": True,
        "message": (
            "VictorJimenezResume and historical CV references are filesystem-locked. "
            "New tailoring runs and workshop revisions are private copies."
        ),
        "unlock": {
            "required": True,
            "command": ".venv/bin/python scripts/resume_lock.py unlock",
            "note": "Unlock requires the owner PIN at an interactive prompt; lock again after deliberate edits.",
        },
        "files": [
            {
                "name": "CV/%s" % name,
                "kind": "source" if name.endswith(".tex") else "reference PDF",
                "exists": (cv / name).is_file(),
            }
            for name in CANONICAL_RESUME_FILES
        ],
    }


def assert_resume_workspace(run_dir: Path, root: Optional[Path] = None) -> None:
    """Reject render targets inside the canonical CV directory.

    The Studio's only writable CV area is ``CV/.resume_studio``.  Keeping this
    check at the rendering boundary protects against a future caller passing
    ``CV/`` (or another canonical subdirectory) as a compile target.
    Temporary test directories and other non-CV workspaces remain valid.
    """
    candidate = Path(run_dir).resolve()
    cv = cv_root(root or repo_root()).resolve()
    private = studio_root(root or repo_root()).resolve()
    inside_cv = candidate == cv or cv in candidate.parents
    inside_private = candidate == private or private in candidate.parents
    if inside_cv and not inside_private:
        raise RuntimeError(
            "Resume Studio cannot render inside CV/; canonical resume files are locked. "
            "Use a private CV/.resume_studio run directory."
        )


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


def evidence_review_view(root: Optional[Path] = None, limit: int = 120) -> Dict[str, Any]:
    """Return a compact, reviewable claim queue for the private UI."""
    graph = evidence_graph(root)
    nodes = []
    for node in graph.get("nodes", []):
        text = str(node.get("text") or "").strip()
        if not text:
            continue
        nodes.append({
            key: node.get(key)
            for key in (
                "id", "source", "heading", "text", "authority", "claim_allowed",
                "source_kind", "review_status", "review_note", "blocked_reason",
            )
        })
    nodes.sort(key=lambda node: (
        node.get("review_status") != "unreviewed",
        not bool(node.get("claim_allowed")),
        -int(node.get("authority") or 0),
        str(node.get("source") or ""),
    ))
    return {
        "version": graph.get("version"),
        "hash": graph.get("hash"),
        "summary": graph.get("review_summary") or review_summary(graph),
        "claims": nodes[: max(1, min(int(limit or 120), 500))],
        "review_file": str(review_path(studio_root(root or repo_root()))),
    }


def update_evidence_review(
    node_id: str,
    status: str,
    note: str = "",
    claim_allowed: bool = False,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist one user review and invalidate the in-process graph cache."""
    status = str(status or "").strip().lower()
    if status not in REVIEW_STATUSES:
        raise ValueError("invalid evidence review status")
    node_id = str(node_id or "").strip()
    if not node_id:
        raise ValueError("evidence node id is required")
    graph = evidence_graph(root)
    if not any(str(node.get("id") or "") == node_id for node in graph.get("nodes", [])):
        raise ValueError("evidence node not found")
    note = str(note or "").strip()
    if len(note) > 2000:
        raise ValueError("evidence review note is too long")
    reviews = load_reviews(studio_root(root or repo_root()))
    claims = reviews.setdefault("claims", {})
    claims[node_id] = {
        "status": status,
        "note": note,
        "reviewed_by": "Victor",
        "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if status in {"confirmed", "public_safe"} and claim_allowed:
        claims[node_id]["claim_allowed"] = True
    elif status not in {"confirmed", "public_safe"}:
        # A stale true flag must not survive a downgrade to disputed,
        # rejected, superseded, or private-only evidence.
        claims[node_id].pop("claim_allowed", None)
    write_json(review_path(studio_root(root or repo_root())), reviews)
    key = str((root or repo_root()).resolve())
    _EVIDENCE_GRAPH_CACHE.pop(key, None)
    return evidence_review_view(root)


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

    Every new PDF includes a company slug and a use-case suffix so opening
    several drafts cannot hide which role each artifact belongs to.
    """
    company = re.sub(r"[^a-z0-9]+", "_", str(job.get("company") or "company").lower()).strip("_")
    return "%s_resume_ai.pdf" % (company[:64] or "company")


def run_pdf_path(run_dir: Path) -> Path:
    """Find the generated PDF for both new named runs and legacy runs."""
    status = read_json(run_dir / "status.json", {}) or {}
    configured = str(status.get("pdf_filename") or "").strip()
    if configured:
        return run_dir / Path(configured).name
    named = sorted(run_dir.glob("*_resume_ai*.pdf"))
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
    name = Path(str(value or "company_resume_ai.pdf")).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if safe == "resume.pdf":
        return "company_resume_ai.pdf"
    return safe or "company_resume_ai.pdf"


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


def _low_value_rewrite(source: str, candidate: str) -> bool:
    """Detect a near-copy that does not buy a new hiring signal.

    This intentionally errs on the side of preserving an approved source
    line.  A near-copy is allowed through when it adds a concrete metric or a
    meaningful technical/role term; otherwise the source wording is clearer
    and the apparent diff is just churn.
    """
    source_plain = _latex_plain(source).strip()
    candidate_plain = _latex_plain(candidate).strip()
    if not source_plain or not candidate_plain or source_plain == candidate_plain:
        return False
    similarity = SequenceMatcher(
        None, source_plain.lower(), candidate_plain.lower(), autojunk=False
    ).ratio()
    overlap = _resume_text_similarity(source_plain, candidate_plain)
    if similarity < LOW_VALUE_REWRITE_SIMILARITY or overlap < LOW_VALUE_REWRITE_OVERLAP:
        return False
    if _resume_numeric_anchors(source_plain) != _resume_numeric_anchors(candidate_plain):
        return False
    source_tokens = _resume_tokens(source_plain)
    candidate_tokens = _resume_tokens(candidate_plain)
    added = candidate_tokens - source_tokens
    meaningful_added = added - GENERIC_REWRITE_TOKENS
    return not meaningful_added


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
    order = (CANONICAL_TEMPLATE, "cv_full.tex")
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
            role_text = _plain_heading(role).lower()
            kind = (
                "leadership"
                if "leadership" in section
                or "extracurricular" in section
                or "resident assistant" in role_text
                else "experience"
            )
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


def _canonical_source_name(value: Any) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    if normalized.startswith("CV/"):
        normalized = normalized[3:]
    return normalized


def _is_canonical_source(value: Any) -> bool:
    """Recognize the immutable benchmark without treating generated output as authority."""
    normalized = _canonical_source_name(value)
    # ``resume.tex`` is retained only for small historical test fixtures. The
    # real catalog uses the immutable filename above.
    return normalized in {CANONICAL_TEMPLATE, "resume.tex"}


def canonical_resume_benchmark(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """Return compact canonical structure for marginal-change comparisons.

    This is a benchmark, not a preservation rule. It tells the model what the
    current resume proves so that a creative swap can explain what it gains
    and what evidence it gives up.
    """
    experiences = []
    projects = []
    canonical_bullets = []
    for entry in (catalog.get("entries") or {}).values():
        canonical = [
            bullet for bullet in (entry.get("bullets") or [])
            if _is_canonical_source(bullet.get("source"))
        ]
        if not canonical:
            continue
        bullet_ids = [str(item.get("id") or "") for item in canonical]
        if entry.get("kind") == "experience":
            experiences.append({
                "source_id": str(entry.get("id") or ""),
                "company": str(entry.get("company") or ""),
                "role": str(entry.get("role") or ""),
                "dates": str(entry.get("dates") or ""),
                "bullet_ids": bullet_ids,
            })
        elif entry.get("kind") == "project":
            projects.append({
                "source_id": str(entry.get("id") or ""),
                "heading": str(entry.get("heading") or ""),
                "bullet_ids": bullet_ids,
            })
        for bullet in canonical:
            canonical_bullets.append({
                "source_id": str(bullet.get("id") or ""),
                "entry_id": str(entry.get("id") or ""),
                "section": str(entry.get("kind") or ""),
                "text": str(bullet.get("text") or ""),
            })
    return {
        "template": CANONICAL_TEMPLATE,
        "experience_order": experiences,
        "projects": projects,
        "canonical_bullets": canonical_bullets,
    }


def _portfolio_signal_families(text: Any) -> List[str]:
    plain = _latex_plain(str(text or ""))
    return [
        family for family, pattern in PORTFOLIO_SIGNAL_PATTERNS.items()
        if re.search(pattern, plain, flags=re.I)
    ]


def portfolio_diagnostics(
    plan: Dict[str, Any], catalog: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare the whole selected portfolio, not just isolated bullets.

    This is a review instrument rather than a composite score. It exposes
    breadth, overlap, and stronger unused alternatives so a writer or
    independent critic can catch a technically polished but strategically
    weaker draft.
    """
    entries = catalog.get("entries") or {}
    selected: Dict[str, Dict[str, Any]] = {}
    selected_families: Dict[str, List[str]] = {}
    section_by_id: Dict[str, str] = {}
    for section in ("experiences", "projects", "leadership"):
        for selection in plan.get(section, []) or []:
            entry_id = str(selection.get("source_id") or "")
            entry = entries.get(entry_id) or {}
            text = " ".join([
                str(entry.get("heading") or ""),
                str(entry.get("company") or ""),
                str(entry.get("role") or ""),
                " ".join(str(item.get("text") or "") for item in selection.get("bullets", [])),
            ])
            selected[entry_id] = {
                "source_id": entry_id,
                "section": section,
                "label": _project_heading(entry.get("heading")) if entry.get("kind") == "project" else str(
                    entry.get("company") or entry.get("role") or entry_id
                ),
            }
            selected_families[entry_id] = _portfolio_signal_families(text)
            section_by_id[entry_id] = section

    selected_experience_families = set(
        family
        for entry_id, families in selected_families.items()
        if section_by_id.get(entry_id) == "experiences"
        for family in families
    )
    selected_project_ids = [
        str(selection.get("source_id") or "")
        for selection in plan.get("projects", []) or []
    ]
    selected_project_families = set(
        family
        for entry_id, families in selected_families.items()
        if section_by_id.get(entry_id) == "projects"
        for family in families
    )
    overlap_families = {
        "ai_orchestration", "retrieval_search", "backend_api", "cloud_data",
        "ml_modeling", "computer_vision_multimodal", "security_access",
        "algorithms_validation", "systems_reliability",
    }
    project_overlap = []
    for entry_id in selected_project_ids:
        families = set(selected_families.get(entry_id, []))
        # Awards and people signals are useful, but should not by themselves
        # make two technically different projects look redundant.
        shared = sorted(families & selected_experience_families & overlap_families)
        distinct = sorted(families - selected_experience_families)
        if len(shared) >= 2:
            project_overlap.append({
                "source_id": entry_id,
                "label": selected.get(entry_id, {}).get("label", entry_id),
                "shared_with_experience": shared,
                "distinct_from_experience": distinct,
                "severity": "high" if len(distinct) <= 1 else "review",
            })

    unused_projects = []
    for entry_id, entry in entries.items():
        if entry.get("kind") != "project" or str(entry_id) in selected:
            continue
        text = " ".join([
            str(entry.get("heading") or ""),
            " ".join(str(item.get("text") or "") for item in entry.get("bullets", [])),
        ])
        families = set(_portfolio_signal_families(text))
        unique_to_alternative = sorted(families - selected_experience_families - selected_project_families)
        unused_projects.append({
            "source_id": str(entry_id),
            "label": _project_heading(entry.get("heading") or entry_id),
            "signal_families": sorted(families),
            "unique_to_alternative": unique_to_alternative,
            "technical_alternative": bool(
                families & {
                    "backend_api", "cloud_data", "ml_modeling",
                    "computer_vision_multimodal", "security_access",
                    "algorithms_validation", "systems_reliability",
                }
            ),
        })
    unused_projects.sort(
        key=lambda item: (
            not bool(item["unique_to_alternative"]),
            not item["technical_alternative"],
            -len(item["unique_to_alternative"]),
            item["label"],
        )
    )
    leadership_ids = [
        str(selection.get("source_id") or "")
        for selection in plan.get("leadership", []) or []
    ]
    leadership_competition = []
    if leadership_ids:
        # Advisory by design: leadership may still be justified for a target,
        # but the plan must explain retaining it when a credible technical
        # project is available for the same page space.
        leadership_competition = [
            item for item in unused_projects if item["technical_alternative"]
        ][:6]

    canonical = canonical_resume_benchmark(catalog)
    canonical_project_ids = [
        str(item.get("source_id") or "") for item in canonical.get("projects", [])
    ]
    selected_families_all = sorted({
        family for families in selected_families.values() for family in families
    })
    warnings = []
    blocking_warnings = []
    if leadership_competition:
        warning = (
            "Leadership competes with stronger unused technical project alternatives; "
            "retain it only if the alternatives are genuinely weaker for this target."
        )
        warnings.append(warning)
        ledger_text = " ".join(
            str(item.get(field) or "").lower()
            for item in (plan.get("decision_ledger") or [])
            if isinstance(item, dict)
            for field in ("action", "current_evidence", "replacement_or_exclusion", "why_stronger")
        )
        if "leadership" not in ledger_text and "resident assistant" not in ledger_text and "ra" not in ledger_text:
            blocking_warnings.append(warning)
    high_overlap = [item for item in project_overlap if item["severity"] == "high"]
    if high_overlap and unused_projects:
        warnings.append(
            "At least one selected project repeats multiple experience signal families; "
            "compare it against unused projects before expanding the repeated story."
        )
    if canonical_project_ids and not set(canonical_project_ids) <= set(selected_project_ids):
        warnings.append(
            "The tailored portfolio differs from the canonical project set; every dropped "
            "strong project needs a clear replacement or explicit tradeoff."
        )
    return {
        "selected_entry_families": selected_families,
        "selected_signal_families": selected_families_all,
        "selected_signal_family_count": len(selected_families_all),
        "project_overlap": project_overlap,
        "unused_project_alternatives": unused_projects[:8],
        "leadership_competition": leadership_competition,
        "canonical_project_ids": canonical_project_ids,
        "selected_project_ids": selected_project_ids,
        "warnings": warnings,
        "blocking_warnings": blocking_warnings,
    }


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
    totals = {
        "codex_tokens": 0, "codex_calls": 0,
        "claude_tokens": 0, "claude_calls": 0,
    }
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
    # These are first-party subscription CLIs only.  Do not add Ollama,
    # arbitrary OpenAI-compatible endpoints, or local model servers here.
    # Luna is the Codex model selected for this harness, not a separate local
    # executable. Claude remains the independent provider lane.
    return {
        "codex": shutil.which("codex"),
        "claude": shutil.which("claude"),
    }


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
    if label.startswith("space_expansion"):
        return isinstance(data.get("additions"), list) and isinstance(data.get("decision"), str)
    return all(key in data for key in ("experiences", "projects", "leadership", "positioning_thesis"))


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
    decision_ledger_item = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "current_evidence": {"type": "string"},
            "replacement_or_exclusion": {"type": "string"},
            "target_signal": {"type": "string"},
            "why_stronger": {"type": "string"},
            "signal_lost": {"type": "string"},
        },
        "required": [
            "action", "current_evidence", "replacement_or_exclusion",
            "target_signal", "why_stronger", "signal_lost",
        ],
        "additionalProperties": False,
    }
    front_matter_policy = {
        "type": "object",
        "properties": {
            "coursework": {"type": "string", "enum": ["keep", "omit"]},
            "awards": {"type": "string", "enum": ["keep", "omit"]},
        },
        "required": ["coursework", "awards"],
        "additionalProperties": False,
    }

    def selection(max_bullets: int) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "bullets": {
                    "type": "array",
                    "items": bullet,
                    "minItems": 1,
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
                "items": selection(10),
                "minItems": 0,
                "maxItems": PORTFOLIO_CAPS["experiences"]["entries"],
            },
            "projects": {
                "type": "array",
                "items": selection(8),
                "minItems": 0,
                "maxItems": PORTFOLIO_CAPS["projects"]["entries"],
            },
            "leadership": {
                "type": "array",
                "items": selection(4),
                "minItems": 0,
                "maxItems": PORTFOLIO_CAPS["leadership"]["entries"],
            },
            "revision_notes": {"type": "array", "items": {"type": "string"}},
            "decision_ledger": {
                "type": "array", "maxItems": 40, "items": decision_ledger_item,
            },
            "front_matter_policy": front_matter_policy,
        },
        "required": [
            "positioning_thesis",
            "selected_evidence",
            "excluded_evidence",
            "experiences",
            "projects",
            "leadership",
            "revision_notes",
            "decision_ledger",
            "front_matter_policy",
        ],
        "additionalProperties": False,
    }


def space_expansion_schema() -> Dict[str, Any]:
    """Structured contract for filling measured page room with source evidence."""
    addition = {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string"},
            "placement": {"type": "string", "enum": ["append_bullet", "new_entry"]},
            "source_id": {"type": "string"},
            "source_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
            "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
            "text": {"type": "string"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 100},
            "target_signal": {"type": "string"},
            "why": {"type": "string"},
        },
        "required": [
            "entry_id", "placement", "source_id", "source_ids", "evidence_ids", "text",
            "priority", "target_signal", "why",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "additions": {"type": "array", "items": addition, "maxItems": MAX_SPACE_EXPANSION_CANDIDATES},
            "decision": {"type": "string"},
        },
        "required": ["additions", "decision"],
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
            "blocking_issues": {"type": "array", "items": {"type": "string"}},
            "line_feedback": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "issue": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "severity": {"type": "string"},
                    },
                    "required": ["source_id", "issue", "recommendation", "severity"],
                    "additionalProperties": False,
                },
            },
            "unsupported_claims": {"type": "array", "items": {"type": "string"}},
            "missing_evidence": {"type": "array", "items": {"type": "string"}},
            "revision_priorities": {"type": "array", "items": {"type": "string"}},
            "decision_feedback": {
                "type": "array", "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "current_evidence": {"type": "string"},
                        "replacement_or_exclusion": {"type": "string"},
                        "issue": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "severity": {"type": "string"},
                    },
                    "required": [
                        "action", "current_evidence", "replacement_or_exclusion",
                        "issue", "recommendation", "severity",
                    ],
                    "additionalProperties": False,
                },
            },
            "portfolio_comparison": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "reason": {"type": "string"},
                    "preserved_strengths": {"type": "array", "items": {"type": "string"}},
                    "gained_strengths": {"type": "array", "items": {"type": "string"}},
                    "lost_strengths": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "status", "reason", "preserved_strengths",
                    "gained_strengths", "lost_strengths",
                ],
                "additionalProperties": False,
            },
        },
        "required": [
            "criteria", "blocking_issues", "line_feedback", "unsupported_claims",
            "missing_evidence", "revision_priorities",
            "decision_feedback",
            "portfolio_comparison",
        ],
        "additionalProperties": False,
    }


def reviewed_plan_schema(enhance: bool) -> Dict[str, Any]:
    """Backward-compatible name for the critique-only contract."""
    return review_schema()


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


def provider_model_label(provider: str) -> str:
    """Name the approved subscription lane without pretending to know hidden model routing."""
    if str(provider or "") == "codex":
        return CODEX_LUNA_MODEL
    if str(provider or "") == "claude":
        return "Claude Code subscription CLI"
    return str(provider or "unknown")


def stop_provider_process(proc: subprocess.Popen) -> None:
    """Stop a provider and children after a usable answer or timeout."""
    try:
        if proc.poll() is not None:
            return
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


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
            "model=" + CODEX_LUNA_MODEL,
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
    elif provider == "claude":
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
    else:
        return {"provider": provider, "ok": False, "error": "unsupported provider lane"}
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
                start_new_session=(os.name == "posix"),
            )
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            timed_out = False
            while proc.poll() is None:
                recovered = provider_data_from_files(stdout_path, stderr_path, label)
                if recovered is not None:
                    stop_provider_process(proc)
                    return {
                        "provider": provider,
                        "ok": True,
                        "elapsed_seconds": round(time.time() - started, 1),
                        "data": recovered,
                        "usage_tokens": provider_usage_tokens(stderr_path),
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                    }
                if time.time() - started >= timeout:
                    timed_out = True
                    stop_provider_process(proc)
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
        "CV/immutable/tldp_resume.tex",
        "CV/immutable/og_resume.tex (visual benchmark only)",
        "CV/AGENTS.md",
        "CV/CLAUDE.md",
        "CV/README.md",
        "CV/RESUME_TAILORING_PLAYBOOK.md",
        "CV/RESUME_BULLET_METHODOLOGY.md",
        "CV/RESUME_NOTES.md",
        "CV/JJ_RESUME_CONTEXT.md",
        "CV/Victor_Jimenez_Knowledge_Base_v2.md",
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
        # Method and owner-authored context come first. Historical resumes
        # are bounded benchmarks, never an instruction to copy their shape.
        "RESUME_NOTES.md",
        # Owner-authored notes contain current claim boundaries and regression
        # benchmarks; give them the first context window so important middle
        # sections are not lost to the bounded authority excerpt.
        "RESUME_TAILORING_PLAYBOOK.md",
        "RESUME_BULLET_METHODOLOGY.md",
        "JJ_RESUME_CONTEXT.md",
        "experiences/JJ_SOURCE_OF_TRUTH.md",
        "experiences/JJ_AI_Data_Science_Intern.md",
        "experiences/JJ_BULLET_ITERATION_LOG.md",
        "Victor_Jimenez_Knowledge_Base_v2.md",
        "immutable/VictorJimenezResume.tex",
        "immutable/tldp_resume.tex",
        "immutable/og_resume.tex",
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
        label = name
        if name.startswith("immutable/") and name != CANONICAL_TEMPLATE:
            label += " [visual benchmark only; do not reuse claims or metadata]"
        parts.append("# " + label + "\n" + excerpt)
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
    for filename in (CANONICAL_TEMPLATE, "cv_full.tex"):
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
- Victor has two interaction modes. In back-and-forth mode, treat his latest
  wording, claim-boundary, and layout feedback as hard constraints; preserve
  approved text and revise only the disputed part. In take-the-wheel mode,
  inspect the Markdown source-of-truth files, form a positioning thesis, and
  make the strongest defensible draft without asking for preferences already
  recorded in the contract.
- Before returning a bullet, simulate Victor's critic: what should the reader
  think, what did he actually do, what technical mechanism proves it, what is
  the defensible impact, what must stay private, and does the line earn its
  space? Ask Victor only when a missing fact, disputed claim, privacy boundary,
  or materially different positioning choice changes the result.
- Quality and interview defensibility outrank forced keyword coverage. Avoid
  whitespace, short filler bullets, fake speedups, commercialization claims,
  vague infrastructure ownership, and two-line bullets when the target is a
  one-page artifact.
- CV/immutable/VictorJimenezResume.tex is the immutable visual template. You are selecting content,
  not designing a resume. The harness, not you, renders the LaTeX.
- Never read CV/.resume_studio/ or use earlier generated resumes/reports as
  evidence. They are outputs, not authority.
- Use the complete CV/RESUME_TAILORING_PLAYBOOK.md and
  CV/RESUME_BULLET_METHODOLOGY.md. Use CV/cv_full.tex as the responsibility
  bank and the experience dossiers as higher-authority factual sources.
- The immutable historical og_resume/tldp_resume files are visual and wording
  benchmarks only. They contain superseded claims; never copy a claim from
  them unless a current authority source independently supports it.
- The target keyword strategy below is an ATS aid, not a license to keyword
  stuff. Use exact supported terms when they naturally describe an authorized
  artifact; put unsupported requirements in the gap list rather than inventing
  them. Skills may carry a supported term, but the strongest terms should also
  appear in meaningful experience/project lines when evidence allows.
- CV/immutable/VictorJimenezResume.tex is authoritative for the immutable contact, education, skills,
  employer-heading metadata, and dates that the renderer copies. Treat older
  conflicting metadata in cv_full.tex or target-specific resumes as stale;
  those files authorize bullet evidence, not replacement template metadata.
- Preserve qualifiers such as prototype, synthetic, simulation, or demo when
  they distinguish the work from production or real-user deployment.
- Employer entries are rendered company-first, then role/title. TLDP is one
  target program, not a generic style.
- Return a rich but ranked candidate pool. Choose the number of experiences,
  projects, leadership items, and bullets that best support this role. There
  is no required leadership section, project count, or bullet floor. Every
  selected bullet must introduce a distinct, defensible interview thread;
  lower-ranked candidates are packing alternatives, not filler.
- Choose the portfolio before polishing individual lines. Compare the
  whole-resume signal: technical breadth, project differentiation, external
  validation, and the number of distinct interview stories. A good bullet can
  still be the wrong portfolio choice if it repeats a stronger experience
  story or displaces a better project.
- For technical software-engineering roles, Resident Assistant/community
  leadership is discretionary. Select it only when it adds a needed,
  otherwise-uncovered signal and no stronger unused project or technical
  evidence earns the space. Do not use RA to fill a page while a materially
  stronger technical alternative is available.
- If an experience already proves agents, RAG, or retrieval, a project using
  those tools must add a clearly distinct engineering surface—such as access
  control, document processing at scale, caching, or a different product
  boundary—or yield its slot to a stronger nonredundant project. Keep a strong
  fifth experience bullet when the page can carry it.
- Coursework, the HackMIT acceptance-pool proof bullet, and the aggregated
  Awards skill line are flexible reserves. Keep them by default, but when page
  space is genuinely needed, reclaim them in this order: coursework first,
  then the HackMIT acceptance/selection bullet if present, then Awards. This
  reserve policy must run before deleting strong technical experience/project
  evidence. Record the tradeoff in decision_ledger or the packing report.
- Victor's immutable human references are quality and visual benchmarks, not a
  mandatory portfolio shape. Use available page space for strong evidence when
  it genuinely earns space; if measured capacity can carry a verified unused
  bullet with real hiring value, prefer that over avoidable whitespace. Accept
  bottom clearance only when no useful authorized line fits. Never pad a page
  or preserve a weak category just to imitate a reference's density.
- Assign priority 1-100 to every bullet based on target relevance, proof,
  distinctiveness, and interview value. Do not inflate every priority.
- Return source IDs from the supplied catalog. Never return a LaTeX document,
  preamble, section command, margin, font size, spacing command, or page break.
- Projects are deliberately replaceable. Choose only the strongest projects
  needed for this target; do not preserve a base-CV project merely because it
  appeared in CV/immutable/VictorJimenezResume.tex. Reword supported bullets
  around the posting's exact terms and explain swaps or omissions in
  revision_notes.
- Creative permission is not itself a reason to change content. Compare every
  substantive candidate change with the canonical/current benchmark and make
  it only when the expected hiring-value gain is meaningful. Consider target
  relevance, evidence strength, technical impressiveness, specificity,
  differentiation, accurate ATS terminology, breadth, redundancy,
  readability/structural cost, and the information lost by removing the
  current evidence. Prefer high-value project swaps, stronger unused bullets,
  newly exposed technical dimensions, useful reordering, and redundancy
  reduction over paraphrase churn.
- Use action-specific thresholds: reordering has a low threshold; surfacing an
  unused verified bullet has a moderate threshold; rewriting wording has a
  moderate-to-high threshold; removing a strong metric or specific claim has a
  high threshold; removing a strong project or experience has a high
  threshold; and breaking reverse chronology or another resume convention has
  a very high threshold. Preserve conventional reverse-chronological job
  order unless a deliberate exception has a strong, recorded reason.
- Do not infer an AI-productivity-tools requirement merely from building AI
  systems. If the supplied evidence does not satisfy a requirement, leave it
  as a gap rather than stretching adjacent experience.
- Return a concise decision_ledger for every substantive swap, exclusion,
  removal, rewrite, or nonstandard reorder. Each item must state the current
  evidence, replacement or exclusion, target signal, why the change is
  stronger, and important signal lost. Leave it empty when no substantive
  change is justified.
- Return front_matter_policy with coursework and awards set to keep unless
  reclaiming that flexible space is preferable to removing strong technical
  evidence.
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
    benchmark_text = json.dumps(canonical_resume_benchmark(catalog), indent=2, ensure_ascii=False)
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
        + "\n\nCanonical/current benchmark (comparison point, not a preservation rule):\n"
        + benchmark_text[:MAX_CATALOG_PROMPT_CHARS]
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
                "decision_ledger": data.get("decision_ledger", []),
                "front_matter_policy": data.get("front_matter_policy", {}),
            }
        )
    return (
        "You are the senior resume evidence editor synthesizing competing plans for Victor.\n"
        "This request is self-contained. Do not inspect the filesystem or run commands; use the supplied source IDs "
        "and evidence only. Do not edit files and do not return a LaTeX document. "
        "CV/immutable/VictorJimenezResume.tex remains immutable and the harness renders it. Return a strongest-first "
        "ranked evidence pool with adaptive sections and no filler. Choose the number of entries and bullets that best "
        "support the target; each retained bullet must earn its place through target fit, proof, and a distinct "
        "interview story. Use a verified unused bullet when measured page capacity can carry it and it adds hiring "
        "value; normal bottom clearance is acceptable only when no additional evidence is useful. "
        + ("You may substantially rewrite or synthesize bullet text from the authorized source bank; preserve the primary source_id, add all supporting source IDs, and retain every scope-limiting qualifier. " if enhance else "Select source IDs verbatim; do not rewrite bullets. ")
        + ("This is the unrestricted creative pass: write genuinely original, role-specific bullets and make decisive project swaps when the evidence supports them; do not collapse back to base-resume phrasing. " if unrestricted else "")
        + "Choose the stronger defensible plan rather than averaging it. Judge the whole portfolio before individual wording: preserve strong fifth experience bullets when space permits, remove Resident Assistant before a stronger unused technical project, and do not spend a project slot repeating an experience's agents/RAG/retrieval story unless the project adds a materially distinct engineering surface. If the rendered page needs room for a distinct project or bullet, use flexible reserves before substantive evidence: coursework first, then the HackMIT acceptance-pool bullet when present, then Awards. Do not change an already strong line merely to make the draft look tailored. Compare each substantive swap, exclusion, rewrite, or reorder with the canonical/current benchmark and record the hiring-value gain and important signal lost in decision_ledger. High-value changes include stronger unused evidence, a materially better project, a newly exposed technical dimension, useful ordering, accurate ATS terminology, and reduced redundancy; low-value paraphrase churn is not a goal. Preserve reverse-chronological job order unless the exception is genuinely stronger and explicitly recorded. \n\n"
        "Job context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nEvidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nCanonical/current benchmark (comparison point, not a preservation rule):\n"
        + json.dumps(canonical_resume_benchmark(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
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
        "You are an independent adversarial resume critic. This is a fresh review: do not "
        "trust the generation agent, its explanations, or any score it may have claimed. "
        "This request is self-contained: do not inspect the filesystem, run commands, or "
        "read prior generated reports. Use only the target, proposed plan, rendered text, "
        "catalog, and authorized evidence supplied below. Return critique JSON only. "
        "Do not return a replacement plan and do not mutate any line. Identify the highest-value "
        "blocking issues and line-level recommendations for the separate writer to apply. "
        "Sections and bullet counts are adaptive: do not penalize an omitted leadership or "
        "project section unless the target argument genuinely needs that evidence. "
        + ("This is an unrestricted creative pass; preserve factual boundaries but prefer a fresh, specific argument over safe base-CV wording. " if unrestricted else "")
        + "Use the exact ATS keyword strategy to improve supported keyword coverage, while recording unsupported requirements as missing evidence. "
        "unsupported_claims must describe only claims present in the proposed plan.\n\n"
        "Authority rule: CV/immutable/VictorJimenezResume.tex is canonical for the immutable contact, "
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
        "distinctiveness: selected items are complementary and exclusions are sensible\n"
        "privacy: private or startup-sensitive details stay out of publishable copy\n"
        "Do not penalize a draft merely because it is shorter than a human reference. Penalize "
        "actual repetition, weak hierarchy, sparse reasoning, or hard-to-parse writing.\n"
        "Audit the proposed plan against the canonical/current benchmark, but do not apply a blanket "
        "preserve-the-base rule. Creative changes are justified only by meaningful expected hiring-value "
        "gain. Specifically inspect the decision_ledger for low-value paraphrase churn, keyword-only "
        "choices, removal of a strong metric or specific technical signal, failure to surface stronger "
        "unused evidence, repeated evidence across Awards/title/bullet, and a break from reverse "
        "chronological job order without a strong recorded reason. For each substantive issue, return "
        "decision_feedback with the action, current evidence, replacement or exclusion, the issue, a "
        "concrete recommendation, and severity. Do not manufacture feedback when the change is clearly "
        "higher-value than the benchmark.\n"
        "Treat the following as a portfolio-level review, not a bullet spelling exercise: compare "
        "technical breadth and distinct interview stories across the entire resume; flag a selected RA "
        "section when a stronger unused technical project exists; flag a project that repeats the "
        "experience's agents/RAG/retrieval story without a distinct surface; and flag a removed strong "
        "fifth experience bullet when the page has room or flexible coursework/awards could have been "
        "cut first. Also flag avoidable whitespace when a verified unused bullet would fit cleanly and add "
        "real hiring value. The writer may still overrule a flag with source-grounded reasoning.\n"
        "Also return unsupported_claims (array), missing_evidence (array), and "
        "revision_priorities (array). Never make the test easier because the "
        "draft is polished. Include blocking_issues and line_feedback with source IDs.\n\n"
        "Also return portfolio_comparison: an overall pass/partial/fail judgment "
        "against the canonical/current resume, with preserved_strengths, "
        "gained_strengths, and lost_strengths. A tailored resume may win, tie, "
        "or lose; do not assume change is improvement or that the base is always "
        "right.\n\n"
        "Fixed rubric version: "
        + RUBRIC_VERSION
        + "\nTarget context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nBullet provenance plan:\n"
        + json.dumps(plan or {}, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nSource-addressable evidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog or {}), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nCanonical/current benchmark (comparison point, not a preservation rule):\n"
        + json.dumps(canonical_resume_benchmark(catalog or {}), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nDeterministic portfolio diagnostics:\n"
        + json.dumps(portfolio_diagnostics(plan or {}, catalog or {}), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nAuthorized evidence context:\n"
        + json.dumps(graph_context or [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nCV authority dossier:\n"
        + resume_authority_context(repo_root())
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
    )


def revision_prompt(
    context: Dict[str, Any], plan: Dict[str, Any], critique: Dict[str, Any],
    catalog: Dict[str, Any], graph: Optional[Dict[str, Any]] = None,
    unrestricted: bool = False,
) -> str:
    """Ask the writer to apply independent criticism without self-grading."""
    return (
        "You are Codex, the revision writer for Victor's resume studio. An independent "
        "critic reviewed the proposed resume below. Apply the highest-value corrections "
        "to produce a complete replacement plan. You may change sections, project choices, "
        "bullet count, ordering, and wording when the supplied evidence supports it. Do not "
        "blindly follow a critic that conflicts with source authority. Preserve every factual "
        "scope qualifier and cite all fact-bearing source/evidence IDs. Do not return LaTeX. "
        "Do not assign a score; return only the structured plan requested by the schema. "
        "Apply only corrections with meaningful expected hiring-value gain. Keep strong existing "
        "evidence when a proposed replacement is merely a paraphrase or keyword match; retain "
        "important metrics and technical specificity unless the replacement clearly wins. Preserve "
        "reverse-chronological job order by default. Update decision_ledger for every substantive "
        "swap, exclusion, removal, rewrite, or nonstandard reorder, including the important signal "
        "lost. Re-evaluate the whole portfolio before revising a line: a stronger unused technical "
        "project outranks Resident Assistant for a technical SWE role, and an experience's agents/RAG "
        "evidence should not be duplicated by a project unless its engineering surface is distinct. "
        "The ledger is an audit trail, not permission to create churn. "
        + ("This is the unrestricted creative pass; make a sharper role-specific argument rather than reverting to base-CV wording. " if unrestricted else "")
        + "\n\nTarget context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nCurrent plan:\n"
        + json.dumps(plan, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nIndependent critique:\n"
        + json.dumps(critique, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nAuthorized evidence:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nSource catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nCanonical/current benchmark (comparison point, not a preservation rule):\n"
        + json.dumps(canonical_resume_benchmark(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nDeterministic portfolio diagnostics:\n"
        + json.dumps(portfolio_diagnostics(plan, catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nMethodology:\n"
        + resume_methodology_context(repo_root())[:MAX_METHODOLOGY_CONTEXT_CHARS]
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
        "supported ATS term, or proof. The minimum safe right slack is 12pt: every returned bullet must clear that "
        "threshold, and a near-wrap is still a failure even when the PDF technically stays on one line. Prefer a "
        "short, readable line that ends early; never "
        "expand a bullet merely to approach the right margin. Preserve decision_ledger and front_matter_policy unchanged. Do not pad, invent, change layout, or return LaTeX beyond inline textbf/emph. Return the complete "
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


def space_expansion_prompt(
    context: Dict[str, Any], plan: Dict[str, Any], layout: Dict[str, Any],
    catalog: Dict[str, Any], graph: Optional[Dict[str, Any]] = None,
    unrestricted: bool = False,
) -> str:
    """Ask Codex for only the extra evidence that measured page room can carry."""
    selected_entries = [
        {
            "section": section,
            "entry_id": entry.get("source_id"),
            "selected_bullets": [bullet.get("source_id") for bullet in entry.get("bullets", [])],
        }
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
    ]
    selected_entry_ids = {
        str(entry.get("source_id") or "")
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
    }
    selected_bullet_ids = _selected_bullet_ids(plan)
    candidate_entries = []
    for entry in catalog.get("entries", {}).values():
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        for bullet in entry.get("bullets", [])[:8]:
            bullet_id = str(bullet.get("id") or "")
            if not bullet_id or bullet_id in selected_bullet_ids:
                continue
            candidate_entries.append({
                "entry_id": entry_id,
                "kind": entry.get("kind"),
                "heading": _project_heading(entry.get("heading")) if entry.get("kind") == "project" else entry.get("company") or entry_id,
                "already_selected_entry": entry_id in selected_entry_ids,
                "source_id": bullet_id,
                "text": _latex_plain(str(bullet.get("text") or "")),
            })
    return (
        "You are the measured-density editor for Victor's private resume. A compiled one-page draft has passed its "
        "hard layout checks, and deterministic QA found usable bottom capacity. Return only the narrow expansion "
        "object requested by the schema. Do not return a complete resume plan or LaTeX.\n\n"
        "Additions are optional only when no unused source line materially improves the target argument. Never pad "
        "whitespace. Fill the measured window until the next compiled trial would overflow: prefer strong unused "
        "bullets over weak or redundant lines, and do not stop after one line if another verified line still fits. "
        "Use placement=append_bullet for an entry already selected below. You may use placement=new_entry for one "
        "unused project or experience when it adds unique capability coverage and the compiled trial proves the "
        "heading-plus-two-bullets fits; a new entry has a higher bar than an extra bullet and must earn both lines. Do not reorder existing bullets "
        "or entries. Experience entries must remain in reverse chronological job order. Preserve core experience evidence. "
        "If a unique two-bullet project is materially stronger, the deterministic harness may reclaim coursework/Awards "
        "first and then only lower-value project or leadership lines; record that tradeoff rather than padding the page.\n\n"
        "Choose an unused line only when it adds target relevance, technical depth, breadth, differentiation, or a "
        "distinct interview thread. Do not repeat an experience's agents/RAG/retrieval story through a project unless "
        "the line adds a different engineering surface. Preserve prototype, synthetic, simulation, POC, and privacy "
        "boundaries. Use exact supported ATS terms naturally; unsupported requirements remain gaps.\n\n"
        + ("This is the unrestricted/take-the-wheel pass, so select the strongest creative addition when the evidence supports it. " if unrestricted else "Keep this conservative and evidence-first. ")
        + "Every addition must include placement, the catalog entry_id, the exact catalog bullet source_id, source_ids, claim-authorizing "
        "evidence_ids, a complete source-grounded text line, priority, target_signal, and why.\n\n"
        "Target context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nCurrent selected entries and bullets:\n"
        + json.dumps(selected_entries, indent=2, ensure_ascii=False)
        + "\n\nUnused source candidates (new entries are allowed only when the trial earns the heading cost):\n"
        + json.dumps(candidate_entries[:80], indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nMeasured layout:\n"
        + json.dumps({
            "density_gap_pt": layout.get("density_gap_pt"),
            "max_density_gap_pt": MAX_DENSITY_GAP_PT,
            "vertical_capacity": layout.get("vertical_capacity"),
            "horizontal": layout.get("horizontal"),
        }, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nSource-addressable evidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nAuthorized evidence graph:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
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
    raw_ledger = plan.get("decision_ledger")
    if not isinstance(raw_ledger, list):
        raw_ledger = []
    normalized["decision_ledger"] = []
    for item in raw_ledger[:40]:
        if not isinstance(item, dict):
            validation_warnings.append("dropped malformed decision ledger item")
            continue
        normalized["decision_ledger"].append({
            field: str(item.get(field) or "").strip()
            for field in (
                "action", "current_evidence", "replacement_or_exclusion",
                "target_signal", "why_stronger", "signal_lost",
            )
        })
    raw_front_matter = plan.get("front_matter_policy")
    if not isinstance(raw_front_matter, dict):
        raw_front_matter = {}
    normalized["front_matter_policy"] = {
        "coursework": "omit" if str(raw_front_matter.get("coursework") or "").lower() == "omit" else "keep",
        "awards": "omit" if str(raw_front_matter.get("awards") or "").lower() == "omit" else "keep",
    }
    # Providers occasionally place a known source under the wrong resume
    # section while preserving the source ID and evidence. Rebucket only when
    # the catalog proves the source kind; unknown IDs remain validation errors
    # and no claim text is altered.
    section_for_kind = {
        "experience": "experiences",
        "project": "projects",
        "leadership": "leadership",
    }
    rebucketed = {section: [] for section in ("experiences", "projects", "leadership")}
    for section in ("experiences", "projects", "leadership"):
        for selection in plan.get(section, []) or []:
            source_id = str(selection.get("source_id") or "") if isinstance(selection, dict) else ""
            entry = entries.get(source_id) or {}
            target_section = section_for_kind.get(str(entry.get("kind") or ""), section)
            if target_section != section:
                validation_warnings.append(
                    "reclassified %s from %s to %s based on catalog kind"
                    % (source_id, section, target_section)
                )
            rebucketed[target_section].append(selection)
    for section, selections in rebucketed.items():
        normalized[section] = selections
    used_entries = set()
    used_bullets = set()
    for kind, minimum, maximum in (
        ("experiences", 0, PORTFOLIO_CAPS["experiences"]["entries"]),
        ("projects", 0, PORTFOLIO_CAPS["projects"]["entries"]),
        ("leadership", 0, PORTFOLIO_CAPS["leadership"]["entries"]),
    ):
        selections = normalized.get(kind)
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
                    # Some providers understand ``source_id`` as the parent
                    # entry ID even though the schema asks for a bullet ID.
                    # Recover only when the same object supplies an exact
                    # catalog bullet in source_ids/evidence_ids; never guess
                    # from prose or position.
                    references = []
                    for field in ("source_ids", "evidence_ids"):
                        value = bullet.get(field) or []
                        if not isinstance(value, list):
                            value = [value]
                        references.extend(str(item or "") for item in value)
                    replacement = next((value for value in references if value in bullet_bank), "")
                    if replacement:
                        validation_warnings.append(
                            "normalized entry-level bullet source %s to %s for %s"
                            % (bullet_id, replacement, entry_id)
                        )
                        bullet_id = replacement
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
                    elif _low_value_rewrite(bullet_bank[bullet_id], text):
                        validation_warnings.append(
                            "reverted low-value paraphrase %s to its authorized source wording"
                            % bullet_id
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
                        if (
                            (normalized_id in all_bullet_bank or normalized_id in evidence_ids)
                            and normalized_id not in supporting_ids
                        ):
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
    total_bullets = sum(
        len(entry.get("bullets", []))
        for section in ("experiences", "projects", "leadership")
        for entry in normalized.get(section, [])
    )
    if total_bullets == 0:
        errors.append("plan must select at least one evidence bullet")
    if total_bullets > MAX_CANDIDATE_BULLETS:
        errors.append("plan exceeds the %s-bullet candidate safety limit" % MAX_CANDIDATE_BULLETS)
    if validation_warnings:
        normalized["validation_warnings"] = validation_warnings
    return normalized, errors


def enforce_experience_order(plan: Dict[str, Any], catalog: Dict[str, Any]) -> Dict[str, Any]:
    """Keep rendered jobs in the canonical reverse-chronological order.

    Portfolio selection may omit an experience, but importance never changes
    the order of the experiences that remain. The catalog is built from the
    current Markdown dossiers and its order is the same order used by the
    immutable resume benchmark.
    """
    value = copy.deepcopy(plan)
    canonical = canonical_resume_benchmark(catalog).get("experience_order") or []
    rank = {
        str(item.get("source_id") or ""): index
        for index, item in enumerate(canonical)
        if item.get("source_id")
    }
    fallback = {
        str(entry.get("id") or ""): index
        for index, entry in enumerate(
            item for item in (catalog.get("entries") or {}).values()
            if item.get("kind") == "experience"
        )
    }
    value["experiences"] = sorted(
        value.get("experiences", []) or [],
        key=lambda entry: (
            rank.get(str(entry.get("source_id") or ""), 10_000 + fallback.get(str(entry.get("source_id") or ""), 10_000)),
            str(entry.get("source_id") or ""),
        ),
    )
    return value


def _selected_bullet_ids(plan: Dict[str, Any]) -> set:
    return {
        str(bullet.get("source_id") or "")
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, []) or []
        for bullet in entry.get("bullets", []) or []
        if bullet.get("source_id")
    }


def _validate_space_additions(
    data: Dict[str, Any], plan: Dict[str, Any], catalog: Dict[str, Any],
    graph: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Validate narrow model additions before they touch the selected plan."""
    entries = catalog.get("entries") or {}
    selected_entries = {
        str(entry.get("source_id") or ""): (section, entry)
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, []) or []
        if entry.get("source_id")
    }
    selected_ids = _selected_bullet_ids(plan)
    evidence_nodes = {str(node.get("id")) for node in (graph or {}).get("nodes", [])}
    claim_authorities = {
        str(node.get("id")) for node in (graph or {}).get("nodes", [])
        if node.get("claim_allowed")
    }
    errors: List[str] = []
    additions: List[Dict[str, Any]] = []
    seen = set(selected_ids)
    for index, raw in enumerate(data.get("additions") or []):
        if not isinstance(raw, dict):
            errors.append("addition %s is not an object" % index)
            continue
        entry_id = str(raw.get("entry_id") or "")
        source_id = str(raw.get("source_id") or "")
        source_entry = entries.get(entry_id) or {}
        selected = selected_entries.get(entry_id)
        placement = str(raw.get("placement") or "append_bullet")
        if placement not in {"append_bullet", "new_entry"}:
            errors.append("addition %s has invalid placement: %s" % (index, placement))
            continue
        if not source_entry:
            errors.append("addition %s has unknown entry %s" % (index, entry_id))
            continue
        if placement == "append_bullet" and not selected:
            errors.append("addition %s must select its entry before appending: %s" % (index, entry_id))
            continue
        if placement == "new_entry" and selected:
            errors.append("addition %s marks an already selected entry as new: %s" % (index, entry_id))
            continue
        if placement == "new_entry" and str(source_entry.get("kind") or "") not in {"experience", "project"}:
            errors.append("addition %s may add only an experience or project entry" % index)
            continue
        source_bullets = {
            str(item.get("id") or ""): str(item.get("text") or "")
            for item in source_entry.get("bullets", [])
        }
        if source_id not in source_bullets:
            errors.append("addition %s has unknown bullet %s" % (index, source_id))
            continue
        if source_id in seen:
            errors.append("addition %s repeats selected bullet %s" % (index, source_id))
            continue
        source_text = source_bullets[source_id]
        text = _normalize_model_fragment(raw.get("text")) or source_text
        if len(_latex_plain(text)) < MIN_MEANINGFUL_BULLET_CHARS:
            errors.append("addition %s is too short to earn page space" % index)
            continue
        if _contains_forbidden_resume_term(text):
            errors.append("addition %s contains a permanently excluded term" % index)
            continue
        if _missing_protected_qualifiers(source_text, text):
            errors.append("addition %s drops a protected scope qualifier" % index)
            continue
        if FORBIDDEN_CONTENT_COMMANDS.search(text):
            errors.append("addition %s contains a forbidden layout command" % index)
            continue
        unsupported = _unsupported_inline_commands(text)
        if unsupported:
            errors.append("addition %s contains unsupported inline command(s): %s" % (index, ", ".join(unsupported)))
            continue
        source_ids = [str(item) for item in (raw.get("source_ids") or []) if str(item)]
        if source_id not in source_ids:
            source_ids.insert(0, source_id)
        evidence_ids = [str(item) for item in (raw.get("evidence_ids") or []) if str(item)]
        unknown_evidence = [item for item in evidence_ids if item not in evidence_nodes]
        if unknown_evidence:
            errors.append("addition %s cites unknown evidence: %s" % (index, ", ".join(unknown_evidence)))
            continue
        if not evidence_ids or (claim_authorities and not set(evidence_ids) & claim_authorities):
            errors.append("addition %s has no claim-authorizing evidence" % index)
            continue
        additions.append({
            "entry_id": entry_id,
            "placement": placement,
            "section": selected[0] if selected else {
                "experience": "experiences",
                "project": "projects",
                "leadership": "leadership",
            }.get(str(source_entry.get("kind") or ""), "projects"),
            "source_id": source_id,
            "source_ids": list(dict.fromkeys(source_ids)),
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "text": text,
            "priority": max(1, min(100, int(raw.get("priority") or 50))),
            "target_signal": str(raw.get("target_signal") or ""),
            "why": str(raw.get("why") or ""),
            "source_text": source_text,
        })
        seen.add(source_id)
    additions.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("source_id") or "")))
    return additions, errors


def _append_space_addition(plan: Dict[str, Any], addition: Dict[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(plan)
    section = str(addition.get("section") or "")
    target = next(
        (
            entry for entry in value.get(section, [])
            if str(entry.get("source_id") or "") == str(addition.get("entry_id") or "")
        ),
        None,
    )
    if target is None:
        if str(addition.get("placement") or "") != "new_entry":
            raise ValueError("space addition targets an entry that is not in the plan")
        target = {
            "source_id": str(addition.get("entry_id") or ""),
            "bullets": [],
            "why": str(addition.get("why") or "Measured-space capability coverage"),
        }
        value.setdefault(section, []).append(target)
    target.setdefault("bullets", []).append({
        "source_id": addition["source_id"],
        "source_ids": list(addition.get("source_ids") or [addition["source_id"]]),
        "evidence_ids": list(addition.get("evidence_ids") or [addition["source_id"]]),
        "text": addition["text"],
        "priority": addition.get("priority", 50),
        "candidate_rationale": addition.get("why", ""),
    })
    return value


def deterministic_space_additions(
    plan: Dict[str, Any], catalog: Dict[str, Any],
    graph: Optional[Dict[str, Any]] = None,
    keyword_strategy: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build a ranked fallback pool when the expansion lane returns no usable line.

    This is not a replacement for editorial selection. It is a last-mile
    density guard: it only uses unused authoritative bullets, prefers an
    existing entry (no heading cost), and proposes a new technical entry only
    with two bullets so the page does not gain a lonely project heading.
    """
    entries = catalog.get("entries") or {}
    selected_ids = _selected_bullet_ids(plan)
    selected_entries = {
        str(entry.get("source_id") or ""): (section, entry)
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, []) or []
    }
    authority = {
        str(node.get("id"))
        for node in (graph or {}).get("nodes", [])
        if node.get("claim_allowed")
    }
    supported_terms = [
        str(item.get("term") or "")
        for item in (keyword_strategy or {}).get("terms", [])
        if item.get("supported") and str(item.get("term") or "")
    ]

    def candidate_score(text: str, kind: str, placement: str, entry_bullet_count: int, heading: str) -> float:
        score = 0.0
        score += 8.0 if placement == "append_bullet" else 0.0
        score += max(0, 3 - min(entry_bullet_count, 3))
        score += 6.0 * sum(_keyword_present(term, text) for term in supported_terms)
        score += 2.0 * sum(bool(re.search(pattern, text, re.I)) for pattern in PORTFOLIO_SIGNAL_PATTERNS.values())
        if re.search(r"\b\d[\d,.]*\+?%?|\$\d", text):
            score += 3.0
        if re.search(r"\b(?:architected|engineered|implemented|designed|built|trained|orchestrated|pipeline|model|api|database)\b", text, re.I):
            score += 2.0
        if kind == "leadership":
            score -= 10.0
        if heading and re.search(r"1st place|best security|overall|hack", heading, re.I) and re.search(r"\b(?:won|selected|place|competitors?)\b", text, re.I):
            score -= 5.0
        return score

    def raw_candidate(entry_id: str, entry: Dict[str, Any], bullet: Dict[str, Any], placement: str, section: str) -> Dict[str, Any]:
        bullet_id = str(bullet.get("id") or "")
        text = str(bullet.get("text") or "")
        return {
            "entry_id": entry_id,
            "placement": placement,
            "section": section,
            "source_id": bullet_id,
            "source_ids": [bullet_id],
            "evidence_ids": [bullet_id],
            "text": text,
            "priority": 75,
            "target_signal": "measured space capability coverage",
            "why": "Uses verified unused evidence to close measured page capacity without displacing selected lines.",
            "_score": candidate_score(
                _latex_plain(text), str(entry.get("kind") or ""), placement,
                len(selected_entries.get(entry_id, ("", {"bullets": []}))[1].get("bullets", [])),
                str(entry.get("heading") or ""),
            ),
        }

    append_candidates = []
    new_entry_groups: Dict[str, List[Dict[str, Any]]] = {}
    for entry_id, entry in entries.items():
        entry_id = str(entry_id)
        kind = str(entry.get("kind") or "")
        section = {"experience": "experiences", "project": "projects", "leadership": "leadership"}.get(kind, "")
        if not section:
            continue
        selected = selected_entries.get(entry_id)
        selected_bullets = [
            _latex_plain(str(bullet.get("text") or ""))
            for bullet in (selected[1].get("bullets", []) if selected else [])
        ]
        if selected and len(selected[1].get("bullets", [])) >= PORTFOLIO_CAPS[section]["bullets"]:
            continue
        unused = []
        for bullet in entry.get("bullets", [])[:12]:
            bullet_id = str(bullet.get("id") or "")
            text = _latex_plain(str(bullet.get("text") or ""))
            if not bullet_id or bullet_id in selected_ids or len(text) < MIN_MEANINGFUL_BULLET_CHARS:
                continue
            if authority and bullet_id not in authority:
                continue
            if any(_same_entry_resume_bullet(text, selected_text) for selected_text in selected_bullets):
                continue
            candidate = raw_candidate(entry_id, entry, bullet, "append_bullet" if selected else "new_entry", section)
            unused.append(candidate)
        if selected:
            append_candidates.extend(unused)
        elif kind in {"project", "experience"} and len(unused) >= 2:
            new_entry_groups[entry_id] = sorted(unused, key=lambda item: (-item["_score"], item["source_id"]))[:2]

    append_candidates.sort(key=lambda item: (-item["_score"], item["source_id"]))
    new_groups = sorted(
        new_entry_groups.values(),
        key=lambda group: (-sum(item["_score"] for item in group), group[0]["entry_id"]),
    )
    # Give distinct selected entries a first look, then allow a second bullet
    # from the strongest entry if the compiled trials still have room.
    diversified = []
    seen_entries = set()
    for item in append_candidates:
        if item["entry_id"] in seen_entries:
            continue
        diversified.append(item)
        seen_entries.add(item["entry_id"])
        if len(diversified) >= 2:
            break
    diversified.extend(item for item in append_candidates if item not in diversified)
    result = diversified[:2]
    remaining = MAX_SPACE_EXPANSION_CANDIDATES - len(result)
    if new_groups and len(new_groups[0]) <= remaining:
        result.extend(new_groups[0])
        remaining -= len(new_groups[0])
    result.extend(diversified[2:2 + max(0, remaining)])
    for item in result:
        item.pop("_score", None)
    return result


def _space_removal_candidates(plan: Dict[str, Any]) -> List[Tuple[float, str, int, int]]:
    """Rank safe evidence to reclaim when a stronger two-line entry needs room.

    Expansion is deliberately conservative: flexible coursework/Awards are
    reclaimed by the normal packer first, then only project or leadership
    bullets may be displaced. Core chronological experience is never trimmed
    by this last-mile density pass.
    """
    candidates: List[Tuple[float, str, int, int]] = []
    for section in ("leadership", "projects"):
        for entry_index, entry in enumerate(plan.get(section, []) or []):
            bullets = entry.get("bullets", []) or []
            if len(bullets) <= 1:
                continue
            heading = _latex_plain(str(entry.get("heading") or ""))
            for bullet_index, bullet in enumerate(bullets):
                text = _latex_plain(str(bullet.get("text") or ""))
                cost = _bullet_value(bullet)
                # Leadership is useful only when it is the best available
                # evidence; it is the first substantive category to reclaim.
                if section == "leadership":
                    cost -= 35.0
                # A project-heading award/selection line is a safer sacrifice
                # than implementation evidence that proves a capability.
                if re.search(r"1st place|best security|overall|hack", heading, re.I) and re.search(
                    r"\b(?:won|selected|place|competitors?)\b", text, re.I
                ):
                    cost -= 20.0
                candidates.append((cost, section, entry_index, bullet_index))
    return sorted(candidates, key=lambda item: (item[0], item[1], item[2], item[3]))


def _apply_space_removals(
    plan: Dict[str, Any], actions: Iterable[Tuple[float, str, int, int]]
) -> Dict[str, Any]:
    value = copy.deepcopy(plan)
    # Removing from the end of a bullet list first keeps source indexes valid
    # when two lower-value lines from the same project are reclaimed together.
    ordered = sorted(actions, key=lambda item: (item[1], item[2], item[3]), reverse=True)
    for action in ordered:
        _apply_removal(value, (action[0], action[1], action[2], action[3]))
    return value


def expand_into_measured_space(
    plan: Dict[str, Any], additions: List[Dict[str, Any]], catalog: Dict[str, Any],
    graph: Optional[Dict[str, Any]], run_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Try additions while preserving evidence and atomic new-entry coverage.

    An extra bullet can be tested independently. A new project/experience is
    different: its heading cost is only justified when at least two bullets
    from that entry fit together, so the trial is atomic for new entries.
    """
    current = copy.deepcopy(plan)
    original_ids = _selected_bullet_ids(plan)
    applied = []
    replaced = []
    rejected = []
    groups: List[List[Dict[str, Any]]] = []
    new_entry_groups: Dict[str, List[Dict[str, Any]]] = {}
    for addition in additions[:MAX_SPACE_EXPANSION_CANDIDATES]:
        if str(addition.get("placement") or "") == "new_entry":
            new_entry_groups.setdefault(str(addition.get("entry_id") or ""), []).append(addition)
        else:
            groups.append([addition])
    for group in new_entry_groups.values():
        groups.append(group)
    for index, group in enumerate(groups, start=1):
        if any(str(item.get("placement") or "") == "new_entry" for item in group) and len(group) < 2:
            rejected.append({
                "source_id": str(group[0].get("source_id") or ""),
                "reason": "new project/experience requires at least two bullets to earn its heading",
            })
            continue
        group_ids = {str(item.get("source_id") or "") for item in group}
        attempts = [("direct", [])]
        removals = _space_removal_candidates(current)
        # A stronger unused line or a new entry can displace one or two
        # low-value project/leadership lines when flexible front matter is not
        # enough. Limit the search so a dense run remains bounded while still
        # covering Victor's explicit "replace two weaker bullets with a
        # stronger project" case.
        attempts.extend(("swap", [action]) for action in removals[:6])
        attempts.extend(
            ("swap", list(actions))
            for actions in itertools.combinations(removals[:6], 2)
        )
        accepted = None
        last_reason = "would displace existing selected evidence or fail the one-page contract"
        for attempt_index, (attempt_kind, actions) in enumerate(attempts, start=1):
            trial = _apply_space_removals(current, actions) if actions else copy.deepcopy(current)
            for addition in group:
                trial = _append_space_addition(trial, addition)
            trial = enforce_experience_order(trial, catalog)
            normalized, errors = validate_plan(trial, catalog, enhance=True, graph=graph)
            if errors:
                last_reason = "; ".join(errors[:3])
                continue
            try:
                packed, packing = pack_plan_to_page(
                    normalized, catalog, run_dir / ("space_expansion_%02d_%s%02d" % (index, attempt_kind, attempt_index))
                )
            except (OSError, RuntimeError, ValueError) as exc:
                last_reason = str(exc)
                continue
            packed_ids = _selected_bullet_ids(packed)
            preserved_ids = set(original_ids)
            removed_ids = {
                str(current[section][entry_index]["bullets"][bullet_index].get("source_id") or "")
                for _, section, entry_index, bullet_index in actions
            }
            preserved_ids -= removed_ids
            if preserved_ids.issubset(packed_ids) and group_ids.issubset(packed_ids):
                accepted = (packed, packing, actions)
                break
            last_reason = (
                "would displace evidence beyond the explicitly allowed lower-value project/leadership swap"
                if actions else "would displace existing selected evidence or fail the one-page contract"
            )
        if accepted is None:
            rejected.append({
                "source_id": group[0]["source_id"],
                "reason": last_reason,
            })
            continue
        packed, packing, actions = accepted
        if actions:
            for _, section, entry_index, bullet_index in actions:
                removed = current[section][entry_index]["bullets"][bullet_index]
                replaced.append({
                    "source_id": str(removed.get("source_id") or ""),
                    "entry_id": str(current[section][entry_index].get("source_id") or ""),
                    "section": section,
                    "text": _latex_plain(str(removed.get("text") or "")),
                    "reason": "reclaimed lower-value evidence for a stronger unique measured-space entry",
                })
        current = packed
        for addition in group:
            applied.append({
                "source_id": addition["source_id"],
                "entry_id": addition["entry_id"],
                "target_signal": addition.get("target_signal", ""),
                "why": addition.get("why", ""),
                "text": _latex_plain(addition.get("text", "")),
                "packing": packing,
                "replaced_source_ids": [item["source_id"] for item in replaced[-len(actions):]] if actions else [],
            })
        original_ids = _selected_bullet_ids(current)
    return current, {
        "applied": applied,
        "replaced": replaced,
        "rejected": rejected,
        "attempted": bool(additions),
        "candidate_count": len(additions),
        "decision": (
            "Added verified evidence until measured page capacity was consumed."
            if applied else "No unused verified line earned the available page space without displacing stronger evidence."
        ),
    }


def _render_bullets(bullets: List[Dict[str, str]]) -> List[str]:
    lines = ["        \\resumeItemListStart"]
    for bullet in bullets:
        lines.extend(["            \\resumeItem{", "                " + bullet["text"], "            }"])
    lines.append("        \\resumeItemListEnd")
    return lines


def expand_candidate_portfolio(
    plan: Dict[str, Any], catalog: Dict[str, Any], enhance: bool
) -> Dict[str, Any]:
    """Preserve the agent's rich, ranked pool without adding filler.

    Older versions filled every short plan from source backups to imitate the
    human reference's density.  That made leadership and low-signal bullets
    mandatory.  Selection is now the agent's editorial decision; the page
    packer only removes evidence when the rendered artifact cannot fit.
    """
    return copy.deepcopy(plan)


def curate_candidate_portfolio(
    candidate_plan: Dict[str, Any], catalog: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Remove only duplicate evidence before compile-time packing.

    Section choice and ordering are editorial decisions owned by the agent,
    not deterministic defaults inherited from the general resume.
    """
    curated = copy.deepcopy(candidate_plan)
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
    for section, cap in PORTFOLIO_CAPS.items():
        if counts[section]["entries"] > cap["entries"]:
            violations.append("%s has too many entries" % section)
        for entry_index, entry in enumerate(plan.get(section, [])):
            if len(entry.get("bullets", [])) > cap["bullets"]:
                violations.append("%s exceeds the per-entry bullet cap" % entry.get("source_id"))
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
        "min_total_bullets": None,
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
    benchmark = canonical_resume_benchmark(catalog)
    source_bullets = {
        str(bullet.get("id")): _latex_plain(str(bullet.get("text") or ""))
        for entry in entries.values()
        for bullet in entry.get("bullets", [])
    }
    base_project_ids = {
        str(entry.get("id"))
        for entry in entries.values()
        if entry.get("kind") == "project"
        and (
            any(_is_canonical_source(source) for source in (entry.get("sources") or []))
            or str(entry.get("id") or "") in {
                str(item.get("source_id") or "") for item in benchmark.get("projects", [])
            }
        )
    }
    selected_project_ids = [
        str(entry.get("source_id")) for entry in plan.get("projects", [])
    ]
    rewritten = []
    suppressed_rewrites = []
    selected_bullet_ids = []
    selected_entry_ids = {
        str(entry.get("source_id") or "")
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
    }
    for section in ("experiences", "projects", "leadership"):
        for entry in plan.get(section, []):
            for bullet in entry.get("bullets", []):
                source_id = str(bullet.get("source_id") or "")
                selected_bullet_ids.append(source_id)
                final_text = _latex_plain(str(bullet.get("text") or ""))
                source_text = source_bullets.get(source_id, "")
                supporting = [str(value) for value in (bullet.get("source_ids") or []) if str(value)]
                if final_text != source_text and _low_value_rewrite(source_text, final_text):
                    suppressed_rewrites.append({
                        "section": section,
                        "source_id": source_id,
                        "source_text": source_text,
                        "final_text": final_text,
                        "reason": "near-copy without a new metric, technical term, or target signal",
                    })
                elif final_text != source_text or len(supporting) > 1:
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
    front_matter_policy = {
        "coursework": "omit" if str((plan.get("front_matter_policy") or {}).get("coursework") or "keep") == "omit" else "keep",
        "awards": "omit" if str((plan.get("front_matter_policy") or {}).get("awards") or "keep") == "omit" else "keep",
    }
    canonical_bullet_by_id = {
        str(item.get("source_id") or ""): item
        for item in benchmark.get("canonical_bullets", [])
        if str(item.get("source_id") or "")
    }
    selected_bullet_set = set(selected_bullet_ids)
    added_bullets = [
        {
            "source_id": source_id,
            "entry_id": next(
                (
                    str(entry.get("source_id") or "")
                    for section in ("experiences", "projects", "leadership")
                    for entry in plan.get(section, [])
                    if any(str(item.get("source_id") or "") == source_id for item in entry.get("bullets", []))
                ),
                "",
            ),
            "text": source_bullets.get(source_id, ""),
        }
        for source_id in selected_bullet_ids
        if source_id not in canonical_bullet_by_id
    ]
    removed_bullets = [
        {
            "source_id": source_id,
            "entry_id": item.get("entry_id", ""),
            "section": item.get("section", ""),
            "text": _latex_plain(str(item.get("text") or "")),
        }
        for source_id, item in canonical_bullet_by_id.items()
        if source_id not in selected_bullet_set
    ]
    canonical_experience_ids = [
        str(item.get("source_id") or "")
        for item in benchmark.get("experience_order", [])
        if str(item.get("source_id") or "")
    ]
    selected_experience_ids = [
        str(entry.get("source_id") or "")
        for entry in plan.get("experiences", [])
    ]
    canonical_rank = {
        source_id: index
        for index, source_id in enumerate(canonical_experience_ids)
    }
    selected_rank = [
        canonical_rank[source_id]
        for source_id in selected_experience_ids
        if source_id in canonical_rank
    ]
    chronology_preserved = selected_rank == sorted(selected_rank)
    return {
        "changed_bullet_count": len(rewritten),
        "rewritten_bullets": rewritten,
        "low_value_rewrite_count": len(suppressed_rewrites),
        "suppressed_rewrites": suppressed_rewrites,
        "selected_bullet_count": len(selected_bullet_ids),
        "canonical_bullet_count": len(canonical_bullet_by_id),
        "added_bullets": added_bullets,
        "removed_canonical_bullets": removed_bullets,
        "project_swaps": {
            "swapped_in": [_project_heading(entries.get(entry_id, {}).get("heading") or entry_id) for entry_id in swapped_in],
            "swapped_out": [_project_heading(entries.get(entry_id, {}).get("heading") or entry_id) for entry_id in swapped_out],
        },
        "experience_order": {
            "canonical": canonical_experience_ids,
            "selected": selected_experience_ids,
            "changed": not chronology_preserved,
            "chronology_preserved": chronology_preserved,
        },
        "decision_ledger": [
            {
                field: str(item.get(field) or "")
                for field in (
                    "action", "current_evidence", "replacement_or_exclusion",
                    "target_signal", "why_stronger", "signal_lost",
                )
            }
            for item in (plan.get("decision_ledger") or [])
            if isinstance(item, dict)
        ][:40],
        "front_matter_policy": front_matter_policy,
        "removed_front_matter": [
            field for field, state in front_matter_policy.items() if state == "omit"
        ],
        "portfolio_diagnostics": portfolio_diagnostics(plan, catalog),
        "keyword_coverage": keyword_coverage,
    }


def owner_change_summary(
    plan: Dict[str, Any], catalog: Dict[str, Any], changes: Dict[str, Any],
) -> Dict[str, Any]:
    """Produce the short explanation Victor should see before opening the PDF."""
    entries = catalog.get("entries") or {}
    project_names = [
        _project_heading(entries.get(str(item.get("source_id") or ""), {}).get("heading") or item.get("source_id"))
        for item in plan.get("projects", []) or []
    ]
    removed_projects = list((changes.get("project_swaps") or {}).get("swapped_out") or [])
    ledger = [
        item for item in changes.get("decision_ledger", [])
        if isinstance(item, dict)
    ]
    diagnostics = changes.get("portfolio_diagnostics") or {}
    leadership_competition = diagnostics.get("leadership_competition") or []
    if plan.get("leadership") and leadership_competition:
        headline = "Technical portfolio selected first; leadership was retained only as an explicit tradeoff."
    elif plan.get("leadership"):
        headline = "Portfolio selected first; leadership adds a target-relevant signal not covered elsewhere."
    else:
        headline = "Technical portfolio selected first; leadership was omitted because stronger technical evidence filled the page."
    preserved_fifth = any(
        str(bullet.get("source_id") or "").endswith(":b4")
        for entry in plan.get("experiences", []) or []
        if "johnson-johnson" in str(entry.get("source_id") or "")
        for bullet in entry.get("bullets", [])
    )
    return {
        "headline": headline,
        "projects_kept": project_names,
        "projects_removed": removed_projects,
        "leadership_included": bool(plan.get("leadership")),
        "strong_fifth_experience_preserved": preserved_fifth,
        "front_matter_tradeoffs": list(changes.get("removed_front_matter") or []),
        "portfolio_warnings": list(diagnostics.get("warnings") or [])[:6],
        "redundant_project_flags": [
            str(item.get("label") or item.get("source_id") or "")
            for item in (diagnostics.get("project_overlap") or [])
            if item.get("severity") == "high"
        ][:6],
        "key_decisions": [
            {
                "action": str(item.get("action") or ""),
                "why": str(item.get("why_stronger") or ""),
                "signal_lost": str(item.get("signal_lost") or ""),
            }
            for item in ledger[:6]
        ],
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
        raise ValueError("CV/immutable/VictorJimenezResume.tex is missing the experience marker")
    prefix = _generated_one_page_prefix(template)
    prefix = _apply_front_matter_policy(prefix, plan.get("front_matter_policy"), root)
    entries = catalog["entries"]
    lines = [prefix, "", BODY_MARKER]
    experiences = plan.get("experiences") or []
    projects = plan.get("projects") or []
    leadership = plan.get("leadership") or []
    if experiences:
        lines.extend(["\\section{Experience}", "\\resumeSubHeadingListStart", ""])
        for selection in experiences:
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
    if projects:
        lines.extend(["%-----------PROJECTS-----------", "\\section{Projects}", "\\resumeSubHeadingListStart", ""])
        for selection in projects:
            entry = entries[selection["source_id"]]
            lines.extend(["    \\resumeProjectHeading", "        {\\large %s}{}" % _project_heading(entry["heading"])])
            lines.extend(_render_bullets(selection["bullets"]))
            lines.append("")
        lines.extend(["\\resumeSubHeadingListEnd", ""])
    if leadership:
        lines.extend(["%-----------LEADERSHIP-----------", "\\section{Leadership \\& Extracurriculars}", "\\resumeSubHeadingListStart", ""])
        for selection in leadership:
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


def _generated_one_page_prefix(template: str) -> str:
    """Prepare a future one-page copy without changing protected references."""
    prefix = template.split(BODY_MARKER, 1)[0].rstrip()
    footer_count = prefix.count(CANONICAL_PAGE_FOOTER)
    if footer_count == 0:
        return prefix
    if footer_count != 1:
        raise ValueError("canonical resume template must contain exactly one page footer")
    return prefix.replace(CANONICAL_PAGE_FOOTER, GENERATED_ONE_PAGE_FOOTER, 1)


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
    education_start = prefix.lower().find("%-----------education-----------")
    education_items = [
        (template_index, call)
        for template_index, call in enumerate(all_items)
        if education_start >= 0 and call[0] > education_start and call[0] < before_skills
    ]
    for key, label, position in (
        ("gpa", "GPA", 0),
        ("coursework", "Coursework", 1),
    ):
        if position >= len(education_items):
            continue
        template_index, (_, args, _) = education_items[position]
        result.append({
            "line_id": "front:education:" + key,
            "section": "education",
            "entry_id": "front:education",
            "entry_label": "Education",
            "role": label,
            "text": args[0],
            "source_text": args[0],
            "template": "education",
            "template_index": template_index,
            "argument_index": 0,
        })
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


def _apply_front_matter_policy(
    prefix: str, policy: Optional[Dict[str, Any]], root: Optional[Path] = None,
) -> str:
    """Remove only optional coursework/award lines when the plan earns the space."""
    policy = policy if isinstance(policy, dict) else {}
    omit_ids = set()
    omit_awards = str(policy.get("awards") or "keep").lower() == "omit"
    if str(policy.get("coursework") or "keep").lower() == "omit":
        omit_ids.add("front:education:coursework")
    if omit_awards:
        omit_ids.add("front:skills:4")
    if not omit_ids:
        return prefix
    catalog = {
        str(item.get("line_id") or ""): item
        for item in front_matter_catalog(root or repo_root())
    }
    if omit_awards:
        # Keep the policy stable even if a future canonical skills list moves
        # the aggregated Awards line away from its current fifth position.
        award_lines = [
            item for item in catalog.values()
            if "Awards:" in _latex_plain(str(item.get("text") or ""))
        ]
        if award_lines:
            omit_ids.discard("front:skills:4")
            omit_ids.add(str(award_lines[0].get("line_id") or "front:skills:4"))
    calls = _macro_calls(prefix, "resumeItem", 1)
    removals = []
    for line_id in omit_ids:
        item = catalog.get(line_id)
        if not item:
            continue
        try:
            call = calls[int(item.get("template_index"))]
        except (IndexError, TypeError, ValueError):
            continue
        removals.append((call[0], call[2]))
    for start, end in sorted(removals, reverse=True):
        prefix = prefix[:start] + prefix[end:]
    return prefix


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
    # Education GPA/coursework and skills are editable text lines; the
    # automatic tailoring policy only omits the optional lines when needed.
    for item in plan.get("front_matter", []):
        line_id = str(item.get("line_id") or "")
        if not (
            line_id.startswith("front:education:gpa")
            or line_id.startswith("front:education:coursework")
            or line_id.startswith("front:skills:")
        ):
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
    if not isinstance(plan, dict) or not any(
        plan.get(section) for section in ("experiences", "projects", "leadership")
    ):
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
    total_bullets = sum(
        len(entry.get("bullets", []))
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
    )
    # Packing may remove weak evidence, but it must never turn a valid plan into
    # an empty resume or leave a selected entry with a heading and no bullet.
    can_remove = total_bullets > 1
    for section in ("experiences", "projects", "leadership"):
        entries = plan.get(section, [])
        minimum_entries = PORTFOLIO_FLOORS[section]["entries"]
        minimum_bullets = PORTFOLIO_FLOORS[section]["bullets"]
        for entry_index, entry in enumerate(entries):
            bullets = entry.get("bullets", [])
            if can_remove and len(bullets) > max(1, minimum_bullets):
                for bullet_index, bullet in enumerate(bullets):
                    actions.append((_bullet_value(bullet), section, entry_index, bullet_index))
            if can_remove and len(entries) > minimum_entries:
                # Removing an entry saves its heading too, so compare value per
                # vertical unit rather than raw total value.
                density = sum(_bullet_value(bullet) for bullet in bullets) / max(1, len(bullets) + 1)
                actions.append((density, section, entry_index, None))
    return actions


def _hackmit_acceptance_removal(plan: Dict[str, Any]) -> Optional[Tuple[float, str, int, int]]:
    """Locate the low-signal HackMIT selection proof reserve.

    The acceptance-pool line is useful prestige context, but once the project
    itself is selected it is less valuable than another distinct technical
    bullet. Keep this narrow: only the HackMIT acceptance/selection bullet is
    eligible, never the project's implementation evidence.
    """
    for section in ("projects",):
        for entry_index, entry in enumerate(plan.get(section, [])):
            for bullet_index, bullet in enumerate(entry.get("bullets", [])):
                text = _latex_plain(str(bullet.get("text") or ""))
                lowered = text.lower()
                if "hackmit" in lowered and "acceptance" in lowered:
                    return (0.0, section, entry_index, bullet_index)
    return None


def _reclaim_flexible_content(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Use flexible content in Victor's preferred order before strong evidence.

    Coursework is the first reserve. The HackMIT acceptance-pool line is the
    next reserve because it duplicates the project's prestige context without
    adding a new technical capability. Awards are flexible too, but remain
    ahead of removing substantive technical evidence only after that project
    proof has been considered.
    """
    policy = plan.setdefault("front_matter_policy", {"coursework": "keep", "awards": "keep"})
    if str(policy.get("coursework") or "keep") == "keep":
        policy["coursework"] = "omit"
        return {
            "kind": "front_matter",
            "field": "coursework",
            "reason": "reclaimed flexible coursework space before removing strong resume evidence",
        }
    hackmit_action = _hackmit_acceptance_removal(plan)
    if hackmit_action:
        return {
            "kind": "deferred_bullet_removal",
            "action": hackmit_action,
            "reason": "removed redundant HackMIT selection proof before substantive technical evidence",
        }
    if str(policy.get("awards") or "keep") == "keep":
        policy["awards"] = "omit"
        return {
            "kind": "front_matter",
            "field": "awards",
            "reason": "reclaimed flexible award-line space before removing strong resume evidence",
        }
    return None


def _reclaim_optional_front_matter(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Backward-compatible front-matter-only reserve helper."""
    policy = plan.setdefault("front_matter_policy", {"coursework": "keep", "awards": "keep"})
    for field in ("coursework", "awards"):
        if str(policy.get(field) or "keep") == "keep":
            policy[field] = "omit"
            return {
                "kind": "front_matter",
                "field": field,
                "reason": "reclaimed flexible %s space before removing strong resume evidence" % field,
            }
    return None


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
    plan = enforce_experience_order(curate_candidate_portfolio(candidate_plan, catalog), catalog)
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
        flexible_removal = _reclaim_flexible_content(plan)
        if flexible_removal:
            if flexible_removal.get("kind") == "deferred_bullet_removal":
                removed.append(_apply_removal(plan, flexible_removal["action"]))
                removed[-1]["reason"] = flexible_removal.get("reason", "")
            else:
                removed.append(flexible_removal)
            continue
        actions = _removal_actions(plan)
        if not actions:
            break
        removed.append(_apply_removal(plan, min(actions, key=lambda item: item[0])))

    if not (
        layout.get("compiled")
        and layout.get("pages") == 1
        and not layout.get("overfull")
    ):
        raise RuntimeError(
            "candidate portfolio could not meet the one-page render contract"
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
        "strategy": "adaptive evidence portfolio with compile-measured overflow removal",
        "attempts": attempts,
        "kept_bullets": len(kept_ids),
        "excluded_bullet_ids": sorted(value for value in all_ids - kept_ids if value),
        "removed_front_matter": [
            item for item in removed if item.get("kind") == "front_matter"
        ],
        "style_change_percent": 0.0,
        "density_warning": (
            "normal bottom clearance remains because no additional authorized evidence was packed"
            if not layout.get("density_pass") else ""
        ),
    }


def _horizontal_safety_score(layout: Dict[str, Any]) -> Tuple[int, int, float]:
    horizontal = layout.get("horizontal") or {}
    slacks = [
        float(item.get("right_slack_pt"))
        for item in horizontal.get("bullets", [])
        if isinstance(item.get("right_slack_pt"), (int, float))
    ]
    minimum_slack = min(slacks) if slacks else 0.0
    return (
        int(horizontal.get("wrap_count") or 0),
        int(horizontal.get("near_wrap_count") or 0),
        -minimum_slack,
    )


def _line_compaction_candidates(text: str, source_text: str) -> List[str]:
    """Return conservative, evidence-preserving alternatives for a tight line."""
    current = str(text or "")
    source_plain = _latex_plain(source_text).lower()
    candidates: List[str] = []
    seen = {current}

    def add(value: str) -> None:
        value = str(value or "")
        if value in seen or len(_latex_plain(value)) >= len(_latex_plain(current)):
            return
        seen.add(value)
        candidates.append(value)

    # Prefer terminology already authorized by the source bullet. These are
    # common resume compressions, not new claims.
    if "poc" in source_plain:
        add(re.sub(r"\bproof of concept\b", "POC", current, flags=re.I))
    if "rag" in source_plain:
        add(re.sub(r"\bretrieval[- ]augmented generation\b", "RAG", current, flags=re.I))
    add(re.sub(r"\blearned features\b", "features", current, flags=re.I))
    add(re.sub(r"\bacross RNN/LLM architectures\b", "in RNN/LLM architectures", current, flags=re.I))

    # Articles are the safest generic deletion when the line is otherwise
    # unchanged; generate one-at-a-time variants so the result remains
    # readable and the geometry pass decides whether the edit is worthwhile.
    for match in re.finditer(r"\b(?:a|an|the)\s+", current, flags=re.I):
        add(current[:match.start()] + current[match.end():])
    return candidates


def compact_plan_to_geometry(
    plan: Dict[str, Any], layout: Dict[str, Any], catalog: Dict[str, Any], run_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """Make a final conservative compaction pass after model line editing.

    The model remains responsible for judgment and wording. This fallback only
    tries shorter, source-authorized variants for lines that still fail the
    measured one-line gate, and accepts a variant only when a real PDF compile
    improves the complete resume's horizontal safety.
    """
    best_plan = copy.deepcopy(plan)
    best_layout = layout
    applied: List[Dict[str, Any]] = []
    source_text = {
        str(bullet.get("id")): str(bullet.get("text") or "")
        for entry in (catalog.get("entries") or {}).values()
        for bullet in entry.get("bullets", [])
        if bullet.get("id")
    }
    attempt = 0
    for _ in range(4):
        unsafe_ids = [
            str(item.get("source_id") or "")
            for item in (best_layout.get("horizontal") or {}).get("bullets", [])
            if not item.get("horizontal_pass")
        ]
        if not unsafe_ids:
            break
        changed = False
        for source_id in unsafe_ids:
            current_bullet = None
            for section in ("experiences", "projects", "leadership"):
                for entry in best_plan.get(section, []) or []:
                    for bullet in entry.get("bullets", []) or []:
                        if str(bullet.get("source_id") or "") == source_id:
                            current_bullet = bullet
                            break
                    if current_bullet:
                        break
                if current_bullet:
                    break
            if not current_bullet:
                continue
            current_text = str(current_bullet.get("text") or "")
            candidates = _line_compaction_candidates(current_text, source_text.get(source_id, ""))
            for candidate_text in candidates:
                trial = copy.deepcopy(best_plan)
                replaced = False
                for section in ("experiences", "projects", "leadership"):
                    for entry in trial.get(section, []) or []:
                        for bullet in entry.get("bullets", []) or []:
                            if str(bullet.get("source_id") or "") == source_id:
                                bullet["text"] = candidate_text
                                replaced = True
                                break
                        if replaced:
                            break
                    if replaced:
                        break
                if not replaced:
                    continue
                attempt += 1
                attempt_dir = run_dir / "line_compaction_search" / ("attempt-%02d" % attempt)
                attempt_dir.mkdir(parents=True, exist_ok=True)
                tex = render_plan(trial, catalog, repo_root())
                (attempt_dir / "resume.tex").write_text(tex)
                compiled = compile_resume(attempt_dir)
                if not compiled.get("compiled"):
                    continue
                candidate_layout = pdf_layout(
                    attempt_dir, compiled, plan=trial, run_capacity_test=False,
                )
                if _horizontal_safety_score(candidate_layout) < _horizontal_safety_score(best_layout):
                    applied.append({
                        "source_id": source_id,
                        "from": current_text,
                        "to": candidate_text,
                        "reason": "measured conservative compaction improved one-line safety",
                    })
                    best_plan = trial
                    best_layout = candidate_layout
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
    return best_plan, best_layout, applied


def template_style_guard(
    tex: str, root: Optional[Path] = None,
    front_matter_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    template = (cv_root(root) / CANONICAL_TEMPLATE).read_text()
    template_prefix = template.split(BODY_MARKER, 1)[0].rstrip()
    generated_prefix = tex.split(BODY_MARKER, 1)[0].rstrip() if BODY_MARKER in tex else ""
    generated_prefix = generated_prefix.replace(GENERATED_ONE_PAGE_FOOTER, CANONICAL_PAGE_FOOTER, 1)
    if isinstance(front_matter_policy, dict):
        # Coursework and the aggregated Awards line are the only sanctioned
        # front-matter removals. Compare against the same policy applied to
        # the canonical prefix so this flexibility does not look like format
        # drift to the deterministic gate.
        template_prefix = _apply_front_matter_policy(template_prefix, front_matter_policy, root).rstrip()
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
        "allowed_front_matter_policy": front_matter_policy or {"coursework": "keep", "awards": "keep"},
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
        return {"page_width": None, "page_height": None, "lines": []}
    try:
        raw = subprocess.check_output([pdftotext, "-bbox", str(pdf), "-"], timeout=30)
        root = ET.fromstring(raw)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return {"page_width": None, "page_height": None, "lines": []}
    page = next((element for element in root.iter() if element.tag.endswith("page")), None)
    try:
        page_width = float(page.attrib.get("width")) if page is not None else None
        page_height = float(page.attrib.get("height")) if page is not None else None
    except (TypeError, ValueError):
        page_width = None
        page_height = None
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
    return {"page_width": page_width, "page_height": page_height, "lines": lines}


def review_preview_overlay(
    pdf: Path, plan: Dict[str, Any], changes: Dict[str, Any],
    keyword_strategy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return line-level highlights for a review-only overlay on the clean PDF.

    The image remains the ordinary generated preview.  These transparent
    boxes are positioned from the PDF text geometry in the browser, so review
    highlighting cannot alter or leak into the downloadable application PDF.
    """
    geometry = pdf_line_geometry(pdf)
    page_width = geometry.get("page_width")
    page_height = geometry.get("page_height")
    if not page_width or not page_height:
        return {"available": False, "boxes": [], "page_width": page_width, "page_height": page_height}
    terms = [
        str(item.get("term") or "")
        for item in (keyword_strategy or {}).get("terms", [])
        if item.get("supported") and item.get("rendered") and str(item.get("term") or "")
    ]
    changed_ids = {
        str(item.get("source_id") or "")
        for item in (changes.get("rewritten_bullets") or [])
        if str(item.get("source_id") or "")
    }
    added_ids = {
        str(item.get("source_id") or "")
        for item in (changes.get("added_bullets") or [])
        if str(item.get("source_id") or "")
    }
    changed_bullets = {
        str(bullet.get("source_id") or ""): _latex_plain(str(bullet.get("text") or ""))
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
        for bullet in entry.get("bullets", [])
        if str(bullet.get("source_id") or "") in changed_ids | added_ids
    }
    boxes = []
    for line in geometry.get("lines", []):
        line_text = str(line.get("text") or "")
        line_lower = line_text.lower()
        matched_terms = [term for term in terms if _keyword_present(term, line_lower)]
        changed_id = None
        line_tokens = set(re.findall(r"[a-z0-9]+", line_lower))
        for source_id, bullet_text in changed_bullets.items():
            bullet_tokens = re.findall(r"[a-z0-9]+", bullet_text.lower())
            if len(bullet_tokens) >= 2 and set(bullet_tokens[:2]).issubset(line_tokens):
                changed_id = source_id
                break
        if not matched_terms and not changed_id:
            continue
        boxes.append({
            "left_percent": round(float(line.get("x_min") or 0) / page_width * 100, 3),
            "top_percent": round(float(line.get("y_min") or 0) / page_height * 100, 3),
            "width_percent": round((float(line.get("x_max") or 0) - float(line.get("x_min") or 0)) / page_width * 100, 3),
            "height_percent": round((float(line.get("y_max") or 0) - float(line.get("y_min") or 0)) / page_height * 100, 3),
            "terms": matched_terms,
            "changed_source_id": changed_id,
            "kind": "changed" if changed_id and not matched_terms else "both" if changed_id else "ats",
        })
    return {
        "available": bool(boxes),
        "page_width": page_width,
        "page_height": page_height,
        "boxes": boxes,
        "legend": {"ats": "supported ATS term", "changed": "meaningful content change"},
    }


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
    assert_resume_workspace(run_dir)
    qa_dir = run_dir / "qa_vertical_capacity"
    qa_dir.mkdir(exist_ok=True)
    sentinel = (
        "\\resumeItem{\\textbf{Additional verified technical evidence} "
        "with concrete implementation scope and measurable outcome}\n"
    )
    body_start = tex.find(BODY_MARKER)
    # Test the last list, where an extra line consumes the actual remaining
    # bottom capacity. Inserting into the first experience list can pass even
    # when the final page is already dense below it.
    marker = "\\resumeItemListEnd"
    insertion = tex.rfind(marker, body_start if body_start >= 0 else 0)
    if insertion < 0:
        return {"pass": False, "warning": "could not insert QA bullet"}
    qa_tex = tex[:insertion] + sentinel + tex[insertion:]
    (qa_dir / "resume.tex").write_text(qa_tex)
    compiled = compile_resume(qa_dir)
    layout = pdf_layout(qa_dir, compiled, plan=None, run_capacity_test=False)
    return {
        "pass": layout.get("pages") is not None and layout.get("pages") > 1,
        "one_more_bullet_fits": layout.get("pages") == 1,
        "qa_pages": layout.get("pages"),
        "sentinel": "one standard one-line bullet",
        "warning": (
            "one more bullet still fits" if layout.get("pages") == 1
            else "; ".join(layout.get("warnings") or []) if not layout.get("compiled") else ""
        ),
    }


def render_preview(run_dir: Path) -> Optional[str]:
    assert_resume_workspace(run_dir)
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
    assert_resume_workspace(run_dir)
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
        # Tectonic emits ``resume.pdf`` from ``resume.tex``. Give every
        # generated attempt a role-specific filename before it becomes an
        # artifact, so internal layout searches cannot create ambiguous PDFs.
        if pdf.name == "resume.pdf":
            for parent in (run_dir, *run_dir.parents):
                status = read_json(parent / "status.json", {}) or {}
                job = status.get("job") if isinstance(status, dict) else None
                if not isinstance(job, dict):
                    job = read_json(parent / "job.json", {}) or {}
                if not isinstance(job, dict) or not job.get("company"):
                    continue
                stem = Path(resume_pdf_filename(job)).stem
                suffix = ""
                if parent != run_dir:
                    suffix = "_" + re.sub(r"[^A-Za-z0-9_-]+", "_", run_dir.name)
                pdf = run_dir / (stem + suffix + ".pdf")
                break
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
    assert_resume_workspace(run_dir)
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
        "one_more_bullet_fits": False,
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
    reference_pdf = cv_root(repo_root()) / CANONICAL_PDF
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
            result["one_more_bullet_fits"] = bool(
                (result.get("vertical_capacity") or {}).get("one_more_bullet_fits")
            )
    if result["overfull"]:
        result["warnings"].append("LaTeX log contains an overfull box or fatal layout warning")
    if result["pages"] != 1:
        result["warnings"].append("strict one-page target is not met")
    if not result["text_extractable"]:
        result["warnings"].append("PDF text is not extractable")
    if result["density_gap_pt"] is not None and not result["density_pass"]:
        result["warnings"].append(
            "informational: bottom clearance differs from the human reference; no filler was added"
        )
    return result


def measured_space_available(layout: Dict[str, Any]) -> bool:
    """Read current and legacy capacity-test shapes without trusting whitespace."""
    capacity = layout.get("vertical_capacity") or {}
    if capacity.get("one_more_bullet_fits") or layout.get("one_more_bullet_fits"):
        return True
    # Runs made just before the explicit boolean was added recorded the same
    # result as ``qa_pages=1`` plus this warning. Keep those saved runs
    # auditable and make future reruns use the same decision.
    warning = str(capacity.get("warning") or "").lower()
    return capacity.get("qa_pages") == 1 and "one more bullet still fits" in warning


def space_audit(
    plan: Dict[str, Any], layout: Dict[str, Any], catalog: Dict[str, Any],
    expansion: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Expose exactly why a page was or was not filled further."""
    entries = catalog.get("entries") or {}
    selected_ids = _selected_bullet_ids(plan)
    selected_entries = {
        str(entry.get("source_id") or "")
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, []) or []
    }
    unused = []
    for entry_id, entry in entries.items():
        entry_id = str(entry_id)
        placement = "append_bullet" if entry_id in selected_entries else "new_entry"
        selected_texts = [
            _latex_plain(str(bullet.get("text") or ""))
            for section in ("experiences", "projects", "leadership")
            for selected_entry in plan.get(section, []) or []
            if str(selected_entry.get("source_id") or "") == entry_id
            for bullet in selected_entry.get("bullets", []) or []
        ]
        for bullet in entry.get("bullets", []) or []:
            bullet_id = str(bullet.get("id") or "")
            if not bullet_id or bullet_id in selected_ids:
                continue
            text = _latex_plain(str(bullet.get("text") or ""))
            if len(text) < MIN_MEANINGFUL_BULLET_CHARS:
                continue
            if any(_same_entry_resume_bullet(text, selected) for selected in selected_texts):
                continue
            unused.append({
                "source_id": bullet_id,
                "entry_id": entry_id,
                "placement": placement,
                "kind": entry.get("kind"),
                "text": text,
            })
    unused.sort(key=lambda item: (-_bullet_value(item), item["source_id"]))
    expansion = expansion or {}
    applied = list(expansion.get("applied") or [])
    replaced = list(expansion.get("replaced") or [])
    one_more_fits = measured_space_available(layout)
    density_gap = layout.get("density_gap_pt")
    space_review_needed = one_more_fits or (
        isinstance(density_gap, (int, float)) and density_gap > MAX_DENSITY_GAP_PT
    )
    return {
        "measured_content_bottom_pt": layout.get("content_bottom_pt"),
        "canonical_reference_bottom_pt": layout.get("reference_content_bottom_pt"),
        "density_gap_pt": layout.get("density_gap_pt"),
        "max_density_gap_pt": MAX_DENSITY_GAP_PT,
        "density_pass": bool(layout.get("density_pass")),
        "one_more_standard_bullet_fits": one_more_fits,
        "space_review_needed": space_review_needed,
        "unused_candidate_count": len(unused),
        "unused_verified_candidates": unused[:12],
        "expansion_attempted": bool(expansion.get("attempted")),
        "expansion_candidate_count": int(expansion.get("candidate_count") or 0),
        "expansion_applied": applied,
        "expansion_replaced": replaced,
        "expansion_rejected": list(expansion.get("rejected") or []),
        "decision": str(expansion.get("decision") or (
            "Measured bottom clearance exceeds the density target; the expansion pass should evaluate verified unused evidence."
            if space_review_needed and not one_more_fits else
            "A further standard line did not fit the measured bottom capacity."
            if not one_more_fits else
            "A further line fits; the expansion pass should evaluate verified unused evidence."
        )),
    }


def deterministic_review(
    job: Dict[str, Any], tex: str, layout: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None, catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    warnings = list(layout.get("warnings", []))
    policy = plan.get("front_matter_policy") if isinstance(plan, dict) else None
    style = (
        template_style_guard(tex, repo_root(), policy)
        if isinstance(policy, dict)
        else template_style_guard(tex, repo_root())
    )
    if not style.get("passed"):
        warnings.append("generated resume changed or bypassed the canonical CV/immutable/VictorJimenezResume.tex formatting")
    company = str(job.get("company", "")).lower()
    if "Victor Jimenez" not in tex or "vmj@njit.edu" not in tex:
        warnings.append("canonical owner name/contact header is missing")
    if _contains_forbidden_resume_term(tex):
        warnings.append("a permanently excluded resume term appears in the draft")
    layout_gate = (
        layout.get("compiled")
        and layout.get("pages") == 1
        and not layout.get("overfull")
        and (layout.get("horizontal") or {}).get("pass")
        and style.get("passed")
    )
    portfolio = layout.get("portfolio") or portfolio_metrics({})
    portfolio_gate = bool(portfolio.get("pass"))
    portfolio_diagnostics_value = (
        portfolio_diagnostics(plan, catalog)
        if isinstance(plan, dict) and isinstance(catalog, dict)
        else {}
    )
    portfolio_warnings = list(portfolio_diagnostics_value.get("blocking_warnings") or [])
    if portfolio_warnings:
        portfolio_gate = False
    eligibility_blocks = posting_eligibility_blocks(str(job.get("posting_text") or ""))
    if eligibility_blocks:
        eligibility = {"status": "fail", "reason": "; ".join(eligibility_blocks)}
    elif job.get("alert_ok"):
        eligibility = {"status": "pass", "reason": "Radar verifies the role as new-grad/eligible"}
    elif job.get("early_career_possible"):
        eligibility = {"status": "partial", "reason": "Early-career possible; posting eligibility needs confirmation"}
    else:
        eligibility = {"status": "partial", "reason": "Resume Studio does not independently verify posting eligibility"}
    factual_status = "fail" if _contains_forbidden_resume_term(tex) else "pass"
    return {
        "rubric_version": RUBRIC_VERSION,
        "hard_fail": not layout_gate,
        "warnings": warnings,
        "layout": layout,
        "style": style,
        "gates": {
            "factual": {
                "status": factual_status,
                "reason": "permanently excluded resume term detected" if factual_status == "fail" else "no deterministic forbidden claim detected",
            },
            "layout": {"status": "pass" if layout_gate else "fail", "reason": "; ".join(warnings) or "all rendered layout checks passed"},
            "portfolio": {
                "status": "pass" if portfolio_gate else "fail",
                "reason": "; ".join(
                    list(portfolio.get("violations") or []) + portfolio_warnings
                ) or "portfolio is compact and nonredundant",
            },
            "eligibility": eligibility,
        },
        "portfolio_diagnostics": portfolio_diagnostics_value,
    }


def score_review(
    agent_review: Dict[str, Any], deterministic: Dict[str, Any],
    independent_available: bool = False,
) -> Dict[str, Any]:
    """Combine independent critique and deterministic gates without self-scoring.

    This intentionally returns no composite craft score.  A draft may be
    useful while still awaiting Victor or an independent provider; that state
    must remain visible instead of being converted into a flattering number.
    """
    data = agent_review.get("data") or {}
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else data
    unsupported = data.get("unsupported_claims", [])
    if not isinstance(unsupported, list):
        unsupported = [str(unsupported)]
    decision_feedback = data.get("decision_feedback", [])
    if not isinstance(decision_feedback, list):
        decision_feedback = []
    portfolio_comparison = data.get("portfolio_comparison")
    if not isinstance(portfolio_comparison, dict):
        portfolio_comparison = {
            "status": "unknown",
            "reason": "independent portfolio comparison was not returned",
            "preserved_strengths": [],
            "gained_strengths": [],
            "lost_strengths": [],
        }
    deterministic_gates = deterministic.get("gates") or {}
    gates = {}
    for name in REVIEW_CRITERIA:
        raw = criteria.get(name, {}) if isinstance(criteria, dict) else {}
        status = raw.get("status") if isinstance(raw, dict) else raw
        status = str(status or "fail").lower()
        if status not in STATUS_MULTIPLIER:
            status = "fail"
        reason = raw.get("reason", "") if isinstance(raw, dict) else ""
        gates[name] = {"status": status, "reason": reason}
    if unsupported:
        gates["factual"] = {"status": "fail", "reason": "independent critic reported unsupported claims"}
    if "factual" in deterministic_gates and deterministic_gates["factual"].get("status") == "fail":
        gates["factual"] = deterministic_gates["factual"]
    gates["layout"] = deterministic_gates.get("layout", {"status": "fail", "reason": "layout unavailable"})
    gates["portfolio"] = deterministic_gates.get("portfolio", {"status": "fail", "reason": "portfolio unavailable"})
    if "eligibility" in deterministic_gates:
        gates["eligibility"] = deterministic_gates["eligibility"]
    gates["independent_review"] = {
        "status": "pass" if independent_available else "fail",
        "reason": "independent provider critique completed" if independent_available else "no independent provider critique was available",
    }
    comparison_status = str(portfolio_comparison.get("status") or "unknown").lower()
    if comparison_status not in {"pass", "partial", "fail"}:
        comparison_status = "fail"
    gates["portfolio_comparison"] = {
        "status": comparison_status,
        "reason": str(portfolio_comparison.get("reason") or "portfolio comparison unavailable"),
    }
    blocking = data.get("blocking_issues", [])
    if not isinstance(blocking, list):
        blocking = [str(blocking)]
    required_gates = ("factual", "target_fit", "evidence", "distinctiveness", "clarity", "privacy", "layout", "portfolio", "independent_review", "portfolio_comparison")
    hard_fail = bool(blocking) or any(gates.get(name, {}).get("status") != "pass" for name in required_gates)
    return {
        "rubric_version": RUBRIC_VERSION,
        "craft_score": None,
        "score": None,
        "ready": not hard_fail,
        "hard_fail": hard_fail,
        "gates": gates,
        "unsupported_claims": unsupported,
        "missing_evidence": data.get("missing_evidence", []),
        "revision_priorities": data.get("revision_priorities", []),
        "blocking_issues": blocking,
        "line_feedback": data.get("line_feedback", []),
        "decision_feedback": decision_feedback[:20],
        "portfolio_comparison": portfolio_comparison,
        "reviewer": agent_review.get("provider"),
        "independent_review": independent_available,
        "deterministic": deterministic,
    }


def make_report(run_dir: Path, payload: Dict[str, Any]) -> None:
    write_json(run_dir / "report.json", payload)
    status_path = run_dir / "status.json"
    current = read_json(status_path, {}) or {}
    current["report"] = payload
    write_json(status_path, current)


def approve_run(root: Optional[Path], run_id: str) -> Dict[str, Any]:
    """Promote a privately rendered draft only after the owner checkpoint.

    Provider output and a compiled PDF are still drafts until Victor approves
    the gate report. This endpoint changes only private run metadata; it never
    writes a canonical CV file or copies anything into ``CV/immutable``.
    """
    run_dir = _workshop_run_dir(root or repo_root(), run_id)
    if run_dir is None:
        raise ValueError("run not found")
    status = read_json(run_dir / "status.json", {}) or {}
    report = read_json(run_dir / "report.json", {}) or status.get("report") or {}
    if not isinstance(report, dict):
        raise ValueError("run has no review report")
    if str(status.get("status") or "") == "complete" and report.get("approval_state") == "approved":
        return status
    if str(status.get("status") or "") != "awaiting_review":
        raise ValueError("run is not waiting for owner review")
    review = report.get("review") if isinstance(report.get("review"), dict) else {}
    if review.get("ready") is not True:
        raise ValueError("draft cannot be approved while one or more quality gates fail")
    if not run_pdf_path(run_dir).is_file():
        raise ValueError("run has no rendered PDF")
    approved_at = now_iso()
    report["approval_state"] = "approved"
    report["approved_by"] = "Victor"
    report["approved_at"] = approved_at
    make_report(run_dir, report)
    current = read_json(run_dir / "status.json", {}) or status
    current.update({
        "status": "complete",
        "step": "approved",
        "message": "Victor approved the draft after reviewing its gate report",
        "updated_at": approved_at,
        "approval_state": "approved",
        "approved_by": "Victor",
        "approved_at": approved_at,
        "report": report,
    })
    write_json(run_dir / "status.json", current)
    return current


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
    run_started_clock = time.time()
    run_started_at = now_iso()
    update("context", "Fetching the posting and preparing the private CV context")
    context = job_context(job)
    catalog = source_catalog(repo_root())
    graph = evidence_graph(repo_root())
    graph_context = evidence_context(graph, context, str(context.get("posting_text") or ""))
    match = resume_match_for_job(job, repo_root(), posting_text=str(context.get("posting_text") or ""))
    context["resume_match"] = match
    context["target_keywords"] = target_keyword_strategy(context, catalog, repo_root())
    context["provider_policy"] = {
        "allowed_lanes": [name for name, path in provider_commands().items() if path],
        "codex_model": CODEX_LUNA_MODEL,
        "local_models_allowed": False,
        "api_fallback_allowed": False,
    }
    write_json(run_dir / "job_context.json", context)
    write_json(run_dir / "evidence_catalog.json", catalog_for_prompt(catalog))
    write_json(run_dir / "evidence_graph_context.json", graph_context)
    markdown_sources = sorted({
        str(node.get("source") or "")
        for node in graph.get("nodes", [])
        if str(node.get("source") or "").lower().endswith(".md")
    })
    write_json(run_dir / "brief.json", {
        "positioning_thesis": "",
        "job": job_summary(job),
        "posting_text_available": bool(context.get("posting_text")),
        "target_keywords": context.get("target_keywords"),
        "provider_policy": context["provider_policy"],
        "evidence_graph": {
            "version": graph.get("version"),
            "hash": graph.get("hash"),
            "review_summary": graph.get("review_summary") or {},
            "markdown_sources": markdown_sources,
        },
    })
    mode_label = "unrestricted" if unrestricted else "enhanced" if enhance else "source-only"
    prompt = base_prompt(
        context, "an independent resume evidence strategist", catalog, enhance,
        graph=graph, unrestricted=unrestricted,
    )
    schema = plan_schema(enhance)
    available = [name for name, path in provider_commands().items() if path]
    if not available:
        raise RuntimeError("No approved Codex or Claude Code subscription CLI is installed")
    update("drafting", "Building adaptive %s evidence plans with: %s" % (mode_label, ", ".join(available)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(available)) as pool:
        futures = {
            pool.submit(run_provider, provider, prompt, run_dir, "draft", RUN_TIMEOUT_SECONDS, schema): provider
            for provider in available
        }
        drafts = [future.result() for future in concurrent.futures.as_completed(futures)]
    for draft in drafts:
        draft["label"] = "draft"
        write_json(run_dir / (draft.get("provider", "unknown") + "_draft.json"), draft)
    successful = [draft for draft in drafts if draft.get("ok")]
    if not successful:
        raise RuntimeError("Approved provider lanes returned no usable evidence plan; inspect *_draft.json")

    writer = "codex" if any(item.get("provider") == "codex" for item in successful) else successful[0].get("provider")
    update("synthesis", "Codex is synthesizing the strongest adaptive evidence plan")
    if len(successful) == 1 and successful[0].get("provider") == writer:
        synthesis = {
            "provider": writer, "ok": True, "skipped": True,
            "reason": "Only one usable planning lane was available; its plan was preserved for independent-review status.",
            "data": successful[0].get("data") or {},
        }
    else:
        synthesis = run_provider(
            writer,
            synthesis_prompt(context, successful, catalog, enhance, graph=graph, unrestricted=unrestricted),
            run_dir, "synthesis", timeout=4 * 60, schema=schema,
        )
    synthesis["label"] = "synthesis"
    write_json(run_dir / "synthesis.json", synthesis)
    candidates = [synthesis] + successful
    candidate_plan, plan_errors, _ = _select_valid_plan(candidates, catalog, enhance, graph=graph)
    if candidate_plan is None:
        write_json(run_dir / "plan_errors.json", plan_errors)
        raise RuntimeError("No provider returned a valid adaptive source-addressed plan; inspect plan_errors.json")
    candidate_plan = expand_candidate_portfolio(candidate_plan, catalog, enhance)
    write_json(run_dir / "candidate_plan.json", candidate_plan)
    write_json(run_dir / "brief.json", {
        "positioning_thesis": candidate_plan.get("positioning_thesis", ""),
        "selected_evidence": candidate_plan.get("selected_evidence", []),
        "excluded_evidence": candidate_plan.get("excluded_evidence", []),
        "revision_notes": candidate_plan.get("revision_notes", []),
        "decision_ledger": candidate_plan.get("decision_ledger", []),
        "front_matter_policy": candidate_plan.get("front_matter_policy", {"coursework": "keep", "awards": "keep"}),
        "job": job_summary(job),
        "target_keywords": context.get("target_keywords"),
        "provider_policy": context["provider_policy"],
        "evidence_graph": {
            "version": graph.get("version"),
            "hash": graph.get("hash"),
            "review_summary": graph.get("review_summary") or {},
            "markdown_sources": markdown_sources,
        },
    })

    def render_candidate(value: Dict[str, Any], attempt_root: Path) -> Tuple[str, Dict[str, Any], Optional[str]]:
        attempt_root.mkdir(parents=True, exist_ok=True)
        tex_value = render_plan(value, catalog, repo_root())
        (attempt_root / "resume.tex").write_text(tex_value)
        compiled_value = compile_resume(attempt_root)
        layout_value = pdf_layout(attempt_root, compiled_value, plan=value)
        preview_value = render_preview(attempt_root)
        return tex_value, layout_value, preview_value

    update("packing", "Packing the strongest role-specific evidence without portfolio floors")
    plan, packing = pack_plan_to_page(candidate_plan, catalog, run_dir)
    write_json(run_dir / "content_plan.json", plan)
    write_json(run_dir / "layout_packing.json", packing)
    chosen, layout, preview = render_candidate(plan, run_dir)
    space_expansion_records: List[Dict[str, Any]] = []
    space_expansion: Dict[str, Any] = {
        "attempted": False,
        "candidate_count": 0,
        "applied": [],
        "rejected": [],
        "decision": "No expansion was needed because the measured page had no spare standard line.",
    }
    if enhance and (
        measured_space_available(layout)
        or (isinstance(layout.get("density_gap_pt"), (int, float)) and layout["density_gap_pt"] > MAX_DENSITY_GAP_PT)
    ):
        update("space_review", "Measured spare page capacity; asking Codex to fill it with verified unused evidence")
        expansion_record = run_provider(
            writer,
            space_expansion_prompt(context, plan, layout, catalog, graph=graph, unrestricted=unrestricted),
            run_dir,
            "space_expansion",
            timeout=4 * 60,
            schema=space_expansion_schema(),
        )
        expansion_record["label"] = "space_expansion"
        space_expansion_records.append(expansion_record)
        write_json(run_dir / "space_expansion.json", expansion_record)
        additions, expansion_errors = _validate_space_additions(
            expansion_record.get("data") or {}, plan, catalog, graph=graph,
        ) if expansion_record.get("ok") else ([], [str(expansion_record.get("error") or "space expansion provider failed")])
        expanded_plan, space_expansion = expand_into_measured_space(
            plan, additions, catalog, graph, run_dir,
        ) if additions else (plan, {
            "attempted": True,
            "candidate_count": 0,
            "applied": [],
            "rejected": [{"source_id": "", "reason": error} for error in expansion_errors[:8]],
            "decision": str((expansion_record.get("data") or {}).get("decision") or "No verified unused line was returned for the available space."),
        })
        if additions and expansion_errors:
            space_expansion.setdefault("validation_errors", []).extend(expansion_errors[:8])
        if space_expansion.get("applied"):
            plan = expanded_plan
            packing["space_expansion"] = space_expansion
            write_json(run_dir / "content_plan.json", plan)
            write_json(run_dir / "layout_packing.json", packing)
            chosen, layout, preview = render_candidate(plan, run_dir)
        else:
            packing["space_expansion"] = space_expansion
            write_json(run_dir / "layout_packing.json", packing)
    # Provider output is judgment-first, but it cannot be allowed to leave a
    # measured window empty simply because it returned no addition. Use the
    # source catalog as a deterministic last-mile fallback, then compile every
    # candidate trial. Existing selected entries are preferred; new entries
    # arrive only as atomic two-bullet groups.
    if enhance and (
        measured_space_available(layout)
        or (isinstance(layout.get("density_gap_pt"), (int, float)) and layout["density_gap_pt"] > MAX_DENSITY_GAP_PT)
    ):
        fallback_additions = deterministic_space_additions(
            plan, catalog, graph=graph, keyword_strategy=context.get("target_keywords")
        )
        if fallback_additions:
            fallback_plan, fallback_result = expand_into_measured_space(
                plan, fallback_additions, catalog, graph, run_dir / "deterministic_space_fallback",
            )
            space_expansion["fallback_candidates"] = len(fallback_additions)
            space_expansion["fallback_rejected"] = list(fallback_result.get("rejected") or [])
            space_expansion["replaced"] = list(space_expansion.get("replaced") or []) + list(fallback_result.get("replaced") or [])
            if fallback_result.get("applied"):
                plan = fallback_plan
                space_expansion["applied"] = list(space_expansion.get("applied") or []) + list(fallback_result["applied"])
                space_expansion["decision"] = "Filled measured page capacity with model-selected and deterministic verified evidence until the next compiled trial failed."
                packing["space_expansion"] = space_expansion
                write_json(run_dir / "content_plan.json", plan)
                write_json(run_dir / "layout_packing.json", packing)
                write_json(run_dir / "space_expansion_fallback.json", fallback_result)
                chosen, layout, preview = render_candidate(plan, run_dir)
    line_edits: List[Dict[str, Any]] = []
    line_compactions: List[Dict[str, Any]] = []
    editable_pool = copy.deepcopy(plan)
    for line_round in range(1, MAX_LINE_EDIT_PASSES + 1):
        if not enhance or (layout.get("horizontal") or {}).get("pass"):
            break
        label = "line_edit" if line_round == 1 else "line_edit_%s" % line_round
        update("line_editing", "Repairing rendered one-line geometry (pass %s/%s)" % (line_round, MAX_LINE_EDIT_PASSES))
        line_edit = run_provider(
            writer, line_editor_prompt(context, plan, layout, graph), run_dir, label,
            timeout=6 * 60, schema=plan_schema(True),
        )
        line_edits.append(line_edit)
        line_edit["label"] = label
        write_json(run_dir / (label + ".json"), line_edit)
        if not line_edit.get("ok"):
            break
        edited, edit_errors = validate_plan(line_edit.get("data") or {}, catalog, True, graph=graph)
        if edit_errors or _plan_source_signature(edited) != _plan_source_signature(plan):
            write_json(run_dir / (label + "_errors.json"), edit_errors or ["line editor changed selected source IDs"])
            break
        editable_pool = merge_edited_bullets(editable_pool, edited)
        try:
            candidate_plan, line_packing = pack_plan_to_page(
                editable_pool, catalog, run_dir / (label + "_pack")
            )
        except (OSError, RuntimeError, ValueError) as exc:
            # A provider can satisfy the structured plan schema while still
            # returning malformed inline LaTeX (for example an unmatched
            # brace). Reject only this editorial candidate and preserve the
            # last valid rendered plan; a bad line edit must never erase a
            # good density-expanded resume.
            write_json(run_dir / (label + "_errors.json"), [
                "line editor candidate rejected during compiled packing: %s" % exc,
            ])
            line_edit["compile_rejected"] = str(exc)
            break
        plan = candidate_plan
        packing[label + "_pack"] = line_packing
        write_json(run_dir / "content_plan.json", plan)
        write_json(run_dir / "layout_packing.json", packing)
        chosen, layout, preview = render_candidate(plan, run_dir)

    if enhance and not (layout.get("horizontal") or {}).get("pass"):
        update("line_compacting", "Applying measured conservative fallbacks to remaining tight lines")
        compacted, compact_layout, line_compactions = compact_plan_to_geometry(
            plan, layout, catalog, run_dir,
        )
        if line_compactions:
            plan = compacted
            packing["line_compaction"] = {
                "applied": line_compactions,
                "horizontal": compact_layout.get("horizontal", {}),
            }
            write_json(run_dir / "content_plan.json", plan)
            write_json(run_dir / "layout_packing.json", packing)
            chosen, layout, preview = render_candidate(plan, run_dir)
            write_json(run_dir / "line_compaction.json", line_compactions)

    def combined_critique(records: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not records:
            return {"provider": "", "ok": False, "data": {
                "criteria": {name: {"status": "fail", "reason": "no independent critic available"} for name in REVIEW_CRITERIA},
                "blocking_issues": ["Independent review was unavailable."],
                "line_feedback": [], "unsupported_claims": [], "missing_evidence": [],
                "revision_priorities": ["Obtain an independent provider critique before calling this ready."],
                "decision_feedback": [],
                "portfolio_comparison": {
                    "status": "unknown",
                    "reason": "independent portfolio comparison was unavailable",
                    "preserved_strengths": [],
                    "gained_strengths": [],
                    "lost_strengths": [],
                },
            }}
        data = copy.deepcopy(records[0].get("data") or {})
        data.setdefault("blocking_issues", [])
        data.setdefault("line_feedback", [])
        data.setdefault("unsupported_claims", [])
        data.setdefault("missing_evidence", [])
        data.setdefault("revision_priorities", [])
        data.setdefault("decision_feedback", [])
        data.setdefault("portfolio_comparison", {
            "status": "unknown",
            "reason": "missing independent portfolio comparison",
            "preserved_strengths": [],
            "gained_strengths": [],
            "lost_strengths": [],
        })
        criteria = data.setdefault("criteria", {})
        for record in records[1:]:
            other = record.get("data") or {}
            data["blocking_issues"].extend(other.get("blocking_issues") or [])
            data["line_feedback"].extend(other.get("line_feedback") or [])
            data["unsupported_claims"].extend(other.get("unsupported_claims") or [])
            data["missing_evidence"].extend(other.get("missing_evidence") or [])
            data["revision_priorities"].extend(other.get("revision_priorities") or [])
            data["decision_feedback"].extend(other.get("decision_feedback") or [])
            current_comparison = data.get("portfolio_comparison") or {}
            other_comparison = other.get("portfolio_comparison") or {}
            if str(other_comparison.get("status") or "") == "fail" or (
                str(current_comparison.get("status") or "") == "unknown"
                and other_comparison
            ):
                data["portfolio_comparison"] = copy.deepcopy(other_comparison)
            for name in REVIEW_CRITERIA:
                left = criteria.get(name) or {"status": "fail", "reason": "missing critique"}
                right = (other.get("criteria") or {}).get(name) or {"status": "fail", "reason": "missing critique"}
                order = {"fail": 0, "partial": 1, "pass": 2}
                if order.get(str(right.get("status")), 0) < order.get(str(left.get("status")), 0):
                    criteria[name] = right
                elif right.get("reason") and right.get("reason") not in str(left.get("reason") or ""):
                    left["reason"] = "; ".join(value for value in (str(left.get("reason") or ""), str(right.get("reason") or "")) if value)
                    criteria[name] = left
        for key in ("blocking_issues", "unsupported_claims", "missing_evidence", "revision_priorities"):
            data[key] = list(dict.fromkeys(str(value) for value in data.get(key) or []))
        data["line_feedback"] = list({json.dumps(item, sort_keys=True): item for item in data.get("line_feedback") or []}.values())[:20]
        data["decision_feedback"] = list({
            json.dumps(item, sort_keys=True): item
            for item in data.get("decision_feedback") or []
            if isinstance(item, dict)
        }.values())[:20]
        comparison = data.get("portfolio_comparison") or {}
        for field in ("preserved_strengths", "gained_strengths", "lost_strengths"):
            if not isinstance(comparison.get(field), list):
                comparison[field] = []
        data["portfolio_comparison"] = comparison
        return {"provider": "+".join(str(item.get("provider") or "") for item in records), "ok": True, "data": data}

    def critique_current(round_label: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
        critic_lanes = [name for name in ("claude",) if name in available and name != writer]
        if not critic_lanes:
            critique = combined_critique([])
            write_json(run_dir / (round_label + ".json"), critique)
            return critique, [], False
        update("reviewing", "Running independent critique lanes: %s" % ", ".join(critic_lanes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(critic_lanes)) as pool:
            futures = {
                pool.submit(
                    run_provider, provider,
                    reviewer_prompt(context, chosen, plan=plan, graph_context=graph_context, catalog=catalog, unrestricted=unrestricted),
                    run_dir, round_label + "_" + provider, timeout=8 * 60, schema=review_schema(),
                ): provider for provider in critic_lanes
            }
        records = [future.result() for future in concurrent.futures.as_completed(futures)]
        for record in records:
            record["label"] = round_label + "_" + str(record.get("provider") or "unknown")
            write_json(run_dir / (round_label + "_" + str(record.get("provider") or "unknown") + ".json"), record)
        usable = [record for record in records if record.get("ok")]
        critique = combined_critique(usable)
        write_json(run_dir / (round_label + ".json"), critique)
        return critique, usable, bool(usable)

    critique, critique_records, independent_available = critique_current("critique")
    revision_records: List[Dict[str, Any]] = []
    revision_log: List[Dict[str, Any]] = []
    for revision_round in range(1, 3):
        critique_data = critique.get("data") or {}
        statuses = [str((critique_data.get("criteria") or {}).get(name, {}).get("status") or "fail") for name in REVIEW_CRITERIA]
        if not enhance or not independent_available or (not critique_data.get("blocking_issues") and all(status == "pass" for status in statuses)):
            break
        label = "revision" if revision_round == 1 else "revision_%s" % revision_round
        update("revising", "Codex is applying independent critique (pass %s/2)" % revision_round)
        revision = run_provider(
            writer, revision_prompt(context, plan, critique, catalog, graph=graph, unrestricted=unrestricted),
            run_dir, label, timeout=8 * 60, schema=plan_schema(True),
        )
        revision_records.append(revision)
        revision["label"] = label
        write_json(run_dir / (label + ".json"), revision)
        if not revision.get("ok"):
            revision_log.append({"round": revision_round, "status": "failed", "reason": revision.get("error", "provider failed")})
            break
        revised_plan, revision_errors = validate_plan(revision.get("data") or {}, catalog, True, graph=graph)
        if revision_errors:
            revision_log.append({"round": revision_round, "status": "rejected", "errors": revision_errors})
            write_json(run_dir / (label + "_errors.json"), revision_errors)
            break
        plan, revision_packing = pack_plan_to_page(revised_plan, catalog, run_dir / (label + "_pack"))
        packing[label + "_pack"] = revision_packing
        write_json(run_dir / "content_plan.json", plan)
        write_json(run_dir / "layout_packing.json", packing)
        chosen, layout, preview = render_candidate(plan, run_dir)
        revision_log.append({"round": revision_round, "status": "applied", "provider": writer})
        critique, new_records, independent_available = critique_current(label + "_critique")
        critique_records.extend(new_records)
    write_json(run_dir / "revision_log.json", revision_log)
    deterministic = deterministic_review(context, chosen, layout, plan=plan, catalog=catalog)
    if not (layout.get("horizontal") or {}).get("pass"):
        rejection = {
            "reason": "Final resume rejected by the hard one-line bullet gate",
            "safe_right_slack_pt": MIN_RIGHT_SLACK_PT,
            "wrap_count": (layout.get("horizontal") or {}).get("wrap_count", 0),
            "near_wrap_count": (layout.get("horizontal") or {}).get("near_wrap_count", 0),
            "bullets": [item for item in (layout.get("horizontal") or {}).get("bullets", []) if item.get("wraps") or item.get("near_wrap") or not item.get("horizontal_pass")],
        }
        write_json(run_dir / "layout_rejection.json", rejection)
        raise RuntimeError("final resume rejected: %s wrap(s), %s near-wrap(s); see layout_rejection.json" % (rejection["wrap_count"], rejection["near_wrap_count"]))
    scored = score_review(critique, deterministic, independent_available=independent_available)
    synthesis_data = plan
    provider_records = []
    all_provider_records = (
        drafts + [synthesis] + space_expansion_records + line_edits
        + revision_records + critique_records
    )
    for record in all_provider_records:
        provider = str(record.get("provider") or "")
        provider_records.append({
            "label": str(record.get("label") or "provider"),
            "provider": provider,
            "model": provider_model_label(provider),
            "ok": record.get("ok"), "called": not record.get("skipped", False),
            "elapsed_seconds": record.get("elapsed_seconds"), "usage_tokens": record.get("usage_tokens"),
        })
    known_by_provider = {name: [int(item.get("usage_tokens")) for item in provider_records if item.get("called") and str(item.get("provider") or "").split("/")[-1] == name and item.get("usage_tokens") is not None] for name in ("codex", "claude")}
    changes = content_change_report(plan, catalog, chosen, context.get("target_keywords"))
    review_overlay = review_preview_overlay(
        run_pdf_path(run_dir), plan, changes, changes.get("keyword_coverage")
    )
    space_audit_value = space_audit(plan, layout, catalog, space_expansion)
    provider_flow = [
        {
            "label": item.get("label"),
            "provider": item.get("provider"),
            "model": item.get("model"),
            "status": "complete" if item.get("ok") else "failed" if item.get("called") else "skipped",
            "elapsed_seconds": item.get("elapsed_seconds"),
            "usage_tokens": item.get("usage_tokens"),
        }
        for item in provider_records
    ]
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
        "decision_ledger": synthesis_data.get("decision_ledger", []),
        "front_matter_policy": synthesis_data.get("front_matter_policy", {"coursework": "keep", "awards": "keep"}),
        "line_compactions": line_compactions,
        "validation_warnings": synthesis_data.get("validation_warnings", []),
        "content_changes": changes,
        "review_overlay": review_overlay,
        "space_audit": space_audit_value,
        "provider_flow": provider_flow,
        "run_metrics": {
            "started_at": run_started_at,
            "finished_at": now_iso(),
            "elapsed_seconds": round(time.time() - run_started_clock, 1),
        },
        "portfolio_diagnostics": changes.get("portfolio_diagnostics", {}),
        "owner_summary": owner_change_summary(plan, catalog, changes),
        "content_plan": {section: synthesis_data.get(section, []) for section in ("experiences", "projects", "leadership")},
        "layout_packing": packing,
        "format_contract": {"template": "CV/" + CANONICAL_TEMPLATE, "model_can_write_latex_document": False, "font_size_reduction_percent": 0.0, "font_size_increase_percent": 0.0, "allowed_max_reduction_percent": MAX_STYLE_REDUCTION_PERCENT},
        "providers": provider_records,
        "provider_policy": context["provider_policy"],
        "evidence_graph": {
            "version": graph.get("version"),
            "hash": graph.get("hash"),
            "review_summary": graph.get("review_summary") or {},
            "markdown_sources": markdown_sources,
        },
        "usage": {
            **{name + "_tokens": sum(values) for name, values in known_by_provider.items()},
            **{name + "_calls": sum(1 for item in provider_records if item.get("called") and str(item.get("provider") or "").split("/")[-1] == name) for name in ("codex", "claude")},
            "codex_complete": all(item.get("usage_tokens") is not None for item in provider_records if item.get("called") and str(item.get("provider") or "").split("/")[-1] == "codex"),
        },
        "review": scored,
        "critique": critique,
        "independent_review": {"available": independent_available, "providers": [item.get("provider") for item in critique_records]},
        "approval_state": "awaiting_review",
        "artifacts": [
            "resume.tex", run_pdf_path(run_dir).name, "resume.txt", run_preview_path(run_dir).name if preview else None,
            "job.json", "report.json", "job_context.json", "brief.json", "evidence_catalog.json", "evidence_graph_context.json",
            "candidate_plan.json", "content_plan.json", "layout_packing.json", "critique.json", "revision_log.json",
            "space_expansion.json" if space_expansion_records else None,
            *[("line_edit.json" if index == 1 else "line_edit_%s.json" % index) for index in range(1, len(line_edits) + 1)],
            *[("revision.json" if index == 1 else "revision_%s.json" % index) for index in range(1, len(revision_records) + 1)],
        ],
    }
    report["artifacts"] = [artifact for artifact in report["artifacts"] if artifact]
    make_report(run_dir, report)
    _workshop_state(run_dir, catalog)
    update("awaiting_review", "Draft and independent critique are ready for Victor's review", report=report)


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
        started_clock = time.time()
        started_at = now_iso()
        self.update(
            run_id, run_dir, "running", "starting", "Starting the approved tailoring lanes",
            started_at=started_at,
        )

        def update(step: str, message: str, **extra: Any) -> None:
            status = (
                "complete" if step == "complete"
                else "awaiting_review" if step == "awaiting_review"
                else "running"
            )
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
        finally:
            current = read_json(run_dir / "status.json", {}) or {}
            self.update(
                run_id,
                run_dir,
                str(current.get("status") or "failed"),
                str(current.get("step") or "error"),
                str(current.get("message") or "Run ended"),
                finished_at=now_iso(),
                elapsed_seconds=round(time.time() - started_clock, 1),
            )

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
        report = read_json(path.parent / "report.json", {}) or {}
        if isinstance(report, dict) and report:
            # Backfill the review-only presentation for runs created before
            # the visual audit shipped.  This is intentionally in-memory:
            # opening an old run must not rewrite its historical report.
            if (
                not report.get("review_overlay")
                and isinstance(report.get("content_plan"), dict)
                and physical.is_file()
            ):
                try:
                    catalog = source_catalog(self.root)
                    context = read_json(path.parent / "job_context.json", {}) or {}
                    tex = (path.parent / "resume.tex").read_text(errors="replace")
                    refreshed_changes = content_change_report(
                        report["content_plan"], catalog, tex, context.get("target_keywords")
                    )
                    report["content_changes"] = refreshed_changes
                    report["owner_summary"] = owner_change_summary(
                        report["content_plan"], catalog, refreshed_changes
                    )
                    report["review_overlay"] = review_preview_overlay(
                        physical,
                        report["content_plan"],
                        refreshed_changes,
                        refreshed_changes.get("keyword_coverage"),
                    )
                except (OSError, KeyError, TypeError, ValueError):
                    # Historical reports remain viewable even if their source
                    # catalog or local rendering tools are no longer present.
                    pass
            value["report"] = report
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
let selected=null,activeRunId=null,libraryEntries=[],runTimers=new Map(),jobsCache=[],workshopState=null,workshopSuggestions=[],studioUsage=null;
const $=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtDate(value){if(!value)return 'date unavailable';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString([], {dateStyle:'medium',timeStyle:'short'});}
function modeLabel(mode){return ({used:'Used bullets',strict:'Used bullets','source-only':'Used bullets',ai:'AI tailor',dream:'AI tailor',enhanced:'AI tailor',unrestricted:'Take-the-wheel'})[mode]||'Tailor';}
function artifactUrl(source,id,name){return '/artifacts/'+encodeURIComponent(source)+'/'+encodeURIComponent(id)+'/'+encodeURIComponent(name);}
function runArtifact(id,name){return artifactUrl('run',id,name);}
function renderUsage(usage){if(!usage)return;studioUsage=usage;$('usageStrip').innerHTML=`<strong>Usage</strong><span><strong>${Number(usage.codex_tokens||0).toLocaleString()}</strong> observed Codex tokens · ${usage.codex_calls||0} calls this week · ${usage.runs||0} saved runs</span><span class="meta">${usage.weekly_limit_tokens?`${usage.percent_of_limit}% of configured limit`:'Plus weekly allowance is not exposed by the local CLI'}</span>`;}
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
async function loadProtection(){try{const r=await fetch('/api/locks');const data=await r.json();if(!r.ok)throw new Error(data.error||'Lock status unavailable');const present=(data.files||[]).filter(item=>item.exists).map(item=>item.name.replace('CV/',''));$('protectionStrip').innerHTML='<strong>Canonical resumes locked</strong><span>'+esc(data.message||'Studio writes private copies only.')+'</span><span class="meta">Protected: '+esc(present.join(', ')||'canonical CV files')+'</span>';}catch(error){$('protectionStrip').innerHTML='<strong>Canonical resumes locked</strong><span class="meta">'+esc(error.message)+'</span>';}}
let evidenceClaims=[],evidenceFilter='unreviewed';
function renderEvidenceReview(data){const summary=data.summary||{},counts=summary.counts||{};const filtered=evidenceClaims.filter(item=>evidenceFilter==='all'||(item.review_status||'unreviewed')===evidenceFilter);let html='<div class="meta">'+(summary.nodes||0)+' indexed evidence records · '+(summary.usable_claims||0)+' usable now · '+(summary.default_blocked||0)+' blocked by default · '+(counts.rejected||0)+' rejected</div><div class="button-row"><select id="evidenceFilter" aria-label="Evidence review filter"><option value="unreviewed">Needs review</option><option value="confirmed">Confirmed</option><option value="rejected">Rejected</option><option value="superseded">Superseded</option><option value="all">All</option></select></div>';html+=filtered.slice(0,24).map(item=>{const id=encodeURIComponent(item.id||'');const status=item.review_status||'unreviewed';return '<article class="evidence-card"><div class="line-meta"><span>'+esc(item.heading||'Evidence')+'</span><span>'+esc(status)+' · authority '+esc(item.authority||0)+'</span></div><div>'+esc(item.text||'')+'</div><p class="source-note">'+esc(item.source||'')+'</p><div class="line-actions"><button data-evidence-id="'+id+'" data-evidence-status="confirmed">Confirm</button><button data-evidence-id="'+id+'" data-evidence-status="rejected">Reject</button></div></article>';}).join('')||'<p class="meta">No records match this filter.</p>';$('evidenceReview').innerHTML=html;const filter=$('evidenceFilter');if(filter)filter.value=evidenceFilter;if(filter)filter.onchange=()=>{evidenceFilter=filter.value;renderEvidenceReview(data);};}
async function loadEvidenceReview(){try{const r=await fetch('/api/evidence');const data=await r.json();if(!r.ok)throw new Error(data.error||'Evidence review unavailable');evidenceClaims=data.claims||[];renderEvidenceReview(data);}catch(error){$('evidenceReview').innerHTML='<p class="meta">'+esc(error.message)+'</p>';}}
async function reviewEvidence(id,status){try{const r=await fetch('/api/evidence/review',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({node_id:id,status,claim_allowed:status==='confirmed'})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not save evidence review');evidenceClaims=data.claims||[];renderEvidenceReview(data);}catch(error){alert(error.message);}}
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
document.addEventListener('click',async event=>{const evidence=event.target.closest('[data-evidence-status]');if(evidence){event.preventDefault();return reviewEvidence(decodeURIComponent(evidence.dataset.evidenceId||''),evidence.dataset.evidenceStatus||'');}const open=event.target.closest('[data-open-workshop]');if(open){event.preventDefault();return openWorkshop(open.dataset.openWorkshop||open.dataset.run);}const button=event.target.closest('[data-view-posting]');if(!button)return;const card=button.closest('.resume-card'),panel=card.querySelector('.posting-snapshot');if(!panel.classList.contains('hidden')){panel.classList.add('hidden');button.textContent='View posting snapshot';return;}button.disabled=true;try{const r=await fetch(button.dataset.posting),data=await r.json();if(!r.ok)throw new Error(data.error||'Posting snapshot unavailable');panel.innerHTML=`<strong>Saved posting snapshot</strong><pre>${esc(data.posting_text||'Only posting metadata was available for this run.')}</pre>`;panel.classList.remove('hidden');button.textContent='Hide posting snapshot';}catch(error){panel.textContent=error.message;panel.classList.remove('hidden');}finally{button.disabled=false;}});
function explainRadarReason(reason){const text=String(reason||'');if(text.startsWith('raw utility'))return 'Calibration: '+text;const labels=[['base utility','Baseline role utility'],['role:','Role family fit'],['sector:','Sector fit'],['new-grad/early-career priority','Verified early-career signal'],['early-career possible','Plausible first-role signal'],['new-grad evidence absent','No explicit early-career evidence'],['company tier','Company quality'],['explicit goal company','Personal goal-company preference'],['company concentration','Company diversity adjustment'],['compensation','Compensation'],['posted','Freshness'],['remote','Remote access'],['Resume Match','Resume Match']];const label=(labels.find(item=>text.startsWith(item[0]))||[])[1]||'Scoring input';return label+': '+text;}
 $('search').oninput=()=>{clearTimeout(window.searchTimer);window.searchTimer=setTimeout(loadJobs,250)};$('sort').onchange=loadJobs;$('librarySearch').oninput=renderLibrary;$('libraryMode').onchange=renderLibrary;$('analyzeMatch').onclick=analyzeMatch;$('refreshEvidence').onclick=refreshEvidence;$('strict').onclick=()=>start('used');$('dream').onclick=()=>start('ai');$('unrestricted').onclick=()=>start('unrestricted');$('showScoreReasons').onclick=()=>{if(!selected)return;const reasons=selected.score_reasons||[];$('scoreReasons').classList.remove('hidden');$('scoreReasons').innerHTML='<strong>Why Radar gave this role '+esc(selected.score)+'/100</strong><p>Radar is deterministic job fit. Resume Match is a separate CV/evidence alignment score. 90+ is strong; the company-diversity adjustment only nudges weaker duplicates.</p><ul>'+reasons.map(reason=>'<li>'+esc(explainRadarReason(reason))+'</li>').join('')+'</ul>';};$('queueOpen').onclick=()=>showView('library');$('tailorTab').onclick=()=>showView('tailor');$('libraryTab').onclick=()=>showView('library');$('selectedLibrary').onclick=()=>showView('library');$('allSaved').onclick=()=>showView('library');$('backToTailor').onclick=()=>showView('tailor');$('workshopBack').onclick=()=>showView('library');$('workshopTailor').onclick=()=>showView('tailor');$('workshopAsk').onclick=()=>askWorkshop('');Promise.all([loadJobs(),loadLibrary(),loadUsage(),loadProtection(),loadEvidenceReview()]);
</script></main></body></html>"""


UI_HTML = UI_HTML.replace(
    '<div id="usageStrip" class="usage-strip"><strong>Usage</strong><span class="meta">Loading observed local Codex usage…</span></div>',
    '<details class="utility-details" open><summary>Safety and usage</summary><div id="protectionStrip" class="protection-strip"><strong>Canonical resumes locked</strong><span>Studio creates private copies only; protected CV/immutable/ artifacts are never overwritten.</span><span class="meta">Owner edits require: .venv/bin/python scripts/resume_lock.py unlock</span></div><div id="usageStrip" class="usage-strip"><strong>Usage</strong><span class="meta">Loading observed local Codex usage…</span></div></details>',
)
UI_HTML = UI_HTML.replace("CV/resume.tex locked", "CV/immutable/VictorJimenezResume.tex locked")
UI_HTML = UI_HTML.replace("||'resume.pdf',previewName", "||'company_resume_ai.pdf',previewName")
UI_HTML = UI_HTML.replace("||report.pdf_filename||'resume.pdf'", "||report.pdf_filename||'company_resume_ai.pdf'")
UI_HTML = UI_HTML.replace('<option value="unrestricted">Unrestricted AI</option>', '<option value="unrestricted">Take-the-wheel</option>')
UI_HTML = UI_HTML.replace('unrestricted drafts are intentionally more original', 'Take-the-wheel drafts are intentionally more original')
UI_HTML = UI_HTML.replace(
    r'''${report?`<a href="${report}" target="_blank" rel="noreferrer">Report</a>`:''}''',
    r'''${report?(entry.source==='run'?`<button class="secondary" data-open-saved-report="${esc(entry.entry_id)}">View audit</button>`:`<a href="${report}" target="_blank" rel="noreferrer">Report</a>`):''}''',
)
UI_HTML = UI_HTML.replace(
    "if(data.status==='complete'||data.status==='failed')",
    "if(data.status==='complete'||data.status==='awaiting_review'||data.status==='failed')",
)
UI_HTML = UI_HTML.replace(
    "claim_allowed:status==='confirmed'",
    "claim_allowed:status==='confirmed'||status==='public_safe'",
)
UI_HTML = UI_HTML.replace(
    "function renderUsage(usage){if(!usage)return;$('usageStrip').innerHTML=`<strong>Usage</strong><span><strong>${Number(usage.codex_tokens||0).toLocaleString()}</strong> observed Codex tokens · ${usage.codex_calls||0} calls this week · ${usage.runs||0} saved runs</span><span class=\"meta\">${usage.weekly_limit_tokens?`${usage.percent_of_limit}% of configured limit`:'Plus weekly allowance is not exposed by the local CLI'}</span>;}",
    "function renderUsage(usage){if(!usage)return;$('usageStrip').innerHTML=`<strong>Usage</strong><span><strong>${Number(usage.codex_tokens||0).toLocaleString()}</strong> Codex · ${Number(usage.claude_tokens||0).toLocaleString()} Claude observed tokens · ${usage.runs||0} saved runs</span><span class=\"meta\">Codex model: gpt-5.6-luna · first-party subscription CLIs only; no local-model or API fallback</span>`;}",
)
UI_HTML = UI_HTML.replace(
    "observed Codex tokens · ${usage.codex_calls||0} calls this week",
    "observed Codex tokens · ${usage.codex_calls||0} calls this week · Codex model gpt-5.6-luna",
)
UI_HTML = UI_HTML.replace(
    "if(review.craft_score!==undefined)",
    "if(review.craft_score!==undefined&&review.craft_score!==null)",
)
UI_HTML = UI_HTML.replace(
    '</details>\n<div id="queueStrip"',
    '</details><details class="evidence-review-details"><summary>Evidence review</summary><div class="meta evidence-review-hint">Local sources are usable by default. Review only stale, disputed, or public records; public evidence stays blocked until you confirm it.</div><div id="evidenceReview"><span class="meta">Loading indexed evidence…</span></div></details>\n<div id="queueStrip"',
)
UI_HTML = UI_HTML.replace(
    '<option value="confirmed">Confirmed</option><option value="rejected">Rejected</option><option value="superseded">Superseded</option>',
    '<option value="confirmed">Confirmed</option><option value="public_safe">Public safe</option><option value="disputed">Disputed</option><option value="private_do_not_publish">Private only</option><option value="rejected">Rejected</option><option value="superseded">Superseded</option>',
)
UI_HTML = UI_HTML.replace(
    '<button data-evidence-id="\'+id+\'" data-evidence-status="confirmed">Confirm</button><button data-evidence-id="\'+id+\'" data-evidence-status="rejected">Reject</button>',
    '<button data-evidence-id="\'+id+\'" data-evidence-status="confirmed">Confirm</button><button data-evidence-id="\'+id+\'" data-evidence-status="public_safe">Public safe</button><button data-evidence-id="\'+id+\'" data-evidence-status="disputed">Dispute</button><button data-evidence-id="\'+id+\'" data-evidence-status="private_do_not_publish">Private only</button><button data-evidence-id="\'+id+\'" data-evidence-status="rejected">Reject</button>',
)
UI_HTML = UI_HTML.replace(
    '<h2>Postings</h2><span id="jobCount" class="count"></span></div><p class="hint">Choose a role. Saved resumes stay in the bank when you switch.</p>',
    '<h2>1. Choose a posting</h2><span id="jobCount" class="count"></span></div><p class="hint">Select a role, then choose one draft mode. Saved resumes stay in the bank when you switch.</p>',
)
UI_HTML = UI_HTML.replace(
    '<div id="match" class="match-card"></div><div class="button-row"><button id="analyzeMatch" class="secondary">Analyze full posting match</button><button id="showScoreReasons" class="secondary">Explain Radar score</button></div>',
    '<div id="match" class="match-card"></div><details class="secondary-tools"><summary>Optional analysis</summary><div class="button-row"><button id="analyzeMatch" class="secondary">Analyze full posting match</button><button id="showScoreReasons" class="secondary">Explain Radar score</button></div></details>',
)
UI_HTML = UI_HTML.replace(
    '<div class="action-grid"><div class="action-card"><h3>Used bullets</h3><p>Approved wording and selections only. Your clean comparison baseline.</p><p class="micro">Lowest creative variance · still queues a complete draft</p><button id="strict">Queue used-bullets tailor</button></div><div class="action-card featured"><h3>AI tailor</h3><p>Role-specific rewrites, project swaps, ATS coverage, and a review pass.</p><p class="micro">Evidence-grounded original wording</p><button id="dream">Queue AI tailor</button></div><div class="action-card"><h3>Unrestricted AI tailor</h3><p>Freer synthesis across your CV evidence bank for a sharper, more original argument.</p><p class="micro">Still factual and layout-safe · human-review flag stays visible</p><button id="unrestricted">Queue unrestricted tailor</button></div></div>',
    '<div class="action-grid"><div class="action-card featured"><h3>1. Take-the-wheel</h3><p>Primary mode: choose the strongest portfolio, surface deeper evidence, and rewrite when the hiring-value gain is real.</p><p class="micro">Creative and adaptive · still evidence-grounded, chronological, and layout-safe</p><button id="unrestricted">Create take-the-wheel draft</button></div><div class="action-card"><h3>2. AI tailor</h3><p>Role-specific project selection, ATS terminology, rewrites, and an independent review pass.</p><p class="micro">Adaptive with a more conservative change threshold</p><button id="dream">Create AI-tailored draft</button></div><div class="action-card"><h3>3. Used bullets</h3><p>Approved wording and selections only. The clean comparison baseline.</p><p class="micro">Lowest creative variance</p><button id="strict">Create used-bullets draft</button></div></div><details class="mode-guide"><summary>How the modes differ</summary><div class="mode-guide-copy"><p><strong>Take-the-wheel:</strong> may substantially restructure the portfolio when stronger verified evidence supports it.</p><p><strong>AI tailor:</strong> makes role-specific changes, but clears a higher bar for replacing already-strong evidence.</p><p><strong>Used bullets:</strong> selects approved source lines with minimal creative variance.</p><p class="meta">All three modes use the same evidence graph, factual checks, one-page contract, chronological experience order, and owner review.</p></div></details>',
)
UI_HTML = UI_HTML.replace(
    '<div class="section-title"><h3>Saved for this posting</h3><button id="allSaved" class="secondary">See all saved resumes</button></div><div id="selectedResumes"></div>',
    '<div class="section-title"><h3>Saved for this posting</h3><button id="allSaved" class="secondary">Open resume bank</button></div><details class="saved-inline"><summary>Show saved drafts for this posting</summary><div id="selectedResumes"></div></details>',
)
UI_HTML = UI_HTML.replace(
    "html+='<pre>'+esc(JSON.stringify(review,null,2))+'</pre>'",
    "html+='<details class=\"report-details\"><summary>Raw review data</summary><pre>'+esc(JSON.stringify(review,null,2))+'</pre></details>'",
)

UI_HTML = UI_HTML.replace(
    "</head>",
    "<style>.review-preview{position:relative;max-width:760px;margin:10px auto;background:#fff;border:1px solid var(--line);border-radius:5px;overflow:hidden}.review-preview>img{display:block;width:100%;height:auto}.review-box{position:absolute;border:2px solid rgba(255,183,0,.9);background:rgba(255,220,80,.22);border-radius:2px;pointer-events:auto}.review-box.changed{border-color:rgba(88,166,255,.95);background:rgba(88,166,255,.18)}.review-box.both{border-color:rgba(168,85,247,.95);background:rgba(168,85,247,.18)}.review-legend{display:flex;gap:10px;flex-wrap:wrap;margin:7px 0 0;color:var(--muted);font-size:11px}.review-legend span{display:inline-flex;align-items:center;gap:4px}.review-legend i{display:inline-block;width:11px;height:11px;border:2px solid #ffb700;background:rgba(255,220,80,.22);border-radius:2px}.review-legend .changed i{border-color:#58a6ff;background:rgba(88,166,255,.18)}.review-legend .both i{border-color:#a855f7;background:rgba(168,85,247,.18)}</style></head>",
)


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
  const owner=report.owner_summary||{};
  if(owner.headline)extra+=`<p><strong>Portfolio read:</strong> ${esc(owner.headline)}</p>`;
  if(owner.strong_fifth_experience_preserved)extra+=`<div class=\"meta\">Preserved the strong fifth J&amp;J experience bullet.</div>`;
  if((owner.front_matter_tradeoffs||[]).length)extra+=`<div class=\"meta\"><strong>Space reclaimed:</strong> ${esc(owner.front_matter_tradeoffs.join(', '))}</div>`;
  if((report.line_compactions||[]).length)extra+=`<details><summary>Measured line safety edits (${report.line_compactions.length})</summary>${report.line_compactions.map(item=>`<div class=\"meta\"><strong>${esc(item.source_id||'line')}</strong><br><s>${esc(item.from||'')}</s><br>→ ${esc(item.to||'')}</div>`).join('')}</details>`;
  if((owner.redundant_project_flags||[]).length)extra+=`<details><summary>Portfolio overlap flagged (${owner.redundant_project_flags.length})</summary><div class=\"meta\">${owner.redundant_project_flags.map(item=>esc(item)).join(' · ')}</div></details>`;
  if((owner.portfolio_warnings||[]).length)extra+=`<details><summary>Portfolio checks (${owner.portfolio_warnings.length})</summary>${owner.portfolio_warnings.map(item=>`<div class=\"meta\">${esc(item)}</div>`).join('')}</details>`;
 const rewrites=changes.rewritten_bullets||[];
 if(rewrites.length)extra+=`<details><summary>Show ${rewrites.length} rewritten lines</summary>${rewrites.map(item=>`<div class=\"meta\"><strong>${esc(item.source_id||'line')}</strong><br><s>${esc(item.source_text||'')}</s><br>→ ${esc(item.final_text||'')}</div>`).join('')}</details>`;
  const ledger=changes.decision_ledger||[];
  if(ledger.length)extra+=`<details><summary>Decision ledger (${ledger.length})</summary>${ledger.map(item=>`<div class=\"meta\"><strong>${esc(item.action||'change')}</strong> · ${esc(item.target_signal||'target signal')}<br><s>${esc(item.current_evidence||'')}</s><br>→ ${esc(item.replacement_or_exclusion||'')}<br><strong>Why:</strong> ${esc(item.why_stronger||'')} ${item.signal_lost?`<br><strong>Signal lost:</strong> ${esc(item.signal_lost)}`:''}</div>`).join('')}</details>`;
  if((changes.removed_canonical_bullets||[]).length)extra+=`<details><summary>Removed canonical evidence (${changes.removed_canonical_bullets.length})</summary>${changes.removed_canonical_bullets.map(item=>`<div class=\"meta\"><strong>${esc(item.source_id||'line')}</strong><br>${esc(item.text||'')}</div>`).join('')}</details>`;
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
const baseCheckpointRenderReport=renderReport;
renderReport=function(status){
  baseCheckpointRenderReport(status);
  const report=status.report||{},review=report.review||{},gates=review.gates||{};
  let checkpoint=`<div class="match-card"><strong>${status.status==='awaiting_review'?'Owner checkpoint':'Approval record'}</strong><div class="meta">${review.ready===true?'All required gates pass; inspect the diff before approval.':'This draft is not ready for approval yet.'}</div><p>${Object.entries(gates).map(([name,gate])=>`<span class="badge">${esc(name)}: ${esc(gate.status||'unknown')}</span>`).join(' ')}</p>`;
  if(status.status==='awaiting_review'&&review.ready===true)checkpoint+=`<button data-approve-run="${esc(status.run_id)}">Approve final PDF</button><span class="meta">Approval only changes this private run to complete.</span>`;
  else if(status.status==='awaiting_review')checkpoint+=`<p class="meta">Open the workshop or inspect the critique below; a failed gate keeps this run in draft state.</p>`;
  else if(report.approval_state==='approved')checkpoint+=`<p class="meta">Approved by ${esc(report.approved_by||'Victor')} · ${esc(report.approved_at||'')}</p>`;
  checkpoint+='</div>';
  $('report').insertAdjacentHTML('afterbegin',checkpoint);
};
async function approveRun(id){const button=document.querySelector('[data-approve-run]');if(button)button.disabled=true;try{const r=await fetch('/api/run/approve',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({run_id:id})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Approval failed');if(id===activeRunId){$('status').className='status complete';$('status').textContent='Draft approved and saved in the private resume bank';renderReport(data);}await loadLibrary();}catch(error){if(button)button.disabled=false;alert(error.message);}}
document.addEventListener('click',event=>{const approve=event.target.closest('[data-approve-run]');if(approve){event.preventDefault();approveRun(approve.dataset.approveRun||'');}});
function showView(view){""",
)

UI_HTML = UI_HTML.replace(
    "${layout.horizontal.underfilled_line_count||0} underfilled lines · one-more-bullet",
    "${layout.horizontal.near_wrap_count||0} near-wraps · ${layout.horizontal.underfilled_line_count||0} roomy lines · one-more-bullet",
)
UI_HTML = UI_HTML.replace(
    "</head>",
    "<style>.workshop-preview{display:none!important}.utility-details{margin:0 0 16px;border:1px solid var(--line);border-radius:8px;background:#111827}.utility-details summary{cursor:pointer;padding:10px 12px;color:var(--muted)}.utility-details[open] summary{border-bottom:1px solid var(--line)}.protection-strip{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 12px;color:#d7f9df;background:#102a1a;border-bottom:1px solid #245c34}.protection-strip strong{color:var(--good)}.evidence-review-details{margin:0 0 16px;border:1px solid var(--line);border-radius:8px;background:#111827;padding:0 10px}.evidence-review-details summary{cursor:pointer;color:var(--accent);padding:9px 2px}.evidence-review-hint{padding:0 2px 8px}.evidence-card{border-top:1px solid var(--line);padding:10px 0;font-size:12px}.evidence-card:first-of-type{border-top:0}.secondary-tools,.advanced-mode,.saved-inline{margin:10px 0;border:1px solid var(--line);border-radius:7px;padding:0 10px;background:#111827}.secondary-tools summary,.advanced-mode summary,.saved-inline summary{cursor:pointer;color:var(--accent);padding:9px 2px}.secondary-tools .button-row,.advanced-mode .action-card,.saved-inline>div{margin-bottom:8px}.report-details pre{margin-top:8px}</style></head>",
)
UI_HTML = UI_HTML.replace(
    "The original generated PDF stays untouched. Header, education, and technical skills remain the canonical base; experience, projects, and leadership lines are editable here.",
    "The original generated PDF stays untouched. The template shell remains canonical, but every visible resume line—education, skills, experience, projects, and leadership—is editable here.",
)

UI_HTML = UI_HTML.replace(
    "</head>",
    "<style>.mode-guide-copy{padding:0 2px 8px;color:var(--muted)}.mode-guide-copy p{margin:7px 0}.audit-shell{margin:14px 0;display:grid;gap:10px}.audit-card{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px}.audit-card h4{margin:0 0 7px;font-size:14px}.audit-card .meta{line-height:1.55}.audit-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.audit-metric{min-height:72px;padding:9px;background:#111827;border:1px solid var(--line);border-radius:7px}.audit-metric strong{display:block;font-size:18px;color:var(--text)}.audit-metric span{display:block;color:var(--muted);font-size:11px;margin-top:2px}.audit-good{color:var(--good)!important}.audit-warn{color:var(--warn)!important}.audit-bad{color:var(--bad)!important}.diff-list{display:grid;gap:7px}.diff-row{padding:8px;border-left:3px solid var(--line);background:#111827;border-radius:4px;font-size:12px;line-height:1.45}.diff-row.added{border-color:var(--good)}.diff-row.removed{border-color:var(--bad)}.diff-row.rewritten{border-color:var(--warn)}.diff-row s{color:#b17b7b}.diff-row .diff-label{display:block;color:var(--muted);font-size:10px;letter-spacing:.04em;text-transform:uppercase;margin-bottom:3px}.flow{display:grid;gap:6px}.flow-step{display:grid;grid-template-columns:14px 90px minmax(0,1fr) auto;align-items:center;gap:8px;padding:7px 8px;background:#111827;border:1px solid var(--line);border-radius:6px;font-size:12px}.flow-dot{width:9px;height:9px;border-radius:50%;background:var(--good);box-shadow:0 0 0 3px rgba(63,185,80,.12)}.flow-step.failed .flow-dot{background:var(--bad);box-shadow:0 0 0 3px rgba(248,81,73,.12)}.flow-step.skipped .flow-dot{background:var(--muted);box-shadow:none}.flow-step small,.flow-step .flow-meta{color:var(--muted)}.ats-overlay{background:#f6f8fa;color:#17202b;border-radius:6px;padding:10px 12px;margin-top:8px;font:12px/1.45 -apple-system,BlinkMacSystemFont,sans-serif}.ats-entry{border-top:1px solid #d7dee7;padding:7px 0}.ats-entry:first-child{border-top:0;padding-top:0}.ats-entry strong{display:block;font-size:11px;color:#4f6072;margin-bottom:2px}.ats-mark{background:#ffe38a;color:#151515;border-radius:2px;padding:0 2px}.audit-candidate{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 7px;margin:3px 4px 0 0;color:var(--muted);font-size:11px}.audit-candidate.applied{border-color:#2ea043;color:#b8f5c2}.audit-candidate.rejected{border-color:#8a3d3d;color:#ffb4b4}.audit-subgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.audit-scroll{max-height:300px;overflow:auto}@media(max-width:850px){.audit-grid,.audit-subgrid{grid-template-columns:1fr}.flow-step{grid-template-columns:14px minmax(80px,auto) 1fr}.flow-step .flow-meta{grid-column:2 / -1}}</style></head>",
)

UI_HTML = UI_HTML.replace(
    "function showView(view){",
    r'''const visualBaseRenderReport=renderReport;
renderReport=function(status){
  visualBaseRenderReport(status);
  const report=status.report||{},changes=report.content_changes||{},space=report.space_audit||{},usage=report.usage||{},metrics=report.run_metrics||{},previewName=status.preview_filename||report.preview_filename||'';
  if(!report.content_changes)return;
  const fmtSeconds=value=>{const n=Number(value);if(!Number.isFinite(n))return '—';if(n<60)return n.toFixed(1)+'s';return Math.floor(n/60)+'m '+Math.round(n%60)+'s';};
  const terms=(changes.keyword_coverage?.terms||[]).filter(item=>item.supported&&item.rendered).map(item=>String(item.term||'')).filter(Boolean);
  const markTerms=value=>{
    let html=esc(plainLine(value||''));
    const ordered=[...terms].sort((a,b)=>b.length-a.length);
    ordered.forEach(term=>{
      const escaped=esc(term).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
      const pattern=escaped.replace(/\s+/g,'\\s+');
      const matcher=new RegExp('(^|[^A-Za-z0-9])('+pattern+')(?=$|[^A-Za-z0-9])','ig');
      html=html.replace(matcher,(whole,prefix,match)=>prefix+'<mark class="ats-mark">'+match+'</mark>');
    });
    return html;
  };
  const list=(items,kind,empty)=>items.length?`<div class="diff-list">${items.map(item=>`<div class="diff-row ${kind}"><span class="diff-label">${esc(item.label||kind)}</span>${item.html||esc(item.text||'')}</div>`).join('')}</div>`:`<div class="meta">${esc(empty)}</div>`;
  const rewrites=(changes.rewritten_bullets||[]).map(item=>({label:'rewritten · '+(item.source_id||'line'),html:'<s>'+esc(item.source_text||'')+'</s><br>→ '+esc(item.final_text||'')}));
  const additions=(changes.added_bullets||[]).map(item=>({label:'new selected evidence · '+(item.source_id||'line'),text:item.text||''}));
  const removals=(changes.removed_canonical_bullets||[]).map(item=>({label:'removed from canonical · '+(item.source_id||'line'),text:item.text||''}));
  const flow=report.provider_flow||report.providers||[];
  const flowHtml=flow.length?flow.map(item=>`<div class="flow-step ${item.status==='failed'?'failed':item.status==='skipped'?'skipped':''}"><span class="flow-dot"></span><strong>${esc(item.label||'provider')}</strong><span>${esc(item.model||item.provider||'unknown')}<br><small>${esc(item.status||'unknown')}</small></span><span class="flow-meta">${fmtSeconds(item.elapsed_seconds)} · ${item.usage_tokens==null?'tokens n/a':Number(item.usage_tokens).toLocaleString()+' tokens'}</span></div>`).join(''):'<div class="meta">No provider flow was recorded.</div>';
  const plan=report.content_plan||{};
  const atsEntries=[];
  ['experiences','projects','leadership'].forEach(section=>(plan[section]||[]).forEach(entry=>(entry.bullets||[]).forEach(bullet=>atsEntries.push({entry:entry.source_id||section,text:bullet.text||''}))));
  const atsHtml=atsEntries.length?atsEntries.map(item=>`<div class="ats-entry"><strong>${esc(item.entry)}</strong><span>${markTerms(item.text)}</span></div>`).join(''):'<div class="meta">No final bullets were returned.</div>';
  const weekly=studioUsage?`<p class="meta"><strong>This week:</strong> ${Number(studioUsage.codex_tokens||0).toLocaleString()} observed Codex tokens · ${studioUsage.codex_calls||0} Codex calls · ${studioUsage.runs||0} saved runs${studioUsage.weekly_limit_tokens?` · ${studioUsage.percent_of_limit}% of configured limit`:''}</p>`:'<p class="meta">Weekly usage is loading or unavailable from the local CLI.</p>';
  const chronology=changes.experience_order?.chronology_preserved!==false;
  const oneMore=space.one_more_standard_bullet_fits,spaceReview=space.space_review_needed;
  const applied=space.expansion_applied||[],replaced=space.expansion_replaced||[],rejected=space.expansion_rejected||[],candidates=space.unused_verified_candidates||[];
  const overlay=report.review_overlay||{},previewUrl=previewName?runArtifact(status.run_id,previewName):'';
  const overlayBoxes=(overlay.boxes||[]).map(box=>`<div class="review-box ${esc(box.kind||'ats')}" style="left:${Number(box.left_percent)||0}%;top:${Number(box.top_percent)||0}%;width:${Number(box.width_percent)||0}%;height:${Number(box.height_percent)||0}%" title="${esc((box.terms||[]).join(', ')||box.changed_source_id||'review highlight')}"></div>`).join('');
  const reviewPreview=overlay.available&&previewUrl?`<div class="audit-card"><h4>Highlighted review render</h4><p class="meta">This is a review overlay on the clean rendered page. The downloadable PDF above is unchanged.</p><div class="review-preview"><img src="${previewUrl}" alt="Clean resume with review highlights">${overlayBoxes}</div><div class="review-legend"><span><i></i>supported ATS term</span><span class="changed"><i></i>meaningful content change</span><span class="both"><i></i>both</span></div></div>`:'';
  let panel=`<section class="audit-shell"><div class="audit-card"><h4>Tailoring audit · what happened</h4><div class="audit-grid"><div class="audit-metric"><strong>${changes.changed_bullet_count||0}</strong><span>meaningful rewrites</span></div><div class="audit-metric"><strong>${(changes.added_bullets||[]).length}</strong><span>new evidence lines surfaced</span></div><div class="audit-metric"><strong>${fmtSeconds(metrics.elapsed_seconds||status.elapsed_seconds)}</strong><span>total run time</span></div></div><p class="meta">${esc(report.owner_summary?.headline||'The report records selection, replacement, and layout decisions.')}</p>${changes.low_value_rewrite_count?`<p class="meta">${changes.low_value_rewrite_count} near-copy paraphrase${changes.low_value_rewrite_count===1?' was':'s were'} suppressed because it added no measurable hiring value.</p>`:''}<p class="meta"><span class="badge ${chronology?'audit-good':'audit-bad'}">${chronology?'Experience chronology preserved':'Chronology needs review'}</span> <span class="badge">${esc(report.mode||status.mode||'tailor')}</span> <span class="badge">PDF: ${esc(report.pdf_filename||status.pdf_filename||'named resume')}</span></p></div>${reviewPreview}`;
  panel+=`<div class="audit-subgrid"><div class="audit-card"><h4>Measured page use</h4><div class="meta">Bottom: <strong>${space.measured_content_bottom_pt==null?'—':space.measured_content_bottom_pt+'pt'}</strong> · reference: <strong>${space.canonical_reference_bottom_pt==null?'—':space.canonical_reference_bottom_pt+'pt'}</strong><br>Clearance gap: <strong>${space.density_gap_pt==null?'—':space.density_gap_pt+'pt'}</strong> · extra-space review: <strong class="${spaceReview?'audit-warn':'audit-good'}">${spaceReview?(oneMore?'one line fits':'measured gap exceeds target'):'no safe addition'}</strong></div><p class="meta">${esc(space.decision||'No space decision recorded.')}</p>${applied.length?'<div>'+applied.map(item=>`<span class="audit-candidate applied">added ${esc(item.source_id||'line')}</span>`).join('')+'</div>':''}${replaced.length?'<details><summary>Replaced lower-value evidence (${replaced.length})</summary>'+replaced.map(item=>`<div class="meta"><strong>${esc(item.source_id||'line')}</strong> · ${esc(item.entry_id||'entry')}<br>${esc(item.text||'')}<br><em>${esc(item.reason||'')}</em></div>`).join('')+'</details>':''}${rejected.length?'<div>'+rejected.map(item=>`<span class="audit-candidate rejected">held ${esc(item.source_id||'candidate')}</span>`).join('')+'</div>':''}</div><div class="audit-card"><h4>Verified evidence left on the table</h4><div class="meta"><strong>${space.unused_candidate_count||candidates.length}</strong> candidate lines inspected${candidates.length?'<br>':''}${candidates.length?candidates.slice(0,8).map(item=>`<div><strong>${esc(item.source_id||'candidate')}</strong> · ${esc(item.text||'')}</div>`).join(''):'<span> No unused strong line was found for the selected portfolio.</span>'}</div></div></div>`;
  panel+=`<div class="audit-card"><h4>Provider flow, model, and usage</h4><div class="flow">${flowHtml}</div><p class="meta"><strong>This run:</strong> ${Number(usage.codex_tokens||0).toLocaleString()} Codex tokens · ${usage.codex_calls||0} Codex calls · ${Number(usage.claude_tokens||0).toLocaleString()} Claude tokens · ${usage.claude_calls||0} Claude calls</p>${weekly}</div>`;
  panel+=`<div class="audit-card"><h4>Text diff</h4><div class="audit-scroll">${list(rewrites,'rewritten','No meaningful wording rewrites were recorded.')}${list(additions,'added','No canonical-source additions were recorded.')}${list(removals,'removed','No canonical evidence was removed.')}${changes.low_value_rewrite_count?`<p class="meta">${changes.low_value_rewrite_count} low-value paraphrase${changes.low_value_rewrite_count===1?'':'s'} hidden from the substantive diff.</p>`:''}</div></div>`;
  panel+=`<div class="audit-card"><h4>ATS overlay <span class="meta">(review view; the downloadable PDF stays clean)</span></h4><p class="meta">Highlighted terms are supported and rendered in the final text. Missing or unsupported terms remain visible in the full report instead of being stretched.</p><div class="ats-overlay">${atsHtml}</div></div></section>`;
  $('report').insertAdjacentHTML('afterbegin',panel);
};
function showView(view){''',
)

UI_HTML = UI_HTML.replace(
    "function showView(view){",
    r'''async function openSavedReport(runId){
  try{
    const response=await fetch('/api/run?id='+encodeURIComponent(runId));
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'Saved report unavailable');
    if(!data.report)throw new Error('This older run has no saved report data. Open its JSON report instead.');
    activeRunId=runId;
    $('empty').classList.add('hidden');
    $('workspace').classList.remove('hidden');
    $('status').className='status '+(data.status||'awaiting_review');
    $('status').textContent='Loaded saved audit for '+(data.job?.company||data.report.job?.company||'this run');
    $('status').classList.remove('hidden');
    showView('tailor');
    renderReport(data);
  }catch(error){alert(error.message);}
}
document.addEventListener('click',event=>{
  const saved=event.target.closest('[data-open-saved-report]');
  if(saved){event.preventDefault();openSavedReport(saved.dataset.openSavedReport||'');}
});
function showView(view){''',
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
        if parsed.path == "/api/locks":
            return self.send_json(canonical_resume_lock(repo_root()))
        if parsed.path == "/api/evidence":
            return self.send_json(evidence_review_view(repo_root()))
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
            "/api/run", "/api/run/approve", "/api/match", "/api/evidence/refresh", "/api/evidence/review",
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
                    "public_refresh_warnings": graph.get("public_refresh_warnings", []),
                })
            if parsed.path == "/api/evidence/review":
                return self.send_json(update_evidence_review(
                    node_id=str(body.get("node_id") or ""),
                    status=str(body.get("status") or ""),
                    note=str(body.get("note") or ""),
                    claim_allowed=bool(body.get("claim_allowed")),
                    root=repo_root(),
                ))
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
            if parsed.path == "/api/run/approve":
                return self.send_json(approve_run(repo_root(), str(body.get("run_id") or "")))
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
