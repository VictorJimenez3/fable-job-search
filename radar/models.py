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
    locations: list[str] = field(default_factory=list)
    posted_at: int | None = None     # unix epoch seconds, None if unknown
    description: str = ""            # plain text, may be empty; trimmed to 4000 chars
    salary: str = ""
    remote: bool = False
    ats: str = ""                    # which ATS the canonical URL lives on
    sector: str = ""                 # filled by sector inference
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    alert_ok: bool = False           # passed gates AND internship-eligible
    llm_note: str = ""
    posting: dict = field(default_factory=dict)  # scraped-text analysis (radar/posting.py)

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
        return d
