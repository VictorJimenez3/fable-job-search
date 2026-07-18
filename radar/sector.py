"""Sector inference: seed registry first, then lexicon over the company name."""
from __future__ import annotations

from .models import norm

KNOWN_SECTORS = {
    "dow": "chemicals_materials", "dupont": "chemicals_materials",
    "3m": "chemicals_materials", "basf": "chemicals_materials",
    "ecolab": "chemicals_materials", "albemarle": "chemicals_materials",
    "covestro": "chemicals_materials", "air products": "chemicals_materials",
    "chevron": "energy", "shell": "energy", "exxonmobil": "energy",
    "bp": "energy", "baker hughes": "energy", "halliburton": "energy",
    "johnson johnson": "pharma_biotech", "merck": "pharma_biotech",
    "pfizer": "pharma_biotech", "eli lilly": "pharma_biotech",
    "bristol myers squibb": "pharma_biotech", "gilead": "pharma_biotech",
    "amgen": "pharma_biotech", "gsk": "pharma_biotech",
    "novartis": "pharma_biotech", "moderna": "pharma_biotech",
    "thermo fisher scientific": "pharma_biotech",
    "micron": "semiconductors", "micron technology": "semiconductors",
    "applied materials": "semiconductors", "globalfoundries": "semiconductors",
    "intel": "semiconductors", "kla": "semiconductors", "asml": "semiconductors",
    "tesla": "consumer_manufacturing", "procter gamble": "consumer_manufacturing",
    "colgate palmolive": "consumer_manufacturing", "unilever": "consumer_manufacturing",
    "veolia": "environmental", "suez": "environmental",
    "jacobs": "engineering_consulting", "aecom": "engineering_consulting",
}

LEXICONS: dict[str, list[str]] = {
    "chemicals_materials": [
        "chemical", "chem", "material", "polymer", "coating", "resin", "specialty",
        "industrial gas", "minerals", "lithium", "carbon",
    ],
    "pharma_biotech": [
        "pharma", "biotech", "bio", "therapeut", "life sciences", "medic", "health",
        "genom", "vaccine", "diagnostic",
    ],
    "energy": ["energy", "petroleum", "oil", "gas", "refining", "power", "solar"],
    "semiconductors": ["semiconductor", "microelectronics", "wafer", "chip"],
    "consumer_manufacturing": [
        "manufacturing", "foods", "food", "beverage", "consumer", "automotive", "motors",
    ],
    "environmental": ["environment", "water", "waste", "sustainab", "ecology"],
    "engineering_consulting": ["engineering group", "engineering services", "consulting"],
}

# Lexicon order follows the profile's ChemE sector priorities.
_ORDER = ["chemicals_materials", "pharma_biotech", "energy", "semiconductors",
          "consumer_manufacturing", "environmental", "engineering_consulting"]


def infer(company: str, seed_sectors: dict[str, str]) -> str:
    n = norm(company)
    if not n:
        return "other"
    if n in seed_sectors:
        return seed_sectors[n]
    compact = n.replace(" ", "")
    for company, sector in KNOWN_SECTORS.items():
        if n == company or n.startswith(company + " ") or compact == company.replace(" ", ""):
            return sector
    words = n.split()
    padded = f" {n} "
    deny = {"media", "medium", "comedy", "academic"}  # false-positive stems
    for sector in _ORDER:
        for kw in LEXICONS[sector]:
            kw = kw.strip()
            if not kw:
                continue
            if len(kw) <= 3:
                # short stems only match whole words / word prefixes
                if any(w == kw or (len(kw) == 3 and w.startswith(kw) and w not in deny)
                       for w in words):
                    return sector
            elif kw in padded and not any(d in words for d in deny):
                return sector
    return "other"
