# Roadmap — designed, deliberately deferred

The dream-system backlog. Each item is scoped enough to build on request;
none block anything currently running.

## North star: a platform anyone can log into (direction, 2026-07-13)

Today the multi-user answer is fork-per-person (DECISIONS #25,
docs/FORKING.md): perfect isolation, ~10 min of setup. The long-term
direction is **"log in with GitHub, then seamless"** for anyone:

- **Phase 1 (now):** fork + enable Actions/Pages + paste `NOTION_TOKEN`.
  Works, documented, zero shared infrastructure.
- **Phase 2:** a setup wizard *inside the platform* — signed-in non-owners
  see a "Get your own radar" flow that forks via API, enables workflows,
  and walks through the Notion integration. Same architecture, less friction.
- **Phase 3 (the real thing):** a GitHub App + Notion OAuth. Install the
  app, click "Connect Notion", done — the app provisions the fork (or a
  repo from template), stores the Notion token as a repo secret via the
  API, and the person never sees YAML. Still repo-per-person under the
  hood (that isolation model is a feature, not a limitation).

Accepted asymmetry: the original owner's instance may always have extra
functionality (Mac companion, tuned taste model, email autopilot). That's
fine — the bar for everyone else is "log in, then seamless", not parity.

## Job-quality LLM pass — ✅ SHIPPED 2026-07-13 (radar/quality.py, DECISIONS #30)

Layers 0–2 run inside the Mac companion's 2-hour enrich cycle: HTTP link
liveness (dead → `closed_at`, off every board), LLM new-grad verification
and role-fit cleanup (one JSON verdict per posting, cached on the job,
score/alert adjusted with logged reasons — never silently deleted; marquee
companies are never alert-suppressed by a verdict). ~15 jobs verified per
cycle, aggregator links first. Still open from this cluster:

- **SPA-host coverage** — ✅ SHIPPED 2026-07-16 (`quality.fetch_posting_spa`,
  DECISIONS #34): Workday (wday/cxs job JSON), Oracle ORC (requisition
  details REST), and Eightfold (apply/v2 API, careers domain from the
  registry) posting text now feeds the LLM verdict instead of being
  skipped — ~480 alert-worthy jobs gained coverage. The paste-in JD box
  (DECISIONS #33) remains the fallback for anything else. First live cycle
  runs on the Mac companion / CI (this was built in a sandbox whose egress
  can't reach ATS hosts) — if a shape drifted, jobs degrade to "unclear"
  (never suppressed) at the usual 2-attempt cap.
- **Free cloud fallback** — ✅ documented + hardened 2026-07-16: any
  OpenAI-compatible free tier (NVIDIA NIM, Google AI Studio) works via the
  `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` secrets that enrich.yml already
  wires, and `llm.complete()` now retries 429/500/503 with Retry-After.
  Remaining human step: Victor creates a free key and adds the three repo
  secrets (see docs/CLI_HANDOFF.md "Needs a human").
- **Data hygiene (no LLM)** — ✅ SHIPPED 2026-07-16: jobright parser drops
  unresolvable continuation rows, and every crawl scrubs glyph-company
  records from state (`scrub_glyph_companies` — 101 in the backlog, all
  alert_ok, gone on the next crawl).

## Ranking v2 + platform research tabs — ✅ SHIPPED 2026-07-16 (DECISIONS #31-33)

Field fit and seniority now outrank the Shams rule (off-field/mid-level
title demotions, LLM verdicts may suppress marquee, numeric-level hard
gates); `priority_sectors: [healthtech]` alerts strong engineering titles
without new-grad wording (the WHOOP fix); `regate()` re-applies rule bumps
to stored jobs. The platform drawer leads with a Company tab (what they
do), adds a Role-fit tab with the LLM posting verdict + paste-in JD
grading, and builds LinkedIn search links with entry-level/date filters in
the URL (links only — #16 stands). Search boxes keep focus while typing.

## CV auto-tailoring — ⏸️ ON HOLD (Victor's call, 2026-07-16; direction unchanged)

`CV/` now exists locally (gitignored — DECISIONS #29: personal documents
never enter this public repo). Victor will flesh out `cv_full.tex` as the
superset of everything he's done; then, per target role, the Mac companion
picks the strongest bullets/experiences for that posting and emits a
one-page `resume.tex` draft into a local review folder. Human stays the
author (DECISIONS §6): drafts are reviewed, never auto-submitted. Because
the CV is local-only, this feature runs exclusively on the Mac companion —
never in Actions.

## Pipeline intelligence
- **Response-rate analytics** — per-sector/per-source conversion rates in the
  strategist memo: "healthtech replies 3× more than big tech for you — shift
  volume." Becomes meaningful after ~30 tracked applications.
- **Interview-loop dossiers** — when Stage hits Interview, auto-generate the
  company's known loop structure, question themes, and prep checklist
  (local-LLM job, so it's free on the Mac).
- **Offer-comparison calculator** — feeds the Offer Playbook scorecard with
  real numbers: equity discounting, COL adjustment, side-by-side memo.

## Reach
- **Referral-finder** — cross NJIT alumni signals against target companies;
  drafts the outreach note into the Networking CRM. (Needs a data source that
  isn't LinkedIn scraping — evaluating SHPE directory + GitHub org members.)
- **H-1B/sponsorship cross-check** — join companies against the public DOL
  LCA disclosure data; add a `sponsors: likely/no-history` column. Cheap,
  official data. Build if sponsorship ever becomes a filter.
- **Meta careers adapter** — auth-gated GraphQL today; revisit if they ship a
  public search endpoint. Covered by aggregators meanwhile.
- **SHPE deep mode** — rep CRM per booth, session planner, live exhibitor sync
  from careercenter.shpe.org, SHPExchange resume-book optimizer. Light mode
  (exhibitor boost + battle plan) shipped in `docs/SHPE.md`.

## Content
- **Auto cover-letter skeletons** for S-tier roles only, same review-file
  flow as CV tailoring above.

## Ops
- **Calendar sync** — interview emails → Google Calendar holds.
- **Weekly Notion rollup** — mirror the Monday memo into Job Search HQ.
- **Registry hygiene job** — ✅ SHIPPED 2026-07-16 (`discovery.hygiene`,
  monthly inside enrich): dead boards get a fresh probe cycle every 30 d,
  non-seed 90-day invalids are pruned, and duplicate employer entries that
  stopped producing while a sibling still does are parked as `dup`
  (producers are never touched).
