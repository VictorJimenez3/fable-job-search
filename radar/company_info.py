"""Human-readable employer context for alert issues and notification emails.

This metadata is explanatory only: it never changes ranking.  It prevents a
job at an unfamiliar employer from being reduced to an unhelpful ``other``
label while keeping claims concise and auditable.
"""
from __future__ import annotations

from .culture import dossier_for
from .company_research import claim_text, dossier_for as research_for
from .models import norm


KNOWN: dict[str, tuple[str, str]] = {
    "anduril": ("defense technology", "autonomous defense systems"),
    "lockheed martin": ("defense & aerospace", "defense and space systems"),
    "mantech": ("defense contractor", "technology services for U.S. government"),
    "shield ai": ("defense technology", "autonomy for aircraft and defense"),
    "palantir": ("data / defense technology", "data platforms for government and industry"),
    "spacex": ("space & aerospace", "rockets, satellites, and space systems"),
    "northrop grumman": ("defense & aerospace", "defense and aerospace systems"),
    "rtx": ("defense & aerospace", "aerospace and defense systems"),
    "cvs health": ("healthcare / insurance", "pharmacy, insurance, and care services"),
    "carefirst bluecross blueshield": ("health insurance", "health plans and care services"),
    "northwell health": ("health system", "hospitals and clinical care"),
    "medtronic": ("medical devices", "medical devices and therapy technology"),
    "cadence": ("semiconductors", "electronic-design automation software"),
    "nvidia": ("semiconductors / AI", "AI chips and accelerated computing"),
    "openai": ("AI lab", "generative AI models and products"),
    "anthropic": ("AI lab", "AI models and safety research"),
    "notion": ("productivity software", "collaborative workspace software"),
    "capital one": ("financial services", "banking and consumer finance"),
    "jpmorgan chase": ("financial services", "banking and investment services"),
}

SECTOR_LABELS = {
    "healthtech": "health technology",
    "ai_lab": "AI / ML",
    "big_tech": "technology",
    "edtech": "education technology",
    "fintech": "financial technology",
}


def context(company: str, sector: str = "", dossiers: dict | None = None) -> tuple[str, str]:
    """Return ``(industry, what_the_company_does)`` without an ``other`` bucket."""
    key = norm(company)
    if key in KNOWN:
        return KNOWN[key]

    research = research_for(company)
    if research and research.get("status") == "ready":
        summary = claim_text(research, "summary")
        if summary != "Not confirmed":
            industry = claim_text(research, "business_model")
            if industry == "Not confirmed":
                industry = SECTOR_LABELS.get(sector, "technology")
            return industry, summary

    dossier = dossier_for(company, dossiers)
    if dossier and dossier.get("industry"):
        return str(dossier["industry"]), "company profile available in Culture Compass"

    words = set(key.split())
    if words & {"defense", "aerospace", "aircraft", "missile", "space"}:
        return "defense & aerospace", "defense, aerospace, or space technology"
    if words & {"health", "medical", "hospital", "care", "clinic"}:
        return "healthcare", "healthcare products or services"
    if words & {"bank", "capital", "finance", "financial", "insurance"}:
        return "financial services", "financial products or services"
    return SECTOR_LABELS.get(sector, "general technology"), "industry context not yet profiled"
