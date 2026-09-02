#!/usr/bin/env python3
"""Victor-first local Resume Studio.

This is deliberately a local companion, not a hosted CV service.  It reads the
radar's public job snapshot and Victor's ignored ``CV/`` directory, then can
ask the installed first-party Codex CLI to work on a private
resume draft using their existing local authentication.

The service has two modes:

* ``strict`` selects only existing, human-approved source bullets and runs
  deterministic layout checks against the canonical one-page resume format.
* ``dream``/``unrestricted`` run a frontier draft, a synthesis pass, and a
  separate Codex Luna multi-role jury. Codex may apply critique in bounded
  revision rounds; critics never mutate or self-grade the plan, and the module
  reports separate quality gates instead of a composite craft score.
* ``generation`` adds a requirement-to-evidence gap pass before drafting. It
  may synthesize new bullets and tailored skill lines from authorized Markdown
  evidence while leaving unsupported requirements visible.

Run with::

    .venv/bin/python scripts/resume_studio.py

Then open http://127.0.0.1:4317/ .  Private run history stays below the
ignored ``CV/.resume_studio/`` directory. The newest primary PDFs are also
copied to the easy-to-find ``CV/tailored/`` folder.
"""

from __future__ import annotations

import argparse
import copy
import concurrent.futures
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import html
import itertools
import json
import os
import re
import shutil
import secrets
import signal
import subprocess
import sys
import tempfile
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
from radar.company_research import dossier_for
from radar.evidence_review import (BLOCKING_STATUSES, REVIEW_STATUSES,
                                   add_question_hint as save_context_hint,
                                   answer_question as save_context_answer,
                                   dismiss_question_hint as dismiss_context_hint,
                                   load_reviews, review_path, review_summary,
                                   upsert_questions)
from radar.application_agent import (
    add_issue as add_application_issue,
    apply_confirmation as apply_application_confirmation,
    create_session as create_application_session,
    get_session as get_application_session,
    list_issues as list_application_issues,
    plan_form as plan_application_form,
    prepare_review as prepare_application_review,
    public_context as application_context,
    public_sessions as application_sessions,
    record_event as record_application_event,
    save_answer as save_application_answer,
    save_mapping as save_application_mapping,
    store_path as application_store_path,
    verify_submission_page as verify_application_submission_page,
)
from scripts import resume_evaluator
from scripts import resume_projects


ENGINE_SOURCE_PATH = Path(__file__).resolve()
ENGINE_EVALUATOR_SOURCE_PATH = Path(resume_evaluator.__file__).resolve()
ENGINE_RUNTIME_VERSION = "resume-studio-runtime-v4"


def _sha256_file(path: Path) -> str:
    """Return a stable source identity without shelling out to git.

    Resume Studio is commonly started by launchd from a dirty checkout.  A git
    commit is therefore not a sufficient runtime identity: the service can
    keep an old module loaded after the file on disk has changed.  Hashing the
    loaded script at import and comparing it with the current file gives the
    service an honest, local-only stale-process check.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unavailable"
    return digest.hexdigest()


ENGINE_LOADED_SOURCE_FINGERPRINT = _sha256_file(ENGINE_SOURCE_PATH)
ENGINE_LOADED_EVALUATOR_SOURCE_FINGERPRINT = _sha256_file(ENGINE_EVALUATOR_SOURCE_PATH)
_TRACKER_SYNC_LOCK = threading.Lock()
_TRACKER_SYNC_LAST_REQUEST = 0.0
TRACKER_SYNC_MIN_INTERVAL_SECONDS = 15 * 60


def request_tracker_sync(root: Optional[Path] = None) -> Dict[str, Any]:
    """Nudge the existing secret-backed GitHub tracker reconciliation.

    The local companion never receives the Notion secret. It asks the already
    configured workflow to run, at most once per interval, so a paired Mac can
    recover from GitHub's delayed scheduled runs without owner clicks.
    """
    global _TRACKER_SYNC_LAST_REQUEST
    base = (root or repo_root()).resolve()
    now = time.time()
    with _TRACKER_SYNC_LOCK:
        remaining = TRACKER_SYNC_MIN_INTERVAL_SECONDS - (now - _TRACKER_SYNC_LAST_REQUEST)
        if _TRACKER_SYNC_LAST_REQUEST and remaining > 0:
            return {"ok": True, "triggered": False, "retry_after_seconds": int(remaining)}
        _TRACKER_SYNC_LAST_REQUEST = now
    gh = shutil.which("gh")
    if not gh:
        with _TRACKER_SYNC_LOCK:
            _TRACKER_SYNC_LAST_REQUEST = 0.0
        return {"ok": False, "triggered": False, "error": "GitHub CLI is unavailable"}
    try:
        result = subprocess.run(
            [gh, "workflow", "run", "tracker-sync.yml"],
            cwd=str(base), capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        with _TRACKER_SYNC_LOCK:
            _TRACKER_SYNC_LAST_REQUEST = 0.0
        return {"ok": False, "triggered": False, "error": str(exc)[:300]}
    if result.returncode != 0:
        with _TRACKER_SYNC_LOCK:
            _TRACKER_SYNC_LAST_REQUEST = 0.0
        return {"ok": False, "triggered": False, "error": (result.stderr or result.stdout or "workflow dispatch failed")[:300]}
    return {"ok": True, "triggered": True, "message": "Notion tracker reconciliation requested"}


def engine_runtime_identity(workers: Optional[int] = None) -> Dict[str, Any]:
    """Describe the code and evaluator contract currently serving requests."""
    disk_fingerprint = _sha256_file(ENGINE_SOURCE_PATH)
    disk_evaluator_fingerprint = _sha256_file(ENGINE_EVALUATOR_SOURCE_PATH)
    evaluator_stale = disk_evaluator_fingerprint != ENGINE_LOADED_EVALUATOR_SOURCE_FINGERPRINT
    stale = disk_fingerprint != ENGINE_LOADED_SOURCE_FINGERPRINT or evaluator_stale
    value: Dict[str, Any] = {
        "version": ENGINE_RUNTIME_VERSION,
        "pid": os.getpid(),
        "loaded_source_fingerprint": ENGINE_LOADED_SOURCE_FINGERPRINT,
        "disk_source_fingerprint": disk_fingerprint,
        "loaded_evaluator_source_fingerprint": ENGINE_LOADED_EVALUATOR_SOURCE_FINGERPRINT,
        "disk_evaluator_source_fingerprint": disk_evaluator_fingerprint,
        "evaluator_source_stale": evaluator_stale,
        "restart_required": stale,
        "evaluator_contract": resume_evaluator.contract_fingerprint(),
    }
    if workers is not None:
        value["workers"] = int(workers)
    return value


DEFAULT_RUN_WORKERS = 2
MAX_RUN_WORKERS = 4
RUN_STALE_AFTER_SECONDS = 30 * 60


def configured_run_workers(value: Any = None) -> int:
    """Return a bounded worker count for local tailoring runs.

    Two full runs is the safe default for a Mac-bound Codex queue.  The
    environment knob is intentionally capped so a typo or an overenthusiastic
    batch cannot silently create an unbounded provider storm.
    """
    raw = os.environ.get("RESUME_STUDIO_WORKERS") if value is None else value
    try:
        parsed = int(str(raw).strip()) if raw is not None and str(raw).strip() else DEFAULT_RUN_WORKERS
    except (TypeError, ValueError):
        parsed = DEFAULT_RUN_WORKERS
    return max(1, min(MAX_RUN_WORKERS, parsed))


def timestamp_age_seconds(value: Any) -> Optional[float]:
    """Return the age of an ISO timestamp, or ``None`` when it is invalid."""
    stamp = str(value or "").strip()
    if not stamp:
        return None
    try:
        return max(0.0, time.time() - dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


# The deterministic gates and the sealed Luna evaluator have separate frozen
# contracts.  Naming the former explicitly prevents a reviewer from reading
# the two version strings as if they were one rubric.
RUBRIC_VERSION = "resume-deterministic-gates-v1"
OBJECTIVE_RESUME_RUBRIC_VERSION = "objective-resume-v1"
JOB_INTELLIGENCE_VERSION = "job-intelligence-v2"
TAILORING_BRIEF_VERSION = "tailoring-brief-v1"
TAILORING_AUDIT_VERSION = "tailoring-audit-v2"
COMPARISON_CONTROL_VERSION = "comparison-control-v1"
IMMUTABLE_COMPARISON_CONTROL_ID = "immutable-default"
CODEX_LUNA_MODEL = "gpt-5.6-luna"
CODEX_REVIEW_MODE = "codex_luna_multi_role_jury"
SEALED_EVALUATOR_CONTRACT = resume_evaluator.EVALUATOR_CONTRACT_VERSION
# High is the single ordinary production effort. Max remains available only
# through an explicit quality-frontier profile or override.
CODEX_RECHECK_EFFORT = "high"
CODEX_CRITIC_ROLES = resume_evaluator.ROLE_DEFINITIONS
QUALITY_PROFILE_DEFAULT = "balanced"
QUALITY_PROFILES = {
    # The deep lane remains available for controlled comparisons and difficult
    # owner-reviewed jobs. It preserves the original frontier behavior.
    "deep": {
        "author_effort": "max",
        "line_editor_effort": "max",
        "line_editor_timeout_seconds": 6 * 60,
        "evaluator_effort": "max",
        "revision_rounds": 2,
        "model_space_expansion": True,
        "model_line_editor": True,
        "line_editor_fallback": False,
        "audit_repair": True,
        "evaluator_timeout_seconds": 8 * 60,
        "max_post_line_density_rounds": 4,
    },
    # Balanced is the normal application lane: deterministic density and
    # control-preservation run first, while Luna is reserved for decisions the
    # compiler cannot safely make. This removes two common serial calls without
    # weakening the sealed evaluator or any hard gate.
    "balanced": {
        # High is the one ordinary Luna lane; the independent evaluator uses
        # the same effort so timing comparisons are easy to interpret.
        "author_effort": "high",
        "line_editor_effort": "high",
        # Geometry editing is a fallback, not a second author. If Luna does
        # not return promptly, the source-preserving compactor and sealed
        # panel still receive a complete candidate.
        "line_editor_timeout_seconds": 3 * 60,
        "evaluator_effort": "high",
        # The normal lane is a bounded final-candidate experiment: let the
        # sealed panel judge the compiled candidate once, then select base or
        # tailored. Critic-driven rewriting remains available in ``deep``;
        # making it routine caused revision + audit-repair cascades to dominate
        # wall time without producing a positive Stryker comparison.
        "revision_rounds": 0,
        "model_space_expansion": False,
        "model_line_editor": False,
        "line_editor_fallback": True,
        # A measured spare line is not permission to swap out a stronger
        # mechanism or validation result. The lab found that a 0.03--0.06pt
        # capacity signal caused density recovery to replace exactly the
        # distinctive evidence the comparative jury later said was lost.
        "deterministic_space_expansion": False,
        "role_evidence_floor": False,
        "audit_repair": False,
        # A single Luna role occasionally needs just over six minutes.
        # The normal lane is still bounded to one panel; this ceiling avoids
        # turning provider jitter into a false incomplete review while keeping
        # the old multi-round cascade out of the default path.
        "evaluator_timeout_seconds": 8 * 60,
        "max_post_line_density_rounds": 2,
    },
    # ``search`` is the quality-first experimental lane. It wraps several
    # complete ``search_single`` candidates; each candidate is still judged
    # by the unchanged sealed jury before it can replace the base control.
    "search": {
        "author_effort": "high",
        "line_editor_effort": "high",
        "line_editor_timeout_seconds": 3 * 60,
        "evaluator_effort": "high",
        "revision_rounds": 0,
        "model_space_expansion": False,
        "model_line_editor": False,
        "line_editor_fallback": True,
        # Search compares whole authored candidates. Do not mutate one after
        # authoring merely because the page has microscopic spare capacity.
        "deterministic_space_expansion": False,
        "role_evidence_floor": False,
        # A rejected candidate gets one critique-directed repair, then a
        # fresh sealed panel. This is the quality-first distinction from the
        # old single-shot tailor: critique may inform a new draft, but cannot
        # approve the draft it just caused.
        "audit_repair": True,
        "evaluator_timeout_seconds": 8 * 60,
        "max_post_line_density_rounds": 2,
        "candidate_variants": 3,
        "child_profile": "search_single",
    },
    # Internal child profile for the portfolio-search wrapper. Keeping it
    # separate from balanced lets the search policy evolve independently from
    # ordinary single-candidate runs.
    "search_single": {
        "author_effort": "high",
        "line_editor_effort": "high",
        "line_editor_timeout_seconds": 3 * 60,
        "evaluator_effort": "high",
        "revision_rounds": 0,
        "model_space_expansion": False,
        "model_line_editor": False,
        "line_editor_fallback": True,
        "deterministic_space_expansion": False,
        "role_evidence_floor": False,
        "audit_repair": True,
        "evaluator_timeout_seconds": 8 * 60,
        "max_post_line_density_rounds": 2,
        "candidate_variants": 0,
        "child_profile": "",
    },
    # This is the production policy for the UI's Unchained generation mode.
    # It gets one high-effort source-grounded author, deterministic packing and
    # control recovery, then a bounded source-grounded opportunity counterfactual
    # before the sealed jury.  The lab's repair loop did not produce a positive
    # Stryker comparison and added roughly twelve minutes, so Unchained does not
    # pay for that blind rewrite/recheck cascade.  It intentionally does not pay
    # for three independent portfolio candidates; that remains a lab/search
    # policy until its quality win rate justifies the latency.
    "unchained": {
        # High is intentional here: the Max generation call timed out after
        # 720s on a full requirement map, while the quality-critical repair
        # and sealed jury still run with their stronger bounded lanes.
        "author_effort": "high",
        # Requirement mapping is planning, not final prose. High keeps it
        # useful without spending the Max budget on duplicated strategy text.
        "gap_analysis_effort": "high",
        "line_editor_effort": "high",
        "line_editor_timeout_seconds": 3 * 60,
        "evaluator_effort": "high",
        "revision_rounds": 0,
        "model_space_expansion": False,
        # A clean page is not an instruction to fill every remaining line.
        # Unchained should preserve a coherent skim and let the sealed panel
        # judge a deliberately short candidate rather than creating density
        # churn from deterministic unused-bullet guesses.
        "deterministic_space_expansion": False,
        "model_line_editor": False,
        "line_editor_fallback": True,
        "audit_repair": False,
        "target_opportunity_replacement": True,
        "evaluator_timeout_seconds": 8 * 60,
        "max_post_line_density_rounds": 2,
        "candidate_variants": 0,
        "child_profile": "",
    },
}
CANONICAL_CONTROL_PROMPT_LIMIT = 12
CANONICAL_CONTROL_PROMPT_CHARS = 7000
REVIEW_CRITERIA = (
    "factual", "target_fit", "evidence", "distinctiveness", "clarity", "privacy",
)
STATUS_MULTIPLIER = {"pass": 1.0, "partial": 0.5, "fail": 0.0}

# These are deliberately different editorial hypotheses, not three copies of
# "add more keywords." Every hypothesis still uses the same source-addressed
# schema and must beat the canonical base through the same sealed evaluator
# contract before it can become the primary artifact.
PORTFOLIO_SEARCH_VARIANTS = (
    {
        "id": "control_preserver",
        "label": "Control-preserving marginal-value pass",
        "instruction": (
            "Start from the canonical resume as a strong control. Make only changes with a clear, material "
            "target-specific gain. Preserve quantified proof, external validation, technical mechanisms, "
            "communication/ownership evidence, and scope qualifiers unless an actually stronger replacement "
            "is present. A near-base candidate is a successful outcome; do not create novelty for its own sake. "
            "Prefer at most a few high-confidence changes over broad portfolio churn."
        ),
    },
    {
        "id": "product_integration",
        "label": "Product and integration portfolio pass",
        "instruction": (
            "Build the strongest product-facing software argument the posting actually supports: surface user-facing "
            "interfaces, APIs, data flow, reliability, security, integration, documentation, and stakeholder-facing "
            "evidence when authorized. Keep the resume's most distinctive proof and interview threads; never trade a "
            "strong mechanism or validation result for a generic domain keyword. Treat repeated AI/RAG/backend lines "
            "as competing for one information budget, not as independent reasons to add more."
        ),
    },
    {
        "id": "systems_ml",
        "label": "Systems and technical-conviction portfolio pass",
        "instruction": (
            "Build a technically deep but complementary systems/ML argument: show concrete APIs, data systems, "
            "security, performance, validation, algorithms, or compute mechanisms that the target values. Avoid "
            "duplicating the same agent/RAG/cloud story across sections. Preserve communication, leadership, "
            "external-validation, and unusual technical evidence when they add distinct conviction. Unsupported "
            "testing, process, framework, or technology requirements remain explicit gaps."
        ),
    },
)


def normalize_quality_profile(value: Any) -> str:
    """Return a known authoring profile without changing evaluator policy."""
    name = str(value or QUALITY_PROFILE_DEFAULT).strip().lower()
    return name if name in QUALITY_PROFILES else QUALITY_PROFILE_DEFAULT


def actionable_unsupported_claims(values: Any) -> Tuple[List[str], List[str]]:
    """Separate actual unsupported-claim findings from explicit negations.

    Critic prompts ask for a list, and Luna sometimes places a sentence such
    as ``No unsupported claim was identified; ... are unsupported target
    terms, not claims made`` in that field. Treating that sentence as a
    factual violation is an evaluator false positive: it describes a role
    gap, not a fabricated resume statement. Preserve the sentence for audit
    visibility while excluding it from the hard factual gate.
    """
    if not isinstance(values, list):
        values = [values]
    actionable: List[str] = []
    negated: List[str] = []
    explicit_no_claim = re.compile(
        r"^\s*no\b.*\b(?:unsupported|fabricated|exaggerated|false|hallucinated)\b"
        r".*\b(?:claim|bullet|resume)\b.*\b(?:identified|detected|found|present)\b",
        re.I | re.S,
    )
    not_a_claim = re.compile(
        r"\b(?:not|never)\s+(?:an?\s+)?(?:claim|bullet|resume claim)\b"
        r"|\b(?:not|never)\s+claims?\s+(?:made|rendered|present)\b",
        re.I,
    )
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if explicit_no_claim.search(text) or not_a_claim.search(text):
            negated.append(text[:700])
        else:
            actionable.append(text[:700])
    return actionable[:20], negated[:20]

# A critic's ``blocking_issues`` field is intentionally broad: it gives a
# writer useful repair material, but it must not be treated as a list of
# equally severe application blockers.  These patterns are a deterministic
# post-processing boundary between (a) claims/safety/layout failures, (b)
# tailoring regressions, and (c) honest candidate-role gaps.  The evaluator
# remains free to describe a concern in its own words; this layer decides how
# that concern affects readiness.
CRITIC_HARD_ISSUE_RE = re.compile(
    r"\b(unsupported\s+(?:or\s+)?(?:fabricated|exaggerated|false)?\s*claim|"
    r"fabricat(?:ed|ion)|hallucinat(?:ed|ion)|invented\s+(?:claim|experience)|"
    r"false\s+claim|privacy|confidential|secret|ineligible|eligibility\s+conflict|"
    r"visa\s+(?:conflict|requirement)|credential\s+(?:conflict|absent)|"
    r"degree\s+(?:conflict|required|mismatch)|parse(?:r|ing)?\s+(?:failure|error)|"
    r"compil(?:e|ation)\s+(?:failure|error)|near[- ]?wrap\w*|wrap\w*\s+(?:bullet|line)|"
    r"right[- ]side\s+slack|one[- ]line\s+(?:safety|check)|"
    r"layout\s+(?:failure|defect|check|gate|unsafe|overflow|regression|problem|issue|contract)|"
    r"layout\s+(?:has|shows?|contains?)\s+(?:a\s+)?(?:material\s+)?(?:failure|defect|overflow|problem|issue)|"
    r"readability\s+(?:failure|defect|risk|regression|problem|issue)|"
    r"(?:poor|material|unreadable|fragile)\s+readability)\b",
    re.I,
)
CRITIC_FIT_GAP_RE = re.compile(
    r"\b(?:not\s+(?:demonstrated|demonstrate|visible|established|explicit|evidenced|shown|legible)|"
    r"does\s+not\s+(?:\w+\s+){0,2}(?:demonstrate|evidence|establish|show)|"
    r"doesn['’]t\s+(?:\w+\s+){0,2}(?:demonstrate|evidence|show)|"
    r"not\s+(?:authorized|supported)\s+(?:by\s+)?(?:the\s+)?(?:evidence|packet|source)|"
    r"unsupported\s+(?:exact\s+)?(?:term|phrase|requirement)|no\s+authorized\s+evidence|"
    r"\b(?:absent|missing|gap|thin|limited)\b|not\s+at\s+the\s+level)\b",
    re.I,
)
CRITIC_REGRESSION_RE = re.compile(
    r"\b(?:duplicat(?:e|ed|es|ion)|redundan(?:t|cy)|repetition|repeat\w*|overlap(?:s|ping)?|"
    r"lost|drop\w*|remov(?:e|ed|es|al)|omitt(?:ed|ing|s)|silently|unexplained|"
    r"no\s+longer|weaken\w*|sacrific\w*|"
    r"without[^.]{0,80}tradeoff|without\s+explanation|"
    r"hides?\s+(?:high[- ]value|distinctive)|not\s+dominant|not\s+legible)",
    re.I,
)


def classify_critic_issue(issue: Any) -> str:
    """Classify one critic concern without letting fit gaps become blockers."""
    text = str(issue or "").strip()
    if not text:
        return "quality_concern"
    if CRITIC_HARD_ISSUE_RE.search(text):
        return "hard_blocker"
    # A sentence can mention both an absent requirement and the reason it is
    # absent. Prefer the tailoring-regression interpretation when it says that
    # authorized/base evidence was removed or replaced by repetitive content.
    if CRITIC_REGRESSION_RE.search(text):
        return "tailoring_regression"
    if CRITIC_FIT_GAP_RE.search(text):
        return "candidate_fit_gap"
    return "quality_concern"


def critic_issue_finding(issue: Any, kind: str = "") -> Tuple[str, str]:
    """Map a normalized critic kind to the audit taxonomy and severity."""
    resolved = kind or classify_critic_issue(issue)
    if resolved == "hard_blocker":
        return "BLOCKER", "critical"
    if resolved == "tailoring_regression":
        return "REGRESSION", "warning"
    if resolved == "candidate_fit_gap":
        return "QUESTIONABLE", "warning"
    return "QUESTIONABLE", "warning"


def _critic_issue_cluster(issue: Any, kind: str) -> str:
    """Return a narrow semantic family for issues with highly variable prose."""
    text = str(issue or "")
    if kind == "hard_blocker" and re.search(
        r"near[- ]?wrap|wrap\w*|right[- ]side\s+slack|one[- ]line|layout|readability",
        text, re.I,
    ):
        return "layout_safety"
    if kind == "tailoring_regression":
        # A panel commonly describes one loss in three different ways: the
        # recruiter names the visible qualifier, the screening critic names
        # the quantified selection, and the evidence critic names the
        # underlying replacement.  These are one opportunity-cost issue, not
        # three regressions.  Keep the families deliberately narrow so a
        # generic "removed" sentence cannot collapse unrelated changes.
        if re.search(
            r"\b(?:fellowship|funding|selection|external[- ]validation|prestige|selected\s+(?:for|to\s+lead))\b",
            text, re.I,
        ):
            return "external_validation_loss"
        if re.search(
            r"\b(?:sqlite|schema|tables?|data[- ]fusion|data[- ]architecture|unif(?:y|ied|ying)|integration\s+evidence)\b",
            text, re.I,
        ):
            return "data_architecture_loss"
        if re.search(
            r"\b(?:redundan(?:t|cy)|overlap(?:s|ping)?|repeats?|duplicate(?:d|s)?|repetition)\b",
            text, re.I,
        ):
            return "portfolio_overlap"
        if re.search(
            r"\b(?:prototype|proof\s+of\s+concept|\bpoc\b|demo|scope[- ]limiting)\b",
            text, re.I,
        ):
            return "scope_qualifier_loss"
    return ""


def _critic_issue_tokens(value: Any) -> set:
    """Return stable signal tokens used only to collapse repeated panel prose."""
    tokens = _resume_tokens(str(value or ""))
    result = {
        token for token in tokens
        if len(token) > 2 and token not in {
            "resume", "resume's", "tailored", "version", "target", "posting",
            "role", "candidate", "evidence", "signal", "story", "line", "lines",
            "bullet", "bullets", "section", "selected", "selection", "supplied",
            "visible", "appears", "appearing", "central", "specific", "material",
            "high", "value", "technical", "direct", "concrete", "multiple",
        }
    }
    # Preserve normalized numeric anchors.  ``_resume_tokens`` splits
    # ``$8,000`` into ``8`` and ``000``; the normalized anchor lets the
    # fellowship loss cluster across critics without making short numeric
    # fragments part of the generic prose similarity calculation.
    result.update(
        "number:" + anchor.lstrip("$").replace(",", "")
        for anchor in _resume_numeric_anchors(str(value or ""))
        if anchor
    )
    return result


def collapse_critic_issues(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse near-identical concerns while retaining role agreement.

    The raw child JSON remains untouched.  The parent report receives one
    finding per underlying issue plus ``supporting_roles`` and
    ``support_count`` so a four-role consensus is visible without counting
    four phrasings as four independent regressions.
    """
    collapsed: List[Dict[str, Any]] = []
    for record in records:
        data = record.get("data") if isinstance(record, dict) else {}
        role = str(record.get("critic_role") or "") if isinstance(record, dict) else ""
        for raw_issue in (data.get("blocking_issues") or []) if isinstance(data, dict) else []:
            issue = str(raw_issue or "").strip()
            if not issue:
                continue
            kind = classify_critic_issue(issue)
            cluster = _critic_issue_cluster(issue, kind)
            tokens = _critic_issue_tokens(issue)
            match = None
            for existing in collapsed:
                if existing.get("kind") != kind:
                    continue
                if cluster and existing.get("cluster") == cluster:
                    match = existing
                    break
                other = set(existing.get("tokens") or [])
                if issue.casefold() == str(existing.get("issue") or "").casefold():
                    match = existing
                    break
                if tokens and other:
                    overlap = len(tokens & other) / max(1, min(len(tokens), len(other)))
                    union = len(tokens & other) / max(1, len(tokens | other))
                    if overlap >= 0.50 or union >= 0.30:
                        match = existing
                        break
            if match is None:
                classification, severity = critic_issue_finding(issue, kind)
                match = {
                    "issue": issue,
                    "kind": kind,
                    "cluster": cluster,
                    "classification": classification,
                    "severity": severity,
                    "supporting_roles": [],
                    "support_count": 0,
                    "variants": [],
                    "tokens": sorted(tokens),
                }
                collapsed.append(match)
            if role and role not in match["supporting_roles"]:
                match["supporting_roles"].append(role)
            match["support_count"] = len(match["supporting_roles"])
            if issue != match["issue"] and issue not in match["variants"] and len(match["variants"]) < 4:
                match["variants"].append(issue)
    for item in collapsed:
        item.pop("tokens", None)
        item.pop("cluster", None)
        item["agreement"] = "consensus" if item["support_count"] >= 2 else "single_role"
    return collapsed


def sealed_panel_status(records: Iterable[Dict[str, Any]], roles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Return exact role completeness for one sealed evaluator round.

    Counting successful records is insufficient: a duplicated role or a
    successful non-sealed record must never substitute for a missing critic.
    This helper is intentionally deterministic and order-stable for reports
    and tests.
    """
    required = [str(item.get("key") or "") for item in roles if str(item.get("key") or "")]
    required_set = set(required)
    attempted_set = {
        str(record.get("critic_role") or "")
        for record in records
        if str(record.get("critic_role") or "") in required_set
    }
    completed_set = {
        str(record.get("critic_role") or "")
        for record in records
        if record.get("ok")
        and record.get("execution_lane") == "sealed_evaluator"
        and record.get("contract_version") == SEALED_EVALUATOR_CONTRACT
        and str(record.get("critic_role") or "") in required_set
    }
    return {
        "complete": completed_set == required_set and len(completed_set) == len(required_set),
        "required_roles": required,
        "attempted_roles": [role for role in required if role in attempted_set],
        "completed_roles": [role for role in required if role in completed_set],
        "failed_roles": [role for role in required if role not in completed_set],
    }
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
# A single geometry-aware editor pass is enough before the deterministic
# compactor takes over.  A second frontier call was usually paraphrase churn
# after the first pass had already made the line safe.
MAX_LINE_EDIT_PASSES = 1

_ACTIVE_PROVIDER_PROCESSES: Dict[int, subprocess.Popen] = {}
_ACTIVE_PROVIDER_PROCESSES_LOCK = threading.Lock()


def register_provider_process(proc: subprocess.Popen) -> None:
    """Track provider launchers so a launchd stop can reap the full tree."""
    with _ACTIVE_PROVIDER_PROCESSES_LOCK:
        _ACTIVE_PROVIDER_PROCESSES[proc.pid] = proc


def unregister_provider_process(proc: subprocess.Popen) -> None:
    with _ACTIVE_PROVIDER_PROCESSES_LOCK:
        _ACTIVE_PROVIDER_PROCESSES.pop(proc.pid, None)


def stop_all_provider_processes() -> None:
    """Terminate every provider process owned by this engine instance."""
    with _ACTIVE_PROVIDER_PROCESSES_LOCK:
        processes = list(_ACTIVE_PROVIDER_PROCESSES.values())
    for proc in processes:
        stop_provider_process(proc)
MAX_SPACE_EXPANSION_CANDIDATES = 4
# Keep the two-bullet replacement path available, but bound its compiled
# frontier.  The old six-item frontier tried 6 + C(6, 2) = 21 removal sets
# for every addition; each trial compiles LaTeX, so equivalent low-value
# swaps could dominate an otherwise bounded run.
# Two lower-value removals are enough to test an atomic stronger replacement.
# A four-item frontier created 4 + C(4, 2) compiled trials per addition, which
# was expensive and encouraged low-signal portfolio churn.
MAX_SPACE_SWAP_CANDIDATES = 2
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
# A valid tailored artifact must contain substantive evidence. Section floors
# remain adaptive (an experience/project section may be omitted), but the
# compiler may never satisfy the one-page contract by deleting every bullet.
MIN_TOTAL_BULLETS = 1
MAX_TOTAL_BULLETS = MAX_RENDERED_BULLETS
# A one-page PDF can technically fit more, but a recruiter cannot skim every
# line with equal weight. These are editorial budgets for *tailored* plans,
# not claims about the immutable control. They prevent an author from winning
# page geometry by adding a fifth project or six near-duplicate experience
# bullets. The reducer below is source-addressed, conservative, and auditable.
HUMAN_PORTFOLIO_POLICY_VERSION = "human-skim-budget-v1"
HUMAN_PORTFOLIO_CAPS = {
    "total_bullets": 25,
    "project_entries": 4,
    "project_bullets": 11,
    "experience_bullets_per_entry": 5,
}
MAX_AUTHORITY_CONTEXT_CHARS = 16000
MAX_METHODOLOGY_CONTEXT_CHARS = 12000
MAX_CONTEXT_PROMPT_CHARS = 12000
MAX_CATALOG_PROMPT_CHARS = 18000
MAX_GRAPH_PROMPT_CHARS = 28000
MAX_TARGET_KEYWORDS = 48
MAX_AUDIT_FINDINGS = 80
TAILORING_PRIORITY_WEIGHTS = {
    "eligibility": 5,
    "required": 5,
    "preferred": 4,
    "responsibility": 3,
    "mentioned": 1,
}
MAX_WORKSHOP_TEXT_CHARS = 900
MAX_WORKSHOP_REQUEST_CHARS = 3000
MAX_WORKSHOP_REVISIONS = 100
MAX_BRIDGED_JOB_ID_CHARS = 240
MAX_BRIDGED_FIELD_CHARS = 500
TAILOR_MODE_ALIASES = {
    "strict": "used", "source": "used", "source-only": "used", "used": "used",
    "dream": "ai", "enhanced": "ai", "ai": "ai", "ai-enhanced": "ai",
    "free": "unrestricted", "unrestricted": "unrestricted",
    "unchained": "generation", "generate": "generation",
    "generation": "generation", "generative": "generation",
}
# Victor's ordinary lab preference is one Luna High effort level. Keep the task
# map explicit for auditability; Max is an explicit quality frontier rather
# than a hidden default that makes timing comparisons incomparable.
CODEX_EFFORTS = frozenset(("high", "max"))
CODEX_TASK_EFFORT_DEFAULTS = {
    "draft": "high",
    "synthesis": "high",
    "gap_analysis": "high",
    "space_expansion": "high",
    "line_edit": "high",
    "revision": "high",
    "review": "high",
    "workshop": "high",
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
    "software development", "software development life cycle", "sdlc",
    "object-oriented", "distributed systems", "data structures", "algorithms",
    "cloud computing", "natural language processing", "large language models",
    "generative ai", "retrieval augmented generation", "statistical analysis",
    "experimental design", "version control", "unit testing", "testing", "debugging",
    "troubleshooting", "continuous integration", "continuous improvement", "code reviews",
    "document", "documentation", "knowledge sharing", "agile", "agile methodology",
    "collaboration", "communication", "problem-solving", "business value creation",
    "data engineering", "data visualization", "data analytics", "data platforms",
    "scientific computing", "scientific research", "life sciences", "bioinformatics",
    "computational chemistry", "web frameworks", "ai-assisted development tools",
    "ai-enabled solutions", "learning new technologies",
    "linux", "bash", "slurm", "gpu", "cuda", "c++", "c#", "python", "java", "sql",
    "javascript", "typescript", "react", "pytorch", "tensorflow", "scikit-learn",
    "numpy", "pandas", "fastapi", "flask", "sharepoint", "power platform", "databricks",
    "docker", "kubernetes", "aws", "azure", "gcp", "google cloud", "alloydb", "postgresql",
    "postgres", "pgvector", "mongodb", "sqlite", "git", "github", "rest api", "api",
    "rag", "llm", "gemini", "agentic", "inference", "training", "quantization",
    "optimization", "hpc", "real-time", "multimodal", "data pipeline", "microservices",
    "pytest",
)
LEGACY_TARGET_KEYWORD_TERMS = (
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
    "pytest",
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


TAILORED_RESUME_DIRNAME = "tailored"
TAILORED_RESUME_INDEX = "index.json"
CURATED_RESUME_STATUSES = {"complete", "completed", "awaiting_review"}
OFFLINE_RESUME_DIRNAME = "offline"
OFFLINE_RESUME_INDEX = "index.json"


def tailored_resume_root(root: Optional[Path] = None) -> Path:
    """Return the visible folder containing the newest local resume copies."""
    return cv_root(root) / TAILORED_RESUME_DIRNAME


def offline_resume_root(root: Optional[Path] = None) -> Path:
    """Return the visible folder for unchanged offline role selections."""
    return tailored_resume_root(root) / OFFLINE_RESUME_DIRNAME


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
        raise ValueError("mode must be used, ai, unrestricted, or generation")
    return normalized


def tailor_mode_label(mode: str) -> str:
    return {
        "used": "Used bullets",
        "ai": "AI tailor",
        "unrestricted": "Take-the-wheel (moderate)",
        "generation": "Unchained generation",
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

    records = load_jobs(base)
    from radar.score import RULES_VERSION

    # The crawler persists the complete current score projection, including
    # quality/posting verdicts and concentration adjustments.  Rebuilding a
    # 45k-record in-memory projection on every Resume Studio launch made the
    # local UI appear dead for minutes even when no record was stale.  Only
    # take the expensive compatibility path when at least one record actually
    # predates the active rules.
    if records and all(
        rec.get("score_version") == RULES_VERSION
        and rec.get("rules_v") == RULES_VERSION
        for rec in records.values()
    ):
        _CURRENT_SCORE_CACHE[key] = {"signature": signature, "jobs": records}
        return records

    from radar import posting, quality
    from radar.models import Job
    import radar.score as score_mod
    from radar.score import (apply_company_concentration,
                             early_career_possible, explicit_new_grad, gates,
                             score, source_new_grad)

    records = copy.deepcopy(records)
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


def context_questions_for_job(
    job: Dict[str, Any], posting_text: str, root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Turn exact unsupported posting terms into one durable question per topic."""
    base = root or repo_root()
    strategy = target_keyword_strategy(
        {"posting_text": posting_text}, source_catalog(base), base,
        graph=evidence_graph(base), comprehensive=True,
    )
    gaps = []
    for item in strategy.get("terms") or []:
        if item.get("supported") or item.get("importance") == "mentioned":
            continue
        gaps.append({
            "term": item.get("term"),
            "importance": item.get("importance"),
            "job_id": job.get("id"),
            "company": job.get("company"),
            "title": job.get("title"),
            "url": job.get("url"),
        })
        if len(gaps) >= 16:
            break
    if gaps:
        upsert_questions(studio_root(base), gaps)
    return gaps


CONTEXT_HINT_ASSOCIATIONS = {
    "ci/cd": {"pipeline", "deployment", "deploy", "docker", "github", "git", "automation", "testing", "build", "workflow", "infrastructure"},
    "continuous integration": {"pipeline", "deployment", "deploy", "docker", "github", "git", "automation", "testing", "build", "workflow"},
    "continuous deployment": {"pipeline", "deployment", "deploy", "docker", "github", "git", "automation", "testing", "build", "workflow"},
    "version control": {"git", "github", "branch", "merge", "repository", "code review", "collaboration"},
    "cloud computing": {"cloud", "gcp", "google cloud", "aws", "azure", "alloydb", "docker", "deployment", "infrastructure"},
    "data visualization": {"visualization", "dashboard", "plot", "chart", "matplotlib", "tableau", "power bi", "looker"},
    "continuous improvement": {"iteration", "prototype", "testing", "feedback", "customer discovery", "optimization", "revision"},
}


def _context_candidate_hints(
    question: Dict[str, Any], graph: Dict[str, Any], limit: int = 4,
) -> List[Dict[str, Any]]:
    """Suggest plausible places to investigate without converting them to evidence."""
    term = str(question.get("term") or "").strip().lower()
    term_tokens = set(re.findall(r"[a-z0-9+#.]+", term))
    related = set()
    for key, values in CONTEXT_HINT_ASSOCIATIONS.items():
        if key in term or term in key:
            related.update(values)
    clue_tokens = term_tokens | set(
        token for value in related for token in re.findall(r"[a-z0-9+#.]+", value.lower())
    )
    grouped: Dict[str, Dict[str, Any]] = {}
    for node in graph.get("nodes", []):
        if not node.get("claim_allowed"):
            continue
        source = str(node.get("source") or "").strip()
        heading = _latex_plain(str(node.get("heading") or "")).strip()
        source_match = re.search(r"CV/experiences/([^/]+?)(?:_SOURCE_OF_TRUTH|_BULLET_ITERATION_LOG|\.md)", source, re.I)
        if source_match:
            stem = source_match.group(1).replace("_", " ").strip()
            known_places = {
                "jj": "Johnson & Johnson internship",
                "j&j": "Johnson & Johnson internship",
            }
            place_label = known_places.get(stem.lower(), stem.title() + " experience materials")
            group_key = "experience-source:" + stem.lower()
        else:
            place_label = heading
            group_key = str(node.get("entry_id") or heading).lower()
        if not source_match and (
            re.search(r"\b(skills?|technologies|nodes?|signals?|contradictions?|rendered set)\b", place_label, re.I)
            or len(place_label) < 5
        ):
            continue
        text = _latex_plain(str(node.get("text") or "")).strip()
        if not place_label or not text:
            continue
        haystack = (place_label + " " + heading + " " + text + " " + source).lower()
        matched = sorted({token for token in clue_tokens if len(token) > 2 and re.search(r"\b%s\b" % re.escape(token), haystack)})
        if not matched:
            continue
        score = len(matched) * 12 + int(node.get("authority") or 0) / 10
        if source_match:
            score += 8
        current = grouped.get(group_key)
        if current is None or score > current["score"]:
            grouped[group_key] = {
                "label": place_label[:220],
                "source": source[:500],
                "source_url": "",
                "matched_clues": matched[:6],
                "score": score,
                "kind": "evidence-neighbor",
                "claim_allowed": False,
            }
    candidates = []
    seen_places = set()
    for candidate in sorted(grouped.values(), key=lambda item: (-item["score"], item["label"])):
        place_key = re.sub(
            r"\b(ai|data|science|internship|intern|experience|materials)\b", " ",
            re.sub(r"[^a-z0-9]+", " ", candidate["label"].lower()),
        )
        place_key = " ".join(place_key.split())
        if place_key in seen_places:
            continue
        seen_places.add(place_key)
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    for item in candidates:
        clues = ", ".join(item.pop("matched_clues", []))
        item.pop("score", None)
        item["reason"] = "Related evidence mentions %s; that is a clue to investigate, not proof." % clues
        item["question"] = (
            "Did you use %s in %s—for example around %s? If yes, what did you personally configure, "
            "where did it run, and what happened?"
        ) % (term, item["label"], clues)
    owner_hints = []
    for hint in question.get("hints") or []:
        if not isinstance(hint, dict) or not str(hint.get("label") or "").strip():
            continue
        label = str(hint.get("label") or "").strip()
        note = str(hint.get("note") or "").strip()
        owner_hints.append({
            "id": hint.get("id"),
            "label": label,
            "source": "Owner-supplied place to investigate",
            "source_url": str(hint.get("source_url") or ""),
            "reason": note or "You added this as a possible place to check; it is not resume evidence yet.",
            "question": "Did you use %s in %s? What did you personally build or configure, using which tools, and what was the outcome?" % (term, label),
            "kind": "owner-hint",
            "claim_allowed": False,
        })
    dismissed = {str(value).strip().lower() for value in (question.get("dismissed_hints") or []) if str(value).strip()}
    candidates = [item for item in candidates if str(item.get("label") or "").strip().lower() not in dismissed]
    return (owner_hints + candidates)[: max(1, min(int(limit or 4), 8))]


def context_inventory(
    root: Optional[Path] = None,
    job: Optional[Dict[str, Any]] = None,
    posting_text: str = "",
    limit: int = 240,
) -> Dict[str, Any]:
    """Explain what Studio knows, where it came from, and its durable gaps."""
    base = root or repo_root()
    if job and posting_text:
        context_questions_for_job(job, posting_text, base)
    graph = evidence_graph(base)
    reviews = load_reviews(studio_root(base))
    questions = []
    for item in (reviews.get("questions") or {}).values():
        if not isinstance(item, dict):
            continue
        view = dict(item)
        view["candidate_hints"] = _context_candidate_hints(view, graph)
        questions.append(view)
    questions.sort(key=lambda item: (
        item.get("status") == "answered",
        {"required": 0, "preferred": 1, "responsibility": 2}.get(
            str(((item.get("triggers") or [{}])[-1] or {}).get("importance") or ""), 3
        ),
        str(item.get("term") or ""),
    ))
    facts = []
    source_counts: Dict[str, int] = {}
    for node in graph.get("nodes", []):
        if not node.get("claim_allowed") or not str(node.get("text") or "").strip():
            continue
        kind = str(node.get("source_kind") or "local evidence")
        source_counts[kind] = source_counts.get(kind, 0) + 1
        facts.append({key: node.get(key) for key in (
            "id", "source", "heading", "text", "authority", "source_kind",
            "review_status", "reviewed_by", "reviewed_at",
        )})
    facts.sort(key=lambda item: (
        item.get("source_kind") != "owner-confirmed answer",
        -int(item.get("authority") or 0),
        str(item.get("source") or ""),
    ))
    answered = [item for item in questions if item.get("status") == "answered"]
    return {
        "version": graph.get("version"),
        "hash": graph.get("hash"),
        "summary": {
            "known_facts": len(facts),
            "source_kinds": source_counts,
            "open_questions": sum(item.get("status") != "answered" for item in questions),
            "answered_questions": len(answered),
            "confirmed_answers": sum(item.get("response") == "used" for item in answered),
            "known_absences": sum(item.get("response") == "not_used" for item in answered),
        },
        "questions": questions[:200],
        "facts": facts[: max(1, min(int(limit or 240), 500))],
        "privacy": "Source CV files and owner answers remain in the ignored private Mac workspace.",
    }


def update_context_answer(
    item_id: str, response: str, answer: str = "", where_when: str = "",
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    base = root or repo_root()
    save_context_answer(
        studio_root(base), item_id, response, answer=answer, where_when=where_when,
    )
    _EVIDENCE_GRAPH_CACHE.pop(str(base.resolve()), None)
    return context_inventory(base)


def update_context_hint(
    item_id: str, label: str, note: str = "", source_url: str = "",
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    base = root or repo_root()
    save_context_hint(
        studio_root(base), item_id, label=label, note=note, source_url=source_url,
    )
    return context_inventory(base)


def update_context_hint_dismissal(
    item_id: str, label: str, root: Optional[Path] = None,
) -> Dict[str, Any]:
    base = root or repo_root()
    dismiss_context_hint(studio_root(base), item_id, label=label)
    return context_inventory(base)


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


def resume_pdf_filename(job: Dict[str, Any], mode: str = "ai") -> str:
    """Return Victor's stable, employer-facing filename for a generated PDF.

    ``mode`` remains an accepted argument for callers and old records, but it
    intentionally does not appear in the filename. A resume should identify
    its owner and target company without exposing an internal generation lane.
    """
    company = re.sub(r"[^a-z0-9]+", "_", str(job.get("company") or "company").lower()).strip("_")
    return "victor_jimenez_%s.pdf" % (company[:64] or "company")


def _resume_record_timestamp(value: Any, fallback: Optional[float] = None) -> dt.datetime:
    """Parse a run timestamp, falling back to filesystem time when needed."""
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(fallback or time.time(), dt.timezone.utc)


def export_local_tailored_resumes(
    root: Optional[Path] = None,
    since_days: Optional[int] = 14,
) -> Dict[str, Any]:
    """Keep one newest usable primary PDF per company in ``CV/tailored``.

    The private run directories remain the source of truth. This folder is a
    convenience export for local applications, so it deliberately excludes
    failed runs and diagnostic ``tailored_candidate.pdf`` files. A recent
    ``awaiting_review`` run is included but marked in ``index.json`` so Victor
    can inspect it before applying.
    """
    root = root or repo_root()
    destination = tailored_resume_root(root)
    destination.mkdir(parents=True, exist_ok=True)
    cutoff = None
    if since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(0, int(since_days)))

    newest: Dict[str, Tuple[dt.datetime, Path, Dict[str, Any], Dict[str, Any], Path]] = {}
    runs = studio_root(root) / "runs"
    if runs.is_dir():
        for run_dir in runs.iterdir():
            if not run_dir.is_dir():
                continue
            status = read_json(run_dir / "status.json", {}) or {}
            if not isinstance(status, dict) or status.get("status") not in CURATED_RESUME_STATUSES:
                continue
            job = read_json(run_dir / "job.json", {}) or status.get("job") or {}
            if not isinstance(job, dict) or not str(job.get("company") or "").strip():
                continue
            pdf = run_pdf_path(run_dir)
            if not pdf.is_file():
                continue
            timestamp = _resume_record_timestamp(status.get("created_at"), run_dir.stat().st_mtime)
            if cutoff and timestamp < cutoff:
                continue
            filename = resume_pdf_filename(job, str(status.get("mode") or "ai"))
            key = filename.lower()
            current = newest.get(key)
            candidate = (timestamp, run_dir, status, job, pdf)
            if current is None or (timestamp, run_dir.name) > (current[0], current[1].name):
                newest[key] = candidate

    managed_patterns = ("victor_jimenez_*.pdf", "victor_jimenez_*-preview.png")
    for pattern in managed_patterns:
        for existing in destination.glob(pattern):
            if existing.is_file():
                existing.unlink()

    entries: List[Dict[str, Any]] = []
    for filename, (timestamp, run_dir, status, job, pdf) in sorted(
        newest.items(), key=lambda item: item[1][0], reverse=True
    ):
        target = destination / filename
        shutil.copy2(pdf, target)
        preview = run_preview_path(run_dir)
        preview_name = Path(filename).stem + "-preview.png"
        if preview.is_file():
            shutil.copy2(preview, destination / preview_name)
        entries.append({
            "filename": filename,
            "preview_filename": preview_name if preview.is_file() else "",
            "company": str(job.get("company") or ""),
            "title": str(job.get("title") or ""),
            "created_at": timestamp.isoformat(timespec="seconds"),
            "status": str(status.get("status") or ""),
            "mode": str(status.get("mode") or ""),
            "run_id": run_dir.name,
            "review_required": str(status.get("status") or "") != "complete",
        })

    write_json(destination / TAILORED_RESUME_INDEX, {
        "generated_at": now_iso(),
        "source": "CV/.resume_studio/runs",
        "selection": "newest usable primary PDF per company",
        "since_days": since_days,
        "count": len(entries),
        "resumes": entries,
    })
    return {
        "folder": str(destination),
        "count": len(entries),
        "since_days": since_days,
        "resumes": entries,
    }


def run_pdf_path(run_dir: Path) -> Path:
    """Find the generated PDF for both new named runs and legacy runs."""
    status = read_json(run_dir / "status.json", {}) or {}
    configured = str(status.get("pdf_filename") or "").strip()
    if configured:
        return run_dir / Path(configured).name
    named = sorted(
        [*run_dir.glob("victor_jimenez_*.pdf"), *run_dir.glob("*_resume_*.pdf")]
    )
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
    name = Path(str(value or "victor_jimenez_company.pdf")).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if safe == "resume.pdf":
        return "victor_jimenez_company.pdf"
    return safe or "victor_jimenez_company.pdf"


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
    """Detect two phrasings of the same evidence story.

    The caller usually applies this within one source entry, but the same
    conservative test is also useful across an experience and project: a
    project repeating a quantified experience story is not new breadth. The
    mechanism fallback covers cases such as two All-NBA bullets that share
    resampling/stratified validation but do not repeat a number.
    """
    if _same_resume_bullet(left, right):
        return True
    shared_numbers = _resume_numeric_anchors(left) & _resume_numeric_anchors(right)
    if not shared_numbers:
        shared_mechanisms = _resume_tokens(left) & _resume_tokens(right) & {
            "resampling", "rebalancing", "stratified", "validation",
            "calibration", "debugging", "troubleshooting",
        }
        return len(shared_mechanisms) >= 2
    generic = {
        "built", "engineered", "developed", "designed", "implemented",
        "led", "using", "across", "real", "time", "data", "system",
        "systems", "workflow", "workflows", "project", "experience",
        "item", "items", "findings", "stakeholders", "presented",
        "improved", "created", "delivered", "with", "for",
    }
    shared_terms = {
        term for term in (_resume_tokens(left) & _resume_tokens(right)) - generic
        if re.search(r"[a-z]", term)
    }
    return bool(shared_terms)


def _same_cross_entry_resume_bullet(left: str, right: str) -> bool:
    """Use a stricter story test before dropping a project for an experience.

    Shared numbers plus one common word are enough to flag two lines inside a
    single entry for human review, but not enough to delete evidence across
    entries (for example, ``3 AI systems`` and a ``3-person team``). Cross-entry
    removal requires two distinctive shared terms or a strong shared
    mechanism bundle.
    """
    if _same_resume_bullet(left, right):
        return True
    shared_numbers = _resume_numeric_anchors(left) & _resume_numeric_anchors(right)
    generic = {
        "built", "engineered", "developed", "designed", "implemented",
        "led", "using", "across", "real", "time", "data", "system",
        "systems", "workflow", "workflows", "project", "experience",
        "item", "items", "findings", "stakeholders", "presented",
        "improved", "created", "delivered", "with", "for",
    }
    shared_terms = {
        term for term in (_resume_tokens(left) & _resume_tokens(right)) - generic
        if re.search(r"[a-z]", term)
    }
    if shared_numbers and len(shared_terms) >= 2:
        return True
    shared_mechanisms = shared_terms & {
        "resampling", "rebalancing", "stratified", "validation",
        "calibration", "debugging", "troubleshooting",
    }
    return len(shared_mechanisms) >= 2


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


# These are high-risk claim anchors: a writer may rephrase connective prose,
# but it must not move a technology, implementation surface, or concrete
# mechanism from another source bullet without citing that source. This is a
# deliberately narrow provenance lint, not a full semantic parser.
CLAIM_BOUNDARY_TECH_TERMS = (
    "c++", "c#", "python", "java", "javascript", "typescript", "react",
    "next.js", "bert", "fastapi", "flask", "pytorch", "tensorflow", "sql",
    "bash", "linux", "slurm", "gpu", "hpc", "jwt", "oauth", "rest", "api",
    "database", "sqlite", "mongodb", "postgres", "alloydb", "pgvector", "rag",
    "retrieval", "agent", "agents", "streaming", "asynchronous", "async",
    "backend", "frontend", "network", "storage", "cache", "schema", "endpoint",
    "dashboard", "simulation", "monte carlo", "row-level", "role-based",
    "multi-user", "computer vision",
)


def _unsupported_introduced_claim_anchors(
    source: str, candidate: str, authorized_sources: Iterable[str],
) -> List[str]:
    """Return high-risk terms in a rewrite absent from its cited sources."""
    authorized_text = " ".join(
        _latex_plain(str(value or "")) for value in authorized_sources
    )
    source_plain = _latex_plain(str(source or ""))
    candidate_plain = _latex_plain(str(candidate or ""))
    return [
        term for term in CLAIM_BOUNDARY_TECH_TERMS
        if _keyword_present(term, candidate_plain)
        and not _keyword_present(term, source_plain)
        and not _keyword_present(term, authorized_text)
    ]


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


def _project_tradeoff_source_errors(
    plan: Dict[str, Any], catalog: Dict[str, Any],
) -> List[str]:
    """Require exact source accountability when a canonical project is dropped.

    A project-level explanation is not enough to account for every mechanism
    inside the omitted project. Requiring each canonical bullet ID keeps a
    legitimate swap auditable and prevents one unmentioned high-information
    line from disappearing behind a broadly reasonable portfolio rationale.
    """
    selected_project_ids = {
        str(entry.get("source_id") or "")
        for entry in (plan.get("projects") or [])
        if isinstance(entry, dict)
    }
    ledger_source_ids = {
        str(source_id)
        for item in (plan.get("decision_ledger") or [])
        if isinstance(item, dict)
        for source_id in (item.get("source_ids") or [])
        if str(source_id)
    }
    errors = []
    for project in canonical_resume_benchmark(catalog).get("projects", []):
        entry_id = str(project.get("source_id") or "")
        if not entry_id or entry_id in selected_project_ids:
            continue
        missing = [
            str(source_id) for source_id in (project.get("bullet_ids") or [])
            if str(source_id) and str(source_id) not in ledger_source_ids
        ]
        if missing:
            errors.append(
                "project tradeoff for %s must name every omitted canonical bullet source_id; missing: %s"
                % (entry_id, ", ".join(missing[:8]))
            )
    return errors


def canonical_control_evidence(
    catalog: Dict[str, Any], keyword_strategy: Optional[Dict[str, Any]] = None,
    limit: int = 24,
) -> List[Dict[str, Any]]:
    """Rank high-information canonical lines used as a packing/control guard.

    This is deliberately not a preservation mandate. It gives the author and
    compiler a visible list of the base resume's strongest proof points so a
    lower-information keyword or project swap must clear a higher bar.
    """
    supported_terms = [
        str(item.get("term") or "")
        for item in (keyword_strategy or {}).get("terms") or []
        if isinstance(item, dict) and item.get("supported") and str(item.get("term") or "")
    ]
    ranked: List[Dict[str, Any]] = []
    for entry in (catalog.get("entries") or {}).values():
        for bullet in entry.get("bullets") or []:
            if not _is_canonical_source(bullet.get("source")):
                continue
            source_id = str(bullet.get("id") or "")
            text = _latex_plain(str(bullet.get("text") or ""))
            if not source_id or not text:
                continue
            numeric = sorted(_resume_numeric_anchors(text))
            families = _portfolio_signal_families(text)
            matched_terms = sorted({term for term in supported_terms if _keyword_present(term, text)})
            reasons = []
            if numeric:
                reasons.append("quantified proof")
            if families:
                reasons.append("distinct technical signal")
            if matched_terms:
                reasons.append("supported target terminology")
            # Numeric anchors and role-specific terms are the strongest
            # deterministic clues, but the score is only a packing tie-breaker.
            control_priority = (
                50
                + min(18, 6 * len(numeric))
                + min(12, 4 * len(families))
                + min(15, 5 * len(matched_terms))
            )
            ranked.append({
                "source_id": source_id,
                "entry_id": str(entry.get("id") or ""),
                "text": text,
                "control_priority": control_priority,
                "reasons": reasons,
                "numeric_anchors": numeric[:8],
                "signal_families": families[:8],
                "supported_terms": matched_terms[:8],
            })
    ranked.sort(key=lambda item: (-int(item.get("control_priority") or 0), str(item.get("source_id") or "")))
    return ranked[:max(1, int(limit))]


def canonical_control_plan(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """Build a source-addressed fallback from the immutable resume only.

    This is an execution fallback, not a second author. If Luna returns an
    empty or malformed plan, Resume Studio should still be able to compile and
    compare a known control rather than fail the application with no artifact.
    Every selected line is copied verbatim from the canonical template and is
    still sent through normal packing, geometry, and sealed evaluation.
    """
    sections = {"experience": "experiences", "project": "projects", "leadership": "leadership"}
    value: Dict[str, Any] = {
        "positioning_thesis": "Use the immutable resume as the safe control when the author lane is unavailable.",
        "selected_evidence": [],
        "excluded_evidence": [],
        "experiences": [],
        "projects": [],
        "leadership": [],
        "revision_notes": ["Deterministic canonical-control fallback; no new claims or rewrites."],
        "decision_ledger": [],
        "front_matter_policy": {"coursework": "keep", "awards": "keep"},
    }
    control_scores = canonical_control_scores(catalog)
    for entry in (catalog.get("entries") or {}).values():
        section = sections.get(str(entry.get("kind") or ""))
        if not section:
            continue
        bullets = [
            bullet for bullet in (entry.get("bullets") or [])
            if _is_canonical_source(bullet.get("source"))
        ]
        if not bullets:
            continue
        entry_id = str(entry.get("id") or "")
        value[section].append({
            "source_id": entry_id,
            "bullets": [{
                "source_id": str(bullet.get("id") or ""),
                "source_ids": [str(bullet.get("id") or "")],
                "text": str(bullet.get("text") or ""),
                "evidence_ids": [str(bullet.get("id") or "")],
                "priority": int(control_scores.get(str(bullet.get("id") or ""), 75)),
                "candidate_rationale": "Canonical source line retained verbatim as the control fallback.",
            } for bullet in bullets if str(bullet.get("id") or "") and str(bullet.get("text") or "")],
            "why": "Canonical immutable control entry retained verbatim.",
        })
        value["selected_evidence"].append({
            "source": entry_id,
            "why": "Immutable canonical source used because the author response was unavailable or invalid.",
        })
    return value


def canonical_control_scores(
    catalog: Optional[Dict[str, Any]] = None,
    keyword_strategy: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """Return deterministic control bonuses for compile-time removal search."""
    return {
        str(item.get("source_id") or ""): float(item.get("control_priority") or 0)
        for item in canonical_control_evidence(catalog or {}, keyword_strategy)
        if str(item.get("source_id") or "")
    }


def canonical_control_prompt(
    catalog: Dict[str, Any], keyword_strategy: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a bounded control receipt for author prompts.

    The full evidence catalog remains available to the writer. This compact
    section exists to prevent repeated prompt copies of every canonical line
    from becoming a latency tax across authoring and repair calls.
    """
    return json.dumps(
        canonical_control_evidence(
            catalog, keyword_strategy, limit=CANONICAL_CONTROL_PROMPT_LIMIT,
        ),
        indent=2,
        ensure_ascii=False,
    )[:CANONICAL_CONTROL_PROMPT_CHARS]


def _portfolio_signal_families(text: Any) -> List[str]:
    plain = _latex_plain(str(text or ""))
    return [
        family for family, pattern in PORTFOLIO_SIGNAL_PATTERNS.items()
        if re.search(pattern, plain, flags=re.I)
    ]


def comparison_control_summary(control: Any) -> Dict[str, Any]:
    """Return a cloud-safe receipt for the control selected for a run.

    A role-family control is a reference artifact, not a new source of truth.
    Keep the artifact text and local paths out of reports that may be synced to
    the cloud; the receipt is enough to explain which approved run was used and
    whether the engine fell back to the immutable resume.
    """
    value = control if isinstance(control, dict) else {}
    source = str(value.get("source") or "immutable")[:40]
    control_id = str(value.get("id") or IMMUTABLE_COMPARISON_CONTROL_ID)[:120]
    if control_id == IMMUTABLE_COMPARISON_CONTROL_ID or source == "immutable":
        artifact_label = "immutable canonical resume"
    elif source == "run":
        artifact_label = "approved tailored PDF"
    else:
        artifact_label = "private resume artifact"
    return {
        "version": COMPARISON_CONTROL_VERSION,
        "id": str(value.get("id") or IMMUTABLE_COMPARISON_CONTROL_ID)[:120],
        "label": str(value.get("label") or "Immutable default")[:180],
        "role_family": str(value.get("role_family") or "all")[:80],
        "source": source,
        "entry_id": str(value.get("entry_id") or "")[:80],
        "run_id": str(value.get("run_id") or value.get("entry_id") or "")[:80],
        "artifact": artifact_label,
        "available": bool(value.get("available")),
        "approved": bool(value.get("approved")),
        "selected": bool(value.get("selected", True)),
        "resolution": str(value.get("resolution") or "selected")[:40],
        "fallback_reason": str(value.get("fallback_reason") or "")[:280],
        "reference_only": bool(value.get("reference_only", False)),
    }


def immutable_comparison_control() -> Dict[str, Any]:
    """Describe the locked resume as the universal, always-available floor."""
    return {
        "version": COMPARISON_CONTROL_VERSION,
        "id": IMMUTABLE_COMPARISON_CONTROL_ID,
        "label": "Immutable default",
        "role_family": "all",
        "source": "immutable",
        "artifact": "immutable canonical resume",
        "available": True,
        "approved": True,
        "selected": True,
        "resolution": "fallback",
        "reference_only": False,
        "_baseline_tex": "",
    }


def resolve_comparison_control(
    root: Optional[Path] = None, requested: Any = None,
) -> Dict[str, Any]:
    """Resolve an owner-approved role control to a local private run artifact.

    The cloud queue carries only a sanitized run reference. If the reference
    is missing, stale, not approved, or no longer has a source artifact, the
    immutable resume wins automatically. This makes a cloud/UI mismatch a
    safe fallback rather than a reason to fail or silently use an old draft.
    """
    fallback = immutable_comparison_control()
    value = requested if isinstance(requested, dict) else {}
    requested_id = str(value.get("id") or "").strip()
    if not requested_id or requested_id == IMMUTABLE_COMPARISON_CONTROL_ID:
        return fallback
    source = str(value.get("source") or "run").strip().lower()
    entry_id = str(value.get("entry_id") or value.get("run_id") or "").strip()
    if source != "run" or not re.fullmatch(r"[a-f0-9]{12}", entry_id):
        fallback["resolution"] = "fallback"
        fallback["fallback_reason"] = "The selected role-family control reference was invalid."
        fallback["requested_id"] = requested_id[:120]
        return fallback
    directory = studio_root(root or repo_root()) / "runs" / entry_id
    status = read_json(directory / "status.json", {}) or {}
    report = read_json(directory / "report.json", {}) or {}
    approved = str(
        status.get("approval_state") or report.get("approval_state") or ""
    ).lower() == "approved"
    winner = str(
        report.get("winner_version")
        or (report.get("winner_artifact") or {}).get("winner_version")
        or ""
    ).lower()
    tex_path = directory / "resume.tex"
    pdf_path = run_pdf_path(directory)
    if (
        not directory.is_dir()
        or not approved
        or winner != "tailored"
        or not tex_path.is_file()
        or not pdf_path.is_file()
    ):
        reasons = []
        if not directory.is_dir() or not tex_path.is_file() or not pdf_path.is_file():
            reasons.append("the local control artifact is unavailable")
        if not approved:
            reasons.append("the source run is not owner-approved")
        if winner != "tailored":
            reasons.append("the source run did not publish a tailored winner")
        fallback["fallback_reason"] = "; ".join(reasons)[:280]
        fallback["requested_id"] = requested_id[:120]
        fallback["requested_entry_id"] = entry_id
        return fallback
    try:
        baseline_tex = tex_path.read_text(errors="replace")
    except OSError:
        baseline_tex = ""
    if not baseline_tex.strip():
        fallback["fallback_reason"] = "The selected role-family control had no readable source artifact."
        fallback["requested_id"] = requested_id[:120]
        fallback["requested_entry_id"] = entry_id
        return fallback
    return {
        "version": COMPARISON_CONTROL_VERSION,
        "id": requested_id[:120],
        "label": str(value.get("label") or "Approved role-family control")[:180],
        "role_family": str(value.get("role_family") or "all")[:80],
        "source": "run",
        "entry_id": entry_id,
        "run_id": entry_id,
        "artifact": "approved tailored PDF",
        "available": True,
        "approved": True,
        "selected": True,
        "resolution": "approved_role_control",
        "reference_only": True,
        "_baseline_tex": baseline_tex,
    }


def comparison_control_diff(
    control: Any, keyword_strategy: Any, candidate_tex: str,
) -> Dict[str, Any]:
    """Compare the final candidate with the selected role-family reference.

    This is deliberately secondary to the existing immutable-control audit.
    It surfaces supported term and signal-family gains/losses so Victor can
    see why a prior control influenced the decision without letting historical
    wording override the current evidence graph or hard gates.
    """
    value = control if isinstance(control, dict) else immutable_comparison_control()
    baseline_tex = str(value.get("_baseline_tex") or "")
    if not baseline_tex and str(value.get("id") or "") == IMMUTABLE_COMPARISON_CONTROL_ID:
        try:
            baseline_tex = (cv_root(repo_root()) / CANONICAL_TEMPLATE).read_text(errors="replace")
        except OSError:
            baseline_tex = ""
    keyword_strategy = keyword_strategy if isinstance(keyword_strategy, dict) else {}
    baseline_text = _latex_plain(baseline_tex)
    candidate_text = _latex_plain(candidate_tex)
    terms = [
        str(item.get("term") or "")
        for item in keyword_strategy.get("terms") or []
        if isinstance(item, dict) and item.get("supported") and str(item.get("term") or "")
    ]
    baseline_terms = sorted({term for term in terms if _keyword_present(term, baseline_text)})
    candidate_terms = sorted({term for term in terms if _keyword_present(term, candidate_text)})
    lost_terms = sorted(set(baseline_terms) - set(candidate_terms))
    gained_terms = sorted(set(candidate_terms) - set(baseline_terms))
    baseline_families = sorted(_portfolio_signal_families(baseline_text))
    candidate_families = sorted(_portfolio_signal_families(candidate_text))
    lost_families = sorted(set(baseline_families) - set(candidate_families))
    return {
        **comparison_control_summary(value),
        "scope": "secondary_reference" if value.get("reference_only") else "immutable_primary",
        "supported_term_count": len(terms),
        "baseline_covered_count": len(baseline_terms),
        "candidate_covered_count": len(candidate_terms),
        "baseline_coverage_percent": round(100 * len(baseline_terms) / max(1, len(terms))),
        "candidate_coverage_percent": round(100 * len(candidate_terms) / max(1, len(terms))),
        "gained_terms": gained_terms[:30],
        "lost_terms": lost_terms[:30],
        "baseline_signal_families": baseline_families[:20],
        "candidate_signal_families": candidate_families[:20],
        "lost_signal_families": lost_families[:20],
        "warning": (
            "Candidate loses supported role terms or technical signal families present in the selected control; review the diff."
            if lost_terms or lost_families else
            "Candidate does not lose a supported term or signal family from the selected control."
        ),
    }


def portfolio_diagnostics(
    plan: Dict[str, Any], catalog: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare the whole selected portfolio, not just isolated bullets.

    This is a review instrument rather than a composite score. It exposes
    breadth, overlap, and stronger unused alternatives so a writer or
    role-separated critic panel can catch a technically polished but strategically
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
        decision_ledger = [
            item for item in (plan.get("decision_ledger") or [])
            if isinstance(item, dict)
        ]
        unexplained = [
            entry_id for entry_id in canonical_project_ids
            if entry_id not in set(selected_project_ids)
            and not _ledger_explains_removed_evidence(
                {"entry_id": entry_id, "source_id": ""},
                decision_ledger, entries,
            )
        ]
        if unexplained:
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


def _stable_digest(value: Any, length: int = 24) -> str:
    """Return a deterministic content identity for local audit artifacts."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
    else:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[: max(8, int(length or 24))]


def review_panel_available(review: Any) -> bool:
    """Return whether a usable critique panel completed.

    New runs use a Codex Luna multi-role jury. The legacy boolean/dict field
    is still understood so saved pre-jury reports remain readable, but no new
    run depends on a second vendor.
    """
    if not isinstance(review, dict):
        return False
    jury = review.get("critic_jury")
    if isinstance(jury, dict) and jury.get("available") is True:
        return True
    legacy = review.get("independent_review")
    if isinstance(legacy, dict):
        return legacy.get("available") is True
    return legacy is True


def review_panel_mode(review: Any) -> str:
    if not isinstance(review, dict):
        return "unavailable"
    jury = review.get("critic_jury")
    if isinstance(jury, dict) and jury.get("mode"):
        return str(jury.get("mode"))
    if review.get("review_mode"):
        return str(review.get("review_mode"))
    if review_panel_available(review):
        return "independent_provider"
    return "unavailable"


ROLE_TRACK_TERMS = {
    "systems_performance": {
        "systems", "performance", "hpc", "cuda", "distributed", "parallel",
        "compiler", "inference", "optimization", "infrastructure", "slurm",
        "network", "networking", "latency", "throughput", "linux", "gpu",
        "profiling", "scalability", "architecture",
    },
    "backend_infrastructure": {
        "backend", "api", "service", "services", "cloud", "docker",
        "kubernetes", "database", "databases", "platform", "deployment",
        "microservice", "microservices", "rest", "integration", "reliability",
    },
    "ml_research": {
        "machine", "learning", "deep", "model", "models", "research",
        "experiments", "pytorch", "tensorflow", "vision", "nlp", "publication",
    },
    "data_platform": {
        "data", "analytics", "analysis", "sql", "pipeline", "pipelines",
        "warehouse", "statistics", "visualization", "etl", "feature",
    },
    "product_software": {
        "software", "development", "frontend", "fullstack", "full-stack", "web",
        "ui", "ux", "testing", "agile", "angular", "react", "javascript",
        "typescript", "requirements", "documentation", "git",
    },
}

ROLE_TRACK_LABELS = {
    "systems_performance": "systems / performance / networking",
    "backend_infrastructure": "backend / infrastructure / APIs",
    "ml_research": "ML / research",
    "data_platform": "data / analytics / platform",
    "product_software": "product software / web development",
    "general_software": "general software engineering",
}

# Multi-word signals carry more meaning than isolated common words such as
# software or data. The profile is intentionally small and deterministic: it
# is a role-focus receipt, not a second opaque classifier.
ROLE_TRACK_PHRASES = {
    "systems_performance": (
        "high performance", "high-performance", "distributed systems",
        "parallel computing", "computer architecture", "network engineering",
        "network performance", "low latency", "high throughput", "gpu computing",
        "performance engineering", "systems programming", "systems software",
    ),
    "backend_infrastructure": (
        "backend systems", "backend services", "rest api", "restful api",
        "web services", "microservices", "cloud infrastructure", "data platform",
        "software integration", "api integration", "platform engineering",
    ),
    "ml_research": (
        "machine learning", "deep learning", "large language model",
        "computer vision", "natural language processing", "model development",
        "research engineer", "research engineering",
    ),
    "data_platform": (
        "data engineering", "data pipeline", "data pipelines", "data platform",
        "data warehouse", "data quality", "business intelligence",
    ),
    "product_software": (
        "software engineering", "software development", "web application",
        "user interface", "front end", "front-end", "full stack", "full-stack",
        "unit testing", "test-driven", "requirements gathering",
    ),
}

# These are deliberately mechanism-level terms rather than a copy of the ATS
# inventory.  The role-evidence floor uses them to protect one omitted project
# with a materially stronger role surface (for example, React/multi-user web
# software or C++ streaming) from being crowded out by an adjacent keyword
# project.  It never creates wording or treats a term in Skills as equivalent
# to an implemented artifact.
ROLE_FLOOR_TERMS = {
    "systems_performance": (
        "c++", "real-time", "streaming", "gpu", "hpc", "slurm", "latency",
        "throughput", "performance", "parallel", "systems", "multithread",
    ),
    "backend_infrastructure": (
        "fastapi", "rest", "api", "backend", "service", "database", "cloud",
        "deployment", "reliability", "integration", "sql",
    ),
    "ml_research": (
        "machine learning", "deep learning", "pytorch", "computer vision",
        "model", "validation", "research", "rnn", "llm", "training",
    ),
    "data_platform": (
        "data pipeline", "sql", "etl", "statistics", "visualization",
        "analytics", "data quality", "feature",
    ),
    "product_software": (
        "react", "javascript", "typescript", "web", "frontend", "front end",
        "full stack", "multi-user", "real-time collaboration", "access control",
        "document", "testing", "software engineering",
    ),
}
ROLE_EVIDENCE_FLOOR_VERSION = "role-evidence-floor-v1"

# Company research is a routing signal, not a second source of resume facts.
# These small domain families make the strategy auditable and keep a medical
# employer from being treated as interchangeable with a generic software
# company when the same posting could be supported by different projects.
COMPANY_DOMAIN_PATTERNS = {
    "healthcare": (
        "health", "healthcare", "medical", "medicine", "clinical", "patient",
        "pharma", "pharmaceutical", "biotech", "biomedical", "drug safety",
        "life sciences", "medtech", "healthtech", "health tech", "genetic", "genomics", "diagnostic",
    ),
    "financial_services": (
        "financial", "finance", "bank", "banking", "insurance", "payments",
        "investing", "investment", "trading", "fintech", "credit",
    ),
    "education": (
        "education", "learning", "university", "college", "student", "school",
    ),
    "developer_platform": (
        "developer", "software platform", "cloud platform", "infrastructure",
        "api platform", "devtools", "developer tools",
    ),
    "consumer": (
        "consumer", "social", "marketplace", "retail", "e commerce", "e-commerce",
    ),
}

COMPANY_DOMAIN_PROJECT_TERMS = {
    "healthcare": (
        "health", "medical", "clinical", "patient", "drug", "safety", "biomedical",
        "pharma", "genetic", "genomics", "wearable", "posture", "cognitive", "sensor",
    ),
    "financial_services": (
        "finance", "financial", "trading", "stock", "portfolio", "payment", "risk",
    ),
    "education": (
        "education", "student", "learning", "course", "school", "university",
    ),
    "developer_platform": (
        "api", "backend", "cloud", "platform", "developer", "infrastructure", "service",
    ),
    "consumer": (
        "user", "consumer", "mobile", "web", "marketplace", "recommendation",
    ),
}

COMPANY_CONTEXT_FIELDS = (
    "industry", "summary", "products", "customers", "mission", "technical_work",
    "why_it_matters", "interview_focus",
)


def _role_signal_present(text: str, signal: str) -> bool:
    """Match a role signal as a phrase/word, not a substring of another word."""
    escaped = re.escape(str(signal or "").strip()).replace(r"\ ", r"\s+")
    return bool(escaped and re.search(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", text, re.I))


def role_track_profile(job: Dict[str, Any], posting_text: str) -> Dict[str, Any]:
    """Return an auditable role-focus ranking for a posting.

    Title signals count more than body mentions, phrase signals count more
    than generic single words, and repeated body boilerplate does not inflate
    a track without bound. This keeps broad postings from making every
    sentence equally important while preserving ambiguity when tracks are
    genuinely close.
    """
    title = clean_text(str(job.get("title") or "")).lower()
    body = clean_text(str(posting_text or "")).lower()
    scores: Dict[str, float] = {}
    details: Dict[str, Dict[str, Any]] = {}
    for track, terms in ROLE_TRACK_TERMS.items():
        matched_phrases = [
            phrase for phrase in ROLE_TRACK_PHRASES.get(track, ())
            if _role_signal_present(title + " " + body, phrase)
        ]
        title_terms = [term for term in terms if _role_signal_present(title, term)]
        body_terms = [term for term in terms if _role_signal_present(body, term)]
        # A title hit is a strong routing signal. Body phrase hits are useful,
        # but cap their total so boilerplate cannot dominate the title.
        score = (len(title_terms) * 4.0) + (len(matched_phrases) * 3.0)
        score += min(12.0, len(set(body_terms) - set(title_terms)) * 1.25)
        if score:
            scores[track] = round(score, 2)
            details[track] = {
                "title_terms": sorted(title_terms),
                "phrases": sorted(matched_phrases),
                "body_terms": sorted(body_terms),
            }
    ranked = sorted(scores, key=lambda track: (-scores[track], track))
    if not ranked:
        return {
            "primary_track": "general_software",
            "primary_label": ROLE_TRACK_LABELS["general_software"],
            "confidence": "low",
            "secondary_tracks": [],
            "scores": [],
            "signals": {},
            "rule": "No role-family signal was strong enough; use general software weighting.",
        }
    primary = ranked[0]
    primary_score = scores[primary]
    secondary = [
        track for track in ranked[1:4]
        # Keep real secondary surfaces visible even when the title makes the
        # primary track dominant. Confidence uses the margin separately; a
        # low-scoring adjacent track must not silently become co-primary.
        if scores[track] >= 2.5
    ]
    runner_up = scores[secondary[0]] if secondary else 0.0
    if primary_score >= 10.0 and runner_up < primary_score * 0.72:
        confidence = "high"
    elif secondary and runner_up >= primary_score * 0.82:
        confidence = "ambiguous"
    else:
        confidence = "moderate"
    return {
        "primary_track": primary,
        "primary_label": ROLE_TRACK_LABELS.get(primary, primary),
        "confidence": confidence,
        "secondary_tracks": secondary,
        "scores": [
            {
                "track": track,
                "label": ROLE_TRACK_LABELS.get(track, track),
                "score": scores[track],
                "signals": details.get(track, {}),
            }
            for track in ranked[:4]
        ],
        "signals": details,
        "rule": (
            "Primary track is weighted first; secondary tracks remain eligible for supported evidence, "
            "but an adjacent keyword cannot displace a stronger primary-track proof point without an explicit tradeoff."
        ),
    }


def build_job_intelligence(
    job: Dict[str, Any], posting_text: str,
    match: Optional[Dict[str, Any]] = None,
    target_keywords: Optional[Dict[str, Any]] = None,
    generation_strategy: Optional[Dict[str, Any]] = None,
    tailoring_brief: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a compact, source-grounded requirement model for one posting.

    This is deliberately deterministic in v1.  Provider gap analysis can add
    richer requirement prose, but this artifact remains anchored to captured
    posting terms, authorized evidence IDs, and explicit eligibility rules.
    """
    text = clean_text(str(posting_text or ""))
    target_keywords = target_keywords if isinstance(target_keywords, dict) else {}
    generation_strategy = generation_strategy if isinstance(generation_strategy, dict) else {}
    tailoring_brief = tailoring_brief if isinstance(tailoring_brief, dict) else {}
    requirements: List[Dict[str, Any]] = []
    seen_keys = set()

    def add_requirement(
        requirement: str, importance: str, exact_terms: Iterable[str],
        evidence_status: str = "unsupported", evidence_ids: Optional[Iterable[str]] = None,
        hard_block: bool = False, source: str = "posting",
        recommended_action: str = "leave_gap", reason: str = "",
    ) -> None:
        clean_requirement = clean_text(str(requirement or ""))[:500]
        terms = list(dict.fromkeys(clean_text(str(term or ""))[:160] for term in exact_terms if str(term or "").strip()))
        key = (clean_requirement.lower(), tuple(term.lower() for term in terms))
        if not clean_requirement or key in seen_keys:
            return
        seen_keys.add(key)
        requirements.append({
            "id": "requirement:" + _stable_digest({"requirement": clean_requirement, "terms": terms}, 12),
            "requirement": clean_requirement,
            "importance": importance if importance in {"required", "preferred", "responsibility", "mentioned", "eligibility"} else "mentioned",
            "exact_terms": terms[:8],
            "evidence_status": evidence_status if evidence_status in {"direct", "adjacent", "unsupported", "unknown"} else "unknown",
            "evidence_ids": list(dict.fromkeys(str(value) for value in (evidence_ids or []) if str(value)))[:8],
            "hard_block": bool(hard_block),
            "source": source,
            "recommended_action": recommended_action,
            "reason": str(reason or "")[:500],
        })

    for item in target_keywords.get("terms") or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if not term:
            continue
        support_kind = str(item.get("support_kind") or "none")
        evidence_status = "direct" if item.get("supported") and support_kind != "adjacent" else "adjacent" if item.get("supported") else "unsupported"
        add_requirement(
            term,
            str(item.get("importance") or "mentioned"),
            [term],
            evidence_status=evidence_status,
            evidence_ids=item.get("source_ids") or [],
            source="deterministic_term_inventory",
            recommended_action="keep" if item.get("supported") else "leave_gap",
            reason=(
                "Authorized evidence can support this term."
                if item.get("supported") else
                "No authorized evidence currently supports this exact term."
            ),
        )

    for item in generation_strategy.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        add_requirement(
            str(item.get("requirement") or ""),
            str(item.get("importance") or "mentioned"),
            item.get("exact_terms") or [],
            evidence_status=str(item.get("evidence_status") or "unknown"),
            evidence_ids=item.get("evidence_ids") or [],
            source="normalized_gap_analysis",
            recommended_action=str(item.get("recommended_action") or "leave_gap"),
            reason=str(item.get("reason") or ""),
        )

    eligibility_blocks = posting_eligibility_blocks(text) if text else []
    for block in eligibility_blocks:
        add_requirement(
            block, "eligibility", [], evidence_status="unknown", hard_block=True,
            source="deterministic_eligibility", recommended_action="leave_gap",
            reason="This application-level constraint is evaluated outside resume wording.",
        )

    role_focus = role_track_profile(job, text)
    tracks = [
        str(item.get("track") or "")
        for item in role_focus.get("scores") or []
        if str(item.get("track") or "")
    ]
    if not tracks:
        tracks = ["general_software"]
    primary_track = str(role_focus.get("primary_track") or tracks[0])
    secondary_tracks = set(
        str(value) for value in role_focus.get("secondary_tracks") or []
        if str(value)
    )
    # Attach role affinity to each requirement so the writer and evaluator can
    # distinguish a primary-track loss from an adjacent keyword omission.
    for requirement in requirements:
        requirement_text = " ".join((
            str(requirement.get("requirement") or ""),
            " ".join(str(value) for value in requirement.get("exact_terms") or []),
        ))
        requirement_focus = role_track_profile({}, requirement_text)
        requirement_tracks = [
            str(item.get("track") or "")
            for item in requirement_focus.get("scores") or []
            if str(item.get("track") or "")
        ]
        requirement["role_tracks"] = requirement_tracks[:3]
        if primary_track in requirement_tracks:
            requirement["role_relevance"] = "primary"
            requirement["role_priority"] = 1.0
        elif secondary_tracks.intersection(requirement_tracks):
            requirement["role_relevance"] = "secondary"
            requirement["role_priority"] = 0.8
        else:
            requirement["role_relevance"] = "general"
            requirement["role_priority"] = 0.6

    match = match if isinstance(match, dict) else {}
    match_score = match.get("score")
    fit_band = (
        "strong" if isinstance(match_score, (int, float)) and match_score >= 75 else
        "moderate" if isinstance(match_score, (int, float)) and match_score >= 50 else
        "stretch" if isinstance(match_score, (int, float)) else "unknown"
    )
    artifact = {
        "version": JOB_INTELLIGENCE_VERSION,
        "posting_available": len(text) >= 300,
        "posting_chars": len(text),
        "posting_snapshot_hash": _stable_digest(text),
        "role_tracks": tracks[:4],
        "primary_role_track": primary_track,
        "secondary_role_tracks": sorted(secondary_tracks),
        "track_confidence": str(role_focus.get("confidence") or "low"),
        "role_focus": role_focus,
        "requirements": requirements[:80],
        "hard_blockers": eligibility_blocks,
        "tailoring_brief": copy.deepcopy(tailoring_brief),
        "company_context": copy.deepcopy(tailoring_brief.get("company_context") or {}),
        "fit": {
            "band": fit_band,
            "score": match_score if isinstance(match_score, (int, float)) else None,
            "confidence": str(match.get("confidence") or "low"),
            "missing_requirements": list(match.get("missing_requirements") or [])[:20],
        },
    }
    artifact["hash"] = _stable_digest(artifact)
    return artifact


def build_change_findings(
    changes: Dict[str, Any], deterministic: Optional[Dict[str, Any]] = None,
    review: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Classify material base-to-tailored changes without asking a provider."""
    changes = changes if isinstance(changes, dict) else {}
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    review = review if isinstance(review, dict) else {}
    findings: List[Dict[str, Any]] = []

    def add(
        classification: str, severity: str, reason: str,
        source_ids: Optional[Iterable[str]] = None,
        requirement_ids: Optional[Iterable[str]] = None,
        action: str = "",
        critic_consensus: Optional[Dict[str, Any]] = None,
    ) -> None:
        if classification not in {"KEEP_GOOD", "QUESTIONABLE", "REGRESSION", "MISSED_OPPORTUNITY", "BLOCKER"}:
            classification = "QUESTIONABLE"
        item = {
            "id": "finding:" + _stable_digest({
                "classification": classification, "reason": reason,
                "source_ids": list(source_ids or []), "requirement_ids": list(requirement_ids or []),
                "critic_consensus": critic_consensus or {},
            }, 14),
            "classification": classification,
            "severity": severity if severity in {"info", "warning", "critical"} else "warning",
            "reason": str(reason or "")[:700],
            "source_ids": list(dict.fromkeys(str(value) for value in (source_ids or []) if str(value)))[:8],
            "requirement_ids": list(dict.fromkeys(str(value) for value in (requirement_ids or []) if str(value)))[:8],
            "action": str(action or "")[:500],
        }
        if isinstance(critic_consensus, dict) and critic_consensus:
            item["critic_consensus"] = {
                "supporting_roles": list(dict.fromkeys(
                    str(value) for value in critic_consensus.get("supporting_roles") or [] if str(value)
                ))[:8],
                "support_count": int(critic_consensus.get("support_count") or 0),
                "agreement": str(critic_consensus.get("agreement") or "unknown"),
                "variants": [str(value)[:500] for value in critic_consensus.get("variants") or []][:4],
            }
        if item["id"] not in {existing["id"] for existing in findings}:
            findings.append(item)

    keyword_terms = {
        str(item.get("term") or "").lower(): item
        for item in (changes.get("keyword_coverage") or {}).get("terms") or []
        if isinstance(item, dict) and str(item.get("term") or "")
    }
    for item in changes.get("rewritten_bullets") or []:
        source_ids = item.get("source_ids") or [item.get("source_id")]
        missing = _missing_protected_qualifiers(
            str(item.get("source_text") or ""), str(item.get("final_text") or "")
        )
        if missing:
            add(
                "BLOCKER", "critical",
                "Rewrite dropped protected scope qualifier(s): %s." % ", ".join(missing),
                source_ids, action="Restore the authorized qualifier before review.",
            )
        elif any(
            _tailoring_term_weight(keyword_terms.get(str(term).lower(), {})) >= 3
            for term in item.get("dropped_supported_terms") or []
        ):
            dropped = ", ".join(str(term) for term in item.get("dropped_supported_terms") or [])
            add(
                "REGRESSION", "warning",
                "A rewrite dropped higher-priority supported target terminology: %s." % dropped,
                source_ids, action="Restore the supported term or explain why stronger evidence replaced it.",
            )
        elif item.get("added_supported_terms"):
            add(
                "KEEP_GOOD", "info",
                "Evidence-preserving rewrite adds a supported role signal%s." % (
                    ": " + ", ".join(str(term) for term in item.get("added_supported_terms") or [])
                    if item.get("added_supported_terms") else ""
                ),
                source_ids, action="Retain the rewrite if it remains readable and interview-defensible.",
            )
        else:
            add(
                "QUESTIONABLE", "info",
                "Source-addressed wording change preserved authorized evidence, but its role value still needs critic-panel comparison.",
                source_ids, action="Keep only if the paired reviewer confirms better role relevance or clearer proof.",
            )

    # Suppressed near-copy rewrites are a successful safety action, not a
    # negative finding.  They remain inspectable in content_changes.

    # An added source line is not automatically a gain: it may be filler or
    # duplicate the base story. Count it positively only when the sealed panel
    # independently named a matching gained strength. This fixes the old
    # blind spot where every new evidence line was invisible to the uplift
    # audit, without turning source selection into keyword scoring.
    comparison = review.get("portfolio_comparison") or {}
    gained_strengths = [
        str(value).strip()
        for value in comparison.get("gained_strengths") or []
        if str(value).strip()
    ]
    gain_stop = {
        "added", "better", "candidate", "clear", "concrete", "direct", "evidence",
        "explicit", "gained", "new", "relevant", "resume", "role", "signal",
        "strong", "target", "technical", "the", "this", "visible", "work",
    }
    high_signal = {
        "access", "algorithm", "api", "async", "authentication", "cache", "dashboard",
        "document", "encrypted", "endpoint", "fallback", "flask", "hpc", "jwt",
        "metric", "pipeline", "prototype", "quantum", "react", "recall", "rest",
        "row", "security", "sqlite", "validation", "visualization",
    }

    def strength_tokens(value: Any) -> set:
        tokens = set()
        for token in _resume_tokens(_latex_plain(str(value or ""))):
            normalized = token.rstrip("s")
            if len(normalized) >= 4 and normalized not in gain_stop:
                tokens.add(normalized)
        return tokens

    normalized_gains = [(strength, strength_tokens(strength)) for strength in gained_strengths]
    used_gained_strengths = set()
    for added in changes.get("added_bullets") or []:
        source_id = str(added.get("source_id") or "")
        bullet_tokens = strength_tokens(added.get("text"))
        matches = []
        for strength, gain_tokens in normalized_gains:
            overlap = bullet_tokens & gain_tokens
            high_overlap = overlap & high_signal
            if (len(overlap) >= 2 or high_overlap) and strength.casefold() not in used_gained_strengths:
                matches.append(strength)
        if matches:
            used_gained_strengths.update(value.casefold() for value in matches)
            add(
                "KEEP_GOOD", "warning",
                "Sealed critic panel confirmed a target-relevant evidence gain: %s."
                % "; ".join(matches[:2]),
                [source_id],
                action="Retain this authorized line unless a later complete panel identifies a material regression.",
            )

    coverage = changes.get("keyword_coverage") or {}
    for item in coverage.get("terms") or []:
        term = str(item.get("term") or "")
        comparison = str(item.get("comparison_status") or "unknown")
        required = bool(item.get("required"))
        source_ids = item.get("source_ids") or []
        if item.get("status") == "unverified_rendered":
            add(
                "BLOCKER", "critical",
                "Unsupported posting term rendered without authorized evidence: %s." % term,
                source_ids, action="Remove the term or confirm supporting evidence before use.",
            )
        elif comparison == "gained":
            add(
                "KEEP_GOOD", "info",
                "Supported posting terminology was surfaced in the tailored resume: %s." % term,
                source_ids, action="Retain if the surrounding claim remains specific and defensible.",
            )
        elif comparison == "lost":
            weight = _tailoring_term_weight(item)
            classification = "REGRESSION" if weight >= 3 else "QUESTIONABLE"
            severity = "critical" if required else "warning"
            reason = (
                "The tailored resume dropped a higher-priority supported term from the base resume: %s."
                if classification == "REGRESSION" else
                "A lower-priority supported context term was omitted from the tailored resume: %s."
            ) % term
            add(
                classification, severity, reason,
                source_ids, action=(
                    "Restore it or record why stronger evidence displaced it."
                    if classification == "REGRESSION" else
                    "Restore it only if the role benefits more from this context than the evidence already selected."
                ),
            )
        elif item.get("status") == "missing" and item.get("supported"):
            add(
                "MISSED_OPPORTUNITY", "critical" if required else "warning",
                "Authorized evidence supports this posting term, but the tailored resume does not surface it: %s." % term,
                source_ids, action="Consider surfacing the term where the underlying evidence is visible.",
            )

    for item in (changes.get("unexplained_removed_bullets") or changes.get("removed_canonical_bullets") or [])[:20]:
        if item.get("tradeoff_status") == "explained":
            continue
        text = str(item.get("text") or "")
        if _resume_numeric_anchors(text) or _portfolio_signal_families(text):
            add(
                "MISSED_OPPORTUNITY", "warning",
                "A canonical evidence bullet with concrete proof or a technical signal was removed from the tailored portfolio.",
                [item.get("source_id")], action="Confirm the lost signal is intentionally replaced by stronger evidence.",
            )

    diagnostics = changes.get("portfolio_diagnostics") or {}
    for warning in (diagnostics.get("warnings") or [])[:12]:
        add(
            "REGRESSION" if warning in (diagnostics.get("blocking_warnings") or []) else "QUESTIONABLE",
            "critical" if warning in (diagnostics.get("blocking_warnings") or []) else "warning",
            "Portfolio audit warning: %s" % warning,
            action=(
                "Resolve the redundancy or explain the deliberate tradeoff."
                if warning in (diagnostics.get("blocking_warnings") or []) else
                "Compare the flagged alternative; this is advisory until a stronger replacement is demonstrated."
            ),
        )

    gates = deterministic.get("gates") or {}
    for name, gate in gates.items():
        if not isinstance(gate, dict):
            continue
        status = str(gate.get("status") or "").lower()
        if status == "fail":
            add(
                "BLOCKER", "critical",
                "%s gate failed: %s" % (str(name).replace("_", " ").title(), str(gate.get("reason") or "unspecified")),
                action="Resolve this gate before treating the resume as ready.",
            )
        elif status == "partial":
            add(
                "QUESTIONABLE", "warning",
                "%s gate is unresolved: %s" % (str(name).replace("_", " ").title(), str(gate.get("reason") or "unspecified")),
                action="Confirm the missing condition during owner review.",
            )

    unsupported, _ignored_unsupported = actionable_unsupported_claims(
        review.get("unsupported_claims") or []
    )
    for claim in unsupported[:20]:
        add(
            "BLOCKER", "critical",
            "Critic panel reported an unsupported claim: %s" % str(claim),
            action="Remove or ground the claim in authorized evidence.",
        )
    blocking_issues = review.get("blocking_issues") or []
    panel_available = review_panel_available(review)
    if panel_available and not isinstance(blocking_issues, list):
        blocking_issues = [blocking_issues]
    if panel_available:
        assessments = review.get("blocking_issue_assessments")
        if not isinstance(assessments, list):
            assessments = []
            for issue in blocking_issues[:20]:
                kind = classify_critic_issue(issue)
                classification, severity = critic_issue_finding(issue, kind)
                assessments.append({
                    "issue": str(issue), "kind": kind,
                    "classification": classification, "severity": severity,
                    "supporting_roles": [], "support_count": 0,
                    "agreement": "unknown", "variants": [],
                })
        for assessment in assessments[:20]:
            issue = str(assessment.get("issue") or "").strip()
            if not issue:
                continue
            classification, severity = critic_issue_finding(
                issue, str(assessment.get("kind") or ""),
            )
            support_count = int(assessment.get("support_count") or 0)
            # A single role's concern is valuable repair material, but it is
            # not enough evidence to call the candidate demonstrably worse.
            # Keep hard safety failures fail-closed; downgrade only
            # non-hard tailoring claims until another role or deterministic
            # diff confirms the loss.  This prevents correlated/unstable
            # prose from turning a mixed tradeoff into a false regression.
            if (
                classification == "REGRESSION"
                and support_count == 1
                and str(assessment.get("kind") or "") != "hard_blocker"
            ):
                classification = "QUESTIONABLE"
                severity = "warning"
                issue_reason = "Single-critic concern requiring confirmation: %s" % issue
            else:
                issue_reason = "Critic panel reported a blocking issue: %s" % issue
            add(
                classification, severity,
                issue_reason,
                action=(
                    "Resolve this safety or layout failure before treating the resume as ready."
                    if classification == "BLOCKER" else
                    "Repair or explicitly preserve the stronger evidence before shipping this tailored version."
                    if classification == "REGRESSION" else
                    "Record this as a candidate-role gap; do not insert unsupported terminology."
                    if str(assessment.get("kind") or "") == "candidate_fit_gap" else
                    "Confirm the concern during owner review."
                ),
                critic_consensus=assessment,
            )
    return findings[:MAX_AUDIT_FINDINGS]


def build_tailoring_audit(
    job: Dict[str, Any], context: Dict[str, Any], match: Dict[str, Any],
    graph: Dict[str, Any], plan: Dict[str, Any], changes: Dict[str, Any],
    deterministic: Dict[str, Any], review: Dict[str, Any],
    base_tex: str, tailored_tex: str, run_id: str = "", queue_id: str = "",
    comparison_control: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the comparative, hard-gated decision artifact for one run."""
    findings = build_change_findings(changes, deterministic, review)
    candidate_delta = candidate_delta_summary(changes)
    if not candidate_delta["material"]:
        # Critic panels often describe the candidate's missing UI, testing, or
        # documentation evidence even when the candidate is byte-for-byte
        # equivalent in substance to the base. Those are useful base-resume
        # diagnostics, but calling them tailoring regressions is misleading
        # and can trigger an expensive repair of an artifact that did not
        # change. Keep the findings visible while removing the false causal
        # attribution.
        for finding in findings:
            if finding.get("classification") == "REGRESSION":
                finding["classification"] = "QUESTIONABLE"
                finding["severity"] = "warning"
                finding["reason"] = (
                    "Baseline diagnostic; the candidate had no material delta from the canonical control. "
                    + str(finding.get("reason") or "")
                )[:700]
                finding["action"] = (
                    "Treat this as a base-resume gap, not a tailoring regression; only change it with authorized evidence."
                )
    deterministic_gates = deterministic.get("gates") if isinstance(deterministic.get("gates"), dict) else {}
    review_gates = review.get("gates") if isinstance(review.get("gates"), dict) else {}
    gates = review_gates or deterministic_gates
    review_available = review_panel_available(review)
    review_mode = review_panel_mode(review)

    def effective_gate(name: str) -> Dict[str, Any]:
        deterministic_gate = deterministic_gates.get(name)
        review_gate = gates.get(name)
        if isinstance(deterministic_gate, dict) and str(deterministic_gate.get("status") or "").lower() == "fail":
            return deterministic_gate
        if review_available and isinstance(review_gate, dict) and str(review_gate.get("status") or "").lower() == "fail":
            return review_gate
        if isinstance(deterministic_gate, dict):
            return deterministic_gate
        if not review_available and name in REVIEW_CRITERIA:
            return {"status": "unknown", "reason": "Codex Luna critic panel unavailable"}
        return review_gate if isinstance(review_gate, dict) else {}

    hard_gate_names = ("factual", "eligibility", "layout", "privacy")
    hard_failures = [
        {"name": name, "reason": str(effective_gate(name).get("reason") or "")}
        for name in hard_gate_names
        if str(effective_gate(name).get("status") or "").lower() == "fail"
    ]
    finding_blockers = [item for item in findings if item.get("classification") == "BLOCKER"]
    # ``independent_review`` is a legacy compatibility alias.  A same-model
    # Luna jury reports it as partial to make vendor independence explicit,
    # but that alias must not downgrade the actual jury's readiness.
    gate_names = (set(gates) | set(deterministic_gates)) - {"independent_review"}
    hard_gates = [
        {"name": str(name), "status": str(effective_gate(name).get("status") or "unknown"),
         "reason": str(effective_gate(name).get("reason") or "")[:500]}
        for name in sorted(gate_names)
        if isinstance(effective_gate(name), dict)
    ]
    intelligence = context.get("job_intelligence") if isinstance(context.get("job_intelligence"), dict) else {}
    posting_available = bool(intelligence.get("posting_available") or len(str(context.get("posting_text") or "")) >= 300)
    unknown_gate = any(item.get("status") in {"partial", "unknown"} for item in hard_gates)
    if hard_failures or finding_blockers:
        readiness = "blocked"
    elif not posting_available or not review_available or unknown_gate or review.get("ready") is not True:
        readiness = "review"
    else:
        readiness = "ready"

    counts = {
        classification: sum(1 for item in findings if item.get("classification") == classification)
        for classification in ("KEEP_GOOD", "QUESTIONABLE", "REGRESSION", "MISSED_OPPORTUNITY", "BLOCKER")
    }
    gains = counts["KEEP_GOOD"]
    losses = counts["REGRESSION"] + counts["BLOCKER"]
    missed = counts["MISSED_OPPORTUNITY"]
    gain_weight = sum(
        5 if item.get("severity") == "critical" else 2 if item.get("severity") == "warning" else 1
        for item in findings if item.get("classification") == "KEEP_GOOD"
    )
    loss_weight = sum(
        5 if item.get("severity") == "critical" else 2 if item.get("severity") == "warning" else 1
        for item in findings if item.get("classification") in {"REGRESSION", "BLOCKER"}
    )
    missed_weight = sum(
        5 if item.get("severity") == "critical" else 2 if item.get("severity") == "warning" else 1
        for item in findings if item.get("classification") == "MISSED_OPPORTUNITY"
    )
    if not candidate_delta["material"]:
        tailoring = "unchanged"
    elif losses and losses >= max(1, gains):
        tailoring = "regressed"
    elif gains > losses:
        tailoring = "improved"
    elif counts["QUESTIONABLE"] or missed:
        tailoring = "inconclusive"
    elif gains:
        tailoring = "improved"
    else:
        tailoring = "neutral"

    critical_missed = any(
        item.get("classification") == "MISSED_OPPORTUNITY"
        and item.get("severity") == "critical"
        for item in findings
    )
    if hard_failures or finding_blockers:
        recommended_version = "blocked"
        decision = "do_not_ship"
    elif not review_available:
        # Partial critic findings remain useful diagnostics, but they are not
        # an independent comparative decision.  Never promote a tailored
        # artifact from an incomplete panel; the canonical base is the safe
        # primary until the owner can review or a complete panel returns.
        recommended_version = "review"
        decision = "needs_review"
    elif tailoring in {"regressed", "unchanged"}:
        recommended_version = "base"
        decision = "prefer_base"
    elif tailoring == "improved" and not critical_missed:
        recommended_version = "tailored"
        decision = "prefer_tailored"
    else:
        recommended_version = "review"
        decision = "needs_review"
    uplift_band = (
        "neutral" if not candidate_delta["material"] else
        "negative" if loss_weight > gain_weight else
        "positive" if gain_weight > loss_weight and not critical_missed else
        "uncertain"
    )

    score = match.get("score") if isinstance(match, dict) else None
    fit_band = (
        "strong" if isinstance(score, (int, float)) and score >= 75 else
        "moderate" if isinstance(score, (int, float)) and score >= 50 else
        "stretch" if isinstance(score, (int, float)) else "unknown"
    )
    keyword = changes.get("keyword_coverage") or {}
    required_coverage = keyword.get("required_coverage_percent")
    screening = (
        "fail" if any(item.get("status") == "unverified_rendered" for item in keyword.get("terms") or [])
        else "partial" if isinstance(required_coverage, (int, float)) and required_coverage < 100
        else "pass"
    )
    integrity = "fail" if hard_failures or finding_blockers else "partial" if not review_available or any(item.get("status") in {"partial", "unknown"} for item in hard_gates) else "pass"
    portfolio = changes.get("portfolio_diagnostics") or {}
    efficiency = "fail" if portfolio.get("blocking_warnings") else "partial" if portfolio.get("warnings") else "pass"
    criteria = review_gates
    human_relevance = "unknown" if not review_available else str((criteria.get("target_fit") or {}).get("status") or "unknown")
    technical_conviction = "unknown" if not review_available else str((criteria.get("evidence") or {}).get("status") or "unknown")
    dimensions = {
        "fit": {"status": fit_band, "score": score, "source": "resume_match.score"},
        "screening": {"status": screening, "source": "deterministic keyword and layout audit"},
        "human_relevance": {"status": human_relevance, "source": "Codex Luna critic panel"},
        "technical_conviction": {"status": technical_conviction, "source": "evidence gate and Codex Luna critic panel"},
        "integrity": {"status": integrity, "source": "factual, eligibility, privacy, and provenance gates"},
        "information_efficiency": {"status": efficiency, "source": "portfolio and density audit"},
    }
    confidence = (
        "high" if posting_available and review_available and review_mode == "independent_provider" and not unknown_gate else
        "medium" if posting_available and review_available else
        "medium" if posting_available else "low"
    )
    audit = {
        "version": TAILORING_AUDIT_VERSION,
        "status": "blocked" if readiness == "blocked" else "complete" if readiness == "ready" else "review",
        "readiness": readiness,
        "fit": {
            "band": fit_band,
            "score": score,
            "confidence": str(match.get("confidence") or "low"),
            "missing_requirements": list(match.get("missing_requirements") or [])[:20],
            "hard_blockers": list(intelligence.get("hard_blockers") or []),
        },
        "tailoring": tailoring,
        "candidate_delta": candidate_delta,
        "decision": decision,
        "recommended_version": recommended_version,
        "review": {
            "available": review_available,
            "mode": review_mode,
            "separate_vendor": review_mode == "independent_provider",
            "critic_roles": list(review.get("critic_roles") or [])[:8],
        },
        "confidence": confidence,
        "dimensions": dimensions,
        "hard_gates": hard_gates,
        "hard_failures": hard_failures,
        "findings": findings,
        "tradeoffs": list(changes.get("explained_tradeoffs") or [])[:40],
        "finding_counts": counts,
        "comparison": {
            "preference": tailoring,
            "recommended_version": recommended_version,
            "decision": decision,
            "uplift_band": uplift_band,
            "material_delta": bool(candidate_delta["material"]),
            "delta_reasons": list(candidate_delta.get("reasons") or []),
            "gain_weight": gain_weight,
            "loss_weight": loss_weight,
            "missed_opportunity_weight": missed_weight,
            "base_text_hash": _stable_digest(base_tex),
            "tailored_text_hash": _stable_digest(tailored_tex),
            "gains": gains,
            "losses": losses,
            "missed_opportunities": missed,
        },
        "posting_snapshot_hash": str(intelligence.get("posting_snapshot_hash") or _stable_digest(context.get("posting_text") or "")),
        "evidence_graph_hash": str(graph.get("hash") or ""),
        "job_intelligence_hash": str(intelligence.get("hash") or ""),
        "run_id": str(run_id or ""),
        "queue_id": str(queue_id or ""),
        "comparison_control": comparison_control_diff(
            comparison_control or immutable_comparison_control(),
            context.get("target_keywords"),
            tailored_tex,
        ),
    }
    audit["hash"] = _stable_digest(audit)
    return audit


def tailoring_audit_preference_key(audit: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """Order two candidate drafts without pretending to predict hiring."""
    audit = audit if isinstance(audit, dict) else {}
    recommendation = str(audit.get("recommended_version") or "review")
    rank = {"tailored": 3, "review": 2, "base": 1, "unchanged": 1, "blocked": 0}.get(recommendation, 2)
    comparison = audit.get("comparison") if isinstance(audit.get("comparison"), dict) else {}
    gains = int(comparison.get("gain_weight") or 0)
    losses = int(comparison.get("loss_weight") or 0)
    missed = int(comparison.get("missed_opportunity_weight") or 0)
    questionable = int((audit.get("finding_counts") or {}).get("QUESTIONABLE") or 0)
    return (rank, -losses, -missed, gains, -questionable)


def final_winner_version(audit: Dict[str, Any]) -> str:
    """Return the artifact that should be shown as the run's primary output.

    A generated candidate is not automatically the winner just because it
    compiled.  The comparative audit is the control decision: when it prefers
    the canonical resume, or marks the candidate blocked, the base control is
    the safe primary artifact.  The tailored candidate remains available as a
    private diagnostic so a rejected attempt is never mistaken for the best
    version.
    """
    audit = audit if isinstance(audit, dict) else {}
    recommendation = str(audit.get("recommended_version") or "review").lower()
    # ``review`` is deliberately fail-closed.  It may contain useful partial
    # diagnostics, but it has not earned the right to replace the canonical
    # control artifact as the primary output.
    return "base" if recommendation in {"base", "blocked", "review"} else "tailored"


def adopt_base_control_winner(
    run_dir: Path, audit: Dict[str, Any],
) -> Dict[str, Any]:
    """Make the immutable base PDF the public winner when the audit says so.

    This only copies private artifacts below the ignored run directory.  It
    never edits ``CV/immutable`` and never changes evaluator criteria.  The
    generated candidate is retained under an explicit diagnostic filename so
    the owner can inspect exactly what lost the comparison.
    """
    recommendation = str((audit or {}).get("recommended_version") or "review").lower()
    result: Dict[str, Any] = {
        "winner_version": final_winner_version(audit),
        "primary_artifact": run_pdf_path(run_dir).name,
        "tailored_candidate_artifact": "",
        "reason": "The comparative audit did not prefer the generated candidate.",
    }
    if result["winner_version"] != "base":
        result["reason"] = "The comparative audit did not prefer the canonical base over the generated candidate."
        return result
    canonical_pdf = cv_root(repo_root()) / CANONICAL_PDF
    candidate_pdf = run_pdf_path(run_dir)
    if not canonical_pdf.is_file() or not candidate_pdf.is_file():
        result["winner_version"] = "tailored"
        result["reason"] = "Base control could not be installed because a required PDF artifact was missing."
        return result
    candidate_archive = run_dir / "tailored_candidate.pdf"
    try:
        shutil.copy2(candidate_pdf, candidate_archive)
        candidate_preview = run_preview_path(run_dir)
        if candidate_preview.is_file():
            shutil.copy2(candidate_preview, run_dir / "tailored_candidate-preview.png")
        shutil.copy2(cv_root(repo_root()) / CANONICAL_TEMPLATE, run_dir / "base_control.tex")
        shutil.copy2(canonical_pdf, candidate_pdf)
        rendered_preview = render_preview(run_dir)
    except OSError as exc:
        result["winner_version"] = "tailored"
        result["reason"] = "Base control could not be installed: %s" % exc
        return result
    result.update({
        "primary_artifact": candidate_pdf.name,
        "tailored_candidate_artifact": candidate_archive.name,
        "base_control_artifact": candidate_pdf.name,
        "base_control_source": "CV/" + CANONICAL_PDF,
        "base_control_tex": "base_control.tex",
        "tailored_candidate_preview": (
            "tailored_candidate-preview.png"
            if (run_dir / "tailored_candidate-preview.png").is_file() else ""
        ),
        "primary_preview": run_preview_path(run_dir).name if rendered_preview else "",
        "reason": (
            "The critic panel was incomplete; the canonical base was retained as the safe primary and the "
            "generated candidate was retained as tailored_candidate.pdf for diagnostic comparison."
            if recommendation == "review" else
            "The sealed comparative audit preferred the canonical base; the generated candidate was retained "
            "as tailored_candidate.pdf for diagnostic comparison."
        ),
    })
    return result


def tailoring_repair_feedback(audit: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
    """Turn deterministic audit output into bounded repair instructions."""
    audit = audit if isinstance(audit, dict) else {}
    changes = changes if isinstance(changes, dict) else {}
    findings = [item for item in audit.get("findings") or [] if isinstance(item, dict)]
    actionable = [
        {
            "classification": str(item.get("classification") or "QUESTIONABLE"),
            "severity": str(item.get("severity") or "warning"),
            "reason": str(item.get("reason") or "")[:500],
            "action": str(item.get("action") or "")[:400],
            "source_ids": list(item.get("source_ids") or [])[:8],
        }
        for item in findings
        if item.get("classification") in {"BLOCKER", "REGRESSION", "MISSED_OPPORTUNITY"}
    ][:16]
    coverage = changes.get("keyword_coverage") or {}
    portfolio = changes.get("portfolio_diagnostics") or {}
    comparison_control = {
        "canonical_bullet_count": int(changes.get("canonical_bullet_count") or 0),
        "unexplained_removed_bullets": [
            {
                "source_id": str(item.get("source_id") or ""),
                "entry_id": str(item.get("entry_id") or ""),
                "text": str(item.get("text") or "")[:360],
            }
            for item in (changes.get("unexplained_removed_bullets") or [])[:16]
            if isinstance(item, dict)
        ],
        "lost_supported_terms": [
            str(item.get("term") or "")
            for item in (coverage.get("terms") or [])
            if isinstance(item, dict) and item.get("comparison_status") == "lost" and item.get("supported")
        ][:20],
        "project_swaps": changes.get("project_swaps") or {},
        "portfolio_warnings": list(portfolio.get("warnings") or [])[:12],
        "portfolio_blocking_warnings": list(portfolio.get("blocking_warnings") or [])[:12],
    }
    return {
        "decision": str(audit.get("decision") or "needs_review"),
        "recommended_version": str(audit.get("recommended_version") or "review"),
        "tailoring": str(audit.get("tailoring") or "inconclusive"),
        "comparison": audit.get("comparison") or {},
        "actionable_findings": actionable,
        "comparison_control": comparison_control,
        "explained_tradeoffs": list(changes.get("explained_tradeoffs") or [])[:12],
        "rules": [
            "Repair only with authorized source or evidence IDs already present in the plan/catalog.",
            "Treat canonical evidence as the control: do not remove an unexplained canonical bullet when a safe one-line slot remains.",
            "Stay within the human skim budget: at most 25 bullets, four project entries, 11 project bullets, and five bullets per experience entry.",
            "Restore the highest-value unexplained canonical losses and lost supported terms before adding new project wording.",
            "Do not swap a distinctive mechanism, metric, award, or interview thread for a near-duplicate AI/RAG/backend line.",
            "Do not restore a base line merely because it existed; restore it when it closes a target-relevant loss or prevents avoidable information loss.",
            "Do not remove a deliberate project swap when the decision ledger explains a stronger replacement.",
            "Do not add unsupported job terminology, even if the posting emphasizes it.",
            "Prefer a smaller, coherent portfolio over keyword coverage or page filling.",
            "Preserve metrics, scope qualifiers, and reverse-chronological experience order.",
        ],
    }


def tailoring_repair_prompt(
    context: Dict[str, Any], plan: Dict[str, Any], feedback: Dict[str, Any],
    catalog: Dict[str, Any], graph: Optional[Dict[str, Any]] = None,
    unrestricted: bool = False, generation: bool = False,
) -> str:
    """Ask the writer to repair a comparative audit, not to rewrite blindly."""
    return (
        "You are the repair writer for a private resume tailoring system. A deterministic, source-aware audit "
        "found that the current plan may be weaker than the canonical resume. Produce one complete replacement "
        "plan under the supplied schema. This is not a keyword exercise and you must not invent evidence. "
        "Use only the authorized catalog, graph, and existing plan. Repair the highest-value findings first; "
        "retain an explained tradeoff when its replacement is genuinely stronger. Do not create paraphrase churn. "
        "Every substantive swap, exclusion, rewrite, or restored line must be recorded in decision_ledger with the "
        "expected hiring-value gain and signal lost. Preserve all scope qualifiers, metrics, chronology, and the "
        "canonical formatting contract. If a gap is unsupported, leave it as a gap rather than inserting the term. "
        + ("Use a sharp role-specific argument, but remain evidence-bounded. " if unrestricted else "Keep the repair conservative and evidence-first. ")
        + ("Preserve supported generation-mode gap closure only when it remains source-authorized. " if generation else "")
        + "Stay within the human skim budget: at most 25 rendered bullets, four project headings, 11 project bullets, "
        + "and five bullets per experience entry. Remove portfolio expansion before removing distinctive canonical "
        + "mechanisms or validation. A healthtech prototype or synthetic assessment must retain its research/demo "
        + "boundary; do not let words such as safe, clinical, or validated imply more than the source proves.\n\n"
        + "\n\nTarget context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nCurrent plan:\n"
        + json.dumps(plan, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nComparative audit repair instructions:\n"
        + json.dumps(feedback, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nAuthorized evidence:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nSource catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nCanonical/current benchmark:\n"
        + json.dumps(canonical_resume_benchmark(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nHigh-information canonical control evidence:\n"
        + canonical_control_prompt(catalog, context.get("target_keywords"))
        + "\n\nMethodology:\n"
        + resume_methodology_context(repo_root())[:MAX_METHODOLOGY_CONTEXT_CHARS]
    )


def tailoring_audit_summary(audit: Any) -> Dict[str, Any]:
    """Return a cloud-safe audit summary with no CV/evidence text."""
    if not isinstance(audit, dict):
        return {
            "version": TAILORING_AUDIT_VERSION,
            "available": False,
            "readiness": "unavailable",
            "tailoring": "inconclusive",
            "decision": "needs_review",
            "recommended_version": "review",
            "confidence": "low",
            "run_id": "",
            "queue_id": "",
        }
    findings = [item for item in audit.get("findings") or [] if isinstance(item, dict)]
    important = findings
    fit_value = audit.get("fit")
    fit_band = fit_value.get("band", "unknown") if isinstance(fit_value, dict) else str(fit_value or "unknown")

    def summary_reason(item: Dict[str, Any]) -> str:
        reason = str(item.get("reason") or "")
        if reason.lower().startswith(("critic panel reported", "independent critic reported")):
            return "Codex Luna critic panel reported a review blocker; inspect the private local audit."
        return reason[:220]

    control = audit.get("comparison_control") if isinstance(audit.get("comparison_control"), dict) else immutable_comparison_control()
    control_summary = comparison_control_summary(control)
    for key in (
        "scope", "supported_term_count", "baseline_covered_count", "candidate_covered_count",
        "baseline_coverage_percent", "candidate_coverage_percent", "gained_terms", "lost_terms",
        "baseline_signal_families", "candidate_signal_families", "lost_signal_families", "warning",
    ):
        if key in control:
            control_summary[key] = control.get(key)

    return {
        "version": str(audit.get("version") or TAILORING_AUDIT_VERSION),
        "available": True,
        "status": str(audit.get("status") or "review"),
        "readiness": str(audit.get("readiness") or "review"),
        "fit": fit_band,
        "tailoring": str(audit.get("tailoring") or "inconclusive"),
        "decision": str(audit.get("decision") or "needs_review"),
        "recommended_version": str(audit.get("recommended_version") or "review"),
        "confidence": str(audit.get("confidence") or "low"),
        "review": {
            "available": bool((audit.get("review") or {}).get("available")) if isinstance(audit.get("review"), dict) else False,
            "mode": str((audit.get("review") or {}).get("mode") or "unavailable") if isinstance(audit.get("review"), dict) else "unavailable",
            "separate_vendor": bool((audit.get("review") or {}).get("separate_vendor")) if isinstance(audit.get("review"), dict) else False,
        },
        "run_id": str(audit.get("run_id") or "")[:80],
        "queue_id": str(audit.get("queue_id") or "")[:80],
        "finding_counts": dict(audit.get("finding_counts") or {}),
        "blockers": [summary_reason(item) for item in important if item.get("classification") == "BLOCKER"][:6],
        "gains": [summary_reason(item) for item in important if item.get("classification") == "KEEP_GOOD"][:4],
        "losses": [summary_reason(item) for item in important if item.get("classification") in {"REGRESSION", "MISSED_OPPORTUNITY"}][:6],
        "tradeoffs": list(dict.fromkeys(
            str(item.get("reason") or "")[:220]
            for item in audit.get("tradeoffs") or []
            if isinstance(item, dict) and str(item.get("reason") or "")
        ))[:4],
        "comparison_control": control_summary,
        "hash": str(audit.get("hash") or ""),
    }


def objective_resume_assessment(
    report: Dict[str, Any], status: str = "",
) -> Dict[str, Any]:
    """Build a transparent, target-specific shortlist score for Resume Bank.

    This is intentionally not the Studio's craft score and it does not claim
    to predict a hiring decision.  It is a stable comparison aid for versions
    of the *same posting*: deterministic match, factual-safety, layout, and
    portfolio signals are scored; unavailable signals are omitted and the
    result carries its confidence and provenance.  Independent model critique
    is never inferred from a missing provider result.
    """
    report = report if isinstance(report, dict) else {}
    review = report.get("review") if isinstance(report.get("review"), dict) else {}
    deterministic = review.get("deterministic") if isinstance(review.get("deterministic"), dict) else {}
    deterministic_gates = deterministic.get("gates") if isinstance(deterministic.get("gates"), dict) else {}
    review_gates = review.get("gates") if isinstance(review.get("gates"), dict) else {}
    breakdown: List[Dict[str, Any]] = []

    def add_component(name: str, weight: int, score: Any, source: str, detail: str) -> None:
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return
        bounded = max(0.0, min(100.0, float(score)))
        breakdown.append({
            "name": name,
            "weight": weight,
            "score": round(bounded, 1),
            "source": source,
            "detail": str(detail or "")[:280],
        })

    def gate_score(gate: Any) -> Optional[float]:
        if not isinstance(gate, dict):
            return None
        value = str(gate.get("status") or "").lower()
        return {"pass": 100.0, "partial": 60.0, "fail": 0.0}.get(value)

    match = report.get("resume_match") if isinstance(report.get("resume_match"), dict) else {}
    match_score = match.get("score")
    if isinstance(match_score, (int, float)) and not isinstance(match_score, bool):
        add_component(
            "Target fit", 45, match_score, "resume_match.score",
            "Resume Match is the deterministic posting-to-evidence alignment score.",
        )

    unsupported = review.get("unsupported_claims")
    if not isinstance(unsupported, list):
        unsupported = [unsupported] if unsupported else []
    warnings = report.get("validation_warnings")
    if not isinstance(warnings, list):
        warnings = [warnings] if warnings else []
    factual_gate = deterministic_gates.get("factual")
    factual = gate_score(factual_gate)
    if factual is not None:
        safety = factual - min(65.0, len(unsupported) * 20.0) - min(20.0, len(warnings) * 5.0)
        add_component(
            "Evidence safety", 25, safety, "deterministic factual gate + report warnings",
            "Starts from the deterministic factual gate, then subtracts unsupported claims and validation warnings.",
        )

    layout = deterministic.get("layout") if isinstance(deterministic.get("layout"), dict) else {}
    layout_score = 100.0 if layout.get("pass") is True else 0.0 if layout else gate_score(
        deterministic_gates.get("layout") or review_gates.get("layout")
    )
    if layout_score is not None:
        add_component(
            "Layout safety", 15, layout_score, "deterministic layout audit",
            "Compiled page count, text extraction, overflow, and horizontal packing checks.",
        )

    portfolio = deterministic.get("portfolio") if isinstance(deterministic.get("portfolio"), dict) else {}
    portfolio_score = 100.0 if portfolio.get("pass") is True else 0.0 if portfolio else gate_score(
        deterministic_gates.get("portfolio") or review_gates.get("portfolio")
    )
    portfolio_diagnostics = report.get("portfolio_diagnostics")
    if not isinstance(portfolio_diagnostics, dict):
        portfolio_diagnostics = review.get("portfolio_comparison") if isinstance(review.get("portfolio_comparison"), dict) else {}
    portfolio_warnings = portfolio_diagnostics.get("warnings")
    if not isinstance(portfolio_warnings, list):
        portfolio_warnings = []
    if portfolio_score is not None:
        portfolio_score = max(0.0, portfolio_score - min(40.0, len(portfolio_warnings) * 15.0))
        add_component(
            "Portfolio signal", 15, portfolio_score, "deterministic portfolio audit",
            "Checks compactness and redundant project signal families; warnings are visible below.",
        )

    weighted_total = sum(item["weight"] for item in breakdown)
    score = round(sum(item["score"] * item["weight"] for item in breakdown) / weighted_total, 1) if weighted_total else None
    review_available = review_panel_available(review)
    review_mode = review_panel_mode(review)
    if review_available and review_mode == "independent_provider":
        confidence = "high" if len(breakdown) >= 4 else "medium"
    elif review_available and len(breakdown) >= 3:
        confidence = "medium"
    elif breakdown:
        confidence = "low"
    else:
        confidence = "unranked"

    strengths: List[str] = []
    risks: List[str] = []
    if isinstance(match_score, (int, float)):
        strengths.append("Target fit: Resume Match %s/100." % int(match_score))
    if factual == 100.0 and not unsupported and not warnings:
        strengths.append("No deterministic factual or validation warnings.")
    if layout_score == 100.0:
        strengths.append("One-page layout and overflow checks pass.")
    if portfolio_score == 100.0:
        strengths.append("Portfolio compactness and nonredundancy checks pass.")
    if match.get("missing_requirements"):
        risks.append("Missing target requirements remain: %s." % ", ".join(str(item) for item in match["missing_requirements"][:4]))
    if unsupported:
        risks.append("Unsupported claims were reported (%d)." % len(unsupported))
    if warnings:
        risks.append("Validation warnings remain (%d)." % len(warnings))
    if portfolio_warnings:
        risks.append("Portfolio audit has %d warning%s." % (len(portfolio_warnings), "s" if len(portfolio_warnings) != 1 else ""))
    if not review_available:
        risks.append("No critic-panel result; this is a deterministic shortlist, not a ChatGPT verdict.")
    elif review_mode != "independent_provider":
        risks.append("Codex Luna multi-role jury is same-model review; keep owner approval as the final checkpoint.")
    if status in {"failed", "interrupted"}:
        risks.insert(0, "Run did not finish successfully, so it is excluded from the winner.")

    rankable = bool(
        score is not None
        and any(item["score"] > 0 for item in breakdown)
        and status not in {"failed", "interrupted"}
    )
    audit = report.get("tailoring_audit") if isinstance(report.get("tailoring_audit"), dict) else {}
    recommendation = str(audit.get("recommended_version") or "review")
    if recommendation == "base":
        rankable = False
        score = None
        risks.insert(0, "Comparative audit prefers the canonical base resume over this tailored draft.")
    elif recommendation == "blocked" or audit.get("readiness") == "blocked":
        rankable = False
        score = None
        risks.insert(0, "Tailoring audit is blocked by a critical safety or eligibility gate.")
    elif audit.get("readiness") == "review":
        risks.append("Tailoring audit still requires review; numeric ranking remains a diagnostic only.")
    if recommendation == "tailored":
        strengths.append("Comparative audit prefers this tailored evidence selection over the base resume.")
    return {
        "version": OBJECTIVE_RESUME_RUBRIC_VERSION,
        "score": score if rankable else None,
        "confidence": confidence,
        "rankable": rankable,
        "recommended_version": recommendation,
        "tailoring_decision": str(audit.get("decision") or "needs_review"),
        "breakdown": breakdown,
        "strengths": strengths[:5],
        "risks": risks[:6],
        "note": "Per-posting decision aid; unavailable evidence is omitted rather than treated as a zero.",
    }


def bridged_job(value: Any) -> Optional[Dict[str, Any]]:
    """Validate a public Job Radar snapshot opened from the production UI.

    Production sends only public posting metadata in the URL fragment. The
    browser then posts that snapshot to this loopback-only service, allowing a
    newly discovered role to be tailored before the local checkout's generated
    state catches up. The snapshot never becomes repository state; RunManager
    stores it only inside the private, ignored run directory.
    """
    if not isinstance(value, dict):
        return None

    def field(name: str, limit: int = MAX_BRIDGED_FIELD_CHARS) -> str:
        return clean_text(str(value.get(name) or ""))[:limit]

    job_id = field("id", MAX_BRIDGED_JOB_ID_CHARS)
    company = field("company")
    title = field("title")
    url = str(value.get("url") or "").strip()[:2000]
    parsed = urlparse(url)
    if not job_id or not company or not title:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    locations = value.get("locations") or []
    if not isinstance(locations, list):
        locations = []
    locations = [clean_text(str(item))[:160] for item in locations[:12] if clean_text(str(item))]
    try:
        score_value = max(0, min(100, int(value.get("score") or 0)))
    except (TypeError, ValueError):
        score_value = 0
    posted_at = value.get("posted_at")
    if not isinstance(posted_at, (int, float, str)):
        posted_at = None
    description = clean_text(str(value.get("description") or ""))[:MAX_POSTING_CHARS]
    return {
        "id": job_id,
        "company": company,
        "title": title,
        "url": url,
        "locations": locations,
        "sector": field("sector", 120),
        "score": score_value,
        "alert_ok": bool(value.get("alert_ok")),
        "early_career_possible": bool(value.get("early_career_possible")),
        "explicit_new_grad": bool(value.get("explicit_new_grad")),
        "posted_at": posted_at,
        "description": description,
        "source": "job-radar-production-bridge",
        "bridge_origin": "VictorJimenez3 production owner session",
    }


def _library_dir(root: Optional[Path], source: str, entry_id: str) -> Optional[Path]:
    base = studio_root(root)
    if source == "run" and re.fullmatch(r"[a-f0-9]{12}", entry_id or ""):
        return base / "runs" / entry_id
    if source == "experiment" and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", entry_id or ""):
        return base / "architecture_experiments" / entry_id
    return None


def logical_pdf_filename(job: Dict[str, Any], physical: Path) -> str:
    """Give legacy run PDFs the current owner/company-facing public name."""
    if (
        physical.name == "resume.pdf"
        or re.fullmatch(r"[a-z0-9_]+_resume_(?:ai|unchained)\.pdf", physical.name, re.I)
    ):
        return resume_pdf_filename(job)
    return physical.name


def public_tailoring_brief(value: Any) -> Dict[str, Any]:
    """Expose the role-planning receipt without leaking company prose/evidence."""
    brief = value if isinstance(value, dict) else {}
    if not brief or not any(
        key in brief for key in ("essential_capabilities", "ats_terms", "ideal_project_surfaces", "provider_strategy")
    ):
        return {}
    company = brief.get("company_context") if isinstance(brief.get("company_context"), dict) else {}
    essential = []
    for item in brief.get("essential_capabilities") or []:
        if not isinstance(item, dict) or not str(item.get("term") or "").strip():
            continue
        essential.append({
            "term": str(item.get("term") or "")[:160],
            "importance": str(item.get("importance") or "mentioned")[:32],
            "supported": bool(item.get("supported")),
        })
    ats_terms = []
    for item in brief.get("ats_terms") or []:
        if not isinstance(item, dict) or not str(item.get("term") or "").strip():
            continue
        ats_terms.append({
            "term": str(item.get("term") or "")[:160],
            "importance": str(item.get("importance") or "mentioned")[:32],
            "supported": bool(item.get("supported")),
            "support_kind": str(item.get("support_kind") or "none")[:32],
        })
    surfaces = []
    for item in brief.get("ideal_project_surfaces") or []:
        if not isinstance(item, dict) or not str(item.get("entry_id") or "").strip():
            continue
        surfaces.append({
            "entry_id": str(item.get("entry_id") or "")[:180],
            "kind": "project",
            "label": str(item.get("label") or "")[:180],
            "score": item.get("score"),
            "role_signals": [str(v)[:80] for v in (item.get("role_signals") or [])[:8]],
            "domain_signals": [str(v)[:80] for v in (item.get("domain_signals") or [])[:8]],
        })
    provider = brief.get("provider_strategy") if isinstance(brief.get("provider_strategy"), dict) else {}
    try:
        requirement_count = max(0, int(provider.get("requirement_count") or 0))
    except (TypeError, ValueError):
        requirement_count = 0
    return {
        "version": str(brief.get("version") or TAILORING_BRIEF_VERSION)[:80],
        "primary_role_track": str(brief.get("primary_role_track") or "general_software")[:80],
        "company_domain": str(brief.get("company_domain") or company.get("company_domain") or "technology")[:80],
        "domain_priorities": [str(v)[:80] for v in (brief.get("domain_priorities") or company.get("domain_priorities") or [])[:4]],
        "dossier_available": bool(company.get("dossier_available")),
        "essential_capabilities": essential[:20],
        "ats_terms": ats_terms[:32],
        "honest_gaps": [str(v)[:160] for v in (brief.get("honest_gaps") or [])[:24]],
        "ideal_project_surfaces": surfaces[:8],
        "provider_strategy": {
            "portfolio_strategy": str(provider.get("portfolio_strategy") or "")[:1200],
            "must_cover_terms": [str(v)[:160] for v in (provider.get("must_cover_terms") or [])[:32]],
            "honest_gaps": [str(v)[:160] for v in (provider.get("honest_gaps") or [])[:24]],
            "requirement_count": requirement_count,
        },
    }


def artifact_target(directory: Path, filename: str) -> Optional[Path]:
    """Resolve a public artifact name, including the legacy PDF alias."""
    target = (directory / Path(filename).name).resolve()
    if directory.resolve() not in target.parents:
        return None
    if target.is_file():
        return target
    job = read_json(directory / "job.json", {}) or {}
    if not isinstance(job, dict):
        job = {}
    if target.suffix == ".pdf" and (
        target.name == resume_pdf_filename(job)
        or target.name.startswith("victor_jimenez_")
    ):
        legacy = run_pdf_path(directory)
        if legacy.is_file():
            return legacy
    if target.suffix == ".png" and (
        target.name == Path(resume_pdf_filename(job)).stem + "-preview.png"
        or target.name.startswith("victor_jimenez_")
    ):
        legacy = run_preview_path(directory)
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
    audit = report.get("tailoring_audit") if isinstance(report.get("tailoring_audit"), dict) else None
    context = read_json(directory / "job_context.json", {}) or {}
    if not isinstance(context, dict):
        context = {}
    tailoring_brief = public_tailoring_brief(
        report.get("tailoring_brief") or context.get("tailoring_brief")
    )
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
    status_age_seconds = timestamp_age_seconds(
        status.get("updated_at") or status.get("created_at")
    )
    stale = bool(
        display_status in {"queued", "running"}
        and status_age_seconds is not None
        and status_age_seconds > RUN_STALE_AFTER_SECONDS
    )
    objective = objective_resume_assessment(report, display_status)
    changes = report.get("content_changes") if isinstance(report.get("content_changes"), dict) else {}
    coverage = changes.get("keyword_coverage") if isinstance(changes.get("keyword_coverage"), dict) else {}
    overlay = report.get("review_overlay") if isinstance(report.get("review_overlay"), dict) else {}
    if (
        overlay.get("available") and pdf.is_file()
        and any(not str(box.get("text") or "").strip() for box in (overlay.get("boxes") or []) if isinstance(box, dict))
    ):
        refreshed_overlay = review_preview_overlay(
            pdf,
            report.get("content_plan") if isinstance(report.get("content_plan"), dict) else {},
            changes,
            coverage,
        )
        if refreshed_overlay.get("available"):
            overlay = refreshed_overlay
    keyword_terms = []
    for item in coverage.get("terms") or []:
        if not isinstance(item, dict) or not str(item.get("term") or "").strip():
            continue
        keyword_terms.append({
            "term": str(item.get("term") or "")[:160],
            "importance": str(item.get("importance") or "")[:40],
            "required": bool(item.get("required")),
            "preferred": bool(item.get("preferred")),
            "supported": bool(item.get("supported")),
            "rendered": bool(item.get("rendered")),
            "base_rendered": item.get("base_rendered"),
            "comparison_status": str(item.get("comparison_status") or "unknown")[:40],
            "status": str(item.get("status") or "")[:40],
            "support_kind": str(item.get("support_kind") or "")[:120],
            "source_ids": [str(value)[:180] for value in (item.get("source_ids") or [])[:8]],
        })
    overlay_boxes = []
    for box in (overlay.get("boxes") or [])[:160]:
        if not isinstance(box, dict):
            continue
        overlay_boxes.append({key: box.get(key) for key in (
            "left_percent", "top_percent", "width_percent", "height_percent",
            "terms", "text", "changed_source_id", "kind",
        )})
    keyword_audit = {
        "posting_available": bool(coverage.get("posting_available")),
        "detected_count": int(coverage.get("detected_count") or 0),
        "supported_count": int(coverage.get("supported_count") or 0),
        "covered_count": int(coverage.get("covered_count") or 0),
        "supported_coverage_percent": coverage.get("supported_exact_coverage_percent"),
        "overall_coverage_percent": coverage.get("exact_coverage_percent"),
        "required_coverage_percent": coverage.get("required_coverage_percent"),
        "terms": keyword_terms[:80],
        "overlay": {"available": bool(overlay.get("available") and overlay_boxes), "boxes": overlay_boxes},
    }
    artifacts = []
    for name in (public_pdf_name, public_preview_name, "job.json", "job_context.json", "report.json", "content_plan.json", "candidate_plan.json", "layout_packing.json", "job_intelligence.json", "tailoring_audit.json", "comparison_control.json", "resume.tex", "resume.txt", "workshop.json"):
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
        "queue_id": str(status.get("queue_id") or report.get("queue_id") or (audit or {}).get("queue_id") or "")[:80],
        "status": display_status,
        "step": str(status.get("step") or ""),
        "message": str(status.get("message") or ""),
        "mode": mode,
        "created_at": created_at,
        "updated_at": str(status.get("updated_at") or created_at),
        "status_age_seconds": round(status_age_seconds, 1) if status_age_seconds is not None else None,
        "stale": stale,
        "stale_reason": (
            "No status update for more than %d minutes; the run is still recoverable and may be requeued."
            % (RUN_STALE_AFTER_SECONDS // 60)
            if stale else ""
        ),
        "engine_runtime": status.get("engine_runtime") if isinstance(status.get("engine_runtime"), dict) else {},
        "job": job_summary(job, resume_match),
        "pdf_filename": public_pdf_name,
        "preview_filename": public_preview_name if preview.is_file() else "",
        "has_pdf": pdf.is_file(),
        "has_posting_snapshot": bool(str(context.get("posting_text") or job.get("description") or "").strip()),
        "has_workshop": source == "run" and (directory / "content_plan.json").is_file(),
        "approval_state": str(status.get("approval_state") or report.get("approval_state") or "awaiting_review")[:40],
        "winner_version": str(report.get("winner_version") or (report.get("winner_artifact") or {}).get("winner_version") or "")[:24],
        "craft_score": review.get("craft_score"),
        "ready": review.get("ready"),
        "review_plan_applied": report.get("review_plan_applied"),
        "validation_warnings": report.get("validation_warnings") or [],
        "objective": objective,
        "tailoring_audit": tailoring_audit_summary(audit),
        "tailoring_brief": tailoring_brief,
        "keyword_audit": keyword_audit,
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


OFFLINE_TITLE_STOPWORDS = frozenset({
    "a", "an", "and", "associate", "early", "engineer", "engineering",
    "for", "graduate", "i", "ii", "iii", "intern", "junior", "new",
    "of", "role", "senior", "the", "with",
})


def _resume_bank_text(directory: Path) -> str:
    """Read searchable resume text without changing the private artifact."""
    text_path = directory / "resume.txt"
    if text_path.is_file():
        try:
            return clean_text(text_path.read_text(errors="replace"))
        except OSError:
            pass
    tex_path = directory / "resume.tex"
    if tex_path.is_file():
        try:
            return clean_text(_latex_plain(tex_path.read_text(errors="replace")))
        except OSError:
            pass
    pdf = run_pdf_path(directory)
    pdftotext = shutil.which("pdftotext")
    if pdftotext and pdf.is_file():
        try:
            return clean_text(subprocess.check_output(
                [pdftotext, str(pdf), "-"], timeout=20, text=True,
                stderr=subprocess.DEVNULL,
            ))
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def _resume_bank_candidate(
    root: Optional[Path], entry: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Normalize one reusable bank PDF and reject failed/base outcomes."""
    if (
        entry.get("source") != "run"
        or entry.get("status") not in CURATED_RESUME_STATUSES
        or not entry.get("has_pdf")
        or str(entry.get("winner_version") or "").lower() == "base"
    ):
        return None
    directory = _library_dir(root, "run", str(entry.get("entry_id") or ""))
    if directory is None or not directory.is_dir():
        return None
    pdf = run_pdf_path(directory)
    if not pdf.is_file():
        return None
    status = read_json(directory / "status.json", {}) or {}
    report = read_json(directory / "report.json", {}) or {}
    job = read_json(directory / "job.json", {}) or {}
    context = read_json(directory / "job_context.json", {}) or {}
    if not isinstance(status, dict) or not isinstance(report, dict):
        return None
    if not isinstance(job, dict):
        job = {}
    if not isinstance(context, dict):
        context = {}
    approval = str(
        status.get("approval_state") or report.get("approval_state") or "awaiting_review"
    ).lower()
    winner_artifact = report.get("winner_artifact")
    if not isinstance(winner_artifact, dict):
        winner_artifact = {}
    winner = str(
        report.get("winner_version")
        or winner_artifact.get("winner_version")
        or entry.get("winner_version")
        or "legacy_unverified"
    ).lower()
    safe_for_application = approval == "approved" and winner == "tailored"
    posting_text = str(context.get("posting_text") or job.get("description") or "").strip()
    value = {
        "run_id": str(entry.get("run_id") or entry.get("entry_id") or ""),
        "company": str(job.get("company") or (entry.get("job") or {}).get("company") or ""),
        "title": str(job.get("title") or (entry.get("job") or {}).get("title") or ""),
        "sector": str(job.get("sector") or (entry.get("job") or {}).get("sector") or ""),
        "status": str(entry.get("status") or ""),
        "approval_state": approval,
        "winner_version": winner,
        "safe_for_application": safe_for_application,
        "needs_owner_review": not safe_for_application,
        "created_at": str(entry.get("created_at") or ""),
        "updated_at": str(entry.get("updated_at") or entry.get("created_at") or ""),
        "pdf_filename": logical_pdf_filename(job, pdf),
        "pdf_path": str(pdf.resolve()),
        "pdf_sha256": _sha256_file(pdf),
        "posting_available": bool(posting_text),
        "objective": entry.get("objective") if isinstance(entry.get("objective"), dict) else {},
        "_job": job,
        "_posting_text": posting_text,
    }
    return value


def resume_bank(
    root: Optional[Path] = None, query: str = "", approved_only: bool = False,
    limit: int = 500,
) -> Dict[str, Any]:
    """Return unique reusable PDFs from the private run history."""
    candidates = []
    for entry in resume_library(root, query=query, limit=500):
        candidate = _resume_bank_candidate(root, entry)
        if candidate is None or (approved_only and not candidate["safe_for_application"]):
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda item: (
        bool(item["safe_for_application"]),
        item.get("winner_version") == "tailored",
        item.get("updated_at") or "",
        item.get("run_id") or "",
    ), reverse=True)
    unique: List[Dict[str, Any]] = []
    by_digest: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        digest = str(candidate.get("pdf_sha256") or "")
        existing = by_digest.get(digest)
        if existing is not None:
            existing["duplicate_run_ids"].append(candidate["run_id"])
            continue
        public = {key: value for key, value in candidate.items() if not key.startswith("_")}
        public["duplicate_run_ids"] = []
        by_digest[digest] = public
        unique.append(public)
    bounded = unique[:max(1, min(int(limit or 500), 500))]
    return {
        "bank_root": str(studio_root(root) / "runs"),
        "unique_resumes": len(unique),
        "approved_resumes": sum(bool(item["safe_for_application"]) for item in unique),
        "review_required_resumes": sum(bool(item["needs_owner_review"]) for item in unique),
        "entries": bounded,
    }


def _offline_title_tokens(value: Any) -> set:
    return {
        token for token in re.findall(r"[a-z0-9+#.]+", str(value or "").lower())
        if len(token) > 1 and token not in OFFLINE_TITLE_STOPWORDS
    }


def _offline_role_bucket(job: Dict[str, Any], posting_text: str) -> str:
    from radar.score import role_bucket

    return str(role_bucket(str(job.get("title") or ""), posting_text) or "general")


def _offline_match_score(
    target_job: Dict[str, Any], target_text: str, candidate: Dict[str, Any],
) -> Dict[str, Any]:
    source_job = candidate.get("_job") if isinstance(candidate.get("_job"), dict) else {}
    source_text = str(candidate.get("_posting_text") or "")
    resume_text = str(candidate.get("_resume_text") or "")
    target_track = role_track_profile(target_job, target_text)
    source_track = role_track_profile(source_job, source_text)
    target_primary = str(target_track.get("primary_track") or "general_software")
    source_primary = str(source_track.get("primary_track") or "general_software")
    target_secondary = set(target_track.get("secondary_tracks") or [])
    source_secondary = set(source_track.get("secondary_tracks") or [])
    reasons: List[str] = []
    breakdown: Dict[str, float] = {}

    if target_primary == source_primary:
        breakdown["role_track"] = 34.0 if target_primary != "general_software" else 18.0
        reasons.append("same %s role track" % target_track.get("primary_label", target_primary))
    elif source_primary in target_secondary or target_primary in source_secondary:
        breakdown["role_track"] = 21.0
        reasons.append("strong adjacent role-track match")
    elif target_secondary.intersection(source_secondary):
        breakdown["role_track"] = 10.0
        reasons.append("shares a secondary role track")
    else:
        breakdown["role_track"] = 0.0

    target_bucket = _offline_role_bucket(target_job, target_text)
    source_bucket = _offline_role_bucket(source_job, source_text)
    if target_bucket == source_bucket:
        breakdown["role_bucket"] = 14.0
        reasons.append("same %s role family" % target_bucket.replace("_", " "))
    elif {target_bucket, source_bucket} <= {"ai_ml", "data_science", "data_eng"}:
        breakdown["role_bucket"] = 8.0
        reasons.append("adjacent AI/data role family")
    elif "general" in {target_bucket, source_bucket}:
        breakdown["role_bucket"] = 3.0
    else:
        breakdown["role_bucket"] = 0.0

    target_terms = [term for term in TARGET_KEYWORD_TERMS if _keyword_present(term, target_text)]
    covered_terms = [term for term in target_terms if _keyword_present(term, resume_text)]
    breakdown["keyword_coverage"] = round(
        28.0 * len(covered_terms) / max(1, len(target_terms)), 2
    ) if target_terms else 0.0
    if covered_terms:
        reasons.append("covers %d/%d detected target terms" % (len(covered_terms), len(target_terms)))

    target_title = _offline_title_tokens(target_job.get("title"))
    source_title = _offline_title_tokens(source_job.get("title"))
    overlap = target_title.intersection(source_title)
    breakdown["title_overlap"] = round(
        14.0 * len(overlap) / max(1, len(target_title.union(source_title))), 2
    ) if target_title and source_title else 0.0
    if overlap:
        reasons.append("title overlap: %s" % ", ".join(sorted(overlap)[:4]))

    target_sector = str(target_job.get("sector") or "").strip().lower()
    source_sector = str(source_job.get("sector") or "").strip().lower()
    breakdown["sector"] = 4.0 if target_sector and target_sector == source_sector else 0.0
    if breakdown["sector"]:
        reasons.append("same sector")

    quality = 0.0
    if candidate.get("safe_for_application"):
        quality += 8.0
        reasons.append("owner-approved tailored winner")
    if candidate.get("winner_version") == "tailored":
        quality += 4.0
    objective = candidate.get("objective") if isinstance(candidate.get("objective"), dict) else {}
    if objective.get("rankable") and objective.get("score") is not None:
        quality += min(3.0, max(0.0, float(objective.get("score") or 0) / 34.0))
    breakdown["artifact_quality"] = round(quality, 2)

    return {
        "score": round(sum(breakdown.values()), 2),
        "breakdown": breakdown,
        "reasons": reasons[:6],
        "target_track": target_primary,
        "source_track": source_primary,
        "target_bucket": target_bucket,
        "source_bucket": source_bucket,
        "detected_target_terms": target_terms[:40],
        "covered_target_terms": covered_terms[:40],
    }


def offline_resume_matches(
    job: Dict[str, Any], root: Optional[Path] = None, *, approved_only: bool = False,
    limit: int = 5,
) -> Dict[str, Any]:
    """Rank existing private PDFs for a target role without calling Codex."""
    target_job = dict(job or {})
    target_text = clean_text(str(target_job.get("description") or ""))
    ranked = []
    for entry in resume_library(root, limit=500):
        candidate = _resume_bank_candidate(root, entry)
        if candidate is None or (approved_only and not candidate["safe_for_application"]):
            continue
        directory = _library_dir(root, "run", str(candidate.get("run_id") or ""))
        candidate["_resume_text"] = _resume_bank_text(directory) if directory is not None else ""
        fit = _offline_match_score(target_job, target_text, candidate)
        ranked.append({**candidate, **fit})
    ranked.sort(key=lambda item: (
        float(item.get("score") or 0),
        bool(item.get("safe_for_application")),
        item.get("winner_version") == "tailored",
        item.get("updated_at") or "",
    ), reverse=True)
    unique = []
    seen = set()
    for candidate in ranked:
        digest = str(candidate.get("pdf_sha256") or "")
        if digest in seen:
            continue
        seen.add(digest)
        unique.append({key: value for key, value in candidate.items() if not key.startswith("_")})
    bounded = unique[:max(1, min(int(limit or 5), 20))]
    return {
        "offline": True,
        "provider_calls": 0,
        "selection_kind": "existing_pdf_unchanged",
        "approved_only": approved_only,
        "target": job_summary(target_job),
        "match_count": len(unique),
        "matches": bounded,
        "selected": bounded[0] if bounded else None,
        "note": "This ranks and reuses an existing PDF; it does not rewrite resume content.",
    }


def offline_tailor_resume(
    job: Dict[str, Any], root: Optional[Path] = None, *, approved_only: bool = True,
    limit: int = 5, copy_pdf: bool = True,
) -> Dict[str, Any]:
    """Select the best existing role match and optionally make a target-named copy."""
    result = offline_resume_matches(job, root, approved_only=approved_only, limit=limit)
    selected = result.get("selected")
    if not isinstance(selected, dict):
        return {**result, "output_path": "", "copied": False}
    output_path = ""
    if copy_pdf:
        source = Path(str(selected.get("pdf_path") or ""))
        if source.is_file():
            destination = offline_resume_root(root)
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / resume_pdf_filename(job)
            shutil.copy2(source, target)
            output_path = str(target.resolve())
            index_path = destination / OFFLINE_RESUME_INDEX
            index = read_json(index_path, {}) or {}
            selections = index.get("selections") if isinstance(index.get("selections"), dict) else {}
            selections[target.name] = {
                "target_company": str(job.get("company") or ""),
                "target_title": str(job.get("title") or ""),
                "source_run_id": selected.get("run_id"),
                "source_company": selected.get("company"),
                "source_title": selected.get("title"),
                "score": selected.get("score"),
                "approval_state": selected.get("approval_state"),
                "needs_owner_review": selected.get("needs_owner_review"),
                "selected_at": now_iso(),
                "content_changed": False,
            }
            write_json(index_path, {
                "generated_at": now_iso(),
                "selection_kind": "existing_pdf_unchanged",
                "selections": selections,
            })
    return {**result, "output_path": output_path, "copied": bool(output_path)}


def application_resume_status(
    job: Dict[str, Any], root: Optional[Path] = None, queue_id: str = "",
    allow_fallback: bool = False,
) -> Dict[str, Any]:
    """Resolve the Resume Studio artifact an application session may use.

    Application Autopilot asks this local service before it starts scanning an
    employer form.  A safe tailored winner is preferred, an in-flight matching
    run is reported so the extension can wait, and the immutable resume remains
    the last-resort control when a tailor honestly rejects its own candidate.
    The selected PDF stays on Victor's Mac.  The paired extension fetches it
    from the loopback-only service and places it in the employer file control;
    only the filename and progress metadata are mirrored to the cloud queue.
    """
    job_id = str((job or {}).get("id") or "")
    entries = resume_library(root, job_id=job_id, limit=100) if job_id else []
    active_queue = "application-%s" % str(queue_id or "").strip()[:80] if queue_id else ""
    running = [
        item for item in entries
        if item.get("source") == "run"
        and item.get("status") in {"queued", "running"}
        and (not active_queue or str(item.get("queue_id") or "") == active_queue)
    ]
    if running:
        current = sorted(running, key=lambda item: (item.get("updated_at") or "", item.get("entry_id") or ""), reverse=True)[0]
        return {
            "status": "running", "source": "resume_studio", "run_id": current.get("run_id") or current.get("entry_id") or "",
            "message": current.get("message") or "Resume Studio is tailoring this role.",
            "mode": current.get("mode") or "ai",
        }
    tailored = [
        item for item in entries
        if item.get("source") == "run"
        and item.get("has_pdf")
        and item.get("status") in {"complete", "completed", "awaiting_review"}
        and item.get("winner_version") == "tailored"
        and item.get("mode") in {"used", "ai", "unrestricted", "generation"}
    ]
    if tailored:
        selected = sorted(tailored, key=lambda item: (
            item.get("status") == "complete", bool((item.get("objective") or {}).get("rankable")),
            float((item.get("objective") or {}).get("score") or -1), item.get("updated_at") or "",
        ), reverse=True)[0]
        return {
            "status": "ready", "source": "tailored", "run_id": selected.get("run_id") or selected.get("entry_id") or "",
            "mode": selected.get("mode") or "ai", "resume_status": selected.get("status") or "awaiting_review",
            "approval_state": selected.get("approval_state") or "awaiting_review",
            "winner_version": "tailored", "pdf_filename": selected.get("pdf_filename") or "",
            "file_ready": True,
            "message": "Resume Studio found a safe tailored winner for this role.",
            "needs_owner_review": selected.get("status") != "complete" or selected.get("approval_state") != "approved",
        }
    terminal_for_queue = [
        item for item in entries
        if active_queue
        and item.get("source") == "run"
        and item.get("status") in {"complete", "completed", "awaiting_review", "failed"}
        and str(item.get("queue_id") or "") == active_queue
    ]
    if terminal_for_queue:
        selected = sorted(terminal_for_queue, key=lambda item: (
            item.get("updated_at") or "", item.get("entry_id") or "",
        ), reverse=True)[0]
        fallback = application_fallback_resume(job, root)
        result = {
            **fallback,
            "tailor_run_id": selected.get("run_id") or selected.get("entry_id") or "",
            "resume_status": selected.get("status") or "awaiting_review",
            "message": "Resume Studio already finished this queued role without a safe tailored winner; %s" % fallback["message"],
            "needs_owner_review": True,
        }
        if fallback.get("source") == "immutable":
            result["run_id"] = selected.get("run_id") or selected.get("entry_id") or ""
        return result
    if allow_fallback:
        return application_fallback_resume(job, root)
    return {
        "status": "missing", "source": "resume_studio", "recommended_mode": "ai",
        "message": "No safe tailored Resume Studio winner exists for this role yet.",
    }


def application_fallback_resume(job: Dict[str, Any], root: Optional[Path] = None) -> Dict[str, Any]:
    """Choose the best approved offline role match before the canonical base."""
    selection = offline_resume_matches(job, root, approved_only=True, limit=1)
    selected = selection.get("selected")
    if isinstance(selected, dict):
        label = str(selected.get("company") or "approved")
        return {
            "status": "fallback", "source": "reference", "mode": "approved_reference",
            "run_id": selected.get("run_id") or "",
            # The bytes come from the approved source run, but the upload name
            # identifies Victor and the company receiving this application.
            "pdf_filename": resume_pdf_filename(job),
            "source_pdf_filename": selected.get("pdf_filename") or "",
            "fallback_profile": str(selected.get("source_track") or "offline_match"),
            "offline_match_score": selected.get("score"),
            "offline_match_reasons": selected.get("reasons") or [],
            "file_ready": True,
            "message": "Use the owner-approved %s resume selected by the offline role matcher." % label,
            "needs_owner_review": False,
        }
    canonical = cv_root(root) / CANONICAL_PDF
    return {
        "status": "fallback", "source": "immutable", "mode": "canonical",
        "pdf_filename": canonical.name, "fallback_profile": "base",
        "file_ready": canonical.is_file(),
        "message": "Use the immutable canonical resume as the safe default.",
        "needs_owner_review": not canonical.is_file(),
    }


def application_resume_file(
    job: Dict[str, Any], root: Optional[Path] = None, queue_id: str = "",
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Resolve the exact local PDF selected for an application form."""
    status = application_resume_status(job, root, queue_id=queue_id, allow_fallback=True)
    target: Optional[Path] = None
    if status.get("source") in {"tailored", "reference"}:
        directory = _library_dir(root, "run", str(status.get("run_id") or ""))
        if directory is not None:
            source_name = (
                status.get("source_pdf_filename")
                if status.get("source") == "reference"
                else status.get("pdf_filename")
            )
            target = artifact_target(directory, str(source_name or ""))
            if status.get("source") == "reference" and (target is None or not target.is_file()):
                # Older approved bank entries may not have persisted their
                # public filename. The run's actual PDF remains authoritative
                # for bytes; ``pdf_filename`` above remains the safe upload
                # name presented to the employer.
                candidate = run_pdf_path(directory)
                target = candidate if candidate.is_file() else None
    elif status.get("source") == "immutable":
        candidate = (cv_root(root) / CANONICAL_PDF).resolve()
        if candidate.is_file():
            target = candidate
    if target is None or target.suffix.lower() != ".pdf" or not target.is_file():
        return ({**status, "file_ready": False, "message": "The selected local resume PDF is unavailable."}, None)
    return ({**status, "file_ready": True, "pdf_filename": status.get("pdf_filename") or target.name}, target)


def studio_usage(root: Optional[Path] = None) -> Dict[str, Any]:
    """Aggregate observed provider usage from durable run reports.

    Codex CLI does not expose a user's Plus weekly allowance to this local
    process. We therefore report measured calls/tokens and only calculate a
    percentage when the owner explicitly supplies CODEX_WEEKLY_LIMIT_TOKENS.
    """
    base = studio_root(root)
    now = dt.datetime.now(dt.timezone.utc)
    week_start = (now - dt.timedelta(days=now.weekday())).date().isoformat()
    totals = {"codex_tokens": 0, "codex_calls": 0}
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
    # executable. Codex Luna is the sole approved execution lane.
    return {"codex": shutil.which("codex")}


def subscription_environment(run_dir: Path) -> Dict[str, str]:
    """Prefer cached first-party subscription sessions over API keys.

    The local CLIs own their credential stores.  Removing these variables is a
    cost/privacy guard: a stray shell API key must not silently turn this into
    billable API traffic.
    """
    env = dict(os.environ)
    for name in (
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
    if label.startswith("application_essay"):
        return bool(str(data.get("answer") or "").strip()) or bool(data.get("needs_owner_input"))
    if label.startswith(("review", "critique")) or "_critique" in label:
        criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else data
        return any(
            isinstance(value, dict) and str(value.get("status", "")).lower() in STATUS_MULTIPLIER
            for value in criteria.values()
        )
    if label.startswith("space_expansion"):
        return isinstance(data.get("additions"), list) and isinstance(data.get("decision"), str)
    if label.startswith("gap_analysis"):
        return isinstance(data.get("requirements"), list) and isinstance(data.get("portfolio_strategy"), str)
    return all(key in data for key in ("experiences", "projects", "leadership", "positioning_thesis"))


def application_essay_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "needs_owner_input": {"type": "boolean"},
            "missing_facts": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "assumption": {"type": "string"},
        },
        "required": ["answer", "needs_owner_input", "missing_facts", "evidence_ids", "assumption"],
        "additionalProperties": False,
    }


def application_essay_answer(
    root: Path, job: Dict[str, Any], question: str, session_id: str = "",
    character_limit: int = 0, word_limit: int = 0, category: str = "essay",
) -> Dict[str, Any]:
    """Draft one role-specific response with Victor's installed warm-writing skill."""
    question = str(question or "").strip()[:1200]
    if not question:
        raise ValueError("application question is required")
    skill_path = Path.home() / ".codex" / "skills" / "warm-scholarship-essay" / "SKILL.md"
    if not skill_path.is_file():
        raise RuntimeError("warm-scholarship-essay skill is not installed on this Mac")
    private_root = studio_root(root) / "application_answers" / uuid.uuid4().hex[:12]
    private_root.mkdir(parents=True, exist_ok=True)
    inventory = context_inventory(root, limit=180)
    facts = [
        {key: item.get(key) for key in ("id", "source", "heading", "text", "authority", "review_status")}
        for item in inventory.get("facts", [])
    ]
    application_answers = application_context(root).get("answers", [])
    approved_application_answers = [
        {
            "question": item.get("question"), "value": item.get("value"),
            "category": item.get("category"), "evidence_ids": item.get("evidence_ids") or [],
        }
        for item in application_answers
        if item.get("reusable") is not False
        and item.get("category") in {"essay", "cover_letter", "llm_experience", "work_schedule", "education", "location"}
    ][:120]
    scoped_writing_context = [
        {
            "question": item.get("question"), "value": item.get("value"),
            "category": item.get("category"), "evidence_ids": item.get("evidence_ids") or [],
        }
        for item in application_answers
        if session_id and session_id in (item.get("session_ids") or [])
        and item.get("category") in {"essay_context", "essay", "cover_letter"}
    ][:40]
    profile_text = (root / "profile.yaml").read_text(errors="replace") if (root / "profile.yaml").is_file() else ""
    prompt = (
        "Use the installed $warm-scholarship-essay skill for this job-application response. "
        "The exact skill instructions are included below and are mandatory. Return only JSON matching the schema. "
        "Treat this as a short application essay, even when the employer calls it an open response. Never invent facts. "
        "Write in Victor's direct, straightforward voice, with no em dashes. If a truthful, specific answer cannot be "
        "written from the supplied evidence, set needs_owner_input true, leave answer empty, and list only the smallest "
        "missing facts needed. Answer the exact question immediately, use concrete evidence, and do not turn the resume "
        "into a paragraph. Victor's current expected NJIT graduation is May 2027; any 2026 graduation reference in older "
        "evidence is stale. Respect every positive character or word limit.\n\n"
        "EXACT QUESTION:\n" + question + "\n\n"
        "CHARACTER LIMIT:\n" + (str(max(0, int(character_limit))) if character_limit else "No explicit limit") + "\n\n"
        "WORD LIMIT:\n" + (str(max(0, int(word_limit))) if word_limit else "No explicit limit") + "\n\n"
        "ROLE:\n" + json.dumps({
            "company": job.get("company"), "title": job.get("title"),
            "locations": job.get("locations") or [], "description": str(job.get("description") or "")[:MAX_POSTING_CHARS],
        }, ensure_ascii=False, indent=2) + "\n\n"
        "VICTOR PROFILE:\n" + profile_text[:12000] + "\n\n"
        "APPROVED APPLICATION CONTEXT:\n" + json.dumps(approved_application_answers, ensure_ascii=False, indent=2)[:16000] + "\n\n"
        "OWNER-PROVIDED CONTEXT FOR THIS APPLICATION:\n" + json.dumps(scoped_writing_context, ensure_ascii=False, indent=2)[:8000] + "\n\n"
        "AUTHORIZED PRIVATE EVIDENCE:\n" + json.dumps(facts, ensure_ascii=False, indent=2)[:24000] + "\n\n"
        "WARM SCHOLARSHIP ESSAY SKILL:\n" + skill_path.read_text(errors="replace")[:16000]
    )
    result = run_provider(
        "codex", prompt[:MAX_PROMPT_CHARS], private_root,
        "application_essay_%s" % uuid.uuid4().hex[:8], timeout=5 * 60,
        schema=application_essay_schema(),
    )
    write_json(private_root / "result.json", result)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "warm application response generation failed")
    data = result.get("data") or {}
    answer = str(data.get("answer") or "").strip()
    if data.get("needs_owner_input") or not answer:
        missing = "; ".join(str(item) for item in (data.get("missing_facts") or []) if str(item).strip())
        raise ValueError(missing or "This response needs one role-specific fact from Victor")
    answer = re.sub(r"\s*—\s*", ", ", answer)
    if character_limit and len(answer) > int(character_limit):
        raise RuntimeError("Generated response exceeded the employer's character limit")
    if word_limit and len(re.findall(r"\S+", answer)) > int(word_limit):
        raise RuntimeError("Generated response exceeded the employer's word limit")
    if re.search(r"\b(?:class of|graduat(?:e|ing|ion)[^.!?]{0,30})\s*2026\b", answer, re.IGNORECASE):
        raise RuntimeError("Generated response used the stale 2026 graduation year instead of May 2027")
    saved = save_application_answer(
        root, question=question, value=answer,
        category=category if category in {"essay", "cover_letter"} else "essay", reusable=False,
        evidence_ids=data.get("evidence_ids") or [], session_id=session_id,
    )
    return {
        "ok": True, "answer": saved, "skill": "warm-scholarship-essay",
        "assumption": str(data.get("assumption") or "")[:800],
        "provider": "codex", "usage_tokens": result.get("usage_tokens"),
    }


def plan_schema(enhance: bool, generation: bool = False) -> Dict[str, Any]:
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
            "source_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        },
        "required": [
            "action", "current_evidence", "replacement_or_exclusion",
            "target_signal", "why_stronger", "signal_lost", "source_ids",
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

    properties = {
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
        }
    required = [
        "positioning_thesis", "selected_evidence", "excluded_evidence",
        "experiences", "projects", "leadership", "revision_notes",
        "decision_ledger", "front_matter_policy",
    ]
    if generation:
        properties["front_matter_rewrites"] = {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "line_id": {
                        "type": "string",
                        "enum": ["front:skills:%d" % index for index in range(5)],
                    },
                    "text": {"type": "string"},
                    "evidence_ids": {
                        "type": "array", "items": {"type": "string"},
                        "minItems": 1, "maxItems": 8,
                    },
                    "why": {"type": "string"},
                },
                "required": ["line_id", "text", "evidence_ids", "why"],
                "additionalProperties": False,
            },
        }
        required.append("front_matter_rewrites")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def gap_analysis_schema() -> Dict[str, Any]:
    requirement = {
        "type": "object",
        "properties": {
            "requirement": {"type": "string"},
            "importance": {
                "type": "string",
                "enum": ["required", "preferred", "responsibility", "mentioned"],
            },
            "exact_terms": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 8,
            },
            "evidence_status": {
                "type": "string", "enum": ["direct", "adjacent", "unsupported"],
            },
            "evidence_ids": {
                "type": "array", "items": {"type": "string"}, "maxItems": 8,
            },
            "target_entry_id": {"type": "string"},
            "recommended_action": {
                "type": "string",
                "enum": ["keep", "reorder", "rewrite", "synthesize", "tailor_skills", "leave_gap"],
            },
            "candidate_angle": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": [
            "requirement", "importance", "exact_terms", "evidence_status",
            "evidence_ids", "target_entry_id", "recommended_action",
            "candidate_angle", "reason",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "portfolio_strategy": {"type": "string"},
            # Do not accept a provider's promise to analyze the posting as the
            # analysis itself. The normalizer still fills individual omissions.
            "requirements": {
                "type": "array", "items": requirement,
                "minItems": 8, "maxItems": 48,
            },
            "must_cover_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "honest_gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 24},
        },
        "required": ["portfolio_strategy", "requirements", "must_cover_terms", "honest_gaps"],
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
    """Recover a final response from the CLI's declared output file.

    Do not parse stderr as a fallback.  Codex can echo prompts, tool traces,
    and retrieved context there; those blobs may themselves contain a JSON
    object with the plan's top-level keys.  Treating that object as the model
    answer is a silent writer-contamination bug: the run appears successful
    while the real author response is still pending.  The ``-o`` output file
    is the only response channel authorized by ``run_provider`` and remains
    sufficient for recovering a response from a CLI that lingers afterward.
    """
    del stderr_path
    try:
        raw = stdout_path.read_text(errors="replace")
    except OSError:
        return None
    if not raw.strip():
        return None
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


def codex_effort_task(label: str) -> str:
    """Map a durable provider label to its reasoning-budget category."""
    normalized = str(label or "").strip().lower()
    if (
        normalized.startswith(("review", "critique"))
        or "_critique" in normalized
    ):
        return "review"
    if normalized.startswith("line_edit"):
        return "line_edit"
    if normalized.startswith("revision"):
        return "revision"
    if normalized.startswith("workshop"):
        return "workshop"
    if normalized.startswith("gap_analysis"):
        return "gap_analysis"
    if normalized.startswith("space_expansion"):
        return "space_expansion"
    if normalized.startswith("synthesis"):
        return "synthesis"
    return "draft"


def codex_reasoning_effort(label: str, override: Optional[str] = None) -> str:
    """Return the configured Luna effort for one stage.

    The lookup order is explicit for auditability. Ordinary tasks default to
    the configured High lane; Max is an explicit profile/override rather than
    a hidden fallback. Unsupported lower-effort overrides fall back to high
    only for legacy callers, never silently to a different provider.
    """
    task = codex_effort_task(label)
    candidates = [
        override,
        os.environ.get("RESUME_STUDIO_%s_CODEX_EFFORT" % task.upper()),
        os.environ.get("RESUME_STUDIO_CODEX_EFFORT"),
        CODEX_TASK_EFFORT_DEFAULTS.get(task, "high"),
    ]
    for value in candidates:
        normalized = str(value or "").strip().lower()
        if normalized in CODEX_EFFORTS or (override is not None and normalized == CODEX_RECHECK_EFFORT):
            return normalized
    return "high"


def provider_model_label(provider: str) -> str:
    """Name the approved subscription lane without pretending to know hidden model routing."""
    if str(provider or "") == "codex":
        return CODEX_LUNA_MODEL
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
    codex_effort: Optional[str] = None,
) -> Dict[str, Any]:
    commands = provider_commands()
    executable = commands.get(provider)
    requested_effort = codex_reasoning_effort(label, codex_effort) if provider == "codex" else ""
    if not executable:
        return {
            "provider": provider, "ok": False, "error": "CLI not installed",
            "reasoning_effort": requested_effort,
        }
    prompt_path = run_dir / ("prompt_" + label + "_" + provider + ".txt")
    stdout_path = run_dir / ("stdout_" + label + "_" + provider + ".txt")
    stderr_path = run_dir / ("stderr_" + label + "_" + provider + ".txt")
    prompt_path.write_text(prompt)
    review_label = label.startswith(("review", "critique")) or "_critique" in label
    schema = schema or (review_schema() if review_label else plan_schema(False))
    schema_path = run_dir / ("schema_" + label + "_" + provider + ".json")
    write_json(schema_path, schema)
    if provider != "codex":
        return {
            "provider": provider, "ok": False, "error": "unsupported provider lane",
            "reasoning_effort": requested_effort,
        }
    args = [
        executable,
        "exec",
        "-c",
        "model=" + CODEX_LUNA_MODEL,
        "-c",
        "model_reasoning_effort=" + requested_effort,
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--output-schema",
        str(schema_path),
        "-o",
        str(stdout_path),
        "-",
    ]
    started = time.time()
    stdout_path.touch()
    proc: Optional[subprocess.Popen] = None
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
            register_provider_process(proc)
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
                        "reasoning_effort": requested_effort,
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
                "reasoning_effort": requested_effort,
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
                "reasoning_effort": requested_effort,
                "error": "timed out after %ss" % timeout,
                "elapsed_seconds": round(time.time() - started, 1),
                "usage_tokens": provider_usage_tokens(stderr_path),
                "stderr_path": str(stderr_path),
            }
        if proc.returncode != 0:
            return {
                "provider": provider,
                "ok": False,
                "reasoning_effort": requested_effort,
                "error": "CLI exited with code %s" % proc.returncode,
                "elapsed_seconds": round(time.time() - started, 1),
                "usage_tokens": provider_usage_tokens(stderr_path),
                "stderr_path": str(stderr_path),
            }
        data = response_data(stdout_path.read_text() if stdout_path.exists() else "")
        return {
            "provider": provider,
            "ok": useful_provider_data(data, label),
            "reasoning_effort": requested_effort,
            "elapsed_seconds": round(time.time() - started, 1),
            "data": data,
            "usage_tokens": provider_usage_tokens(stderr_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    except OSError as exc:
        return {
            "provider": provider, "ok": False, "error": str(exc),
            "reasoning_effort": requested_effort,
        }
    finally:
        if proc is not None:
            unregister_provider_process(proc)


def run_sealed_evaluator(
    packet: Dict[str, Any],
    run_dir: Path,
    label: str,
    timeout: int = RUN_TIMEOUT_SECONDS,
    evaluator_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one immutable evaluator packet in a fresh, critique-only process.

    This deliberately does not call ``run_provider``.  The writer and the
    evaluator therefore have separate subprocesses, prompts, schemas, and
    working directories.  A result is usable only when the child proves the
    frozen contract, role, and packet hash that the parent supplied.
    """
    role = str(packet.get("role") or "")
    evaluator_root = run_dir / "sealed_evaluator"
    packet_path = evaluator_root / (label + "_input.json")
    output_path = evaluator_root / (label + "_result.json")
    stdout_path = evaluator_root / (label + "_launcher.stdout.txt")
    stderr_path = evaluator_root / (label + "_launcher.stderr.txt")
    codex_stdout_path = evaluator_root / (label + "_codex.stdout.json")
    codex_stderr_path = evaluator_root / (label + "_codex.stderr.txt")
    evaluator_root.mkdir(parents=True, exist_ok=True)
    write_json(packet_path, packet)
    # The child receives a disposable copy in a fresh system-temp directory.
    # Keeping its cwd and packet away from the writer run directory prevents a
    # prompt-following evaluator from discovering drafts, prior critiques, or
    # repair artifacts by filesystem traversal.
    isolated_root = Path(tempfile.mkdtemp(prefix="resume-evaluator-"))
    isolated_packet = isolated_root / "packet.json"
    isolated_output = isolated_root / "result.json"
    isolated_scratch = isolated_root / "codex"
    isolated_scratch.mkdir(parents=True, exist_ok=True)
    isolated_packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
    args = [
        sys.executable,
        str(SCRIPT_REPO_ROOT / "scripts" / "resume_evaluator.py"),
        "--packet", str(isolated_packet),
        "--output", str(isolated_output),
        "--scratch", str(isolated_scratch),
        "--timeout", str(max(30, int(timeout))),
    ]
    requested_effort = str(evaluator_effort or resume_evaluator.CODEX_EFFORT).strip().lower()
    if requested_effort not in getattr(resume_evaluator, "CODEX_EFFORTS", {resume_evaluator.CODEX_EFFORT}):
        requested_effort = resume_evaluator.CODEX_EFFORT
    args.extend(["--effort", requested_effort])
    started = time.time()
    proc = None
    try:
        with stdout_path.open("w") as out, stderr_path.open("w") as err:
            proc = subprocess.Popen(
                args,
                cwd=str(isolated_scratch),
                env=subscription_environment(run_dir),
                stdout=out,
                stderr=err,
                text=True,
                start_new_session=(os.name == "posix"),
            )
            register_provider_process(proc)
            timed_out = False
            while proc.poll() is None:
                if time.time() - started >= timeout + 30:
                    timed_out = True
                    stop_provider_process(proc)
                    break
                time.sleep(0.25)
        result = read_json(isolated_output, {}) or {}
        if not isinstance(result, dict):
            result = {}
        child_stdout = isolated_scratch / "codex.stdout.json"
        child_stderr = isolated_scratch / "codex.stderr.txt"
        if child_stdout.exists():
            shutil.copyfile(child_stdout, codex_stdout_path)
        if child_stderr.exists():
            shutil.copyfile(child_stderr, codex_stderr_path)
        result["stdout_path"] = str(codex_stdout_path)
        result["stderr_path"] = str(codex_stderr_path)
        result.setdefault("provider", "codex")
        result.setdefault("execution_lane", "sealed_evaluator")
        result.setdefault("role", role)
        result.setdefault("reasoning_effort", requested_effort)
        result.setdefault("elapsed_seconds", round(time.time() - started, 1))
        result.setdefault("stdout_path", str(stdout_path))
        result.setdefault("stderr_path", str(stderr_path))
        if timed_out:
            result.update({
                "ok": False,
                "error": "sealed evaluator launcher timed out after %ss" % (timeout + 30),
            })
        elif result.get("ok"):
            mismatches = []
            if result.get("contract_version") != SEALED_EVALUATOR_CONTRACT:
                mismatches.append("contract version")
            if result.get("role") != role:
                mismatches.append("role")
            if result.get("input_sha256") != packet.get("input_sha256"):
                mismatches.append("input hash")
            if result.get("execution_lane") != "sealed_evaluator":
                mismatches.append("execution lane")
            if result.get("contract_fingerprint") != resume_evaluator.contract_fingerprint():
                mismatches.append("contract fingerprint")
            if result.get("rubric_sha256") != resume_evaluator.EVALUATOR_RUBRIC_SHA256:
                mismatches.append("rubric hash")
            if mismatches:
                result.update({
                    "ok": False,
                    "error": "sealed evaluator attestation mismatch: %s" % ", ".join(mismatches),
                })
        if not result:
            result = {
                "provider": "codex", "execution_lane": "sealed_evaluator", "role": role,
                "ok": False, "error": "sealed evaluator returned no result",
            }
        write_json(output_path, result)
        return result
    except (OSError, ValueError, TypeError) as exc:
        if proc is not None:
            stop_provider_process(proc)
        return {
            "provider": "codex", "execution_lane": "sealed_evaluator", "role": role,
            "ok": False, "reasoning_effort": requested_effort, "error": str(exc),
            "elapsed_seconds": round(time.time() - started, 1),
            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
        }
    finally:
        if proc is not None:
            unregister_provider_process(proc)
        shutil.rmtree(isolated_root, ignore_errors=True)


def sealed_evaluator_packet(
    *,
    role: str,
    context: Dict[str, Any],
    base_tex: str,
    tailored_tex: str,
    plan: Dict[str, Any],
    graph_context: List[Dict[str, Any]],
    catalog: Dict[str, Any],
    deterministic: Dict[str, Any],
    changes: Dict[str, Any],
    run_id: str,
) -> Dict[str, Any]:
    """Build the only input shape the sealed evaluator is allowed to read."""
    portfolio_snapshot = portfolio_metrics(plan) if isinstance(plan, dict) else {}
    selected_plan = {}
    for section in ("experiences", "projects", "leadership"):
        selected_plan[section] = []
        for entry in plan.get(section, []):
            selected_plan[section].append({
                "source_id": entry.get("source_id"),
                "bullets": [
                    {
                        key: bullet.get(key)
                        for key in ("source_id", "source_ids", "evidence_ids", "text")
                        if key in bullet
                    }
                    for bullet in entry.get("bullets", [])
                ],
            })
    deterministic_snapshot = {
        "rubric_version": deterministic.get("rubric_version"),
        "hard_fail": deterministic.get("hard_fail"),
        "warnings": list(deterministic.get("warnings") or [])[:40],
        "gates": copy.deepcopy(deterministic.get("gates") or {}),
        "layout": {
            key: (deterministic.get("layout") or {}).get(key)
            for key in ("compiled", "pages", "overfull", "density_gap_pt", "horizontal", "vertical_capacity")
            if key in (deterministic.get("layout") or {})
        },
        "human_skim_budget": copy.deepcopy(portfolio_snapshot.get("human_skim_budget") or {}),
        "content_bullet_count": int(portfolio_snapshot.get("total_bullets") or 0),
        "style": copy.deepcopy(deterministic.get("style") or {}),
    }
    comparison_snapshot = {
        "keyword_coverage": copy.deepcopy(changes.get("keyword_coverage") or {}),
        "portfolio_diagnostics": copy.deepcopy(changes.get("portfolio_diagnostics") or {}),
        "removed_canonical_bullets": copy.deepcopy(changes.get("removed_canonical_bullets") or [])[:30],
        "unexplained_removed_bullets": copy.deepcopy(changes.get("unexplained_removed_bullets") or [])[:30],
        "rewritten_bullets": copy.deepcopy(changes.get("rewritten_bullets") or [])[:30],
        "added_bullets": copy.deepcopy(changes.get("added_bullets") or [])[:30],
        "selected_plan": selected_plan,
    }
    evidence_snapshot = {
        "graph_context": copy.deepcopy(graph_context or [])[:240],
        "catalog": catalog_for_prompt(catalog or {}),
    }
    return resume_evaluator.make_packet(
        role=role,
        job=context,
        base_text=_latex_plain(base_tex),
        tailored_text=_latex_plain(tailored_tex),
        evidence_snapshot=evidence_snapshot,
        deterministic_snapshot=deterministic_snapshot,
        comparison_snapshot=comparison_snapshot,
        run_id=run_id,
    )


def _company_claim(record: Dict[str, Any], field: str) -> Dict[str, Any]:
    """Return one bounded, source-labeled company claim for prompt routing."""
    raw = record.get(field)
    if isinstance(raw, dict):
        value = str(raw.get("value") or "Not confirmed").strip()
        confidence = str(raw.get("confidence") or "unknown").strip().lower()
        source_ids = [str(item) for item in raw.get("source_ids") or [] if str(item)]
    else:
        value = str(raw or "Not confirmed").strip()
        confidence = "unknown"
        source_ids = []
    return {
        "value": value[:700] or "Not confirmed",
        "confidence": confidence if confidence in {"high", "medium", "low", "estimated", "unknown"} else "unknown",
        "source_ids": list(dict.fromkeys(source_ids))[:5],
    }


def _company_domain_scores(text: str) -> Dict[str, int]:
    lowered = clean_text(text).lower()
    scores: Dict[str, int] = {}
    for domain, terms in COMPANY_DOMAIN_PATTERNS.items():
        hits = sum(1 for term in terms if _role_signal_present(lowered, term))
        if hits:
            scores[domain] = hits
    return scores


def company_tailoring_context(job: Dict[str, Any], root: Optional[Path] = None) -> Dict[str, Any]:
    """Build a safe company/domain routing receipt from committed research.

    Company dossiers contain estimates and model-synthesized prose.  The
    tailor may use them to choose which authorized work to foreground, but the
    returned contract makes the provenance and confidence visible and tells
    downstream writers that these are not candidate accomplishments.
    """
    company = clean_text(str(job.get("company") or ""))
    sector = clean_text(str(job.get("sector") or ""))
    base = (root or repo_root()).resolve()
    records = read_json(base / "state" / "company_research.json", {}) or {}
    record = dossier_for(company, records) if company and isinstance(records, dict) else None
    dossier_text_parts = [company, sector]
    claims: Dict[str, Dict[str, Any]] = {}
    if isinstance(record, dict):
        for field in COMPANY_CONTEXT_FIELDS:
            claim = _company_claim(record, field)
            claims[field] = claim
            dossier_text_parts.append(claim["value"])
    domain_scores = _company_domain_scores(" ".join(dossier_text_parts))
    ranked_domains = sorted(domain_scores, key=lambda item: (-domain_scores[item], item))
    primary_domain = ranked_domains[0] if ranked_domains else "technology"
    if primary_domain == "healthcare":
        domain_rule = (
            "When role fit is comparable, prefer verified healthcare/medical evidence (for example drug-safety, "
            "clinical, biomedical, privacy, wearable, or patient-facing work) over a generic project. The domain "
            "is a portfolio-routing preference, not permission to claim healthcare outcomes that are not sourced."
        )
    elif ranked_domains:
        domain_rule = (
            "Use the company domain to break portfolio ties only after the posting's primary role requirements, "
            "evidence strength, and distinctiveness are satisfied. Do not force a domain keyword into an unrelated "
            "bullet."
        )
    else:
        domain_rule = (
            "No reliable company domain signal was found. Use the posting and authorized evidence as the primary "
            "routing inputs; do not infer a sector-specific story."
        )
    return {
        "version": TAILORING_BRIEF_VERSION,
        "company": company,
        "sector_hint": sector,
        "dossier_available": bool(record),
        "dossier_status": str((record or {}).get("status") or "not_found"),
        "company_domain": primary_domain,
        "domain_scores": domain_scores,
        "domain_priorities": ranked_domains[:3],
        "domain_rule": domain_rule,
        "claims": claims,
        "source_ids": list(dict.fromkeys(
            source_id
            for claim in claims.values()
            for source_id in claim.get("source_ids") or []
        ))[:16],
        "safety": (
            "Company claims are routing context only. Never copy them into the resume, and never present an "
            "estimated or uncited dossier value as Victor's experience."
        ),
    }


def _project_surface_reason(
    entry: Dict[str, Any], primary_track: str, domain: str,
    target_terms: Iterable[str],
) -> Tuple[float, List[str], List[str]]:
    """Rank an authorized entry as an ideal project/evidence surface."""
    text = " ".join(
        [str(entry.get("heading") or ""), str(entry.get("company") or ""), str(entry.get("role") or "")]
        + [str(item.get("text") or "") for item in entry.get("bullets") or []]
    ).lower()
    role_terms = ROLE_FLOOR_TERMS.get(primary_track, ())
    role_hits = [term for term in role_terms if _role_signal_present(text, term)]
    domain_terms = COMPANY_DOMAIN_PROJECT_TERMS.get(domain, ())
    domain_hits = [term for term in domain_terms if _role_signal_present(text, term)]
    target_hits = [term for term in target_terms if _role_signal_present(text, str(term).lower())]
    score = len(role_hits) * 5.0 + len(domain_hits) * 7.0 + len(target_hits) * 3.0
    if entry.get("kind") == "experience":
        score += 1.0
    return score, list(dict.fromkeys(role_hits + target_hits))[:8], list(dict.fromkeys(domain_hits))[:8]


def build_tailoring_brief(
    job: Dict[str, Any], posting_text: str, company_context: Dict[str, Any],
    target_keywords: Dict[str, Any], catalog: Dict[str, Any],
    graph: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create the explicit role → ATS → evidence → portfolio decision receipt."""
    text = clean_text(str(posting_text or ""))
    role_focus = role_track_profile(job, text)
    primary_track = str(role_focus.get("primary_track") or "general_software")
    terms = [
        item for item in target_keywords.get("terms") or []
        if isinstance(item, dict) and str(item.get("term") or "").strip()
    ]
    essential = [
        {
            "term": str(item.get("term") or ""),
            "importance": str(item.get("importance") or "mentioned"),
            "supported": bool(item.get("supported")),
            "source_ids": list(item.get("source_ids") or [])[:6],
        }
        for item in terms
        if str(item.get("importance") or "").lower() in {"required", "responsibility"}
    ][:20]
    ats_terms = [
        {
            "term": str(item.get("term") or ""),
            "importance": str(item.get("importance") or "mentioned"),
            "supported": bool(item.get("supported")),
            "support_kind": str(item.get("support_kind") or "none"),
            "source_ids": list(item.get("source_ids") or [])[:6],
        }
        for item in terms
    ][:32]
    domain = str(company_context.get("company_domain") or "technology")
    target_terms = [str(item.get("term") or "") for item in terms]
    surfaces = []
    for entry in (catalog.get("entries") or {}).values():
        if not isinstance(entry, dict) or entry.get("kind") not in {"project", "experience", "leadership"}:
            continue
        score, role_hits, domain_hits = _project_surface_reason(
            entry, primary_track, domain, target_terms,
        )
        if score <= 0:
            continue
        label = str(entry.get("heading") or entry.get("company") or entry.get("id") or "")
        surfaces.append({
            "entry_id": str(entry.get("id") or ""),
            "kind": str(entry.get("kind") or ""),
            "label": label[:180],
            "score": round(score, 2),
            "role_signals": role_hits,
            "domain_signals": domain_hits,
            "why": (
                "Adds %s evidence%s. Verify the cited bullets before selecting or rewriting."
                % (
                    ", ".join(role_hits[:4]) or "role-relevant",
                    " plus " + ", ".join(domain_hits[:3]) + " domain context" if domain_hits else "",
                )
            ),
        })
    surfaces.sort(key=lambda item: (-item["score"], item["label"].lower()))
    unsupported = [
        str(item.get("term") or "")
        for item in terms if not item.get("supported")
    ]
    return {
        "version": TAILORING_BRIEF_VERSION,
        "role_focus": role_focus,
        "primary_role_track": primary_track,
        "essential_capabilities": essential,
        "ats_terms": ats_terms,
        "honest_gaps": list(dict.fromkeys(unsupported))[:24],
        "company_context": copy.deepcopy(company_context),
        "company_domain": domain,
        "domain_priorities": list(company_context.get("domain_priorities") or [])[:3],
        "ideal_project_surfaces": [item for item in surfaces if item["kind"] == "project"][:8],
        "ideal_evidence_surfaces": surfaces[:12],
        "selection_rule": (
            "First identify what success requires in this role; then cover exact posting language where authorized; "
            "then compare the current portfolio with these evidence surfaces. Swap or rewrite only when the new line "
            "adds a distinct, defensible interview thread and the decision ledger records the tradeoff."
        ),
        "evidence_graph_available": bool((graph or {}).get("nodes")),
    }


def job_context(job: Dict[str, Any], root: Optional[Path] = None) -> Dict[str, Any]:
    context = dict(job)
    context["posting_text"] = fetch_job_description(job)
    context["company_context"] = company_tailoring_context(job, root or repo_root())
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


def _keyword_affirmed(term: str, text: str) -> bool:
    """Reject claim-boundary mentions such as 'do not claim Agile'."""
    lowered = str(text or "").lower()
    match = re.search(_keyword_pattern(term), lowered)
    if not match:
        return False
    window = lowered[max(0, match.start() - 90): min(len(lowered), match.end() + 90)]
    denied = re.search(
        r"\b(?:do not|does not|did not|never|not|without|unsupported|cannot|can't)\b.{0,70}"
        + _keyword_pattern(term)
        + r"|"
        + _keyword_pattern(term)
        + r"(?:(?!\b(?:and|but|while|although)\b)[^.!?;,]){0,70}\b(?:unsupported|not authorized|not supported|without confirmation|without additional confirmation|absent|missing)\b",
        window,
        re.I,
    )
    return not bool(denied)


def target_keyword_strategy(
    context: Dict[str, Any], catalog: Dict[str, Any], root: Optional[Path] = None,
    graph: Optional[Dict[str, Any]] = None,
    comprehensive: bool = False,
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

    # The generation mode is specifically allowed to retrieve buried facts
    # from Victor's reviewed Markdown corpus. Exact support therefore cannot
    # be limited to wording that already made a LaTeX resume. Public
    # corroboration and rejected/private-only records remain non-authorizing.
    for node in (graph or {}).get("nodes", []) if comprehensive else []:
        if not node.get("claim_allowed"):
            continue
        node_id = str(node.get("id") or "")
        text = " ".join(
            str(node.get(field) or "") for field in ("heading", "text")
        )
        if node_id and text.strip():
            source_texts.append((node_id, _latex_plain(text)))

    sentences = [part.strip() for part in re.split(r"[\n.!?;]+", posting) if part.strip()]
    terms: List[Dict[str, Any]] = []
    vocabulary = TARGET_KEYWORD_TERMS if comprehensive else LEGACY_TARGET_KEYWORD_TERMS
    found_terms = [term for term in vocabulary if _keyword_present(term, posting)]
    for term in sorted(found_terms, key=lambda value: posting.lower().find(value.replace("-", " "))):
        if not _keyword_present(term, posting):
            continue
        matching_sources = list(dict.fromkeys(
            source_id for source_id, text in source_texts if _keyword_affirmed(term, text)
        ))
        preferred = any(
            _keyword_present(term, sentence)
            and re.search(r"\b(preferred|nice to have|bonus|ideally)\b", sentence, re.I)
            for sentence in sentences
        )
        required = not preferred and any(
            _keyword_present(term, sentence)
            and re.search(r"\b(required|must|minimum|required experience|required skills)\b", sentence, re.I)
            for sentence in sentences
        )
        responsibility = not required and not preferred and any(
            _keyword_present(term, sentence)
            and re.search(r"\b(responsibilit|you will|contribute|support|participate|collaborate|design|develop|maintain)\w*\b", sentence, re.I)
            for sentence in sentences
        )
        importance = "required" if required else "preferred" if preferred else "responsibility" if responsibility else "mentioned"
        terms.append({
            "term": term,
            "required": bool(required),
            "preferred": bool(preferred),
            "responsibility": bool(responsibility),
            "importance": importance,
            "supported": bool(matching_sources),
            "support_kind": "exact" if matching_sources else "none",
            "source_ids": matching_sources[:6],
        })
        if len(terms) >= MAX_TARGET_KEYWORDS:
            break
    required_terms = [item["term"] for item in terms if item["required"]]
    preferred_terms = [item["term"] for item in terms if item["preferred"]]
    return {
        "posting_available": True,
        "reason": (
            "Exact terms extracted from the captured posting and checked against authorized resume and Markdown evidence."
            if comprehensive else
            "Exact terms extracted from the captured posting and checked against the authorized CV corpus."
        ),
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


def gap_analysis_prompt(
    context: Dict[str, Any], catalog: Dict[str, Any], graph: Dict[str, Any],
) -> str:
    """Ask for the human-style role → evidence pass used by enhanced modes."""
    focused_context = {
        "company": context.get("company"),
        "title": context.get("title"),
        "sector": context.get("sector"),
        "posting_text": context.get("posting_text"),
        "company_context": context.get("company_context") or {},
        "tailoring_brief": context.get("tailoring_brief") or {},
        "role_focus": role_track_profile(
            context, str(context.get("posting_text") or "")
        ),
    }
    return (
        "Return the requested JSON strategy now; do not narrate progress, promise future work, or "
        "treat these instructions as job requirements. You are the requirement-to-evidence planner "
        "for Victor Jimenez's private resume studio, and this pass runs for ordinary enhanced tailoring "
        "as well as the unchained generation lane. "
        "Read the complete posting, then account for every material qualification, responsibility, "
        "named technology, workflow, domain signal, and collaboration expectation. For each one, "
        "decide whether the authorized evidence is direct, adjacent-but-defensible, or unsupported. "
        "Search beyond existing resume bullets: reviewed Markdown evidence is specifically provided "
        "so buried work can become a new source-grounded bullet or tailored skill line. Recommend "
        "synthesis only when it makes a real requirement visible. Do not manufacture AWS, Azure, "
        "Databricks, Power Platform, Agile, code reviews, deployment, metrics, or any other missing "
        "claim. Exact ATS wording is useful only when the cited evidence genuinely supports it. "
        "Treat the resume as one information budget: stronger gap-filling evidence may replace a "
        "redundant bullet, project, coursework, or awards. Preserve chronology and factual qualifiers. "
        "Use the supplied role_focus as a routing aid: identify the primary role track first, then "
        "rank supported evidence for that track above adjacent requirements. If the posting is genuinely "
        "broad or ambiguous, say so in the strategy instead of trying to satisfy every subrole equally. "
        "Use company_context only to prioritize a domain-relevant portfolio when it strengthens the role case: "
        "for a medical, healthcare, pharma, biotech, or biomedical company, explicitly compare verified medical "
        "assignments against generic projects and prefer the medical evidence when it adds equal-or-better role "
        "proof. Company research is routing context, not a source for Victor's accomplishments; never turn it "
        "into a resume claim. Use tailoring_brief.ideal_project_surfaces as hypotheses to verify against the "
        "catalog, not as automatic selections. "
        "Every requirements[].exact_terms value must come verbatim from the supplied Exact ATS "
        "inventory. Do not create requirements about output format, chronology, evidence review, "
        "or the analysis process. Include a term in must_cover_terms only when direct or adjacent "
        "authorized evidence supports it; unsupported terms belong only in honest_gaps. Return the "
        "auditable strategy requested by the schema, not resume copy or LaTeX.\n\n"
        "Job context:\n"
        + json.dumps(focused_context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nExact ATS inventory:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
        + "\n\nSource-addressable resume catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nTarget-ranked authorized evidence, including Markdown:\n"
        + json.dumps(
            evidence_context(graph, context, str(context.get("posting_text") or "")),
            indent=2, ensure_ascii=False,
        )[:MAX_GRAPH_PROMPT_CHARS]
    )


def normalize_gap_analysis(
    data: Dict[str, Any], keyword_strategy: Dict[str, Any],
    catalog: Dict[str, Any], graph: Dict[str, Any], posting_text: str,
) -> Dict[str, Any]:
    """Ground a provider gap plan and guarantee coverage of the ATS inventory."""
    node_by_id = {
        str(node.get("id") or ""): node
        for node in graph.get("nodes", [])
        if str(node.get("id") or "")
    }
    claim_ids = {
        node_id for node_id, node in node_by_id.items() if node.get("claim_allowed")
    }
    entry_ids = set((catalog.get("entries") or {}).keys())
    posting = str(posting_text or "")
    allowed_terms = {
        str(item.get("term") or "").lower()
        for item in (keyword_strategy.get("terms") or [])
        if str(item.get("term") or "").strip()
    }
    normalized: List[Dict[str, Any]] = []
    represented_terms = set()
    for raw in (data.get("requirements") or [])[:48]:
        if not isinstance(raw, dict):
            continue
        requirement = str(raw.get("requirement") or "").strip()[:240]
        if not requirement:
            continue
        importance = str(raw.get("importance") or "mentioned").lower()
        if importance not in {"required", "preferred", "responsibility", "mentioned"}:
            importance = "mentioned"
        exact_terms = []
        evidence_explanation = " ".join((
            str(raw.get("candidate_angle") or ""),
            str(raw.get("reason") or ""),
        ))
        evidence_text = " ".join(
            " ".join((
                str(node_by_id[node_id].get("heading") or ""),
                str(node_by_id[node_id].get("text") or ""),
            ))
            for node_id in (raw.get("evidence_ids") or [])
            if str(node_id) in node_by_id
        )
        for value in raw.get("exact_terms") or []:
            term = str(value or "").strip().lower()[:80]
            explicitly_denied = (
                _keyword_present(term, evidence_explanation)
                and not _keyword_affirmed(term, evidence_explanation)
            )
            if (
                term in allowed_terms and _keyword_present(term, posting)
                and not explicitly_denied
                and (
                    _keyword_present(term, requirement)
                    or _keyword_present(term, evidence_explanation)
                    or _keyword_present(term, evidence_text)
                )
                and term not in exact_terms
            ):
                exact_terms.append(term)
                represented_terms.add(term)
        if not exact_terms:
            continue
        evidence_ids = list(dict.fromkeys(
            str(value) for value in (raw.get("evidence_ids") or [])
            if str(value) in claim_ids
        ))[:8]
        status = str(raw.get("evidence_status") or "unsupported").lower()
        if not evidence_ids:
            status = "unsupported"
        elif status not in {"direct", "adjacent"}:
            status = "adjacent"
        if status == "direct" and exact_terms:
            exact_supported = any(
                any(_keyword_present(term, " ".join((str(node_by_id[node_id].get("heading") or ""), str(node_by_id[node_id].get("text") or "")))) for term in exact_terms)
                for node_id in evidence_ids
            )
            if not exact_supported:
                status = "adjacent"
        action = str(raw.get("recommended_action") or "leave_gap").lower()
        if action not in {"keep", "reorder", "rewrite", "synthesize", "tailor_skills", "leave_gap"}:
            action = "leave_gap"
        if status == "unsupported":
            action = "leave_gap"
        target_entry = str(raw.get("target_entry_id") or "")
        if target_entry not in entry_ids:
            target_entry = ""
        normalized.append({
            "requirement": requirement,
            "importance": importance,
            "exact_terms": exact_terms,
            "evidence_status": status,
            "evidence_ids": evidence_ids,
            "target_entry_id": target_entry,
            "recommended_action": action,
            "candidate_angle": str(raw.get("candidate_angle") or "").strip()[:500],
            "reason": str(raw.get("reason") or "").strip()[:500],
        })

    # A model may group several requirements or simply overlook one. Preserve
    # the complete deterministic inventory so the report never hides that gap.
    for term_item in keyword_strategy.get("terms") or []:
        term = str(term_item.get("term") or "").lower()
        if not term or term in represented_terms:
            continue
        source_ids = [
            str(value) for value in (term_item.get("source_ids") or [])
            if str(value) in claim_ids
        ][:8]
        supported = bool(source_ids)
        normalized.append({
            "requirement": term,
            "importance": str(term_item.get("importance") or "mentioned"),
            "exact_terms": [term],
            "evidence_status": "direct" if supported else "unsupported",
            "evidence_ids": source_ids,
            "target_entry_id": "",
            "recommended_action": "rewrite" if supported else "leave_gap",
            "candidate_angle": "Use the exact term naturally where the cited evidence earns space." if supported else "",
            "reason": "Deterministic ATS inventory item retained because the planning lane did not address it.",
        })

    supported_terms = {
        str(term).lower()
        for item in normalized
        if item.get("evidence_status") in {"direct", "adjacent"}
        for term in (item.get("exact_terms") or [])
    }
    # Models sometimes group one real gap with generic supported technologies
    # (for example, an unsupported bioinformatics requirement tagged Python).
    # A term is an honest gap only when no grounded requirement supports it.
    cleaned = []
    for item in normalized:
        if item.get("evidence_status") == "unsupported":
            item = copy.deepcopy(item)
            item["exact_terms"] = [
                term for term in (item.get("exact_terms") or [])
                if str(term).lower() not in supported_terms
            ]
            if not item["exact_terms"]:
                continue
        cleaned.append(item)
    normalized = cleaned
    must_cover = []
    gaps = []
    for item in normalized:
        terms = item.get("exact_terms") or [item.get("requirement")]
        if item.get("evidence_status") in {"direct", "adjacent"} and item.get("importance") != "mentioned":
            must_cover.extend(str(term) for term in terms if str(term))
        elif item.get("evidence_status") == "unsupported":
            gaps.extend(str(term) for term in terms if str(term))
    unsupported_terms = allowed_terms - supported_terms
    must_cover.extend(
        str(value).lower() for value in (data.get("must_cover_terms") or [])
        if str(value).lower() in supported_terms
    )
    gaps.extend(
        str(value).lower() for value in (data.get("honest_gaps") or [])
        if str(value).lower() in unsupported_terms
    )
    return {
        "portfolio_strategy": str(data.get("portfolio_strategy") or "").strip()[:1200]
        or "Use direct and adjacent authorized evidence to close material posting gaps; leave unsupported requirements explicit.",
        "requirements": normalized[:48],
        "must_cover_terms": list(dict.fromkeys(must_cover))[:32],
        "honest_gaps": list(dict.fromkeys(gaps))[:24],
    }


def apply_gap_support_to_keywords(
    keyword_strategy: Dict[str, Any], gap_strategy: Dict[str, Any],
) -> Dict[str, Any]:
    """Promote provider-audited adjacent evidence without hiding its status."""
    value = copy.deepcopy(keyword_strategy)
    by_term: Dict[str, Dict[str, Any]] = {}
    for requirement in gap_strategy.get("requirements") or []:
        if requirement.get("evidence_status") not in {"direct", "adjacent"}:
            continue
        for term in requirement.get("exact_terms") or []:
            by_term[str(term).lower()] = requirement
    for item in value.get("terms") or []:
        requirement = by_term.get(str(item.get("term") or "").lower())
        if not requirement:
            continue
        evidence_ids = list(requirement.get("evidence_ids") or [])
        if not evidence_ids:
            continue
        item["supported"] = True
        item["support_kind"] = str(requirement.get("evidence_status") or "adjacent")
        item["source_ids"] = list(dict.fromkeys(
            list(item.get("source_ids") or []) + evidence_ids
        ))[:8]
    value["reason"] = (
        "Exact posting terms checked against authorized resume/Markdown evidence; "
        "generation-mode adjacent support is labeled separately from exact-source support."
    )
    return value


def base_prompt(
    context: Dict[str, Any],
    role: str,
    catalog: Dict[str, Any],
    enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
    unrestricted: bool = False,
    generation: bool = False,
    variant_instruction: str = "",
) -> str:
    role_guardrails = """
Victor-specific guardrails:
- The master CV is a responsibility/evidence bank, not a keyword dump.
- Never invent metrics, users, adoption, production status, scope, accuracy,
  dates, technologies, or business outcomes.
- Every selected claim must be traceable to CV/ source material.
- Do not merge a mechanism, qualifier, metric, or causal relationship from
  separate source bullets into one stronger-sounding claim unless the supplied
  sources explicitly connect those facts. When sources authorize adjacent
  facts but not their relationship, keep the boundary visible or use separate
  bullets; citation count is not proof of semantic linkage.
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
- Begin with the role-and-company tailoring brief: identify the essential
  capabilities needed to succeed and pass the posting, separate exact ATS
  wording from broader skills, and compare the current portfolio with the
  ideal evidence surfaces before editing bullets. Company domain is a tie-break
  and prioritization input, not a replacement for role fit. For a medical,
  healthcare, pharma, biotech, or biomedical employer, prefer verified medical
  assignments when they provide equal-or-better technical proof; do not force
  healthcare language into unrelated work.
- Company research is routing context only. It may contain estimates or
  synthesized employer prose and is never evidence for Victor's resume. Do
  not copy company products, customers, mission, or metrics into a candidate
  bullet. Record meaningful portfolio swaps, rewrites, and omissions in the
  decision ledger, including the important signal lost.
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
- Human skim budget for this tailored candidate: target at most 25 rendered
  bullets, four project headings, 11 project bullets, and five bullets per
  experience entry. Do not use spare LaTeX space to exceed that budget. If a
  new project is added, first prove that it earns a slot over the canonical
  project evidence it displaces; preserve distinctive mechanisms, validation,
  ownership, and external proof over a fifth adjacent project. Treat every
  project heading as a real skim cost.
- Choose the portfolio before polishing individual lines. Compare the
  whole-resume signal: technical breadth, project differentiation, external
  validation, and the number of distinct interview stories. A good bullet can
  still be the wrong portfolio choice if it repeats a stronger experience
  story or displaces a better project.
- Use the deterministic job_intelligence.role_focus as a routing receipt. The
  primary role track is the center of gravity; secondary tracks may add
  supported evidence, but a generic adjacent keyword must not displace a
  stronger primary-track mechanism, metric, validation result, or ownership
  proof without an explicit decision-ledger tradeoff. When confidence is
  ambiguous, preserve breadth and explain the ambiguity rather than pretending
  the posting is one narrow role.
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
- Coursework and the aggregated Awards skill line are flexible reserves. Keep
  them by default, but when page space is genuinely needed, reclaim coursework
  first and Awards second. Treat quantified external-selection proof (including
  the HackMIT acceptance line) as substantive evidence, not an automatic
  reserve: preserve it unless a clearly stronger distinct replacement is
  already present and the tradeoff is explicit.
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
- Treat the high-information canonical lines listed below as control evidence.
  Do not remove a quantified metric, validation result, integration mechanism,
  or communication/ownership proof merely to make a project or keyword look
  more tailored. A replacement must add a clearly distinct, target-relevant
  proof point; if it does not, preserve the control line and explain the choice.
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
        if generation:
            role_guardrails += (
                "\n- Unchained generation mode begins from the supplied requirement-to-evidence strategy. "
                "For each material direct or adjacent opportunity, decide whether to keep, reorder, rewrite, "
                "synthesize, or tailor a Skills line so the final resume visibly answers the posting. You may "
                "create a genuinely new bullet by combining an existing catalog bullet as its primary source_id "
                "with claim-authorizing Markdown evidence_ids. Replace redundant evidence when needed. Return "
                "front_matter_rewrites only for evidence-backed Skills changes, and leave unsupported requirements "
                "as explicit gaps instead of inventing them."
            )
    else:
        role_guardrails += (
            "\n- Source-only mode is selection, not rewriting. Choose source IDs only; the harness will copy every heading and bullet verbatim."
        )
    role_guardrails += "\n- TICC is permanently excluded from every resume; never select, rewrite, or mention that activity, even for a TLDP target."
    # The generation strategy is included in its own bounded section below.
    # Omitting the duplicate copy from the broad context block materially
    # reduces Unchained author latency after a successful gap-analysis pass.
    prompt_context = copy.deepcopy(context)
    if enhance:
        prompt_context.pop("generation_strategy", None)
    context_text = json.dumps(prompt_context, indent=2, ensure_ascii=False)
    catalog_text = json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)
    graph_text = json.dumps(
        evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [],
        indent=2,
        ensure_ascii=False,
    )
    benchmark_text = json.dumps(canonical_resume_benchmark(catalog), indent=2, ensure_ascii=False)
    front_matter_text = json.dumps(front_matter_catalog(repo_root()), indent=2, ensure_ascii=False)
    generation_text = json.dumps(
        context.get("generation_strategy") or {}, indent=2, ensure_ascii=False,
    )
    generation_strategy = context.get("generation_strategy") or {}
    supported_skills_checklist = [
        {
            "requirement": str(item.get("requirement") or "")[:240],
            "exact_terms": list(item.get("exact_terms") or [])[:8],
            "evidence_status": str(item.get("evidence_status") or ""),
            "evidence_ids": list(item.get("evidence_ids") or [])[:8],
            "recommended_action": str(item.get("recommended_action") or ""),
        }
        for item in (generation_strategy.get("requirements") or [])
        if isinstance(item, dict)
        and item.get("evidence_status") in {"direct", "adjacent"}
        and item.get("recommended_action") == "tailor_skills"
    ][:8]
    skills_checklist_text = json.dumps(
        supported_skills_checklist, indent=2, ensure_ascii=False,
    )
    control_text = canonical_control_prompt(catalog, context.get("target_keywords"))
    variant_text = str(variant_instruction or "").strip()
    if variant_text:
        role_guardrails += (
            "\n\nIndependent portfolio-search hypothesis (one candidate among several):\n"
            + variant_text[:2400]
            + "\nThis hypothesis is not an instruction to force a change. Return the strongest plan you can defend; "
            "the sealed comparative jury, not this hypothesis, decides whether it beat the canonical base."
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
        + (("\n\nEditable front-matter catalog (use these exact line_id values; rewrite one existing "
            "Skills line per item rather than inventing a combined section ID):\n"
            + front_matter_text[:8000]) if generation else "")
        + "\n\nCanonical/current benchmark (comparison point, not a preservation rule):\n"
        + benchmark_text[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nHigh-information canonical control evidence (preserve unless a stronger replacement is explicit):\n"
        + control_text[:12000]
        + "\n\nTarget-ranked evidence graph nodes (authority and claim_allowed are binding):\n"
        + graph_text[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nCompany/domain routing context (not resume evidence; use only for portfolio prioritization):\n"
        + json.dumps(context.get("company_context") or {}, indent=2, ensure_ascii=False)[:9000]
        + "\n\nRole-and-company tailoring brief (requirements first, then evidence surfaces):\n"
        + json.dumps(context.get("tailoring_brief") or {}, indent=2, ensure_ascii=False)[:14000]
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
        + "\n\nHuman skim budget (binding editorial constraint):\n"
        + json.dumps(HUMAN_PORTFOLIO_CAPS, indent=2, ensure_ascii=False)
        + (("\n\nBinding requirement-to-evidence strategy (act on supported opportunities; leave unsupported gaps honest):\n"
            + generation_text[:18000]) if enhance and generation_strategy else "")
        + (("\n\nShort supported-skills checklist (binding in generation mode):\n"
            "For each listed requirement, either surface the exact supported terms in a meaningful cited body line "
            "or use one existing Skills rewrite with the listed evidence_ids. Do not omit a direct supported item "
            "merely because the page is full; do not add it if the cited evidence cannot authorize the wording.\n"
            + skills_checklist_text) if generation and supported_skills_checklist else "")
    )


def synthesis_prompt(
    context: Dict[str, Any], drafts: List[Dict[str, Any]], catalog: Dict[str, Any], enhance: bool,
    graph: Optional[Dict[str, Any]] = None, unrestricted: bool = False,
    generation: bool = False,
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
                "front_matter_rewrites": data.get("front_matter_rewrites", []),
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
        + ("This is the unchained generation pass: use the requirement-to-evidence strategy to close material supported gaps with newly synthesized bullets or evidence-backed Skills rewrites, and explain every gap left open. " if generation else "")
        + "Choose the stronger defensible plan rather than averaging it. Judge the whole portfolio before individual wording: preserve strong fifth experience bullets when space permits, remove Resident Assistant before a stronger unused technical project, and do not spend a project slot repeating an experience's agents/RAG/retrieval story unless the project adds a materially distinct engineering surface. If the rendered page needs room for a distinct project or bullet, use optional coursework first and the aggregate Awards line second; do not treat quantified external-selection proof such as the HackMIT acceptance line as automatic filler. Do not change an already strong line merely to make the draft look tailored. Compare each substantive swap, exclusion, rewrite, or reorder with the canonical/current benchmark and record the hiring-value gain and important signal lost in decision_ledger. High-value changes include stronger unused evidence, a materially better project, a newly exposed technical dimension, useful ordering, accurate ATS terminology, and reduced redundancy; low-value paraphrase churn is not a goal. Preserve reverse-chronological job order unless the exception is genuinely stronger and explicitly recorded. \n\n"
        "Job context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nEvidence catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nCanonical/current benchmark (comparison point, not a preservation rule):\n"
        + json.dumps(canonical_resume_benchmark(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nHigh-information canonical control evidence (do not trade away for a keyword-only change):\n"
        + canonical_control_prompt(catalog, context.get("target_keywords"))
        + "\n\nTarget-ranked evidence graph:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nCV authority dossier:\n"
        + resume_authority_context(repo_root())
        + "\n\nExact ATS keyword strategy:\n"
        + json.dumps(context.get("target_keywords") or {}, indent=2, ensure_ascii=False)
        + "\n\nHuman skim budget (binding editorial constraint):\n"
        + json.dumps(HUMAN_PORTFOLIO_CAPS, indent=2, ensure_ascii=False)
        + "\n\nCompeting drafts:\n"
        + json.dumps(packed, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
    )


def reviewer_prompt(
    context: Dict[str, Any], tex: str,
    plan: Optional[Dict[str, Any]] = None,
    graph_context: Optional[List[Dict[str, Any]]] = None,
    catalog: Optional[Dict[str, Any]] = None,
    unrestricted: bool = False,
    generation: bool = False,
    critic_role: Optional[Dict[str, Any]] = None,
) -> str:
    critic_role = critic_role if isinstance(critic_role, dict) else {}
    role_label = str(critic_role.get("label") or "general adversarial critic")
    role_focus = str(critic_role.get("focus") or "Audit the full resume against the target and authorized evidence.")
    return (
        "You are the "
        + role_label
        + " in a Codex Luna multi-role critic jury. This is a fresh adversarial review: do not "
        "trust the generation agent, its explanations, or any score it may have claimed. "
        "This is same-model role-separated review, not a claim of vendor independence. "
        "This request is self-contained: do not inspect the filesystem, run commands, or "
        "read prior generated reports. Use only the target, proposed plan, rendered text, "
        "catalog, and authorized evidence supplied below. Return critique JSON only. "
        "Do not return a replacement plan and do not mutate any line. Identify the highest-value "
        "blocking issues and line-level recommendations for the separate writer to apply. "
        "Sections and bullet counts are adaptive: do not penalize an omitted leadership or "
        "project section unless the target argument genuinely needs that evidence. "
        + ("This is an unrestricted creative pass; preserve factual boundaries but prefer a fresh, specific argument over safe base-CV wording. " if unrestricted else "")
        + ("This is an unchained generation pass. Audit whether it closed the strongest supported requirement gaps, whether every new line cites authorizing evidence, and whether any unsupported term was smuggled into the resume. " if generation else "")
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
        "Your assigned focus: "
        + role_focus
        + " Report only material issues within that focus, while still escalating any critical factual, eligibility, or privacy failure.\n"
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
        + "\n\nHigh-information canonical control evidence:\n"
        + canonical_control_prompt(catalog or {}, context.get("target_keywords"))
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
    generation: bool = False,
) -> str:
    """Ask the writer to apply critic-panel feedback without self-grading."""
    return (
        "You are Codex, the revision writer for Victor's resume studio. The Codex Luna "
        "critic panel reviewed the proposed resume below. Apply the highest-value corrections "
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
        + ("This is the unchained generation pass; preserve or improve evidence-backed gap closure and front_matter_rewrites, while keeping unsupported requirements out. " if generation else "")
        + "\n\nTarget context:\n"
        + json.dumps(context, indent=2, ensure_ascii=False)[:MAX_CONTEXT_PROMPT_CHARS]
        + "\n\nCurrent plan:\n"
        + json.dumps(plan, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nCodex Luna critic-panel review:\n"
        + json.dumps(critique, indent=2, ensure_ascii=False)[:MAX_PROMPT_CHARS]
        + "\n\nAuthorized evidence:\n"
        + json.dumps(evidence_context(graph, context, str(context.get("posting_text") or "")) if graph else [], indent=2, ensure_ascii=False)[:MAX_GRAPH_PROMPT_CHARS]
        + "\n\nSource catalog:\n"
        + json.dumps(catalog_for_prompt(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nCanonical/current benchmark (comparison point, not a preservation rule):\n"
        + json.dumps(canonical_resume_benchmark(catalog), indent=2, ensure_ascii=False)[:MAX_CATALOG_PROMPT_CHARS]
        + "\n\nHigh-information canonical control evidence:\n"
        + canonical_control_prompt(catalog, context.get("target_keywords"))
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
        "evidence_id, fact, priority, and section order. Change only bullet text or the text of an existing "
        "front_matter_rewrite. Never delete a front-matter rewrite, change its line_id/evidence_ids, or remove "
        "the supported target terms that motivated it. For wrapped or near-wrap lines "
        "(less than the stated safe right slack), cut filler and compress clauses without losing the technical object, "
        "supported ATS term, or proof. The minimum safe right slack is 12pt: every returned bullet must clear that "
        "threshold, and a near-wrap is still a failure even when the PDF technically stays on one line. Prefer a "
        "short, readable line that ends early; never "
        "expand a bullet merely to approach the right margin. Preserve decision_ledger and front_matter_policy; "
        "compact an unsafe Skills rewrite by removing lower-value non-target tools and compressing separators. "
        "Do not pad, invent, change layout, or return LaTeX beyond inline textbf/emph. Return the complete "
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
    generation: bool = False,
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
        + ("Prioritize a verified line that closes a material supported requirement from the generation strategy. " if generation else "")
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
    selected = provider or "codex"
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
    generation: bool = False,
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
    evidence_text_by_id = {
        str(node.get("id") or ""): " ".join((
            str(node.get("heading") or ""), str(node.get("text") or "")
        ))
        for node in (graph or {}).get("nodes", [])
        if str(node.get("id") or "")
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
        normalized["decision_ledger"][-1]["source_ids"] = list(dict.fromkeys(
            str(value) for value in (item.get("source_ids") or []) if str(value)
        ))[:12]
    raw_front_matter = plan.get("front_matter_policy")
    if not isinstance(raw_front_matter, dict):
        raw_front_matter = {}
    normalized["front_matter_policy"] = {
        "coursework": "omit" if str(raw_front_matter.get("coursework") or "").lower() == "omit" else "keep",
        "awards": "omit" if str(raw_front_matter.get("awards") or "").lower() == "omit" else "keep",
    }
    normalized["front_matter_rewrites"] = []
    # Once a generation plan has evidence-backed Skills rewrites, downstream
    # packers and space-expansion validators must not erase them merely because
    # their helper call did not repeat the mode flag.
    if generation or bool(plan.get("front_matter_rewrites")):
        skill_lines = {
            str(item.get("line_id") or ""): item
            for item in front_matter_catalog(repo_root())
            if str(item.get("line_id") or "").startswith("front:skills:")
        }
        for item in (plan.get("front_matter_rewrites") or [])[:5]:
            if not isinstance(item, dict):
                validation_warnings.append("dropped malformed front-matter rewrite")
                continue
            line_id = str(item.get("line_id") or "")
            if line_id not in skill_lines:
                validation_warnings.append("dropped unknown Skills line rewrite: %s" % line_id)
                continue
            text = _normalize_model_fragment(item.get("text"))
            if not text or FORBIDDEN_CONTENT_COMMANDS.search(text):
                validation_warnings.append("dropped invalid Skills line rewrite: %s" % line_id)
                continue
            unsupported_commands = _unsupported_inline_commands(text)
            if unsupported_commands:
                validation_warnings.append(
                    "dropped Skills rewrite %s with unsupported command(s): %s"
                    % (line_id, ", ".join(unsupported_commands))
                )
                continue
            cited = list(dict.fromkeys(
                str(value) for value in (item.get("evidence_ids") or []) if str(value)
            ))[:8]
            if graph is not None:
                cited = [value for value in cited if value in evidence_ids]
                # Ground exact technologies in a generated Skills line even
                # when a provider mangles or omits a source ID. Only
                # claim-authorized graph nodes may repair the citation set.
                for term in TARGET_KEYWORD_TERMS:
                    if not _keyword_present(term, text):
                        continue
                    for node in graph.get("nodes", []):
                        node_id = str(node.get("id") or "")
                        node_text = " ".join((
                            str(node.get("heading") or ""),
                            str(node.get("text") or ""),
                        ))
                        if (
                            node_id in claim_authorities
                            and _keyword_affirmed(term, node_text)
                            and node_id not in cited
                        ):
                            cited.append(node_id)
                            if len(cited) >= 8:
                                break
                    if len(cited) >= 8:
                        break
                # A Skills rewrite may cite several valid bullets while still
                # smuggling in one unsupported technology.  Verify every
                # target-vocabulary term newly introduced relative to the
                # canonical Skills line against the cited claim-authorized
                # nodes; unrelated citations do not authorize the addition.
                source_text = str(skill_lines[line_id].get("text") or "")
                cited_nodes = [
                    node for node in graph.get("nodes", [])
                    if str(node.get("id") or "") in cited
                    and str(node.get("id") or "") in claim_authorities
                ]
                cited_text = " ".join(
                    " ".join((str(node.get("heading") or ""), str(node.get("text") or "")))
                    for node in cited_nodes
                )
                unsupported_introduced = [
                    term for term in TARGET_KEYWORD_TERMS
                    if _keyword_present(term, text)
                    and not _keyword_present(term, source_text)
                    and not _keyword_affirmed(term, cited_text)
                ]
                if unsupported_introduced:
                    validation_warnings.append(
                        "dropped Skills rewrite %s with unsupported introduced term(s): %s"
                        % (line_id, ", ".join(unsupported_introduced[:8]))
                    )
                    continue
                if not cited or not set(cited) & claim_authorities:
                    validation_warnings.append(
                        "dropped Skills rewrite %s without claim-authorizing evidence" % line_id
                    )
                    continue
            normalized["front_matter_rewrites"].append({
                "line_id": line_id,
                "text": text,
                "evidence_ids": cited,
                "why": str(item.get("why") or "").strip(),
                "source_text": str(skill_lines[line_id].get("text") or ""),
            })
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
    if enhance:
        errors.extend(_project_tradeoff_source_errors(normalized, catalog))
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
                    # A model can copy the primary source ID correctly while
                    # making a small transcription error in the corresponding
                    # evidence citation (for example, repeating one segment
                    # of a project ID).  The source bullet itself is safe to
                    # use as a citation only when the graph attests that exact
                    # node and marks it claim-authorized.  This repairs a
                    # bounded identifier typo without accepting prose or
                    # inventing evidence, and leaves an auditable warning.
                    if not set(cited) & claim_authorities and bullet_id in claim_authorities:
                        cited.append(bullet_id)
                        validation_warnings.append(
                            "repaired evidence citation for %s to its claim-authorized source bullet"
                            % bullet_id
                        )
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
                authorized_sources = [bullet_bank[bullet_id]]
                for supporting_id in supporting_ids:
                    if supporting_id in all_bullet_bank:
                        authorized_sources.append(all_bullet_bank[supporting_id])
                    elif supporting_id in evidence_text_by_id and supporting_id in claim_authorities:
                        authorized_sources.append(evidence_text_by_id[supporting_id])
                for cited_id in cited:
                    if cited_id in evidence_text_by_id and cited_id in claim_authorities:
                        authorized_sources.append(evidence_text_by_id[cited_id])
                unsupported_anchors = _unsupported_introduced_claim_anchors(
                    bullet_bank[bullet_id], text, authorized_sources,
                )
                if unsupported_anchors:
                    validation_warnings.append(
                        "reverted enhanced bullet %s after it introduced uncited claim anchor(s): %s"
                        % (bullet_id, ", ".join(unsupported_anchors[:8]))
                    )
                    text = bullet_bank[bullet_id]
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
        families = _portfolio_signal_families(text)
        score += 2.0 * len(families)
        # When the page is full, a deterministic replacement should prefer a
        # genuinely distinct mechanism or externally validated result over a
        # second generic AI/backend line. This remains a tie-breaker; the
        # sealed panel still decides whether the swap helped the job.
        if "external_validation" in families:
            score += 8.0
        if "algorithms_validation" in families:
            score += 4.0
        if re.search(
            r"\b(?:quantum|qubit|brownian|monte\s+carlo|historical[- ]market|"
            r"knowledge[- ]graph|transcript|coordination|simulation|clustering)\b",
            text, re.I,
        ):
            score += 8.0
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
        removal_frontier = removals[:MAX_SPACE_SWAP_CANDIDATES]
        attempts.extend(("swap", [action]) for action in removal_frontier)
        attempts.extend(
            ("swap", list(actions))
            for actions in itertools.combinations(removal_frontier, 2)
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


def deterministic_distinctive_replacement(
    plan: Dict[str, Any], catalog: Dict[str, Any],
    graph: Optional[Dict[str, Any]], keyword_strategy: Optional[Dict[str, Any]],
    run_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Try one bounded full-page swap for stronger unused evidence.

    Measured whitespace is not the only useful capacity signal: a lower-value
    selected bullet can be worth replacing with a distinctive, authorized
    two-bullet project. This helper is intentionally deterministic and
    single-shot. It cannot add unsupported text, displace core experience, or
    bypass the later sealed panel; failed geometry restores the prior plan.
    """
    additions = deterministic_space_additions(
        plan, catalog, graph=graph, keyword_strategy=keyword_strategy,
    )
    new_groups: Dict[str, List[Dict[str, Any]]] = {}
    appends: List[Dict[str, Any]] = []
    for item in additions:
        entry_id = str(item.get("entry_id") or "")
        if str(item.get("placement") or "") == "new_entry":
            new_groups.setdefault(entry_id, []).append(item)
        else:
            appends.append(item)

    def distinctiveness(item: Dict[str, Any]) -> float:
        text = _latex_plain(str(item.get("text") or ""))
        families = _portfolio_signal_families(text)
        score = 3.0 * len(families)
        score += 10.0 if "external_validation" in families else 0.0
        score += 5.0 if "algorithms_validation" in families else 0.0
        score += 9.0 if re.search(
            r"\b(?:quantum|qubit|brownian|monte\s+carlo|historical[- ]market|"
            r"knowledge[- ]graph|transcript|coordination|simulation|clustering)\b",
            text, re.I,
        ) else 0.0
        score += 2.0 * len(_resume_numeric_anchors(text))
        return score

    groups = [
        sorted(group, key=lambda item: (-distinctiveness(item), str(item.get("source_id") or "")))
        for group in new_groups.values()
        if len(group) >= 2
    ]
    groups.sort(key=lambda group: (-sum(distinctiveness(item) for item in group), str(group[0].get("entry_id") or "")))
    candidates = groups[0] if groups else ([max(appends, key=distinctiveness)] if appends else [])
    if not candidates:
        return plan, {
            "attempted": False,
            "status": "not_available",
            "candidates": [],
            "applied": [],
            "replaced": [],
        }
    prior = copy.deepcopy(plan)
    replacement, result = expand_into_measured_space(
        plan, candidates, catalog, graph, run_dir,
    )
    if not result.get("applied"):
        return plan, {
            "attempted": True,
            "status": "rejected",
            "candidates": [str(item.get("source_id") or "") for item in candidates],
            "applied": [],
            "replaced": list(result.get("replaced") or []),
            "reason": str(result.get("decision") or "candidate did not earn a compiled replacement"),
        }
    # ``expand_into_measured_space`` guarantees the vertical contract, but
    # retain the prior plan if this bounded replacement introduces a near-wrap
    # that the deterministic compactor cannot safely repair at this stage.
    candidate_root = run_dir / "distinctive_replacement"
    candidate_tex = render_plan(replacement, catalog, repo_root())
    (candidate_root).mkdir(parents=True, exist_ok=True)
    (candidate_root / "resume.tex").write_text(candidate_tex)
    compiled = compile_resume(candidate_root)
    candidate_layout = pdf_layout(candidate_root, compiled, plan=replacement, run_capacity_test=False)
    if not (
        candidate_layout.get("compiled")
        and candidate_layout.get("pages") == 1
        and not candidate_layout.get("overfull")
    ):
        return prior, {
            "attempted": True,
            "status": "rejected",
            "candidates": [str(item.get("source_id") or "") for item in candidates],
            "applied": [],
            "replaced": [],
            "reason": "distinctive replacement failed the compiled one-page prerequisite",
        }
    return replacement, {
        "attempted": True,
        "status": "applied",
        "candidates": [str(item.get("source_id") or "") for item in candidates],
        "applied": list(result.get("applied") or []),
        "replaced": list(result.get("replaced") or []),
        "layout": candidate_layout.get("horizontal") or {},
    }


def deterministic_control_recovery(
    plan: Dict[str, Any], catalog: Dict[str, Any],
    keyword_strategy: Optional[Dict[str, Any]], run_dir: Path,
    trim_overlap: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Recover high-information base proof before independent judging.

    The author can make a valid but strategically poor choice: keep several
    generated project bullets while dropping a canonical HPC, validation, or
    mechanism line. This bounded pass does not invent or rewrite anything. It
    restores omitted canonical lines whenever a selected non-canonical line is
    available to displace, and, when enabled, removes at most one additional
    non-canonical bullet when a selected project is clearly repeating an
    experience signal family. The sealed panel still decides whether the
    resulting candidate actually improved the job match. Audit repairs use the
    canonical-replacement portion only: a repair should not receive a second
    deterministic portfolio edit before its fresh sealed recheck.
    """
    recovered = copy.deepcopy(plan)
    entries = catalog.get("entries") or {}
    all_control = canonical_control_evidence(
        catalog, keyword_strategy, limit=max(1, len(entries) * 12),
    )
    control_scores = {
        str(item.get("source_id") or ""): float(item.get("control_priority") or 0)
        for item in all_control
        if str(item.get("source_id") or "")
    }
    source_bullets = {
        str(bullet.get("id") or ""): bullet
        for entry in entries.values()
        for bullet in entry.get("bullets") or []
        if str(bullet.get("id") or "")
    }
    actions: List[Dict[str, Any]] = []
    restored_ids = set()
    skipped_explained: List[Dict[str, Any]] = []
    decision_ledger = [
        item for item in (recovered.get("decision_ledger") or [])
        if isinstance(item, dict)
    ]

    def make_bullet(source_id: str, rationale: str) -> Optional[Dict[str, Any]]:
        source = source_bullets.get(source_id)
        if not source:
            return None
        text = str(source.get("text") or "")
        if not text:
            return None
        return {
            "source_id": source_id,
            "source_ids": [source_id],
            "text": text,
            "evidence_ids": [source_id],
            "priority": max(1, min(100, int(control_scores.get(source_id, 75)))),
            "candidate_rationale": rationale,
        }

    # First protect omitted canonical lines inside entries the author already
    # selected. Replacing an extra/cv_full line is preferred; replacing a
    # canonical line is allowed only when the omitted line has a higher
    # deterministic proof priority.
    for section in ("experiences", "projects", "leadership"):
        for selection in recovered.get(section, []) or []:
            entry_id = str(selection.get("source_id") or "")
            entry = entries.get(entry_id) or {}
            selected_ids = {
                str(bullet.get("source_id") or "")
                for bullet in selection.get("bullets", []) or []
            }
            omitted = [
                bullet for bullet in entry.get("bullets") or []
                if _is_canonical_source(bullet.get("source"))
                and str(bullet.get("id") or "") not in selected_ids
            ]
            omitted.sort(key=lambda bullet: (-control_scores.get(str(bullet.get("id") or ""), 0), str(bullet.get("id") or "")))
            if not omitted:
                continue
            # An explicit source-grounded tradeoff is already the author's
            # claim that the omitted canonical line lost a marginal slot to a
            # stronger target-specific replacement. Do not silently undo that
            # editorial decision with a generic proof-priority heuristic. If
            # the choice is wrong, the sealed panel and audit repair can still
            # reject it; unexplained losses remain eligible for recovery.
            explained = [
                bullet for bullet in omitted
                if _ledger_explicitly_names_source(
                    str(bullet.get("id") or ""), decision_ledger,
                )
            ]
            if explained:
                skipped_explained.append({
                    "section": section,
                    "entry_id": entry_id,
                    "source_ids": [str(item.get("id") or "") for item in explained],
                    "reason": "explicit decision_ledger tradeoff preserved before independent judging",
                })
                omitted = [item for item in omitted if item not in explained]
                if not omitted:
                    continue
            remaining_omitted = list(omitted)
            while remaining_omitted:
                source_id = str(remaining_omitted[0].get("id") or "")
                omitted_families = set(
                    _portfolio_signal_families(remaining_omitted[0].get("text") or "")
                )
                noncanonical_targets = [
                    (index, bullet)
                    for index, bullet in enumerate(selection.get("bullets", []) or [])
                    if not _is_canonical_source(
                        source_bullets.get(str(bullet.get("source_id") or ""), {}).get("source")
                    )
                ]
                if not noncanonical_targets:
                    break
                # Restore every omitted canonical line that has a selected
                # noncanonical line to displace. A new source line may add
                # value, but it cannot silently spend the same entry's
                # canonical mechanism/metric budget. If one new line is
                # genuinely worth keeping, the author can preserve it by
                # explicitly naming the canonical source it replaces.
                ranked_targets = []
                for index, bullet in noncanonical_targets:
                    target_families = set(
                        _portfolio_signal_families(bullet.get("text") or "")
                    )
                    shared = len(target_families & omitted_families)
                    ranked_targets.append((
                        shared,
                        -_control_bullet_value(bullet, catalog),
                        -index,
                        index,
                        bullet,
                    ))
                _shared, _value, _order, target_index, target = max(ranked_targets)
                replacement = make_bullet(
                    source_id,
                    "Recovered canonical proof before independent judging; replaced lower-information selected evidence.",
                )
                if not replacement:
                    break
                old_id = str(target.get("source_id") or "")
                selection["bullets"][target_index] = replacement
                restored_ids.add(source_id)
                actions.append({
                    "kind": "canonical_replacement",
                    "section": section,
                    "entry_id": entry_id,
                    "restored_source_id": source_id,
                    "replaced_source_id": old_id,
                    "reason": "canonical proof priority exceeded the selected replacement",
                })
                remaining_omitted.pop(0)

    # Then make one narrow overlap repair. This specifically targets a
    # non-canonical project bullet whose signal family is already established
    # by experience, and only when the project has more than four bullets.
    diagnostics = portfolio_diagnostics(recovered, catalog) if trim_overlap else {}
    overlap_ids = {
        str(item.get("source_id") or "")
        for item in diagnostics.get("project_overlap", [])
        if item.get("severity") == "high"
    }
    experience_families = set()
    for entry_id, families in (diagnostics.get("selected_entry_families") or {}).items():
        if str(entry_id).startswith("experience:"):
            experience_families.update(families)
    for selection in recovered.get("projects", []) or []:
        entry_id = str(selection.get("source_id") or "")
        bullets = selection.get("bullets", []) or []
        if entry_id not in overlap_ids or len(bullets) <= 4:
            continue
        removable = []
        for index, bullet in enumerate(bullets):
            source_id = str(bullet.get("source_id") or "")
            source = source_bullets.get(source_id) or {}
            if _is_canonical_source(source.get("source")) or source_id in restored_ids:
                continue
            families = set(_portfolio_signal_families(bullet.get("text") or ""))
            shared = families & experience_families
            if shared:
                removable.append((
                    _bullet_value(bullet), index, source_id, sorted(shared),
                ))
        if removable:
            _, index, source_id, shared = min(removable)
            removed = bullets.pop(index)
            actions.append({
                "kind": "overlap_trim",
                "section": "projects",
                "entry_id": entry_id,
                "removed_source_id": source_id,
                "shared_signal_families": shared,
                "reason": "trim one non-canonical project line repeating selected experience families",
            })
            break

    record = {
        "attempted": True,
        "status": "applied" if actions else "not_needed",
        "actions": actions,
        "restored_source_ids": sorted(restored_ids),
        "skipped_explained": skipped_explained,
        "reason": (
            "Recovered canonical proof and/or trimmed one repeated project signal before sealed review."
            if actions else "No bounded control recovery cleared its deterministic conditions."
        ),
    }
    write_json(run_dir / "control_recovery.json", record)
    return recovered, record


def deterministic_role_evidence_floor(
    plan: Dict[str, Any], catalog: Dict[str, Any], context: Dict[str, Any],
    graph: Optional[Dict[str, Any]], run_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Protect omitted project-level evidence for the role's real center of gravity.

    The canonical control recovery above can only replace a bullet inside an
    entry the author already selected.  That leaves a blind spot: a valid
    author plan can omit an entire React, C++, or systems project and spend the
    project budget on adjacent ML/keyword evidence instead.  This floor is a
    narrow counterfactual, not a preserve-the-base rule:

    * it considers only source-authorized project entries (canonical or
      current evidence-bank bullets attested by the evidence graph);
    * it scores implemented mechanism terms against the deterministic primary
      and secondary role tracks plus the requirement map;
    * it replaces at most one weak project entry; ambiguity changes the
      comparison margin, not the number of deterministic edits;
    * it compiles the result before returning it; and
    * the normal sealed jury still decides whether the replacement beats base.

    This specifically prevents keyword-adjacent projects from displacing a
    materially better role surface while keeping the final judgment
    comparative and fail-closed.
    """
    original = copy.deepcopy(plan)
    intelligence = context.get("job_intelligence") if isinstance(context, dict) else {}
    intelligence = intelligence if isinstance(intelligence, dict) else {}
    primary = str(intelligence.get("primary_role_track") or "").strip()
    secondary = [
        str(value) for value in (intelligence.get("secondary_role_tracks") or [])
        if str(value)
    ]
    if not primary or primary not in ROLE_FLOOR_TERMS:
        return original, {
            "attempted": False,
            "status": "not_available",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "reason": "no supported deterministic primary role track",
        }

    entries = catalog.get("entries") or {}
    selected_projects = [
        entry for entry in original.get("projects", []) or []
        if str(entry.get("source_id") or "")
    ]
    selected_ids = {str(entry.get("source_id") or "") for entry in selected_projects}
    graph_authority = {
        str(node.get("id") or "")
        for node in (graph or {}).get("nodes", [])
        if node.get("claim_allowed") and str(node.get("id") or "")
    }
    eligible_project_ids = set()
    for entry_id, entry in entries.items():
        if str(entry.get("kind") or "") != "project":
            continue
        eligible_bullets = [
            bullet for bullet in (entry.get("bullets") or [])
            if _is_canonical_source(bullet.get("source"))
            or str(bullet.get("id") or "") in graph_authority
        ]
        if len(eligible_bullets) >= 2:
            eligible_project_ids.add(str(entry_id))
    if not eligible_project_ids:
        return original, {
            "attempted": False,
            "status": "not_available",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "reason": "catalog has no canonical project control entries",
        }

    requirements = [
        item for item in (intelligence.get("requirements") or [])
        if isinstance(item, dict)
    ]
    required_source_bonus: Dict[str, float] = {}
    required_terms: Dict[str, List[str]] = {}
    for item in requirements:
        importance = str(item.get("importance") or "mentioned").lower()
        role_relevance = str(item.get("role_relevance") or "general").lower()
        if importance not in {"required", "responsibility", "eligibility"} and role_relevance != "primary":
            continue
        bonus = 9.0 if role_relevance == "primary" else 5.0
        if importance == "required":
            bonus += 3.0
        terms = [
            str(value).lower() for value in (item.get("exact_terms") or [])
            if str(value).strip()
        ]
        for source_id in item.get("evidence_ids") or []:
            source_key = str(source_id)
            required_source_bonus[source_key] = max(
                bonus, required_source_bonus.get(source_key, 0.0)
            )
            required_terms.setdefault(source_key, []).extend(terms)

    keyword_sources: Dict[str, List[Tuple[str, float]]] = {}
    for item in (context.get("target_keywords") or {}).get("terms", []) if isinstance(context, dict) else []:
        if not isinstance(item, dict) or not item.get("supported"):
            continue
        term = str(item.get("term") or "").lower()
        if not term:
            continue
        weight = 3.0 if item.get("required") else 1.5
        for source_id in item.get("source_ids") or []:
            keyword_sources.setdefault(str(source_id), []).append((term, weight))

    def matching_terms(text: str, track: str) -> List[str]:
        return [
            term for term in ROLE_FLOOR_TERMS.get(track, ())
            if _role_signal_present(text, term)
        ]

    def score_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        eligible_bullets = [
            bullet for bullet in (entry.get("bullets") or [])
            if _is_canonical_source(bullet.get("source"))
            or str(bullet.get("id") or "") in graph_authority
        ]
        bullet_scores = []
        for bullet in eligible_bullets:
            source_id = str(bullet.get("id") or "")
            text = _latex_plain(str(bullet.get("text") or ""))
            primary_terms = matching_terms(text, primary)
            secondary_terms = sorted({
                term for track in secondary
                for term in matching_terms(text, track)
            })
            score = 0.0
            score += 5.0 * len(primary_terms)
            score += 1.75 * len(secondary_terms)
            score += required_source_bonus.get(source_id, 0.0)
            score += sum(weight for term, weight in keyword_sources.get(source_id, []) if _role_signal_present(text, term))
            score += min(4.0, 1.25 * len(_portfolio_signal_families(text)))
            score += min(3.0, 1.0 * len(_resume_numeric_anchors(text)))
            bullet_scores.append({
                "source_id": source_id,
                "text": str(bullet.get("text") or ""),
                "score": round(score, 2),
                "primary_terms": primary_terms,
                "secondary_terms": secondary_terms,
                "required_terms": sorted(set(required_terms.get(source_id, []))),
            })
        bullet_scores.sort(key=lambda item: (-float(item["score"]), str(item["source_id"])))
        top = bullet_scores[:2]
        primary_terms = sorted({term for item in top for term in item["primary_terms"]})
        secondary_terms = sorted({term for item in top for term in item["secondary_terms"]})
        required_hits = sorted({term for item in top for term in item["required_terms"]})
        return {
            "entry_id": str(entry.get("id") or ""),
            "label": _project_heading(entry.get("heading") or entry.get("id") or ""),
            "score": round(sum(float(item["score"]) for item in top), 2),
            "primary_terms": primary_terms,
            "secondary_terms": secondary_terms,
            "required_hits": required_hits,
            "bullets": bullet_scores,
            "eligible_bullet_count": len(eligible_bullets),
        }

    scored_entries = {
        entry_id: score_entry(entry)
        for entry_id, entry in entries.items()
        if str(entry.get("kind") or "") == "project"
        and entry_id in eligible_project_ids
    }
    omitted = [
        item for entry_id, item in scored_entries.items()
        if entry_id not in selected_ids
        and (item["primary_terms"] or item["required_hits"])
    ]
    selected_scores = {
        entry_id: scored_entries.get(entry_id) or score_entry(entries.get(entry_id) or {})
        for entry_id in selected_ids
    }
    if not omitted:
        return original, {
            "attempted": True,
            "status": "not_needed",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "primary_role_track": primary,
            "secondary_role_tracks": secondary,
            "candidates": [],
            "actions": [],
            "reason": "no omitted canonical project had a supported primary/required role surface",
        }

    omitted.sort(key=lambda item: (-float(item["score"]), item["entry_id"]))
    selected_project_scores = [
        item for item in selected_scores.values()
        if item.get("entry_id") in selected_ids
    ]
    selected_project_scores.sort(key=lambda item: (float(item.get("score") or 0), item.get("entry_id") or ""))
    # Even an ambiguous posting gets one bounded role-floor edit. A second
    # attractive project is a portfolio hypothesis, not deterministic truth;
    # adding it here can solve one omission by displacing a different
    # distinctive interview story. The search profile can explore that
    # alternative under fresh sealed panels.
    max_replacements = 1
    trial = copy.deepcopy(original)
    actions: List[Dict[str, Any]] = []
    chosen_candidates: List[Dict[str, Any]] = []
    remaining_selected = list(selected_project_scores)
    for candidate in omitted:
        if len(chosen_candidates) >= max_replacements:
            break
        weak = remaining_selected[0] if remaining_selected else None
        weak_score = float(weak.get("score") or 0) if weak else 0.0
        candidate_score = float(candidate.get("score") or 0)
        # A project must earn a real role surface, and an omitted project must
        # clear a meaningful margin over the weakest selected project. This
        # keeps ordinary tailoring from drifting toward a canonical project
        # merely because it contains one shared keyword.
        minimum = 12.0 if not candidate.get("required_hits") else 10.0
        if candidate_score < minimum or candidate_score < weak_score + 7.0:
            continue
        if not weak:
            chosen_candidates.append(candidate)
            continue
        chosen_candidates.append(candidate)
        remaining_selected.pop(0)

    if not chosen_candidates:
        return original, {
            "attempted": True,
            "status": "not_needed",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "primary_role_track": primary,
            "secondary_role_tracks": secondary,
            "candidates": omitted[:6],
            "actions": [],
            "reason": "omitted role evidence did not clear the bounded replacement margin",
        }

    for candidate in chosen_candidates:
        entry_id = candidate["entry_id"]
        remove_id = ""
        source_entry = entries.get(entry_id) or {}
        selected_bullets = [
            bullet for bullet in candidate.get("bullets") or []
            if str(bullet.get("source_id") or "")
        ][:2]
        if len(selected_bullets) < 2:
            continue
        new_entry = {
            "source_id": entry_id,
            "bullets": [{
                "source_id": item["source_id"],
                "source_ids": [item["source_id"]],
                "evidence_ids": [item["source_id"]],
                "text": item["text"],
                "priority": max(75, min(100, int(round(item["score"])))),
                "candidate_rationale": "Role-evidence floor preserved an omitted source-authorized project surface for %s." % primary,
            } for item in selected_bullets],
            "why": "Role-evidence floor preserved a materially stronger supported role surface before sealed judging.",
        }
        trial.setdefault("projects", []).append(new_entry)
        # Replace the current lowest-scoring project when the roster is full;
        # otherwise the normal packer may retain the additional entry.
        if len(trial.get("projects", []) or []) > HUMAN_PORTFOLIO_CAPS["project_entries"]:
            removable = [
                entry for entry in trial.get("projects", []) or []
                if str(entry.get("source_id") or "") != entry_id
                and str(entry.get("source_id") or "") in selected_ids
            ]
            if not removable:
                continue
            remove_id = str(min(
                removable,
                key=lambda entry: (
                    float((selected_scores.get(str(entry.get("source_id") or "")) or {}).get("score") or 0),
                    str(entry.get("source_id") or ""),
                ),
            ).get("source_id") or "")
            trial["projects"] = [
                entry for entry in trial.get("projects", []) or []
                if str(entry.get("source_id") or "") != str(remove_id or "")
            ]
        actions.append({
            "kind": "role_evidence_floor_replacement",
            "replaced_project_id": remove_id,
            "restored_project_id": entry_id,
            "restored_label": candidate.get("label") or entry_id,
            "primary_role_track": primary,
            "primary_terms": candidate.get("primary_terms") or [],
            "secondary_terms": candidate.get("secondary_terms") or [],
            "required_hits": candidate.get("required_hits") or [],
            "candidate_score": candidate.get("score"),
            "tradeoff_source_ids": [
                str(bullet.get("id") or "")
                for bullet in (entries.get(remove_id) or {}).get("bullets") or []
                if _is_canonical_source(bullet.get("source")) and str(bullet.get("id") or "")
            ],
            "reason": "An omitted source-authorized project cleared the bounded role-surface margin over the weakest selected project.",
        })

    if not actions:
        return original, {
            "attempted": True,
            "status": "not_needed",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "primary_role_track": primary,
            "secondary_role_tracks": secondary,
            "candidates": omitted[:6],
            "actions": [],
            "reason": "candidate roster could not be changed safely",
        }

    trial.setdefault("decision_ledger", []).extend({
        "action": "role-evidence floor replacement",
        "current_evidence": action.get("replaced_project_id") or "",
        "replacement_or_exclusion": action.get("restored_project_id") or "",
        "target_signal": ", ".join(action.get("primary_terms") or action.get("required_hits") or []),
        "why_stronger": action.get("reason") or "",
        "signal_lost": "Adjacent project evidence was displaced only after the role-surface margin was measured.",
        "source_ids": list(action.get("tradeoff_source_ids") or []),
    } for action in actions)
    normalized, errors = validate_plan(
        trial, catalog, enhance=True, graph=graph,
    )
    if errors:
        return original, {
            "attempted": True,
            "status": "rejected",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "primary_role_track": primary,
            "secondary_role_tracks": secondary,
            "candidates": omitted[:6],
            "actions": actions,
            "reason": "source validation rejected the bounded role floor: " + "; ".join(errors[:4]),
        }
    try:
        packed, packing = pack_plan_to_page(
            normalized, catalog, run_dir / "packing",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return original, {
            "attempted": True,
            "status": "rejected",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "primary_role_track": primary,
            "secondary_role_tracks": secondary,
            "candidates": omitted[:6],
            "actions": actions,
            "reason": "bounded role floor failed compiled packing: %s" % exc,
        }
    restored_ids = {
        str(item.get("restored_project_id") or "") for item in actions
    }
    packed_ids = {str(entry.get("source_id") or "") for entry in packed.get("projects", []) or []}
    if not restored_ids <= packed_ids:
        return original, {
            "attempted": True,
            "status": "rejected",
            "version": ROLE_EVIDENCE_FLOOR_VERSION,
            "primary_role_track": primary,
            "secondary_role_tracks": secondary,
            "candidates": omitted[:6],
            "actions": actions,
            "reason": "page packer did not retain every role-floor project",
    }
    trial = enforce_experience_order(packed, catalog)
    receipt = {
        "attempted": True,
        "status": "applied",
        "version": ROLE_EVIDENCE_FLOOR_VERSION,
        "primary_role_track": primary,
        "secondary_role_tracks": secondary,
        "track_confidence": intelligence.get("track_confidence"),
        "candidates": omitted[:6],
        "actions": actions,
        "packing": packing,
        "reason": "Applied a bounded source-authorized role-evidence floor; the exact artifact still requires sealed comparative judging.",
    }
    write_json(run_dir / "role_evidence_floor.json", receipt)
    return trial, receipt


def deterministic_target_opportunity_replacement(
    plan: Dict[str, Any], catalog: Dict[str, Any], graph: Dict[str, Any],
    context: Dict[str, Any], run_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Surface one buried, authorized requirement opportunity before judging.

    Unchained generation can correctly identify a requirement-to-evidence
    mapping and still omit the best source line because the author is solving
    a crowded page-selection problem.  This bounded pass turns only the
    strongest *supported* opportunity into one compiled counterfactual: it
    adds a source-verbatim bullet from an already selected entry and removes
    the lowest-marginal selected line needed to pay for it.  It never adds a
    heading, invents wording, or bypasses the sealed jury.  If the result is
    not one-page safe or the addition is packed away, the original plan is
    returned unchanged.

    This is deliberately one opportunity, not a deterministic keyword
    optimizer.  The panel must still establish whether the newly visible
    evidence beats the control resume.
    """
    original = copy.deepcopy(plan)
    strategy = context.get("generation_strategy")
    if not isinstance(strategy, dict):
        return original, {
            "attempted": False, "status": "not_available",
            "reason": "generation strategy unavailable",
        }
    requirements = strategy.get("requirements") or []
    if not isinstance(requirements, list):
        return original, {
            "attempted": False, "status": "not_available",
            "reason": "generation requirements were not a list",
        }

    entries = catalog.get("entries") or {}
    source_bullets = {
        str(bullet.get("id") or ""): bullet
        for entry in entries.values()
        for bullet in entry.get("bullets") or []
        if str(bullet.get("id") or "")
    }
    bullet_locations = {
        str(bullet.get("id") or ""): (section, entry)
        for section in ("experiences", "projects", "leadership")
        for entry in original.get(section, []) or []
        for bullet in entry.get("bullets") or []
        if str(bullet.get("id") or "")
    }
    entry_locations = {
        str(entry.get("source_id") or ""): (section, entry)
        for section in ("experiences", "projects", "leadership")
        for entry in original.get(section, []) or []
        if str(entry.get("source_id") or "")
    }
    selected_ids = _selected_bullet_ids(original)
    allowed_ids = {
        str(node.get("id") or "")
        for node in (graph or {}).get("nodes", [])
        if node.get("claim_allowed") and str(node.get("id") or "")
    }
    if not allowed_ids:
        return original, {
            "attempted": False,
            "status": "not_available",
            "reason": "evidence graph had no claim-allowed source nodes",
        }
    role_tracks = set(
        str(value) for value in
        ((context.get("job_intelligence") or {}).get("role_tracks") or [])
    )

    def requirement_text(item: Dict[str, Any]) -> str:
        return " ".join(
            str(item.get(field) or "")
            for field in ("requirement", "candidate_angle", "reason", "target_signal")
        ) + " " + " ".join(str(value) for value in item.get("exact_terms") or [])

    def opportunity_score(item: Dict[str, Any], source_text: str) -> Tuple[float, str]:
        req_text = requirement_text(item)
        req_tokens = _resume_tokens(req_text)
        source_tokens = _resume_tokens(source_text)
        overlap = len(req_tokens & source_tokens)
        importance = str(item.get("importance") or "mentioned").lower()
        score = float(TAILORING_PRIORITY_WEIGHTS.get(importance, 1) * 10)
        score += min(18.0, float(overlap * 3))
        if str(item.get("evidence_status") or "").lower() == "direct":
            score += 12.0
        if str(item.get("recommended_action") or "").lower() in {"synthesize", "rewrite", "tailor_skills"}:
            score += 6.0
        # These are high-value surfaces for a buried source line. They are
        # used only as a ranking tie-breaker after source authority and the
        # requirement map have been established.
        if re.search(
            r"\b(?:dashboard|interface|display|web|rest|endpoint|flask|react|"
            r"testing|documentation|stakeholder|reliab|integration)\b",
            source_text + " " + req_text, re.I,
        ):
            score += 12.0
        return score, req_text

    candidates: List[Dict[str, Any]] = []
    for item in requirements:
        if not isinstance(item, dict):
            continue
        status = str(item.get("evidence_status") or "").lower()
        action = str(item.get("recommended_action") or "").lower()
        if status not in {"direct", "adjacent", "supported"}:
            continue
        if action not in {"keep", "rewrite", "synthesize", "tailor_skills", "surface"}:
            continue
        for raw_id in item.get("evidence_ids") or []:
            source_id = str(raw_id or "")
            source = source_bullets.get(source_id)
            if not source or source_id in selected_ids:
                continue
            if allowed_ids and source_id not in allowed_ids:
                continue
            location = bullet_locations.get(source_id)
            if location is None:
                location = entry_locations.get(source_id.rsplit(":b", 1)[0])
            if location is None:
                # A new heading is outside this pass's bounded contract. The
                # author/search lanes remain responsible for whole-project
                # alternatives.
                continue
            section, target_entry = location
            source_text = _latex_plain(str(source.get("text") or ""))
            if not source_text or any(
                _same_entry_resume_bullet(source_text, _latex_plain(str(bullet.get("text") or "")))
                for bullet in target_entry.get("bullets") or []
            ):
                continue
            score, req_text = opportunity_score(item, source_text)
            candidates.append({
                "source_id": source_id,
                "entry_id": str(target_entry.get("source_id") or ""),
                "section": section,
                "text": str(source.get("text") or ""),
                "score": score,
                "requirement": str(item.get("requirement") or "")[:300],
                "requirement_text": req_text[:700],
                "importance": str(item.get("importance") or "mentioned"),
                "target_signal": str(item.get("candidate_angle") or item.get("requirement") or "")[:400],
                "source": str(source.get("source") or ""),
            })
    if not candidates:
        return original, {
            "attempted": False, "status": "not_available",
            "reason": "no unused, source-authorized bullet from a supported selected-entry opportunity",
        }
    candidates.sort(key=lambda item: (-float(item["score"]), str(item["source_id"])))
    chosen = candidates[0]
    target_entry = next(
        entry for entry in original.get(chosen["section"], []) or []
        if str(entry.get("source_id") or "") == chosen["entry_id"]
    )
    source_by_id = source_bullets
    usage_counts: Dict[str, int] = {}
    for item in requirements:
        if not isinstance(item, dict):
            continue
        for value in item.get("evidence_ids") or []:
            usage_counts[str(value)] = usage_counts.get(str(value), 0) + 1

    def removal_cost(bullet: Dict[str, Any]) -> float:
        source_id = str(bullet.get("source_id") or "")
        text = _latex_plain(str(bullet.get("text") or ""))
        cost = _control_bullet_value(bullet, catalog)
        if _is_canonical_source((source_by_id.get(source_id) or {}).get("source")):
            cost += 8.0
        if _resume_numeric_anchors(text):
            cost += 24.0
        if "external_validation" in _portfolio_signal_families(text):
            cost += 20.0
        cost += min(12.0, float(usage_counts.get(source_id, 0) * 4))
        families = set(_portfolio_signal_families(text))
        if (
            "systems_performance" in role_tracks
            and "systems_reliability" in families
        ) or (
            "backend_infrastructure" in role_tracks
            and "backend_api" in families
        ):
            cost += 14.0
        return cost

    same_entry_limit = (
        HUMAN_PORTFOLIO_CAPS["experience_bullets_per_entry"]
        if chosen["section"] == "experiences"
        else PORTFOLIO_CAPS.get(chosen["section"], {}).get("bullets", 8)
    )
    removal: Optional[Tuple[str, str]] = None
    if len(target_entry.get("bullets", []) or []) >= same_entry_limit:
        eligible = [
            bullet for bullet in target_entry.get("bullets", []) or []
            if str(bullet.get("source_id") or "") != chosen["source_id"]
            and len(target_entry.get("bullets", []) or []) > 1
        ]
        if eligible:
            selected = min(eligible, key=lambda bullet: (
                removal_cost(bullet), str(bullet.get("source_id") or "")
            ))
            removal = (chosen["section"], str(selected.get("source_id") or ""))
    if removal is None:
        total = portfolio_metrics(original).get("total_bullets", 0)
        if total >= HUMAN_PORTFOLIO_CAPS["total_bullets"]:
            global_candidates = []
            for section in ("projects", "leadership"):
                for entry in original.get(section, []) or []:
                    for bullet in entry.get("bullets", []) or []:
                        if len(entry.get("bullets", []) or []) <= 1:
                            continue
                        global_candidates.append((removal_cost(bullet), section, str(bullet.get("source_id") or "")))
            if global_candidates:
                _, section, source_id = min(global_candidates, key=lambda value: (value[0], value[1], value[2]))
                removal = (section, source_id)
    if len(target_entry.get("bullets", []) or []) >= same_entry_limit and removal is None:
        return original, {
            "attempted": True, "status": "not_available",
            "source_id": chosen["source_id"],
            "requirement": chosen["requirement"],
            "reason": "supported opportunity had no safe marginal line to displace",
        }

    trial = copy.deepcopy(original)
    target = next(
        entry for entry in trial.get(chosen["section"], []) or []
        if str(entry.get("source_id") or "") == chosen["entry_id"]
    )
    addition = {
        "source_id": chosen["source_id"],
        "source_ids": [chosen["source_id"]],
        "evidence_ids": [chosen["source_id"]],
        "text": chosen["text"],
        "priority": 90,
        "candidate_rationale": "Surfaced buried authorized evidence for: %s" % chosen["requirement"],
    }
    target.setdefault("bullets", []).append(addition)
    removed_text = ""
    if removal is not None:
        removal_section, removal_source_id = removal
        for entry in trial.get(removal_section, []) or []:
            for index, bullet in enumerate(entry.get("bullets", []) or []):
                if str(bullet.get("source_id") or "") == removal_source_id:
                    removed_text = _latex_plain(str(bullet.get("text") or ""))
                    entry["bullets"].pop(index)
                    break
            if removed_text:
                break
    trial.setdefault("decision_ledger", []).append({
        "action": "Surface buried authorized evidence for a mapped role opportunity",
        "current_evidence": removal[1] if removal else "",
        "replacement_or_exclusion": chosen["source_id"],
        "target_signal": chosen["target_signal"],
        "why_stronger": "The requirement map identifies this unused source line as supported evidence for a target opportunity; the pass pays for it with the lowest-marginal selected line available under the skim budget.",
        "signal_lost": removed_text,
    })
    normalized, errors = validate_plan(
        trial, catalog, enhance=True, graph=graph, generation=True,
    )
    if errors:
        return original, {
            "attempted": True, "status": "rejected",
            "source_id": chosen["source_id"], "requirement": chosen["requirement"],
            "reason": "source validation rejected the bounded opportunity: " + "; ".join(errors[:4]),
        }
    try:
        packed, packing = pack_plan_to_page(normalized, catalog, run_dir / "packing")
    except (OSError, RuntimeError, ValueError) as exc:
        return original, {
            "attempted": True, "status": "rejected",
            "source_id": chosen["source_id"], "requirement": chosen["requirement"],
            "reason": "bounded opportunity failed compiled packing: %s" % exc,
        }
    if chosen["source_id"] not in _selected_bullet_ids(packed):
        return original, {
            "attempted": True, "status": "rejected",
            "source_id": chosen["source_id"], "requirement": chosen["requirement"],
            "reason": "page packer did not retain the target opportunity",
        }
    candidate_root = run_dir / "candidate"
    candidate_tex = render_plan(packed, catalog, repo_root())
    candidate_root.mkdir(parents=True, exist_ok=True)
    (candidate_root / "resume.tex").write_text(candidate_tex)
    compiled = compile_resume(candidate_root)
    candidate_layout = pdf_layout(candidate_root, compiled, plan=packed)
    compactions: List[Dict[str, Any]] = []
    if not (candidate_layout.get("horizontal") or {}).get("pass"):
        compacted, compact_layout, compactions = compact_plan_to_geometry(
            packed, candidate_layout, catalog, run_dir / "geometry",
        )
        if compactions:
            packed = compacted
            candidate_tex = render_plan(packed, catalog, repo_root())
            (candidate_root / "resume.tex").write_text(candidate_tex)
            compiled = compile_resume(candidate_root)
            candidate_layout = pdf_layout(candidate_root, compiled, plan=packed)
    safe = bool(
        candidate_layout.get("compiled")
        and candidate_layout.get("pages") == 1
        and not candidate_layout.get("overfull")
        and (candidate_layout.get("horizontal") or {}).get("pass")
        and chosen["source_id"] in _selected_bullet_ids(packed)
    )
    original_ids = _selected_bullet_ids(original)
    packed_ids = _selected_bullet_ids(packed)
    actual_added = sorted(packed_ids - original_ids)
    actual_removed = sorted(original_ids - packed_ids)
    receipt = {
        "attempted": True,
        "status": "applied" if safe else "rejected",
        "source_id": chosen["source_id"],
        "entry_id": chosen["entry_id"],
        "requirement": chosen["requirement"],
        "importance": chosen["importance"],
        "target_signal": chosen["target_signal"],
        "source": chosen["source"],
        "score": round(chosen["score"], 1),
        "requested_removed_source_id": removal[1] if removal else "",
        "removed_source_id": actual_removed[0] if len(actual_removed) == 1 else "",
        "actual_added_source_ids": actual_added,
        "actual_removed_source_ids": actual_removed,
        "removed_text": removed_text,
        "compactions": compactions,
        "packing": packing,
        "layout": {
            "compiled": candidate_layout.get("compiled"),
            "pages": candidate_layout.get("pages"),
            "near_wrap_count": (candidate_layout.get("horizontal") or {}).get("near_wrap_count", 0),
            "horizontal_pass": bool((candidate_layout.get("horizontal") or {}).get("pass")),
        },
        "reason": (
            "Applied one compiled, source-verbatim opportunity replacement before sealed judging."
            if safe else
            "Rejected the opportunity because the final compiled geometry gate was unsafe."
        ),
    }
    if not safe:
        return original, receipt
    write_json(run_dir / "receipt.json", receipt)
    return packed, receipt


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
    """Apply bounded source-aware curation before compile-time packing.

    The model still owns the role thesis, evidence ranking, and wording. This
    last-mile pass only enforces a human skim budget that geometry cannot
    express: no fifth project, no oversized experience block, and no portfolio
    expansion that displaces all of the canonical project proof. Every removal
    is recorded in ``portfolio_budget`` so the evaluator can distinguish a
    deliberate budget decision from lost evidence.
    """
    curated = copy.deepcopy(candidate_plan)
    catalog = catalog or {}
    seen_texts: List[str] = []
    actions: List[Dict[str, Any]] = []
    for section in ("experiences", "projects", "leadership"):
        retained_entries = []
        for entry in curated.get(section, []):
            retained = []
            entry_seen_texts: List[str] = []
            for bullet in entry.get("bullets", []):
                text = str(bullet.get("text") or "")
                same_entry_index = next(
                    (
                        index for index, existing in enumerate(entry_seen_texts)
                        if _same_entry_resume_bullet(text, existing)
                    ),
                    None,
                )
                global_duplicate = any(
                    _same_resume_bullet(text, existing)
                    or _same_cross_entry_resume_bullet(text, existing)
                    for existing in seen_texts
                )
                if global_duplicate or same_entry_index is not None:
                    replaced_existing = None
                    if same_entry_index is not None:
                        existing_bullet = retained[same_entry_index]
                        if _control_bullet_value(bullet, catalog) > _control_bullet_value(existing_bullet, catalog):
                            replaced_existing = existing_bullet
                            retained[same_entry_index] = bullet
                            old_text = entry_seen_texts[same_entry_index]
                            entry_seen_texts[same_entry_index] = text
                            seen_texts[:] = [text if value == old_text else value for value in seen_texts]
                    if replaced_existing is not None:
                        removed_source_id = str(replaced_existing.get("source_id") or "")
                        duplicate_of = str(bullet.get("source_id") or "")
                    else:
                        removed_source_id = str(bullet.get("source_id") or "")
                        duplicate_of = str(
                            retained[same_entry_index].get("source_id") or ""
                        ) if same_entry_index is not None else ""
                    actions.append({
                        "kind": "near_duplicate",
                        "section": section,
                        "entry_id": str(entry.get("source_id") or ""),
                        "source_id": removed_source_id,
                        "reason": (
                            "replaced a weaker repeated claim with a stronger source-addressed line"
                            if replaced_existing is not None
                            else "removed a repeated claim or repeated proof anchors before layout packing"
                        ),
                        "duplicate_of": duplicate_of,
                    })
                    if replaced_existing is None:
                        continue
                    # The new line is already retained and its provenance is
                    # represented in the replacement action; do not append it
                    # a second time below.
                    continue
                retained.append(bullet)
                seen_texts.append(text)
                entry_seen_texts.append(text)
            if retained:
                entry["bullets"] = retained
                retained_entries.append(entry)
        curated[section] = retained_entries

    entries = catalog.get("entries") or {}

    # The first pass above catches obvious repeats while it walks the source
    # order. A writer/revision can phrase the same measured story differently
    # enough that it survives that pass, though. Re-check each entry as a
    # bounded semantic duplicate set and remove the lower-value member. This
    # is deliberately limited to one entry: two different entries may share a
    # broad capability without being the same interview story.
    for section in ("experiences", "projects", "leadership"):
        for entry in curated.get(section, []) or []:
            bullets = entry.get("bullets", []) or []
            while True:
                duplicate_pair: Optional[Tuple[int, int]] = None
                for left_index, left in enumerate(bullets):
                    for right_index in range(left_index + 1, len(bullets)):
                        right = bullets[right_index]
                        if _same_entry_resume_bullet(
                            str(left.get("text") or ""),
                            str(right.get("text") or ""),
                        ):
                            duplicate_pair = (left_index, right_index)
                            break
                    if duplicate_pair is not None:
                        break
                if duplicate_pair is None:
                    break
                left_index, right_index = duplicate_pair
                left, right = bullets[left_index], bullets[right_index]
                # Keep the stronger source-addressed line. The score is only
                # used to choose which duplicate to remove; it never creates
                # or rewrites evidence.
                left_score = _control_bullet_value(left, catalog)
                right_score = _control_bullet_value(right, catalog)
                remove_index = right_index if left_score >= right_score else left_index
                removed = bullets.pop(remove_index)
                actions.append({
                    "kind": "semantic_duplicate",
                    "section": section,
                    "entry_id": str(entry.get("source_id") or ""),
                    "source_id": str(removed.get("source_id") or ""),
                    "reason": (
                        "removed the lower-value member of a same-entry repeated metric/mechanism story"
                    ),
                    "duplicate_of": str(
                        (left if remove_index == right_index else right).get("source_id") or ""
                    ),
                })

    def source_is_canonical(source_id: str) -> bool:
        source = entries.get(str(source_id).split(":b", 1)[0], {})
        # Entry-level lookup above is not sufficient for all catalog shapes;
        # use the exact bullet source when available.
        for bullet in source.get("bullets", []) or []:
            if str(bullet.get("id") or "") == str(source_id):
                return _is_canonical_source(bullet.get("source"))
        for entry in entries.values():
            for bullet in entry.get("bullets", []) or []:
                if str(bullet.get("id") or "") == str(source_id):
                    return _is_canonical_source(bullet.get("source"))
        return False

    def bullet_value(bullet: Dict[str, Any]) -> float:
        # _control_bullet_value is defined later in the module but is present
        # by the time this runtime path is called. Keep the fallback for small
        # isolated tests that import this helper with a fixture catalog.
        try:
            return _control_bullet_value(bullet, catalog)
        except NameError:
            return _bullet_value(bullet)

    def entry_value(entry: Dict[str, Any]) -> float:
        bullets = entry.get("bullets", []) or []
        return sum(bullet_value(bullet) for bullet in bullets) / max(1, len(bullets) + 1)

    def remove_bullet(section: str, entry_index: int, bullet_index: int, reason: str) -> None:
        entry = curated[section][entry_index]
        removed = entry.get("bullets", []).pop(bullet_index)
        actions.append({
            "kind": "skim_budget",
            "section": section,
            "entry_id": str(entry.get("source_id") or ""),
            "source_id": str(removed.get("source_id") or ""),
            "reason": reason,
            "canonical_source": source_is_canonical(str(removed.get("source_id") or "")),
        })

    def project_entry_is_canonical(entry: Dict[str, Any], canonical_ids: set) -> bool:
        return str(entry.get("source_id") or "") in canonical_ids

    # A new project is allowed to replace a canonical one only when the model
    # selects four or fewer projects. If it expands beyond the control roster,
    # remove the extra non-canonical entry first. This specifically prevents a
    # health/keyword-adjacent fifth project from crowding out stronger proof.
    canonical_project_ids = {
        str(item.get("source_id") or "")
        for item in (canonical_resume_benchmark(catalog).get("projects") or [])
        if item.get("source_id")
    }
    while len(curated.get("projects", [])) > HUMAN_PORTFOLIO_CAPS["project_entries"]:
        projects = curated["projects"]
        noncanonical = [
            (index, entry) for index, entry in enumerate(projects)
            if not project_entry_is_canonical(entry, canonical_project_ids)
        ]
        pool = noncanonical or list(enumerate(projects))
        index, entry = min(
            pool,
            key=lambda item: (entry_value(item[1]), str(item[1].get("source_id") or "")),
        )
        removed = projects.pop(index)
        actions.append({
            "kind": "project_roster_cap",
            "section": "projects",
            "entry_id": str(removed.get("source_id") or ""),
            "source_id": str(removed.get("source_id") or ""),
            "reason": (
                "removed non-canonical project expansion to preserve a four-project skim budget"
                if noncanonical else
                "removed lowest-value project to preserve a four-project skim budget"
            ),
            "canonical_source": project_entry_is_canonical(removed, canonical_project_ids),
        })

    # Keep each experience legible as one coherent interview thread. Prefer
    # dropping a non-canonical/low-value line; do not delete the last bullet.
    for entry_index, entry in enumerate(curated.get("experiences", []) or []):
        while len(entry.get("bullets", []) or []) > HUMAN_PORTFOLIO_CAPS["experience_bullets_per_entry"]:
            candidates = list(enumerate(entry.get("bullets", []) or []))
            noncanonical = [
                item for item in candidates
                if not source_is_canonical(str(item[1].get("source_id") or ""))
            ]
            pool = noncanonical or candidates
            bullet_index, _ = min(
                pool,
                key=lambda item: (bullet_value(item[1]), str(item[1].get("source_id") or "")),
            )
            remove_bullet(
                "experiences", entry_index, bullet_index,
                "trimmed experience entry to the human skim budget",
            )

    # Project entries are the most common place for a model to add breadth
    # without adding a new interview thread. Keep a bounded project budget and
    # favor canonical, mechanism-bearing lines when trimming.
    def project_bullet_count() -> int:
        return sum(len(entry.get("bullets", []) or []) for entry in curated.get("projects", []) or [])

    while project_bullet_count() > HUMAN_PORTFOLIO_CAPS["project_bullets"]:
        candidates: List[Tuple[int, int, Dict[str, Any]]] = []
        for entry_index, entry in enumerate(curated.get("projects", []) or []):
            bullets = entry.get("bullets", []) or []
            if len(bullets) <= 1:
                continue
            heading = _latex_plain(str((entries.get(str(entry.get("source_id") or "")) or {}).get("heading") or ""))
            for bullet_index, bullet in enumerate(bullets):
                text = _latex_plain(str(bullet.get("text") or ""))
                # A repeated award line is the safest first trim when the
                # project heading already carries the same recognition.
                award_duplicate = bool(
                    re.search(r"1st place|best security|overall|hack", heading, re.I)
                    and re.search(r"\b(?:won|selected|place|competitors?)\b", text, re.I)
                )
                score = bullet_value(bullet)
                if award_duplicate:
                    score -= 30.0
                if source_is_canonical(str(bullet.get("source_id") or "")):
                    score += 18.0
                candidates.append((entry_index, bullet_index, {"bullet": bullet, "score": score}))
        if not candidates:
            break
        entry_index, bullet_index, _ = min(
            candidates,
            key=lambda item: (item[2]["score"], str(item[2]["bullet"].get("source_id") or "")),
        )
        remove_bullet(
            "projects", entry_index, bullet_index,
            "trimmed lowest-marginal-value project line to the project skim budget",
        )

    # Finally cap the whole tailored artifact. Prefer project/leadership lines
    # over chronological experience, then prefer non-canonical evidence. This
    # is a space allocation rule, not a claim that the omitted fact is false.
    def total_bullet_count() -> int:
        return sum(
            len(entry.get("bullets", []) or [])
            for section in ("experiences", "projects", "leadership")
            for entry in curated.get(section, []) or []
        )

    while total_bullet_count() > HUMAN_PORTFOLIO_CAPS["total_bullets"]:
        candidates = []
        for section_rank, section in enumerate(("leadership", "projects", "experiences")):
            for entry_index, entry in enumerate(curated.get(section, []) or []):
                bullets = entry.get("bullets", []) or []
                if len(bullets) <= 1:
                    continue
                for bullet_index, bullet in enumerate(bullets):
                    score = bullet_value(bullet)
                    if source_is_canonical(str(bullet.get("source_id") or "")):
                        score += 18.0
                    candidates.append((
                        section_rank, score, section, entry_index, bullet_index,
                        str(bullet.get("source_id") or ""),
                    ))
        if not candidates:
            break
        _, _, section, entry_index, bullet_index, _ = min(
            candidates, key=lambda item: (item[0], item[1], item[5])
        )
        remove_bullet(
            section, entry_index, bullet_index,
            "trimmed lowest-marginal-value evidence to the total human skim budget",
        )

    curated["portfolio_budget"] = {
        "version": HUMAN_PORTFOLIO_POLICY_VERSION,
        "caps": dict(HUMAN_PORTFOLIO_CAPS),
        "actions": actions,
        "decision": (
            "Applied source-aware human skim budget before compiled packing."
            if actions else "Candidate already fit the source-aware human skim budget."
        ),
    }

    return curated


def deterministic_final_portfolio_guard(
    plan: Dict[str, Any], catalog: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Apply the last deterministic skim/duplicate guard to the exact draft.

    Curation normally happens before packing, but later revision, density, and
    repair passes can introduce a repeated source story after that checkpoint.
    This guard is intentionally not a new author: it only applies the existing
    source-aware curation policy and chronology rule. Callers must compile the
    returned plan and, when it changes, run a fresh sealed panel against the
    resulting artifact.
    """
    before_signature = _plan_source_signature(plan)
    guarded = enforce_experience_order(curate_candidate_portfolio(plan, catalog), catalog)
    after_signature = _plan_source_signature(guarded)
    budget = guarded.get("portfolio_budget") if isinstance(guarded, dict) else {}
    actions = list((budget or {}).get("actions") or []) if isinstance(budget, dict) else []
    changed = before_signature != after_signature
    return guarded, {
        "attempted": True,
        "changed": changed,
        "before_bullet_count": sum(len(entry.get("bullets", []) or []) for section in ("experiences", "projects", "leadership") for entry in plan.get(section, []) or []),
        "after_bullet_count": sum(len(entry.get("bullets", []) or []) for section in ("experiences", "projects", "leadership") for entry in guarded.get(section, []) or []),
        "actions": actions,
        "removed_source_ids": [
            str(item.get("source_id") or "") for item in actions
            if str(item.get("source_id") or "")
        ],
        "reason": (
            "Applied final source-aware duplicate/skim guard; exact artifact requires fresh sealed review."
            if changed else "Final artifact already satisfied the source-aware duplicate/skim guard."
        ),
    }


def portfolio_metrics(plan: Dict[str, Any]) -> Dict[str, Any]:
    counts = {
        section: {
            "entries": len(plan.get(section, [])),
            "bullets": sum(len(entry.get("bullets", [])) for entry in plan.get(section, [])),
        }
        for section in ("experiences", "projects", "leadership")
    }
    bullet_records = [
        (bullet, str(entry.get("source_id") or ""))
        for section in ("experiences", "projects", "leadership")
        for entry in plan.get(section, [])
        for bullet in entry.get("bullets", [])
    ]
    duplicates = []
    for index, (bullet, entry_id) in enumerate(bullet_records):
        for other, other_entry_id in bullet_records[index + 1 :]:
            same_entry = bool(entry_id and entry_id == other_entry_id)
            if (
                _same_resume_bullet(str(bullet.get("text") or ""), str(other.get("text") or ""))
                or (
                    same_entry
                    and _same_entry_resume_bullet(
                        str(bullet.get("text") or ""),
                        str(other.get("text") or ""),
                    )
                )
            ):
                duplicates.append([bullet.get("source_id"), other.get("source_id")])
    violations = []
    for section, cap in PORTFOLIO_CAPS.items():
        if counts[section]["entries"] > cap["entries"]:
            violations.append("%s has too many entries" % section)
        for entry_index, entry in enumerate(plan.get(section, [])):
            if len(entry.get("bullets", [])) > cap["bullets"]:
                violations.append("%s exceeds the per-entry bullet cap" % entry.get("source_id"))
    if len(bullet_records) > MAX_TOTAL_BULLETS:
        violations.append("resume exceeds %s bullets" % MAX_TOTAL_BULLETS)
    if len(bullet_records) < MIN_TOTAL_BULLETS:
        violations.append("resume needs at least %s distinct bullets" % MIN_TOTAL_BULLETS)
    if duplicates:
        violations.append("resume contains duplicate or near-duplicate bullets")
    return {
        "pass": not violations,
        "counts": counts,
        "total_bullets": len(bullet_records),
        "min_total_bullets": None,
        "max_total_bullets": MAX_TOTAL_BULLETS,
        "human_skim_budget": {
            "version": HUMAN_PORTFOLIO_POLICY_VERSION,
            "caps": dict(HUMAN_PORTFOLIO_CAPS),
            "within_budget": (
                len(bullet_records) <= HUMAN_PORTFOLIO_CAPS["total_bullets"]
                and counts["projects"]["entries"] <= HUMAN_PORTFOLIO_CAPS["project_entries"]
                and counts["projects"]["bullets"] <= HUMAN_PORTFOLIO_CAPS["project_bullets"]
                and all(
                    len(entry.get("bullets", []) or [])
                    <= HUMAN_PORTFOLIO_CAPS["experience_bullets_per_entry"]
                    for entry in plan.get("experiences", []) or []
                )
            ),
        },
        "duplicates": duplicates,
        "violations": violations,
    }


def _tailoring_term_weight(item: Dict[str, Any]) -> int:
    """Return an auditable priority weight for a posting term."""
    importance = str(item.get("importance") or "mentioned").lower()
    if item.get("required"):
        importance = "required"
    elif item.get("preferred"):
        importance = "preferred"
    return TAILORING_PRIORITY_WEIGHTS.get(importance, 1)


def _ledger_text(item: Dict[str, Any]) -> str:
    return " ".join(
        str(item.get(field) or "")
        for field in (
            "action", "current_evidence", "replacement_or_exclusion",
            "target_signal", "why_stronger", "signal_lost", "source_ids",
        )
    ).strip()


def _meaningful_label_tokens(value: Any) -> List[str]:
    tokens = list(_resume_tokens(_latex_plain(str(value or ""))))
    return [token for token in tokens if len(token) >= 3 and token not in {
        "the", "and", "for", "with", "from", "into", "using", "built",
        "project", "experience", "resume", "work",
    }]


def _ledger_explains_removed_evidence(
    removed: Dict[str, Any], ledger: List[Dict[str, Any]], entries: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Find a source-grounded decision explaining an omitted canonical line.

    A canonical resume is a benchmark, not a preservation contract.  The old
    audit treated every omitted line as a loss, which punished explicit,
    evidence-backed project swaps.  This matcher is intentionally conservative:
    a bullet-level omission must name the exact source ID (or quote enough of
    the omitted line to identify it). Merely naming the project or saying that
    projects were reordered is not enough, because that can hide a lost
    mechanism inside an otherwise valid project-level tradeoff.
    """
    source_id = str(removed.get("source_id") or "")
    for item in ledger:
        text = _ledger_text(item).lower()
        if source_id and source_id.lower() in text:
            return item
        removed_tokens = _meaningful_label_tokens(removed.get("text") or "")
        if removed_tokens:
            matches = sum(token in text for token in removed_tokens)
            # Require a recognizable portion of the actual omitted line, not
            # just its parent heading. Short lines need three tokens; longer
            # lines need roughly half of their distinctive tokens.
            threshold = max(3, int((len(removed_tokens) + 1) * 0.5))
            if matches >= min(threshold, len(removed_tokens)):
                return item
    return None


def _ledger_explicitly_names_source(source_id: str, ledger: List[Dict[str, Any]]) -> bool:
    """Return whether a tradeoff names this exact source bullet ID."""
    needle = str(source_id or "").strip().lower()
    if not needle:
        return False
    return any(
        needle in _ledger_text(item).lower()
        for item in ledger
        if isinstance(item, dict)
    )


def content_change_report(
    plan: Dict[str, Any], catalog: Dict[str, Any], tex: str,
    keyword_strategy: Optional[Dict[str, Any]] = None,
    base_tex: str = "",
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
                source_terms = [
                    str(item.get("term") or "")
                    for item in (keyword_strategy or {}).get("terms", [])
                    if str(item.get("term") or "") and _keyword_present(str(item.get("term") or ""), source_text)
                ]
                final_terms = [
                    str(item.get("term") or "")
                    for item in (keyword_strategy or {}).get("terms", [])
                    if str(item.get("term") or "") and _keyword_present(str(item.get("term") or ""), final_text)
                ]
                if final_text != source_text and _low_value_rewrite(source_text, final_text):
                    suppressed_rewrites.append({
                        "section": section,
                        "source_id": source_id,
                        "source_text": source_text,
                        "final_text": final_text,
                        "reason": "near-copy without a new metric, technical term, or target signal",
                        "added_supported_terms": sorted(set(final_terms) - set(source_terms)),
                        "dropped_supported_terms": sorted(set(source_terms) - set(final_terms)),
                    })
                elif final_text != source_text or len(supporting) > 1:
                    rewritten.append({
                        "section": section,
                        "source_id": source_id,
                        "source_text": source_text,
                        "final_text": final_text,
                        "source_ids": supporting or [source_id],
                        "rationale": str(bullet.get("candidate_rationale") or ""),
                        "source_terms": sorted(set(source_terms)),
                        "final_terms": sorted(set(final_terms)),
                        "added_supported_terms": sorted(set(final_terms) - set(source_terms)),
                        "dropped_supported_terms": sorted(set(source_terms) - set(final_terms)),
                        "numeric_anchors_preserved": _resume_numeric_anchors(source_text).issubset(
                            _resume_numeric_anchors(final_text)
                        ),
                    })

    keyword_terms = []
    rendered_text = _latex_plain(tex)
    base_rendered_text = _latex_plain(base_tex) if base_tex else ""
    for item in (keyword_strategy or {}).get("terms", []):
        term = str(item.get("term") or "")
        if not term:
            continue
        supported = bool(item.get("supported"))
        rendered = _keyword_present(term, rendered_text)
        base_rendered = _keyword_present(term, base_rendered_text) if base_rendered_text else None
        status = (
            "covered" if supported and rendered
            else "missing" if supported
            else "unverified_rendered" if rendered
            else "unsupported"
        )
        comparison_status = "unknown"
        if base_rendered is not None:
            comparison_status = (
                "gained" if rendered and not base_rendered else
                "lost" if base_rendered and not rendered else
                "retained" if rendered and base_rendered else
                "absent"
            )
        keyword_terms.append({
            "term": term,
            "required": bool(item.get("required")),
            "preferred": bool(item.get("preferred")),
            "responsibility": bool(item.get("responsibility")),
            "importance": str(item.get("importance") or "mentioned"),
            "supported": supported,
            "support_kind": str(item.get("support_kind") or ("exact" if supported else "none")),
            "rendered": rendered,
            "base_rendered": base_rendered,
            "comparison_status": comparison_status,
            "status": status,
            "source_ids": list(item.get("source_ids") or [])[:6],
            "priority_weight": _tailoring_term_weight(item),
            "selected_evidence_ids": [
                source_id for source_id in (item.get("source_ids") or [])
                if str(source_id) in set(selected_bullet_ids)
            ][:6],
        })
    supported_terms = [item for item in keyword_terms if item["supported"]]
    covered_terms = [item for item in supported_terms if item["rendered"]]
    required_terms = [item for item in keyword_terms if item["required"]]
    required_covered = [item for item in required_terms if item["supported"] and item["rendered"]]
    unverified_rendered = [item for item in keyword_terms if item["status"] == "unverified_rendered"]
    base_supported_terms = [item for item in keyword_terms if item["supported"] and item.get("base_rendered")]
    gained_terms = [item for item in keyword_terms if item.get("comparison_status") == "gained"]
    lost_terms = [item for item in keyword_terms if item.get("comparison_status") == "lost"]
    keyword_coverage = {
        "posting_available": bool((keyword_strategy or {}).get("posting_available")),
        "reason": str((keyword_strategy or {}).get("reason") or ""),
        "supported_count": len(supported_terms),
        "covered_count": len(covered_terms),
        "detected_count": len(keyword_terms),
        "exact_coverage_percent": round(100 * len(covered_terms) / max(1, len(keyword_terms))),
        "supported_exact_coverage_percent": round(100 * len(covered_terms) / max(1, len(supported_terms))),
        "required_count": len(required_terms),
        "required_supported_count": sum(item["supported"] for item in required_terms),
        "required_covered_count": len(required_covered),
        "required_coverage_percent": round(100 * len(required_covered) / max(1, len(required_terms))),
        "unverified_rendered_count": len(unverified_rendered),
        "unverified_rendered_terms": [item["term"] for item in unverified_rendered],
        "base_available": bool(base_rendered_text),
        "base_supported_count": len(base_supported_terms),
        "gained_count": len(gained_terms),
        "gained_terms": [item["term"] for item in gained_terms],
        "lost_count": len(lost_terms),
        "lost_terms": [item["term"] for item in lost_terms],
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
            "entry_label": _project_heading(
                (entries.get(str(item.get("entry_id") or "")) or {}).get("heading")
            ) or str(
                (entries.get(str(item.get("entry_id") or "")) or {}).get("company")
                or (entries.get(str(item.get("entry_id") or "")) or {}).get("role")
                or item.get("entry_id") or ""
            ),
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
    decision_ledger = []
    for raw in (plan.get("decision_ledger") or []):
        if not isinstance(raw, dict):
            continue
        record = {
            field: str(raw.get(field) or "")
            for field in (
                "action", "current_evidence", "replacement_or_exclusion",
                "target_signal", "why_stronger", "signal_lost",
            )
        }
        record["source_ids"] = [
            source_id for source_id in (
                list(raw.get("source_ids") or [])
                + re.findall(r"(?:experience|project|leadership):[a-z0-9_.-]+(?::b\d+)?", _ledger_text(raw), re.I)
            )
            if str(source_id)
        ][:12]
        decision_ledger.append(record)
    explained_tradeoffs = []
    for item in removed_bullets:
        match = _ledger_explains_removed_evidence(item, decision_ledger, entries)
        if match:
            item["tradeoff_status"] = "explained"
            item["tradeoff_reason"] = str(match.get("why_stronger") or match.get("replacement_or_exclusion") or "")[:500]
            item["tradeoff_action"] = str(match.get("action") or "")[:300]
            explained_tradeoffs.append({
                "source_id": item.get("source_id", ""),
                "entry_id": item.get("entry_id", ""),
                "reason": item.get("tradeoff_reason", ""),
            })
        else:
            item["tradeoff_status"] = "unexplained"
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
        "decision_ledger": decision_ledger[:40],
        "explained_tradeoffs": explained_tradeoffs[:40],
        "unexplained_removed_bullets": [
            item for item in removed_bullets
            if item.get("tradeoff_status") != "explained"
        ][:40],
        "front_matter_policy": front_matter_policy,
        "removed_front_matter": [
            field for field, state in front_matter_policy.items() if state == "omit"
        ],
        "front_matter_rewrites": [
            {
                "line_id": str(item.get("line_id") or ""),
                "source_text": _latex_plain(str(item.get("source_text") or "")),
                "final_text": _latex_plain(str(item.get("text") or "")),
                "evidence_ids": list(item.get("evidence_ids") or []),
                "why": str(item.get("why") or ""),
            }
            for item in (plan.get("front_matter_rewrites") or [])
            if isinstance(item, dict)
        ],
        "portfolio_diagnostics": portfolio_diagnostics(plan, catalog),
        "keyword_coverage": keyword_coverage,
        "base_text_hash": _stable_digest(base_tex) if base_tex else "",
        "tailored_text_hash": _stable_digest(tex),
    }


def candidate_delta_summary(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Describe whether a candidate materially differs from the base control.

    A rendered PDF can differ from the immutable control because a line
    compactor removed a word or changed punctuation.  Those changes should be
    visible in the diff, but they are not a reason to launch a twelve-minute
    semantic repair pass.  Conversely, a new supported term, source-backed
    rewrite, project swap, or evidence replacement is material even when the
    page still contains the same number of bullets.
    """
    changes = changes if isinstance(changes, dict) else {}
    reasons: List[str] = []

    def mark(reason: str) -> None:
        if reason and reason not in reasons:
            reasons.append(reason)

    if changes.get("added_bullets"):
        mark("new source-backed evidence line(s)")
    if changes.get("removed_canonical_bullets"):
        mark("canonical evidence line(s) removed or replaced")
    swaps = changes.get("project_swaps") if isinstance(changes.get("project_swaps"), dict) else {}
    if swaps.get("swapped_in") or swaps.get("swapped_out"):
        mark("project portfolio changed")
    order = changes.get("experience_order") if isinstance(changes.get("experience_order"), dict) else {}
    if order.get("changed"):
        mark("experience order changed")
    if changes.get("removed_front_matter"):
        mark("front matter removed")
    for rewrite in changes.get("front_matter_rewrites") or []:
        if not isinstance(rewrite, dict):
            continue
        if str(rewrite.get("source_text") or "") != str(rewrite.get("final_text") or ""):
            mark("evidence-backed Skills rewrite")
    coverage = changes.get("keyword_coverage") if isinstance(changes.get("keyword_coverage"), dict) else {}
    if coverage.get("gained_terms"):
        mark("supported target terminology surfaced")
    if coverage.get("lost_terms"):
        mark("supported target terminology lost")
    for term in coverage.get("terms") or []:
        if not isinstance(term, dict):
            continue
        if term.get("comparison_status") == "gained":
            mark("supported target terminology surfaced")
        elif term.get("comparison_status") == "lost":
            mark("supported target terminology lost")

    for rewrite in changes.get("rewritten_bullets") or []:
        if not isinstance(rewrite, dict):
            continue
        source_id = str(rewrite.get("source_id") or "")
        supporting = {
            str(value) for value in (rewrite.get("source_ids") or []) if str(value)
        }
        if supporting - {source_id}:
            mark("bullet synthesized from multiple authorized sources")
            continue
        if rewrite.get("added_supported_terms") or rewrite.get("dropped_supported_terms"):
            mark("supported target terminology changed in a bullet")
            continue
        source_text = str(rewrite.get("source_text") or "")
        final_text = str(rewrite.get("final_text") or "")
        if _resume_numeric_anchors(source_text) != _resume_numeric_anchors(final_text):
            mark("quantified proof changed")
            continue
        # Treat a close, same-signal-family edit as layout/editorial churn.
        # A materially different sentence remains eligible for repair even if
        # it did not happen to contain one of the known posting terms.
        source_families = set(_portfolio_signal_families(source_text))
        final_families = set(_portfolio_signal_families(final_text))
        if (
            _resume_text_similarity(source_text, final_text) < 0.76
            or source_families != final_families
        ):
            mark("material source-addressed wording rewrite")

    return {
        "material": bool(reasons),
        "status": "material" if reasons else "unchanged",
        "reasons": reasons[:12],
    }


def has_material_candidate_delta(changes: Dict[str, Any]) -> bool:
    """Return whether a candidate earned a semantic comparison/repair pass."""
    return bool(candidate_delta_summary(changes).get("material"))


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


def portfolio_search_variant_cards(
    job: Optional[Dict[str, Any]] = None, limit: int = 3,
) -> List[Dict[str, Any]]:
    """Return stable, role-agnostic editorial hypotheses for search runs.

    The cards are intentionally not selected from keyword overlap.  They are
    diversity controls for the author search: one conservative control pass,
    one product/integration pass, and one systems/technical-conviction pass.
    The target posting and evidence graph remain the authority inside each
    child run.
    """
    del job  # The cards are fixed to keep cross-job experiments comparable.
    count = max(1, min(int(limit or 3), len(PORTFOLIO_SEARCH_VARIANTS)))
    return [copy.deepcopy(item) for item in PORTFOLIO_SEARCH_VARIANTS[:count]]


def portfolio_search_candidate_summary(
    variant: Dict[str, Any], report: Optional[Dict[str, Any]],
    elapsed_seconds: float = 0.0, error: str = "",
) -> Dict[str, Any]:
    """Summarize one search child without inventing a composite quality score."""
    report = report if isinstance(report, dict) else {}
    audit = report.get("tailoring_audit") if isinstance(report.get("tailoring_audit"), dict) else {}
    panel = report.get("critic_panel") if isinstance(report.get("critic_panel"), dict) else {}
    recommendation = str(audit.get("recommended_version") or "review").lower()
    winner = str(report.get("winner_version") or "base").lower()
    complete_panel = bool(
        audit.get("review", {}).get("available")
        if isinstance(audit.get("review"), dict) else False
    ) and bool(panel.get("all_required_roles"))
    # The comparative audit and the critic-jury readiness gates answer
    # different questions.  A jury can prefer the tailored draft while still
    # marking a non-averagable quality gate (for example distinctiveness) as a
    # hard failure.  Portfolio search must never promote that candidate just
    # because its aggregate audit says ``prefer_tailored``.
    critic_hard_fail = bool(
        isinstance(report.get("review"), dict)
        and report["review"].get("hard_fail") is True
    )
    eligible = bool(
        winner == "tailored"
        and recommendation == "tailored"
        and complete_panel
        and not critic_hard_fail
        and str(audit.get("decision") or "") == "prefer_tailored"
    )
    return {
        "variant_id": str(variant.get("id") or ""),
        "variant_label": str(variant.get("label") or ""),
        "run_dir": str(variant.get("run_dir") or ""),
        "ok": bool(report),
        "error": str(error or "")[:500],
        "elapsed_seconds": round(float(elapsed_seconds or 0.0), 1),
        "winner_version": winner,
        "recommended_version": recommendation,
        "audit_status": str(audit.get("status") or "unknown"),
        "tailoring": str(audit.get("tailoring") or "unknown"),
        "decision": str(audit.get("decision") or ""),
        "complete_panel": complete_panel,
        "critic_hard_fail": critic_hard_fail,
        "eligible_positive_win": eligible,
        "preference_key": list(tailoring_audit_preference_key(audit)),
        "finding_counts": dict(audit.get("finding_counts") or {}),
        "run_id": str(report.get("run_id") or ""),
    }


def _copy_if_present(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _write_resume_text_from_pdf(pdf: Path, target: Path) -> None:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext or not pdf.is_file():
        return
    try:
        text = subprocess.check_output([pdftotext, str(pdf), "-"], timeout=30, text=True)
    except (OSError, subprocess.SubprocessError):
        return
    target.write_text(text)


def run_portfolio_search(
    run_dir: Path, job: Dict[str, Any], update, *, enhance: bool,
    unrestricted: bool = False, generation: bool = False,
    comparison_control: Any = None,
) -> None:
    """Run several complete author/jury candidates and publish only a win.

    This is deliberately a wrapper around the existing single-candidate path,
    rather than a second evaluator. Every child gets its own prompt, render,
    source audit, and sealed four-role panel. A child can become the published
    candidate only when the existing comparative audit says ``prefer_tailored``
    and every required critic role completed. Otherwise the immutable base is
    the parent run's primary artifact.
    """
    started = time.time()
    parent_run_id = str(job.get("_resume_studio_run_id") or uuid.uuid4().hex[:12])
    queue_id = str(job.get("_resume_studio_queue_id") or "")
    parent_control = resolve_comparison_control(repo_root(), comparison_control)
    write_json(run_dir / "comparison_control.json", comparison_control_summary(parent_control))
    cards = portfolio_search_variant_cards(job, limit=QUALITY_PROFILES["search"].get("candidate_variants", 3))
    search_root = run_dir / "portfolio_search"
    search_root.mkdir(parents=True, exist_ok=True)
    update("searching", "Generating %s independent evidence portfolios before judging them" % len(cards))

    def run_child(card: Dict[str, Any]) -> Dict[str, Any]:
        child_started = time.time()
        variant_id = str(card.get("id") or "variant")
        child_dir = search_root / variant_id
        child_dir.mkdir(parents=True, exist_ok=True)
        # Child receipts use the same twelve-hex run-id shape as ordinary
        # Studio runs, while remaining stable and distinct inside a parent.
        child_run_id = hashlib.sha256(
            (parent_run_id + ":" + variant_id).encode("utf-8")
        ).hexdigest()[:12]
        child_job = copy.deepcopy(job)
        child_job["_resume_studio_run_id"] = child_run_id
        child_job["_resume_studio_queue_id"] = queue_id
        write_json(child_dir / "status.json", {
            "run_id": child_run_id, "status": "queued", "step": "queued",
            "message": "Portfolio-search child queued", "variant_id": variant_id,
            "variant_label": card.get("label"), "created_at": now_iso(),
        })

        def child_update(step: str, message: str, **extra: Any) -> None:
            current = read_json(child_dir / "status.json", {}) or {}
            current.update({
                "run_id": child_run_id,
                "status": "awaiting_review" if step == "awaiting_review" else "running",
                "step": step, "message": message, "updated_at": now_iso(),
            })
            current.update(extra)
            write_json(child_dir / "status.json", current)

        try:
            run_tailoring(
                child_dir, child_job, child_update, enhance=enhance,
                unrestricted=unrestricted, generation=generation,
                quality_profile="search_single",
                variant_instruction=str(card.get("instruction") or ""),
                _search_child=True,
            )
            report = read_json(child_dir / "report.json", {}) or {}
            record = portfolio_search_candidate_summary(
                {**card, "run_dir": str(child_dir)}, report,
                elapsed_seconds=time.time() - child_started,
            )
            record["report_path"] = str(child_dir / "report.json")
            return record
        except Exception as exc:  # noqa: BLE001 - preserve one failed child
            trace = traceback.format_exc()
            (child_dir / "error.log").write_text(trace)
            write_json(child_dir / "status.json", {
                "run_id": child_run_id, "status": "failed", "step": "error",
                "message": str(exc)[:500], "error_log": "error.log",
                "finished_at": now_iso(), "updated_at": now_iso(),
            })
            return portfolio_search_candidate_summary(
                {**card, "run_dir": str(child_dir)}, None,
                elapsed_seconds=time.time() - child_started, error=str(exc),
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(cards))) as pool:
        futures = [pool.submit(run_child, card) for card in cards]
        records = [future.result() for future in futures]
    variant_order = {
        str(card.get("id") or ""): index
        for index, card in enumerate(cards)
    }
    records.sort(key=lambda item: variant_order.get(str(item.get("variant_id") or ""), 999))

    complete_wins = [item for item in records if item.get("eligible_positive_win")]
    complete_wins.sort(
        key=lambda item: (tuple(item.get("preference_key") or []), -float(item.get("elapsed_seconds") or 0.0)),
        reverse=True,
    )
    selected = complete_wins[0] if complete_wins else None
    diagnostic_candidates = [item for item in records if item.get("ok")]
    diagnostic_candidates.sort(
        key=lambda item: (tuple(item.get("preference_key") or []), item.get("complete_panel"), -float(item.get("elapsed_seconds") or 0.0)),
        reverse=True,
    )
    diagnostic = diagnostic_candidates[0] if diagnostic_candidates else None
    chosen_record = selected or diagnostic
    selected_dir = Path(str(chosen_record.get("run_dir") or "")) if chosen_record else None
    selected_report = read_json(selected_dir / "report.json", {}) if selected_dir else {}
    selected_report = selected_report if isinstance(selected_report, dict) else {}
    parent_pdf = run_pdf_path(run_dir)
    parent_pdf.parent.mkdir(parents=True, exist_ok=True)
    winner_version = "tailored" if selected else "base"
    copied_candidate = False

    if selected and selected_dir:
        source_pdf = run_pdf_path(selected_dir)
        _copy_if_present(source_pdf, parent_pdf)
        for name in ("resume.tex", "resume.txt"):
            _copy_if_present(selected_dir / name, run_dir / name)
        _copy_if_present(run_preview_path(selected_dir), run_preview_path(run_dir))
    else:
        canonical_pdf = cv_root(repo_root()) / CANONICAL_PDF
        canonical_tex = cv_root(repo_root()) / CANONICAL_TEMPLATE
        _copy_if_present(canonical_pdf, parent_pdf)
        _copy_if_present(canonical_tex, run_dir / "resume.tex")
        _write_resume_text_from_pdf(parent_pdf, run_dir / "resume.txt")
        render_preview(run_dir)

    # Keep the best rejected/generated candidate available for diagnosis, but
    # never let it become the primary artifact merely because it compiled.
    if diagnostic and selected_dir:
        diagnostic_candidate = selected_dir / "tailored_candidate.pdf"
        if diagnostic_candidate.is_file():
            copied_candidate = _copy_if_present(diagnostic_candidate, run_dir / "tailored_candidate.pdf")
            _copy_if_present(
                selected_dir / "tailored_candidate-preview.png",
                run_dir / "tailored_candidate-preview.png",
            )
    if winner_version == "base":
        _copy_if_present(cv_root(repo_root()) / CANONICAL_TEMPLATE, run_dir / "base_control.tex")

    # Copy the selected child's durable decision artifacts to the parent root;
    # all child prompts/raw jury receipts remain under portfolio_search/.
    if selected_dir:
        for name in (
            "job_context.json", "job_intelligence.json", "brief.json",
            "evidence_catalog.json", "evidence_graph_context.json",
            "content_plan.json", "candidate_plan.json", "layout_packing.json",
            "tailoring_audit.json", "critique.json", "revision_log.json",
            "post_line_density.json", "final_geometry_recovery.json",
        ):
            _copy_if_present(selected_dir / name, run_dir / name)

    if selected_report:
        report = copy.deepcopy(selected_report)
    else:
        report = {
            "mode": "generation" if generation else "unrestricted" if unrestricted else "enhanced",
            "job": job_summary(job),
            "winner_version": "base",
            "resume_match": {},
            "tailoring_audit": {
                "status": "review", "readiness": "review", "tailoring": "unknown",
                "decision": "needs_review", "recommended_version": "review",
                "review": {"available": False, "mode": "unavailable", "critic_roles": []},
                "comparison": {"uplift_band": "uncertain", "gain_weight": 0, "loss_weight": 0, "missed_opportunity_weight": 0},
                "finding_counts": {},
            },
            "review": {"ready": False, "hard_fail": False, "needs_review": True},
            "approval_state": "awaiting_review",
        }
    report["run_id"] = parent_run_id
    report["quality_profile"] = "search"
    report["winner_version"] = winner_version
    report["pdf_filename"] = parent_pdf.name
    report["approval_state"] = "awaiting_review"
    report["comparison_control"] = comparison_control_summary(
        (report.get("comparison_control") if isinstance(report.get("comparison_control"), dict) else parent_control)
    )
    report["run_metrics"] = {
        **(report.get("run_metrics") if isinstance(report.get("run_metrics"), dict) else {}),
        "portfolio_search_elapsed_seconds": round(time.time() - started, 1),
    }
    report["portfolio_search"] = {
        "version": "portfolio-search-v1",
        "candidate_count": len(cards),
        "completed_candidates": sum(1 for item in records if item.get("ok")),
        "complete_panels": sum(1 for item in records if item.get("complete_panel")),
        "positive_wins": len(complete_wins),
        "selected_variant": selected.get("variant_id") if selected else "base_control",
        "diagnostic_variant": diagnostic.get("variant_id") if diagnostic else "",
        "selection_rule": "Only a complete sealed-panel prefer_tailored result may replace the canonical base; otherwise base remains primary.",
        "candidates": records,
    }
    report["winner_artifact"] = {
        "winner_version": winner_version,
        "primary_artifact": parent_pdf.name,
        "tailored_candidate_artifact": "tailored_candidate.pdf" if copied_candidate else "",
        "base_control_artifact": parent_pdf.name if winner_version == "base" else "",
        "base_control_tex": "base_control.tex" if winner_version == "base" else "",
        "reason": (
            "A complete sealed-panel candidate won the comparative audit."
            if selected else
            "No complete candidate earned prefer_tailored; the immutable canonical base remains primary."
        ),
    }
    report["artifacts"] = list(dict.fromkeys([
        "resume.tex", parent_pdf.name, "resume.txt", run_preview_path(run_dir).name,
        "job.json", "report.json", "portfolio_search.json", "job_context.json",
        "comparison_control.json",
        "job_intelligence.json", "brief.json", "evidence_catalog.json",
        "evidence_graph_context.json", "content_plan.json", "candidate_plan.json",
        "layout_packing.json", "tailoring_audit.json", "critique.json",
        "tailored_candidate.pdf" if copied_candidate else "",
        "tailored_candidate-preview.png" if (run_dir / "tailored_candidate-preview.png").is_file() else "",
        "base_control.tex" if winner_version == "base" else "",
    ]))
    report["artifacts"] = [item for item in report["artifacts"] if item]
    write_json(run_dir / "portfolio_search.json", report["portfolio_search"])
    write_json(run_dir / "report.json", report)
    try:
        _workshop_state(run_dir, source_catalog(repo_root()))
    except (OSError, RuntimeError, ValueError):
        pass
    update(
        "awaiting_review",
        "Portfolio search complete: %s" % (
            "a sealed-panel candidate beat the canonical base" if selected else
            "the canonical base remains primary after candidate rejection"
        ),
        report=report,
    )


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
    original_rewrites = merged.get("front_matter_rewrites") or []
    edited_rewrites = {
        str(item.get("line_id") or ""): item
        for item in (edited_plan.get("front_matter_rewrites") or [])
        if isinstance(item, dict) and str(item.get("line_id") or "")
    }
    if original_rewrites:
        preserved = []
        for original in original_rewrites:
            line_id = str(original.get("line_id") or "")
            replacement = copy.deepcopy(edited_rewrites.get(line_id) or original)
            replacement["line_id"] = line_id
            replacement["evidence_ids"] = list(original.get("evidence_ids") or [])
            replacement["why"] = str(original.get("why") or "")
            replacement["source_text"] = str(original.get("source_text") or "")
            preserved.append(replacement)
        merged["front_matter_rewrites"] = preserved
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
    prefix = _apply_front_matter_rewrites(prefix, plan.get("front_matter_rewrites"), root)
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


def _apply_front_matter_rewrites(
    prefix: str, rewrites: Optional[List[Dict[str, Any]]], root: Optional[Path] = None,
) -> str:
    """Apply evidence-backed generated Skills text without changing the shell."""
    if not isinstance(rewrites, list) or not rewrites:
        return prefix
    catalog = {
        str(item.get("line_id") or ""): item
        for item in front_matter_catalog(root or repo_root())
        if str(item.get("line_id") or "").startswith("front:skills:")
    }
    for rewrite in rewrites:
        line_id = str(rewrite.get("line_id") or "") if isinstance(rewrite, dict) else ""
        item = catalog.get(line_id)
        text = str(rewrite.get("text") or "") if isinstance(rewrite, dict) else ""
        if not item or not text:
            continue
        try:
            index = int(item.get("template_index"))
        except (TypeError, ValueError):
            continue
        prefix = _replace_macro_call(prefix, "resumeItem", index, [text])
    return prefix


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


def _control_bullet_value(
    bullet: Dict[str, Any], catalog: Optional[Dict[str, Any]] = None,
) -> float:
    """Value a line for packing while protecting high-information controls."""
    value = _bullet_value(bullet)
    source_id = str(bullet.get("source_id") or "")
    control = canonical_control_scores(catalog or {}).get(source_id)
    if control is not None:
        # The model's target priority still leads. The bounded bonus prevents
        # compile overflow from deleting a quantified/base proof line merely
        # because a fresh project received an inflated priority.
        value += 12.0 + max(0.0, min(45.0, control - 50.0)) * 0.25
    return value


def _removal_actions(
    plan: Dict[str, Any], catalog: Optional[Dict[str, Any]] = None,
) -> List[Tuple[float, str, int, Optional[int]]]:
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
                    actions.append((_control_bullet_value(bullet, catalog), section, entry_index, bullet_index))
            if can_remove and len(entries) > minimum_entries and total_bullets - len(bullets) >= 1:
                # Removing an entry saves its heading too, so compare value per
                # vertical unit rather than raw total value.
                density = sum(_control_bullet_value(bullet, catalog) for bullet in bullets) / max(1, len(bullets) + 1)
                actions.append((density, section, entry_index, None))
    return actions


def _reclaim_flexible_content(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Use flexible content in Victor's preferred order before strong evidence.

    Coursework is the first reserve and the aggregate Awards line is the
    second. A quantified external-selection line is not a generic reserve:
    it can be the strongest distinctiveness proof in a project and must be
    compared like substantive evidence. If a candidate truly needs another
    removal after optional front matter is gone, the normal control-aware
    removal ranking decides rather than deleting HackMIT by name.
    """
    policy = plan.setdefault("front_matter_policy", {"coursework": "keep", "awards": "keep"})
    if str(policy.get("coursework") or "keep") == "keep":
        policy["coursework"] = "omit"
        return {
            "kind": "front_matter",
            "field": "coursework",
            "reason": "reclaimed flexible coursework space before removing strong resume evidence",
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
        horizontal = layout.get("horizontal") or {}
        horizontal_safe = bool(horizontal.get("bullets")) and bool(horizontal.get("pass"))
        total_bullets = sum(
            len(entry.get("bullets", []) or [])
            for section in ("experiences", "projects", "leadership")
            for entry in (plan.get(section, []) or [])
        )
        attempts.append({
            "attempt": attempt_number, "pages": layout.get("pages"),
            "overfull": layout.get("overfull"),
            "horizontal_pass": horizontal_safe,
            "wrap_count": horizontal.get("wrap_count", 0),
            "near_wrap_count": horizontal.get("near_wrap_count", 0),
            "density_gap_pt": layout.get("density_gap_pt"),
            "density_pass": layout.get("density_pass"),
            "bullets": sum(len(entry.get("bullets", [])) for section in ("experiences", "projects", "leadership") for entry in plan.get(section, [])),
        })
        if not layout.get("compiled"):
            raise RuntimeError(
                "candidate LaTeX failed to compile; packing cannot treat a syntax error as page overflow"
            )
        # Vertical/page overflow is the packer's removal contract. Near-wraps
        # are recorded for the bounded line editor/compactor after packing;
        # deleting whole evidence families just to make every line safe here
        # was worse than preserving the content for a geometry repair or an
        # honest final rejection.
        if layout.get("compiled") and layout.get("pages") == 1 and not layout.get("overfull") and total_bullets >= MIN_TOTAL_BULLETS:
            break
        flexible_removal = _reclaim_flexible_content(plan)
        if flexible_removal:
            if flexible_removal.get("kind") == "deferred_bullet_removal":
                removed.append(_apply_removal(plan, flexible_removal["action"]))
                removed[-1]["reason"] = flexible_removal.get("reason", "")
            else:
                removed.append(flexible_removal)
            continue
        actions = _removal_actions(plan, catalog)
        if not actions:
            break
        removed.append(_apply_removal(plan, min(actions, key=lambda item: item[0])))

    if not (
        layout.get("compiled")
        and layout.get("pages") == 1
        and not layout.get("overfull")
        and sum(
            len(entry.get("bullets", []) or [])
            for section in ("experiences", "projects", "leadership")
            for entry in (plan.get(section, []) or [])
        ) >= MIN_TOTAL_BULLETS
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
        "human_skim_budget": plan.get("portfolio_budget") or {
            "version": HUMAN_PORTFOLIO_POLICY_VERSION,
            "caps": dict(HUMAN_PORTFOLIO_CAPS),
            "actions": [],
            "decision": "No pre-packing budget receipt was present.",
        },
        "excluded_bullet_ids": sorted(value for value in all_ids - kept_ids if value),
        "removed_front_matter": [
            item for item in removed if item.get("kind") == "front_matter"
        ],
        "style_change_percent": 0.0,
        "density_warning": (
            "normal bottom clearance remains because no additional authorized evidence was packed"
            if not layout.get("density_pass") else ""
        ),
        "horizontal_pass": bool(
            (layout.get("horizontal") or {}).get("bullets")
            and (layout.get("horizontal") or {}).get("pass")
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

    # If a writer expanded an authoritative source line into a longer
    # role-specific variant, the exact source wording is the safest geometry
    # fallback. It may surrender a nonessential paraphrase, but it cannot
    # invent a new claim and the resulting artifact is still sealed-reviewed.
    if source_text and len(_latex_plain(source_text)) < len(_latex_plain(current)):
        add(source_text)

    # Prefer terminology already authorized by the source bullet. These are
    # common resume compressions, not new claims.
    if "poc" in source_plain:
        add(re.sub(r"\bproof of concept\b", "POC", current, flags=re.I))
    if "rag" in source_plain:
        add(re.sub(r"\bretrieval[- ]augmented generation\b", "RAG", current, flags=re.I))
    # Skills rows are especially vulnerable to a single wrap when a writer
    # preserves every expanded label. These are abbreviations of terminology
    # already present in the source row, not new capabilities; the full forms
    # remain available in the evidence packet and posting diff.
    add(re.sub(r"\bMachine Learning\b", "ML", current, flags=re.I))
    add(re.sub(r"\bComputer Vision\b", "CV", current, flags=re.I))
    add(re.sub(r"\bLarge Language Models\b", "LLMs", current, flags=re.I))
    add(re.sub(r"\bAgentic AI\b", "Agentic", current, flags=re.I))
    add(re.sub(r"\blearned features\b", "features", current, flags=re.I))
    add(re.sub(r"\bacross RNN/LLM architectures\b", "in RNN/LLM architectures", current, flags=re.I))
    # Compact common list/connective wording without dropping a technical
    # object or changing the claim. These are especially useful for lines
    # that technically stay on one line but fail the 12pt safety margin.
    add(re.sub(r",\s+and\s+(?=[A-Za-z])", ", ", current))
    add(re.sub(r",\s+enabling\s+", " for ", current, flags=re.I))
    add(re.sub(r"\bclassification models\b", "classifiers", current, flags=re.I))
    add(re.sub(r"\bto handle\b", "for", current, flags=re.I))
    add(re.sub(r"\bGit, GitHub\b", "Git/GitHub", current, flags=re.I))
    add(re.sub(r"\bTesting, Debugging\b", "Testing/Debugging", current, flags=re.I))
    add(re.sub(r"\bVector Databases,\s*", "", current, flags=re.I))
    # These are conservative, source-preserving reductions for common
    # latency/fallback phrasing. They remove connective filler only; they do
    # not add a capability or change the outcome being claimed. Keeping them
    # deterministic matters because a final geometry repair must not become a
    # second unbounded writer pass.
    add(re.sub(
        r"\breturning a safe fallback when processing lagged\b",
        "with safe fallback on lag", current, flags=re.I,
    ))
    add(re.sub(
        r"\bKept live conversations responsive with asynchronous emotion analysis, returning a safe fallback when processing lagged\b",
        "Kept conversations responsive via asynchronous emotion analysis, with safe fallback on lag",
        current, flags=re.I,
    ))
    add(re.sub(
        r"\breturning a safe fallback when (?:processing )?lagged\b",
        "with safe fallback on lag", current, flags=re.I,
    ))
    add(re.sub(r"\bKept live conversations responsive with\b", "Kept conversations responsive via", current, flags=re.I))
    add(re.sub(r"\bwhen processing lagged\b", "on lag", current, flags=re.I))
    add(re.sub(
        r"\bwhile handling (a|an|the) (.+?) with\b",
        r"on \1 \2 using", current, flags=re.I,
    ))
    compacted = re.sub(r"\bclassification models\b", "classifiers", current, flags=re.I)
    add(re.sub(r"\bto handle\b", "for", compacted, flags=re.I))

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
    # A dense page can contain several independent near-wraps. Keep the pass
    # bounded, but do not stop before reaching a later line after four earlier
    # safe compactions.
    for _ in range(12):
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
            if not current_bullet and source_id.startswith("front:skills:"):
                current_bullet = next((
                    item for item in (best_plan.get("front_matter_rewrites") or [])
                    if str(item.get("line_id") or "") == source_id
                ), None)
            if not current_bullet:
                continue
            current_text = str(current_bullet.get("text") or "")
            candidates = _line_compaction_candidates(current_text, source_text.get(source_id, ""))
            for candidate_text in candidates:
                trial = copy.deepcopy(best_plan)
                replaced = False
                if source_id.startswith("front:skills:"):
                    front_line = next((
                        item for item in (trial.get("front_matter_rewrites") or [])
                        if str(item.get("line_id") or "") == source_id
                    ), None)
                    if front_line:
                        front_line["text"] = candidate_text
                        replaced = True
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
    front_matter_rewrites: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    template = (cv_root(root) / CANONICAL_TEMPLATE).read_text()
    template_prefix = template.split(BODY_MARKER, 1)[0].rstrip()
    generated_prefix = tex.split(BODY_MARKER, 1)[0].rstrip() if BODY_MARKER in tex else ""
    generated_prefix = generated_prefix.replace(GENERATED_ONE_PAGE_FOOTER, CANONICAL_PAGE_FOOTER, 1)
    template_prefix = _apply_front_matter_rewrites(
        template_prefix, front_matter_rewrites, root,
    ).rstrip()
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
        "evidence_backed_front_matter_rewrites": len(front_matter_rewrites or []),
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
            "text": line_text[:500],
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
    candidates = [
        {
            "source_id": str(item.get("line_id") or ""),
            "text": str(item.get("text") or ""),
            "kind": "front_matter",
        }
        for item in (plan.get("front_matter_rewrites") or [])
        if str(item.get("line_id") or "") and str(item.get("text") or "")
    ]
    for section in ("experiences", "projects", "leadership"):
        for entry in plan.get(section, []):
            for bullet in entry.get("bullets", []):
                candidates.append({
                    "source_id": bullet.get("source_id"),
                    "text": str(bullet.get("text") or ""),
                    "kind": "bullet",
                })
    for candidate in candidates:
        plain = _latex_plain(candidate["text"])
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
                "source_id": candidate["source_id"],
                "kind": candidate["kind"],
                "text": plain,
                "wraps": wraps,
                "right_slack_pt": slack,
                "near_wrap": slack < MIN_RIGHT_SLACK_PT,
                "horizontal_pass": not wraps and slack >= MIN_RIGHT_SLACK_PT,
            })
        else:
            results.append({
                "source_id": candidate["source_id"], "kind": candidate["kind"],
                "text": plain, "wraps": None, "right_slack_pt": None,
                "horizontal_pass": False,
                "warning": "resume line not found in PDF geometry",
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
    # Callers may hand us a repository-relative private run directory (the
    # portfolio-search children do this intentionally).  Tectonic receives
    # ``cwd=run_dir`` below, so a relative ``--outdir`` would be resolved a
    # second time and look like ``run_dir/run_dir``.  Resolve once at the
    # process boundary; this keeps the compile contract identical for direct,
    # nested, and concurrent runs.
    run_dir = Path(run_dir).resolve()
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
    rewrites = plan.get("front_matter_rewrites") if isinstance(plan, dict) else None
    style = (
        template_style_guard(tex, repo_root(), policy, rewrites)
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
    rendered_plain = _latex_plain(tex)
    unsupported_rendered = [
        str(item.get("term") or "")
        for item in (job.get("target_keywords") or {}).get("terms", [])
        if not item.get("supported")
        and str(item.get("term") or "")
        and _keyword_present(str(item.get("term") or ""), rendered_plain)
    ]
    if unsupported_rendered:
        warnings.append(
            "unsupported posting term(s) rendered without authorized evidence: %s"
            % ", ".join(unsupported_rendered)
        )
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
    factual_status = "fail" if _contains_forbidden_resume_term(tex) or unsupported_rendered else "pass"
    eligibility_status = str(eligibility.get("status") or "partial").lower()
    return {
        "rubric_version": RUBRIC_VERSION,
        "hard_fail": (
            not layout_gate
            or factual_status == "fail"
            or eligibility_status == "fail"
        ),
        "warnings": warnings,
        "layout": layout,
        "style": style,
        "gates": {
            "factual": {
                "status": factual_status,
                "reason": (
                    "unsupported or permanently excluded terminology detected: %s"
                    % ", ".join(unsupported_rendered)
                    if factual_status == "fail" and unsupported_rendered
                    else "permanently excluded resume term detected"
                    if factual_status == "fail"
                    else "no deterministic forbidden claim detected"
                ),
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
    review_mode: str = "",
    critic_roles: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Combine a critique panel and deterministic gates without self-scoring.

    This intentionally returns no composite craft score.  A draft may be
    useful while still awaiting Victor or a critique panel; that state must
    remain visible instead of being converted into a flattering number. New
    runs use multiple Codex Luna roles. The old parameter name is retained for
    compatibility with saved-run/test callers.
    """
    review_available = bool(independent_available)
    review_mode = str(review_mode or ("independent_provider" if review_available else "unavailable"))
    roles = [str(item) for item in (critic_roles or []) if str(item)]
    data = agent_review.get("data") or {}
    criteria = data.get("criteria") if isinstance(data.get("criteria"), dict) else data
    unsupported, ignored_unsupported = actionable_unsupported_claims(
        data.get("unsupported_claims", [])
    )
    decision_feedback = data.get("decision_feedback", [])
    if not isinstance(decision_feedback, list):
        decision_feedback = []
    portfolio_comparison = data.get("portfolio_comparison")
    if not isinstance(portfolio_comparison, dict):
        portfolio_comparison = {
            "status": "unknown",
            "reason": "critic-panel portfolio comparison was not returned",
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
        gates["factual"] = {"status": "fail", "reason": "critic panel reported unsupported claims"}
    if "factual" in deterministic_gates and deterministic_gates["factual"].get("status") == "fail":
        gates["factual"] = deterministic_gates["factual"]
    gates["layout"] = deterministic_gates.get("layout", {"status": "fail", "reason": "layout unavailable"})
    gates["portfolio"] = deterministic_gates.get("portfolio", {"status": "fail", "reason": "portfolio unavailable"})
    if "eligibility" in deterministic_gates:
        gates["eligibility"] = deterministic_gates["eligibility"]
    gates["critic_jury"] = {
        "status": "pass" if review_available else "fail",
        "reason": "Codex Luna multi-role critic panel completed" if review_available else "Codex Luna critic panel was unavailable",
    }
    # Keep a readable compatibility field for older Resume Bank consumers, but
    # do not call a same-model jury vendor-independent.
    gates["independent_review"] = {
        "status": "pass" if review_mode == "independent_provider" else "partial" if review_available else "fail",
        "reason": (
            "separate provider critique completed" if review_mode == "independent_provider" else
            "same-model Codex Luna jury; no separate vendor was used" if review_available else
            "no critique panel was available"
        ),
    }
    comparison_status = str(portfolio_comparison.get("status") or "unknown").lower()
    if comparison_status not in {"pass", "partial", "fail"}:
        comparison_status = "fail"
    gates["portfolio_comparison"] = {
        "status": comparison_status,
            "reason": str(portfolio_comparison.get("reason") or "critic-panel portfolio comparison unavailable"),
    }
    blocking = data.get("blocking_issues", [])
    if not isinstance(blocking, list):
        blocking = [str(blocking)]
    assessments = data.get("blocking_issue_assessments")
    if not isinstance(assessments, list):
        assessments = []
        for issue in blocking:
            kind = classify_critic_issue(issue)
            classification, severity = critic_issue_finding(issue, kind)
            assessments.append({
                "issue": str(issue), "kind": kind, "classification": classification,
                "severity": severity, "supporting_roles": [], "support_count": 0,
                "agreement": "unknown", "variants": [],
            })
    hard_blocking = [
        item for item in assessments
        if str(item.get("kind") or "") == "hard_blocker"
    ]
    fit_gaps = [
        item for item in assessments
        if str(item.get("kind") or "") == "candidate_fit_gap"
    ]
    quality_concerns = [
        item for item in assessments
        if str(item.get("kind") or "") in {"tailoring_regression", "quality_concern"}
    ]
    # Candidate-role fit is deliberately descriptive, not a readiness gate:
    # a moderate-fit candidate can still have an excellent, evidence-safe
    # resume.  Quality/safety gates remain non-averagable. Partial quality
    # gates produce review rather than a fabricated score.
    readiness_gates = (
        "factual", "evidence", "distinctiveness", "clarity", "privacy",
        "layout", "portfolio", "eligibility", "critic_jury", "portfolio_comparison",
    )
    failed_readiness_gates = [
        name for name in readiness_gates
        if gates.get(name, {}).get("status") != "pass"
    ]
    partial_readiness_gates = [
        name for name in readiness_gates
        if gates.get(name, {}).get("status") in {"partial", "unknown"}
    ]
    hard_fail = bool(unsupported or hard_blocking or any(
        gates.get(name, {}).get("status") == "fail" for name in readiness_gates
    ))
    needs_review = bool(partial_readiness_gates)
    return {
        "rubric_version": RUBRIC_VERSION,
        "craft_score": None,
        "score": None,
        "ready": not hard_fail and not needs_review,
        "hard_fail": hard_fail,
        "gates": gates,
        "unsupported_claims": unsupported,
        "ignored_unsupported_claims": ignored_unsupported,
        "missing_evidence": data.get("missing_evidence", []),
        "revision_priorities": data.get("revision_priorities", []),
        "blocking_issues": blocking,
        "blocking_issue_assessments": assessments,
        "hard_blocking_issues": hard_blocking,
        "fit_gaps": fit_gaps,
        "quality_concerns": quality_concerns,
        "failed_readiness_gates": failed_readiness_gates,
        "needs_review": needs_review,
        "line_feedback": data.get("line_feedback", []),
        "decision_feedback": decision_feedback[:20],
        "portfolio_comparison": portfolio_comparison,
        "reviewer": agent_review.get("provider"),
        "review_mode": review_mode,
        "critic_roles": roles[:8],
        "critic_jury": {
            "available": review_available,
            "mode": review_mode,
            "roles": roles[:8],
            "separate_vendor": review_mode == "independent_provider",
        },
        "independent_review": review_mode == "independent_provider" and review_available,
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
    try:
        export_local_tailored_resumes(root or repo_root())
    except (OSError, ValueError):
        pass
    return current


def _select_valid_plan(
    candidates: List[Dict[str, Any]], catalog: Dict[str, Any], enhance: bool,
    graph: Optional[Dict[str, Any]] = None,
    generation: bool = False,
) -> Tuple[Optional[Dict[str, Any]], List[str], Optional[Dict[str, Any]]]:
    all_errors: List[str] = []
    for candidate in candidates:
        if not candidate.get("ok"):
            continue
        normalized, errors = validate_plan(
            candidate.get("data") or {}, catalog, enhance, graph=graph,
            generation=generation,
        )
        if not errors:
            return normalized, [], candidate
        all_errors.extend(["%s: %s" % (candidate.get("provider", "provider"), error) for error in errors])
    return None, all_errors, None


def run_tailoring(
    run_dir: Path, job: Dict[str, Any], update, enhance: bool,
    unrestricted: bool = False,
    generation: bool = False,
    quality_profile: str = QUALITY_PROFILE_DEFAULT,
    variant_instruction: str = "",
    _search_child: bool = False,
) -> None:
    run_started_clock = time.time()
    run_started_at = now_iso()
    quality_profile = normalize_quality_profile(quality_profile)
    requested_comparison_control = job.get("_resume_studio_control_profile")
    if quality_profile == "search" and not _search_child:
        run_portfolio_search(
            run_dir, job, update, enhance=enhance, unrestricted=unrestricted,
            generation=generation, comparison_control=requested_comparison_control,
        )
        return
    profile = QUALITY_PROFILES[quality_profile]
    run_id = str(job.get("_resume_studio_run_id") or "")
    queue_id = str(job.get("_resume_studio_queue_id") or "")
    comparison_control = resolve_comparison_control(repo_root(), requested_comparison_control)
    # Queue/run correlation is local operational metadata, never prompt or
    # evidence content. Keep it out of the persisted public posting snapshot.
    job = {key: value for key, value in job.items() if not str(key).startswith("_resume_studio_")}
    update("context", "Fetching the posting and preparing the private CV context")
    context = job_context(job)
    catalog = source_catalog(repo_root())
    graph = evidence_graph(repo_root())
    graph_context = evidence_context(graph, context, str(context.get("posting_text") or ""))
    match = resume_match_for_job(job, repo_root(), posting_text=str(context.get("posting_text") or ""))
    context["resume_match"] = match
    context["target_keywords"] = target_keyword_strategy(
        context, catalog, repo_root(), graph=graph,
        # Enhanced and unchained lanes may surface buried, reviewed Markdown
        # evidence. Source-only mode remains limited to the rendered CV.
        comprehensive=enhance,
    )
    context["tailoring_brief"] = build_tailoring_brief(
        job, str(context.get("posting_text") or ""),
        context.get("company_context") or {}, context.get("target_keywords") or {},
        catalog, graph=graph,
    )
    context["provider_policy"] = {
        "allowed_lanes": [name for name, path in provider_commands().items() if path],
        "codex_model": CODEX_LUNA_MODEL,
        "codex_effort_defaults": dict(CODEX_TASK_EFFORT_DEFAULTS),
        "repair_effort_override": "high",
        "sealed_evaluator_effort": profile.get("evaluator_effort") or resume_evaluator.CODEX_EFFORT,
        "review_mode": CODEX_REVIEW_MODE,
        "critic_roles": [item["key"] for item in CODEX_CRITIC_ROLES],
        "sealed_evaluator_contract": {
            "version": SEALED_EVALUATOR_CONTRACT,
            "fingerprint": resume_evaluator.contract_fingerprint(),
            "rubric_sha256": resume_evaluator.EVALUATOR_RUBRIC_SHA256,
            "execution_lane": "sealed_evaluator",
        },
        "separate_vendor_review": False,
        "local_models_allowed": False,
        "api_fallback_allowed": False,
        "quality_profile": quality_profile,
        "quality_profile_policy": dict(profile),
        "portfolio_variant_instruction": str(variant_instruction or "")[:2400],
    }
    available = [name for name, path in provider_commands().items() if path and name == "codex"]
    if not available:
        raise RuntimeError("Codex CLI is not installed; Resume Studio requires the Codex Luna lane")
    gap_records: List[Dict[str, Any]] = []
    if enhance and context["target_keywords"].get("posting_available"):
        update("gap_analysis", "Mapping role essentials, ATS terms, company domain, and authorized evidence")
        gap_provider = "codex" if "codex" in available else available[0]
        gap_record = run_provider(
            gap_provider,
            gap_analysis_prompt(context, catalog, graph),
            run_dir,
            "gap_analysis",
            timeout=5 * 60,
            schema=gap_analysis_schema(),
            codex_effort=profile.get("gap_analysis_effort") or profile["author_effort"],
        )
        gap_record["label"] = "gap_analysis"
        gap_records.append(gap_record)
        write_json(run_dir / "gap_analysis.json", gap_record)
        if gap_record.get("ok"):
            context["generation_strategy"] = normalize_gap_analysis(
                gap_record.get("data") or {}, context["target_keywords"], catalog,
                graph, str(context.get("posting_text") or ""),
            )
        else:
            # Keep generation recoverable when the planning call times out,
            # but make the degradation explicit. The author may use the
            # deterministic supported-term strategy; it must not pretend that
            # a full requirement map was completed.
            context["generation_strategy"] = {
                "version": "gap-analysis-fallback-v1",
                "status": "unavailable",
                "portfolio_strategy": (
                    "Gap analysis was unavailable; use the deterministic supported-term strategy only. "
                    "Do not infer unsupported requirements or claim that every posting requirement was mapped."
                ),
                "requirements": [],
                "must_cover_terms": [],
                "honest_gaps": [],
                "error": str(gap_record.get("error") or "gap analysis provider failed")[:500],
            }
        context["target_keywords"] = apply_gap_support_to_keywords(
            context["target_keywords"], context["generation_strategy"],
        )
        context["tailoring_brief"]["provider_strategy"] = {
            "portfolio_strategy": str(context["generation_strategy"].get("portfolio_strategy") or "")[:1200],
            "must_cover_terms": list(context["generation_strategy"].get("must_cover_terms") or [])[:32],
            "honest_gaps": list(context["generation_strategy"].get("honest_gaps") or [])[:24],
            "requirement_count": len(context["generation_strategy"].get("requirements") or []),
        }
    elif enhance:
        context["generation_strategy"] = {
            "version": "gap-analysis-fallback-v1",
            "status": "unavailable",
            "portfolio_strategy": (
                "No full posting text was captured; use the deterministic role/company brief and do not infer "
                "requirements from a title or search snippet."
            ),
            "requirements": [],
            "must_cover_terms": [],
            "honest_gaps": [],
        }
        context["tailoring_brief"]["provider_strategy"] = {
            "portfolio_strategy": context["generation_strategy"]["portfolio_strategy"],
            "must_cover_terms": [], "honest_gaps": [], "requirement_count": 0,
        }
    else:
        context["generation_strategy"] = {}
    context["job_intelligence"] = build_job_intelligence(
        job, str(context.get("posting_text") or ""), match,
        context.get("target_keywords"), context.get("generation_strategy"),
        context.get("tailoring_brief"),
    )
    context["posting_snapshot_hash"] = context["job_intelligence"].get("posting_snapshot_hash", "")
    write_json(run_dir / "comparison_control.json", comparison_control_summary(comparison_control))
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
        "tailoring_brief": context.get("tailoring_brief"),
        "generation_strategy": context.get("generation_strategy"),
        "provider_policy": context["provider_policy"],
        "quality_profile": quality_profile,
        "evidence_graph": {
            "version": graph.get("version"),
            "hash": graph.get("hash"),
            "review_summary": graph.get("review_summary") or {},
            "markdown_sources": markdown_sources,
        },
    })
    mode_label = "generation" if generation else "unrestricted" if unrestricted else "enhanced" if enhance else "source-only"
    prompt = base_prompt(
        context, "a Codex Luna resume evidence strategist", catalog, enhance,
        graph=graph, unrestricted=unrestricted, generation=generation,
        variant_instruction=variant_instruction,
    )
    schema = plan_schema(enhance, generation=generation)
    update("drafting", "Building adaptive %s evidence plan with Codex Luna" % mode_label)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(available)) as pool:
        futures = {
            pool.submit(
                run_provider, provider, prompt, run_dir, "draft", RUN_TIMEOUT_SECONDS,
                schema, codex_effort=profile["author_effort"],
            ): provider
            for provider in available
        }
        drafts = [future.result() for future in concurrent.futures.as_completed(futures)]
    for draft in drafts:
        draft["label"] = "draft"
        write_json(run_dir / (draft.get("provider", "unknown") + "_draft.json"), draft)
    successful = [draft for draft in drafts if draft.get("ok")]
    if not successful:
        # A timed-out or malformed author must not turn the whole application
        # into a hard failure. Fall back to the immutable, source-addressed
        # control and still run the same packing, geometry, and sealed audit.
        # This is deliberately not reported as a tailored win: it produces a
        # usable base artifact plus an honest provider-failure receipt.
        fallback_data = canonical_control_plan(catalog)
        fallback_plan, fallback_errors = validate_plan(
            fallback_data, catalog, enhance, graph=graph, generation=generation,
        )
        if fallback_errors:
            write_json(run_dir / "plan_errors.json", [
                "canonical control fallback: %s" % error for error in fallback_errors
            ])
            raise RuntimeError(
                "Approved provider lanes returned no usable evidence plan and the canonical fallback failed; "
                "inspect *_draft.json and plan_errors.json"
            )
        fallback_record = {
            "provider": "deterministic",
            "ok": True,
            "label": "canonical_control_fallback",
            "reasoning_effort": "none",
            "data": fallback_plan,
            "fallback": True,
            "error_context": [
                str(draft.get("error") or "provider returned no usable plan")[:500]
                for draft in drafts
            ],
        }
        drafts.append(fallback_record)
        successful = [fallback_record]
        write_json(run_dir / "canonical_control_fallback.json", fallback_record)

    writer = "codex" if any(item.get("provider") == "codex" for item in successful) else successful[0].get("provider")
    update("synthesis", "Codex is synthesizing the strongest adaptive evidence plan")
    if len(successful) == 1 and (
        successful[0].get("provider") == writer or successful[0].get("fallback")
    ):
        synthesis = {
            "provider": writer, "ok": True, "skipped": True,
            "reason": "Codex Luna is the sole author lane; a separate multi-role Luna critic panel follows.",
            "data": successful[0].get("data") or {},
        }
    else:
        synthesis = run_provider(
            writer,
            synthesis_prompt(
                context, successful, catalog, enhance, graph=graph,
                unrestricted=unrestricted, generation=generation,
            ),
            run_dir, "synthesis", timeout=4 * 60, schema=schema,
        )
    synthesis["label"] = "synthesis"
    write_json(run_dir / "synthesis.json", synthesis)
    candidates = [synthesis] + successful
    candidate_plan, plan_errors, _ = _select_valid_plan(
        candidates, catalog, enhance, graph=graph, generation=generation,
    )
    if candidate_plan is None:
        # A blank structured response is a provider/execution failure, not a
        # reason to leave the user with no resume artifact. Fall back to the
        # immutable source-addressed control, then send that exact compiled
        # candidate through the same packer, geometry gates, and sealed panel.
        # This preserves fail-closed quality semantics while making transient
        # Luna failures recoverable and auditable.
        fallback_data = canonical_control_plan(catalog)
        fallback_plan, fallback_errors = validate_plan(
            fallback_data, catalog, enhance, graph=graph, generation=generation,
        )
        if fallback_errors:
            write_json(run_dir / "plan_errors.json", plan_errors + [
                "canonical control fallback: %s" % error for error in fallback_errors
            ])
            raise RuntimeError("No provider returned a valid adaptive source-addressed plan; inspect plan_errors.json")
        fallback_record = {
            "provider": "deterministic",
            "ok": True,
            "label": "canonical_control_fallback",
            "reasoning_effort": "none",
            "data": fallback_plan,
            "fallback": True,
            "error_context": plan_errors[:12],
        }
        write_json(run_dir / "canonical_control_fallback.json", fallback_record)
        candidate_plan = fallback_plan
        plan_errors = plan_errors + ["selected deterministic canonical-control fallback"]
    candidate_plan = expand_candidate_portfolio(candidate_plan, catalog, enhance)
    control_recovery: Dict[str, Any] = {"attempted": False, "status": "not_run"}
    role_evidence_floor: Dict[str, Any] = {"attempted": False, "status": "not_run"}
    if enhance:
        candidate_plan, control_recovery = deterministic_control_recovery(
            candidate_plan, catalog, context.get("target_keywords"), run_dir,
        )
        if profile.get("role_evidence_floor", False):
            candidate_plan, role_evidence_floor = deterministic_role_evidence_floor(
                candidate_plan, catalog, context, graph, run_dir / "role_evidence_floor",
            )
        else:
            role_evidence_floor = {
                "attempted": False,
                "status": "disabled_by_quality_profile",
                "version": ROLE_EVIDENCE_FLOOR_VERSION,
                "reason": "project-level role swaps remain a lab hypothesis until a sealed positive win justifies default mutation",
            }
    write_json(run_dir / "candidate_plan.json", candidate_plan)
    write_json(run_dir / "brief.json", {
        "positioning_thesis": candidate_plan.get("positioning_thesis", ""),
        "selected_evidence": candidate_plan.get("selected_evidence", []),
        "excluded_evidence": candidate_plan.get("excluded_evidence", []),
        "revision_notes": candidate_plan.get("revision_notes", []),
        "decision_ledger": candidate_plan.get("decision_ledger", []),
        "front_matter_policy": candidate_plan.get("front_matter_policy", {"coursework": "keep", "awards": "keep"}),
        "front_matter_rewrites": candidate_plan.get("front_matter_rewrites", []),
        "job": job_summary(job),
        "target_keywords": context.get("target_keywords"),
        "tailoring_brief": context.get("tailoring_brief"),
        "generation_strategy": context.get("generation_strategy"),
        "provider_policy": context["provider_policy"],
        "quality_profile": quality_profile,
        "control_recovery": control_recovery,
        "role_evidence_floor": role_evidence_floor,
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

    def recover_repair_geometry(
        value: Dict[str, Any], value_layout: Dict[str, Any], recovery_root: Path,
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any], Optional[str], bool, Dict[str, Any]]:
        """Try deterministic source-preserving geometry recovery for a repair.

        A repair writer can make the right portfolio decision and still return
        a candidate that is one near-wrap away from being shippable.  The
        normal final recovery below handles the already-selected candidate;
        this earlier pass gives a source-valid audit repair the same bounded
        deterministic opportunity before it is rejected.  No new evidence or
        wording is invented here, and a successful recovery still receives a
        fresh sealed panel before acceptance.
        """
        recovery_root.mkdir(parents=True, exist_ok=True)
        recovered, restored_ids = restore_wrapped_source_text(value, value_layout, catalog)
        source_root = recovery_root / "source_restore"
        _, source_layout, _ = render_candidate(recovered, source_root)
        compacted, _, compactions = compact_plan_to_geometry(
            recovered, source_layout, catalog, recovery_root,
        )
        changed = bool(restored_ids or compactions)
        receipt: Dict[str, Any] = {
            "attempted": True,
            "restored_source_ids": list(restored_ids),
            "compactions": list(compactions),
            "changed": changed,
        }
        if not changed:
            receipt.update({
                "safe_render": False,
                "status": "no_safe_deterministic_change",
            })
            return value, "", value_layout, None, False, receipt
        candidate_root = recovery_root / "candidate"
        candidate_tex, candidate_layout, candidate_preview = render_candidate(
            compacted, candidate_root,
        )
        safe = bool(
            candidate_layout.get("compiled")
            and candidate_layout.get("pages") == 1
            and not candidate_layout.get("overfull")
            and (candidate_layout.get("horizontal") or {}).get("pass")
        )
        receipt.update({
            "safe_render": safe,
            "status": "safe" if safe else "still_unsafe",
            "wrap_count": (candidate_layout.get("horizontal") or {}).get("wrap_count", 0),
            "near_wrap_count": (candidate_layout.get("horizontal") or {}).get("near_wrap_count", 0),
        })
        return compacted, candidate_tex, candidate_layout, candidate_preview, safe, receipt

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
    if enhance and profile["model_space_expansion"] and (
        measured_space_available(layout)
        or (isinstance(layout.get("density_gap_pt"), (int, float)) and layout["density_gap_pt"] > MAX_DENSITY_GAP_PT)
    ):
        update("space_review", "Measured spare page capacity; asking Codex to fill it with verified unused evidence")
        expansion_record = run_provider(
            writer,
            space_expansion_prompt(
                context, plan, layout, catalog, graph=graph,
                unrestricted=unrestricted, generation=generation,
            ),
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
    if enhance and profile.get("deterministic_space_expansion", True) and (
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
    line_edit_passes = MAX_LINE_EDIT_PASSES if enhance and (
        profile["model_line_editor"]
        or (profile["line_editor_fallback"] and not (layout.get("horizontal") or {}).get("pass"))
    ) else 0
    for line_round in range(1, line_edit_passes + 1):
        if not enhance or (layout.get("horizontal") or {}).get("pass"):
            break
        label = "line_edit" if line_round == 1 else "line_edit_%s" % line_round
        update("line_editing", "Repairing rendered one-line geometry (pass %s/%s)" % (line_round, line_edit_passes))
        line_edit = run_provider(
            writer, line_editor_prompt(context, plan, layout, graph), run_dir, label,
            timeout=int(profile.get("line_editor_timeout_seconds") or 3 * 60),
            schema=plan_schema(True, generation=generation),
            codex_effort=profile["line_editor_effort"],
        )
        line_edits.append(line_edit)
        line_edit["label"] = label
        write_json(run_dir / (label + ".json"), line_edit)
        if not line_edit.get("ok"):
            break
        edited, edit_errors = validate_plan(
            line_edit.get("data") or {}, catalog, True, graph=graph,
            generation=generation,
        )
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

    # Line editing can reclaim a few points of horizontal space, which can
    # make an additional verified bullet fit even though the earlier density
    # pass correctly stopped.  Re-measure after all wording changes and keep
    # filling that newly exposed capacity.  This is intentionally a
    # deterministic evidence pass: it cannot invent filler, change
    # chronology, or replace core experience, and every trial is compiled.
    post_line_density: List[Dict[str, Any]] = []
    post_line_attempted: set = set()
    distinctive_replacement_attempted = False
    target_opportunity: Dict[str, Any] = {
        "attempted": False,
        "status": "not_run",
    }

    def fill_post_line_capacity(round_label: str) -> None:
        nonlocal plan, packing, chosen, layout, preview, line_compactions, distinctive_replacement_attempted
        if not enhance or not profile.get("deterministic_space_expansion", True):
            return
        # A revision can reintroduce a near-wrap after the first compaction
        # pass. Repair that geometry before deciding whether to add more.
        if not (layout.get("horizontal") or {}).get("pass"):
            compacted, compact_layout, extra_compactions = compact_plan_to_geometry(
                plan, layout, catalog, run_dir / (round_label + "_compaction"),
            )
            if extra_compactions:
                plan = compacted
                line_compactions.extend(extra_compactions)
                packing[round_label + "_line_compaction"] = {
                    "applied": extra_compactions,
                    "horizontal": compact_layout.get("horizontal", {}),
                }
                write_json(run_dir / "content_plan.json", plan)
                write_json(run_dir / "layout_packing.json", packing)
                chosen, layout, preview = render_candidate(plan, run_dir)

        # Generation mode gets one explicit portfolio check for the known
        # low-signal Resident Assistant entry.  If a verified technical
        # project line can replace it, make that trade even when the page is
        # otherwise full.  This encodes Victor's preference without turning
        # the moderate tailor into a rigid "never show leadership" system.
        if generation:
            leadership_targets = []
            for entry_index, entry in enumerate(plan.get("leadership", []) or []):
                source_entry = (catalog.get("entries") or {}).get(str(entry.get("source_id") or ""), {})
                label = " ".join(
                    str(source_entry.get(key) or "")
                    for key in ("heading", "company", "role")
                )
                if re.search(r"resident assistant|residence life", label, re.I):
                    leadership_targets.append((entry_index, entry))
            if leadership_targets:
                raw_candidates = deterministic_space_additions(
                    plan, catalog, graph=graph, keyword_strategy=context.get("target_keywords"),
                )
                technical_candidates = [
                    item for item in raw_candidates
                    if str(item.get("section") or "") == "projects"
                    and re.search(
                        r"\b(?:architected|engineered|implemented|built|designed|trained|pipeline|model|api|database|flask|pytorch|react|python|sql|cloud|validation|classification|ingestion)\b",
                        _latex_plain(str(item.get("text") or "")), re.I,
                    )
                ]
                if technical_candidates:
                    target_index, target_entry = leadership_targets[0]
                    target_bullet = (target_entry.get("bullets") or [{}])[0]
                    prior_plan = copy.deepcopy(plan)
                    replacement_base = copy.deepcopy(plan)
                    removed_entry = replacement_base["leadership"].pop(target_index)
                    # A new project earns its heading atomically. Existing
                    # technical entries can use the strongest unused bullet;
                    # a new entry receives the paired candidates returned by
                    # deterministic_space_additions.
                    first = technical_candidates[0]
                    replacement_candidates = [first]
                    if str(first.get("placement") or "") == "new_entry":
                        replacement_candidates = [
                            item for item in technical_candidates
                            if str(item.get("entry_id") or "") == str(first.get("entry_id") or "")
                        ][:2]
                    replacement_plan, replacement_result = expand_into_measured_space(
                        replacement_base, replacement_candidates, catalog, graph,
                        run_dir / (round_label + "_leadership_replacement"),
                    )
                    replacement_record: Dict[str, Any] = {
                        "round": 0,
                        "label": round_label,
                        "kind": "leadership_replacement",
                        "before": {
                            "removed_entry_id": str(removed_entry.get("source_id") or ""),
                            "removed_source_ids": [
                                str(item.get("source_id") or "")
                                for item in removed_entry.get("bullets", [])
                            ],
                            "bullet_count": portfolio_metrics(plan).get("total_bullets"),
                        },
                        "candidate_source_ids": [
                            str(item.get("source_id") or "") for item in replacement_candidates
                        ],
                        "expansion": replacement_result,
                    }
                    if replacement_result.get("applied"):
                        plan = replacement_plan
                        chosen, layout, preview = render_candidate(plan, run_dir)
                        replacement_compactions: List[Dict[str, Any]] = []
                        if not (layout.get("horizontal") or {}).get("pass"):
                            compacted, compact_layout, replacement_compactions = compact_plan_to_geometry(
                                plan, layout, catalog,
                                run_dir / (round_label + "_leadership_compaction"),
                            )
                            if replacement_compactions:
                                plan = compacted
                                line_compactions.extend(replacement_compactions)
                                chosen, layout, preview = render_candidate(plan, run_dir)
                        if not (layout.get("horizontal") or {}).get("pass") or layout.get("pages") != 1:
                            plan = prior_plan
                            chosen, layout, preview = render_candidate(plan, run_dir)
                            replacement_record["decision"] = "Restored Resident Assistant evidence; the technical replacement failed the final geometry gate."
                        else:
                            replacement_result.setdefault("replaced", []).append({
                                "source_id": str(target_bullet.get("source_id") or ""),
                                "entry_id": str(removed_entry.get("source_id") or ""),
                                "section": "leadership",
                                "text": _latex_plain(str(target_bullet.get("text") or "")),
                                "reason": "replaced low-signal Resident Assistant evidence with stronger verified technical project evidence",
                            })
                            space_expansion["applied"] = list(space_expansion.get("applied") or []) + list(replacement_result.get("applied") or [])
                            space_expansion["replaced"] = list(space_expansion.get("replaced") or []) + list(replacement_result.get("replaced") or [])
                            space_expansion.setdefault("post_line_density", []).append(replacement_record)
                            replacement_record["decision"] = "Replaced Resident Assistant evidence with stronger verified technical project evidence."
                            replacement_record["after"] = {
                                "bullet_count": portfolio_metrics(plan).get("total_bullets"),
                                "density_gap_pt": layout.get("density_gap_pt"),
                                "one_more_bullet_fits": measured_space_available(layout),
                                "horizontal_pass": bool((layout.get("horizontal") or {}).get("pass")),
                            }
                            packing[round_label + "_leadership_replacement"] = replacement_result
                            post_line_density.append(replacement_record)
                            post_line_attempted.update(
                                str(item.get("source_id") or "") for item in replacement_candidates
                            )
                            write_json(run_dir / "content_plan.json", plan)
                            write_json(run_dir / "layout_packing.json", packing)

        max_density_rounds = int(profile.get("max_post_line_density_rounds") or 2)
        for density_round in range(1, max_density_rounds + 1):
            capacity_open = measured_space_available(layout)
            density_gap = layout.get("density_gap_pt")
            if not capacity_open and not (
                isinstance(density_gap, (int, float)) and density_gap > MAX_DENSITY_GAP_PT
            ):
                if not distinctive_replacement_attempted and round_label == "post_line_density":
                    distinctive_replacement_attempted = True
                    prior_plan = copy.deepcopy(plan)
                    replacement_plan, replacement_record = deterministic_distinctive_replacement(
                        plan, catalog, graph, context.get("target_keywords"),
                        run_dir / "distinctive_replacement",
                    )
                    replacement_record.update({
                        "round": density_round,
                        "label": round_label,
                        "before_bullet_count": portfolio_metrics(plan).get("total_bullets"),
                    })
                    if replacement_record.get("status") == "applied":
                        plan = replacement_plan
                        chosen, layout, preview = render_candidate(plan, run_dir)
                        if not (layout.get("horizontal") or {}).get("pass"):
                            compacted, compact_layout, extra_compactions = compact_plan_to_geometry(
                                plan, layout, catalog, run_dir / "distinctive_replacement_compaction",
                            )
                            if extra_compactions:
                                plan = compacted
                                line_compactions.extend(extra_compactions)
                                chosen, layout, preview = render_candidate(plan, run_dir)
                        if (
                            layout.get("pages") != 1
                            or layout.get("overfull")
                            or not (layout.get("horizontal") or {}).get("pass")
                        ):
                            plan = prior_plan
                            chosen, layout, preview = render_candidate(plan, run_dir)
                            replacement_record["status"] = "rejected"
                            replacement_record["reason"] = "replacement did not satisfy the final geometry gate"
                        else:
                            space_expansion["applied"] = list(space_expansion.get("applied") or []) + list(replacement_record.get("applied") or [])
                            space_expansion["replaced"] = list(space_expansion.get("replaced") or []) + list(replacement_record.get("replaced") or [])
                            replacement_record["after_bullet_count"] = portfolio_metrics(plan).get("total_bullets")
                            replacement_record["decision"] = "Kept one compiled distinctive evidence replacement before the sealed panel."
                            packing[round_label + "_distinctive_replacement"] = replacement_record
                            write_json(run_dir / "content_plan.json", plan)
                            write_json(run_dir / "layout_packing.json", packing)
                    post_line_density.append(replacement_record)
                break
            candidates = deterministic_space_additions(
                plan, catalog, graph=graph, keyword_strategy=context.get("target_keywords"),
            )
            # Do not retry a compiled failure forever.  New-entry candidates
            # are atomic, so once one bullet from a proposed new entry fails,
            # suppress the rest of that same heading for this pass as well.
            blocked_new_entries = {
                str(item.get("entry_id") or "")
                for item in candidates
                if str(item.get("placement") or "") == "new_entry"
                and str(item.get("source_id") or "") in post_line_attempted
            }
            candidates = [
                item for item in candidates
                if str(item.get("source_id") or "") not in post_line_attempted
                and not (
                    str(item.get("placement") or "") == "new_entry"
                    and str(item.get("entry_id") or "") in blocked_new_entries
                )
            ]
            record: Dict[str, Any] = {
                "round": density_round,
                "label": round_label,
                "before": {
                    "bullet_count": portfolio_metrics(plan).get("total_bullets"),
                    "density_gap_pt": density_gap,
                    "one_more_bullet_fits": measured_space_available(layout),
                },
                "candidate_source_ids": [str(item.get("source_id") or "") for item in candidates],
            }
            if not candidates:
                record["decision"] = "No further unused verified candidate remained after compiled density trials."
                post_line_density.append(record)
                break
            post_line_attempted.update(str(item.get("source_id") or "") for item in candidates)
            prior_plan = copy.deepcopy(plan)
            expanded_plan, expansion_result = expand_into_measured_space(
                plan, candidates, catalog, graph,
                run_dir / (round_label + "_space_expansion_%02d" % density_round),
            )
            record["expansion"] = expansion_result
            if not expansion_result.get("applied"):
                record["decision"] = "Rejected every remaining candidate because it could not earn the measured page space."
                post_line_density.append(record)
                continue

            plan = expanded_plan
            chosen, layout, preview = render_candidate(plan, run_dir)
            extra_compactions: List[Dict[str, Any]] = []
            if not (layout.get("horizontal") or {}).get("pass"):
                compacted, compact_layout, extra_compactions = compact_plan_to_geometry(
                    plan, layout, catalog,
                    run_dir / (round_label + "_compaction_%02d" % density_round),
                )
                if extra_compactions:
                    plan = compacted
                    line_compactions.extend(extra_compactions)
                    chosen, layout, preview = render_candidate(plan, run_dir)
                    packing[round_label + "_line_compaction_%02d" % density_round] = {
                        "applied": extra_compactions,
                        "horizontal": layout.get("horizontal", {}),
                    }

            # The density helper must never smuggle a horizontally unsafe
            # line into the final artifact. Restore the last good compiled
            # plan if the new evidence cannot be made safe.
            if not (layout.get("horizontal") or {}).get("pass") or layout.get("pages") != 1:
                plan = prior_plan
                chosen, layout, preview = render_candidate(plan, run_dir)
                record["decision"] = "Restored the prior safe plan; the candidate did not satisfy the final geometry gate."
                record["rejected_after_compaction"] = True
                record["after"] = {
                    "bullet_count": portfolio_metrics(plan).get("total_bullets"),
                    "density_gap_pt": layout.get("density_gap_pt"),
                    "one_more_bullet_fits": measured_space_available(layout),
                }
                post_line_density.append(record)
                continue

            packing[round_label + "_density_%02d" % density_round] = expansion_result
            space_expansion["applied"] = list(space_expansion.get("applied") or []) + list(expansion_result.get("applied") or [])
            space_expansion["replaced"] = list(space_expansion.get("replaced") or []) + list(expansion_result.get("replaced") or [])
            space_expansion.setdefault("post_line_density", []).append(record)
            record["decision"] = "Kept compiled verified additions and re-measured the remaining capacity."
            record["after"] = {
                "bullet_count": portfolio_metrics(plan).get("total_bullets"),
                "density_gap_pt": layout.get("density_gap_pt"),
                "one_more_bullet_fits": measured_space_available(layout),
                "horizontal_pass": bool((layout.get("horizontal") or {}).get("pass")),
            }
            post_line_density.append(record)
            write_json(run_dir / "content_plan.json", plan)
            write_json(run_dir / "layout_packing.json", packing)

        if enhance:
            write_json(run_dir / "post_line_density.json", post_line_density)
            write_json(run_dir / "content_plan.json", plan)
            write_json(run_dir / "layout_packing.json", packing)

    fill_post_line_capacity("post_line_density")

    # The generation author can identify the right source evidence and still
    # leave one high-value line buried in the candidate's history. Give only
    # Unchained one deterministic, source-verbatim counterfactual before the
    # sealed panel. It must compile, remain within the human skim budget, and
    # survive the same base-vs-tailored jury; this is an opportunity probe, not
    # an ATS keyword insertion rule.
    if generation and profile.get("target_opportunity_replacement", False):
        update(
            "target_opportunity",
            "Surfacing one buried source-grounded requirement opportunity",
        )
        opportunity_plan, target_opportunity = deterministic_target_opportunity_replacement(
            plan,
            catalog,
            graph,
            context,
            run_dir / "target_opportunity",
        )
        if target_opportunity.get("status") == "applied":
            plan = opportunity_plan
            chosen, layout, preview = render_candidate(plan, run_dir)
            packing["target_opportunity"] = target_opportunity
            write_json(run_dir / "content_plan.json", plan)
            write_json(run_dir / "layout_packing.json", packing)
    # Keep the artifact list truthful for non-generation modes as well.
    write_json(run_dir / "target_opportunity.json", target_opportunity)

    def combined_critique(records: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not records:
            return {"provider": "", "ok": False, "data": {
                "criteria": {name: {"status": "fail", "reason": "no Codex Luna critic panel available"} for name in REVIEW_CRITERIA},
                "blocking_issues": ["Codex Luna critic panel was unavailable."],
                "line_feedback": [], "unsupported_claims": [], "missing_evidence": [],
                "revision_priorities": ["Run the Codex Luna critic panel before calling this ready."],
                "decision_feedback": [],
                "portfolio_comparison": {
                    "status": "unknown",
                    "reason": "critic-panel portfolio comparison was unavailable",
                    "preserved_strengths": [],
                    "gained_strengths": [],
                    "lost_strengths": [],
                },
            }, "review_mode": "unavailable", "critic_roles": []}
        data = copy.deepcopy(records[0].get("data") or {})
        data.setdefault("blocking_issues", [])
        data.setdefault("blocking_issue_assessments", [])
        data.setdefault("line_feedback", [])
        data.setdefault("unsupported_claims", [])
        data.setdefault("missing_evidence", [])
        data.setdefault("revision_priorities", [])
        data.setdefault("decision_feedback", [])
        data.setdefault("portfolio_comparison", {
            "status": "unknown",
            "reason": "missing critic-panel portfolio comparison",
            "preserved_strengths": [],
            "gained_strengths": [],
            "lost_strengths": [],
        })
        comparison_records = [copy.deepcopy(data.get("portfolio_comparison") or {})]
        criteria = data.setdefault("criteria", {})
        for record in records[1:]:
            other = record.get("data") or {}
            data["blocking_issues"].extend(other.get("blocking_issues") or [])
            data["line_feedback"].extend(other.get("line_feedback") or [])
            data["unsupported_claims"].extend(other.get("unsupported_claims") or [])
            data["missing_evidence"].extend(other.get("missing_evidence") or [])
            data["revision_priorities"].extend(other.get("revision_priorities") or [])
            data["decision_feedback"].extend(other.get("decision_feedback") or [])
            other_comparison = other.get("portfolio_comparison") or {}
            comparison_records.append(copy.deepcopy(other_comparison))
            for name in REVIEW_CRITERIA:
                left = criteria.get(name) or {"status": "fail", "reason": "missing critique"}
                right = (other.get("criteria") or {}).get(name) or {"status": "fail", "reason": "missing critique"}
                order = {"fail": 0, "partial": 1, "pass": 2}
                if order.get(str(right.get("status")), 0) < order.get(str(left.get("status")), 0):
                    criteria[name] = right
                elif right.get("reason") and right.get("reason") not in str(left.get("reason") or ""):
                    left["reason"] = "; ".join(value for value in (str(left.get("reason") or ""), str(right.get("reason") or "")) if value)
                    criteria[name] = left
        # A panel comparison is an aggregate judgment, not whichever role
        # happened to finish first. Keep the worst status for safety while
        # retaining every role-confirmed preserved/gained/lost strength for
        # the comparative audit. This makes positive findings traceable without
        # allowing one optimistic critic to erase another role's loss.
        comparison_order = {"fail": 0, "partial": 1, "pass": 2, "unknown": -1}
        valid_comparisons = [
            item for item in comparison_records
            if isinstance(item, dict) and str(item.get("status") or "") in comparison_order
        ]
        if valid_comparisons:
            known_comparisons = [
                item for item in valid_comparisons
                if str(item.get("status") or "") in {"fail", "partial", "pass"}
            ]
            # An unavailable role must not erase a complete comparison from
            # the other sealed roles. ``unknown`` is the fallback only when
            # every role failed to produce a determinate comparison.
            aggregate_comparisons = known_comparisons or valid_comparisons
            selected_comparison = min(
                aggregate_comparisons,
                key=lambda item: comparison_order.get(str(item.get("status") or "unknown"), -1),
            )
            merged_comparison = {
                "status": str(selected_comparison.get("status") or "unknown"),
                "reason": "",
                "preserved_strengths": [],
                "gained_strengths": [],
                "lost_strengths": [],
            }
            reasons = []
            for item in aggregate_comparisons:
                reason = str(item.get("reason") or "").strip()
                if reason and reason not in reasons:
                    reasons.append(reason)
                for field in ("preserved_strengths", "gained_strengths", "lost_strengths"):
                    for strength in item.get(field) or []:
                        value = str(strength or "").strip()
                        if value and value not in merged_comparison[field]:
                            merged_comparison[field].append(value)
            merged_comparison["reason"] = "; ".join(reasons[:8])
            data["portfolio_comparison"] = merged_comparison
        for key in ("blocking_issues", "unsupported_claims", "missing_evidence", "revision_priorities"):
            data[key] = list(dict.fromkeys(str(value) for value in data.get(key) or []))
        # Child results remain raw and independently inspectable.  The parent
        # collapses repeated phrasings only for audit/readiness accounting and
        # records which critic roles agreed with each underlying concern.
        issue_assessments = collapse_critic_issues(records)
        data["blocking_issue_assessments"] = issue_assessments
        data["blocking_issues"] = [item["issue"] for item in issue_assessments]
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
        roles = list(dict.fromkeys(
            str(item.get("critic_role") or "")
            for item in records
            if str(item.get("critic_role") or "")
        ))
        data["critic_panel"] = {
            "mode": CODEX_REVIEW_MODE,
            "roles": roles[:8],
            "review_count": len(records),
        }
        return {
            "provider": "codex",
            "ok": True,
            "review_mode": CODEX_REVIEW_MODE,
            "critic_roles": roles[:8],
            "data": data,
        }

    try:
        base_tex_for_panel = (cv_root(repo_root()) / CANONICAL_TEMPLATE).read_text(errors="replace")
    except OSError:
        base_tex_for_panel = ""

    def critique_current(round_label: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
        nonlocal plan, packing, chosen, layout, preview
        critic_roles = list(CODEX_CRITIC_ROLES) if writer == "codex" and "codex" in available else []
        if not critic_roles:
            critique = combined_critique([])
            write_json(run_dir / (round_label + ".json"), critique)
            return critique, [], False
        # Geometry is a deterministic prerequisite for a useful panel. Older
        # flow judged a near-wrap first and only attempted source restoration
        # after the panel, which could spend four Luna calls on an artifact
        # that the compiler would later reject. Restore approved source text
        # and compact before sending the packet; this keeps the evaluator
        # independent while removing avoidable review latency.
        if not (layout.get("horizontal") or {}).get("pass"):
            pre_panel_root = run_dir / (round_label + "_pre_panel_geometry")
            pre_panel_record: Dict[str, Any] = {"attempted": True, "status": "unsafe"}
            try:
                recovered_plan, restored_ids = restore_wrapped_source_text(
                    plan, layout, catalog,
                )
                source_root = pre_panel_root / "source_restore"
                _, source_layout, _ = render_candidate(recovered_plan, source_root)
                compacted_plan, _, compactions = compact_plan_to_geometry(
                    recovered_plan, source_layout, catalog, pre_panel_root,
                )
                changed = bool(restored_ids or compactions)
                if changed:
                    candidate_root = pre_panel_root / "candidate"
                    candidate_tex, candidate_layout, candidate_preview = render_candidate(
                        compacted_plan, candidate_root,
                    )
                    candidate_safe = bool(
                        candidate_layout.get("compiled")
                        and candidate_layout.get("pages") == 1
                        and not candidate_layout.get("overfull")
                        and (candidate_layout.get("horizontal") or {}).get("pass")
                    )
                    pre_panel_record.update({
                        "restored_source_ids": list(restored_ids),
                        "compactions": list(compactions),
                        "candidate": {
                            "safe_render": candidate_safe,
                            "wrap_count": (candidate_layout.get("horizontal") or {}).get("wrap_count", 0),
                            "near_wrap_count": (candidate_layout.get("horizontal") or {}).get("near_wrap_count", 0),
                        },
                    })
                    if candidate_safe:
                        plan, chosen, layout, preview = (
                            compacted_plan, candidate_tex, candidate_layout, candidate_preview,
                        )
                        packing[round_label + "_pre_panel_geometry"] = pre_panel_record
                        write_json(run_dir / "content_plan.json", plan)
                        write_json(run_dir / "layout_packing.json", packing)
                        pre_panel_record["status"] = "accepted"
                    else:
                        pre_panel_record["status"] = "rejected_candidate_unsafe"
                else:
                    pre_panel_record["status"] = "no_deterministic_change"
            except (OSError, RuntimeError, ValueError) as exc:
                pre_panel_record["status"] = "error"
                pre_panel_record["error"] = str(exc)
            write_json(run_dir / (round_label + "_pre_panel_geometry.json"), pre_panel_record)
        substantive_bullets = sum(
            len(entry.get("bullets", []) or [])
            for section in ("experiences", "projects", "leadership")
            for entry in (plan.get(section, []) or [])
        )
        initial_substantive_bullets = substantive_bullets
        # A provider/editor mutation must never send an empty or stale render
        # to the independent jury. Recover the last persisted source-addressed
        # plan when possible; otherwise fail closed without spending four Luna
        # calls on an artifact that cannot be a resume.
        if substantive_bullets <= 0 or not chosen or "resumeItem" not in chosen:
            persisted_plan = read_json(run_dir / "content_plan.json", {}) or {}
            persisted_bullets = sum(
                len(entry.get("bullets", []) or [])
                for section in ("experiences", "projects", "leadership")
                for entry in (persisted_plan.get(section, []) or [])
            ) if isinstance(persisted_plan, dict) else 0
            if persisted_bullets > substantive_bullets:
                plan = persisted_plan
                chosen, layout, preview = render_candidate(plan, run_dir)
                substantive_bullets = persisted_bullets
            elif substantive_bullets > 0:
                chosen, layout, preview = render_candidate(plan, run_dir)
            write_json(run_dir / (round_label + "_candidate_guard.json"), {
                "initial_bullets": initial_substantive_bullets,
                "persisted_bullets": persisted_bullets,
                "render_has_resume_items": "resumeItem" in chosen if chosen else False,
                "decision": "recovered_persisted_plan" if persisted_bullets > 0 else "failed_closed",
            })
        if substantive_bullets <= 0 or not chosen or "resumeItem" not in chosen:
            critique = combined_critique([])
            critique["critic_round"] = round_label
            critique["candidate_guard"] = {
                "available": False,
                "reason": "candidate had no substantive rendered evidence; sealed panel skipped",
            }
            write_json(run_dir / (round_label + ".json"), critique)
            return critique, [], False
        recheck = round_label != "critique"
        update(
            "reviewing",
            "Running sealed Codex Luna critic roles at %s%s: %s" % (
                profile.get("evaluator_effort") or resume_evaluator.CODEX_EFFORT,
                " (recheck)" if recheck else "",
                ", ".join(item["key"] for item in critic_roles),
            ),
        )
        panel_deterministic = deterministic_review(
            context, chosen, layout, plan=plan, catalog=catalog,
        )
        panel_changes = content_change_report(
            plan, catalog, chosen, context.get("target_keywords"),
            base_tex=base_tex_for_panel,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(critic_roles)) as pool:
            futures = {
                pool.submit(
                    run_sealed_evaluator,
                    sealed_evaluator_packet(
                        role=role["key"], context=context,
                        base_tex=base_tex_for_panel, tailored_tex=chosen,
                        plan=plan, graph_context=graph_context, catalog=catalog,
                        deterministic=panel_deterministic, changes=panel_changes,
                        run_id=run_dir.name,
                    ),
                    run_dir,
                    round_label + "_" + role["key"],
                    timeout=int(profile.get("evaluator_timeout_seconds") or 8 * 60),
                    evaluator_effort=profile.get("evaluator_effort") or resume_evaluator.CODEX_EFFORT,
                ): role for role in critic_roles
            }
        # Futures complete in arbitrary order; retain the role key alongside
        # each result so the durable report explains which Luna sub-agent
        # produced each critique.
        role_records = []
        for future in concurrent.futures.as_completed(futures):
            role = futures[future]
            record = future.result()
            record["critic_role"] = role["key"]
            record["critic_role_label"] = role["label"]
            role_records.append(record)
        records = role_records
        for record in records:
            record["critic_round"] = round_label
            record["label"] = round_label + "_" + str(record.get("critic_role") or "critic")
            write_json(run_dir / (round_label + "_" + str(record.get("critic_role") or "critic") + ".json"), record)
        usable = [record for record in records if record.get("ok")]
        critique = combined_critique(usable)
        critique["critic_round"] = round_label
        panel_status = sealed_panel_status(records, critic_roles)
        critique["evaluator_contract"] = {
            "version": SEALED_EVALUATOR_CONTRACT,
            "fingerprint": resume_evaluator.contract_fingerprint(),
            "rubric_sha256": resume_evaluator.EVALUATOR_RUBRIC_SHA256,
            "execution_lane": "sealed_evaluator",
            **panel_status,
        }
        write_json(run_dir / (round_label + ".json"), critique)
        # Keep failed/time-limited role calls in the durable report.  They do
        # not count as a usable panel, but hiding them makes usage and
        # evaluator reliability look better than it was.
        # A panel is usable for readiness only when every required role has
        # returned a valid, attested result. Partial feedback can inform a
        # revision, but it can never turn into a pass.
        return critique, records, bool(panel_status["complete"])

    critique, critique_records, review_available = critique_current("critique")
    revision_records: List[Dict[str, Any]] = []
    revision_log: List[Dict[str, Any]] = []
    for revision_round in range(1, int(profile["revision_rounds"]) + 1):
        critique_data = critique.get("data") or {}
        statuses = [str((critique_data.get("criteria") or {}).get(name, {}).get("status") or "fail") for name in REVIEW_CRITERIA]
        if not enhance or not review_available or (not critique_data.get("blocking_issues") and all(status == "pass" for status in statuses)):
            break
        label = "revision" if revision_round == 1 else "revision_%s" % revision_round
        update(
            "revising",
            "Codex Luna is applying the critic-panel feedback (pass %s/%s)"
            % (revision_round, profile["revision_rounds"]),
        )
        revision = run_provider(
            writer, revision_prompt(
                context, plan, critique, catalog, graph=graph,
                unrestricted=unrestricted, generation=generation,
            ),
            # Repair calls receive the full run budget; a shorter timeout
            # turns a repairable critique into a stale candidate. The initial
            # author remains Max; repeated repair is deliberately High
            # after the lab showed Max spending its whole budget without a
            # structured replacement.
            run_dir, label, timeout=RUN_TIMEOUT_SECONDS, codex_effort="high",
            schema=plan_schema(True, generation=generation),
        )
        revision_records.append(revision)
        revision["label"] = label
        write_json(run_dir / (label + ".json"), revision)
        if not revision.get("ok"):
            revision_log.append({"round": revision_round, "status": "failed", "reason": revision.get("error", "provider failed")})
            break
        revised_plan, revision_errors = validate_plan(
            revision.get("data") or {}, catalog, True, graph=graph,
            generation=generation,
        )
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
        critique, new_records, review_available = critique_current(label + "_critique")
        critique_records.extend(new_records)
    # A revision is allowed to change wording and flexible front matter, so
    # the final density contract must be checked again after the last critique
    # pass rather than trusting the pre-critique measurement.
    fill_post_line_capacity("post_revision_density")
    write_json(run_dir / "revision_log.json", revision_log)
    audit_repair_records: List[Dict[str, Any]] = []
    audit_repair_log: List[Dict[str, Any]] = []
    final_geometry_recovery: Dict[str, Any] = {
        "attempted": False,
        "status": "not_needed",
        "restored_source_ids": [],
        "compactions": [],
    }
    try:
        base_tex_for_audit = (cv_root(repo_root()) / CANONICAL_TEMPLATE).read_text(errors="replace")
    except OSError:
        base_tex_for_audit = ""
    initial_deterministic = deterministic_review(
        context, chosen, layout, plan=plan, catalog=catalog,
    )
    initial_scored = score_review(
        critique, initial_deterministic, independent_available=review_available,
        review_mode=str(critique.get("review_mode") or "unavailable"),
        critic_roles=critique.get("critic_roles") or [],
    )
    initial_changes = content_change_report(
        plan, catalog, chosen, context.get("target_keywords"), base_tex=base_tex_for_audit,
    )
    initial_audit = build_tailoring_audit(
        job, context, match, graph, plan, initial_changes,
        initial_deterministic, initial_scored, base_tex_for_audit, chosen,
        run_id=run_id, queue_id=queue_id,
    )
    initial_candidate_delta = candidate_delta_summary(initial_changes)
    repair_needed = enhance and bool(profile.get("audit_repair")) and initial_candidate_delta["material"] and (
        initial_audit.get("tailoring") == "regressed"
        or any(
            item.get("classification") in {"REGRESSION", "BLOCKER"}
            for item in initial_audit.get("findings") or []
            if isinstance(item, dict)
        )
    )
    if enhance and bool(profile.get("audit_repair")) and not initial_candidate_delta["material"]:
        audit_repair_log.append({
            "status": "skipped",
            "reason": "candidate matched the canonical control in all material dimensions; semantic repair would add latency without a changed artifact",
            "candidate_delta": initial_candidate_delta,
            "post_density": {
                "status": "not_run",
                "reason": "no material base-to-candidate delta",
            },
        })
    if repair_needed:
        update("audit_repair", "Repairing source-aware regressions before selecting the final draft")
        feedback = tailoring_repair_feedback(initial_audit, initial_changes)
        repair_record = run_provider(
            writer,
            tailoring_repair_prompt(
                context, plan, feedback, catalog, graph=graph,
                unrestricted=unrestricted, generation=generation,
            ),
            run_dir, "audit_repair", timeout=RUN_TIMEOUT_SECONDS, codex_effort="high",
            schema=plan_schema(True, generation=generation),
        )
        repair_record["label"] = "audit_repair"
        audit_repair_records.append(repair_record)
        write_json(run_dir / "audit_repair.json", repair_record)
        if not repair_record.get("ok"):
            audit_repair_log.append({
                "status": "failed",
                "reason": str(repair_record.get("error") or "repair provider failed"),
                "post_density": {
                    "status": "not_run",
                    "reason": "audit repair did not produce a validated candidate",
                },
            })
        else:
            repaired_plan, repair_errors = validate_plan(
                repair_record.get("data") or {}, catalog, True,
                graph=graph, generation=generation,
            )
            if repair_errors:
                audit_repair_log.append({
                    "status": "rejected",
                    "errors": repair_errors[:12],
                    "post_density": {
                        "status": "not_run",
                        "reason": "audit repair plan failed source validation",
                    },
                })
                write_json(run_dir / "audit_repair_errors.json", repair_errors)
            else:
                repair_root = run_dir / "audit_repair_candidate"
                try:
                    prior_repair_state = (plan, chosen, layout, preview)
                    # The repair writer is judged as a new candidate, but the
                    # packing stage must not silently trade away a stronger
                    # canonical proof line before that fresh jury sees it.
                    # Reuse only the source-verbatim canonical replacement
                    # guard; overlap trimming remains disabled for repairs so
                    # the repair writer's portfolio decision is not edited a
                    # second time by a deterministic heuristic.
                    repaired_plan, repair_control_recovery = deterministic_control_recovery(
                        repaired_plan,
                        catalog,
                        context.get("target_keywords"),
                        repair_root / "control_recovery",
                        trim_overlap=False,
                    )
                    repaired_plan, repair_packing = pack_plan_to_page(
                        repaired_plan, catalog, repair_root,
                    )
                    repair_packing["control_recovery"] = repair_control_recovery
                    repaired_tex, repaired_layout, repaired_preview = render_candidate(
                        repaired_plan, repair_root,
                    )
                    repair_geometry_recovery: Dict[str, Any] = {
                        "attempted": False,
                        "status": "not_needed",
                    }
                    safe_render = bool(
                        repaired_layout.get("compiled")
                        and repaired_layout.get("pages") == 1
                        and not repaired_layout.get("overfull")
                        and (repaired_layout.get("horizontal") or {}).get("pass")
                    )
                    if not safe_render:
                        (
                            repaired_plan, repaired_tex, repaired_layout, repaired_preview,
                            safe_render, repair_geometry_recovery,
                        ) = recover_repair_geometry(
                            repaired_plan,
                            repaired_layout,
                            repair_root / "geometry_recovery",
                        )
                        repair_packing["geometry_recovery"] = repair_geometry_recovery
                    plan, chosen, layout, preview = (
                        repaired_plan, repaired_tex, repaired_layout, repaired_preview,
                    )
                    if safe_render:
                        # The repair writer changed the candidate after the
                        # last critic. Evaluate this exact replacement in a
                        # fresh sealed panel before comparing or accepting it.
                        # Reusing the old critique here would let a repair pass
                        # without being judged for newly introduced claims or
                        # regressions.
                        repair_critique, repair_records, repair_review_available = critique_current(
                            "audit_repair_critique"
                        )
                        critique_records.extend(repair_records)
                    else:
                        # Geometry is a deterministic prerequisite. Do not
                        # spend four Luna calls judging a candidate that cannot
                        # ship regardless of its prose; retain an explicit
                        # unavailable receipt so the audit cannot mistake a
                        # skipped panel for approval.
                        repair_critique = {
                            "provider": "codex",
                            "ok": False,
                            "review_mode": "unavailable",
                            "critic_roles": [],
                            "data": {
                                "criteria": {
                                    name: {"status": "fail", "reason": "candidate failed deterministic geometry gate"}
                                    for name in REVIEW_CRITERIA
                                },
                                "blocking_issues": [
                                    "Repair candidate failed the deterministic one-page/horizontal geometry gate; sealed recheck skipped."
                                ],
                                "blocking_issue_assessments": [],
                                "line_feedback": [],
                                "unsupported_claims": [],
                                "missing_evidence": [],
                                "revision_priorities": [],
                                "decision_feedback": [],
                                "portfolio_comparison": {
                                    "status": "fail",
                                    "reason": "candidate failed deterministic geometry gate",
                                    "preserved_strengths": [],
                                    "gained_strengths": [],
                                    "lost_strengths": [],
                                },
                            },
                        }
                        repair_records = []
                        repair_review_available = False
                    repaired_deterministic = deterministic_review(
                        context, repaired_tex, repaired_layout,
                        plan=repaired_plan, catalog=catalog,
                    )
                    repaired_scored = score_review(
                        repair_critique, repaired_deterministic,
                        independent_available=repair_review_available,
                        review_mode=str(repair_critique.get("review_mode") or "unavailable"),
                        critic_roles=repair_critique.get("critic_roles") or [],
                    )
                    repaired_changes = content_change_report(
                        repaired_plan, catalog, repaired_tex,
                        context.get("target_keywords"), base_tex=base_tex_for_audit,
                    )
                    repaired_audit = build_tailoring_audit(
                        job, context, match, graph, repaired_plan, repaired_changes,
                        repaired_deterministic, repaired_scored,
                        base_tex_for_audit, repaired_tex,
                        run_id=run_id, queue_id=queue_id,
                    )
                    current_key = tailoring_audit_preference_key(initial_audit)
                    repaired_key = tailoring_audit_preference_key(repaired_audit)
                    if safe_render and repair_review_available and repaired_key > current_key:
                        critique = repair_critique
                        review_available = repair_review_available
                        packing["audit_repair"] = repair_packing
                        packing["audit_repair_comparison"] = {
                            "before": current_key,
                            "after": repaired_key,
                            "before_audit": initial_audit.get("comparison", {}),
                            "after_audit": repaired_audit.get("comparison", {}),
                        }
                        write_json(run_dir / "content_plan.json", plan)
                        write_json(run_dir / "layout_packing.json", packing)
                        chosen, layout, preview = render_candidate(plan, run_dir)
                        audit_repair_log.append({
                            "status": "accepted",
                            "before": current_key,
                            "after": repaired_key,
                            "tailoring": repaired_audit.get("tailoring"),
                            "recommended_version": repaired_audit.get("recommended_version"),
                        })

                        # The repair writer is packed independently from the
                        # prior candidate. It can restore a broad source-aware
                        # plan, have the one-page packer trim it, and still
                        # leave measurable room after that trim. Refill this
                        # exact repaired artifact, then judge the changed
                        # artifact in a fresh sealed panel. If the refill does
                        # not improve the source-aware comparison or its panel
                        # is incomplete, restore the already accepted repair.
                        pre_density_plan = copy.deepcopy(plan)
                        pre_density_digest = _stable_digest(plan)
                        pre_density_state = (
                            copy.deepcopy(plan), chosen, copy.deepcopy(layout), preview,
                            copy.deepcopy(critique), review_available,
                            copy.deepcopy(packing), copy.deepcopy(line_compactions),
                            copy.deepcopy(space_expansion),
                        )
                        fill_post_line_capacity("post_audit_repair_density")
                        if _stable_digest(plan) != pre_density_digest:
                            post_density_record: Dict[str, Any] = {
                                "status": "attempted",
                                "before_bullet_count": portfolio_metrics(pre_density_plan).get("total_bullets"),
                                "after_bullet_count": portfolio_metrics(plan).get("total_bullets"),
                            }
                            try:
                                post_density_critique, post_density_records, post_density_available = critique_current(
                                    "post_audit_repair_critique"
                                )
                                critique_records.extend(post_density_records)
                                post_density_deterministic = deterministic_review(
                                    context, chosen, layout, plan=plan, catalog=catalog,
                                )
                                post_density_changes = content_change_report(
                                    plan, catalog, chosen,
                                    context.get("target_keywords"),
                                    base_tex=base_tex_for_audit,
                                )
                                post_density_scored = score_review(
                                    post_density_critique, post_density_deterministic,
                                    independent_available=post_density_available,
                                    review_mode=str(post_density_critique.get("review_mode") or "unavailable"),
                                    critic_roles=post_density_critique.get("critic_roles") or [],
                                )
                                post_density_audit = build_tailoring_audit(
                                    job, context, match, graph, plan, post_density_changes,
                                    post_density_deterministic, post_density_scored,
                                    base_tex_for_audit, chosen,
                                    run_id=run_id, queue_id=queue_id,
                                )
                                post_density_key = tailoring_audit_preference_key(post_density_audit)
                                post_density_record.update({
                                    "available": post_density_available,
                                    "before": repaired_key,
                                    "after": post_density_key,
                                    "tailoring": post_density_audit.get("tailoring"),
                                    "recommended_version": post_density_audit.get("recommended_version"),
                                })
                                if post_density_available and post_density_key > repaired_key:
                                    critique = post_density_critique
                                    review_available = post_density_available
                                    post_density_record["status"] = "accepted"
                                else:
                                    (
                                        plan, chosen, layout, preview, critique,
                                        review_available, packing, line_compactions,
                                        space_expansion,
                                    ) = pre_density_state
                                    chosen, layout, preview = render_candidate(plan, run_dir)
                                    write_json(run_dir / "content_plan.json", plan)
                                    write_json(run_dir / "layout_packing.json", packing)
                                    post_density_record["status"] = "rejected"
                                    post_density_record["reason"] = (
                                        "post-repair density refill did not improve its sealed source-aware "
                                        "comparison or did not return a complete panel"
                                    )
                            except (OSError, RuntimeError, ValueError) as exc:
                                (
                                    plan, chosen, layout, preview, critique,
                                    review_available, packing, line_compactions,
                                    space_expansion,
                                ) = pre_density_state
                                chosen, layout, preview = render_candidate(plan, run_dir)
                                write_json(run_dir / "content_plan.json", plan)
                                write_json(run_dir / "layout_packing.json", packing)
                                post_density_record.update({
                                    "status": "rejected",
                                    "reason": "post-repair density jury failed: %s" % exc,
                                })
                            audit_repair_log[-1]["post_density"] = post_density_record
                        else:
                            audit_repair_log[-1]["post_density"] = {
                                "status": "not_needed",
                                "before_bullet_count": portfolio_metrics(pre_density_plan).get("total_bullets"),
                                "after_bullet_count": portfolio_metrics(plan).get("total_bullets"),
                            }
                    else:
                        plan, chosen, layout, preview = prior_repair_state
                        audit_repair_log.append({
                            "status": "rejected",
                            "reason": "repair did not improve the source-aware comparison, failed layout safety, or failed its sealed recheck",
                            "before": current_key,
                            "after": repaired_key,
                            "safe_render": safe_render,
                            "geometry_recovery": repair_geometry_recovery,
                            "sealed_recheck": {
                                "available": repair_review_available,
                                "roles": repair_critique.get("critic_roles") or [],
                            },
                            "post_density": {
                                "status": "not_run",
                                "reason": "post-repair refill requires an accepted, one-page, sealed-rechecked repair",
                            },
                        })
                except (OSError, RuntimeError, ValueError) as exc:
                    plan, chosen, layout, preview = prior_repair_state
                    audit_repair_log.append({
                        "status": "rejected",
                        "reason": "repair candidate failed compilation or validation: %s" % exc,
                        "post_density": {
                            "status": "not_run",
                            "reason": "audit repair candidate failed before the refill prerequisite",
                        },
                    })

    # A repair can be source-valid and still leave the previously selected
    # candidate with a wrapped/near-wrapped line.  The hard geometry gate must
    # remain hard, but this is a safe last-mile recovery opportunity before
    # rejecting the entire run: restore the exact authorized source wording,
    # try bounded deterministic compactions, then re-run the sealed panel on
    # the exact recovered artifact. No evaluator criterion or pass threshold
    # is changed here.
    if not (layout.get("horizontal") or {}).get("pass"):
        final_geometry_recovery["attempted"] = True
        final_geometry_recovery["status"] = "candidate_unsafe"
        recovery_root = run_dir / "final_geometry_recovery"
        prior_geometry_state = (plan, chosen, layout, preview, critique, review_available)
        try:
            recovered_plan, restored_ids = restore_wrapped_source_text(
                plan, layout, catalog,
            )
            final_geometry_recovery["restored_source_ids"] = list(restored_ids)
            source_root = recovery_root / "source_restore"
            _, source_layout, _ = render_candidate(recovered_plan, source_root)
            compacted_plan, _, compactions = compact_plan_to_geometry(
                recovered_plan, source_layout, catalog, recovery_root,
            )
            final_geometry_recovery["compactions"] = list(compactions)
            changed = bool(restored_ids or compactions)
            if changed:
                candidate_root = recovery_root / "candidate"
                candidate_tex, candidate_layout, candidate_preview = render_candidate(
                    compacted_plan, candidate_root,
                )
                candidate_safe = bool(
                    candidate_layout.get("compiled")
                    and candidate_layout.get("pages") == 1
                    and not candidate_layout.get("overfull")
                    and (candidate_layout.get("horizontal") or {}).get("pass")
                )
                final_geometry_recovery["candidate"] = {
                    "safe_render": candidate_safe,
                    "wrap_count": (candidate_layout.get("horizontal") or {}).get("wrap_count", 0),
                    "near_wrap_count": (candidate_layout.get("horizontal") or {}).get("near_wrap_count", 0),
                }
                if candidate_safe:
                    # Critique the recovered artifact, not the stale unsafe
                    # candidate. A deterministic wording change still needs a
                    # fresh complete panel before it can be selected.
                    plan, chosen, layout, preview = (
                        compacted_plan, candidate_tex, candidate_layout, candidate_preview,
                    )
                    recovery_critique, recovery_records, recovery_available = critique_current(
                        "final_geometry_critique"
                    )
                    critique_records.extend(recovery_records)
                    if recovery_available:
                        critique = recovery_critique
                        review_available = recovery_available
                        packing["final_geometry_recovery"] = {
                            "restored_source_ids": list(restored_ids),
                            "compactions": list(compactions),
                            "candidate": final_geometry_recovery.get("candidate", {}),
                        }
                        write_json(run_dir / "content_plan.json", plan)
                        write_json(run_dir / "layout_packing.json", packing)
                        chosen, layout, preview = render_candidate(plan, run_dir)
                        final_geometry_recovery["status"] = "accepted_after_sealed_recheck"
                    else:
                        # The geometry repair is still valuable even when the
                        # optional fresh jury misses its latency budget. Keep
                        # the safe artifact, mark the independent review
                        # unavailable, and let the normal fail-closed audit
                        # select the base rather than restoring an unsafe PDF.
                        final_geometry_recovery["status"] = "accepted_geometry_only_incomplete_sealed_recheck"
                        critique = combined_critique([])
                        critique["critic_round"] = "final_geometry_critique"
                        critique["candidate_guard"] = {
                            "available": False,
                            "reason": "safe deterministic recovery retained, but its sealed recheck was incomplete",
                        }
                        write_json(run_dir / "final_geometry_critique.json", critique)
                        review_available = False
                else:
                    final_geometry_recovery["status"] = "rejected_candidate_unsafe"
                    plan, chosen, layout, preview, critique, review_available = prior_geometry_state
            else:
                final_geometry_recovery["status"] = "no_safe_deterministic_change"
        except (OSError, RuntimeError, ValueError) as exc:
            final_geometry_recovery["status"] = "recovery_error"
            final_geometry_recovery["error"] = str(exc)
            plan, chosen, layout, preview, critique, review_available = prior_geometry_state
        write_json(run_dir / "final_geometry_recovery.json", final_geometry_recovery)

    # Revision, density, and audit-repair passes are allowed to return a new
    # source-addressed plan. That is useful, but it means the earlier packer's
    # duplicate guard is no longer necessarily the guard for the final PDF.
    # Re-apply the bounded policy to the exact artifact and seal-review any
    # real change. This closes the loophole where a later writer reintroduced
    # the repeated 10,000-sample/2.74%-validation story after it was initially
    # removed.
    final_portfolio_guard: Dict[str, Any] = {
        "attempted": False,
        "changed": False,
        "status": "not_run",
    }
    guarded_plan, guard_record = deterministic_final_portfolio_guard(plan, catalog)
    final_portfolio_guard.update(guard_record)
    if guard_record.get("changed"):
        prior_guard_state = (plan, chosen, layout, preview, critique, review_available)
        guard_root = run_dir / "final_portfolio_guard"
        try:
            guard_tex, guard_layout, guard_preview = render_candidate(
                guarded_plan, guard_root,
            )
            safe_guard = bool(
                guard_layout.get("compiled")
                and guard_layout.get("pages") == 1
                and not guard_layout.get("overfull")
                and (guard_layout.get("horizontal") or {}).get("pass")
            )
            final_portfolio_guard["safe_render"] = safe_guard
            if safe_guard:
                plan, chosen, layout, preview = (
                    guarded_plan, guard_tex, guard_layout, guard_preview,
                )
                packing["final_portfolio_guard"] = final_portfolio_guard
                write_json(run_dir / "content_plan.json", plan)
                write_json(run_dir / "layout_packing.json", packing)
                guard_critique, guard_records, guard_available = critique_current(
                    "final_portfolio_guard_critique"
                )
                critique_records.extend(guard_records)
                final_portfolio_guard["sealed_recheck"] = {
                    "available": guard_available,
                    "roles": guard_critique.get("critic_roles") or [],
                }
                if guard_available:
                    critique = guard_critique
                    review_available = guard_available
                    final_portfolio_guard["status"] = "accepted_after_sealed_recheck"
                else:
                    # Never reuse the prior panel for a changed artifact. The
                    # safe deterministic result remains inspectable, but the
                    # audit must fail closed until its exact draft is judged.
                    critique = combined_critique([])
                    critique["critic_round"] = "final_portfolio_guard_critique"
                    review_available = False
                    final_portfolio_guard["status"] = "accepted_geometry_only_incomplete_sealed_recheck"
            else:
                plan, chosen, layout, preview, critique, review_available = prior_guard_state
                final_portfolio_guard["status"] = "rejected_unsafe_render"
        except (OSError, RuntimeError, ValueError) as exc:
            plan, chosen, layout, preview, critique, review_available = prior_guard_state
            final_portfolio_guard["status"] = "rejected_error"
            final_portfolio_guard["error"] = str(exc)
    else:
        final_portfolio_guard["status"] = "not_needed"
    write_json(run_dir / "final_portfolio_guard.json", final_portfolio_guard)

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
    scored = score_review(
        critique, deterministic, independent_available=review_available,
        review_mode=str(critique.get("review_mode") or "unavailable"),
        critic_roles=critique.get("critic_roles") or [],
    )
    synthesis_data = plan
    provider_records = []
    all_provider_records = (
        gap_records + drafts + [synthesis] + space_expansion_records + line_edits
        + revision_records + critique_records + audit_repair_records
    )
    for record in all_provider_records:
        provider = str(record.get("provider") or "")
        provider_records.append({
            "label": str(record.get("label") or "provider"),
            "provider": provider,
            "execution_lane": str(record.get("execution_lane") or "writer_provider"),
            "model": provider_model_label(provider),
            "reasoning_effort": str(record.get("reasoning_effort") or ""),
            "ok": record.get("ok"), "called": not record.get("skipped", False),
            "elapsed_seconds": record.get("elapsed_seconds"), "usage_tokens": record.get("usage_tokens"),
            "contract_version": record.get("contract_version"),
            "input_sha256": record.get("input_sha256"),
        })
    known_by_provider = {
        "codex": [
            int(item.get("usage_tokens"))
            for item in provider_records
            if item.get("called")
            and str(item.get("provider") or "").split("/")[-1] == "codex"
            and item.get("usage_tokens") is not None
        ]
    }
    try:
        base_tex = (cv_root(repo_root()) / CANONICAL_TEMPLATE).read_text(errors="replace")
    except OSError:
        base_tex = ""
    changes = content_change_report(
        plan, catalog, chosen, context.get("target_keywords"), base_tex=base_tex,
    )
    tailoring_audit = build_tailoring_audit(
        job, context, match, graph, plan, changes, deterministic, scored,
        base_tex, chosen, run_id=run_id, queue_id=queue_id,
        comparison_control=comparison_control,
    )
    winner = adopt_base_control_winner(run_dir, tailoring_audit)
    write_json(run_dir / "job_intelligence.json", context.get("job_intelligence", {}))
    write_json(run_dir / "tailoring_audit.json", tailoring_audit)
    review_overlay = (
        {
            "available": False,
            "winner_version": "base",
            "reason": "The canonical base PDF is the selected winner; inspect tailored_candidate.pdf for the rejected candidate.",
        }
        if winner.get("winner_version") == "base"
        else review_preview_overlay(
            run_pdf_path(run_dir), plan, changes, changes.get("keyword_coverage")
        )
    )
    space_audit_value = space_audit(plan, layout, catalog, space_expansion)
    provider_flow = [
        {
            "label": item.get("label"),
            "provider": item.get("provider"),
            "model": item.get("model"),
            "reasoning_effort": item.get("reasoning_effort"),
            "status": "complete" if item.get("ok") else "failed" if item.get("called") else "skipped",
            "elapsed_seconds": item.get("elapsed_seconds"),
            "usage_tokens": item.get("usage_tokens"),
        }
        for item in provider_records
    ]
    critic_attempts = [
        item for item in critique_records
        if str(item.get("critic_role") or "")
    ]
    critic_roles_attempted = list(dict.fromkeys(
        str(item.get("critic_role") or "") for item in critic_attempts
        if str(item.get("critic_role") or "")
    ))
    critic_roles_completed = list(dict.fromkeys(
        str(item.get("critic_role") or "") for item in critic_attempts
        if item.get("ok") and str(item.get("critic_role") or "")
    ))
    critic_roles_failed = list(dict.fromkeys(
        str(item.get("critic_role") or "") for item in critic_attempts
        if not item.get("ok") and str(item.get("critic_role") or "")
    ))
    critic_history_by_round: Dict[str, Dict[str, Any]] = {}
    for item in critic_attempts:
        round_label = str(item.get("critic_round") or "unknown")
        history = critic_history_by_round.setdefault(round_label, {
            "round": round_label, "attempted_roles": [],
            "completed_roles": [], "failed_roles": [],
        })
        role = str(item.get("critic_role") or "")
        if role and role not in history["attempted_roles"]:
            history["attempted_roles"].append(role)
        target = "completed_roles" if item.get("ok") else "failed_roles"
        if role and role not in history[target]:
            history[target].append(role)
        if not item.get("ok") and item.get("error"):
            history.setdefault("failures", []).append({
                "role": role, "reason": str(item.get("error") or "")[:240],
            })
    critic_history = list(critic_history_by_round.values())
    final_round = str(critique.get("critic_round") or "")
    final_panel_status = sealed_panel_status(
        [item for item in critic_attempts if str(item.get("critic_round") or "") == final_round],
        CODEX_CRITIC_ROLES,
    )
    report = {
        "mode": mode_label,
        "pdf_filename": run_pdf_path(run_dir).name,
        "preview_filename": run_preview_path(run_dir).name if preview else "",
        "winner_version": winner.get("winner_version", "tailored"),
        "winner_artifact": winner,
        "job": job_summary(job),
        "resume_match": match,
        "positioning_thesis": synthesis_data.get("positioning_thesis", ""),
        "selected_evidence": synthesis_data.get("selected_evidence", []),
        "excluded_evidence": synthesis_data.get("excluded_evidence", []),
        "revision_notes": synthesis_data.get("revision_notes", []),
        "decision_ledger": synthesis_data.get("decision_ledger", []),
        "front_matter_policy": synthesis_data.get("front_matter_policy", {"coursework": "keep", "awards": "keep"}),
        "front_matter_rewrites": synthesis_data.get("front_matter_rewrites", []),
        "generation_strategy": context.get("generation_strategy", {}),
        "company_context": context.get("company_context", {}),
        "tailoring_brief": context.get("tailoring_brief", {}),
        "job_intelligence": context.get("job_intelligence", {}),
        "posting_snapshot_hash": context.get("posting_snapshot_hash", ""),
        "queue_id": queue_id,
        "run_id": run_id,
        "line_compactions": line_compactions,
        "final_geometry_recovery": final_geometry_recovery,
        "target_opportunity": target_opportunity,
        "role_evidence_floor": role_evidence_floor,
        "audit_repair_log": audit_repair_log,
        "post_line_density": post_line_density,
        "validation_warnings": synthesis_data.get("validation_warnings", []),
        "content_changes": changes,
        "tailoring_audit": tailoring_audit,
        "tailoring_audit_summary": tailoring_audit_summary(tailoring_audit),
        "comparison_control": comparison_control_summary(comparison_control),
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
        "content_plan": {section: plan.get(section, []) for section in ("experiences", "projects", "leadership")},
        "layout_packing": packing,
        "format_contract": {"template": "CV/" + CANONICAL_TEMPLATE, "model_can_write_latex_document": False, "font_size_reduction_percent": 0.0, "font_size_increase_percent": 0.0, "allowed_max_reduction_percent": MAX_STYLE_REDUCTION_PERCENT},
        "providers": provider_records,
        "provider_policy": context["provider_policy"],
        "quality_profile": quality_profile,
        "critic_panel": {
            "available": review_available,
            "mode": str(critique.get("review_mode") or "unavailable"),
            "contract_version": SEALED_EVALUATOR_CONTRACT,
            "contract_fingerprint": resume_evaluator.contract_fingerprint(),
            "rubric_sha256": resume_evaluator.EVALUATOR_RUBRIC_SHA256,
            "execution_lane": "sealed_evaluator",
            "all_required_roles": bool(final_panel_status["complete"]),
            "roles": list(critique.get("critic_roles") or [])[:8],
            "attempted_roles": critic_roles_attempted[:8],
            "completed_roles": final_panel_status["completed_roles"][:8],
            "failed_roles": final_panel_status["failed_roles"][:8],
            "history": critic_history[-4:],
            "separate_vendor": False,
        },
        "evidence_graph": {
            "version": graph.get("version"),
            "hash": graph.get("hash"),
            "review_summary": graph.get("review_summary") or {},
            "markdown_sources": markdown_sources,
        },
        "usage": {
            **{name + "_tokens": sum(values) for name, values in known_by_provider.items()},
            "codex_calls": sum(
                1 for item in provider_records
                if item.get("called") and str(item.get("provider") or "").split("/")[-1] == "codex"
            ),
            "codex_complete": all(item.get("usage_tokens") is not None for item in provider_records if item.get("called") and str(item.get("provider") or "").split("/")[-1] == "codex"),
        },
        "review": scored,
        "critique": critique,
        "review_panel": {
            "available": review_available,
            "mode": str(critique.get("review_mode") or "unavailable"),
            "contract_version": SEALED_EVALUATOR_CONTRACT,
            "contract_fingerprint": resume_evaluator.contract_fingerprint(),
            "rubric_sha256": resume_evaluator.EVALUATOR_RUBRIC_SHA256,
            "execution_lane": "sealed_evaluator",
            "all_required_roles": bool(final_panel_status["complete"]),
            "roles": list(critique.get("critic_roles") or [])[:8],
            "attempted_roles": critic_roles_attempted[:8],
            "completed_roles": final_panel_status["completed_roles"][:8],
            "failed_roles": final_panel_status["failed_roles"][:8],
            "history": critic_history[-4:],
            "providers": list(dict.fromkeys(item.get("provider") for item in critic_attempts if item.get("provider"))),
            "separate_vendor": False,
        },
        "independent_review": {
            "available": False,
            "disabled": True,
            "reason": "Separate-vendor review is disabled; Codex Luna multi-role jury is the configured panel.",
            "providers": [],
        },
        "approval_state": "awaiting_review",
        "artifacts": [
            "resume.tex", run_pdf_path(run_dir).name, "resume.txt", run_preview_path(run_dir).name if preview else None,
            "job.json", "report.json", "job_context.json", "brief.json", "evidence_catalog.json", "evidence_graph_context.json",
            "job_intelligence.json", "tailoring_audit.json",
            "comparison_control.json",
            "target_opportunity.json",
            winner.get("tailored_candidate_artifact") or None,
            winner.get("tailored_candidate_preview") or None,
            winner.get("base_control_tex") or None,
            "audit_repair.json" if audit_repair_records else None,
            "final_geometry_recovery.json" if final_geometry_recovery.get("attempted") else None,
            "gap_analysis.json" if gap_records else None,
            "candidate_plan.json", "content_plan.json", "layout_packing.json", "critique.json", "revision_log.json",
            "space_expansion.json" if space_expansion_records else None,
            "post_line_density.json" if enhance else None,
            *[("line_edit.json" if index == 1 else "line_edit_%s.json" % index) for index in range(1, len(line_edits) + 1)],
            *[("revision.json" if index == 1 else "revision_%s.json" % index) for index in range(1, len(revision_records) + 1)],
        ],
    }
    report["artifacts"] = [artifact for artifact in report["artifacts"] if artifact]
    make_report(run_dir, report)
    _workshop_state(run_dir, catalog)
    update("awaiting_review", "Draft and Codex Luna critic-panel review are ready for Victor's review", report=report)


def run_strict(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=False)


def run_dream(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=True)


def run_unrestricted(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(run_dir, job, update, enhance=True, unrestricted=True)


def run_generation(run_dir: Path, job: Dict[str, Any], update) -> None:
    run_tailoring(
        run_dir, job, update, enhance=True, unrestricted=True,
        generation=True, quality_profile="unchained",
    )


class ResumeStudioRuntimeStale(RuntimeError):
    """Raised when a launchd process has older code than the checkout."""


class RunManager:
    def __init__(self, root: Optional[Path] = None, max_workers: Any = None):
        self.root = root or repo_root()
        self.workers = configured_run_workers(max_workers)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers)
        self.lock = threading.Lock()
        self._submitted: set = set()
        self._futures: Dict[str, Any] = {}
        self._shutdown = False
        self._last_recovery: Dict[str, Any] = {
            "at": "",
            "recovered": 0,
            "reset_running": 0,
            "failed_invalid": 0,
            "repaired_shutdown_failures": 0,
            "skipped_duplicate": 0,
        }

    def health(self) -> Dict[str, Any]:
        """Return operational state without treating age as a terminal status."""
        with self.lock:
            submitted = list(self._submitted)
        running = 0
        queued = 0
        for run_id in submitted:
            value = read_json(studio_root(self.root) / "runs" / run_id / "status.json", {}) or {}
            if value.get("status") == "running":
                running += 1
            elif value.get("status") == "queued":
                queued += 1
        return {
            "workers": self.workers,
            "submitted": len(submitted),
            "running": running,
            "queued": queued,
            "shutdown": self._shutdown,
            "last_recovery": copy.deepcopy(self._last_recovery),
        }

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting work and leave queued snapshots recoverable."""
        self._shutdown = True
        self._requeue_submitted_for_shutdown()
        try:
            # Cancel queued futures even when waiting for active workers. The
            # active workers have already been made durable as queued above;
            # running them during interpreter shutdown is what caused the
            # nested-pool "cannot schedule new futures" failure.
            self.executor.shutdown(wait=wait, cancel_futures=True)
        except TypeError:  # Python 3.8-compatible fallback for the helper/tests.
            self.executor.shutdown(wait=wait)

    def _requeue_submitted_for_shutdown(self) -> None:
        with self.lock:
            submitted = list(self._submitted)
        stopped_at = now_iso()
        for run_id in submitted:
            run_dir = studio_root(self.root) / "runs" / run_id
            path = run_dir / "status.json"
            value = read_json(path, {}) or {}
            if not isinstance(value, dict) or value.get("status") not in {"queued", "running"}:
                continue
            value.update({
                "status": "queued",
                "step": "queued",
                "message": "Resume Studio is restarting; the run will be recovered automatically",
                "updated_at": stopped_at,
                "shutdown_requeue": True,
                "recovery_reason": "engine_shutdown",
            })
            for key in ("started_at", "finished_at", "elapsed_seconds"):
                value.pop(key, None)
            write_json(path, value)

    def _future_done(self, run_id: str, future: Any) -> None:
        with self.lock:
            self._futures.pop(run_id, None)
            self._submitted.discard(run_id)

    def _submit(self, run_id: str, run_dir: Path, job: Dict[str, Any], mode: str) -> bool:
        with self.lock:
            if self._shutdown or run_id in self._submitted:
                return False
            self._submitted.add(run_id)
        try:
            future = self.executor.submit(self._worker, run_id, run_dir, job, mode)
        except Exception as exc:
            with self.lock:
                self._submitted.discard(run_id)
            self.update(
                run_id, run_dir, "failed", "queue_error",
                "Could not submit the run to the local worker pool: %s" % exc,
                error_code="queue_submit_failed",
            )
            return False
        if future is not None and hasattr(future, "add_done_callback"):
            with self.lock:
                self._futures[run_id] = future
            future.add_done_callback(lambda completed: self._future_done(run_id, completed))
        return True

    def start(
        self, job: Dict[str, Any], mode: str, queue_id: str = "",
        control_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        identity = engine_runtime_identity(self.workers)
        if identity["restart_required"]:
            raise ResumeStudioRuntimeStale(
                "Resume Studio source changed while this service was running; restart the local engine before queueing new work."
            )
        if self._shutdown:
            raise RuntimeError("Resume Studio is shutting down; retry after it restarts.")
        mode = normalize_tailor_mode(mode)
        queue_id = str(queue_id or "").strip()[:80]
        runs_root = studio_root(self.root) / "runs"
        job_id = str((job or {}).get("id") or "")
        with self.lock:
            existing: List[Dict[str, Any]] = []
            if runs_root.is_dir():
                for candidate in runs_root.iterdir():
                    if not candidate.is_dir() or not re.fullmatch(r"[a-f0-9]{12}", candidate.name):
                        continue
                    candidate_status = read_json(candidate / "status.json", {}) or {}
                    if not isinstance(candidate_status, dict):
                        continue
                    same_queue = bool(queue_id and str(candidate_status.get("queue_id") or "") == queue_id)
                    same_active_job = bool(
                        not queue_id
                        and candidate_status.get("status") in {"queued", "running"}
                        and str((candidate_status.get("job") or {}).get("id") or "") == job_id
                        and normalize_tailor_mode(str(candidate_status.get("mode") or "")) == mode
                    )
                    if same_queue or same_active_job:
                        existing.append(candidate_status)
            if existing:
                selected = sorted(
                    existing,
                    key=lambda value: (
                        value.get("status") in {"complete", "completed", "awaiting_review"},
                        value.get("status") in {"running", "queued"},
                        str(value.get("updated_at") or value.get("created_at") or ""),
                        str(value.get("run_id") or ""),
                    ),
                    reverse=True,
                )[0]
                return {**selected, "duplicate": True, "message": selected.get("message") or "Already queued"}

            run_id = uuid.uuid4().hex[:12]
            run_dir = runs_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            pdf_filename = resume_pdf_filename(job, mode)
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
                "engine_runtime": identity,
                "recovery_count": 0,
            }
            if queue_id:
                status["queue_id"] = queue_id
            if isinstance(control_profile, dict):
                status["comparison_control_profile"] = comparison_control_summary(control_profile)
            # Keep the historical posting record attached to the run even if the
            # radar later removes or updates the live job.  This is private ignored
            # state, not a second source of truth for the public radar database.
            job_snapshot = copy.deepcopy(job)
            if isinstance(control_profile, dict):
                job_snapshot["_resume_studio_control_profile"] = copy.deepcopy(control_profile)
            write_json(run_dir / "job.json", job_snapshot)
            write_json(run_dir / "status.json", status)
        worker_job = copy.deepcopy(job_snapshot)
        worker_job["_resume_studio_run_id"] = run_id
        worker_job["_resume_studio_queue_id"] = queue_id
        self._submit(run_id, run_dir, worker_job, mode)
        return status

    def recover_pending(self) -> Dict[str, Any]:
        """Requeue snapshots left queued/running by a prior engine process.

        Run state is deliberately file-backed, while the executor is not.  A
        launchd restart therefore resets abandoned ``running`` snapshots to
        ``queued`` and submits both them and pre-existing queued snapshots to
        the new bounded pool.  Completed, failed, and owner-review runs are
        never replayed.
        """
        summary = {
            "at": now_iso(),
            "recovered": 0,
            "reset_running": 0,
            "failed_invalid": 0,
            "repaired_shutdown_failures": 0,
            "skipped_duplicate": 0,
        }
        identity = engine_runtime_identity(self.workers)
        if identity["restart_required"]:
            summary["error"] = "runtime source is stale"
            self._last_recovery = summary
            return summary
        runs = studio_root(self.root) / "runs"
        if not runs.is_dir():
            self._last_recovery = summary
            return summary
        terminal_queue_ids = set()
        active_queue_groups: Dict[str, List[Tuple[Path, Dict[str, Any]]]] = {}
        for candidate in runs.iterdir():
            if not candidate.is_dir() or not re.fullmatch(r"[a-f0-9]{12}", candidate.name):
                continue
            candidate_status = read_json(candidate / "status.json", {}) or {}
            if (
                isinstance(candidate_status, dict)
                and candidate_status.get("status") in {"complete", "completed", "awaiting_review", "failed"}
                and str(candidate_status.get("queue_id") or "")
            ):
                terminal_queue_ids.add(str(candidate_status.get("queue_id")))
            if (
                isinstance(candidate_status, dict)
                and candidate_status.get("status") in {"queued", "running"}
                and str(candidate_status.get("queue_id") or "")
            ):
                active_queue_groups.setdefault(str(candidate_status.get("queue_id")), []).append((candidate, candidate_status))
        active_queue_winners: Dict[str, str] = {}
        for queue_key, candidates in active_queue_groups.items():
            winner_dir, _ = sorted(
                candidates,
                key=lambda item: (
                    item[1].get("status") == "running",
                    str(item[1].get("updated_at") or item[1].get("created_at") or ""),
                    item[0].name,
                ),
                reverse=True,
            )[0]
            active_queue_winners[queue_key] = winner_dir.name
        for run_dir in sorted(runs.iterdir(), key=lambda item: item.name):
            if not run_dir.is_dir() or not re.fullmatch(r"[a-f0-9]{12}", run_dir.name):
                continue
            run_id = run_dir.name
            status_path = run_dir / "status.json"
            status = read_json(status_path, {}) or {}
            repairable_shutdown_failure = bool(
                isinstance(status, dict)
                and status.get("status") == "failed"
                and "interpreter shutdown" in str(status.get("message") or "").lower()
            )
            if not isinstance(status, dict) or (
                status.get("status") not in {"queued", "running"}
                and not repairable_shutdown_failure
            ):
                continue
            queue_id = str(status.get("queue_id") or "")
            if queue_id and queue_id in terminal_queue_ids:
                self.update(
                    run_id, run_dir, "failed", "superseded",
                    "A terminal run already exists for this application queue item; duplicate recovery was skipped.",
                    error_code="duplicate_application_run",
                )
                summary["skipped_duplicate"] += 1
                continue
            if queue_id and active_queue_winners.get(queue_id) not in {None, run_id}:
                self.update(
                    run_id, run_dir, "failed", "superseded",
                    "A newer active run already owns this application queue item; duplicate recovery was skipped.",
                    error_code="duplicate_application_run",
                )
                summary["skipped_duplicate"] += 1
                continue
            with self.lock:
                if run_id in self._submitted:
                    continue
            job = read_json(run_dir / "job.json", {}) or {}
            if not isinstance(job, dict) or not job:
                self.update(
                    run_id, run_dir, "failed", "recovery_error",
                    "Queued run has no durable job snapshot and cannot be resumed.",
                    error_code="missing_job_snapshot",
                    recovery_attempted_at=summary["at"],
                )
                summary["failed_invalid"] += 1
                continue
            try:
                mode = normalize_tailor_mode(str(status.get("mode") or ""))
            except ValueError as exc:
                self.update(
                    run_id, run_dir, "failed", "recovery_error", str(exc),
                    error_code="invalid_run_mode",
                    recovery_attempted_at=summary["at"],
                )
                summary["failed_invalid"] += 1
                continue
            previous_status = str(status.get("status"))
            recovered = copy.deepcopy(status)
            recovery_count = recovered.get("recovery_count")
            try:
                recovery_count = int(recovery_count or 0) + 1
            except (TypeError, ValueError):
                recovery_count = 1
            recovered.update({
                "status": "queued",
                "step": "queued",
                "message": "Recovered after Resume Studio restart; waiting for a worker",
                "updated_at": summary["at"],
                "recovered_at": summary["at"],
                "recovery_count": recovery_count,
                "recovery_reason": "engine_restart",
                "engine_runtime": identity,
            })
            if previous_status == "running":
                recovered["recovered_from_status"] = "running"
                summary["reset_running"] += 1
            elif repairable_shutdown_failure:
                recovered["recovered_from_status"] = "failed"
                recovered["recovery_reason"] = "interpreter_shutdown_repair"
                summary["repaired_shutdown_failures"] += 1
            for key in ("started_at", "finished_at", "elapsed_seconds"):
                recovered.pop(key, None)
            write_json(status_path, recovered)
            worker_job = copy.deepcopy(job)
            worker_job["_resume_studio_run_id"] = run_id
            worker_job["_resume_studio_queue_id"] = str(recovered.get("queue_id") or "")
            if self._submit(run_id, run_dir, worker_job, mode):
                summary["recovered"] += 1
        self._last_recovery = summary
        return summary

    def update(self, run_id: str, run_dir: Path, status: str, step: str, message: str, **extra) -> None:
        path = run_dir / "status.json"
        value = read_json(path, {}) or {}
        if self._shutdown and status != "queued":
            status = "queued"
            step = "queued"
            message = "Resume Studio is restarting; the run will be recovered automatically"
            extra = dict(extra)
            extra["shutdown_requeue"] = True
            extra["recovery_reason"] = "engine_shutdown"
            extra.pop("finished_at", None)
            extra.pop("elapsed_seconds", None)
        value.update({"run_id": run_id, "status": status, "step": step, "message": message, "updated_at": now_iso()})
        value.update(extra)
        write_json(path, value)
        if status in CURATED_RESUME_STATUSES and run_pdf_path(run_dir).is_file():
            # Keep the Finder-friendly export current without making a failed
            # convenience copy capable of changing the durable run result.
            try:
                export_local_tailored_resumes(self.root)
            except (OSError, ValueError):
                pass

    def _worker(self, run_id: str, run_dir: Path, job: Dict[str, Any], mode: str) -> None:
        started_clock = time.time()
        started_at = now_iso()
        identity = engine_runtime_identity(self.workers)
        if identity["restart_required"]:
            self.update(
                run_id, run_dir, "queued", "stale_runtime",
                "The source checkout changed before this run started; waiting for an engine restart.",
                restart_required=True,
                engine_runtime=identity,
            )
            return
        self.update(
            run_id, run_dir, "running", "starting", "Starting the approved tailoring lanes",
            started_at=started_at, engine_runtime=identity,
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
            elif mode == "unrestricted":
                run_unrestricted(run_dir, job, update)
            else:
                run_generation(run_dir, job, update)
        except Exception as exc:  # keep failure inspectable in the local UI
            trace = traceback.format_exc()
            (run_dir / "error.log").write_text(trace)
            self.update(run_id, run_dir, "failed", "error", str(exc), error_log="error.log")
        finally:
            current_identity = engine_runtime_identity(self.workers)
            if self._shutdown:
                self.update(
                    run_id, run_dir, "queued", "queued",
                    "Resume Studio is restarting; the run will be recovered automatically",
                    shutdown_requeue=True,
                    recovery_reason="engine_shutdown",
                )
            elif current_identity["restart_required"]:
                self.update(
                    run_id, run_dir, "queued", "stale_runtime",
                    "The source checkout changed during this run; waiting for an engine restart before retrying.",
                    restart_required=True,
                    engine_runtime=current_identity,
                )
            else:
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
:root{color-scheme:dark;--bg:#131A21;--panel:#1A232C;--line:#2A343D;--muted:#8FA1AE;--text:#D9E2E8;--accent:#4CAF8A;--good:#4CAF8A;--warn:#D2A24C;--bad:#E07862}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Charter,"Bitstream Charter","Iowan Old Style",Georgia,serif}main{max-width:1250px;margin:0 auto;padding:28px 20px 70px}h1{margin:0 0 6px;font-size:28px}h2{font-size:18px;margin:0 0 12px}.sub{color:var(--muted);margin:0 0 20px}.grid{display:grid;grid-template-columns:410px 1fr;gap:18px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}.jobs{max-height:650px;overflow:auto}.job{width:100%;text-align:left;background:transparent;color:var(--text);border:1px solid transparent;border-radius:8px;padding:10px;margin:4px 0;cursor:pointer}.job:hover,.job.selected{background:#1F2A33;border-color:var(--accent)}.job strong{display:block}.job small{color:var(--muted)}input,select,button{font:14px/1.4 ui-monospace,"SF Mono",Menlo,Consolas,monospace}input,select{background:#131A21;border:1px solid var(--line);border-radius:6px;color:var(--text);padding:9px}input{width:100%;margin-bottom:8px}.toolbar{display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:10px}.toolbar select{min-width:145px}button{background:#2E7D5B;border:1px solid var(--accent);color:#fff;border-radius:6px;padding:9px 12px;cursor:pointer;margin:4px 6px 4px 0}button.secondary{background:#1F2A33;border-color:var(--line)}button:disabled{opacity:.5;cursor:wait}.selected-card{border:1px solid var(--accent);border-radius:8px;padding:13px;margin:10px 0 16px}.meta{color:var(--muted);font-size:13px}.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;margin:3px 4px 0 0;font:12px/1.4 ui-monospace,"SF Mono",Menlo,Consolas,monospace}.match-card{margin:10px 0;padding:10px;border-radius:7px;background:#131A21;border:1px solid var(--line)}.match-card strong{font-size:18px}.status{border-left:3px solid var(--accent);padding:10px 12px;background:#1F2A33;white-space:pre-wrap}.status.complete{border-color:var(--good)}.status.failed{border-color:var(--bad)}.status.running{border-color:var(--warn)}a{color:var(--accent)}pre{white-space:pre-wrap;max-height:360px;overflow:auto;background:#131A21;border:1px solid var(--line);padding:12px;border-radius:6px;font-size:12px}.score{font-size:27px;margin:4px 0}.preview{display:block;width:100%;max-width:760px;margin:14px auto;border:1px solid var(--line);background:#fff}.hidden{display:none}@media(max-width:850px){.grid{grid-template-columns:1fr}.jobs{max-height:360px}}
<style>
.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:22px}.eyebrow{color:var(--accent);font-size:11px;letter-spacing:.12em;font-weight:700}.hero h1{margin-top:4px}.hero-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.hero-actions button{margin:0}.hero-actions button.active{background:var(--accent);border-color:var(--accent);color:#08111d}.radar-link{display:inline-flex;align-items:center;min-height:36px;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:#1F2A33;text-decoration:none;font:13px/1.4 ui-monospace,"SF Mono",Menlo,Consolas,monospace}.grid{grid-template-columns:360px minmax(0,1fr)}.panel{box-shadow:0 12px 35px rgba(0,0,0,.14)}.panel-top,.workspace-heading,.section-title,.library-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.count{color:var(--muted);font-size:12px;padding-top:4px}.hint{color:var(--muted);margin-top:-5px}.notice{background:#10243a;border:1px solid #1f4f7a;border-radius:7px;padding:10px 12px;margin:0 0 14px;color:#c7e5ff}.action-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}.action-card{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px}.action-card h3{font-size:14px;margin:0 0 4px}.action-card p{color:var(--muted);font-size:12px;min-height:36px;margin:0 0 8px}.action-card button{margin:0;width:100%}.section-title{margin-top:20px}.section-title h3{margin:0;font-size:15px}.empty{padding:38px 12px;text-align:center;color:var(--muted)}.library-view{margin-top:18px}.library-toolbar{display:grid;grid-template-columns:minmax(0,1fr) 180px;gap:8px;margin:14px 0}.library-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}.resume-card{background:#0d1117;border:1px solid var(--line);border-radius:9px;padding:12px;min-width:0}.resume-card:hover{border-color:#4b6e91}.card-top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}.card-top strong{display:block}.card-top small{display:block;color:var(--muted);margin-top:3px}.thumb{display:block;width:100%;height:230px;object-fit:contain;object-position:top center;background:#fff;border:1px solid var(--line);margin:10px 0;border-radius:5px}.thumb-placeholder{height:70px;display:flex;align-items:center;justify-content:center;border:1px dashed var(--line);border-radius:5px;margin:10px 0;color:var(--muted)}.card-meta{color:var(--muted);font-size:12px;line-height:1.55}.card-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.card-actions a,.card-actions button{font-size:12px;margin:0;padding:6px 8px}.card-actions button{background:#21262d;border:1px solid var(--line);color:var(--text);border-radius:6px;cursor:pointer}.posting-snapshot{margin-top:10px}.posting-snapshot pre{max-height:230px;margin:6px 0}.legacy{color:var(--warn)}.hidden{display:none!important}.workshop-layout{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);gap:14px}.workshop-lines{max-height:calc(100vh - 220px);overflow:auto;padding-right:3px}.workshop-entry{border-top:1px solid var(--line);padding:12px 0}.workshop-entry:first-child{border-top:0;padding-top:0}.workshop-entry h4{margin:0 0 8px;font-size:14px}.workshop-line{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0}.workshop-line:focus-within{border-color:var(--accent)}.line-meta{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:11px;margin-bottom:6px}.line-text{width:100%;min-height:58px;resize:vertical;line-height:1.4;background:#111827;border:1px solid #263241;border-radius:5px;color:var(--text);padding:8px}.line-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.line-actions button{font-size:12px;margin:0;padding:6px 8px}.source-note{color:var(--muted);font-size:11px;margin:7px 0 0}.workshop-side{position:sticky;top:16px;align-self:start}.workshop-preview{width:100%;max-height:560px;object-fit:contain;object-position:top;background:#fff;border:1px solid var(--line);border-radius:5px}.chat-box textarea{width:100%;min-height:82px;resize:vertical;background:#0d1117;border:1px solid var(--line);border-radius:6px;color:var(--text);padding:9px}.chat-row{display:flex;gap:8px;margin-top:8px}.chat-row select{flex:0 0 120px}.chat-row button{margin:0;flex:1}.ai-reply{background:#10243a;border:1px solid #1f4f7a;border-radius:7px;padding:10px;margin-top:10px;white-space:pre-wrap}.suggestion{border:1px solid var(--line);border-radius:7px;padding:9px;margin:8px 0}.suggestion .text{font-size:13px}.history-list{max-height:180px;overflow:auto}.history-row{display:flex;justify-content:space-between;gap:8px;align-items:center;border-top:1px solid var(--line);padding:7px 0;font-size:12px}.history-row button{font-size:11px;margin:0;padding:4px 7px;background:#21262d;border-color:var(--line)}@media(max-width:850px){.hero{display:block}.hero-actions{margin-top:12px}.action-grid{grid-template-columns:1fr}.library-toolbar{grid-template-columns:1fr}.workshop-layout{grid-template-columns:1fr}.workshop-side{position:static}}
 .usage-strip{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 18px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#111827}.usage-strip strong{color:var(--text)}.queue-strip{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 14px;padding:10px 12px;border:1px solid #3b2e13;border-radius:8px;background:#1b1710}.queue-strip button{margin:0}.mode-tag{color:var(--accent);font-weight:600}.rationale{border-left:3px solid var(--accent);padding:10px 12px;background:#10243a;border-radius:6px;margin:10px 0}.rationale p{margin:5px 0}.rationale ul{margin:6px 0 0 18px;padding:0}.report-details{margin-top:10px}.report-details summary{cursor:pointer;color:var(--accent)}.workshop-preview-frame{width:100%;height:560px;border:1px solid var(--line);border-radius:5px;background:#fff}.preview-fallback{padding:12px;background:#111827;border-radius:6px}.action-card.featured{border-color:var(--accent);box-shadow:0 0 0 1px rgba(88,166,255,.12)}.action-card .micro{min-height:0;margin:4px 0 8px;font-size:11px;color:#b5c7d8}.button-row{display:flex;gap:8px;flex-wrap:wrap}.button-row button{margin:0}.report-meter{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0}.report-meter div{padding:8px;background:#0d1117;border:1px solid var(--line);border-radius:6px}.report-meter strong{display:block;font-size:17px}@media(max-width:850px){.report-meter{grid-template-columns:1fr}}
</style></head><body><main>
<header class="hero"><div><div class="eyebrow">JOB RADAR · PRIVATE RESUME WORKSPACE</div><h1>Resume Studio</h1><p class="sub">Find → save → tailor → apply. Private CV evidence and generated PDFs stay on this Mac.</p></div><div class="hero-actions"><a class="radar-link" href="https://job-radar-newgrad.vercel.app">← Job Radar</a><button id="tailorTab" class="active">New tailoring</button><button id="libraryTab" class="secondary">Resume bank <span id="libraryCount">0</span></button><button id="projectsTab" class="secondary">Projects</button></div></header>
<div id="usageStrip" class="usage-strip"><strong>Usage</strong><span class="meta">Loading observed local Codex usage…</span></div>
<div id="queueStrip" class="queue-strip hidden"><span><strong>Tailor queue</strong> <span id="queueSummary" class="meta"></span></span><button id="queueOpen" class="secondary">Open bank</button></div>
<div id="tailorView" class="grid"><section class="panel"><div class="panel-top"><h2>Postings</h2><span id="jobCount" class="count"></span></div><p class="hint">Choose a role. Saved resumes stay in the bank when you switch.</p><input id="search" placeholder="Search company, title, sector…" autocomplete="off"><div class="toolbar"><select id="sort" aria-label="Sort roles"><option value="best">Best Radar score</option><option value="newest">Newest</option><option value="resume_match">Resume Match</option></select><button id="refreshEvidence" class="secondary" title="Refresh GitHub and Devpost evidence">Refresh evidence</button></div><div id="jobs" class="jobs">Loading roles…</div></section>
<section class="panel"><div id="empty" class="empty">Select a posting to see its match, saved resumes, and tailoring actions.</div><div id="workspace" class="hidden"><div class="workspace-heading"><div id="selected" class="selected-card"></div><button id="selectedLibrary" class="secondary">View resume bank</button></div><div class="notice">Switching postings never deletes a generated resume. Every run is saved with its posting snapshot. Queue several roles; each gets its own durable draft, posting snapshot, and editor history.</div><div id="match" class="match-card"></div><div class="button-row"><button id="analyzeMatch" class="secondary">Analyze full posting match</button><button id="showScoreReasons" class="secondary">Explain Radar score</button></div><div class="action-grid"><div class="action-card"><h3>Used bullets</h3><p>Approved wording and selections only. Your clean comparison baseline.</p><p class="micro">Lowest creative variance · still queues a complete draft</p><button id="strict">Queue used-bullets tailor</button></div><div class="action-card featured"><h3>AI tailor</h3><p>Role-specific rewrites, project swaps, ATS coverage, and a review pass.</p><p class="micro">Evidence-grounded original wording</p><button id="dream">Queue AI tailor</button></div><div class="action-card"><h3>Unrestricted AI tailor</h3><p>Freer synthesis across your CV evidence bank for a sharper, more original argument.</p><p class="micro">Still factual and layout-safe · human-review flag stays visible</p><button id="unrestricted">Queue unrestricted tailor</button></div></div><div id="scoreReasons" class="rationale hidden"></div><div id="status" class="status hidden"></div><div id="report" class="hidden"></div><div class="section-title"><h3>Saved for this posting</h3><button id="allSaved" class="secondary">See all saved resumes</button></div><div id="selectedResumes"></div></div></section></div>
<section id="libraryView" class="panel library-view hidden"><div class="library-head"><div><h2>Resume bank</h2><p class="sub">Every generated run and legacy experiment, paired with the posting it used. Nothing is replaced when you queue another tailor.</p></div><button id="backToTailor" class="secondary">Back to tailoring</button></div><div class="library-toolbar"><input id="librarySearch" placeholder="Filter saved resumes by company or role…" autocomplete="off"><select id="libraryMode" aria-label="Filter resume mode"><option value="all">All modes</option><option value="unrestricted">Unrestricted AI</option><option value="ai">AI tailor</option><option value="used">Used bullets</option></select></div><div id="libraryCards" class="library-grid"></div></section>
<section id="workshopView" class="panel library-view hidden"><div class="library-head"><div><div class="eyebrow">DRAFT WORKSHOP</div><h2 id="workshopTitle">Resume workshop</h2><p id="workshopSubtitle" class="sub">Edit one line at a time. Every save creates a new PDF revision.</p></div><div><button id="workshopBack" class="secondary">Back to bank</button><button id="workshopTailor" class="secondary">Back to posting</button></div></div><div class="notice">The original generated PDF stays untouched. Header, education, and technical skills remain the canonical base; experience, projects, and leadership lines are editable here.</div><div class="workshop-layout"><section class="panel"><div class="section-title"><h3>Editable resume lines</h3><span id="workshopLineCount" class="count"></span></div><div id="workshopLines" class="workshop-lines">Loading workshop…</div></section><aside class="workshop-side"><section class="panel"><div class="section-title"><h3>Preview</h3><span id="workshopSaveStatus" class="meta"></span></div><div id="workshopPreview"></div></section><section class="panel chat-box"><h3>Ask the writing partner</h3><p class="hint">Give it a goal or ask about a specific line. It returns candidates; you choose what to apply.</p><textarea id="workshopRequest" placeholder="e.g. Make the J&J bullets feel more like an AI platform I architected, keep the technical proof, and cut generic wording."></textarea><div class="chat-row"><select id="workshopProvider" aria-label="AI provider"></select><button id="workshopAsk">Ask for candidates</button></div><div id="workshopAiResult"></div></section><section class="panel"><div class="section-title"><h3>Revision history</h3><span class="meta">revert creates a new revision</span></div><div id="workshopHistory" class="history-list"></div></section></aside></div></section>
<script>
let selected=null,activeRunId=null,libraryEntries=[],runTimers=new Map(),jobsCache=[],workshopState=null,workshopSuggestions=[],studioUsage=null,bridgeAutoOpened=false;
const $=id=>document.getElementById(id);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function readBridgedJob(){
  try{
    const token=new URLSearchParams(location.hash.slice(1)).get('job');
    if(!token)return null;
    const normalized=token.replace(/-/g,'+').replace(/_/g,'/');
    const padded=normalized+'='.repeat((4-normalized.length%4)%4);
    const bytes=Uint8Array.from(atob(padded),character=>character.charCodeAt(0));
    const job=JSON.parse(new TextDecoder().decode(bytes));
    if(!job||!job.id||!job.company||!job.title||!/^https?:\/\//i.test(job.url||''))return null;
    job._bridged=true;
    return job;
  }catch(error){return null;}
}
let bridgedSelection=readBridgedJob();
window.addEventListener('hashchange',()=>{bridgedSelection=readBridgedJob();bridgeAutoOpened=false;loadJobs();});
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
  try{const r=await fetch('/api/jobs?query='+q+'&sort='+sort);const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not load postings');jobsCache=data.jobs||[];if(bridgedSelection&&!jobsCache.some(job=>String(job.id)===String(bridgedSelection.id)))jobsCache=[bridgedSelection,...jobsCache];renderJobRows(jobsCache);if(bridgedSelection&&!bridgeAutoOpened){bridgeAutoOpened=true;await choose(bridgedSelection.id);}}catch(error){$('jobs').innerHTML='<p class="sub">'+esc(error.message)+'</p>';}
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
  const providers=workshopState.providers||{};const choices=Object.keys(providers).filter(name=>providers[name]);$('workshopProvider').innerHTML=choices.length?choices.map(name=>`<option value="${esc(name)}">${esc(name==='codex'?'Codex Luna':name)}</option>`).join(''):'<option value="">No local AI CLI</option>';$('workshopAsk').disabled=!choices.length;
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
  let data=bridgedSelection&&String(bridgedSelection.id)===String(id)?bridgedSelection:null;if(!data){const r=await fetch('/api/job?id='+encodeURIComponent(id));data=await r.json();if(!r.ok){$('match').textContent=data.error||'Posting could not be loaded';return;}}selected=data;$('empty').classList.add('hidden');$('workspace').classList.remove('hidden');$('selected').innerHTML=`<strong>${esc(selected.company)} · ${esc(selected.title)}</strong><div class="meta">${esc((selected.locations||[]).join(', '))} · Radar ${selected.score} · <a href="${esc(selected.url)}" target="_blank" rel="noreferrer">open live posting</a></div><div>${(selected.alert_ok?'<span class="badge">alert eligible</span>':'<span class="badge">dashboard role</span>')} ${(selected.early_career_possible?'<span class="badge">early-career possible</span>':'')} ${(selected._bridged?'<span class="badge">opened from production</span>':'')}</div>`;renderMatch(selected.resume_match);document.querySelectorAll('.job').forEach(b=>b.classList.toggle('selected',b.dataset.id===id));renderSelectedResumes();showView('tailor');}
async function analyzeMatch(){if(!selected)return;const button=$('analyzeMatch');button.disabled=true;$('match').innerHTML='<span class="meta">Fetching the posting and matching the full evidence graph…</span>';try{const r=await fetch('/api/match',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:selected.id,job_snapshot:selected._bridged?selected:null})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Match analysis failed');selected.resume_match=data.resume_match;renderMatch(data.resume_match);}catch(error){$('match').textContent=error.message;}finally{button.disabled=false;}}
async function refreshEvidence(){const button=$('refreshEvidence');button.disabled=true;button.textContent='Refreshing…';try{const r=await fetch('/api/evidence/refresh',{method:'POST',headers:{'content-type':'application/json'},body:'{}'});const data=await r.json();if(!r.ok)throw new Error(data.error||'Evidence refresh failed');await loadJobs();if(selected)await choose(selected.id);}catch(error){alert(error.message);}finally{button.disabled=false;button.textContent='Refresh evidence';}}
function setTailorButtons(disabled){['strict','dream','unrestricted'].forEach(id=>$(id).disabled=disabled);}
async function start(mode){if(!selected)return;const buttons=['strict','dream','unrestricted'];buttons.forEach(id=>$(id).disabled=true);$('status').className='status running';$('status').textContent='Queueing '+modeLabel(mode)+' for '+selected.company+'…';$('status').classList.remove('hidden');$('report').classList.add('hidden');try{const r=await fetch('/api/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:selected.id,mode,job_snapshot:selected._bridged?selected:null})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Could not queue run');activeRunId=data.run_id;$('status').textContent=selected.company+': queued · run '+data.run_id;await loadLibrary();watchRun(data.run_id);}catch(error){$('status').className='status failed';$('status').textContent=error.message;}finally{buttons.forEach(id=>$(id).disabled=false);}}
function watchRun(id){if(runTimers.has(id))return;const tick=async()=>{try{const r=await fetch('/api/run?id='+encodeURIComponent(id));const data=await r.json();if(!r.ok)throw new Error(data.error||'Run status unavailable');if(id===activeRunId){$('status').textContent=(data.job?.company?data.job.company+': ':'')+(data.message||data.status);$('status').className='status '+data.status;}if(data.status==='complete'||data.status==='failed'){runTimers.delete(id);if(id===activeRunId){setTailorButtons(false);if(data.status==='complete')renderReport(data);else $('report').classList.add('hidden');}await loadLibrary();return;}const timer=setTimeout(()=>{runTimers.delete(id);tick();},1500);runTimers.set(id,timer);}catch(error){runTimers.delete(id);if(id===activeRunId){$('status').className='status failed';$('status').textContent=error.message;setTailorButtons(false);}}};tick();}
function renderReport(status){const report=status.report||{};const review=report.review||{},gates=review.gates||{},job=status.job||report.job||{},pdfName=status.pdf_filename||report.pdf_filename||'resume.pdf',previewName=status.preview_filename||report.preview_filename||'';$('report').classList.remove('hidden');let html=`<div class="section-title"><h3>Saved result</h3><span class="badge">${modeLabel(status.mode||report.mode)}</span></div><p class="meta">${esc(job.company||'')} · ${esc(job.title||'')} · ${fmtDate(status.updated_at||status.created_at)}</p>`;if(review.craft_score!==undefined)html+=`<div class="score">${review.craft_score}/100 craft</div><div>${review.ready?'Ready for human review':'Needs revision or fact verification'}</div><p>${Object.entries(gates).map(([name,gate])=>`<span class="badge">${esc(name)}: ${esc(gate.status)}</span>`).join(' ')}</p>`;if(report.resume_match)html+=`<p><strong>Resume Match:</strong> ${report.resume_match.score}/100 <span class="badge">${esc(report.resume_match.confidence)}</span></p>`;if(report.positioning_thesis)html+=`<p><strong>Thesis:</strong> ${esc(report.positioning_thesis)}</p>`;if(status.mode==='ai'||status.mode==='unrestricted'||report.mode==='enhanced'||report.mode==='unrestricted')html+=`<p class="meta">AI tailoring may synthesize authorized source lines; unrestricted drafts are intentionally more original. Edit, compare, or revert in the workshop.</p>`;if(report.format_contract)html+=`<p class="meta"><strong>Format:</strong> CV/resume.tex locked · 0% font-size change · company first</p>`;const layout=review.deterministic?.layout||{};if(layout.horizontal)html+=`<p class="meta"><strong>Space QA:</strong> ${layout.horizontal.measured||0} bullets measured · ${layout.horizontal.wrap_count||0} wraps · ${layout.horizontal.near_wrap_count||0} near-wraps · ${layout.horizontal.underfilled_line_count||0} roomy lines · one-more-bullet ${layout.vertical_capacity?.pass?'overflows':'still fits'}</p>`;if(report.usage)html+=`<p class="meta"><strong>Codex usage:</strong> ${Number(report.usage.codex_tokens||0).toLocaleString()} tokens across ${report.usage.codex_calls||0} calls${report.usage.complete?'':' (some call totals unavailable)'}</p>`;html+=`<p><a href="${runArtifact(status.run_id,pdfName)}" target="_blank" rel="noreferrer">Preview PDF</a> · <button class="secondary" data-open-workshop="${esc(status.run_id)}">Open workshop</button> · <a href="/api/posting?source=run&id=${encodeURIComponent(status.run_id)}" target="_blank" rel="noreferrer">Posting snapshot</a> · <a href="${runArtifact(status.run_id,'content_plan.json')}" target="_blank" rel="noreferrer">Source plan</a> · <a href="${runArtifact(status.run_id,'report.json')}" target="_blank" rel="noreferrer">Full report</a></p>`;if(previewName)html+=`<img class="preview" src="${runArtifact(status.run_id,previewName)}" alt="Rendered resume preview">`;html+='<pre>'+esc(JSON.stringify(review,null,2))+'</pre>';$('report').innerHTML=html;document.querySelectorAll('[data-open-workshop]').forEach(button=>button.onclick=()=>openWorkshop(button.dataset.openWorkshop||status.run_id));}
function showView(view){const bank=view==='library',workshop=view==='workshop';$('tailorView').classList.toggle('hidden',bank||workshop);$('libraryView').classList.toggle('hidden',!bank);$('workshopView').classList.toggle('hidden',!workshop);$('tailorTab').classList.toggle('active',!bank&&!workshop);$('libraryTab').classList.toggle('active',bank);if(bank)renderLibrary();if(workshop)renderWorkshop();}
document.addEventListener('click',async event=>{const evidence=event.target.closest('[data-evidence-status]');if(evidence){event.preventDefault();return reviewEvidence(decodeURIComponent(evidence.dataset.evidenceId||''),evidence.dataset.evidenceStatus||'');}const open=event.target.closest('[data-open-workshop]');if(open){event.preventDefault();return openWorkshop(open.dataset.openWorkshop||open.dataset.run);}const button=event.target.closest('[data-view-posting]');if(!button)return;const card=button.closest('.resume-card'),panel=card.querySelector('.posting-snapshot');if(!panel.classList.contains('hidden')){panel.classList.add('hidden');button.textContent='View posting snapshot';return;}button.disabled=true;try{const r=await fetch(button.dataset.posting),data=await r.json();if(!r.ok)throw new Error(data.error||'Posting snapshot unavailable');panel.innerHTML=`<strong>Saved posting snapshot</strong><pre>${esc(data.posting_text||'Only posting metadata was available for this run.')}</pre>`;panel.classList.remove('hidden');button.textContent='Hide posting snapshot';}catch(error){panel.textContent=error.message;panel.classList.remove('hidden');}finally{button.disabled=false;}});
function explainRadarReason(reason){const text=String(reason||'');if(text.startsWith('raw utility'))return 'Calibration: '+text;const labels=[['base utility','Baseline role utility'],['role:','Role family fit'],['sector:','Sector fit'],['new-grad/early-career priority','Verified early-career signal'],['early-career possible','Plausible first-role signal'],['new-grad evidence absent','No explicit early-career evidence'],['company tier','Company quality'],['explicit goal company','Personal goal-company preference'],['company concentration','Company diversity adjustment'],['compensation','Compensation'],['posted','Freshness'],['remote','Remote access'],['Resume Match','Resume Match']];const label=(labels.find(item=>text.startsWith(item[0]))||[])[1]||'Scoring input';return label+': '+text;}
 $('search').oninput=()=>{clearTimeout(window.searchTimer);window.searchTimer=setTimeout(loadJobs,250)};$('sort').onchange=loadJobs;$('librarySearch').oninput=renderLibrary;$('libraryMode').onchange=renderLibrary;$('analyzeMatch').onclick=analyzeMatch;$('refreshEvidence').onclick=refreshEvidence;$('strict').onclick=()=>start('used');$('dream').onclick=()=>start('ai');$('unrestricted').onclick=()=>start('unrestricted');$('showScoreReasons').onclick=()=>{if(!selected)return;const reasons=selected.score_reasons||[];$('scoreReasons').classList.remove('hidden');$('scoreReasons').innerHTML='<strong>Why Radar gave this role '+esc(selected.score)+'/100</strong><p>Radar is deterministic job fit. Resume Match is a separate CV/evidence alignment score. 90+ is strong; the company-diversity adjustment only nudges weaker duplicates.</p><ul>'+reasons.map(reason=>'<li>'+esc(explainRadarReason(reason))+'</li>').join('')+'</ul>';};$('queueOpen').onclick=()=>showView('library');$('tailorTab').onclick=()=>showView('tailor');$('libraryTab').onclick=()=>showView('library');$('selectedLibrary').onclick=()=>showView('library');$('allSaved').onclick=()=>showView('library');$('backToTailor').onclick=()=>showView('tailor');$('workshopBack').onclick=()=>showView('library');$('workshopTailor').onclick=()=>showView('tailor');$('workshopAsk').onclick=()=>askWorkshop('');Promise.all([loadJobs(),loadLibrary(),loadUsage(),loadProtection(),loadEvidenceReview()]).then(()=>{const runId=new URLSearchParams((location.hash||'').replace(/^#/,'')).get('run');if(runId)openWorkshop(runId);});
const resumeStudioBridgeMode=new URLSearchParams(location.search).get('bridge')==='1';
const resumeStudioBridgeOrigins=new Set(['https://job-radar-newgrad.vercel.app','https://job-radar-vmj-8946s-projects.vercel.app']);
const requestedBridgeOrigin=(()=>{try{return new URLSearchParams(location.search).get('origin')||'';}catch(_){return '';}})();
const resumeStudioBridgeOrigin=resumeStudioBridgeOrigins.has(requestedBridgeOrigin)?requestedBridgeOrigin:'';
const resumeStudioBridgeNonce=(()=>{const bytes=new Uint8Array(32);crypto.getRandomValues(bytes);return Array.from(bytes,value=>value.toString(16).padStart(2,'0')).join('');})();
function validBridgeEvent(event,message){return Boolean(resumeStudioBridgeMode&&resumeStudioBridgeOrigin&&event.source===window.opener&&event.origin===resumeStudioBridgeOrigin&&message&&message.bridge_nonce===resumeStudioBridgeNonce&&/^cloud-[0-9]{10,}-[0-9]+$/.test(String(message.request_id||'')));}
function bridgeReply(event,requestId,data,error=''){if(!validBridgeEvent(event,{bridge_nonce:resumeStudioBridgeNonce,request_id:requestId}))return;event.source.postMessage({type:'resume-studio:response',request_id:requestId,bridge_nonce:resumeStudioBridgeNonce,ok:!error,data,error},resumeStudioBridgeOrigin);}
async function bridgeFetch(path,init={}){const response=await fetch(path,init);let data={};try{data=await response.json();}catch(_){data={};}if(!response.ok)throw new Error(data.error||`engine returned ${response.status}`);return data;}
function projectBridgeInit(payload,init={}){return {...init,headers:{...(init.headers||{}),'X-Resume-Project-Capability':String(payload?.capability||'')}};}
async function handleResumeStudioBridge(event){const message=event.data||{};if(message.type!=='resume-studio:request'||!validBridgeEvent(event,message))return;const action=message.action,payload=message.payload&&typeof message.payload==='object'&&!Array.isArray(message.payload)?message.payload:{};try{let data;if(action==='health')data=await bridgeFetch('/api/health');else if(action==='project_capability')data=await bridgeFetch('/api/project/capability');else if(action==='projects')data=await bridgeFetch('/api/projects',projectBridgeInit(payload));else if(action==='project_read')data=await bridgeFetch('/api/project?id='+encodeURIComponent(payload.project_id||'')+'&path='+encodeURIComponent(payload.path||''),projectBridgeInit(payload));else if(action==='project_history')data=await bridgeFetch('/api/project/history?id='+encodeURIComponent(payload.project_id||''),projectBridgeInit(payload));else if(action==='project_build')data=await bridgeFetch('/api/project/build?id='+encodeURIComponent(payload.project_id||'')+'&build_id='+encodeURIComponent(payload.build_id||''),projectBridgeInit(payload));else if(action==='project_artifact'){const response=await fetch('/api/project/artifact?id='+encodeURIComponent(payload.project_id||'')+'&path='+encodeURIComponent(payload.path||''),projectBridgeInit(payload));if(!response.ok)throw new Error('artifact unavailable');const blob=await response.blob();const reader=new FileReader();data=await new Promise((resolve,reject)=>{reader.onload=()=>resolve({data_url:reader.result});reader.onerror=reject;reader.readAsDataURL(blob);});}else if(action==='project_mutate')data=await bridgeFetch('/api/project/file',{method:'POST',headers:{'content-type':'application/json','X-Resume-Project-Capability':String(payload.capability||'')},body:JSON.stringify(payload)});else if(action==='project_action')data=await bridgeFetch('/api/project',{method:'POST',headers:{'content-type':'application/json','X-Resume-Project-Capability':String(payload.capability||'')},body:JSON.stringify(payload)});else if(action==='project_compile')data=await bridgeFetch('/api/project/compile',{method:'POST',headers:{'content-type':'application/json','X-Resume-Project-Capability':String(payload.capability||'')},body:JSON.stringify(payload)});else if(action==='project_restore')data=await bridgeFetch('/api/project/revert',{method:'POST',headers:{'content-type':'application/json','X-Resume-Project-Capability':String(payload.capability||'')},body:JSON.stringify(payload)});else if(action==='library')data=await bridgeFetch('/api/library?limit='+encodeURIComponent(payload.limit||100));else if(action==='match')data=await bridgeFetch('/api/match',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:payload.job_id,job_snapshot:payload.job_snapshot})});else if(action==='context')data=await bridgeFetch('/api/context');else if(action==='context_job')data=await bridgeFetch('/api/context/job',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:payload.job_id,job_snapshot:payload.job_snapshot})});else if(action==='context_answer')data=await bridgeFetch('/api/context/answer',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='context_hint')data=await bridgeFetch('/api/context/hint',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='context_hint_dismiss')data=await bridgeFetch('/api/context/hint/dismiss',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='queue')data=await bridgeFetch('/api/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({job_id:payload.job_id,mode:payload.mode,queue_id:payload.queue_id,job_snapshot:payload.job_snapshot})});else if(action==='status')data=await bridgeFetch('/api/run?id='+encodeURIComponent(payload.id||''));else throw new Error('unsupported bridge request');bridgeReply(event,message.request_id,data);if(action==='queue'&&data.run_id)bridgePollRun(event,data.run_id);}catch(error){bridgeReply(event,message.request_id,{},error.message||String(error));}}
async function bridgePollRun(event,runId){for(let i=0;i<1200;i+=1){await new Promise(resolve=>setTimeout(resolve,1500));try{const data=await bridgeFetch('/api/run?id='+encodeURIComponent(runId));if(event.source===window.opener&&event.origin===resumeStudioBridgeOrigin)event.source.postMessage({type:'resume-studio:run',run_id:runId,bridge_nonce:resumeStudioBridgeNonce,data},resumeStudioBridgeOrigin);if(['complete','awaiting_review','failed'].includes(data.status))return;}catch(error){if(event.source===window.opener)event.source.postMessage({type:'resume-studio:run',run_id:runId,bridge_nonce:resumeStudioBridgeNonce,data:{status:'failed',message:error.message}},resumeStudioBridgeOrigin);return;}}}
window.addEventListener('message',handleResumeStudioBridge);
if(resumeStudioBridgeMode){document.title='Resume Studio engine';document.body.innerHTML='<main style="max-width:420px;margin:0 auto;padding:28px;font:15px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#131A21;color:#D9E2E8;min-height:100vh"><h1 style="font-size:20px">Resume Studio engine</h1><p id="bridgeState">Connecting to the cloud workspace…</p><p style="color:#8FA1AE;font-size:13px">Keep this small private-engine window open while the cloud Resume Studio queues or reviews a draft. Your CV and generated files remain on this Mac.</p></main>';if(window.opener&&resumeStudioBridgeOrigin)window.opener.postMessage({type:'resume-studio:ready',bridge_nonce:resumeStudioBridgeNonce},resumeStudioBridgeOrigin);}
</script></main></body></html>"""


UI_HTML = UI_HTML.replace(
    '<div id="usageStrip" class="usage-strip"><strong>Usage</strong><span class="meta">Loading observed local Codex usage…</span></div>',
    '<details class="utility-details" open><summary>Safety and usage</summary><div id="protectionStrip" class="protection-strip"><strong>Canonical resumes locked</strong><span>Studio creates private copies only; protected CV/immutable/ artifacts are never overwritten.</span><span class="meta">Owner edits require: .venv/bin/python scripts/resume_lock.py unlock</span></div><div id="usageStrip" class="usage-strip"><strong>Usage</strong><span class="meta">Loading observed local Codex usage…</span></div></details>',
)
UI_HTML = UI_HTML.replace("CV/resume.tex locked", "CV/immutable/VictorJimenezResume.tex locked")

# Keep the localhost engine visually aligned with the owner-only production
# Resume Studio surface. The local page still owns the engine and data, but
# uses the same card, chip, and action language as the cloud control plane so
# switching between the two surfaces does not feel like changing products.
UI_HTML = UI_HTML.replace(
    '</style></head><body><main>',
    '''<style>
.testing-hero.hero{display:flex;flex-wrap:wrap;align-items:flex-start;gap:12px;margin-bottom:18px;padding:15px 16px;background:var(--panel);border:1px solid var(--accent);border-radius:10px;box-shadow:0 12px 35px rgba(0,0,0,.14)}
.testing-hero.hero .studio-topline{display:flex;flex:1 1 100%;align-items:center;justify-content:space-between;gap:10px;min-width:0;padding-bottom:2px}
.testing-hero.hero .studio-topline>div{display:flex;flex-wrap:wrap;gap:6px;min-width:0}
.testing-hero.hero .studio-hero-copy{min-width:0;flex:1 1 520px}
.testing-hero.hero .studio-hero-copy .sub{margin-bottom:0}
.testing-hero.hero .hero-actions{flex:0 0 auto;max-width:100%}
.chip{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;color:var(--muted);font:12px/1.4 ui-monospace,"SF Mono",Menlo,Consolas,monospace;white-space:nowrap}
.chip.good{color:var(--good);border-color:var(--good)}
.chip.bad{color:var(--bad);border-color:var(--bad)}
.chip.unknown{border-style:dashed}
.studio-workflow-note{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin:0 0 14px;padding:10px 12px;background:#10243a;border:1px solid #1f4f7a;border-radius:7px;color:#c7e5ff}
.studio-workflow-note .meta{color:#9fc4df}
@media(max-width:850px){.testing-hero.hero .studio-topline{align-items:flex-start;flex-direction:column}.testing-hero.hero .hero-actions{width:100%}.studio-workflow-note{align-items:flex-start;flex-direction:column}}
</style></head><body><main>''',
    1,
)
UI_HTML = UI_HTML.replace(
    '<header class="hero"><div><div class="eyebrow">JOB RADAR · PRIVATE RESUME WORKSPACE</div>',
    '<header class="hero testing-hero"><div class="studio-topline"><div><span class="chip good">owner only · @VictorJimenez3</span><span class="chip">source stays on the Mac</span></div><span id="engineChip" class="chip unknown">checking Mac engine…</span></div><div class="studio-hero-copy"><div class="eyebrow">JOB RADAR · PRIVATE RESUME WORKSPACE</div>',
    1,
)
UI_HTML = UI_HTML.replace(
    '<div id="usageStrip" class="usage-strip"><strong>Usage</strong>',
    '<div class="studio-workflow-note"><strong>Private engine workflow</strong><span class="meta">Choose a posting, queue a draft, inspect the diff in Workshop, then keep the resulting PDF in the private bank.</span></div><div id="usageStrip" class="usage-strip"><strong>Usage</strong>',
    1,
)
UI_HTML = UI_HTML.replace(
    "async function loadUsage(){try{const r=await fetch('/api/usage');",
    "async function loadEngineStatus(){const chip=$('engineChip');if(!chip)return;try{const r=await fetch('/api/health');const data=await r.json();if(!r.ok)throw new Error(data.error||'health check failed');const ready=Boolean(data.ready)&&!data.restart_required;chip.className='chip '+(ready?'good':'bad');chip.textContent=ready?'Mac engine ready':'restart required';}catch(error){chip.className='chip bad';chip.textContent='Mac engine offline';}}\nasync function loadUsage(){try{const r=await fetch('/api/usage');",
    1,
)
UI_HTML = UI_HTML.replace(
    'Promise.all([loadJobs(),loadLibrary(),loadUsage(),loadProtection(),loadEvidenceReview()])',
    'Promise.all([loadJobs(),loadLibrary(),loadEngineStatus(),loadUsage(),loadProtection(),loadEvidenceReview()])',
    1,
)
UI_HTML = UI_HTML.replace("||'resume.pdf',previewName", "||'victor_jimenez_company.pdf',previewName")
UI_HTML = UI_HTML.replace("||report.pdf_filename||'resume.pdf'", "||report.pdf_filename||'victor_jimenez_company.pdf'")
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
    "function renderUsage(usage){if(!usage)return;$('usageStrip').innerHTML=`<strong>Usage</strong><span><strong>${Number(usage.codex_tokens||0).toLocaleString()}</strong> Codex Luna observed tokens · ${usage.runs||0} saved runs</span><span class=\"meta\">Codex model: gpt-5.6-luna · first-party subscription CLI only; no local-model or API fallback</span>`;}",
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
    '<div class="action-grid"><div class="action-card featured"><h3>1. Take-the-wheel</h3><p>Primary mode: choose the strongest portfolio, surface deeper evidence, and rewrite when the hiring-value gain is real.</p><p class="micro">Creative and adaptive · still evidence-grounded, chronological, and layout-safe</p><button id="unrestricted">Create take-the-wheel draft</button></div><div class="action-card"><h3>2. AI tailor</h3><p>Role-specific project selection, ATS terminology, rewrites, and a Codex Luna critic-panel pass.</p><p class="micro">Adaptive with a more conservative change threshold</p><button id="dream">Create AI-tailored draft</button></div><div class="action-card"><h3>3. Used bullets</h3><p>Approved wording and selections only. The clean comparison baseline.</p><p class="micro">Lowest creative variance</p><button id="strict">Create used-bullets draft</button></div></div><details class="mode-guide"><summary>How the modes differ</summary><div class="mode-guide-copy"><p><strong>Take-the-wheel:</strong> may substantially restructure the portfolio when stronger verified evidence supports it.</p><p><strong>AI tailor:</strong> makes role-specific changes, but clears a higher bar for replacing already-strong evidence.</p><p><strong>Used bullets:</strong> selects approved source lines with minimal creative variance.</p><p class="meta">All three modes use the same evidence graph, factual checks, one-page contract, chronological experience order, and owner review.</p></div></details>',
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
    "</head>",
    "<style>.warning{color:var(--warn)}.status.stale{border-color:var(--warn)}</style></head>",
)
UI_HTML = UI_HTML.replace(
    "queue_id:payload.queue_id,job_snapshot:payload.job_snapshot",
    "queue_id:payload.queue_id,control_profile:payload.control_profile||null,job_snapshot:payload.job_snapshot",
)
UI_HTML = UI_HTML.replace(
    "function modeLabel(mode){return ({used:'Used bullets',strict:'Used bullets','source-only':'Used bullets',ai:'AI tailor',dream:'AI tailor',enhanced:'AI tailor',unrestricted:'Take-the-wheel'})[mode]||'Tailor';}",
    "function modeLabel(mode){return ({used:'Used bullets',strict:'Used bullets','source-only':'Used bullets',ai:'AI tailor',dream:'AI tailor',enhanced:'AI tailor',unrestricted:'Take-the-wheel',generation:'Unchained generation'})[mode]||'Tailor';}",
)
UI_HTML = UI_HTML.replace(
    "function renderQueue(){const active=libraryEntries.filter(entry=>entry.status==='queued'||entry.status==='running');const queued=active.filter(entry=>entry.status==='queued').length,running=active.filter(entry=>entry.status==='running').length;const strip=$('queueStrip');if(!active.length){strip.classList.add('hidden');return;}strip.classList.remove('hidden');$('queueSummary').textContent=`${queued} queued · ${running} running · ${active.length} total`;}",
    "function renderQueue(){const active=libraryEntries.filter(entry=>entry.status==='queued'||entry.status==='running');const queued=active.filter(entry=>entry.status==='queued').length,running=active.filter(entry=>entry.status==='running').length,stale=active.filter(entry=>entry.stale).length;const strip=$('queueStrip');if(!active.length){strip.classList.add('hidden');return;}strip.classList.remove('hidden');$('queueSummary').textContent=`${queued} queued · ${running} running · ${active.length} total${stale?` · ${stale} need attention`:''}`;}",
)
UI_HTML = UI_HTML.replace(
    "const workshop=entry.has_workshop?`<button data-open-workshop data-run=\"${esc(entry.run_id)}\">Open workshop</button>`:'';const warning=entry.legacy?'<span class=\"legacy\">legacy experiment</span>':'';return `<article class=\"resume-card\">",
    "const workshop=entry.has_workshop?`<button data-open-workshop data-run=\"${esc(entry.run_id)}\">Open workshop</button>`:'';const warning=entry.legacy?'<span class=\"legacy\">legacy experiment</span>':'';const stale=entry.stale?`<div class=\"warning\">${esc(entry.stale_reason||'This run needs engine attention; it remains recoverable.')}</div>`:'';return `<article class=\"resume-card\">",
)
UI_HTML = UI_HTML.replace(
    "${job.resume_match?` · Match ${esc(job.resume_match.score)}/100`:''}</div><div class=\"card-actions\">",
    "${job.resume_match?` · Match ${esc(job.resume_match.score)}/100`:''}</div>${stale}<div class=\"card-actions\">",
)
UI_HTML = UI_HTML.replace(
    "function showView(view){",
    """const baseRenderReport=renderReport;
renderReport=function(status){
  baseRenderReport(status);
  const report=status.report||{},audit=report.tailoring_audit||{},changes=report.content_changes||{},swaps=changes.project_swaps||{},ats=changes.keyword_coverage||{};
  if(audit.version){
    const fit=typeof audit.fit==='object'?audit.fit.band:audit.fit||'unknown',readiness=audit.readiness||'review',tailoring=audit.tailoring||'inconclusive';
    const badge=value=>`<span class="badge">${esc(value)}</span>`;
    const recommendation=audit.recommended_version||audit.comparison?.recommended_version||'review',decision=audit.decision||audit.comparison?.decision||'needs_review';
    let auditPanel=`<div class="match-card"><strong>Tailoring decision</strong><p>${badge(`Fit: ${fit}`)} ${badge(`Tailoring: ${tailoring}`)} ${badge(`Readiness: ${readiness}`)} ${badge(`Recommendation: ${recommendation}`)} ${badge(`${audit.confidence||'low'} confidence`)}</p><div class="meta">Compared with the original resume for this posting; not a hiring prediction. <strong>${esc(decision.replaceAll('_',' '))}</strong></div>`;
    const findings=audit.findings||[];
    const blockers=findings.filter(item=>item.classification==='BLOCKER').slice(0,5),gains=findings.filter(item=>item.classification==='KEEP_GOOD').slice(0,4),losses=findings.filter(item=>item.classification==='REGRESSION'||item.classification==='MISSED_OPPORTUNITY').slice(0,6);
    if(gains.length)auditPanel+=`<p><strong>Supported gains</strong></p><ul>${gains.map(item=>`<li>${esc(item.reason||'')}</li>`).join('')}</ul>`;
    if(losses.length)auditPanel+=`<p><strong>Losses or missed opportunities</strong></p><ul>${losses.map(item=>`<li>${esc(item.reason||'')}</li>`).join('')}</ul>`;
    if(blockers.length)auditPanel+=`<p><strong>Blockers</strong></p><ul>${blockers.map(item=>`<li>${esc(item.reason||'')}</li>`).join('')}</ul>`;
    auditPanel+='</div>';
    $('report').insertAdjacentHTML('afterbegin',auditPanel);
  }
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

# Generation is deliberately a fourth, separate mode. The existing
# take-the-wheel path remains selectable and behaviorally unchanged as the
# strong moderate baseline.
UI_HTML = UI_HTML.replace(
    '<option value="all">All modes</option>',
    '<option value="all">All modes</option><option value="generation">Unchained generation</option>',
)
UI_HTML = UI_HTML.replace(
    "unrestricted:'Take-the-wheel'",
    "unrestricted:'Take-the-wheel (moderate)',generation:'Unchained generation'",
)
# Keep the local controls named exactly like the cloud workspace. The
# underlying mode ids remain unchanged so existing runs and API clients keep
# working.
UI_HTML = UI_HTML.replace("Unrestricted AI tailor", "Take-the-wheel (moderate)")
UI_HTML = UI_HTML.replace("Queue unrestricted tailor", "Queue Take-the-wheel")
UI_HTML = UI_HTML.replace(
    '<div class="action-grid"><div class="action-card featured"><h3>1. Take-the-wheel</h3>',
    '<div class="action-grid"><div class="action-card featured"><h3>1. Unchained generation</h3><p>Maps every posting requirement to the full evidence graph, then generates new source-grounded bullets or Skills lines to close truthful gaps.</p><p class="micro">Human-style gap filling · unsupported claims stay visible</p><button id="generation">Create unchained draft</button></div><div class="action-card"><h3>2. Take-the-wheel (moderate)</h3>',
)
UI_HTML = UI_HTML.replace(
    '<p><strong>Take-the-wheel:</strong> may substantially restructure the portfolio when stronger verified evidence supports it.</p>',
    '<p><strong>Unchained generation:</strong> audits every requirement, searches Markdown for buried evidence, and may generate new evidence-backed lines.</p><p><strong>Take-the-wheel (moderate):</strong> may substantially restructure the portfolio when stronger verified evidence supports it.</p>',
)
UI_HTML = UI_HTML.replace(
    "['strict','dream','unrestricted']",
    "['strict','dream','unrestricted','generation']",
)
UI_HTML = UI_HTML.replace(
    "$(`unrestricted`).onclick=()=>start('unrestricted')",
    "$(`unrestricted`).onclick=()=>start('unrestricted');$(`generation`).onclick=()=>start('generation')",
)
# The raw string uses ordinary quote syntax for DOM IDs.
UI_HTML = UI_HTML.replace(
    "$('unrestricted').onclick=()=>start('unrestricted');$('showScoreReasons')",
    "$('unrestricted').onclick=()=>start('unrestricted');$('generation').onclick=()=>start('generation');$('showScoreReasons')",
)
UI_HTML = UI_HTML.replace(
    "status.mode==='ai'||status.mode==='unrestricted'",
    "status.mode==='ai'||status.mode==='unrestricted'||status.mode==='generation'",
)
UI_HTML = UI_HTML.replace(
    "report.mode==='enhanced'||report.mode==='unrestricted'",
    "report.mode==='enhanced'||report.mode==='unrestricted'||report.mode==='generation'",
)
UI_HTML = UI_HTML.replace(
    "${ats.covered_count||0}/${ats.supported_count||0} supported exact terms rendered (${ats.exact_coverage_percent||0}%)",
    "${ats.covered_count||0}/${ats.detected_count||ats.supported_count||0} detected exact terms rendered (${ats.exact_coverage_percent||0}% overall; ${ats.supported_exact_coverage_percent||0}% of supported terms)",
)
UI_HTML = UI_HTML.replace(
    'panel+=`<div class="audit-card"><h4>ATS overlay',
    'const strategy=report.generation_strategy||{};if((strategy.requirements||[]).length)panel+=`<div class="audit-card"><h4>Requirement → evidence map</h4><p class="meta">${esc(strategy.portfolio_strategy||\'\')}</p><div class="audit-scroll">${strategy.requirements.map(item=>`<div class="diff-row ${item.evidence_status===\'unsupported\'?\'removed\':item.recommended_action===\'synthesize\'||item.recommended_action===\'tailor_skills\'?\'added\':\'rewritten\'}"><span class="diff-label">${esc(item.importance)} · ${esc(item.evidence_status)} · ${esc(item.recommended_action)}</span><strong>${esc(item.requirement)}</strong>${item.exact_terms?.length?`<br><span class="meta">terms: ${esc(item.exact_terms.join(\', \'))}</span>`:\'\'}${item.reason?`<br>${esc(item.reason)}`:\'\'}</div>`).join(\'\')}</div></div>`;panel+=`<div class="audit-card"><h4>ATS overlay',
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
  const flowHtml=flow.length?flow.map(item=>`<div class="flow-step ${item.status==='failed'?'failed':item.status==='skipped'?'skipped':''}"><span class="flow-dot"></span><strong>${esc(item.label||'provider')}</strong><span>${esc((item.model||item.provider||'unknown')+(item.reasoning_effort?' · '+item.reasoning_effort:''))}<br><small>${esc(item.status||'unknown')}</small></span><span class="flow-meta">${fmtSeconds(item.elapsed_seconds)} · ${item.usage_tokens==null?'tokens n/a':Number(item.usage_tokens).toLocaleString()+' tokens'}</span></div>`).join(''):'<div class="meta">No provider flow was recorded.</div>';
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
  panel+=`<div class="audit-card"><h4>Provider flow, model, and usage</h4><div class="flow">${flowHtml}</div><p class="meta"><strong>This run:</strong> ${Number(usage.codex_tokens||0).toLocaleString()} Codex Luna tokens · ${usage.codex_calls||0} Codex calls</p>${weekly}</div>`;
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

UI_HTML = UI_HTML.replace(
    '  panel+=`<div class="audit-card"><h4>ATS overlay',
    '  const strategy=report.generation_strategy||{};if((strategy.requirements||[]).length)panel+=`<div class="audit-card"><h4>Requirement → evidence map</h4><p class="meta">${esc(strategy.portfolio_strategy||\'\')}</p><div class="audit-scroll">${strategy.requirements.map(item=>`<div class="diff-row ${item.evidence_status===\'unsupported\'?\'removed\':item.recommended_action===\'synthesize\'||item.recommended_action===\'tailor_skills\'?\'added\':\'rewritten\'}"><span class="diff-label">${esc(item.importance)} · ${esc(item.evidence_status)} · ${esc(item.recommended_action)}</span><strong>${esc(item.requirement)}</strong>${item.exact_terms?.length?`<br><span class="meta">terms: ${esc(item.exact_terms.join(\', \'))}</span>`:\'\'}${item.reason?`<br>${esc(item.reason)}`:\'\'}</div>`).join(\'\')}</div></div>`;\n  panel+=`<div class="audit-card"><h4>ATS overlay',
)


# The application agent shares the same loopback bridge as Resume Studio.  The
# browser extension talks to its JSON API directly; the hosted owner UI uses
# the existing postMessage bridge so no public CV or local-service port is
# exposed to the internet.
UI_HTML = UI_HTML.replace(
    "else if(action==='queue')data=await bridgeFetch('/api/run'",
    "else if(action==='application_health')data=await bridgeFetch('/api/application/health');else if(action==='application_context')data=await bridgeFetch('/api/application/context');else if(action==='application_issues')data=await bridgeFetch('/api/application/issues?status='+encodeURIComponent(payload.status||''));else if(action==='application_sessions')data=await bridgeFetch('/api/application/sessions');else if(action==='application_session')data=await bridgeFetch('/api/application/session?session_id='+encodeURIComponent(payload.session_id||''));else if(action==='application_session_create')data=await bridgeFetch('/api/application/session',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_form')data=await bridgeFetch('/api/application/form',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_review')data=await bridgeFetch('/api/application/review',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_answer')data=await bridgeFetch('/api/application/answer',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_mapping')data=await bridgeFetch('/api/application/mapping',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_event')data=await bridgeFetch('/api/application/event',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_confirm')data=await bridgeFetch('/api/application/confirm',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_verify')data=await bridgeFetch('/api/application/verify',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='application_issue')data=await bridgeFetch('/api/application/issue',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});else if(action==='queue')data=await bridgeFetch('/api/run'",
)

# Approval is intentionally routed through the same private loopback bridge as
# queueing.  The hosted UI may request approval, but the local engine remains
# the authority that verifies the review gates and writes the private bank.
UI_HTML = UI_HTML.replace(
    "else if(action==='queue')data=await bridgeFetch('/api/run'",
    "else if(action==='approve')data=await bridgeFetch('/api/run/approve',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({run_id:payload.run_id})});else if(action==='queue')data=await bridgeFetch('/api/run'",
)

# The cloud control plane forwards the bounded public posting snapshot through
# the loopback bridge so a newly crawled role can still be matched or queued
# before the Mac checkout has ingested the same job ID.
UI_HTML = UI_HTML.replace(
    "body:JSON.stringify({job_id:payload.job_id})",
    "body:JSON.stringify({job_id:payload.job_id,job_snapshot:payload.job_snapshot||null})",
)

UI_HTML = UI_HTML.replace(
    '<section id="libraryView" class="panel library-view hidden">',
    '<section id="projectsView" class="panel library-view hidden"><div class="library-head"><div><div class="eyebrow">PRIVATE PROJECTS</div><h2>Overleaf-style workspace</h2><p class="sub">Edit only private projects. Canonical resumes and tailored runs stay read-only; workspace PDFs are drafts until reviewed.</p></div><button id="projectsBack" class="secondary">Back to tailoring</button></div><div id="resumeProjectsWorkspace"></div></section><section id="libraryView" class="panel library-view hidden">',
    1,
)
UI_HTML = UI_HTML.replace(
    '</head>',
    '<script src="/resume_workspace.js"></script></head>',
    1,
)
UI_HTML = UI_HTML.replace(
    '</script></main>',
    r'''let resumeWorkspaceInstance=null;
function showResumeProjects(){
  ['tailorView','libraryView','workshopView'].forEach(id=>$(id)?.classList.add('hidden'));
  $('projectsView')?.classList.remove('hidden');
  $('tailorTab')?.classList.remove('active'); $('libraryTab')?.classList.remove('active'); $('projectsTab')?.classList.add('active');
  const host=$('resumeProjectsWorkspace');
  if(host&&window.ResumeProjectWorkspace&&!resumeWorkspaceInstance)resumeWorkspaceInstance=window.ResumeProjectWorkspace.mount(host,{transport:null});
}
const resumeStudioShowView=showView;
showView=function(view){$('projectsView')?.classList.add('hidden');$('projectsTab')?.classList.remove('active');return resumeStudioShowView(view);};
document.addEventListener('DOMContentLoaded',()=>{$('projectsTab')?.addEventListener('click',showResumeProjects);$('projectsBack')?.addEventListener('click',()=>showView('tailor'));});</script></main>''',
    1,
)

class StudioHandler(BaseHTTPRequestHandler):
    manager: RunManager = RunManager()
    # Project capabilities are intentionally ephemeral and process-local.  A
    # cloud page can operate the private workspace only through the nonce-
    # verified popup bridge; Vercel and Drive never receive this token.
    project_capabilities: Dict[str, float] = {}
    project_capability_lock = threading.Lock()
    PROJECT_CAPABILITY_TTL = 15 * 60
    # The cloud Job Radar page is only a control plane. The engine remains
    # bound to loopback; cloud requests are limited to this exact allowlist.
    DEFAULT_BRIDGE_ORIGINS = {
        "https://job-radar-newgrad.vercel.app",
        "https://job-radar-vmj-8946s-projects.vercel.app",
        "http://localhost:4317",
        "http://127.0.0.1:4317",
    }

    @classmethod
    def bridge_origins(cls) -> set:
        configured = {
            item.strip().rstrip("/")
            for item in os.environ.get("RESUME_STUDIO_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        }
        return configured or cls.DEFAULT_BRIDGE_ORIGINS

    @classmethod
    def cors_paths(cls, path: str) -> bool:
        """Return whether *path* is a private API route safe for cloud CORS."""
        parsed_path = urlparse(path).path
        return parsed_path.startswith("/api/application/") or parsed_path in {
            "/api/health", "/api/library", "/api/match", "/api/context",
            "/api/context/job", "/api/context/answer", "/api/context/hint",
            "/api/context/hint/dismiss", "/api/run", "/api/run/approve",
            "/api/workshop", "/api/workshop/edit", "/api/workshop/ai",
            "/api/workshop/revert",
        }

    @classmethod
    def issue_project_capability(cls) -> Dict[str, Any]:
        token = secrets.token_urlsafe(32)
        expires = time.time() + cls.PROJECT_CAPABILITY_TTL
        with cls.project_capability_lock:
            now = time.time()
            cls.project_capabilities = {
                key: value for key, value in cls.project_capabilities.items()
                if value > now
            }
            cls.project_capabilities[token] = expires
        return {"capability": token, "expires_at": dt.datetime.fromtimestamp(expires, dt.timezone.utc).isoformat().replace("+00:00", "Z")}

    @classmethod
    def valid_project_capability(cls, token: str) -> bool:
        if not token:
            return False
        with cls.project_capability_lock:
            expires = cls.project_capabilities.get(token, 0)
            if expires <= time.time():
                cls.project_capabilities.pop(token, None)
                return False
            return True

    def project_guard(self, parsed_path: str) -> bool:
        """Require a short-lived capability for all project data routes."""
        if parsed_path == "/api/project/capability":
            return True
        if parsed_path == "/api/projects" or parsed_path.startswith("/api/project/") or parsed_path == "/api/project":
            query_capability = parse_qs(urlparse(self.path).query).get("capability", [""])[0] if parsed_path == "/api/project/artifact" else ""
            token = self.headers.get("X-Resume-Project-Capability", "") or query_capability
            if not self.valid_project_capability(token):
                self.send_json({"error": "project capability required"}, HTTPStatus.FORBIDDEN)
                return False
        return True

    @classmethod
    def cors_headers_for(cls, origin: str, path: str) -> Dict[str, str]:
        """Build exact-origin CORS headers without exposing files or artifacts."""
        normalized = str(origin or "").strip().rstrip("/")
        if not cls.cors_paths(path):
            return {}
        if normalized.startswith("chrome-extension://"):
            allowed = normalized
            private_network = False
        elif normalized in cls.bridge_origins():
            allowed = normalized
            private_network = True
        else:
            return {}
        headers = {
            "Access-Control-Allow-Origin": allowed,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        }
        if private_network:
            headers["Access-Control-Allow-Private-Network"] = "true"
        return headers

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in self.cors_headers_for(self.headers.get("Origin", ""), self.path).items():
            self.send_header(name, value)
        super().end_headers()

    def valid_host(self) -> bool:
        try:
            host = urlparse("//" + (self.headers.get("Host") or "")).hostname
        except ValueError:
            return False
        return host in {"127.0.0.1", "localhost", "::1"}

    def reject_bad_host(self) -> bool:
        if self.valid_host():
            return False
        self.send_json({"error": "loopback host required"}, HTTPStatus.MISDIRECTED_REQUEST)
        return True

    def do_OPTIONS(self) -> None:
        if self.reject_bad_host():
            return
        if self.cors_headers_for(self.headers.get("Origin", ""), self.path):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_json({"error": "cross-origin access is disabled"}, HTTPStatus.METHOD_NOT_ALLOWED)

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
        if self.reject_bad_host():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if parsed.path == "/resume_workspace.js":
            asset = repo_root() / "webapp" / "resume_workspace.js"
            if not asset.is_file():
                return self.send_json({"error": "workspace asset not found"}, HTTPStatus.NOT_FOUND)
            return self.send_bytes(asset.read_bytes(), "text/javascript; charset=utf-8")
        if not self.project_guard(parsed.path):
            return
        if parsed.path == "/":
            return self.send_bytes(UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if parsed.path == "/api/project/capability":
            return self.send_json(self.issue_project_capability())
        if parsed.path == "/api/projects":
            try:
                return self.send_json(resume_projects.list_projects(cv_root(repo_root())))
            except (OSError, ValueError, RuntimeError) as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/project":
            params = parse_qs(parsed.query)
            project_id = params.get("id", [""])[0]
            rel = params.get("path", [""])[0]
            try:
                if rel:
                    return self.send_json(resume_projects.read_file(cv_root(repo_root()), project_id, rel))
                return self.send_json(resume_projects.list_project_files(cv_root(repo_root()), project_id))
            except resume_projects.ProjectNotFound as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (resume_projects.ProjectError, OSError) as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/project/history":
            params = parse_qs(parsed.query)
            try:
                return self.send_json(resume_projects.history(cv_root(repo_root()), params.get("id", [""])[0]))
            except resume_projects.ProjectNotFound as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (resume_projects.ProjectError, OSError) as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/project/build":
            params = parse_qs(parsed.query)
            try:
                return self.send_json(resume_projects.build_status(cv_root(repo_root()), params.get("id", [""])[0], params.get("build_id", [""])[0]))
            except resume_projects.ProjectNotFound as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (resume_projects.ProjectError, OSError) as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/project/artifact":
            params = parse_qs(parsed.query)
            try:
                target, content_type = resume_projects.artifact(cv_root(repo_root()), params.get("id", [""])[0], params.get("path", [""])[0])
                return self.send_bytes(target.read_bytes(), content_type, download_name=target.name if target.suffix.lower() == ".pdf" else "")
            except resume_projects.ProjectNotFound as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (resume_projects.ProjectError, OSError) as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parsed.path == "/api/health":
            graph = evidence_graph(repo_root())
            runtime = engine_runtime_identity(self.manager.workers)
            return self.send_json({
                "ok": True,
                "ready": not runtime["restart_required"],
                "restart_required": runtime["restart_required"],
                "runtime": runtime,
                "queue": self.manager.health(),
                "providers": {k: bool(v) for k, v in provider_commands().items()},
                "cv_present": cv_root(repo_root()).is_dir(),
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
        if parsed.path == "/api/context":
            return self.send_json(context_inventory(repo_root()))
        if parsed.path == "/api/application/health":
            return self.send_json({
                "ok": True,
                "version": "application-agent-v1",
                "store": str(application_store_path(repo_root())),
                "sessions": len(application_sessions(repo_root())),
                "open_issues": len(list_application_issues(repo_root(), status="open")),
            })
        if parsed.path == "/api/application/resume":
            params = parse_qs(parsed.query)
            job_id = params.get("job_id", [""])[0]
            job = current_scored_jobs(repo_root()).get(job_id)
            if not job:
                return self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            return self.send_json(application_resume_status(job, repo_root(), queue_id=params.get("queue_id", [""])[0]))
        if parsed.path == "/api/application/context":
            return self.send_json(application_context(repo_root()))
        if parsed.path == "/api/application/issues":
            status = parse_qs(parsed.query).get("status", [""])[0]
            return self.send_json({"issues": list_application_issues(repo_root(), status=status)})
        if parsed.path == "/api/application/sessions":
            return self.send_json({"sessions": application_sessions(repo_root())})
        if parsed.path == "/api/application/session":
            session_id = parse_qs(parsed.query).get("session_id", [""])[0]
            session = get_application_session(repo_root(), session_id)
            if not session:
                return self.send_json({"error": "application session not found"}, HTTPStatus.NOT_FOUND)
            return self.send_json(session)
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
        if self.reject_bad_host():
            return
        parsed = urlparse(self.path)
        if not self.project_guard(parsed.path):
            return
        if parsed.path not in {
            "/api/run", "/api/run/approve", "/api/match", "/api/evidence/refresh", "/api/evidence/review",
            "/api/context/job", "/api/context/answer", "/api/context/hint", "/api/context/hint/dismiss",
            "/api/workshop/edit", "/api/workshop/ai", "/api/workshop/revert",
            "/api/application/session", "/api/application/resume", "/api/application/resume-file", "/api/application/form", "/api/application/review",
            "/api/application/answer", "/api/application/essay", "/api/application/mapping", "/api/application/event",
            "/api/application/confirm", "/api/application/verify", "/api/application/issue", "/api/application/tracker-sync",
            "/api/project", "/api/project/file", "/api/project/compile", "/api/project/revert",
        }:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > (14_000_000 if parsed.path == "/api/project/file" else 100_000):
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/project":
                action = str(body.get("action") or "")
                if action == "create":
                    return self.send_json(resume_projects.create_project(
                        cv_root(repo_root()), str(body.get("name") or ""), template=str(body.get("template") or "blank")), HTTPStatus.CREATED)
                if action == "archive":
                    return self.send_json(resume_projects.archive_project(
                        cv_root(repo_root()), str(body.get("project_id") or ""), bool(body.get("archived", True))))
                return self.send_json({"error": "unsupported project action"}, HTTPStatus.BAD_REQUEST)
            if parsed.path == "/api/project/file":
                action = str(body.get("action") or "")
                project_id = str(body.get("project_id") or "")
                if action == "save":
                    return self.send_json(resume_projects.save_file(
                        cv_root(repo_root()), project_id, str(body.get("path") or ""), str(body.get("content") or ""), str(body.get("expected_sha256") or "")))
                return self.send_json(resume_projects.mutate_file(
                    cv_root(repo_root()), project_id, action,
                    path=body.get("path"), new_path=body.get("new_path"), content=body.get("content"), data=body.get("data")))
            if parsed.path == "/api/project/compile":
                return self.send_json(resume_projects.compile_project(cv_root(repo_root()), str(body.get("project_id") or "")), HTTPStatus.ACCEPTED)
            if parsed.path == "/api/project/revert":
                return self.send_json(resume_projects.restore(cv_root(repo_root()), str(body.get("project_id") or ""), str(body.get("revision_id") or "")))
            if parsed.path == "/api/application/tracker-sync":
                return self.send_json(request_tracker_sync(repo_root()))
            if parsed.path == "/api/application/resume":
                job_id = str(body.get("job_id") or "")
                job = current_scored_jobs(repo_root()).get(job_id)
                if not job:
                    imported = bridged_job(body.get("job"))
                    if imported and str(imported.get("id") or "") == job_id:
                        job = imported
                if not job:
                    return self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                return self.send_json(application_resume_status(
                    job, repo_root(), queue_id=str(body.get("queue_id") or ""),
                    allow_fallback=bool(body.get("allow_fallback")),
                ))
            if parsed.path == "/api/application/resume-file":
                job_id = str(body.get("job_id") or "")
                job = current_scored_jobs(repo_root()).get(job_id)
                if not job:
                    imported = bridged_job(body.get("job"))
                    if imported and str(imported.get("id") or "") == job_id:
                        job = imported
                if not job:
                    return self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                status, target = application_resume_file(job, repo_root(), str(body.get("queue_id") or ""))
                if target is None:
                    return self.send_json({"error": status.get("message") or "resume PDF unavailable"}, HTTPStatus.NOT_FOUND)
                return self.send_bytes(target.read_bytes(), "application/pdf", download_name=str(status.get("pdf_filename") or target.name))
            if parsed.path == "/api/application/session":
                job = body.get("job") if isinstance(body.get("job"), dict) else body
                return self.send_json(create_application_session(
                    repo_root(), job,
                    mode=str(body.get("mode") or "per_role"),
                    queue_id=str(body.get("queue_id") or ""),
                ), HTTPStatus.CREATED)
            if parsed.path == "/api/application/form":
                return self.send_json(plan_application_form(
                    repo_root(),
                    str(body.get("session_id") or ""),
                    str(body.get("page_url") or ""),
                    body.get("fields") if isinstance(body.get("fields"), list) else [],
                    final=bool(body.get("final")),
                ))
            if parsed.path == "/api/application/essay":
                job = body.get("job") if isinstance(body.get("job"), dict) else {}
                imported = bridged_job(job)
                if imported:
                    job = {**job, **imported, "description": job.get("description") or imported.get("description") or ""}
                return self.send_json(application_essay_answer(
                    repo_root(), job, str(body.get("question") or ""),
                    session_id=str(body.get("session_id") or ""),
                    character_limit=int(body.get("character_limit") or 0),
                    word_limit=int(body.get("word_limit") or 0),
                    category=str(body.get("category") or "essay"),
                ))
            if parsed.path == "/api/application/review":
                return self.send_json({"review": prepare_application_review(
                    repo_root(),
                    str(body.get("session_id") or ""),
                    body.get("fields") if isinstance(body.get("fields"), list) else None,
                )})
            if parsed.path == "/api/application/answer":
                return self.send_json(save_application_answer(
                    repo_root(),
                    question=str(body.get("question") or body.get("label") or ""),
                    value=str(body.get("value") or body.get("answer") or ""),
                    category=str(body.get("category") or ""),
                    reusable=bool(body.get("reusable", True)),
                    sensitive=body.get("sensitive"),
                    answer_id=str(body.get("answer_id") or ""),
                    variants=body.get("variants") if isinstance(body.get("variants"), list) else [],
                    fallback_for=body.get("fallback_for") if isinstance(body.get("fallback_for"), list) else [],
                    select_all=bool(body.get("select_all")),
                    evidence_ids=body.get("evidence_ids") if isinstance(body.get("evidence_ids"), list) else [],
                    session_id=str(body.get("session_id") or ""),
                ))
            if parsed.path == "/api/application/mapping":
                return self.send_json(save_application_mapping(
                    repo_root(), str(body.get("field_key") or ""), str(body.get("answer_id") or "")))
            if parsed.path == "/api/application/event":
                return self.send_json(record_application_event(
                    repo_root(), str(body.get("session_id") or ""), str(body.get("state") or ""),
                    message=str(body.get("message") or ""), error=str(body.get("error") or "")))
            if parsed.path == "/api/application/confirm":
                return self.send_json(apply_application_confirmation(
                    repo_root(), str(body.get("session_id") or ""), str(body.get("review_hash") or ""),
                    str(body.get("nonce") or ""), page_fingerprint=str(body.get("page_fingerprint") or ""),
                    owner_approved_at=str(body.get("owner_approved_at") or ""),
                    approval_expires_at=str(body.get("approval_expires_at") or "")))
            if parsed.path == "/api/application/verify":
                return self.send_json(verify_application_submission_page(
                    repo_root(), str(body.get("session_id") or ""), str(body.get("page_url") or ""),
                    body.get("fields") if isinstance(body.get("fields"), list) else []))
            if parsed.path == "/api/application/issue":
                return self.send_json(add_application_issue(
                    repo_root(), str(body.get("session_id") or ""), str(body.get("issue_type") or "unknown"),
                    str(body.get("message") or ""), field_label=str(body.get("field_label") or ""),
                    page_url=str(body.get("page_url") or ""), fingerprint=str(body.get("fingerprint") or ""),
                    selector_kind=str(body.get("selector_kind") or "")))
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
            if parsed.path == "/api/context/answer":
                return self.send_json(update_context_answer(
                    item_id=str(body.get("id") or ""),
                    response=str(body.get("response") or ""),
                    answer=str(body.get("answer") or ""),
                    where_when=str(body.get("where_when") or ""),
                    root=repo_root(),
                ))
            if parsed.path == "/api/context/hint":
                return self.send_json(update_context_hint(
                    item_id=str(body.get("id") or ""),
                    label=str(body.get("label") or ""),
                    note=str(body.get("note") or ""),
                    source_url=str(body.get("source_url") or ""),
                    root=repo_root(),
                ))
            if parsed.path == "/api/context/hint/dismiss":
                return self.send_json(update_context_hint_dismissal(
                    item_id=str(body.get("id") or ""),
                    label=str(body.get("label") or ""),
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
                imported = bridged_job(body.get("job_snapshot"))
                if imported and str(imported.get("id") or "") == job_id:
                    job = imported
            if not job:
                return self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            if parsed.path in {"/api/match", "/api/context/job"}:
                posting_text = fetch_job_description(job)
                inventory = context_inventory(
                    repo_root(), job=job, posting_text=posting_text,
                )
            if parsed.path == "/api/context/job":
                return self.send_json(inventory)
            if parsed.path == "/api/match":
                match = resume_match_for_job(job, repo_root(), posting_text=posting_text)
                return self.send_json({
                    "job_id": job_id, "resume_match": match,
                    "context_summary": inventory.get("summary", {}),
                })
            mode = str(body.get("mode") or "")
            queue_id = str(body.get("queue_id") or "").strip()[:80]
            control_profile = body.get("control_profile")
            if not isinstance(control_profile, dict):
                control_profile = None
            status = self.manager.start(
                job, mode, queue_id=queue_id, control_profile=control_profile,
            )
            return self.send_json(status, HTTPStatus.ACCEPTED)
        except ResumeStudioRuntimeStale as exc:
            return self.send_json({"error": str(exc), "restart_required": True}, HTTPStatus.CONFLICT)
        except resume_projects.ProjectError as exc:
            return self.send_json({"error": str(exc)}, getattr(exc, "status_code", HTTPStatus.BAD_REQUEST))
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run Victor's local Resume Studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4317)
    args = parser.parse_args(list(argv) if argv is not None else None)
    recovery = StudioHandler.manager.recover_pending()
    server = ThreadingHTTPServer((args.host, args.port), StudioHandler)
    print("Resume Studio: http://%s:%s/" % (args.host, args.port))
    print("Private CV root: %s" % cv_root(repo_root()))
    print("Providers: %s" % ", ".join(name for name, path in provider_commands().items() if path) or "none")
    print("Recovered runs: %s" % recovery.get("recovered", 0))

    def handle_sigterm(signum, frame) -> None:
        del frame
        stop_all_provider_processes()
        StudioHandler.manager.shutdown(wait=False)
        # The queue snapshots and provider termination have already happened.
        # Bypass Python's executor atexit join so a nested worker pool cannot
        # race interpreter shutdown and turn recoverable work into a failure.
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nResume Studio stopped")
    finally:
        server.server_close()
        stop_all_provider_processes()
        StudioHandler.manager.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
