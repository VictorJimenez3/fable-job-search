"""Core data model. A Job is the normalized unit that flows through the pipeline."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_STATE_ABBREV = {
    "california": "ca", "new york": "ny", "washington": "wa", "texas": "tx",
    "massachusetts": "ma", "illinois": "il", "colorado": "co", "georgia": "ga",
    "florida": "fl", "virginia": "va", "pennsylvania": "pa", "oregon": "or",
    "north carolina": "nc", "united states": "us", "usa": "us",
}


def _loc_key(location: str) -> str:
    """Canonicalize a location for identity: city-level, remote collapsed."""
    if "remote" in location.lower():
        return "remote"
    city = norm(location.split(",")[0]).strip()
    return _STATE_ABBREV.get(city, city)[:24]


def job_id(company: str, title: str, location: str) -> str:
    raw = f"{norm(company)}|{norm(title)}|{_loc_key(location)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class Job:
    company: str
    title: str
    url: str
    source: str                      # simplify | vansh | jobright | speedyapply | greenhouse | ...
    source_url: str = ""             # board/feed that surfaced this posting
    locations: list[str] = field(default_factory=list)
    posted_at: int | None = None     # unix epoch seconds, None if unknown
    description: str = ""            # plain text, may be empty; trimmed to 4000 chars
    salary: str = ""
    remote: bool = False
    ats: str = ""                    # which ATS the canonical URL lives on
    sector: str = ""                 # filled by sector inference
    score: int = 0
    score_raw: float = 0.0            # uncapped utility before display calibration
    score_calibrated: int = 0         # pre-verdict calibrated score
    ranking_adjustment: int = 0       # deterministic diversity adjustment after group ranking
    score_dimensions: dict = field(default_factory=dict)
    score_reasons: list[str] = field(default_factory=list)
    alert_ok: bool = False           # passed gates AND new-grad-eligible
    llm_note: str = ""
    posting: dict = field(default_factory=dict)  # scraped-text analysis (radar/posting.py)
    profile: str = "new_grad"
    internship_eligibility: dict = field(default_factory=dict)
    # Posting lifecycle is separate from application stage. Terminal records
    # stay in state for timeline analysis while the active platform hides them.
    posting_status: str = "open"
    posting_status_changed_at: int | None = None
    posting_status_reason: str = ""
    closed_at: int | None = None
    last_closed_at: int | None = None
    last_seen_at: int | None = None
    lifecycle_checked_at: int | None = None
    lifecycle_events: list[dict] = field(default_factory=list)

    @property
    def id(self) -> str:
        return job_id(self.company, self.title, self.primary_location)

    @property
    def primary_location(self) -> str:
        return self.locations[0] if self.locations else ""

    def to_record(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        d["description"] = ""  # never persist descriptions; keeps state small
        if not d["posting"]:
            del d["posting"]   # only persist the key when analysis exists
        if not d["internship_eligibility"]:
            del d["internship_eligibility"]
        for key in ("posting_status_changed_at", "posting_status_reason", "closed_at",
                    "last_closed_at", "last_seen_at", "lifecycle_checked_at",
                    "lifecycle_events"):
            if d.get(key) in (None, "", []):
                d.pop(key, None)
        return d
