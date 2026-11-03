"""Deterministic, auditable new-grad and experience evidence.

This module deliberately separates required experience from preferred
experience. A posting can mention a positive number of years without making
it a hiring floor, and source provenance without readable posting evidence is
useful for discovery but not strong enough for an automatic alert.
"""
from __future__ import annotations

import re

from .config import profile

TRUSTED_NEW_GRAD_SOURCES = {
    "simplify", "vansh", "jobright", "jobright_pm", "speedyapply",
}

_WORD_YEARS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"

# Keep the mention itself permissive. Qualification is decided from the
# nearest wording around each mention below, rather than by one global regex
# that mistakes "preferred" for "required".
_EXPERIENCE_MENTION_RE = re.compile(
    rf"(?P<range>{_NUM}\s*(?:-|–|to)\s*{_NUM})\s+years?"
    rf"|(?P<plus>{_NUM}\s*\+\s*years?)"
    rf"|(?P<plain>{_NUM}\s+years?)",
    re.I,
)
_NUMBER_RE = re.compile(_NUM, re.I)
_PREFERRED_RE = re.compile(
    r"\b(preferred|preferable|nice\s+to\s+have|desired|bonus|plus|ideal|"
    r"recommended|would\s+be\s+a\s+plus|not\s+required)\b", re.I
)
_REQUIRED_RE = re.compile(
    r"\b(required|required\s+for|minimum|at\s+least|must\s+have|"
    r"need(?:s)?|necessary|essential|mandatory|requires?|seeking|"
    r"looking\s+for)\b", re.I
)
_REQUIRED_HEADING_RE = re.compile(
    r"\b(?:basic|minimum|required|must[- ]have|qualifications?|requirements?)\b",
    re.I,
)
_PREFERRED_HEADING_RE = re.compile(
    r"\b(?:preferred|desired|nice[- ]to[- ]have|bonus|plus)\b", re.I,
)
_EXPERIENCE_CONTEXT_RE = re.compile(
    r"\b(?:experience|professional|relevant|related|industry|software|"
    r"engineering|work|worked|working|hands[- ]on|internships?|role|field)\b|"
    r"\byears?\s+(?:in|as)\s+[a-z]",
    re.I,
)
_DEGREE_CONTEXT_RE = re.compile(
    r"\b(?:bachelor(?:'s|’s)?|master(?:'s|’s)?|ph\.?d|doctor(?:ate|al)|degree)\b",
    re.I,
)
_NON_EXPERIENCE_TIME_RE = re.compile(
    r"\b(?:after|before|within|over|for|since|during)\s*$|"
    r"\b(?:company|organization|team|business|brand)\s+"
    r"(?:has|have|with|boasts?)\b",
    re.I,
)
_NON_EXPERIENCE_DURATION_RE = re.compile(
    r"\byears?\s+in\s+(?:the\s+)?(?:business|operation(?:s)?|existence)\b|"
    r"\byears?\s+of\s+(?:company|business|operating)\b",
    re.I,
)
_NO_EXPERIENCE_RE = re.compile(
    r"\b(?:no|without)\s+(?:any\s+)?"
    r"(?:(?:prior|professional|relevant|work|minimum\s+of)\s+)*"
    r"experience\b", re.I
)

_STRONG_NEW_GRAD_RE = re.compile(
    r"\b(new\s*grad(?:uate)?|university\s+grad(?:uate)?|recent(?:ly)?\s+"
    r"grad(?:uate)?|early[- ]career|entry[- ]level|college\s+grad(?:uate)?|"
    r"graduate\s+(?:software|engineer|program|scheme)|rotational\s+program|"
    r"graduate\s+program(?:me)?|emerging\s+talent|future\s+talent)\b", re.I
)
_POSSIBLE_NEW_GRAD_RE = re.compile(
    r"\b(junior|jr\.?|associate|engineer\s+i|developer\s+i|"
    r"scientist\s+i|analyst\s+i)\b", re.I
)
_ENTRY_YEARS_RE = re.compile(r"\b0\s*[-–to ]+\s*[123]\s+years\b", re.I)
_COHORT_RE = re.compile(
    r"\b(?:class\s+of|new(?:\s+college)?\s+grad(?:uate)?|"
    r"university\s+grad(?:uate)?|recent(?:ly)?\s+grad(?:uate)?)\s*"
    r"(?:[:\-/]|of|for)?\s*(20\d{2})\b|"
    r"\b(20\d{2})\s+(?:new\s*grad(?:uate)?|new\s+college\s+grad(?:uate)?)\b",
    re.I,
)
_COHORT_CONTEXT_RE = re.compile(
    r"\b(?:class\s+of|new(?:\s+college)?\s+grad(?:uate)?|"
    r"university\s+grad(?:uate)?|recent(?:ly)?\s+grad(?:uate)?|"
    r"graduat(?:e|ing|ion)\s+(?:in|between|from|by)|cohort)\b"
    r"[^\n.]{0,50}", re.I,
)
_ALL_LEVELS_RE = re.compile(
    r"\b(?:all\s+(?:career\s+)?levels?|all\s+levels\s+of\s+experience|"
    r"early[- ]career\s+(?:and|or)\s+(?:experienced|senior)|"
    r"whether\s+you(?:'|’)re\s+early[- ]career\s+or\s+experienced)\b", re.I
)
_SENIOR_BODY_RE = re.compile(
    r"\b(?:experienced[- ]hire|senior\s+individual\s+contributor)\b|"
    r"\b(?:requires?|seeking|looking\s+for|this\s+is\s+(?:a|an)?|"
    r"the|a|an)\s+(?:senior|staff|principal|lead)[- ]level?\b|"
    r"\b(?:senior[- ]level|staff[- ]level|principal[- ]level|lead[- ]level)\s+"
    r"(?:[a-z]+\s+){0,3}(?:role|position|opening|opportunity|hire|candidate|"
    r"individual\s+contributor|engineer|developer|scientist)\b", re.I
)


def _number(value: str) -> int:
    value = value.lower()
    return int(value) if value.isdigit() else _WORD_YEARS[value]


def _mention_floor(match: re.Match[str]) -> int:
    raw = match.group("range") or match.group("plus") or match.group("plain") or ""
    number = _NUMBER_RE.search(raw)
    return _number(number.group(0)) if number else 0


def _is_experience_mention(text: str, match: re.Match[str]) -> bool:
    """Reject company-age/benefit-tenure years that are not job criteria."""
    start, end = match.span()
    boundaries = ".?!;|\n"
    clause_start = max([text.rfind(mark, 0, start) for mark in boundaries] + [-1]) + 1
    clause_end_candidates = [
        text.find(mark, end) for mark in boundaries if text.find(mark, end) >= 0
    ]
    clause_end = min(clause_end_candidates + [len(text)])
    prefix = text[clause_start:start]
    context = text[clause_start:clause_end]
    # A heading on the immediately preceding line can qualify the mention,
    # but ordinary prose from a prior sentence must not leak into context.
    line_end = text.rfind("\n", 0, start)
    if line_end >= 0:
        previous_start = text.rfind("\n", 0, line_end) + 1
        heading = text[previous_start:line_end].strip(" :-\t")
        if (heading and len(heading) <= 90
                and not re.search(r"[.?!;|]", heading)):
            context = heading + " " + context
    if (_NON_EXPERIENCE_TIME_RE.search(prefix)
            and not re.search(r"\bwork(?:ed|ing)?\s+for\s+$", prefix, re.I)):
        return False
    if _NON_EXPERIENCE_DURATION_RE.search(context):
        return False
    if _EXPERIENCE_CONTEXT_RE.search(context) or _DEGREE_CONTEXT_RE.search(context):
        # "Company has 30 years of experience" describes the employer, not
        # a candidate floor, even though it contains the word experience.
        if re.search(r"\b(?:company|organization|team|business|brand)\s+"
                     r"(?:has|have|with|boasts?)\b", context, re.I):
            return False
        return True
    # A bare number can still be a requirement when its local wording says
    # required/preferred/minimum. This handles "minimum 3 years" without
    # accepting prose such as "leading innovation for 30 years".
    return bool(_PREFERRED_RE.search(context) or _REQUIRED_RE.search(context))


def _nearest_qualifier(text: str, match: re.Match[str]) -> str | None:
    """Return the closest requirement qualifier around one mention."""
    start, end = match.span()
    boundaries = ".?!;|\n"
    window_start = max(
        [text.rfind(mark, 0, start) for mark in boundaries] + [-1]
    ) + 1
    next_boundaries = [
        text.find(mark, end) for mark in boundaries if text.find(mark, end) >= 0
    ]
    window_end = min(next_boundaries + [len(text)])
    window_start = max(window_start, start - 90)
    window_end = min(window_end, end + 90)
    window = text[window_start:window_end]
    candidates: list[tuple[int, str]] = []
    for found in _PREFERRED_RE.finditer(window):
        candidates.append((abs((window_start + found.start()) - start), "preferred"))
    for found in _REQUIRED_RE.finditer(window):
        candidates.append((abs((window_start + found.start()) - start), "required"))
    # Headings commonly put the qualifier on the line before the criterion:
    # ``Preferred Qualifications:\n3+ years ...``. Keep that newline as a
    # clause boundary but inspect the immediately preceding heading.
    line_end = text.rfind("\n", 0, start)
    if line_end >= 0:
        previous_end = line_end
        previous_start = text.rfind("\n", 0, previous_end) + 1
        heading = text[previous_start:previous_end].strip(" :-\t")
        if len(heading) <= 90:
            if _PREFERRED_HEADING_RE.search(heading):
                candidates.append((start - previous_start, "preferred"))
            elif _REQUIRED_HEADING_RE.search(heading):
                candidates.append((start - previous_start, "required"))
    if not candidates:
        return None
    # In "one year preferred; no experience required", the qualifier
    # immediately attached to the mention wins over a later clause.
    return min(candidates, key=lambda item: item[0])[1]


def _senior_role_language(text: str) -> bool:
    """Detect senior candidate requirements, not senior teammates/mentors."""
    for match in _SENIOR_BODY_RE.finditer(text):
        prefix = text[max(0, match.start() - 40):match.start()]
        if re.search(r"\b(?:by|with|from|alongside|under|mentored)\s+$", prefix, re.I):
            continue
        return True
    return False


def experience_evidence(text: str) -> dict:
    """Extract required/preferred experience floors and contradictions."""
    # Collapse horizontal whitespace while keeping line breaks meaningful for
    # Required/Preferred headings and clause boundaries.
    text = re.sub(r"[ \t\r\f\v]+", " ", text or "").strip()
    required: list[int] = []
    numeric_required: list[int] = []
    preferred: list[int] = []
    notes: list[str] = []

    if _NO_EXPERIENCE_RE.search(text):
        required.append(0)
        notes.append("no experience required")

    for match in _EXPERIENCE_MENTION_RE.finditer(text):
        if not _is_experience_mention(text, match):
            continue
        floor = _mention_floor(match)
        qualifier = _nearest_qualifier(text, match)
        if qualifier == "preferred":
            preferred.append(floor)
        else:
            required.append(floor)
            numeric_required.append(floor)
        notes.append(re.sub(r"\s+", " ", match.group(0)).strip())

    unique_required = sorted(set(required))
    alternative = bool(
        len(unique_required) > 1
        and re.search(r"\b(?:or|alternatively|either)\b", text, re.I)
        and _DEGREE_CONTEXT_RE.search(text)
    )
    # Degree/experience alternatives are conditional paths, not contradictory
    # floors. Do not pick the largest number for every candidate; retain the
    # posting as review-only until the candidate's degree path is resolved.
    conflict = len(unique_required) > 1 and not alternative
    required_floor = None if alternative else (max(unique_required) if unique_required else None)
    preferred_floor = max(preferred) if preferred else None
    if alternative:
        note = "alternative degree/experience paths: " + ", ".join(
            f"{value}+" for value in unique_required
        )
    elif conflict:
        note = "conflicting required experience floors: " + ", ".join(
            f"{value}+" for value in unique_required
        )
    elif required_floor is not None and preferred_floor is not None:
        note = f"requires {required_floor}+ yrs; preferred {preferred_floor}+ yrs"
    elif required_floor is not None:
        note = f"requires {required_floor}+ yrs"
    elif preferred_floor is not None:
        note = f"preferred experience {preferred_floor}+ yrs; no required floor"
    else:
        note = "no experience floor stated"

    return {
        "required_floor": required_floor,
        "required_floor_explicit": bool(numeric_required),
        "preferred_floor": preferred_floor,
        "conflict": conflict,
        "alternative": alternative,
        "note": note,
        "mentions": notes[:6],
    }


def _candidate_grad_year(candidate_grad_year: int | None = None) -> int | None:
    if candidate_grad_year is not None:
        return int(candidate_grad_year)
    value = profile().get("candidate", {}).get("grad_year")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def new_grad_evidence(
    title: str,
    description: str = "",
    *,
    source: str = "",
    source_board: str = "",
    source_only: bool = False,
    candidate_grad_year: int | None = None,
) -> dict:
    """Classify deterministic new-grad evidence without overclaiming."""
    title = (title or "").strip()
    description = (description or "").strip()
    body = description[:1800]
    candidate_year = _candidate_grad_year(candidate_grad_year)
    combined = f"{title}\n{body}"
    matches = list(_COHORT_RE.finditer(combined))
    cohort_years_set = {int(m.group(1) or m.group(2)) for m in matches}
    for context in _COHORT_CONTEXT_RE.findall(combined):
        cohort_years_set.update(int(year) for year in re.findall(r"20\d{2}", context))
    cohort_years = sorted(cohort_years_set)
    cohort_conflict = bool(
        candidate_year is not None
        and cohort_years
        and candidate_year not in cohort_years
    )

    title_cohort = bool(
        _COHORT_RE.search(title)
        or any(re.search(r"20\d{2}", context)
               for context in _COHORT_CONTEXT_RE.findall(title))
    )
    body_cohort = bool(
        any(re.search(r"20\d{2}", context)
            for context in _COHORT_CONTEXT_RE.findall(body))
    )
    title_match = _STRONG_NEW_GRAD_RE.search(title) or title_cohort
    body_match = _STRONG_NEW_GRAD_RE.search(body) or _ENTRY_YEARS_RE.search(body) or body_cohort
    possible_title = _POSSIBLE_NEW_GRAD_RE.search(title)
    all_levels = bool(_ALL_LEVELS_RE.search(body))
    contradictory = _senior_role_language(body)

    if cohort_conflict:
        return {
            "status": "review",
            "source": "cohort-conflict",
            "cohort_years": cohort_years,
            "cohort_conflict": True,
            "reason": (
                f"cohort {', '.join(map(str, cohort_years))} conflicts with "
                f"candidate class {candidate_year}"
            ),
        }
    if source_only:
        return {
            "status": "review",
            "source": "source-only",
            "cohort_years": cohort_years,
            "cohort_conflict": False,
            "reason": (
                "source-only lead without employer-backed posting evidence; "
                "title and notes are review-only"
            ),
        }
    if title_match and not all_levels and not contradictory:
        return {
            "status": "verified",
            "source": "title",
            "cohort_years": cohort_years,
            "cohort_conflict": False,
            "reason": (
                f"explicit new-grad/cohort wording in title: "
                f"{title_match.group(0) if hasattr(title_match, 'group') else 'cohort year'}"
            ),
        }
    if body_match and not all_levels and not contradictory:
        return {
            "status": "verified",
            "source": "description",
            "cohort_years": cohort_years,
            "cohort_conflict": False,
            "reason": (
                f"explicit new-grad/cohort wording in posting: "
                f"{body_match.group(0) if hasattr(body_match, 'group') else 'cohort year'}"
            ),
        }
    if contradictory:
        return {
            "status": "review",
            "source": "contradictory-posting",
            "cohort_years": cohort_years,
            "cohort_conflict": False,
            "reason": "posting combines early-career wording with senior-level language",
        }
    if possible_title:
        return {
            "status": "review",
            "source": "title-possible",
            "cohort_years": cohort_years,
            "cohort_conflict": False,
            "reason": f"junior/associate title is early-career possible: {possible_title.group(0)}",
        }
    source_name = (source or "").strip().lower()
    if (source_name in TRUSTED_NEW_GRAD_SOURCES
            or source_board.strip().lower().startswith("aggregator:")):
        return {
            "status": "review",
            "source": "source-only",
            "cohort_years": cohort_years,
            "cohort_conflict": False,
            "reason": "new-grad source provenance without explicit title/body evidence",
        }
    return {
        "status": "absent",
        "source": "none",
        "cohort_years": cohort_years,
        "cohort_conflict": False,
        "reason": "no explicit new-grad/early-career evidence",
    }


def compact_evidence(evidence: dict) -> dict:
    """Keep persisted evidence bounded and JSON-friendly."""
    keys = (
        "status", "source", "reason", "cohort_years", "cohort_conflict",
        "required_floor", "preferred_floor", "experience_conflict",
        "experience_alternative", "experience_note",
    )
    return {
        key: evidence[key]
        for key in keys
        if key in evidence and evidence[key] not in (None, [], "")
    }
