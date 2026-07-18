"""Culture Compass: per-company dossiers scored against the user's stated
criteria (prestige + pay + fast innovative culture + real WLB/shutdowns +
mission + growth, no burnout).

Sources, in trust order:
  1. data/culture_seed.yaml — human-curated (source: "seed")
  2. LLM-generated for companies that fire alerts and lack a dossier
     (source: "est." — always labeled, never silently mixed with curated data)

Store: state/culture.json  {norm_name: dossier}
Surfaces: docs/CULTURE.md table · alert-line tags · `culture <company>` command.
"""
from __future__ import annotations

import json
import re
import time

import yaml

from . import llm, state
from .config import DATA_DIR, DOCS_DIR, profile
from .models import norm

FIELDS = ("industry", "prestige", "pace", "wlb", "vibe", "pto", "shutdowns",
          "new_grad_tc", "rotational", "fit_notes")

_PRESTIGE_PTS = {"S": 25, "A": 19, "B": 11, "C": 4}
_PACE_PTS = {"fast": 20, "moderate": 12, "deliberate": 4}


def load() -> dict:
    return state.load("culture.json", {})


def save(dossiers: dict) -> None:
    state.save("culture.json", dossiers)


def sync_seed(dossiers: dict) -> int:
    """Merge the curated YAML into the store. Seed always wins over est."""
    with open(DATA_DIR / "culture_seed.yaml") as f:
        rows = yaml.safe_load(f)["companies"]
    added = 0
    for row in rows:
        k = norm(row["name"])
        existing = dossiers.get(k, {})
        if existing.get("source") == "seed" and existing == {**row, "source": "seed",
                                                             "fit": existing.get("fit")}:
            continue
        d = {field: row.get(field, "") for field in FIELDS}
        d["name"] = row["name"]
        d["source"] = "seed"
        d["fit"] = fit_score(d)
        if dossiers.get(k) != d:
            dossiers[k] = d
            added += 1
    return added


def _tc_dollars(tc: str) -> int | None:
    m = re.search(r"\$?\s*(\d{2,3})\s*k", str(tc), re.I)
    return int(m.group(1)) * 1000 if m else None


def fit_score(d: dict) -> int:
    """0-100 against the user's criteria. Deterministic and auditable:
    prestige 25 + wlb 25 + pace 20 + shutdowns 10 + comp 20, minus a burnout
    penalty — 'avoiding toxic or high-burnout environments' is a stated
    guardrail, so wlb <= 2 subtracts rather than merely scoring low."""
    pts = 0
    pts += _PRESTIGE_PTS.get(str(d.get("prestige", "")).strip().upper()[:1], 4)
    try:
        wlb = int(d.get("wlb") or 0)
    except (TypeError, ValueError):
        wlb = 0
    pts += max(0, min(5, wlb)) * 5
    if 0 < wlb <= 2:
        pts -= 15
    pts += _PACE_PTS.get(str(d.get("pace", "")).strip().lower(), 8)
    if str(d.get("shutdowns", "")).strip().upper().startswith("Y"):
        pts += 10
    tc = _tc_dollars(d.get("new_grad_tc", ""))
    if tc:
        pts += max(0, min(20, round((tc - 90000) / 6500)))
    return max(0, min(100, pts))


def dossier_for(company: str, dossiers: dict | None = None) -> dict | None:
    dossiers = dossiers if dossiers is not None else load()
    k = norm(company)
    if k in dossiers:
        return dossiers[k]
    # loose match: token overlap on multi-word names ("Uber Technologies, Inc." -> "Uber")
    words = set(k.split())
    for dk, d in dossiers.items():
        if set(dk.split()) & words and (dk in k or k in dk or dk.split()[0] == k.split()[0]):
            return d
    return None


def alert_tag(company: str, dossiers: dict | None = None) -> str:
    d = dossier_for(company, dossiers)
    if not d:
        return ""
    return f"`fit {d.get('fit', '?')}` `wlb {d.get('wlb', '?')}/5·{d.get('pace', '?')}`"


# ---------- LLM enrichment (runs on the Mac companion or with any provider) ----------

_GEN_PROMPT = """You are building a factual culture dossier for a graduating CS senior
evaluating employers. Their criteria: {criteria}

Company: {company}

Reply with ONLY a JSON object with exactly these keys:
industry (short), prestige (one of S/A/B/C for AI-DS resume value),
pace (fast/moderate/deliberate), wlb (integer 1-5), vibe (<=8 words),
pto (days or 'unlimited'), shutdowns ('Y: <type>' or 'N'),
new_grad_tc (like '$150k (est.)'), rotational (program name or 'N'),
fit_notes (<=25 words, honest, referencing their criteria).
Use conservative estimates and mark uncertain numbers with (est.)."""


def enrich_missing(limit: int = 8) -> int:
    """Generate dossiers for recently-alerted companies that lack one."""
    if not llm.available() or limit <= 0:
        return 0
    dossiers = load()
    alert_history = state.load("alert_history.json", [])
    queue, seen = [], set()
    for a in reversed(alert_history):  # newest first
        k = norm(a["company"])
        if k in seen or dossier_for(a["company"], dossiers):
            continue
        seen.add(k)
        queue.append(a["company"])
        if len(queue) >= limit:
            break
    criteria = profile().get("culture_criteria", "")
    made = 0
    for company in queue:
        text = llm.complete(_GEN_PROMPT.format(criteria=criteria, company=company),
                            max_tokens=350, json_mode=True, task="company_research")
        if not text:
            continue
        try:
            start, end = text.index("{"), text.rindex("}") + 1
            row = json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            continue
        d = {field: row.get(field, "") for field in FIELDS}
        d["name"] = company
        d["source"] = "est."
        d["generated_at"] = int(time.time())
        d["fit"] = fit_score(d)
        dossiers[norm(company)] = d
        made += 1
    if made:
        save(dossiers)
    return made


# ---------- rendering ----------

def render_md(dossiers: dict) -> str:
    rows = sorted(dossiers.values(), key=lambda d: -(d.get("fit") or 0))
    lines = [
        "# 🧭 Culture Compass",
        "",
        "_Scored 0–100 against your criteria: elite AI/DS prestige + new-grad pay + "
        "fast innovative culture + real work-life balance (sane hours, shutdowns) + "
        "mission + growth, no burnout. `seed` = human-curated · `est.` = "
        "LLM-generated estimate._",
        "",
        "| Fit | Company | Industry | Prestige | Pace | WLB | Culture vibe | PTO | "
        "Shutdowns | New-grad TC | Rotational | Src |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in rows:
        lines.append(
            f"| **{d.get('fit', '?')}** | {d.get('name', '?')} | {d.get('industry', '')} "
            f"| {d.get('prestige', '')} | {d.get('pace', '')} | {d.get('wlb', '')}/5 "
            f"| {d.get('vibe', '')} | {d.get('pto', '')} | {d.get('shutdowns', '')} "
            f"| {d.get('new_grad_tc', '')} | {d.get('rotational', '')} | {d.get('source', '')} |")
    lines += ["", f"_{len(rows)} dossiers. Ask for any company with a "
              "`culture <company>` comment on an alert issue._"]
    return "\n".join(lines) + "\n"


def render_dossier_reply(d: dict) -> str:
    return (f"### 🧭 {d.get('name')} — culture fit **{d.get('fit', '?')}/100** ({d.get('source')})\n\n"
            f"| | |\n|---|---|\n"
            f"| Industry | {d.get('industry', '—')} |\n"
            f"| AI/DS prestige | {d.get('prestige', '—')} |\n"
            f"| Pace | {d.get('pace', '—')} |\n"
            f"| Work-life balance | {d.get('wlb', '—')}/5 |\n"
            f"| Vibe | {d.get('vibe', '—')} |\n"
            f"| PTO | {d.get('pto', '—')} |\n"
            f"| Shutdowns | {d.get('shutdowns', '—')} |\n"
            f"| New-grad TC | {d.get('new_grad_tc', '—')} |\n"
            f"| Rotational program | {d.get('rotational', '—')} |\n\n"
            f"**Fit notes:** {d.get('fit_notes', '—')}")


def write_outputs() -> None:
    dossiers = load()
    sync_seed(dossiers)
    save(dossiers)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "CULTURE.md").write_text(render_md(dossiers))
