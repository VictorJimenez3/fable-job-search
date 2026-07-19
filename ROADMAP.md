# Roadmap — designed, deliberately deferred

The dream-system backlog. Each item is scoped enough to build on request;
none block anything currently running.

Feature-level user stories and known acceptance gaps live in
[`docs/USER_STORIES.md`](docs/USER_STORIES.md). Use that document when turning a
roadmap item into implementation work; do not infer missing behavior from a
short TODO label.

## CLI/model handoff policy (2026-07-18)

Use the smallest trustworthy model for the task. The repository is the shared
context; every CLI should read `AGENTS.md`, `CLAUDE.md`, `README.md`,
`DECISIONS.md`, `docs/CLI_HANDOFF.md`, and the relevant module/tests before
changing behavior. Do not spend a hosted/strong model on a task that is only
documentation, inspection, or a mechanical edit.

### Safe local/small-model work (Qwen/Ollama, Qwen CLI, or Claude Code through a proxy)

- read-only repository audits, status checks, branch/history inspection, and
  explaining existing behavior;
- documentation maintenance that preserves the existing contract, TODO order,
  handoff facts, and decision-log style;
- formatting, typo fixes, link repairs, and generated-file consistency checks;
- small deterministic parser/regex/UI copy fixes with focused regression tests;
- running the prescribed test/compile/copy checks and reporting failures;
- updating a clearly scoped test fixture or adding a straightforward unit test.

Small models must not invent product policy, edit secrets/CV material, hand-edit
generated state, or make broad scoring changes just because a task looks easy.

### Stronger model recommended

- ranking/gate policy, alert-threshold changes, or anything affecting recall vs.
  precision;
- AI router/provider changes, prompt/schema changes, quota strategy, or model
  evaluation;
- auth/OAuth/owner checks, public write paths, GitHub Actions permissions, or
  deployment/Vercel changes;
- state-shape migrations, concurrency/push-race handling, tracker adapters, or
  changes spanning multiple modules;
- RAG/vector search, CV tailoring, multi-user onboarding, or any new major
  product architecture;
- ambiguous UX work where the intended behavior is not already documented.

These tasks require a stronger reasoning pass, explicit tests, and updates to
`DECISIONS.md` plus the owning operational docs. A local model may implement a
mechanical subtask after the design and acceptance criteria are written by a
stronger model.

### Human step still required

Only Victor should add/rotate provider secrets, authorize Google/Notion OAuth,
provide CV content, approve production pushes/deployments, or decide a change
that materially alters personal ranking preferences. Never ask a model to print
or copy a secret into the repository.

## 2026-07-18 AI/QoL release status

1. **AI functionality foundation / knowledge layer — ✅ SHIPPED.** Four named
   NVIDIA models are task-routed behind per-run/task budgets, bounded retries,
   cross-model fallback, health cooldowns, schema validation, and secret-free
   usage telemetry. Local Ollama stays the bulk lane; deterministic gates stay
   authoritative. Main gets 12 logical calls/18 provider attempts nightly;
   ChemE gets 8/12 through its default-branch orchestrator. Kimi remains the
   final canary until its endpoint is consistently available.
2. **Company research overhaul — ✅ V1 SHIPPED.** The crawler captures bounded
   excerpts from official postings it already reads. `company_research.json`
   stores evidence hashes, source dates/links, claim-level citations,
   confidence, TTLs, and explicit unknowns. The Companies/drawer UI now explains
   products, customers, mission, business, technical work, locations, visa
   context, candidate relevance, and interview focus. Unsupported legacy
   culture estimates remain labeled and no longer affect ranking.
3. **Google collaboration and pluggable tracking — ✅ CODE SHIPPED / AUTH
   PENDING.** `TRACKER_BACKEND=notion|google_sheets` selects a first-class
   tracker with stable-ID upsert and stage readback. Notion now reads manual
   status changes back too. Google activation needs the one-time OAuth values
   in `docs/GOOGLE_SHEETS_SETUP.md`; Notion remains active until then.
4. **Interview workspace — ✅ V1 SHIPPED.** OA/Interview applications now have
   their own workspace with cited company context, likely focus, and a prep
   checklist. A manual company-name packet and independent interview-process
   sources remain later enhancements.

## Active scope for the main board

- The active product is the CS/SWE job radar. ChemE is paused for now and
  should be treated as a separate follow-up pass, not part of the current
  delivery scope.
- Phase order matters: first finish Victor's personal board until it is
  high-quality, efficient, and low-slop; only after that should the app be
  generalized for other users.
- The deployment should stay personalized for Victor by default, but a
  non-owner who signs in should see a generic onboarding path rather than a
  hard reject. That onboarding should collect role interests, target major,
  resume/CV choice, tracker choice, and whether the user wants advanced AI
  features or a manual/basic flow.
- AI usage should remain user-scoped and opt-in for non-owners. Victor's
  deployment should never spend someone else's API keys, and future onboarding
  should require the user to supply their own provider credentials if they want
  cloud AI.
- For the first multi-user version, the fallback product should behave like a
  broad CS internship/new-grad board when the user has not provided a major or
  specialized profile. Later, the same onboarding can branch into major-specific
  boards and trackers.

## Deliberately deferred by Victor

1. **RAG and vector search.** Embed job descriptions, company dossiers,
   candidate profile/CV material, and saved decisions; support semantic search
   and similarity-based ranking with explainable evidence. Keep deterministic
   gates authoritative and log retrieval/similarity reasons. This supersedes
   the currently parked posting↔profile RAG spike below.
2. **CV-aware target-role toggle.** When a CV is available, add a `CV` option
   to the existing “all target roles” dropdown. It should show roles that can
   be meaningfully tailored to the selected CV, then offer a local, review-only
   tailored draft. Personal CV content stays local and never enters public
   state.

3. **Multi-user onboarding and generic fallback.** Add a sign-in flow for
   non-owners that creates a generic onboarding profile, lets the user pick a
   target major or keep the broad CS/SWE default, choose tracker and AI
   settings, and then routes them into a per-user radar configuration. This is
   the path that eventually makes the platform usable for any major without
   reusing Victor's personal assumptions. This is explicitly second-phase work
   and should not interfere with finishing Victor's board first.

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
score/alert adjusted with logged reasons — never silently deleted; field-fit,
seniority, and verified posting facts may suppress marquee alerts). ~15 jobs verified per
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

## Posting scraping + deterministic facts — ✅ SHIPPED 2026-07-17 (DECISIONS #35)

Every crawl now reads real posting text (Greenhouse/Ashby text comes free
in the list call; a budgeted fetch covers the rest incl. SPA hosts) and
extracts, with zero LLM/keys: visa **sponsorship** (yes/no/unknown + the
phrase), **years of experience** required (internships-count detection
included), dead links closed crawl-side. Shown as row tags, a drawer
"Posting facts" card, and in alert-issue lines; `candidate.needs_sponsorship`
in profile.yaml turns no-sponsorship postings into dashboard-only.

## Candidate-first filters + application flow — ✅ SHIPPED 2026-07-18 (DECISIONS #36)

The Jobs view now has persistent role-family, sponsorship, and experience
dropdowns (plus the existing score/sector/status/sort controls), and every row
shows explicit eligibility badges including the honest "not stated" vs "not
analyzed" distinction. Titles open a Fit & eligibility workspace first;
location, salary, score, age, visa, and years are visible before leaving for
the employer. A primary apply button opens the posting and authenticated users
are quietly saved to **To apply**; marking Applied stays explicit. Rules v3
also makes field eligibility title-led, demoting roughly 650 current false
positive alerts found through description boilerplate.

## Posting ↔ profile RAG (to-do, parked — Victor 2026-07-17: "to do list that for sure")

Embed posting text and Victor's profile/CV bullets, use similarity as a
ranking signal ("how related is this role to what I've actually done").
Local-first: Ollama embedding models on the Mac are free (e.g.
nomic-embed-text), store vectors next to the quality cache, keep the score
deterministic-auditable by logging the similarity as a reason line. Needs
the scraped posting text (now shipping) + `CV/` content (Mac-only,
DECISIONS #29). Park until the scrape pass has filled a few weeks of text.

## Ranking v2/v3 + platform research tabs — ✅ SHIPPED 2026-07-18 (DECISIONS #31-33, #36)

Field fit and seniority now outrank the Shams rule (off-field/mid-level
title demotions, LLM verdicts may suppress marquee, numeric-level hard
gates); `priority_sectors: [healthtech]` alerts strong engineering titles
without new-grad wording (the WHOOP fix); `regate()` re-applies rule bumps
to stored jobs. The platform drawer now leads with Fit & eligibility, with
Company research one tab away; the LLM posting verdict + paste-in JD grading
remain in the fit view. It also builds LinkedIn search links with entry-level/date filters in
the URL (links only — #16 stands). Search boxes keep focus while typing.
Rules v3 makes role eligibility title-led and narrows the data-science analyst
match; unrelated titles mentioned above no longer ride company/JD AI prose
into alerts.

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
  official data. Sponsorship is now a platform filter, so this is the next
  useful eligibility upgrade: posting wording stays primary evidence, while
  DOL history is clearly labeled as company-level historical context—not a
  promise that a particular requisition sponsors.
- **Meta careers adapter** — auth-gated GraphQL today; revisit if they ship a
  public search endpoint. Covered by aggregators meanwhile.
- **SHPE deep mode** — rep CRM per booth, session planner, live exhibitor sync
  from careercenter.shpe.org, SHPExchange resume-book optimizer. Light mode
  (exhibitor boost + battle plan) shipped in `docs/SHPE.md`.

## Content
- **Auto cover-letter skeletons** for S-tier roles only, same review-file
  flow as CV tailoring above.
- **Local application-answer vault** — keep reusable answers (graduation,
  work authorization/sponsorship, location, portfolio links, short project
  blurbs) outside this public repo, then expose copy buttons and a per-job
  checklist in the workspace. Candidate reviews every answer; no blind form
  submission. This is the next application-speed QoL step after the shipped
  open/save flow and should reuse the Mac-local privacy boundary from #29.

## Ops
- **Calendar sync** — interview emails → Google Calendar holds.
- **Weekly Notion rollup** — mirror the Monday memo into Job Search HQ.
- **Registry hygiene job** — ✅ SHIPPED 2026-07-16 (`discovery.hygiene`,
  monthly inside enrich): dead boards get a fresh probe cycle every 30 d,
  non-seed 90-day invalids are pruned, and duplicate employer entries that
  stopped producing while a sibling still does are parked as `dup`
  (producers are never touched).
