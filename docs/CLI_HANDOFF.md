# CLI handoff notes

This repository is maintained across more than one coding CLI (including Claude
and Codex). Keep the repository—not a chat transcript—as the shared source of
truth.

## Read first

Before changing the radar, read these in order:

0. [`AGENTS.md`](../AGENTS.md) — Codex's auto-loaded repository instructions,
   validation commands, and production-branch safety notes.
1. [`CLAUDE.md`](../CLAUDE.md) — despite the legacy filename, this remains the
   shared tool-neutral repo map and doc-update mandate.
2. [`README.md`](../README.md) for the system's purpose and operational flow
   (and [`TUTORIAL.md`](TUTORIAL.md) for how Victor actually uses it — keep it
   current when user-facing behavior changes).
3. [`DECISIONS.md`](../DECISIONS.md) for the deliberate architecture and trade-offs.
4. [`profile.yaml`](../profile.yaml) for Victor's active search preferences and ranking thresholds.
5. The relevant module and test under `radar/` and `tests/`.

## Keep these current

- Update `README.md` when user-facing setup, commands, sources, delivery
  channels, or operating behavior changes.
- Add a dated entry to `DECISIONS.md` when making a non-obvious product or
  architecture choice, especially one that trades recall for precision.
- Update `profile.yaml` only for candidate preferences and ranking policy—not
  implementation behavior.
- Add or update tests for scoring gates, source parsing, state migration, or
  output behavior that changes.
- Do not hand-edit generated runtime outputs (`state/*.json`,
  `docs/DASHBOARD.md`, or `docs/feed.xml`) except for a deliberate repair with
  its reason documented in the commit/message. Crawls generate them.

## Model selection for future CLIs

Use the smallest model that can safely complete the task. Local Qwen/Ollama or
Qwen CLI is appropriate for repository orientation, docs/TODO maintenance,
mechanical edits, focused parser/UI fixes, test additions, and validation. A
Claude Code session using a proxy is also fine for bounded implementation when
the acceptance criteria already exist.

Use a stronger model for scoring/gate policy, AI routing/prompts/quotas,
authentication or owner checks, Actions/Vercel/deployment, state migrations,
tracker/concurrency changes, RAG/vector search, CV tailoring, multi-user
onboarding, or ambiguous product decisions. Those tasks need explicit tests,
an auditable decision entry, and updates to the owning docs.

Never let a small model invent ranking policy, touch secrets or `CV/`, hand-edit
generated state, or broaden scope. Human approval remains required for provider
secret changes, OAuth activation, CV content, and production pushes.

## Current operational facts (verified 2026-07-18)

- GitHub Actions is the production runtime; it uses Python 3.12. On Victor's
  Mac, system Python is 3.9 but the repo's `.venv` has the dependencies —
  run tests with `.venv/bin/python -m pytest tests/`. CI commits state every
  ~30 min, so always `git pull --rebase` before pushing.
- **Delivery surfaces, all live:** weekly alert issue (checkbox = track to
  Notion as not-yet-applied), 📌 master board issue (every open alert-worthy
  role, rewritten each crawl), 🏆 daily best-of issue, docs/DASHBOARD.md,
  RSS, and the platform website. Twice-daily reconcile sweep guarantees no
  checked box is ever lost.
- **The platform has two permanent doors** (DECISIONS #27): Vercel
  (job-radar-vmj-8946s-projects.vercel.app — GitHub OAuth, instant writes,
  Victor's daily driver) and GitHub Pages
  (victorjimenez3.github.io/fable-job-search/platform/ — tokenless, what
  forks get). `webapp/index.html` is canonical; `docs/platform/index.html`
  is a byte copy. Jobs tab shows posting age and sorts by best-match or
  newest-first.
- **Current scope:** focus active work on the CS/SWE board. ChemE is paused
  and should not be expanded until Victor explicitly revives it.
- **Always-on local lane (verified 2026-07-18):** Ollama is reachable at
  `127.0.0.1:11434` and `com.jobradar.enrich` is loaded via launchd with
  `qwen3:30b` every two hours. Local enrichment can run alongside cloud
  enrichment; keep local cycles serialized because they commit through the
  same companion clone. The model is released between requests to avoid
  monopolizing Mac memory.
- **Phase ordering:** finish Victor's personal board first. Only after the
  main board is high-quality, efficient, and low-slop should the app be
  generalized for other users.
- **The ChemE profile is a separate production board** at
  `job-radar-cheme.vercel.app`, backed by `claude/cheme-intern-radar` and
  `RADAR_PROFILE=cheme`. The default production branch owns the three
  `cheme-*` scheduled orchestrator workflows because GitHub schedules only
  default-branch workflow files. Dispatches and checkbox/comment events route
  to the correct branch via the profile payload or `radar-cheme` label. Both
  profiles deliberately share the repository `NOTION_TOKEN`; do not create a
  second Notion database or token for ChemE.
- **Notion:** `NOTION_TOKEN` is set and working. Checkbox → entry with the
  `stage_saved` status ("Waiting for a referral" in his DB); Victor promotes
  manually, OR the **email autopilot** (DECISIONS #26, shipped 2026-07-13 by
  an Opus session) advances Stage from lifecycle emails (applied/OA/
  interview/rejected, forward-only, auto-close after 45 d silence) — that
  path is coded and tested but **awaits the `EMAIL_ADDRESS` /
  `EMAIL_APP_PASSWORD` secrets** (confirmed empty in the live workflow on
  2026-07-18; Gmail app password setup is in README §2). The current 142
  tracked entries are all still `saved`, not confirmed applications.
- **Scoring (rules v3, 2026-07-18, DECISIONS #31-32, #36):** hard gates (incl.
  numeric levels: Engineer 3+/L5+/Level 3+/"Leader") + alert-eligibility
  paths: aggregator listing, explicit new-grad wording, marquee
  (`marquee_companies` incl. WHOOP/Oura/Dexcom/Abbott), $150k+ `pay_bank`,
  or `priority_sectors` (healthtech + strong engineering title). Then
  demotions that outrank ALL of those: `OFF_FIELD_RE` (safeguards/policy/
  sales/PM/support/...) and `MIDLEVEL_RE` (II/L4/Engineer 2) → dashboard
  only. Role eligibility is now title-led (description text cannot establish
  field fit), and bare Analyst no longer maps to data science; generic/off-field
  analysts remain dashboard-only for audit. LLM quality verdicts may suppress
  marquee alerts. `regate()`
  runs at the top of every crawl and re-applies rule bumps
  (`score.RULES_VERSION`, records carry `rules_v` + `explicit_new_grad`)
  to stored jobs; manual commands: `python -m radar.main regate` /
  `repair-feedback`. The taste model filters `FEEDBACK_STOPWORDS`. The
  marquee list is duplicated in `webapp/index.html` (`S.marquee`) — keep
  both copies in sync.
- **Posting scraping (DECISIONS #35, 2026-07-17):** every crawl runs
  `posting.scrape_pass` — Greenhouse (`content=true`)/Ashby/Lever text is
  analyzed inline, and up to `RADAR_SCRAPE_LIMIT` (20) postings/run are
  fetched (SPA hosts via their JSON APIs) for new + stored alert-worthy
  jobs. Extracted facts live in `rec["posting"]` (sponsorship / years_min /
  intern_counts + matched phrases): 3+ scraped yrs → dashboard-only;
  `candidate.needs_sponsorship: true` (profile.yaml, default false) also
  demotes no-sponsorship postings. `RADAR_SCRAPE_DISABLE=1` kills the pass.
  No LLM or secret involved.
- **Platform QoL (DECISIONS #36):** Jobs filters persist per browser and now
  cover role family, exact sponsorship state, experience requirement, sector,
  score, pipeline, and sort. Rows always show eligibility badges, distinguishing
  "not stated" from "not analyzed." Titles open the Fit & eligibility drawer;
  the employer link is a separate primary action. Authenticated `open
  application` also saves a new role to To apply, but Applied remains explicit.
  Track/applied writes are idempotent.
- **AI foundation + company research (DECISIONS #39):** `radar/llm.py`
  task-routes the four named NVIDIA NIM keys with hard logical/request budgets,
  two-provider fallback, transient retry, cooldown, output validation, and
  secret-free `state/ai_usage.json`. Main cloud enrichment now runs every two
  hours (still capped at 12 logical / 18 provider requests per run; up to 10
  quality checks and 2 company briefs) and rotates
  the first-choice NVIDIA key by slot; ChemE has a default-branch
  `cheme-enrich.yml` orchestrator at 8/12. Explicit pasted JDs, tracked roles,
  and fresh high-score work outrank cold backlog. Kimi is final fallback
  because its authenticated endpoint has been intermittently 404.
- **NVIDIA quota evidence (2026-07-18):** the account UI says “Up to 40 RPM,”
  but does not identify the scope. A 40-request burst (10 × 4-model probes)
  produced one GLM 429, DeepSeek timeouts, healthy Nemotron responses, and
  Kimi 404s; GLM/Nemotron recovered after a 70-second wait. Treat 40 RPM as an
  unknown provider/account ceiling, not as a guaranteed per-model allowance.
  Do not repeat the stress test; rely on cooldowns, rotation, and
  `state/ai_usage.json` telemetry.
- **Current alert focus (rules v4):** full-stack and systems-engineering titles
  remain on the dashboard for recall but are temporarily excluded from alert
  issues/digests. The active alert stream is AI/ML, data science, and other
  software roles that are not explicitly full-stack/systems; this is a focused
  personal-search preference, not a permanent deletion gate.
  The 30-minute crawls never receive the named keys.
- **Evidence-first dossiers:** `posting.scrape_pass` retains only bounded
  relevant excerpts from official postings in `state/company_research.json`.
  `company_research.py` uses evidence hashes/TTL and accepts only claim-level
  cited synthesis; unsupported facts become `Not confirmed`. Legacy
  `culture.json` estimates stay visible but only `source: seed` affects score.
  The UI shows sources/freshness, fixes company→registry lookup, and includes
  an Interview v1 workspace. Mac push-race merging preserves research/usage.
- **Tracker selection/readback (DECISIONS #40):** Notion now pulls manual stage
  changes by owned page ID. `TRACKER_BACKEND=google_sheets` selects the
  OAuth-refresh-token Sheets adapter (stable ID upsert + stage readback); setup
  is [`GOOGLE_SHEETS_SETUP.md`](GOOGLE_SHEETS_SETUP.md). Default stays Notion.
- **Multi-user = fork-per-person** (DECISIONS #25, docs/FORKING.md). Owner
  gates exist in three layers: workflow condition, Python handler, Vercel
  backend. The Mac companion is fork-portable too: `JOBRADAR_REPO=<you>/<repo>`
  on install.sh; run.sh derives the branch from the clone.
- **The Claude-named default branch is still production.** Codex work does not
  require renaming it. If it is renamed, treat that as a coordinated migration:
  three workflows, the Vercel branch default/environment, raw README/installer
  URLs, and the installed Mac companion all reference or derive it. The full
  checklist is in [`AGENTS.md`](../AGENTS.md).
- **`CV/` is local-only and gitignored** (DECISIONS #29) — never commit it
  or anything derived from it; CV auto-tailoring is a Mac-companion feature.
- **Deliberately deferred:** CV tailoring/CV role toggle, semantic/vector
  RAG, and the multi-user onboarding flow. The current build is personal-first
  for Victor, and non-owners should eventually be routed into a generic
  onboarding path that collects their own preferences and credentials.
  The only user steps for shipped code are optional Google OAuth activation
  and the already-documented ChemE email app password.
- **Multi-user end state:** when Victor's board is finished, the app should
  transition into a generic onboarding flow for other users. That future flow
  should let them set their own major, board style, tracker, and AI
  credentials, but it is intentionally not the current priority.

## Safe handoff practice

At the end of any material change, state:

1. What changed and why.
2. Files changed.
3. Validation run and its result (or why it could not run).
4. Any configuration/secrets or GitHub-side action still required.

Preserve unrelated working-tree changes. Do not assume a secret exists merely
because the code references it.

## Platform frontend/back end (added 2026-07-11)

- `webapp/index.html` is the canonical platform page; `docs/platform/index.html`
  must stay a byte-for-byte copy (`cp webapp/index.html docs/platform/index.html`)
  — Pages serves the copy, Vercel serves webapp/ plus its `api/` functions.
- Never put credentials in the frontend or repo. Auth = GitHub OAuth via the
  Vercel backend (owner-only), or the tokenless prefilled-issue flow on Pages.
