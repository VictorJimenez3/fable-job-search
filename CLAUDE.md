# CLAUDE.md — read this first, every session

This repo IS the database and the shared brain across CLIs (Claude on the
cloud, Claude/Codex on the Mac). The docs below are extensive and current —
**use them, then update them.** Do not re-derive what a doc already answers,
and do not create a new doc when an existing one owns the topic.

## Read before changing anything
1. `README.md` — purpose + operating flow
2. `DECISIONS.md` — every deliberate choice, newest era is #30–#43
3. `docs/CLI_HANDOFF.md` — operational facts, current state, "next up"
4. `profile.yaml` — Victor's search preferences and thresholds
5. the specific `radar/` module + its `tests/` file

## Docs are part of the change, not an afterthought (MANDATORY)
Every material change updates the docs in the same commit. This is not
optional and it is not a separate task — a change isn't done until its docs
are. Match the change to the doc that owns it:

| You changed… | Update… |
|---|---|
| a non-obvious product/architecture choice, or a recall↔precision trade | **`DECISIONS.md`** — a new dated, numbered entry (append-only; amend by adding, never rewrite old entries) |
| user-facing setup, commands, sources, delivery, or behavior | `README.md` and, if it changes how Victor *uses* the platform, `docs/TUTORIAL.md` |
| shipped or reprioritized a roadmap item | `ROADMAP.md` (mark ✅ SHIPPED with the DECISIONS #, or ⏸️ ON HOLD / parked with a reason) |
| operational facts, new env knobs/commands, or the "next up" queue | `docs/CLI_HANDOFF.md` |
| candidate preferences or ranking policy only | `profile.yaml` (never implementation behavior) |
| scoring gates, source parsing, state shape, or output | add/adjust `tests/` |

Topic-specific runbooks that already exist — extend these, don't fork them:
`docs/AI_SETUP.md` (LLM enablement), `docs/FORKING.md`, `docs/SHPE.md`,
`docs/CULTURE.md`. `docs/DASHBOARD.md`, `docs/feed.xml`, `docs/platform/`,
and `state/*.json` are **generated** — never hand-edit except a documented
repair.

## Cross-CLI continuity contract (MANDATORY)

The repository, not the conversation, is the source of truth. Every CLI or
model session must leave enough written context for the next one to continue
without scrolling through chat:

1. Before work, fetch/rebase as appropriate, read the required docs above, and
   inspect the current branch/status. Treat existing edits and CI-generated
   state as potentially belonging to another CLI.
2. During work, update the owning document as soon as a decision, scope change,
   operational fact, TODO reprioritization, or user-facing behavior becomes
   real. Do not keep a new requirement only in a chat message.
3. Before handoff, update `docs/CLI_HANDOFF.md` with the verified current
   state, what changed, what remains, blockers/human steps, and the next
   concrete task. Add a dated numbered entry to `DECISIONS.md` for non-obvious
   tradeoffs; update `ROADMAP.md` for backlog/order changes.
4. End the session with a compact handoff: files changed, validation run and
   result, deployment/commit status, and any secrets or GitHub-side actions
   still required. If nothing changed, record that the audit was read-only.

Small/local models may handle documentation, audits, tests, and mechanical
edits when the acceptance criteria are already explicit. They must not invent
ranking policy, alter secrets/CV material, hand-edit generated state, or leave
architecture decisions undocumented. Stronger-model and human-approval lanes
are defined in `ROADMAP.md` and `docs/CLI_HANDOFF.md`.

## House rules (from DECISIONS — violate none)
- Scoring is deterministic and auditable: every point/demotion appends a
  reason string. The LLM only *adjusts*; it never deletes. Demote to the
  dashboard, never drop, for anything Victor might still act on.
- No servers, no required secrets: GitHub Actions cron is the runtime; the
  crawl and all deterministic analysis (gates, scoring, posting scraping in
  `radar/posting.py`) run with zero keys. AI is optional (`docs/AI_SETUP.md`).
- Never scrape LinkedIn — construct search URLs only (#16).
- `docs/platform/index.html` must stay a byte copy of `webapp/index.html`
  (`cp webapp/index.html docs/platform/index.html` after any webapp edit).
- Marquee list is duplicated in `profile.yaml` and `webapp` `S.marquee` —
  keep both in sync.

## Working conventions
- `git pull --rebase` before every push — CI commits state every ~30 min.
- Run `python -m pytest tests/` before pushing; keep it green.
- End a session by stating: what changed & why, files touched, validation
  run + result, and any secret/config still required (the CLI_HANDOFF
  handoff contract).

## Where things live (map)
- `radar/main.py` — pipeline entry (`crawl`, `enrich`, `regate`, …)
- `radar/score.py` — gates + scoring + `regate` (rules version stamp)
- `radar/posting.py` — deterministic sponsorship/years scraping (no LLM)
- `radar/quality.py` — LLM verdicts + SPA-host fetch + pasted-JD grading
- `radar/llm.py` — provider abstraction (Anthropic / OpenAI-compatible)
- `radar/sources/` — aggregators + ATS fetchers
- `webapp/index.html` — the platform (mirror: `docs/platform/index.html`)
