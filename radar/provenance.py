"""Human-readable provenance for every job discovery path."""
from __future__ import annotations

SOURCE_INFO = {
    "simplify": ("SimplifyJobs New-Grad-Positions", "https://github.com/SimplifyJobs/New-Grad-Positions"),
    "vansh": ("Vansh New-Grad-2026", "https://github.com/vanshb03/New-Grad-2026"),
    "jobright": ("Jobright new-grad GitHub board", "https://github.com/jobright-ai/2026-Software-Engineer-New-Grad"),
    "speedyapply": ("SpeedyApply college jobs", "https://github.com/speedyapply/2027-SWE-College-Jobs"),
    "zapply": ("Zapply new-grad data science / ML board", "https://github.com/zapplyjobs/New-Grad-Data-Science-Jobs-2027"),
    "hn": ("Hacker News Who Is Hiring", "https://news.ycombinator.com/item?id=40789211"),
}


def info(source: str, fallback_url: str = "") -> tuple[str, str]:
    """Return (label, link), preferring the actual discovery source."""
    return SOURCE_INFO.get(source, ("Direct company / ATS monitoring", fallback_url))
