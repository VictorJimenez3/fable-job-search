# AI setup runbook — turn the radar's LLM layer ON

**For: Victor + any local coding-agent session on the Mac.** Written 2026-07-17
and operationally rechecked 2026-07-18. Everything
deterministic already runs without AI (gates, scoring, posting scraping,
sponsorship/years extraction, alerts). This doc is the checklist to light up
the AI layer, which is currently **fully OFF in the cloud** — verified from
live workflow env + state: `ANTHROPIC_API_KEY` is empty in Actions, 0 of
the 11,010 records have an `llm_note`, and quality verdicts currently come from
the Mac companion rather than Actions.

## What turns on when a provider exists

| Feature | Where it runs | What you get |
|---|---|---|
| Re-rank + application angle (`brief.rerank`) | every crawl (needs `ANTHROPIC_API_KEY`) | borderline jobs re-ordered semantically; each alert gets a one-line "angle" note |
| Quality verdicts (`quality.verify`) | `enrich` (nightly CI or Mac 2h cycle) | LLM confirms new-grad / role-family from real posting text; wrong roles demoted |
| Pasted-JD grading (`quality.verify_pasted`) | `enrich` | JDs pasted into the platform's Role-fit tab get a verdict |
| Culture dossiers (`culture.enrich_missing`) | `enrich` | industry/prestige/wlb/vibe/fit per company |
| Company scout (`discovery.llm_scout`) | `enrich`, weekly | employers aggregators miss (the WHOOP class) |

Provider resolution (`radar/llm.py`): `ANTHROPIC_API_KEY` → Anthropic
(model `claude-haiku-4-5` from profile.yaml); else `LLM_BASE_URL` → any
OpenAI-compatible endpoint (`LLM_API_KEY`, `LLM_MODEL`); else heuristics
only. Free-tier rate limits are handled — `llm.complete()` retries
429/500/503 honoring `Retry-After`.

## Step 1 — create ONE free key (Victor, ~3 minutes)

Pick one:

**Option A · NVIDIA NIM (free, hosted open models)**
1. Sign in at https://build.nvidia.com → Get API Key → copy the `nvapi-…` key.
2. Secrets to add:
   - `LLM_BASE_URL` = `https://integrate.api.nvidia.com/v1`
   - `LLM_API_KEY` = `nvapi-…`
   - `LLM_MODEL` = `meta/llama-3.3-70b-instruct` (any hosted instruct model works)

**Option B · Google AI Studio (free Gemini tier)**
1. https://aistudio.google.com/apikey → Create API key.
2. Secrets:
   - `LLM_BASE_URL` = `https://generativelanguage.googleapis.com/v1beta/openai`
   - `LLM_API_KEY` = the AI Studio key
   - `LLM_MODEL` = `gemini-2.5-flash`

**Option C · Anthropic (paid, best quality, also enables crawl-time rerank)**
- Single secret: `ANTHROPIC_API_KEY`. (A/B only power `enrich`; the crawl's
  rerank path currently reads only the Anthropic key.)

## Step 2 — add the secrets

GitHub → repo **Settings → Secrets and variables → Actions → New repository
secret**. `enrich.yml` already wires all four names — no workflow edits
needed.

## Step 3 — verify (Victor or a local coding agent)

1. Actions → **enrich** → Run workflow (branch: the default radar branch).
2. In the run log expect: `enrich: generated N culture dossier(s) via
   openai-compatible` (or `anthropic`), `quality pass … verified N new
   job(s)` — and NOT "no LLM provider configured".
3. Next day, `state/jobs.json` should show fresh `quality.checked_at`
   timestamps and (option C) `llm_note` strings appearing after crawls.
4. Optional once verified: bump `enrich.yml` cron from nightly
   (`20 7 * * *`) to every 6 h.

## Mac companion health check (the free local path)

The Mac does the same enrich cycle with Ollama whenever it's awake — no key
needed. It produced a successful enrichment commit on 2026-07-18; use these
checks if it stops advancing state:

```bash
launchctl list | grep jobradar          # com.jobradar.enrich loaded?
tail -20 ~/.jobradar/logs/enrich.log    # last cycle + errors
ollama ps                               # should be EMPTY between cycles
```

If the launchd job is gone, re-run `scripts/mac-companion/install.sh`.
Cloud key and Mac coexist fine — verdicts are cached per job (`jd_sha` /
attempt caps mean no double spend), and `merge_state.py` reconciles pushes.

## For a local coding-agent session — read first, then next steps

Read order: `AGENTS.md` → `CLAUDE.md` (legacy name, shared rules) →
`README.md` → `DECISIONS.md` (#30–#35 are the current era) →
`docs/CLI_HANDOFF.md` → `profile.yaml`. House rules that bind you:
deterministic scoring with reasons strings; LLM adjusts, never deletes;
demote-don't-delete; never scrape LinkedIn; `docs/platform/index.html`
stays a byte copy of `webapp/index.html`; `git pull --rebase` before every
push (CI commits every ~30 min).

After the key works, the queued AI work in priority order:
1. **Watch one enrich cycle end-to-end** — marquee suppression now applies
   (DECISIONS #31), so expect some Anthropic/OpenAI demotions; sanity-check
   a few verdicts against the actual postings.
2. **Pasted-JD flow** — paste one JD in the platform's Role-fit tab, run
   enrich, confirm the verdict lands with `source: "pasted"`.
3. **CV auto-tailoring** — ON HOLD until Victor fleshes out
   `CV/cv_full.tex` (Mac-only, never committed — DECISIONS #29).
4. **Posting ↔ profile RAG** (ROADMAP, parked): embedding similarity
   between scraped posting text (now accumulating in the quality pass) and
   CV/profile bullets as a ranking signal — local-first via Ollama
   embeddings (e.g. `nomic-embed-text`), similarity logged as a reason
   line to stay auditable. Wait until a few weeks of scraped text exists.
5. Cosmetic: `python -m radar.main repair-feedback` once (stopworded taste
   tokens are already inert at read time; this just cleans the file).
