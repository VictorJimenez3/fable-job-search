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

- **SPA-host coverage** — Workday/Eightfold/Oracle postings are JS shells a
  plain GET can't read; they're skipped today (their liveness is re-confirmed
  by the ATS poll anyway). Fetching their posting text via the ATS JSON APIs
  would let the LLM verdict cover them too.
- **Free cloud fallback** — a Gemini free-tier key via `LLM_BASE_URL` would
  run the same pass when the Mac is asleep for days.
- **Data hygiene (no LLM).** Some jobright rows come in with company `"↳"`
  (a scraped continuation glyph). Parse fix + one-time state repair.

## CV auto-tailoring (committed direction, blocked on the full CV)

`CV/` now exists locally (gitignored — DECISIONS #29: personal documents
never enter this public repo). Victor will flesh out `cv_full.tex` as the
superset of everything he's done; then, per target role, the Mac companion
picks the strongest bullets/experiences for that posting and emits a
one-page `resume.tex` draft into a local review folder. Human stays the
author (DECISIONS §6): drafts are reviewed, never auto-submitted. Because
the CV is local-only, this feature runs exclusively on the Mac companion —
never in Actions.

## Pipeline intelligence
- **Response-rate analytics** — ✅ SHIPPED 2026-07-14 (DECISIONS #31). Monday
  memo now has a "📈 Response rates by sector/source (last 3 weeks)" section,
  gated at 3+ applications per bucket. Still thin on real signal until ~30
  tracked applications accumulate — the plumbing is done, the insight
  sharpens with volume.
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
- **Registry hygiene job** — monthly: retry long-dead boards, prune 90-day
  invalids, dedupe companies that appear under two ATSs.
