"""Sector inference: seed registry first, then lexicon over the company name."""
from __future__ import annotations

from .models import norm

BIG_TECH = {
    "google", "alphabet", "meta", "apple", "amazon", "aws", "microsoft", "netflix",
    "nvidia", "adobe", "salesforce", "oracle", "ibm", "intel", "amd", "qualcomm",
    "uber", "lyft", "airbnb", "stripe", "linkedin", "tiktok", "bytedance", "snap",
    "pinterest", "roblox", "datadog", "cloudflare", "mongodb", "snowflake", "twilio",
    "reddit", "discord", "figma", "atlassian", "dropbox", "block", "square", "paypal",
    "tesla", "spacex", "palantir", "servicenow", "workday", "intuit", "ebay", "shopify",
}

SPORTS = {
    "nike", "espn", "fanatics", "sportradar", "draftkings", "under armour",
    "mlb", "nba", "nfl", "nhl", "formula 1", "fifa", "pga tour",
}
VIDEO_GAMES = {
    "playstation", "sony interactive entertainment", "sony", "nintendo", "xbox",
    "electronic arts", "ea games", "epic games", "take two interactive", "valve",
    "activision", "blizzard", "riot games", "ubisoft", "bungie", "gameloft",
}

LEXICONS: dict[str, list[str]] = {
    "healthtech": [
        "health", "med", "bio", "care", "clinic", "pharma", "patient", "dental",
        "genom", "thera", "onco", "vet", "nurse", "hospital", "diagnost", "surgic",
        "cardio", "derm", "radiol", "epidem", "vaccin", " rx", "wellness", "fitness",
    ],
    "edtech": ["edu", "learn", "academy", "school", "tutor", "class", "student", "course"],
    "fintech": [
        "bank", "fintech", "pay", "capital", "invest", "trading", "finance",
        "credit", "insur", "wealth", "asset", "ledger", "treasury",
    ],
    "ai_lab": ["ai", "ml", "intelligence", "neural", "deep", "cognit", "robot"],
    "sports": ["sports", "athletic", "stadium", "league", "soccer", "baseball", "basketball"],
    "video_games": ["game", "gaming", "playstation", "xbox", "nintendo", "esports"],
}

# lexicon order matters: healthtech wins ties (user priority)
_ORDER = ["healthtech", "sports", "video_games", "edtech", "fintech", "ai_lab"]


def infer(company: str, seed_sectors: dict[str, str]) -> str:
    n = norm(company)
    if not n:
        return "other"
    if n in seed_sectors:
        return seed_sectors[n]
    compact = n.replace(" ", "")
    for bt in BIG_TECH:
        if n == bt or n.startswith(bt + " ") or compact == bt:
            return "big_tech"
    for name in SPORTS:
        if n == name or n.startswith(name + " "):
            return "sports"
    for name in VIDEO_GAMES:
        if n == name or n.startswith(name + " "):
            return "video_games"
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
