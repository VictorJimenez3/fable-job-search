"""Deterministic, owner-reviewed application form agent.

The browser is intentionally a thin client.  This module owns the durable
local context bank, form matching, page fingerprints, review cards, and the
small state machine that makes an application pause instead of guessing.

It does not fetch job sites, submit forms, or write the repository.  A local
HTTP adapter and the first-party browser extension call these functions after
the owner has opened a posting.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


APPLICATION_AGENT_VERSION = "application-agent-v1"
APPLICATION_AGENT_DIRNAME = ".resume_studio"
APPLICATION_AGENT_FILENAME = "application_agent.json"
APPLICATION_AGENT_MARKDOWN = "application_context.md"

SESSION_STATES = {
    "queued",
    "opening",
    "filling",
    "blocked",
    "awaiting_confirmation",
    "submitting",
    "submitted",
    "failed",
    "skipped",
    "cancelled",
}
TERMINAL_STATES = {"submitted", "failed", "skipped", "cancelled"}

ATS_HOST_PATTERNS = {
    "workday": (r"workday", r"myworkdayjobs\.com", r"myworkdaysite\.com"),
    "greenhouse": (r"greenhouse\.io", r"greenhouse\.com"),
    "lever": (r"jobs\.lever\.co", r"lever\.co"),
    "ashby": (r"ashbyhq\.com",),
    "smartrecruiters": (r"smartrecruiters\.com",),
}

SENSITIVE_CATEGORIES = {
    "work_authorization",
    "sponsorship",
    "disability",
    "veteran_status",
    "gender",
    "race_ethnicity",
    "demographic",
    "salary",
    "address",
    "phone",
}

QUESTION_LIMIT = 1200
VALUE_LIMIT = 20000
MAX_CONTEXT_ENTRIES = 500
MAX_SESSIONS = 100
MAX_ISSUES = 300
REVIEW_TTL_SECONDS = 15 * 60


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp(value: Any) -> Optional[float]:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def clean_text(value: Any, limit: int = 500) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def has_field_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return bool(clean_text(value, VALUE_LIMIT))


def display_field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "checked" if value else "unchecked"
    return clean_text(value, VALUE_LIMIT)


def normalize_question(value: Any) -> str:
    text = clean_text(value, QUESTION_LIMIT).lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9@+_.?/-]+", " ", text)
    return text.strip()


def safe_url(value: Any) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.netloc:
        return ""
    return parsed.geturl()[:2500]


def provider_for_url(value: Any) -> str:
    host = (urlparse(safe_url(value)).hostname or "").lower()
    for provider, patterns in ATS_HOST_PATTERNS.items():
        if any(re.search(pattern, host) for pattern in patterns):
            return provider
    return "generic"


def normalized_field_key(field: Dict[str, Any]) -> str:
    return normalize_question(
        field.get("autocomplete")
        or field.get("name")
        or field.get("id")
        or field.get("label")
        or field.get("question")
    )


def infer_category(field: Dict[str, Any]) -> str:
    label = normalize_question(
        " ".join(
            clean_text(field.get(key), 500)
            for key in ("label", "question", "group_question", "name", "id", "autocomplete", "placeholder")
        )
    )
    field_type = clean_text(field.get("type"), 32).lower()
    if field_type == "file":
        return "resume_file"
    # A choice control is an owner decision, not a reusable text field. ATS
    # pages often label options "LinkedIn", "Website", or with a city; using
    # those option labels as profile categories produces a false fill.
    if field_type in {"checkbox", "radio", "button", "select"}:
        if re.search(r"\b(llm\w*|large language model|language model|generative ai)\b", label):
            return "llm_experience"
        if re.search(r"\b(anchor days|work from our offices|on[- ]?site|in[- ]?office)\b", label):
            return "work_schedule"
        if re.search(r"\b(sponsor|sponsorship|visa)\b", label):
            return "sponsorship"
        if re.search(r"\b(work authorization|legally authorized|authorized to work)\b", label):
            return "work_authorization"
        if re.search(r"\b(relocat\w*|location preference|willing to move)\b", label):
            return "location"
        if re.search(r"\b(disability|disabled|accommodation)\b", label):
            return "disability"
        if re.search(r"\b(veteran|military)\b", label):
            return "veteran_status"
        if re.search(r"\b(gender|sex|pronoun|pronouns)\b", label):
            return "gender"
        if re.search(r"\b(race|ethnicity|ethnic)\b", label):
            return "race_ethnicity"
        if field_type in {"checkbox", "radio", "button"}:
            return "attestation"
    if "cover letter" in label:
        return "cover_letter"
    if re.search(r"\b(first|given) name\b", label):
        return "first_name"
    if re.search(r"\b(last|family|sur)name\b", label):
        return "last_name"
    if re.search(r"\b(full|preferred) name\b", label):
        return "full_name"
    if "email" in label or field_type == "email":
        return "email"
    if re.search(r"\b(phone|mobile|telephone|cell)\b", label) or field_type == "tel":
        return "phone"
    if "linkedin" in label:
        return "linkedin"
    if re.search(r"\b(github|gitlab|portfolio|personal site|website|project url)\b", label):
        return "portfolio_link"
    if re.search(r"\b(address|street|apt|apartment|postal|zip code|postcode)\b", label):
        return "address"
    if re.search(r"\b(city|state|province|country|location)\b", label):
        return "location"
    if re.search(r"\b(work authorization|legally authorized|authorized to work|visa|sponsor|sponsorship)\b", label):
        return "work_authorization" if "sponsor" not in label and "visa" not in label else "sponsorship"
    if re.search(r"\b(salary|compensation|pay expectation|desired pay)\b", label):
        return "salary"
    if re.search(r"\b(disability|disabled|accommodation)\b", label):
        return "disability"
    if re.search(r"\b(veteran|military)\b", label):
        return "veteran_status"
    if re.search(r"\b(gender|sex|pronoun|pronouns)\b", label):
        return "gender"
    if re.search(r"\b(race|ethnicity|ethnic)\b", label):
        return "race_ethnicity"
    if re.search(r"\b(agree|consent|certif|attest|acknowledge|accurate|truthful|terms)\b", label):
        return "attestation"
    if field_type in {"checkbox", "radio"} and len(label) > 35:
        return "attestation"
    if "education" in label or re.search(r"\b(school|university|college|degree|major|graduat)\b", label):
        return "education"
    if field_type == "textarea" or len(label) > 65 or re.search(
        r"\b(why|describe|tell us|explain|essay|interest|motivat|experience with|anything else)\b", label
    ):
        return "essay"
    if re.search(r"\b(resume|cv)\b", label):
        return "resume_file"
    return "other"


def is_sensitive(category: str, field: Optional[Dict[str, Any]] = None) -> bool:
    if category in SENSITIVE_CATEGORIES:
        return True
    label = normalize_question((field or {}).get("label") or (field or {}).get("question"))
    return bool(re.search(r"\b(ssn|social security|date of birth|dob|passport)\b", label))


def form_fingerprint(url: Any, fields: Iterable[Dict[str, Any]]) -> str:
    parsed = urlparse(safe_url(url))
    shape = {
        "origin": f"{parsed.scheme}://{parsed.netloc}".lower(),
        "path": parsed.path.rstrip("/") or "/",
        "fields": [
            {
                "key": normalized_field_key(field),
                "type": clean_text(field.get("type"), 32).lower(),
                "category": clean_text(field.get("category") or infer_category(field), 60),
                "required": bool(field.get("required")),
                "options": sorted(
                    normalize_question(option.get("label") if isinstance(option, dict) else option)
                    for option in (field.get("options") or [])
                )[:100],
            }
            for field in fields
            if isinstance(field, dict) and not field.get("hidden")
        ],
    }
    encoded = json.dumps(shape, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _form_value_signature(fields: Iterable[Dict[str, Any]]) -> List[Tuple[str, str, str, bool]]:
    """Capture values that must change before a review-ready rescan is useful."""
    signature: List[Tuple[str, str, str, bool]] = []
    for index, field in enumerate(fields):
        if not isinstance(field, dict) or field.get("hidden"):
            continue
        field_id = clean_text(field.get("field_id") or field.get("id") or f"field-{index}", 160)
        signature.append((
            field_id,
            clean_text(field.get("type"), 32).lower(),
            display_field_value(field.get("value", "")),
            bool(field.get("is_submit")),
        ))
    return signature


def review_hash(value: Dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def store_path(root: Path) -> Path:
    return root / "CV" / APPLICATION_AGENT_DIRNAME / APPLICATION_AGENT_FILENAME


def markdown_path(root: Path) -> Path:
    return root / "CV" / APPLICATION_AGENT_DIRNAME / APPLICATION_AGENT_MARKDOWN


def _canonical_resume_answers(root: Path) -> Dict[str, Dict[str, Any]]:
    """Derive only deterministic profile fields from the local canonical CV.

    These values are already owner-authored source data.  They stay local until
    the extension's normal private-bank sync sends them to the owner's private
    Application Agent Sheet.  Essays, attestations, work authorization, and
    other judgment-heavy fields are deliberately excluded.
    """
    candidates = (
        root / "CV" / "immutable" / "VictorJimenezResume.tex",
        root / "CV" / "cv_full.tex",
    )
    source = ""
    for path in candidates:
        try:
            source = path.read_text(encoding="utf-8")
            if source.strip():
                break
        except OSError:
            continue
    if not source:
        return {}

    # Template attribution and other LaTeX comments are not candidate profile
    # data. They can contain public URLs before the actual resume header.
    source = "\n".join(line.split("%", 1)[0] for line in source.splitlines())

    def match(pattern: str) -> str:
        found = re.search(pattern, source, flags=re.IGNORECASE)
        return clean_text(found.group(1) if found else "", VALUE_LIMIT)

    values = {
        "full_name": match(r"\\textbf\{\\Huge\s+\\scshape\s+([^}]+)\}"),
        "email": match(r"(?:mailto:)?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"),
        "phone": match(r"(\(\d{3}\)\s*\d{3}[-\s]\d{4})"),
        "linkedin": match(r"((?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+)"),
        "github": match(r"((?:https?://)?github\.com/[A-Za-z0-9_-]+)"),
        "school": match(r"\{\\large\s+([^}]*Institute[^}]*)\}"),
    }
    categories = {
        "full_name": "full_name", "email": "email", "phone": "phone",
        "linkedin": "linkedin", "github": "portfolio_link", "school": "education",
    }
    answers: Dict[str, Dict[str, Any]] = {}
    for key, value in values.items():
        if not value:
            continue
        question = {
            "full_name": "Full Name", "email": "Email", "phone": "Phone",
            "linkedin": "LinkedIn Profile", "github": "Github Link", "school": "School",
        }[key]
        category = categories[key]
        answer_id = f"canonical-{key}"
        answers[answer_id] = {
            "answer_id": answer_id,
            "question": question,
            "normalized_question": normalize_question(question),
            "variants": [question, key.replace("_", " ")],
            "category": category,
            "value": value,
            "reusable": True,
            "sensitive": category in SENSITIVE_CATEGORIES,
            "evidence_ids": ["canonical-resume"],
            "source": "canonical-resume",
            "updated_at": utc_now(),
        }
    return answers


def _default_store() -> Dict[str, Any]:
    return {
        "version": APPLICATION_AGENT_VERSION,
        "updated_at": utc_now(),
        "context": {"answers": {}, "mappings": {}},
        "sessions": {},
        "issues": [],
    }


def load_store(root: Path) -> Dict[str, Any]:
    path = store_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = _default_store()
    if not isinstance(value, dict):
        value = _default_store()
    base = _default_store()
    base.update(value)
    context = value.get("context") if isinstance(value.get("context"), dict) else {}
    base["context"] = {"answers": {}, "mappings": {}, **context}
    if not isinstance(base["context"].get("answers"), dict):
        base["context"]["answers"] = {}
    if not isinstance(base["context"].get("mappings"), dict):
        base["context"]["mappings"] = {}
    defaults = _canonical_resume_answers(root)
    if defaults:
        answers = base["context"]["answers"]
        changed: Dict[str, Dict[str, Any]] = {}
        for answer_id, answer in defaults.items():
            existing = answers.get(answer_id)
            canonical_existing = isinstance(existing, dict) and (
                existing.get("source") == "canonical-resume"
                or "canonical-resume" in (existing.get("evidence_ids") or [])
            )
            comparable = (
                "question", "normalized_question", "variants", "category",
                "value", "reusable", "sensitive", "evidence_ids", "source",
            )
            if not isinstance(existing, dict) or (
                canonical_existing
                and any(existing.get(key) != answer.get(key) for key in comparable)
            ):
                changed[answer_id] = answer
        if changed:
            answers.update(changed)
            mappings = base["context"]["mappings"]
            for answer_id, answer in changed.items():
                mappings.setdefault(answer["normalized_question"], answer_id)
            write_store(root, base)
    if not isinstance(base.get("sessions"), dict):
        base["sessions"] = {}
    if not isinstance(base.get("issues"), list):
        base["issues"] = []
    return base


def write_store(root: Path, store: Dict[str, Any]) -> None:
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    store["version"] = APPLICATION_AGENT_VERSION
    store["updated_at"] = utc_now()
    fd, temporary = tempfile.mkstemp(prefix="application-agent-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def write_context_markdown(root: Path, store: Dict[str, Any]) -> Path:
    """Write a readable local mirror; JSON remains the machine source of truth."""
    path = markdown_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    answers = list((store.get("context") or {}).get("answers", {}).values())
    answers.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    lines = [
        "# Job Radar application context",
        "",
        "This file is a readable mirror of `application_agent.json`. Edit answers in Job Radar so question normalization and approval metadata stay intact.",
        "",
        f"Updated: {store.get('updated_at') or utc_now()}",
        "",
    ]
    for answer in answers:
        category = clean_text(answer.get("category"), 80) or "other"
        question = clean_text(answer.get("question"), QUESTION_LIMIT)
        value = clean_text(answer.get("value"), VALUE_LIMIT)
        sensitivity = " · sensitive" if answer.get("sensitive") else ""
        lines.extend([f"## {question or category}", "", f"- Category: `{category}`{sensitivity}", f"- Reusable: `{bool(answer.get('reusable', True))}`"])
        if answer.get("evidence_ids"):
            lines.append(f"- Evidence: {', '.join(clean_text(item, 120) for item in answer['evidence_ids'][:8])}")
        lines.extend(["", value, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _answer_allowed_for_field(answer: Dict[str, Any], field: Dict[str, Any]) -> bool:
    """Honor an explicit fallback only when the preferred option is absent."""
    fallback_for = answer.get("fallback_for")
    if not isinstance(fallback_for, list) or not fallback_for:
        return True
    options = field.get("options") or []
    group_options = field.get("group_options") or []
    labels = []
    for option in [*options, *group_options]:
        if isinstance(option, dict):
            labels.append(option.get("label") or option.get("value"))
        else:
            labels.append(option)
    normalized_options = {normalize_question(item) for item in labels if normalize_question(item)}
    return not any(normalize_question(item) in normalized_options for item in fallback_for)


def _answer_matches_field(answer: Dict[str, Any], field: Dict[str, Any], normalized: str) -> bool:
    variants = [normalize_question(answer.get("question"))]
    variants.extend(normalize_question(item) for item in answer.get("variants", []) if item)
    variants.append(normalize_question(answer.get("value")))
    field_type = clean_text(field.get("type"), 32).lower()
    option_label = field.get("option_label")
    if option_label or field_type in {"radio", "checkbox", "button"}:
        # ATS option groups repeat the same group question on every sibling.
        # Matching that question alone would approve both Yes and No.  Each
        # rendered option must match the approved answer's actual value/label.
        selected_label = option_label or field.get("label")
        return normalize_question(selected_label) in variants
    field_questions = [
        normalized,
        normalize_question(field.get("group_question")),
        normalize_question(field.get("question")),
        normalize_question(field.get("label")),
    ]
    if any(question and question in variants for question in field_questions):
        return True
    options = field.get("options") or []
    group_options = field.get("group_options") or []
    for option in [*options, *group_options]:
        option_label = option.get("label") if isinstance(option, dict) else option
        if normalize_question(option_label) in variants:
            return True
    return False


def _answer_candidates(store: Dict[str, Any], field: Dict[str, Any]) -> List[Dict[str, Any]]:
    answers = (store.get("context") or {}).get("answers", {})
    if not isinstance(answers, dict):
        return []
    category = clean_text(field.get("category") or infer_category(field), 80)
    normalized = normalize_question(field.get("question") or field.get("label"))
    key = normalized_field_key(field)
    mappings = (store.get("context") or {}).get("mappings", {})
    mapped_id = mappings.get(key) or mappings.get(category)
    values: List[Dict[str, Any]] = []
    if mapped_id and isinstance(answers.get(mapped_id), dict):
        mapped = answers[mapped_id]
        if _answer_allowed_for_field(mapped, field):
            values.append(mapped)
    for answer in answers.values():
        if not isinstance(answer, dict) or answer in values:
            continue
        if _answer_allowed_for_field(answer, field) and _answer_matches_field(answer, field, normalized):
            values.append(answer)
    if not values and category == "location" and clean_text(field.get("type"), 32).lower() == "checkbox":
        group_question = normalize_question(field.get("group_question"))
        if "relocat" in group_question:
            values.extend(
                answer for answer in answers.values()
                if isinstance(answer, dict)
                and answer.get("select_all")
                and clean_text(answer.get("category"), 80) == "location"
            )
    choice_control = (
        clean_text(field.get("type"), 32).lower() in {"radio", "checkbox", "button", "select"}
        or bool(field.get("options"))
        or bool(field.get("group_options"))
    )
    # Several ATS forms reuse short option labels such as "Yes" and "No".
    # Prefer an answer from the field's inferred category so one approved
    # decision cannot accidentally satisfy a different choice group.
    category_values = [
        answer for answer in values
        if clean_text(answer.get("category"), 80) == category
    ]
    if choice_control and category in SENSITIVE_CATEGORIES:
        values = category_values
    elif category_values:
        values = category_values + [answer for answer in values if answer not in category_values]
    if not values and not choice_control and category not in {"other", "essay", "attestation", "resume_file"}:
        for answer in answers.values():
            if not isinstance(answer, dict) or not answer.get("reusable", True):
                continue
            if clean_text(answer.get("category"), 80) == category and _answer_allowed_for_field(answer, field):
                values.append(answer)
    return values


def _field_review(field: Dict[str, Any], value: Any = "", answer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    category = clean_text(field.get("category") or infer_category(field), 80)
    label = clean_text(field.get("label") or field.get("question") or field.get("name"), 500)
    group_question = clean_text(field.get("group_question"), 500)
    if group_question and normalize_question(group_question) != normalize_question(label):
        label = f"{group_question} · {label}"[:500]
    return {
        "field_id": clean_text(field.get("field_id") or field.get("id"), 160),
        "label": label,
        "category": category,
        "type": clean_text(field.get("type"), 32).lower(),
        "required": bool(field.get("required")),
        "sensitive": is_sensitive(category, field),
        "value": display_field_value(value),
        "answer_id": clean_text((answer or {}).get("answer_id"), 100),
    }


def _approved_fill_value(field: Dict[str, Any], answer: Dict[str, Any]) -> str:
    """Adapt one approved answer to the exact option rendered by an ATS."""
    field_type = clean_text(field.get("type"), 32).lower()
    if field_type in {"radio", "checkbox", "button"}:
        return clean_text(field.get("option_label") or field.get("label") or answer.get("value"), VALUE_LIMIT)
    if field_type == "select":
        variants = {normalize_question(answer.get("question")), normalize_question(answer.get("value"))}
        variants.update(normalize_question(item) for item in answer.get("variants", []) if item)
        for option in field.get("options") or []:
            label = option.get("label") if isinstance(option, dict) else option
            if normalize_question(label) in variants:
                return clean_text(option.get("value") if isinstance(option, dict) else label, VALUE_LIMIT)
    return clean_text(answer.get("value"), VALUE_LIMIT)


def _new_session(job: Dict[str, Any], mode: str = "per_role", queue_id: str = "") -> Dict[str, Any]:
    clean_job = {
        "id": clean_text(job.get("id"), 160),
        "company": clean_text(job.get("company"), 240),
        "title": clean_text(job.get("title"), 360),
        "url": safe_url(job.get("url")),
        "locations": [clean_text(item, 160) for item in (job.get("locations") or [])[:12]],
    }
    return {
        "session_id": uuid.uuid4().hex,
        "queue_id": clean_text(queue_id, 100),
        "mode": mode if mode in {"per_role", "batch"} else "per_role",
        "state": "queued",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "job": clean_job,
        "url": clean_job["url"],
        "provider": provider_for_url(clean_job["url"]),
        "pages_seen": 0,
        "blockers": [],
        "optional_review": [],
        "review": None,
        "confirmation": None,
        "last_form": None,
        "last_message": "",
        "last_error": "",
    }


def create_session(root: Path, job: Dict[str, Any], mode: str = "per_role", queue_id: str = "") -> Dict[str, Any]:
    store = load_store(root)
    session = _new_session(job, mode=mode, queue_id=queue_id)
    store["sessions"][session["session_id"]] = session
    store["sessions"] = dict(list(store["sessions"].items())[-MAX_SESSIONS:])
    write_store(root, store)
    return public_session(session)


def get_session(root: Path, session_id: str) -> Optional[Dict[str, Any]]:
    store = load_store(root)
    session = store.get("sessions", {}).get(clean_text(session_id, 100))
    return copy.deepcopy(session) if isinstance(session, dict) else None


def _save_session(root: Path, store: Dict[str, Any], session: Dict[str, Any]) -> None:
    session["updated_at"] = utc_now()
    store["sessions"][session["session_id"]] = session
    write_store(root, store)


def plan_form(root: Path, session_id: str, page_url: str, fields: Iterable[Dict[str, Any]], final: bool = False) -> Dict[str, Any]:
    store = load_store(root)
    session = store.get("sessions", {}).get(clean_text(session_id, 100))
    if not isinstance(session, dict):
        raise ValueError("application session not found")
    incoming_fields = [field for field in fields if isinstance(field, dict)]
    previous_form = session.get("last_form") or {}
    if session.get("state") == "awaiting_confirmation" and previous_form:
        same_shape = form_fingerprint(page_url, incoming_fields) == clean_text(session.get("page_fingerprint"), 80)
        same_values = _form_value_signature(incoming_fields) == _form_value_signature(previous_form.get("fields") or [])
        if same_shape and same_values:
            raise ValueError("application review is already current; no page change was detected")
    normalized_fields: List[Dict[str, Any]] = []
    fills: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    optional_review: List[Dict[str, Any]] = []
    for index, raw in enumerate(fields):
        if not isinstance(raw, dict) or raw.get("hidden"):
            continue
        field = dict(raw)
        field["field_id"] = clean_text(field.get("field_id") or field.get("id") or f"field-{index}", 160)
        field["label"] = clean_text(field.get("label") or field.get("question") or field.get("name"), 500)
        field["type"] = clean_text(field.get("type"), 32).lower()
        field["category"] = clean_text(field.get("category") or infer_category(field), 80)
        field["required"] = bool(field.get("required"))
        field["is_submit"] = bool(field.get("is_submit"))
        normalized_fields.append(field)
        if field["is_submit"]:
            continue
        category = field["category"]
        sensitive = is_sensitive(category, field)
        candidates = _answer_candidates(store, field)
        answer = candidates[0] if candidates else None
        if category == "attestation":
            if answer and clean_text(answer.get("value"), VALUE_LIMIT):
                fills.append({
                    "field_id": field["field_id"],
                    "value": _approved_fill_value(field, answer),
                    "answer_id": clean_text(answer.get("answer_id"), 100),
                    "category": category,
                    "sensitive": sensitive,
                    "options": field.get("options") or [],
                })
                continue
            if has_field_value(field.get("value")):
                item = _field_review(field, field.get("value"))
                item["owner_provided"] = True
                optional_review.append(item)
                continue
            item = _field_review(field)
            item["reason"] = "Check this attestation on the page yourself, then retry so it appears on the final review card."
            if field["required"]:
                blockers.append(item)
            else:
                optional_review.append(item)
            continue
        if category in {"resume_file"}:
            if has_field_value(field.get("value")):
                item = _field_review(field, field.get("value"))
                item["owner_provided"] = True
                optional_review.append(item)
                continue
            item = _field_review(field)
            item["reason"] = "Resume Studio has not supplied a local PDF to the browser agent yet."
            if field["required"]:
                blockers.append(item)
            else:
                optional_review.append(item)
            continue
        if answer and clean_text(answer.get("value"), VALUE_LIMIT):
            value = _approved_fill_value(field, answer)
            fills.append({
                "field_id": field["field_id"],
                "value": value,
                "answer_id": clean_text(answer.get("answer_id"), 100),
                "category": category,
                "sensitive": sensitive,
                "options": field.get("options") or [],
            })
            continue
        if has_field_value(field.get("value")):
            item = _field_review(field, field.get("value"))
            item["owner_provided"] = True
            optional_review.append(item)
            continue
        item = _field_review(field)
        item["reason"] = (
            "No approved reusable answer matches this required field."
            if field["required"]
            else "No approved answer exists; review or answer it if the employer asks for it."
        )
        owner_only = category in {"work_authorization", "sponsorship"}
        if category in {"essay", "cover_letter"} or field["required"] or owner_only:
            blockers.append(item)
        else:
            optional_review.append(item)

    # A radio/button option group represents one decision.  Once an approved
    # answer selects one option (for example sponsorship = None), sibling
    # options are alternatives, not four additional unanswered questions.
    # Checkboxes remain independent because those groups may be multi-select.
    field_by_id = {field["field_id"]: field for field in normalized_fields}

    def single_choice_group(field: Dict[str, Any]) -> str:
        if clean_text(field.get("type"), 32).lower() not in {"radio", "button"}:
            return ""
        return clean_text(
            field.get("name") or field.get("group_key") or field.get("group_question"),
            500,
        )

    resolved_groups = {
        single_choice_group(field_by_id.get(fill.get("field_id"), {}))
        for fill in fills
        if single_choice_group(field_by_id.get(fill.get("field_id"), {}))
    }
    if resolved_groups:
        selected_ids = {fill.get("field_id") for fill in fills}

        def unresolved_alternative(item: Dict[str, Any]) -> bool:
            field = field_by_id.get(item.get("field_id"), {})
            group = single_choice_group(field)
            return bool(group and group in resolved_groups and item.get("field_id") not in selected_ids)

        blockers = [item for item in blockers if not unresolved_alternative(item)]
        optional_review = [item for item in optional_review if not unresolved_alternative(item)]

    fingerprint = form_fingerprint(page_url, normalized_fields)
    session["url"] = safe_url(page_url) or session.get("url", "")
    session["provider"] = provider_for_url(session["url"])
    session["pages_seen"] = int(session.get("pages_seen") or 0) + 1
    session["page_fingerprint"] = fingerprint
    session["last_form"] = {
        "url": session["url"],
        "fingerprint": fingerprint,
        "fields": normalized_fields,
        "final": bool(final),
    }
    session["blockers"] = blockers
    session["optional_review"] = optional_review
    session["review"] = None
    session["confirmation"] = None
    fill_by_id = {item["field_id"]: item for item in fills}
    session["review_fields"] = []
    for field in normalized_fields:
        if field.get("is_submit"):
            continue
        decision = fill_by_id.get(field["field_id"])
        answer = None
        if decision:
            answer = {"answer_id": decision.get("answer_id")}
        review_value = decision.get("value") if decision else field.get("value", "")
        session["review_fields"].append(_field_review(field, review_value, answer))
    session["state"] = "blocked" if blockers else "filling"
    if final and not blockers:
        _prepare_review(session)
    _save_session(root, store, session)
    return {
        "version": APPLICATION_AGENT_VERSION,
        "session_id": session["session_id"],
        "provider": session["provider"],
        "fingerprint": fingerprint,
        "state": session["state"],
        "fills": fills,
        "blockers": blockers,
        "optional_review": optional_review,
        "review": copy.deepcopy(session.get("review")),
        "pages_seen": session["pages_seen"],
    }


def _prepare_review(session: Dict[str, Any]) -> Dict[str, Any]:
    form = session.get("last_form") or {}
    fields = form.get("fields") or []
    proposed: List[Dict[str, Any]] = []
    for field in fields:
        if field.get("is_submit"):
            continue
        candidates = []
        # Values are attached by the caller below, or remain empty for unknown
        # optional/owner-confirmed fields.  The review card must show every
        # proposed value, including sensitive values, to make confirmation real.
        category = clean_text(field.get("category") or infer_category(field), 80)
        proposed.append(_field_review(field, "", None))
        proposed[-1]["category"] = category
        candidates.append(category)
    # Fill values from approved answers deterministically without exposing the
    # whole context bank in the session snapshot.
    proposed = []
    for field in fields:
        if field.get("is_submit"):
            continue
        category = clean_text(field.get("category") or infer_category(field), 80)
        answer = None
        # The review is generated from the field shape alone.  The extension's
        # fill decisions are recorded separately in `review_fields` when it
        # POSTs the final snapshot; this fallback still yields a safe card.
        proposed.append(_field_review(field, "", answer))
        proposed[-1]["category"] = category
    review_payload = {
        "session_id": session["session_id"],
        "job": session.get("job") or {},
        "url": session.get("url") or "",
        "provider": session.get("provider") or "generic",
        "page_fingerprint": session.get("page_fingerprint") or "",
        "fields": session.get("review_fields") or proposed,
        "blockers": session.get("blockers") or [],
    }
    digest = review_hash(review_payload)
    review = {
        **review_payload,
        "review_hash": digest,
        "nonce": secrets.token_urlsafe(24),
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=REVIEW_TTL_SECONDS)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "created_at": utc_now(),
    }
    session["review"] = review
    session["state"] = "awaiting_confirmation"
    return review


def prepare_review(root: Path, session_id: str, fields: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    store = load_store(root)
    session = store.get("sessions", {}).get(clean_text(session_id, 100))
    if not isinstance(session, dict):
        raise ValueError("application session not found")
    if fields is not None:
        session["review_fields"] = [
            _field_review(
                dict(field),
                field.get("value") or field.get("proposed_value") or "",
                field.get("answer") if isinstance(field.get("answer"), dict) else None,
            )
            for field in fields
            if isinstance(field, dict) and not field.get("is_submit")
        ]
    if session.get("blockers"):
        raise ValueError("application has unresolved blockers")
    review = _prepare_review(session)
    _save_session(root, store, session)
    return copy.deepcopy(review)


def save_answer(
    root: Path,
    question: str,
    value: str,
    category: str = "",
    reusable: bool = True,
    sensitive: Optional[bool] = None,
    answer_id: str = "",
    variants: Optional[Iterable[str]] = None,
    fallback_for: Optional[Iterable[str]] = None,
    select_all: bool = False,
    evidence_ids: Optional[Iterable[str]] = None,
    session_id: str = "",
) -> Dict[str, Any]:
    question = clean_text(question, QUESTION_LIMIT)
    value = clean_text(value, VALUE_LIMIT)
    if not question or not value:
        raise ValueError("question and answer are required")
    normalized = normalize_question(question)
    category = clean_text(category, 80) or "other"
    generated_id = hashlib.sha256(f"{category}:{normalized}".encode("utf-8")).hexdigest()[:24]
    answer_id = clean_text(answer_id, 100) or generated_id
    store = load_store(root)
    existing = store["context"]["answers"].get(answer_id) or {}
    answer = {
        "answer_id": answer_id,
        "question": question,
        "normalized_question": normalized,
        "variants": list(dict.fromkeys([clean_text(item, QUESTION_LIMIT) for item in (variants or []) if clean_text(item, QUESTION_LIMIT)] + list(existing.get("variants") or [])))[:30],
        "fallback_for": list(dict.fromkeys([clean_text(item, QUESTION_LIMIT) for item in (fallback_for or []) if clean_text(item, QUESTION_LIMIT)] + list(existing.get("fallback_for") or [])))[:20],
        "select_all": bool(select_all) or bool(existing.get("select_all")),
        "category": category,
        "value": value,
        "reusable": bool(reusable),
        "sensitive": bool(is_sensitive(category) if sensitive is None else sensitive),
        "evidence_ids": [clean_text(item, 140) for item in (evidence_ids or []) if clean_text(item, 140)][:20],
        "updated_at": utc_now(),
    }
    store["context"]["answers"][answer_id] = answer
    store["context"]["answers"] = dict(list(store["context"]["answers"].items())[-MAX_CONTEXT_ENTRIES:])
    if session_id:
        session = store["sessions"].get(clean_text(session_id, 100))
        if isinstance(session, dict):
            session["blockers"] = [
                item for item in session.get("blockers", [])
                if normalize_question(item.get("label")) != normalized
                and clean_text(item.get("category"), 80) != category
            ]
            session["state"] = "filling" if not session["blockers"] else "blocked"
            session["review"] = None
            session["review_fields"] = []
    write_store(root, store)
    write_context_markdown(root, store)
    return copy.deepcopy(answer)


def save_mapping(root: Path, field_key: str, answer_id: str) -> Dict[str, Any]:
    field_key = normalize_question(field_key)
    answer_id = clean_text(answer_id, 100)
    if not field_key or not answer_id:
        raise ValueError("field key and answer id are required")
    store = load_store(root)
    if answer_id not in store["context"]["answers"]:
        raise ValueError("answer not found")
    store["context"]["mappings"][field_key] = answer_id
    write_store(root, store)
    write_context_markdown(root, store)
    return {"field_key": field_key, "answer_id": answer_id}


def record_event(root: Path, session_id: str, state: str, message: str = "", error: str = "") -> Dict[str, Any]:
    state = clean_text(state, 40).lower()
    if state not in SESSION_STATES:
        raise ValueError("invalid application session state")
    store = load_store(root)
    session = store["sessions"].get(clean_text(session_id, 100))
    if not isinstance(session, dict):
        raise ValueError("application session not found")
    session["state"] = state
    session["last_message"] = clean_text(message, 1000)
    session["last_error"] = clean_text(error or (message if state == "failed" else ""), 1000)
    if state in TERMINAL_STATES:
        session["confirmation"] = None
        if state == "submitted":
            session["submitted_at"] = utc_now()
        session["review"] = None
    _save_session(root, store, session)
    return public_session(session)


def apply_confirmation(root: Path, session_id: str, review_hash_value: str, nonce: str, page_fingerprint: str = "") -> Dict[str, Any]:
    store = load_store(root)
    session = store["sessions"].get(clean_text(session_id, 100))
    if not isinstance(session, dict) or not isinstance(session.get("review"), dict):
        raise ValueError("no active review card")
    if session.get("confirmation") or session.get("state") == "submitting":
        raise ValueError("application review confirmation has already been consumed")
    review = session["review"]
    if clean_text(review.get("review_hash"), 100) != clean_text(review_hash_value, 100):
        raise ValueError("review card changed; reopen it before confirming")
    if not secrets.compare_digest(clean_text(review.get("nonce"), 100), clean_text(nonce, 100)):
        raise ValueError("review confirmation token is invalid")
    expires = timestamp(review.get("expires_at"))
    if expires is None or expires < dt.datetime.now(dt.timezone.utc).timestamp():
        raise ValueError("review confirmation expired")
    expected = clean_text(review.get("page_fingerprint"), 80)
    if page_fingerprint and expected and not secrets.compare_digest(expected, clean_text(page_fingerprint, 80)):
        raise ValueError("the application page changed; review it again")
    session["confirmation"] = {
        "review_hash": review["review_hash"],
        "page_fingerprint": expected,
        "approved_at": utc_now(),
        "expires_at": review["expires_at"],
    }
    session["state"] = "submitting"
    _save_session(root, store, session)
    return {"ok": True, "session_id": session["session_id"], "state": session["state"], "page_fingerprint": expected}


def verify_submission_page(root: Path, session_id: str, page_url: str, fields: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Check that a remotely approved review still describes the live page."""
    session = get_session(root, session_id)
    if not session or session.get("state") != "submitting":
        raise ValueError("this application is not approved for submission")
    confirmation = session.get("confirmation") or {}
    expected = clean_text(confirmation.get("page_fingerprint"), 80)
    live_fields = [field for field in fields if isinstance(field, dict)]
    actual = form_fingerprint(page_url, live_fields)
    if not expected or expected != actual:
        raise ValueError("the application page changed after confirmation")
    expected_values = {
        normalize_question(field.get("field_id") or field.get("label")): display_field_value(field.get("value"))
        for field in (session.get("review") or {}).get("fields", [])
        if isinstance(field, dict)
    }
    live_values = {
        normalize_question(field.get("field_id") or normalized_field_key(field)): display_field_value(field.get("value"))
        for field in live_fields
        if not field.get("is_submit")
    }
    for key, value in expected_values.items():
        if key and live_values.get(key) != value:
            raise ValueError("application field values changed after confirmation")
    expiry = timestamp(confirmation.get("expires_at"))
    if expiry is None or expiry < dt.datetime.now(dt.timezone.utc).timestamp():
        raise ValueError("submission confirmation expired")
    return {"ok": True, "session_id": session["session_id"], "page_fingerprint": actual}


def add_issue(
    root: Path,
    session_id: str,
    issue_type: str,
    message: str,
    field_label: str = "",
    page_url: str = "",
    fingerprint: str = "",
    selector_kind: str = "",
) -> Dict[str, Any]:
    store = load_store(root)
    issue = {
        "issue_id": uuid.uuid4().hex[:20],
        "session_id": clean_text(session_id, 100),
        "type": clean_text(issue_type, 80) or "unknown",
        "message": clean_text(message, 700),
        "field": clean_text(field_label, 300),
        "provider": provider_for_url(page_url),
        "page": urlparse(safe_url(page_url)).path[:240],
        "fingerprint": clean_text(fingerprint, 80),
        "selector_kind": clean_text(selector_kind, 80),
        "created_at": utc_now(),
        "status": "open",
    }
    store["issues"] = ([issue] + [item for item in store["issues"] if isinstance(item, dict)])[:MAX_ISSUES]
    write_store(root, store)
    return copy.deepcopy(issue)


def list_issues(root: Path, status: str = "") -> List[Dict[str, Any]]:
    store = load_store(root)
    rows = [item for item in store.get("issues", []) if isinstance(item, dict)]
    if status:
        rows = [item for item in rows if item.get("status") == status]
    return copy.deepcopy(rows)


def public_session(session: Dict[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(session)
    value.pop("last_form", None)
    value.pop("review_fields", None)
    # The review card is explicitly owner-facing and short-lived. It is
    # included here so the cloud mirror can show the exact proposed values.
    return value


def public_context(root: Path) -> Dict[str, Any]:
    store = load_store(root)
    answers = list(store.get("context", {}).get("answers", {}).values())
    answers.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {
        "version": APPLICATION_AGENT_VERSION,
        "updated_at": store.get("updated_at") or "",
        "answers": copy.deepcopy(answers[:MAX_CONTEXT_ENTRIES]),
        "mappings": copy.deepcopy(store.get("context", {}).get("mappings", {})),
        "markdown": str(markdown_path(root)),
    }


def public_sessions(root: Path) -> List[Dict[str, Any]]:
    store = load_store(root)
    rows = [public_session(item) for item in store.get("sessions", {}).values() if isinstance(item, dict)]
    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return rows[:MAX_SESSIONS]
