"""Optional LLM layer: semantic re-rank of borderline jobs + one-line
application angle for alerted jobs. Activates when any LLM provider is
configured (Anthropic key, local Ollama, or an OpenAI-compatible endpoint —
see radar/llm.py); every failure degrades silently to heuristic-only behavior.
"""
from __future__ import annotations

import json

from . import llm
from .config import profile


def rerank(jobs: list) -> None:
    """Adjust scores of borderline jobs in place; annotate with an angle note."""
    if not llm.available() or not jobs:
        return
    p = profile()
    batch = jobs[:30]
    rows = [{"id": j.id, "company": j.company, "title": j.title,
             "location": j.primary_location, "sector": j.sector,
             "desc": j.description[:400]} for j in batch]
    prompt = (
        "You are ranking job postings for this candidate:\n"
        f"{p['candidate']['narrative']}\n\n"
        "For each posting, return fit 0-10 (10 = apply immediately) considering "
        "role type, sector priority (healthtech > big tech/AI labs > edtech > other), "
        "new-grad suitability, and growth. Also a <=12 word 'angle' for their application.\n"
        f"Postings:\n{json.dumps(rows, ensure_ascii=False)}\n\n"
        'Reply with ONLY a JSON array: [{"id": "...", "fit": 7, "angle": "..."}]')
    text = llm.complete(prompt)
    if not text:
        return
    try:
        start, end = text.index("["), text.rindex("]") + 1
        results = {r["id"]: r for r in json.loads(text[start:end])}
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"brief: could not parse LLM response: {e}")
        return
    for j in batch:
        r = results.get(j.id)
        if not r:
            continue
        try:
            adj = round((float(r.get("fit", 5)) - 5) * 3)
        except (TypeError, ValueError):
            continue
        if adj:
            j.score = max(0, min(100, j.score + adj))
            j.score_reasons.append(f"llm rerank {'+' if adj > 0 else ''}{adj}")
        angle = (r.get("angle") or "").strip()
        if angle:
            j.llm_note = angle[:120]
