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

### Latest change (verified 2026-08-08)

- **PM dashboard lane:** `pm` is a zero-weight role bucket for Product
  Manager, Technical Product Manager, Product Owner, Project Manager, Business
  Analyst, UX/UI Researcher, and Solutions Architect titles. These rows remain
  dashboard-visible and filterable, but `gates()` always leaves them
  `alert_ok=false`, so they cannot create alert issues, alert batches, or RSS
  delivery. The lane reads Simplify's PM section, Jobright's dedicated
  seven-day PM board (`jobright_pm`), and Zapply's PM breadth board
  (`zapply_pm`). PM-originated official ATS links are marked in the company
  registry and prioritized both when probing new boards and when selecting the
  active-company polling cap.
- **PM direct coverage:** Workday and Phenom receive explicit PM title
  searches (APM, product manager/owner, project manager, business analyst,
  UX/UI researcher, solutions architect, and product management). Amazon,
  Microsoft, Apple, and Google receive the same PM-family query fan-out;
  Amazon drops its technical category restriction only for those PM queries.
  This broadens recall while preserving the existing hard gates and zero-weight
  PM scoring policy.
- **Google preference:** `profile.yaml` contains a data-driven score override
  that makes Google technical new-grad roles `100`, with `pm` explicitly
  excluded. The reason is printed in `score_reasons`; rules version is now 10.
- **Frontend:** the Jobs role-field toggles include `Product / project
  management`; `docs/platform/index.html` remains a byte-for-byte copy of
  `webapp/index.html`.
- **CI hygiene:** the two exact-template Resume Studio tests now skip only on
  GitHub Actions when the intentionally local-only `CV/resume.tex` is absent;
  they continue to run on Victor's Mac where the private CV exists.
- **Taste loop:** saved/applied roles are shown as an owner-only positive
  sample in the platform's **Taste** tab. The tab explains existing
  engagement-related score reasons and offers advisory **Similar jobs to
  inspect** results; similarity never becomes an opaque replacement score.
  Fixed-category explicit feedback is stored in `state/feedback.json` and
  rendered through `python -m radar.main taste-report` to `docs/FEEDBACK.md`.
  Only the owner API or the owner-authenticated issue command can write it;
  the public repository warning is intentional and no email is sent.
- **Posting moderation:** owner archive is a soft, recoverable
  `manual_archived` marker that is carried through future crawls and keeps the
  historical job. Non-owners report expired/filled/duplicate/wrong postings
  through structured GitHub issues. `radar-report` workflow events use the
  issue author's GitHub login, deduplicate by distinct reporter, and add a
  three-distinct-reporter item to `docs/REPORTS.md` plus an owner review
  comment; they never auto-archive.

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
  manually. The email autopilot remains coded/tested but is intentionally
  parked as future multi-user functionality; no `EMAIL_ADDRESS` /
  `EMAIL_APP_PASSWORD` secrets are needed for Victor's current workflow.
- **Scoring (rules v7, 2026-07-24):** verified new-grad or
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
  (`score.RULES_VERSION`, records carry `rules_v` + `explicit_new_grad` +
  `early_career_possible`)
  to stored jobs; every crawl now fully rebuilds every active stored
  score before publishing; `python -m radar.main rescore` is also available to
  manually rebuild every stored
  score after a profile-priority change, while `regate` only refreshes gates;
  `RADAR_SCRAPE_DASHBOARD=1 RADAR_RESCRAPE_LIMIT=100 python -m radar.main
  rescrape` rechecks visible dashboard roles through free ATS JSON/HTML
  endpoints and labels unreadable requirements instead of guessing;
  manual commands: `python -m radar.main regate` /
  `repair-feedback`, `taste-report`, and `report-sync` (the latter is the
  GitHub issue workflow entrypoint). The taste model filters
  `FEEDBACK_STOPWORDS`. The
  marquee list is duplicated in `webapp/index.html` (`S.marquee`) — keep
  both copies in sync.
- **Early-career possible is deliberately separate from new grad:** a target
  technical title with no stated experience floor, but no verified campus/new-
  grad evidence, gets an explicit Jobs badge and filter. It stays dashboard-
  only and never changes `alert_ok`; the Fanatics AI Engineer is the model
  case.
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
- **On-demand company research (DECISIONS #65):** The signed-in owner sees
  **research + prefetch companies** in a job's Company drawer. It dispatches
  the opened job plus up to four distinct not-yet-ready employers from the
  current Jobs ordering to `web-actions.yml`; the opened employer has priority
  and the warmups synthesize concurrently. The workflow receives hosted-provider
  secrets only in GitHub Actions. The UI is asynchronous (normally 1–3 minutes
  for the first result, then warmed drawers) and is not available to visitors,
  Pages/PAT write paths, or non-owner OAuth sessions.
- **Tracker selection/readback (DECISIONS #40, #92):** Notion now pulls manual
  stage changes by owned page ID. `TRACKER_BACKEND=google_sheets` remains the
  server-side Sheets adapter, while the Vercel owner UI explicitly defaults to
  Notion for `VictorJimenez3`. The expanded owner Tracker options can enable a
  connected Google Sheet as an optional personal mirror; other signed-in users
  still use their own Google tracker. Setup is
  [`GOOGLE_SHEETS_SETUP.md`](GOOGLE_SHEETS_SETUP.md).
- **Google tracker creation + multi-user sign-in:**
  `python -m radar.main create-google-tracker` creates the owner metadata and
  legacy automation workbook. The Vercel platform offers GitHub and Google
  OAuth; Google consent requests the least-privilege `drive.file` scope and
  creates one Applications workbook in that user's own Drive. The Google Cloud
  OAuth app must be External + In production; a test-user list is not the public
  deployment path. GitHub users can explicitly connect Google later from the
  Tutorial account center. The private Accounts registry stores only encrypted
  refresh-token ciphertext and Sheet IDs when available, but is no longer a
  prerequisite or single point of failure. The current user's encrypted
  HttpOnly session carries their own grant and Sheet ID; Drive marker/title
  discovery reconnects the same workbook after reauthentication.
  `/api/tracker` reads only the current user's workbook. `GOOGLE_ACCOUNT_SHEET_TAB` remains
  `Accounts`; `GOOGLE_PERSONAL_SHEET_TAB` defaults to `Applications`. Pages and
  tokenless issue mode remain owner-only.
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
  quota. It runs as a sustained worker (up to GitHub's 330-minute job limit)
  with a 30-minute relay, so a short exit or provider cooldown is retried
  promptly and the newest relay follows any long run. It resumes from
  checkpoints automatically; the two-hour `enrich` workflow remains the
  fresh/high-priority lane.
- **Notification cadence:** individual alert issues are silent tracking
  surfaces. `alert-batch.yml` sends up to 15 unsent roles every four hours,
  ranked by score and recency, as the normal alert email. It records delivered IDs in
  `state/notification_state.json`, so overflow is not lost. Crawl delivery
  scans only the active alert replay window and patches only changed master-
  board comment pages; its log reports the bounded GitHub work performed.
- **Maintenance:** `score-maintenance.yml` runs every six hours and rebuilds
  every stored score from the latest production snapshot. `tests.yml` and the
  maintenance workflow run `python -m radar.main score-health`, which fails if
  any stored record lacks the current score/rules version. The manual company
  backfill checkpoints one bounded batch per commit, prioritizes saved/tracked
  employers before alert-worthy, high-score, and fresh roles, and uses GLM-first
  API synthesis with a 75-second request timeout; rerun `company research
  backfill` to resume safely. Failed provider
  or schema attempts are explicit retryable records with capped exponential
  backoff; checkpoint logs expose ready/pending/retry/error counts.
- **`CV/` is local-only and gitignored** (DECISIONS #29) — never commit it or
  anything derived from it. Owner-only Resume Studio now runs locally with
  source-only and enhancement modes; start it with
  `.venv/bin/python scripts/resume_studio.py` and open `http://127.0.0.1:4317/`.
  For temporary human-feedback calibration across varied radar roles, run
  `.venv/bin/python scripts/resume_calibration.py --generate --count 6 --serve`
  and open `http://127.0.0.1:4321/`; generated PDFs and JSONL labels stay under
  `CV/.resume_studio/calibration/`.
  It uses installed first-party Codex/Claude Code sessions, strips API-key
  environment variables, and stores all output under `CV/.resume_studio/`.
  `CV/resume.tex` / `.pdf` is the exact visual baseline; TLDP is only one
  target artifact. Providers return source-addressed content plans, never full
  LaTeX. The renderer preserves the baseline's header, education, skills,
  margins, typography, and spacing, uses company-first employer headings, and
  enforces a 22–26-bullet, nonredundant interview portfolio across three
  experiences and four projects with no wrapped bullets, excessive bottom
  whitespace, or formatting drift. Reports record the fixed
  reviewer score, exclusions, provider calls, and emitted Codex token usage;
  totals are marked incomplete when a provider omits a call's footer.
  Live validation on 2026-07-31 found the Codex subscription session usable;
  the installed Claude client returned an organization-level 403 stating that
  Claude Code subscription access is disabled. The harness fell back to Codex
  and did not request or consume an Anthropic API key.
- **Resume intelligence v1 is shipped locally (DECISIONS #72-74):** the Studio
  builds a source-authority evidence graph from the ignored CV corpus, caches
  public GitHub/Devpost material as corroboration only, and exposes Best,
  Newest, and Resume Match sorts. The full-posting match is fixed-weight and
  source-explained. Generation produces a full ranked candidate portfolio and
  a deterministic curator keeps 22–26 distinct bullets across three
  experiences, four projects, and one or two leadership entries. It measures
  actual PDF bullet geometry and hard-fails excessive bottom whitespace. The
  paid adversarial pass must return an applied corrected plan, while wrapped
  rewrites first fall back to approved source text without another model call.
  Scope qualifiers are protected and compilation errors stop packing instead
  of deleting content. Resume Craft cannot override factuality, eligibility,
  or layout gates.
- **Resume Studio library/preview surface is shipped (DECISIONS #75):** every
  new run snapshots its selected posting under the private run directory,
  receives a company-identifiable PDF name, and remains visible when another
  posting is selected. The **Resume bank** indexes current runs plus legacy
  architecture experiments, shows source-only versus AI-enhanced mode, and can
  reveal the captured posting text. Failed and in-progress runs remain
  inspectable; the immutable human reference PDFs are untouched.
- **Resume Studio workshop is shipped (DECISION #76):** enhanced generation can
  substantially rewrite or synthesize a line from multiple authorized source
  bullets instead of only tightening a source sentence. Completed runs open a
  durable line editor for education, skills, experience, projects, and
  leadership; AI suggestions are opt-in, manual/AI saves render unique PDF
  revisions, and history rollback creates a new revision without overwriting
  the original. The current machine exposes Codex CLI and Claude Code lanes;
  Luna remains an optional future lane if its local executable is installed.
- **Resume Studio ATS/change proof is shipped (DECISION #77):** current
  generation prompts inline the bounded CV authority dossier and exact posting
  keyword strategy, permit target-aware project swaps, and report rewritten
  bullets plus keyword coverage. The final PDF gate rejects wraps and any line
  with less than 12pt of right-edge safety; failed runs remain inspectable
  instead of being labeled complete. Existing historical PDFs are not
  retroactively regenerated; start a new run to exercise this contract.
- **Resume Studio naming/exclusion polish is shipped:** generated project
  headings normalize the visual delimiter to `|`, PDF preview responses send
  the company-identifiable filename to the browser, and TICC is hard-excluded
  from source selection, model plans, rendering, and workshop edits without
  modifying the local CV corpus.
- **Resume Studio queue/editor/usage is shipped:** Used bullets, AI tailor, and
  Unrestricted AI tailor are durable queued modes. The bank keeps each posting
  snapshot and run; the workshop embeds the original/latest PDF, edits every
  visible line, and exposes selection rationale plus observed weekly Codex
  tokens/calls. Codex Plus's weekly allowance is not exposed by the local CLI;
  `CODEX_WEEKLY_LIMIT_TOKENS` is an optional owner-provided comparison limit.
- **Resume Studio Sol-high UAT hardening is shipped (DECISION #82):** repeated
  reviewer selections for one entry merge distinct bullets instead of dropping
  them, supported ATS rewrites reach the margin editor before safe source
  fallback, and workshop front-matter metadata self-refreshes while preserving
  existing edits. A fresh unrestricted Mayo run and edited revision compiled as
  one full page with 24 one-line bullets, zero wraps, and zero near-wraps.
- **Radar v9 diversity scoring is shipped locally:** explicit new-grad evidence
  is weighted more strongly, plausible no-floor first-role postings get a
  smaller dashboard-only lift, and only weaker roles in a company with at least
  three visible roles receive a transparent -1/-2 concentration nudge. The
  strongest same-company role and raw-utility ties are protected. Platform
  rating explanations now use labeled rows, and Resume Match remains separate.
- **Role-aware recruiter search is shipped locally:** the per-job Outreach tab
  generates public Google searches for university recruiters, technical
  recruiters, likely hiring managers, NJIT alumni, and public hiring posts.
  This is link construction only and preserves the no-LinkedIn-scraping rule.
- **Official DOL sponsorship context is shipped locally:**
  `python -m radar.main sponsorship-refresh` downloads the latest quarterly
  OFLC LCA disclosure workbooks, stores only a compact
  `state/sponsorship.json` aggregate, and runs a full rescore. The Jobs view
  and Fit drawer expose likely historical sponsor/no-history/unavailable with
  explicit caveats. Posting-level visa wording remains primary and this signal
  does not affect score. GitHub Actions refreshes it weekly. The source is the
  [DOL OFLC performance-data page](https://www.dol.gov/agencies/eta/foreign-labor/performance).
- **Radar score v8 is shipped (DECISIONS #70-71):** records retain uncapped raw
  utility, calibrated base, dimension values, reasons, and final score. Goal
  companies can reach 100, but posting wording separates nearby roles; cited
  momentum and compensation can compensate for weaker ordinary dimensions.
  Sector points have diminishing influence and hard gates remain absolute.
- **Still deliberately deferred:** semantic/vector RAG, bullet locks/history,
  revision threads, and multi-user CV/provider storage. Email-based
  applied detection is also parked until the multi-user privacy model exists.

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
