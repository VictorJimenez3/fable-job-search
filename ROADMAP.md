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

## Job-quality LLM pass (next up — brainstormed 2026-07-13)

The scoring rubric is deterministic and cheap, which means it has known
failure modes: links that died since crawl, "new grad" boards listing roles
that actually want 3+ years, and roles at well-matched companies that are
the wrong *role* (sales engineer at a great healthtech ≠ SWE). A layered
cleanup pass, cheapest layer first:

- **Layer 0 — link liveness (no LLM).** Revalidate open alert-worthy jobs on
  a cadence: registry/ATS jobs are already confirmed each poll (a job gone
  from the API = closed); aggregator/jobright links need an HTTP check.
  Dead → drop from master board/dashboard, mark `closed_at`. Politeness:
  only jobs currently displayed anywhere, batched, with per-host limits.
- **Layer 1 — new-grad verification (LLM).** Fetch the posting text and ask
  one JSON question: `{years_required, new_grad: yes/no/unclear, visa_flags}`.
  Runs where inference is free: Mac Ollama first (`format:"json"` per
  DECISIONS #20), a free-tier API (e.g. Gemini via the existing
  `LLM_BASE_URL` path) as the cloud fallback, heuristics when neither
  exists. Verified-not-new-grad → suppress alert + score penalty, with the
  reason logged in `score_reasons` so it's auditable.
- **Layer 2 — role-fit cleanup (LLM).** Same fetch, second question: is this
  the *role* Victor does (SWE/AI/DS) or an adjacent-title trap (solutions
  engineer, IT support, analyst-in-name-only)? Company fit stays; role
  mismatch demotes below alert threshold instead of hard-deleting.
- **Data hygiene (no LLM).** Some jobright rows come in with company `"↳"`
  (a scraped continuation glyph). Parse fix + one-time state repair.

Design rule for all layers: LLM verdicts *adjust* scores with logged
reasons; they never silently delete. Precision-first, same philosophy as
the email autopilot (DECISIONS #26).

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
- **Registry hygiene job** — monthly: retry long-dead boards, prune 90-day
  invalids, dedupe companies that appear under two ATSs.
