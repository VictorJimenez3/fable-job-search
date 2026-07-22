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

## Current operational facts (verified 2026-07-18)

- GitHub Actions is the production runtime; it uses Python 3.12. On Victor's
  Mac, system Python is 3.9 but the repo's `.venv` has the dependencies —
  run tests with `.venv/bin/python -m pytest tests/`. CI commits state every
  ~30 min, so fetch/rebase code work before pushing. Generated state is
  deliberately different: never rebase it after a rejected push; use the
  workflow-specific rebuild/cache-merge recovery described below.
- **Delivery surfaces, all live:** one unassigned alert issue per new qualifying
  posting for tracking, one assigned batch issue for controlled GitHub
  push/email, an intentionally unassigned 📌 master board
  (every open alert-worthy role, rewritten each crawl), 🏆 daily best-of issue,
  docs/DASHBOARD.md, RSS, and the platform website. Twice-daily reconcile sweep
  guarantees no checked box is ever lost.
- **The platform has two permanent doors** (DECISIONS #27): Vercel
  (job-radar-vmj-8946s-projects.vercel.app — GitHub OAuth, instant writes,
  Victor's daily driver) and GitHub Pages
  (victorjimenez3.github.io/fable-job-search/platform/ — tokenless, what
  forks get). `webapp/index.html` is canonical; `docs/platform/index.html`
  is a byte copy. Jobs tab shows posting age and sorts by best-match or
  newest-first.
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
- **Scoring (rules v6, 2026-07-20):** verified new-grad or
  early-career evidence is required for alerts, except for a technical/data
  graduate, rotational, or leadership program. Aggregator listings, marquee
  companies, high salary, and healthtech no longer bypass that gate. Eligible
  roles prioritize AI/ML, data science, general SWE, then data engineering and
  systems; program matches receive a separate auditable bonus and
  target healthcare program companies are labeled. Required 1+ years demotes
  to dashboard-only (0-2 years remains acceptable). `OFF_FIELD_RE` and
  `MIDLEVEL_RE` still outrank every alert path; company-tier reasons explicitly
  mark marquee roles as competitive. `regate()`
  runs at the top of every crawl and re-applies rule bumps
  (`score.RULES_VERSION`, records carry `rules_v` + `explicit_new_grad`)
  to stored jobs; every crawl now fully rebuilds every active stored
  score before publishing; `python -m radar.main rescore` is also available to
  manually rebuild every stored
  score after a profile-priority change, while `regate` only refreshes gates;
  `RADAR_SCRAPE_DASHBOARD=1 RADAR_RESCRAPE_LIMIT=100 python -m radar.main
  rescrape` rechecks visible dashboard roles through free ATS JSON/HTML
  endpoints and labels unreadable requirements instead of guessing;
  manual commands: `python -m radar.main regate` /
  `repair-feedback`. The taste model filters `FEEDBACK_STOPWORDS`. The
  marquee list is duplicated in `webapp/index.html` (`S.marquee`) — keep
  both copies in sync.
- **Posting scraping (DECISIONS #35, 2026-07-17):** every crawl runs
  `posting.scrape_pass` — Greenhouse (`content=true`)/Ashby/Lever text is
  analyzed inline, and up to `RADAR_SCRAPE_LIMIT` (20) postings/run are
  fetched (SPA hosts via their JSON APIs) for new + stored alert-worthy
  jobs. Extracted facts live in `rec["posting"]` (sponsorship / years_min /
  intern_counts + matched phrases): 1+ scraped yrs → dashboard-only;
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
- **Fanatics coverage (DECISIONS #63):** Fanatics is a multi-board employer,
  not a single Oracle feed: the curated registry polls its official Greenhouse
  corporate (`fanaticsinc`), Betting & Gaming (`fanaticsfbg`), Commerce, and
  Collectibles boards. A manually saved posting is upgraded to official ATS
  evidence once the crawler finds the same stable company/title/location ID.
- **Manual Pipeline additions (DECISIONS #62):** Pipeline—not the main Jobs
  tab—contains an owner-authenticated form for a company, role, live URL, and
  optional location. It adds a stable manual record to To apply and the same
  Notion sync path, while forcing `alert_ok=false` and
  `explicit_new_grad=false`; it cannot create a misleading alert.
- **AI foundation + company research (DECISIONS #39, #41):** `radar/llm.py`
  task-routes the four named NVIDIA NIM keys with hard logical/request budgets,
  concurrent provider racing, transient retry, cooldown, output validation,
  and secret-free `state/ai_usage.json`. Main cloud enrichment now runs every
  two hours with a 12/18 budget;
  ChemE has a default-branch `cheme-enrich.yml` orchestrator at 8/12. Explicit pasted JDs,
  tracked roles, and fresh high-score work outrank cold backlog. The benchmark
  workflow measures task-specific winners; Kimi remains unreliable because its
  authenticated endpoint has intermittently returned 404.
  The 30-minute crawls never receive the named keys.
- **Leadership-program watch (verified 2026-07-19):** the profile now treats
  technical/data graduate and rotational programs as a first-class alert path.
  The initial healthcare watchlist covers Johnson & Johnson, Merck, Pfizer,
  Bristol Myers Squibb, Roche, Catalent, and Alcon. J&J's TLDP is explicitly
  a two-year technology accelerator for college graduates spanning AI/data,
  software, and digital health; Merck's official pages describe both its
  graduate-oriented Manufacturing LDP and IT emerging-talent tracks. Static
  program pages are evidence for discovery, but only live job postings enter
  the tracker.
- **Evidence-first dossiers:** `posting.scrape_pass` retains bounded official
  posting excerpts, then recurring research also fetches public company/about,
  careers, benefits, culture, and discovery-board pages. Each source URL is
  retained in `state/company_research.json`; the Company tab shows the
  plain-English overview, employer profile table, and posting discovery source.
  Local Ollama (`qwen3:30b`) is supported for synthesis; malformed/truncated
  output is rejected and deterministic crawling continues. Non-public profile
  values are explicitly labeled estimated.
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
- **Publishing authority:** Victor checks production directly. Codex may publish
  validated requested work during the active task and must report the exact
  production commit/PR and visible surface. Any other AI agent needs Victor's
  explicit permission before pushing, merging, or deploying.
- **Generated-state writers:** crawls, enrichment, and the company-dossier
  backfill may all be active. Never resolve a rejected generated-state push
  with `git pull --rebase`. A crawl preserves stable-ID discoveries and alert
  history, resets to fresh production, merges those additions, then rescoring
  rebuilds derived state/docs. Enrichment/backfill merge only additive AI
  evidence caches. The dossier backfill uses eight bounded concurrent company
  tasks with global request budgets and exponential provider circuit breakers;
  `state/ai_usage.json` is the evidence for actual usage versus configured
  quota. It runs as a sustained six-hour scheduled worker (up to GitHub's
  330-minute job limit) and resumes from checkpoints automatically; the
  two-hour `enrich` workflow remains the fresh/high-priority lane.
- **Notification cadence:** individual alert issues are silent tracking
  surfaces. `alert-batch.yml` sends up to 15 unsent roles every four hours,
  ranked by score and recency, as the normal alert email. It records delivered IDs in
  `state/notification_state.json`, so overflow is not lost.
- **Maintenance:** `score-maintenance.yml` runs every six hours and rebuilds
  every stored score from the latest production snapshot. `tests.yml` and the
  maintenance workflow run `python -m radar.main score-health`, which fails if
  any stored record lacks the current score/rules version. The manual company
  backfill checkpoints one bounded batch per commit, prioritizes high-score
  visible employers, and uses GLM-first API synthesis with a 75-second request
  timeout; rerun `company research backfill` to resume safely.
- **`CV/` is local-only and gitignored** (DECISIONS #29) — never commit it
  or anything derived from it; CV auto-tailoring is a Mac-companion feature.
- **Deliberately deferred:** CV tailoring/CV role toggle and semantic/vector
  RAG. The only user steps for shipped code are optional Google OAuth
  activation and the already-documented ChemE email app password.

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
