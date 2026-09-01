# CLI handoff notes

This repository is maintained through Codex and repository automation. Keep
the repository—not a chat transcript—as the shared source of truth.

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
- Do not hand-edit generated runtime outputs (`state/*.json`, including the
  internship `state/intern_*.json` namespace, `docs/DASHBOARD.md`,
  `docs/feed.xml`, or `docs/internships/`) except for a deliberate repair with
  its reason documented in the commit/message. Crawls generate them.

### Current change (verified 2026-08-29)

- **GitHub Pages retired:** the repository's legacy Pages setting was disabled
  after removing the `docs/platform/index.html` mirror and `docs/.nojekyll`.
  `GET https://victorjimenez3.github.io/fable-job-search/platform/` now returns
  HTTP 404, and the Pages API returns 404. The automatic Pages run triggered by
  the teardown push failed at deployment because Pages was already disabled;
  this is expected and is not an application-test failure.
- **Vercel remains the production door:** commit `e63dd724` is live at
  `https://job-radar-newgrad.vercel.app/`; the gated test run
  `33276780846` and Vercel deployment run `33276822082` both passed, and the
  friendly alias verification returned HTTP 200.
- **Validation:** local Python tests (586), compileall, frontend typecheck,
  Vitest, ESLint, and the production frontend build all passed before publish.

### Cloud-first Resume Studio connection (implemented 2026-08-29)

- The production Resume Studio page now follows the intended engine-first
  flow: start the Mac service, then open the cloud workspace. It tries the
  exact-origin loopback API automatically for health, bank, matching, queue,
  context, and run-status requests, so the cloud page no longer depends on a
  popup being permitted during page load.
- The existing nonce-verified popup bridge remains the fallback when a browser
  blocks private-network loopback access. The offline notice now exposes a
  user-gesture **connect Mac engine** button, which lets that fallback open
  reliably. The local service still binds to loopback and sends CORS headers
  only to the explicit production Vercel allowlist; CV files and generated
  artifacts remain on the Mac.
- `webapp/index.html` is deployed directly by Vercel; there is no checked-in
  static mirror to copy. Validate the direct path with the health request and
  preflight checks, then run the full repository suite before publishing the
  production branch.

### Overleaf-style private Projects workspace (implemented 2026-09-01)

- Resume Studio now exposes a shared dependency-free Projects UI in localhost
  and the owner production page. It lists logical canonical/master/TLDP/
  historical/tailored references as read-only and keeps editable projects
  below `CV/.resume_studio/projects/<id>/`.
- Managed files use `source/`, `assets/`, `generated/`, and append-only
  `history/` directories. Autosave is SHA-256 optimistic-concurrency checked;
  stale writes return `409`. File validation rejects traversal, symlinks,
  unsupported/executable extensions, and the documented size/count limits.
- Local compilation stages an immutable snapshot and runs installed Tectonic
  with `--untrusted`, a sanitized environment, bounded timeout, and capped
  diagnostics. PDFs are labeled `workspace_draft`; they are excluded from
  Resume Bank, approval, Autopilot, and application fallback.
- Project routes require a short-lived process-local capability. They are not
  ordinary cross-origin CORS routes; the production page reaches them only via
  the existing nonce-verified popup bridge. Project source, filenames,
  manifests, histories, and absolute local paths are never sent to Vercel or
  Google Drive. If the Mac is offline, cloud Resume Bank remains read-only.

### Current change (2026-08-28)

- **Release and automation recovery hardened:** Vercel production deployment
  now follows a successful `tests` workflow, checks out that exact tested SHA,
  injects an immutable build marker, and verifies the friendly alias serves the
  same marker. The old independent push trigger could deploy while tests for
  the same commit failed. `workflow-recovery` now retries cancelled/timed-out
  runs once, reruns a complete cancelled run, paginates open repair-issue
  lookup, and escalates after the retry. Web actions and tracker sync retry
  from a fresh remote snapshot and re-run their idempotent operation instead
  of rebasing generated JSON. This is DECISION #202.
- **Production snapshot capacity repaired:** the 2026-08-28 14:28 UTC crawl
  completed discovery but GitHub rejected its 100.95 MB `state/jobs.json`, and
  its generic retry timed out rebuilding the same oversized blob. Generated
  job persistence now removes only reconstructible defaults and a duplicate
  `score_dimensions_raw` copy when it exactly equals `score_dimensions`.
  Nondefault values, disabled-dimension raw values, jobs, reasons, evidence,
  and lifecycle history remain intact. Writes preserve the previous snapshot
  and fail at 95 MiB so the workflow reports capacity before GitHub rejects a
  commit. This is DECISION #201; further growth requires sharding or the
  verified Postgres cutover, not a larger production limit.
- **Repository quality pass completed:** the production `radar/` package now
  uses one consistent Python 3.12 typing/timezone style, has correctness lint
  across the complete package, and removes concrete dead imports, ambiguous
  variables, unsafe exception handling, and duplicate entity tokens. The
  repository navigation now has a single Start here section, and the verified
  local contract is 579 tests, compileall, package lint, and the canonical
  Vercel frontend check.
- **Application Agent storage fallback fixed:** the Postgres application-state
  path now writes only its sanitized database payload. It no longer calls the
  Drive Markdown mirror without a Drive folder, which was the source of the
  `undefined.id` failure after a successful database write. The legacy Drive
  path still receives its readable Markdown companions, and the Sheet path
  remains the production `vmj@njit.edu` target.
- **Written-response scoping tightened:** reusable profile answers continue to
  fill deterministic choice/text fields, but `essay` and `cover_letter`
  controls require a session-scoped generated answer. Owner context is stored
  as `essay_context`, passed to the installed `warm-scholarship-essay` skill,
  and cannot be pasted directly into a new employer's essay. Queue-scoped cloud
  answers include their queue IDs, so tracker repairs cannot leak writing
  between roles.
- **Validation:** targeted application/cloud/essay/extension coverage is green
  (46 tests). The full suite is green (579 tests).

### Resume Studio local access (implemented 2026-08-27)

- Generated resume filenames now use `victor_jimenez_<company>.pdf` for every
  tailoring mode. The internal mode is not exposed in the filename; legacy run
  PDFs continue to work through a compatibility alias.
- The local engine keeps its auditable run history under
  `CV/.resume_studio/runs/`, while the newest usable primary PDF per company
  from the last 14 days is copied to the visible `CV/tailored/` folder.
  Failed runs and diagnostic `tailored_candidate.pdf` files are excluded.
  `CV/tailored/index.json` records each source run and whether owner review is
  still required.
- Refresh the folder manually with
  `.venv/bin/python -m radar.cli resume-studio export`; use
  `--all-history` to include the newest usable run for every company. Open the
  folder with `open CV/tailored`. The full walkthrough is
  [`RESUME_CLI.md`](RESUME_CLI.md).
- The CLI now exposes `resume-studio bank`, `offline-tailor`, `approve`, and
  `usage`. The bank excludes failed/base-winning runs and deduplicates identical
  PDFs. Offline tailoring ranks existing artifacts with deterministic role,
  title, keyword, sector, and approval signals, makes no provider call, and
  copies the unchanged selection below `CV/tailored/offline/` using the target
  company filename. The safe default requires an explicitly approved tailored
  winner; `--include-review` is an owner-inspection escape hatch.
- Application fallback now uses the same role matcher and requires real
  approval metadata. It no longer labels hard-coded NVIDIA/Google/Merck history
  as owner-approved. Historical resumes are not automatically inserted into
  Codex prompts: one explicitly promoted role-family control may remain a
  comparison reference, but the immutable resume/evidence graph stays the
  authoring source of truth.
- Validation for this change: 561 tests pass and Python compileall succeeds.

### Notion batch tailoring and platform editing (implemented 2026-08-27)

- The owner-only Resume Studio workspace now exposes **Tailor all To tailor**.
  It reads every current, non-terminal role whose latest repository-synced
  tracker stage is `to_tailor` (with tolerant `tailor`/`tailoring` aliases),
  preselects the full set, asks for explicit confirmation, and queues one
  private draft per role. It never marks a role Applied or submits an
  application. The cloud queue is bounded at 500 items; cloud submissions are
  serialized to avoid Drive JSON read/modify/write races, while direct local
  engine submissions use a small worker pool.
- **Edit resume** in Resume bank opens the existing protected local Workshop
  through the platform. Manual line edits render a new private revision without
  Codex, AI suggestions remain optional, and the original PDF/canonical source
  is unchanged. Saved entries without a content plan are labeled `no editable
  source` instead of being edited unsafely.
- The button uses the latest synced `state/applied.json` mirror; it does not
  make a live Notion API call from Vercel. If the count is stale, run the
  existing Notion tracker sync/backfill in an environment with `NOTION_TOKEN`,
  then refresh Resume Studio. Do not hand-edit generated state.

## Current operational facts (verified 2026-08-19)

### Current change (verified 2026-08-24)

- **Application queue storage now uses the editable `vmj@njit.edu` workbook:**
  production has no `DATABASE_URL`, so the owner path uses the connected user's
  bounded `Application Agent` Sheet tab. The old
  `victormjimenez2017@gmail.com` owner mirror is quota-locked and is no longer
  the application queue target. The legacy app-created Drive JSON/Markdown
  path remains a compatibility fallback, and the staged Postgres adapter stays
  dormant until a database is deliberately configured. No Victor files are
  deleted or moved.
- **Autopilot now gates form filling on Resume Studio:** the Mac extension
  checks for a safe tailored winner for the exact posting, starts the existing
  AI tailor run if needed, waits for its durable terminal result, and records a
  canonical-resume fallback when no safe tailored winner is published. Chrome
  still pauses for local resume-file selection, unknown answers, sensitive
  fields, attestations, changed pages, and final Submit confirmation.
- **Operational test:** the production storage path must be rechecked after the
  deployment by opening Autopilot and verifying the queue loads without a
  storage error and reports `storage: "sheet"`; no application submission is
  part of this test.
- **Pairing repair (verified 2026-08-24):** when Postgres is absent, the Mac
  executor now receives an opaque sealed pairing token carrying the already
  authorized personal Sheet grant. This keeps extension sync on the same
  `vmj@njit.edu` workbook instead of falling back to the expired owner mirror
  grant. Creating a new pair revokes the previous token; no OAuth secret is
  exposed to the extension.
- **Deterministic profile baseline (verified 2026-08-24):** on first local
  Application Agent use, the ignored local context bank imports only the
  canonical resume's owner-authored name, contact, school, and public profile
  links. A paired extension mirrors those answers into the private Application
  Agent Sheet. Essays, attestations, work authorization, resume-file selection,
  and final Submit remain explicit owner blockers. The employer-page banner
  labels these pauses as action needed rather than system errors.
- **Queue restart recovery (verified 2026-08-24):** the extension verifies the
  URL after opening a queued role and repairs an about:blank result. After a
  Chrome/extension restart, opening/filling items reattach to a matching
  application tab or are returned to queued; submitting items remain untouched.
- **Blank-tab guard (implemented 2026-08-24):** queue startup now creates a
  blank tab, explicitly navigates it, waits for a real web URL, and closes the
  tab if Chrome never navigates. A tracked blank tab is repaired during sync;
  roles without a live session return to queued instead of remaining falsely
  `filling`. A tracked page without an attached content session is only trusted
  during a short navigation grace period.
- **Preparation feedback (implemented 2026-08-24):** when the employer page is
  attached but Resume Studio is still checking or tailoring the role, the
  extension shows an explicit preparation banner rather than leaving the page
  visually idle.
- **Stale-posting guard (implemented 2026-08-24):** queue requests now carry
  lifecycle status and reject roles already marked expired or filled. A legacy
  queued role that opens an explicit “job not found”/“job has closed”/Workday
  “page … doesn't exist” page is marked failed before Resume Studio starts or
  any form field is filled. The guard runs before initial attachment and again
  on later DOM mutations because some ATS tombstones render asynchronously.
- **Candidate lifecycle filter (implemented 2026-08-24):** Autopilot's saved
  role picker omits jobs whose current radar posting is expired/filled. This is
  the first UI guard; the API and employer-page checks remain authoritative.
- **Visible queue controls (implemented 2026-08-24):** the production queue
  button disables and reads `queueing…` while writes are in flight. Opening and
  filling rows expose Stop; terminal cloud states detach the paired tab executor
  so a skipped role cannot be advanced or changed back to active on the next
  sync.
- **Worker wake recovery (implemented 2026-08-24):** module load, extension
  install/reload, and Chrome startup all recreate the one-minute alarm and
  immediately sync the queue. The popup is not required to restart queued work.
- **Controlled extension self-reload (implemented 2026-08-25):** the popup now
  displays the loaded extension version and provides a popup-only **reload
  extension** action. The worker acknowledges the request before calling
  `chrome.runtime.reload()`, then its module startup recreates the alarm and
  syncs the queue. It does not auto-reload on sync errors, so pairing and quota
  failures remain diagnosable. One manual reload is still required after a
  source-code change to load the new unpacked version.
- **Owner-page extension control bridge (implemented 2026-08-25):** the
  production Autopilot page now exposes **sync Mac**, **restart extension**, and
  **pair & sync Mac**. The installed content script relays only those
  commands, the worker checks the sender's exact production origin, and the
  page shows a timeout/error if the extension is absent. This removes the
  normal dependency on the Chrome extension manager; the unpacked extension
  still needs one manual reload after this source change so version `0.2.7`
  loads the bridge.
- **Resume/file and loop hardening (implemented 2026-08-26):** version `0.3.2`
  keeps real employer file values during planning, excludes cover-letter and
  supporting controls from resume upload, auto-requeues legacy resume/essay-only
  blocks, and pauses repeated form cycles after 12 scans in 45 seconds.
  Autopilot serializes extension actions and can renew pairing plus recover the
  queue in one explicit click.
- **Dashboard isolation (implemented 2026-08-24):** the content script returns
  after installing Job Radar's start-command bridge. It does not form-scan the
  production dashboard or keep the service worker awake with irrelevant DOM
  mutations.
- **Single-session attachment (implemented 2026-08-24):** page scans and
  background session creation are serialized per tab. This prevents a long
  Resume Studio run plus rapid ATS mutations from creating duplicate sessions
  and pruning the session ID returned to the page.
- **Application-tailor idempotency (implemented 2026-08-24):** a terminal run
  for the same application queue item is authoritative even when its winner is
  the base resume. Recovery returns the immutable fallback and does not launch
  another expensive tailoring run.
- **Duplicate-run recovery guard (implemented 2026-08-24):** engine startup
  does not recover a queued/running application-tailor duplicate when a terminal
  run already exists for that queue id. The duplicate becomes an inspectable
  `failed`/`duplicate_application_run` record.
- **Blocked-tab restart recovery and canonical-contact repair (implemented
  2026-08-24):** after an extension restart, blocked and review-ready queue
  items reattach to the most recently used exact-match employer tab without spawning a
  duplicate or blocking later queue work. Canonical profile seeding strips
  LaTeX comments, refreshes stale canonical-derived values, and syncs corrected
  canonical values back to the private Application Agent Sheet. Choice inputs
  remain owner decisions even when an option label resembles a profile field.
- **Choice-bank and blocker UI repair (implemented 2026-08-24):** the private
  context bank now drives radio, checkbox, select, and ATS button groups using
  exact option labels. Explicit owner-approved answers can cover work
  authorization, sponsorship, relocation, pronouns/gender, veteran status,
  disability, and race/ethnicity; an explicitly marked fallback is skipped
  whenever its preferred option is present. Optional demographic controls no
  longer block a queue item when no answer is banked. Ashby-style `data-option`
  buttons are extracted and filled, and the Autopilot queue now uses full-width
  grouped blocker cards with larger answer areas so only unresolved required
  fields and essays demand attention.
- **Owner-confirmed application context (implemented 2026-08-24):** the local
  private bank now contains Victor's confirmed Anchor Days, LLM experience,
  personal LLM project, Newark location, May 2027 graduation month/year, and
  undergraduate/bachelors degree choice, plus source-backed AI and technology
  responses for the active Notion form. Generic placeholders such as Ashby's
  `Start typing...` and `Pick date...` are matched through stored variants.
  The extension classifies LLM and Anchor Days button groups as their own
  categories and prioritizes category-specific answers when several groups use
  the same option label such as `Yes`.
- **Choice-loop and review-card hardening (implemented 2026-08-25):** short
  choices such as `Yes` and `No` are now reusable only within their inferred
  category, so a sponsorship answer cannot satisfy an unrelated employer
  attestation. The extension fails closed if the server ever returns two
  alternatives from one radio/button group. Review and blocker cards collapse
  option siblings and duplicate resume controls into one readable question,
  while retaining every raw field for page-bound verification.

### vNext foundation (implemented and verified 2026-08-16)

- **Staged product:** `/vnext/` is a React/TypeScript workspace with cursor
  pagination, virtualized Jobs, runtime API contracts, and responsive Jobs,
  Applications, Companies, Resume, and Settings routes. Jobs can use a
  server-side repository fallback; private and not-yet-migrated workflows link
  visibly to the classic UI, so cutover does not discard a feature.
- **Persistence:** `radar/db/schema.py` and Alembic own the normalized Postgres
  schema. `radar db import-legacy` performs an idempotent stable-ID import and
  reports parity; `radar worker` leases durable work with `SKIP LOCKED`, retry
  backoff, and terminal failure records. The worker is not scheduled until a
  production database exists.
- **Identity:** Better Auth is staged at `/api/v1/auth/*`, backed by the same
  Alembic schema and encrypted provider tokens. Existing owner-only APIs keep
  the legacy signed session during migration; do not switch them until the
  imported owner profile and both OAuth callbacks are verified end to end.
- **Crawler/scoring:** posting records retain exact source-board identity,
  source-gap expiry requires that same board's successful run, ATS APIs use
  bounded pagination, and evidence score / eligibility / personal priority are
  distinct auditable outputs. Legacy `score` remains compatible.
- **Tooling:** Python is locked with uv for 3.12, workflows share the pinned
  setup action where the checked-out branch supports it, and cross-branch ChemE
  jobs retain their requirements-based bootstrap. Hosted LLM routing is
  sequential and bounded; `RADAR_LLM_ADAPTER=litellm` is optional. Gmail
  lifecycle automation can use a read-only incremental History API cursor or
  the existing IMAP fallback.

Activation order:

1. Set `DATABASE_URL`, run `radar db migrate`, then `radar db import-legacy`
   and require the printed count/hash parity to pass.
2. Set Vercel `DATABASE_URL`, `BETTER_AUTH_URL`, and a random
   `BETTER_AUTH_SECRET` of at least 32 characters. Configure GitHub and/or
   Google callbacks at `/api/v1/auth/callback/github` and
   `/api/v1/auth/callback/google`.
3. Verify owner identity, applications, preferences, and rollback behavior in
   preview. Only then route private v1 APIs to Better Auth and schedule the
   durable worker.
4. Gmail is independent and optional: use `EMAIL_BACKEND=gmail_api` with
   `GMAIL_REFRESH_TOKEN`, `GOOGLE_AUTH_CLIENT_ID`, and
   `GOOGLE_AUTH_CLIENT_SECRET`, or retain the IMAP secrets.

Local verification contract now also includes:

```bash
uv sync --frozen --extra dev
uv run pytest tests/ -q
uv run python -m compileall -q radar tests
uv run python -m radar.cli db migrate --sql
cd webapp && npm ci && npm run typecheck && npm test -- --run && npm run lint && npm run build
```

### Latest change (verified 2026-08-16)

- **vNext is live behind the Hobby-safe Vercel function budget:** public v1
  reads, private migration endpoints, and Better Auth are dispatched through
  one `/api/v1/router` function with an explicit `/api/v1/:path*` rewrite.
  The production workflow waits for Vercel readiness before moving
  `job-radar-newgrad.vercel.app`, and the vNext HTML entry plus descendants
  receive the strict CSP. The live deployment returns v1 Jobs data, fails
  closed for unconfigured auth, and exposes no source/config files.

- **Owner-only objective Resume Bank comparison:** each grouped posting card now
  identifies the strongest finished variant for that posting using the
  auditable `objective-resume-v1` rubric (target fit, evidence safety, layout
  safety, portfolio signal). It exposes the score components and provenance,
  excludes failed/interrupted runs from winning, and reports missing
  independent review as a confidence limit. This is a private decision aid,
  not an automatic submission or a claim about an outside ChatGPT verdict.

### Current change (verified 2026-08-16)

- **Application history posting duration:** the classic History tab now shows
  how long each application’s posting was up in days, months, or years instead
  of showing the application lifecycle date. It uses the posting’s observed
  start and last-seen/closure timestamps and keeps the separate Posting history
  timeline unchanged.

- **Resume Studio visual ATS audit and contextual Q&A hints:** Resume Bank
  metadata now carries the report's compact keyword coverage and PDF line
  geometry through local and private-cloud paths. The owner UI renders green
  covered, yellow supported-but-omitted, and red unsupported terms, plus an
  optional review-only overlay on the clean preview. Context questions derive
  candidate project/role/course places from claim-authorized neighboring
  evidence and accept owner-supplied labels/URLs. A place-specific dismissal
  suppresses a false lead without answering the whole capability. Every hint remains
  `claim_allowed: false`; only a concrete affirmative owner answer creates an
  evidence node. The private CS485 Nexus CI/CD lead is seeded as an open hint,
  not a resume fact, and J&J is already recorded as ruled out for that
  capability. Existing old bank reports without geometry degrade to the
  term list or an explicit unavailable state.

- **Resume Studio review-first surface (verified 2026-08-22):** the cloud
  posting workspace now exposes a Posting → evidence map before queueing, with
  matched capabilities, preferred matches, explicit gaps, confidence, and
  surfaced source labels. Completed runs show the same requirement map and
  immediately render the saved ATS term/line overlay inside the result panel.
  Grouped Resume Bank cards use the objective winner's preview as their primary
  image, while keeping the newest and all historical versions expandable. The
  map and overlay are diagnostic only; unsupported terms remain gaps and the
  clean PDF remains unmodified.

### Current change (verified 2026-08-23)

- **Opt-in permanent role-family controls:** the cloud Resume Bank stores a
  private `resume-studio-control-profiles.json` registry in the owner's Drive.
  The immutable canonical resume is always present as the safety floor. A
  role-family control can be promoted only from a synced, complete,
  owner-approved run whose published winner is the tailored artifact and whose
  PDF is present. The cloud queue carries only a sanitized control reference;
  the local engine resolves it against the private run directory and falls
  back to the immutable default if the reference is missing, revoked, stale,
  unapproved, or lacks the source PDF/TeX. The selected control is a secondary
  comparison reference, so its supported-term and signal-family diff is
  visible without weakening the existing canonical audit gates. Promotion is
  per family and revokes the prior active profile while retaining history.
  No Google/Merck historical draft is auto-promoted.

### Current change (verified 2026-08-17)

- **Resume Studio effort profile and latency closure:** the local engine keeps
  Codex pinned to `gpt-5.6-luna` and uses one consistent High effort level for
  ordinary planning, writing, line editing, and every evaluator role. Max is
  reserved for the explicit deep quality frontier or a deliberate override.
  A single line-repair pass now precedes the deterministic compactor; the
  second frontier repair pass was removed after controlled runs showed
  diminishing returns. Every provider-flow row records the selected effort
  alongside model, latency, status, and observed tokens.

### Current change (verified 2026-08-21)

- **Sealed evaluator and broad quality lab:** Resume Studio now uses
  the `resume-evaluator-v2-sealed` critique-only contract. The writer supplies
  only an attested packet containing the base/candidate renders, posting,
  authorized evidence, deterministic checks, and comparative diff. A fresh
  evaluator process receives no writer prompt, prior review, readiness state,
  or score control and runs from a disposable system-temp directory. Four
  roles—evidence, recruiter, technical, and screening—must all return valid
  attested results; duplicate, wrong-lane, or partial panels remain unready.
  The deep authoring lane and evaluator roles may use Luna Max; the ordinary
  balanced and Unchained lanes use Luna High for every provider stage,
  including all evaluator roles. Repeated writer repair calls use the same
  explicit High lane. In the bounded balanced lane, critic-driven repair is disabled;
  repair candidates in the deep lane receive a fresh panel
  before acceptance. The benchmark
  harness fetches/matches a broad live corpus concurrently, then runs a
  sector/company-balanced full sample under `CV/.resume_studio/benchmarks/`.
  Its manifest checkpoints each terminal run, distinguishes quality rejection
  from execution failure, and records the active evaluator contract. The
  parent audit collapses repeated panel prose with supporting-role counts and
  keeps candidate-role gaps separate from unsupported claims or layout gates.
  A completed Merck-control comparison used the saved
  `CV/.resume_studio/runs/b6bf060e3a04/merck_resume_ai.pdf` as the provisional
  baseline for a fresh Stryker candidate. All four sealed roles completed and
  recommended `prefer_base_merck`: factual/privacy passed, but the candidate
  lost high-information evidence, repeated AI/RAG/backend stories, and failed
  the consensus near-wrap check. This is a qualitative pairwise control, not a
  score or an owner approval of the Merck draft.
  No evaluator result may be altered to force a pass. The broad cohort
  selected 86 fetched/matched postings and launched a 12-role full sample
  with two concurrent full-run workers and 16 match workers. The final
  receipt is 8 completed full runs with complete four-role panels and 4
  honest hard-layout rejections; two earlier abandoned drafting directories
  were excluded from those counts. The completed tailored runs were still
  mostly `do_not_ship`/`blocked`, which is an evaluator finding rather than a
  harness success claim.

### Current change (verified 2026-08-22)

- **Source-level provenance guard:** enhanced rewrites now revert a narrow set
  of high-risk technical/implementation anchors when the primary bullet and
  its cited `source_ids`/`evidence_ids` do not authorize them. This catches the
  Anduril SynapSense failure where C++/asynchronous/backend wording was merged
  into a dashboard bullet with only the dashboard citation. A fresh replay at
  `CV/.resume_studio/benchmarks/provenance-fix-anduril-20260822/runs/pending-1787393270`
  completed in 551.4 seconds with High Luna calls and all four critic roles;
  it produced no added unsupported bullet and correctly selected the base.

- **Canonical-loss explanation tightened:** control recovery restores every
  omitted canonical line that has a noncanonical selected line to displace,
  unless the ledger names the exact source tradeoff. A generic project reorder
  no longer explains a lost bullet; the audit needs the exact source ID or
  enough of the omitted line to identify it. This keeps the evaluator from
  hiding a lost mechanism behind a valid project-level narrative. Focused
  Resume Studio coverage is now 154 tests.

- **Supported-skills generation checklist:** generation prompts now include a
  compact checklist of direct/adjacent `tailor_skills` requirements with exact
  terms and authorized evidence IDs. The writer may surface a term in cited
  body evidence or one existing Skills rewrite, but the checklist never
  authorizes unsupported additions. The fresh-open Nucleus replay at
  `CV/.resume_studio/benchmarks/skills-checklist-nucleus-20260822/runs/pending-1787394155`
  completed in 609.5 seconds with Luna High and all four critic roles. It added
  supported REST/access-control evidence and no unsupported claims, but still
  omitted authorized documentation/PostgreSQL/Linux/AWS signals and dropped
  the distinctive Quantum project; the audit correctly kept the base/blocked.

- **Planner denial normalization:** postfix negative language such as “Linux
  administration and AWS are unsupported” is now recognized before gap terms
  are promoted into supported ATS/generation evidence. The corrected Nucleus
  replay at
  `CV/.resume_studio/benchmarks/denial-fix-nucleus-20260822/runs/pending-1787395673`
  completed in 570.8 seconds with Luna High and all four critic roles. Its
  checklist contained documentation and Docker only; the audit moved from
  `tailoring: regressed` to `tailoring: improved`, with no unsupported rendered
  claim, but correctly stayed `do_not_ship`/blocked for eligibility and a
  remaining portfolio regression.

- **Canonical project tradeoff preflight:** enhanced plans that drop a
  canonical project must name every omitted canonical bullet `source_id` in
  the decision ledger. A parent project explanation cannot hide a missing
  mechanism. The Stryker replay would now fail closed on the omitted Quantum
  historical-market pipeline bullet (`...:b3`) before judging; valid swaps
  remain allowed when their source-level tradeoff is complete. Focused Resume
  Studio coverage is now 156 tests.

- **Requirement-term association guard:** gap-analysis terms now survive
  normalization only when they appear in the requirement, its rationale, or
  cited authorized evidence. This blocks the Stryker failure where generic
  `software engineering` was attached to networking and AngularJS requirements;
  deterministic inventory terms remain available independently. Focused
  Resume Studio coverage is now 158 tests.

- **Portfolio-search hard-gate promotion:** a three-variant Stryker search
  exposed that a candidate could receive `prefer_tailored` from the comparative
  audit while the sealed panel still reported a hard failure. Portfolio search
  now requires a complete four-role panel with `review.hard_fail == false` in
  addition to the positive comparative decision. Each child receipt records
  `critic_hard_fail`, so relative uplift and promotion safety remain separate.
  The post-fix Stryker replay completed three candidates in 388.5 seconds wall
  time with complete Luna High panels and selected the canonical base because
  none produced a material positive win.

- **Final-artifact portfolio guard:** late revision, density, and audit-repair
  passes can return a new source-addressed plan after the initial packer and
  sealed panel. The exact final plan now passes one deterministic duplicate and
  human-skim-budget guard. Same-entry repeated metric/mechanism stories keep
  the stronger authorized line; no new terminology or claims are created. If
  the guard changes the plan, Resume Studio compiles it and runs a fresh
  complete four-role sealed panel. It never reuses the prior panel for a
  changed artifact; an incomplete recheck fails closed. Receipts are written
  to `final_portfolio_guard.json` and `layout_packing.json`, and focused tests
  cover both the stronger-line selection and duplicate metrics diagnostic.

- **Mechanism-story duplicate guard:** the same final guard now catches
  repeated validation/calibration mechanisms even when the bullets do not
  repeat a number. Cross-entry deletion is stricter than same-entry review and
  requires two distinctive shared terms with a shared metric, or the narrow
  mechanism bundle; generic words cannot erase a project. This specifically
  targets the ByteDance All-NBA and Anduril posture regressions found in the
  fresh cohort. The affected roles are being rerun before this is treated as a
  measured gain.

- **Geometry fallback after High timeout:** if the bounded line editor times
  out, the deterministic compactor may now restore a shorter authoritative
  source line and use source-preserving Skills abbreviations before failing the
  one-line gate. This rescued the exact failed Anduril artifact in an offline
  compile check (zero wraps/near-wraps); the real-role rerun still must pass a
  complete sealed panel.

- **Post-audit density prerequisite receipt (DECISION #155):** the patched
  Stryker run at
  `CV/.resume_studio/benchmarks/stryker-post-repair-density-20260821/runs/78044c21145b`
  completed in 3,557.3 seconds with 26 Codex Luna calls. All four sealed
  roles completed in each recorded round. Its audit repair failed the
  one-page geometry prerequisite, so post-repair density was correctly
  `not_run`; final geometry recovery restored three authorized source lines
  and passed a fresh panel. The tailored version still remained
  `do_not_ship`/`blocked` with zero gains, loss weight 15, five regressions,
  one blocker, and twelve questions, so the immutable base became the primary
  artifact and the candidate remained diagnostic. The code now records this
  prerequisite outcome explicitly instead of leaving an ambiguous absence in
  `audit_repair_log`.

- **Compiled space search is bounded:** measured-capacity expansion still
  permits direct additions, single removals, and two-bullet swaps, but its
  deterministic removal frontier is capped at the top four candidates. This
  avoids spending most of a run compiling equivalent low-value combinations;
  the evaluator, source authority, geometry threshold, and acceptance rules
  are unchanged. The merged production change is PR #2583 (`48b7d479`).

- **Control-preserving balanced lane and geometry-first timing experiment:**
  the default enhanced run is now `quality_profile=balanced`. It uses Luna
  High for the sealed four-role panel and leaves all evaluator contracts and
  hard gates intact, but skips model space expansion and critic-driven
  revision/audit-repair rounds. The deterministic compiler performs control
  recovery first and a conditional Luna High line-edit pass is used only when
  geometry is unsafe; `deep` preserves the previous two-round/model-expansion
  behavior for controlled comparisons.
  Canonical high-information evidence is now supplied as a bounded control
  receipt and receives a compile-time removal bonus; panel-confirmed added
  strengths are the only new positive audit findings. A repair candidate that
  fails deterministic geometry is recorded and rejected without spending a
  four-role sealed recheck on an artifact that cannot ship. The fresh paired
  Stryker/control benchmark is complete under
  `CV/.resume_studio/benchmarks/quality-timing-experiment-20260821/`.

- **Stryker quality/timing experiment completed:** the old deep Stryker run
  (`.../runs/78044c21145b`) took 3,557.3 seconds and 26 Luna calls and still
  selected the immutable base. The bounded complete-panel run
  (`.../runs/6706937669e7`) took 635.5 seconds and 6 calls, with all four
  sealed roles complete; it selected the immutable base because the candidate
  was negative uplift (gain weight 8, loss weight 19 before the parser fix),
  with repeated AI/RAG evidence, lost 400+ presentation proof, duplicated
  validation lines, and omitted stronger UI/REST/dashboard/documentation
  evidence. The candidate remains at `tailored_candidate.pdf` for diagnosis.
  A separate run (`.../runs/2d6e5155cf93`) exposed a screening-role timeout;
  the selector is now fail-closed so an incomplete panel can never promote a
  tailored artifact. The same sealed findings replay to `review/prefer_base`
  after ignoring an explicit “not a claim made” sentence as a grader
  false-positive; the base still wins for substantive regressions. Receipts
  and the post-fix replay are in the benchmark directory, and the fixed
  evaluator semantics are covered by the focused test suite.

### Current change (verified 2026-08-23)

- **Resume Studio runtime and queue recovery:** the local engine now records a
  source fingerprint, evaluator contract, process ID, and worker count in
  health/run metadata. A launchd process whose loaded source differs from the
  checkout reports `restart_required` and refuses new queue requests instead of
  running an outdated evaluator contract. On startup, the bounded worker pool
  scans durable run snapshots, resets abandoned `running` work to `queued`,
  and resumes both queued and interrupted runs; completed, failed, and
  owner-review runs are not replayed. The default remains two workers, with a
  service-install override capped at four. A narrowly recognized historical
  `cannot schedule new futures after interpreter shutdown` record is repaired
  once as queued so the shutdown hardening does not strand work created by the
  old daemon.

- **Truthful queue status:** the library no longer relabels an old queued or
  running run as `interrupted` merely because its timestamp is older than 30
  minutes. It preserves the actual persisted state and exposes age/staleness
  as separate metadata and an attention warning, so backlog age cannot be
  mistaken for a terminal failure. Focused Resume Studio coverage is now 163
  tests. Provider launchers are tracked and terminated with the service, so a
  launchd restart does not leave orphaned Codex calls behind. The launchd
  plist now gives graceful shutdown 30 seconds and the installer no longer
  force-kills a freshly bootstrapped service; the SIGTERM path snapshots,
  reaps providers, cancels queued futures, and exits without an interpreter
  atexit join.

### Current change (verified 2026-08-23)

- **Owner-only Application Autopilot:** `radar/application_agent.py` now owns
  a deterministic local form decision engine and durable context/session/issue
  bank under ignored `CV/.resume_studio/application_agent.json`. The existing
  loopback Resume Studio service exposes `/api/application/*` routes and the
  Chrome extension sends visible field structure only. Named adapter hints
  cover Workday, Greenhouse, Lever, Ashby, and SmartRecruiters; generic pages
  remain conservative.
- **Private Drive control plane:** `webapp/api/application-agent.js` stores
  `application-context.json`, readable `application-context.md`,
  `application-queue.json`, and a sanitized issue ledger in a separate
  app-created private Drive folder. The owner UI edits context; Markdown is a
  mirror. A hashed, revocable one-time pairing token lets the Mac extension
  poll the queue without receiving the owner's session cookie.
- **Review boundary:** approved answers may autofill, including approved
  sensitive answers, but unknown required/essay/sensitive fields, file uploads,
  attestations, and adapter failures block. The extension advances ordinary
  multi-page steps, never clicks Submit until a full owner review is confirmed,
  and verifies the page fingerprint again immediately before the click. Phone
  confirmation is single-use and expires after 15 minutes. A queued batch is
  sequential per browser; blocked roles park while later roles continue.
- **Validation:** focused application-agent tests cover context reuse,
  sensitive blockers, final review/fingerprint checks, issue persistence, and
  the owner/Drive/extension contract. The new unpacked extension lives under
  `browser-extension/`; no extension ID or provider secret is committed.

### Current change (2026-08-25, pending production verification)

- **Automatic local resume upload:** Resume Studio now resolves the exact safe
  tailored PDF or an owner-authorized Google/NVIDIA/Merck/base fallback and
  serves its bytes only over loopback. The extension creates the browser
  `File`, fills the employer upload control, and mirrors only the filename and
  progress to the cloud queue. Resume PDFs do not consume Drive quota.
- **Concurrent durable execution:** the paired Mac may run up to three batch
  tabs. Blocked and confirmation-ready roles park without consuming a slot;
  each row exposes resume choice, stage progress, employer-page access, and a
  durable continue signal for a manual step.
- **Written responses:** new essay and personal-response blockers call the
  installed `warm-scholarship-essay` skill through the existing local Codex
  subscription lane. Exact prompts, limits, role context, profile, and
  claim-authorized private evidence are supplied. Unsupported responses remain
  visible blockers instead of being invented.
- **Resume upload hardening:** the extension now chooses one empty,
  required/named resume input when an ATS renders duplicate file controls. It
  does not replace an accepted `File` object on later DOM scans and waits for a
  clean follow-up scan before advancing a Next step. The local session records
  a normal upload-validation progress message so Autopilot does not present a
  transient employer upload wait as a failure. Extension source changes also
  deduplicate the floating status banner and force long messages to wrap, so
  an extension reload cannot leave overlapping preparation messages. The
  unpacked extension needs one final reload after production verification.

### Current change (verified 2026-08-19)

- **Fresh new-grad action queue:** the classic platform defaults New-grad Jobs
  to entry-compatible or unclear experience and a one-month Best Match window.
  Tracked/Maybe roles remain visible when they are older or experienced so
  saved work is not lost; explicit filters expose the broader research board.
  Expired and filled rows remain in History, not active Jobs.
- **Score verdicts survive company concentration:** full rescoring now resets
  only the prior concentration adjustment. Posting/quality/lifecycle
  demotions remain on the row instead of being promoted back to a sibling's
  calibrated score. The reason ledger still records both the verdict and any
  diversity adjustment.
- **Experience verdicts reach the displayed score:** when posting text is
  fetched during a crawl, the in-memory row now receives the deterministic
  `years_min` penalty immediately as well as `alert_ok=False`; this keeps a
  high score from surviving until a later rescore. `python -m radar.main
  rescore` is the documented repair for older analyzed rows. Committed JSON is
  compact so that that full rebuild remains under GitHub's blob-size limit.
- **Jobright closed-page signal:** a bounded Jobright resolver now recognizes
  the definitive 200-page banner “This job has closed.” as expired evidence,
  stores the page-signal version/reason, and prevents that sighting from
  reopening the role during the same crawl. Older no-direct caches are
  rechecked once under the new signal. `resolve-links` reports closed rows
  separately from transient errors.
- **Stale-link maintenance:** `radar.yml` now runs a separate pre-crawl repair
  job that checks up to 800 still-open aggregator rows with bounded parallel
  workers and commits terminal verdicts before full discovery begins. The
  repair job retries against fresh upstream state on a push race, so a slow or
  timed-out full crawl cannot starve the closed-posting cleanup.
- **Owner Resume Studio batch:** **Tailor today** selects up to 12 roles added
  to the Pipeline on the local calendar day, queues one chosen mode through
  the existing private engine bridge, and never submits or marks applications.
  Successful queueing moves the application to the new `to_tailor` stage. The
  engine's existing two-run concurrency and bank/review gates remain in force.
  Notion's status option must be added manually because the API cannot create
  status options; local state and Google tracker sync still work without it.
- **Canonical posting families:** exact canonical URLs remain the strongest
  identity boundary, followed by a conservative company/title-family pass for
  official-versus-aggregator variants and a same-board/same-posting-day pass
  for marked location fan-outs. Survivors retain all locations, alternate
  URLs, `posting_family_id`, and `posting_identity.matched_by` audit reasons.
  Ambiguous same-title direct requisitions remain separate. `resolve-links`
  runs this repair after bounded liveness work, remaps applied/shortlist/web
  references, and queues duplicate Notion pages for reversible archival.
- **Notion archival retry fix (verified 2026-08-20):** archival probes the
  page first, treats pages already in Notion trash as an idempotent success,
  and uses the current `in_trash` update field for live pages. Production
  tracker sync archived four terminal/duplicate pages with no remaining local
  Notion archive errors. The live Applications database is readable, but its
  `Stage` status options still need a manually-created `To tailor` option.
- **Integration checks (verified 2026-08-20):** `notion-verify` passes against
  the `2026 Applications` database. `email-verify` is currently blocked because
  production has neither IMAP `EMAIL_ADDRESS`/`EMAIL_APP_PASSWORD` nor the
  Gmail API refresh/OAuth credentials; add one supported credential set before
  relying on email-based application-stage advancement.

### Current change (verified 2026-08-20)

- **Resume Studio cold-start reliability:** the Mac launch agent now retries a
  transient `launchctl` bootstrap race and waits for the private loopback health
  endpoint before reporting installation success. The engine uses the repository
  Python 3.12 virtualenv and the local provider CLI path, so the Codex Luna
  lane is visible after login. When the persisted job snapshot already has
  the active score version, Resume Studio reuses that crawler projection instead
  of rebuilding all 45k records; stale records still take the compatibility
  rebuild path. A clean restart now serves `/api/jobs` in under a second and the
  exact NVIDIA job endpoint immediately.
- **Hosted-engine boundary verified:** the Vercel cloud Resume Studio control
  plane is live and retains the owner/session guard, but the private execution
  engine remains a loopback Mac companion by design. It reads the local CV and
  provider sessions and writes private artifacts, so moving it to a stateless
  Vercel function would require a new remote privacy/auth/storage architecture.
  No CV, provider credential, or generated artifact was uploaded as part of this
  repair. Production's safe fallback remains available when the Mac is asleep.
- **Private cloud queue shipped:** the same owner-only `/api/resume-bank` route
  now stores a bounded `resume-studio-cloud-queue.json` in the app-created
  private Drive folder. Single and **Tailor today** batch requests can be
  saved while the Mac is offline; only sanitized posting metadata, mode, and
  status cross into the queue. Once the local engine reports healthy through
  the existing bridge, the open production workspace dispatches up to two
  items and mirrors `queued`/`running`/`awaiting_review`/`complete`/`failed`
  state. The CV, evidence graph, provider sessions, and generated artifacts
  remain local. A production browser tab must stay open for reconnect dispatch.
- **Comparative tailoring audit shipped:** each new local run now writes
  `job_intelligence.json` and `tailoring_audit.json`. The audit separates actual
  candidate fit from base → tailored communication quality, reports supported
  gains/lost or unused evidence and change-level regressions, and keeps
  factuality, eligibility, layout, privacy, and independent-review failures as
  visible readiness blockers. The local report contains the full evidence and
  hashes; Resume Bank/cloud queue views receive only a sanitized summary plus
  `queue_id`/`run_id` correlation. `ready`, `review`, and `blocked` replace a
  misleading universal ATS score for this quality-control decision. Existing
  objective same-posting ranking remains a separate shortlist aid.
- **Comparative audit v2 and repair gate (verified 2026-08-20):** source-aware
  tradeoffs now distinguish an explained project replacement from an actual
  lost evidence signal, and low-priority base-context omissions are advisory
  rather than automatic regressions. Each report exposes `recommended_version`
  (`tailored`, `base`, or `review`) plus a `decision` (`prefer_tailored`,
  `prefer_base`, or `needs_review`). If deterministic audit finds a material
  regression, one bounded Codex repair pass proposes a complete replacement
  plan; the worker compiles and compares it, accepting it only when it improves
  the source-aware preference key and passes the one-page geometry gate. A
  missing critic-panel result remains `review`, never silently `ready`.

### Current change (verified 2026-08-21)

- **Codex Luna is the sole Resume Studio provider:** `provider_commands()` now
  exposes only `codex`; every provider call pins `gpt-5.6-luna`, uses the
  first-party subscription CLI, and rejects local-model, arbitrary endpoint,
  API-key, or second-provider fallbacks. The live loopback health check reports
  `codex: true` and no other provider lane.
- **Evaluator roles are real, durable sub-runs:** enhanced tailoring launches
  four concurrent, role-separated critic calls—`evidence`, `recruiter`,
  `technical`, and `screening`—then combines their critique-only outputs. Each
  call has its own prompt, schema, transcript, usage record, and durable label
  such as `critique_evidence`. The panel is a same-model Luna jury, not an
  independent-vendor claim; old saved reports remain readable through the
  legacy compatibility fields.
- **Readiness uses the jury, not a fake vendor gate:** `critic_jury` is the
  required review gate. The old `independent_review` field is retained only as
  an explicit `partial` compatibility alias for same-model reports and cannot
  downgrade a completed Luna jury by itself. A missing role or unusable
  structured response leaves the run in review.
- **Final geometry recovery is still a hard gate:** if an audit repair leaves
  a wrapped or near-wrapped bullet, the worker may restore that bullet's exact
  authorized source wording and try bounded deterministic compactions. The
  recovered artifact is sent through a fresh complete sealed panel; an unsafe
  or incompletely rechecked candidate is rejected, never promoted by the
  recovery path. The recovery receipt is stored as
  `final_geometry_recovery.json`.
- **The audit decision now controls the primary PDF:** if the comparative audit
  recommends `base` or marks the tailored artifact `blocked`, the immutable
  canonical PDF becomes the run's primary winner. The generated candidate is
  retained as `tailored_candidate.pdf`, alongside `base_control.tex` and a
  `winner_artifact` receipt. This prevents a rejected tailored draft from
  being mistaken for the recommended version without changing the evaluator.
- **Repair writers now receive explicit control losses:** the feedback packet
  lists unexplained canonical bullets, supported terms lost from the base,
  project swaps, and portfolio-overlap warnings. Repair rules restore or
  justify high-value control evidence before adding another keyword-shaped
  line; the evaluator and its hashes remain unchanged.
- **Unchained frontier calibration (verified 2026-08-21/22):** the fresh
  Stryker generation run took 1,596.4 seconds and 12 Codex Luna calls; all
  initial and recheck evidence, recruiter, technical, and screening roles
  completed, but the immutable base still won the comparative audit. The
  candidate's UI/REST additions were genuine, while stronger proof and
  portfolio breadth were lost, so this was an honest quality rejection rather
  than a worker failure. The audit now repairs provider-typo citations only to
  exact graph-authorized source IDs, labels material no-op candidates as
  `unchanged` and skips their repair cascade, clusters semantically identical
  panel concerns, and treats a single non-hard-role regression as
  `QUESTIONABLE` pending confirmation.
- **Bounded Unchained opportunity pass:** before the sealed jury, generation
  may test one unused, claim-authorized, source-verbatim line from an already
  selected entry, paying for it with a bounded marginal replacement and a
  fresh compile/layout gate. It cannot invent wording or approve the change.
  Unchained's old audit-repair/recheck cascade is disabled after adding about
  twelve minutes without a positive Stryker comparison; `deep` and `search`
  retain repair for controlled experiments. This improves the default timing
  path without weakening the four-role High evaluator or any hard gate.
- **Integrated-path Stryker validation (verified 2026-08-22):** run
  `CV/.resume_studio/benchmarks/unchained-frontier-validation-20260822/runs/88ff48da72e5`
  completed in 909.2 seconds with 7 Codex calls. The target opportunity pass
  surfaced authorized REST endpoint evidence, all four Max roles completed,
  and the audit preferred the tailored artifact with gain weight 7 versus loss
  weight 4 and no blockers. The same report retains two regressions (lost
  J&J Pandas/SQL evidence and lost 4+ agent coordination) plus one missed
  `software engineering`/UI opportunity; it remains `awaiting_review`, not
  auto-approved, because the panel is same-model Luna rather than independent
  hiring validation.

- **Fresh-open High-effort cohort and speed bound (verified 2026-08-22):**
  `CV/.resume_studio/benchmarks/20260822T041401Z-0b2ad7/manifest.json` selected
  roles listed within seven days, excluded terminal records, rejected definitive
  closed-page banners, and completed 8/8 full runs. Every run completed the
  evidence, recruiter, technical, and screening roles; 5 preferred the tailored
  candidate and 3 correctly stayed base/blocked. Every provider call used
  `gpt-5.6-luna` at High effort, with per-stage latency and total elapsed time
  recorded in the manifest. The receipts showed optional line editing and
  repeated measured-space trials as the timing bottleneck, so ordinary balanced
  and search lanes now use a three-minute line-editor fallback, two post-line
  density rounds, and a two-candidate compiled swap frontier. Deep retains the
  larger frontier for explicit quality experiments; the evaluator contract and
  fail-closed winner selection are unchanged.

- **Post-fix evidence guard spot check (verified 2026-08-22):** two additional
  fresh open roles (Neuralink and Qualcomm) completed in roughly 653 seconds
  each with complete four-role High panels and no Max calls. Neuralink was a
  `prefer_tailored` review; Qualcomm was correctly `do_not_ship`/`blocked` for
  an unsupported `pytest` Skills addition and a harmful project tradeoff. The
  follow-up patch now rejects newly introduced Skills technologies unless the
  cited claim-authorized evidence actually supports them, and removes repeated
  metric/mechanism anchors within one entry before packing.

- **Microscopic-density swap removed (verified 2026-08-22):** portfolio-search
  receipts showed that the deterministic post-edit density pass could see only
  `0.03--0.06pt` of spare capacity and replace distinctive mechanism evidence
  with unused but weaker lines. The sealed jury rejected those candidates, but
  the swap was still wasted authoring latency and made the candidate worse.
  Ordinary `balanced`, `search`, and `search_single` profiles now disable
  deterministic content expansion while retaining hard geometry gates,
  source-aware control recovery, and the complete sealed panel. The explicit
  `deep` frontier remains available for controlled experiments. The first
  post-fix fresh-open Uber run used Luna High throughout, completed in 631.9
  seconds, and earned `prefer_tailored` with zero blockers; its final PDF
  preserved the authored portfolio because no blind density swap ran. The
  Nucleus follow-up completed in 675.0 seconds with all High panel roles and
  correctly stayed base/blocked on application constraints.

- **Role-evidence floor remains experimental:** the project-level floor can
  find an omitted primary-track project, but the Anduril experiment showed
  that a plausible replacement can still displace more distinctive evidence.
  It is therefore disabled in ordinary and search profiles; its receipt says
  `disabled_by_quality_profile` and the helper remains available for explicit
  lab tests. It will need a sealed positive win before becoming a default
  mutation. A same-job ByteDance A/B supports that decision: floor enabled
  produced loss weight 24 plus an unsupported-claim blocker; no-floor High
  replay produced loss weight 14, no unsupported-claim hard failure, and still
  correctly stayed base for eligibility and genuine portfolio redundancy.

- **Unchained fail-closed stress test:** the post-fix Anduril Unchained run
  completed in 601.0 seconds with Luna High throughout. It surfaced a
  supported Python/simulation opportunity, but the writer broadened SynapSense
  into unsupported asynchronous/Python-module and behavioral-monitoring
  claims. The sealed evidence panel caught those claims plus the unresolved
  experience and clearance blockers, leaving the immutable base as the primary
  `do_not_ship`/`blocked` artifact. This is expected quality-control behavior,
  not a successful-tailor claim.

### Previous change (verified 2026-08-15)

- **Resume Studio grouped bank and context loop:** the cloud Studio uses the live Job Radar
  shell and page layout while retaining the local Studio's four mode buttons,
  full bank filters, posting snapshots, reports, PDF/preview links, and
  Workshop handoff. The owner-only `/api/resume-bank` stores a synchronized
  index and generated artifacts in an app-created private Google Drive folder.
  The bank groups versions by posting and defaults to the Google-onward quality
  era; **all history** remains available, and sync follows that scope.
  **Context & Q&A** deduplicates unsupported posting terms into durable private
  questions. Only a concrete affirmative owner answer becomes evidence; known
  absences remain non-authorizing. The context inventory shows source,
  authority, and confirmation while remaining local and excluded from sync. Cloud
  artifacts are served through an owner-session proxy with private/no-store
  headers. New matching, generation, and Workshop edits still require the
  loopback Mac engine, so the cloud remains a safe fail-safe when that engine
  is asleep.

### Earlier change (verified 2026-08-14)

- **Unified cloud Resume Studio:** the owner-only production platform now has
  one Resume Studio workspace instead of a separate cloud title-preview page
  plus a local launcher. Jobs rows, role drawers, and the Resume Studio tab
  select the same posting; the workspace shows private-engine health, source
  match, Used bullets / AI tailor / Take-the-wheel / Unchained generation
  queues, run status, and bank links. It calls the loopback Mac engine through
  a narrow production-origin postMessage bridge to the compact loopback engine
  window. When the Mac is unavailable, cloud
  ranking, posting links, and apply flow remain usable while draft controls
  stay disabled. The source CV corpus, provider sessions, and generation
  execution remain local-only under `CV/.resume_studio/`; explicit bank sync is
  the only path that copies generated artifacts to the private cloud Drive
  proxy, and no private artifact is committed or exposed by bridge health.

### Earlier operational facts (verified 2026-08-08)

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
  UX/UI researcher, solutions architect, and product management) for the
  PM-prioritized slice of the registry. The slice defaults to 200 companies and
  is controlled by `RADAR_PM_BACKFILL_COMPANIES`; all other direct ATS polling
  keeps the normal query list. Amazon, Microsoft, Apple, and Google receive the
  same PM-family query fan-out; Amazon drops its technical category restriction
  only for those PM queries. This preserves recall where PM evidence is
  strongest without pushing the full crawl past its time budget.
- **Technical internship lane (DECISION #104):** the main platform now has a
  visible New-grad / Internships switch. Internship state is namespaced as
  `state/intern_*.json`; the lane reads curated Simplify, SpeedyApply, Zapply,
  and Dreamwork GitHub feeds plus internship-specific ATS searches. Its
  two-hour crawl, alert delivery, master board, checkbox reconcile, and
  `docs/internships/` outputs are separate and lower-budget so new-grad
  compute remains first. A viewer's expected graduation month is stored only
  in the private Google Preferences tab/local browser and drives deterministic
  freshman/sophomore/junior/senior matching. Internship email batches default
  off; new-grad batches default on; both are owner toggles and neither uses
  Gmail scope. Internship rules v6 uses a neutral friend-facing 0–100 rubric: role
  families are flat, while prestige/crackedness, normalized pay, cited employer
  evidence, work quality, student evidence, and freshness contribute. Victor's
  new-grad preferences, feedback, remote setting, and personal-signal sample
  are excluded. Unknown pay, employers, and work evidence are zero signals,
  not penalties. The crawl workflow rebuilds every stored internship score and
  runs `score-health` before delivery so the separate `intern_*` snapshot cannot
  publish stale score versions. Explicit full-time-only wording without
  internship/student evidence is review-only, capped below the board threshold,
  and never alerted. The default internship list requires a positive internship
  title or posting-body signal; source-only, unknown, and experienced-title rows
  remain discoverable through the explicit review-only filter.
  If an external source crawl stalls, dispatch `internship-radar` with its
  `rescore_only` input to migrate the stored snapshot without fetching sources.
- **Google preference:** `profile.yaml` contains a data-driven score override
  that makes Google technical new-grad roles `100`, with `pm` explicitly
  excluded. The reason is printed in `score_reasons`; rules version is now 12.
- **Frontend:** the Jobs role-field toggles include `Product / project
  management`; Vercel serves the canonical `webapp/index.html` directly.
- **Score transparency:** each score dimension now shows its points, what it
  measures, and one compact plain-English why; the exact rule ledger remains
  available below it. The deterministic scorer now also adds bounded learned
  preference points from the owner’s saved/applied sample inside
  `personal_signal`; every learned contribution remains in that ledger.
- **Row selection:** a Jobs row click saves once and turns green; clicking the
  saved row again marks it red and hides it from active Jobs. **Show excluded**
  reveals it for a reversible restore. This state is view-only and does not
  change score, crawler, tracker history, or notification delivery.
- **Progressive frontend boot:** the shell and Jobs milestone render before
  optional state files hydrate independently, preventing one malformed or
  stale panel from blanking the site. The signed-in `VictorJimenez3` owner gets
  a compact red in-app developer notice with retry/details when a request
  fails; it sends no email and creates no issue automatically.
- **Optional tracker isolation (verified 2026-08-11):** `/api/tracker` now
  treats Google OAuth refresh and private-Sheet read failures as a disconnected
  optional mirror, logs the short provider error server-side, and returns 200
  so the owner’s Jobs/Pipeline/Notion workflow does not show a boot failure.
  GET hydration is read-only, so an older readable workbook is not mutated just
  to render the dashboard. Tracker writes and unexpected backend failures still
  use the error path; reauthorize Google from the account center if writes remain
  unavailable.
- **Role-field filter controls:** each Jobs role-field button remains visible
  while cycling neutral → selected → red excluded. A third click clears the
  exclusion; the red state filters that role family out without hiding the
  button.
- **CI hygiene:** the two exact-template Resume Studio tests now skip only on
  GitHub Actions when the intentionally local-only `CV/resume.tex` is absent;
  they continue to run on Victor's Mac where the private CV exists.
- **Learned Radar preferences:** the owner’s saved/applied roles now form a
  bounded positive sample for the deterministic `personal_signal` score
  dimension. Repeated employers, role-family mix, recognized sectors, and
  meaningful title language add auditable `learned ...` reason strings;
  untracking a role removes its implicit influence on the next rescore. The
  **My job preferences** tab explains these contributions and still offers
  advisory **Similar jobs to inspect** results. Fixed-category explicit
  feedback remains in `state/feedback.json` and is rendered through
  `python -m radar.main taste-report` to `docs/FEEDBACK.md`.
  Only the owner API or the owner-authenticated issue command can write it;
  the public repository warning is intentional and no email is sent.
- **Owner score controls and objective pace:** `state/score_preferences.json`
  stores the optional score sections Victor has enabled. Baseline and
  early-career evidence are locked on; role fit, sector/mission, company
  quality, compensation, personal signals, and timing/access can be toggled
  from the owner Settings panel. The platform dispatches an owner-only
  `score-preferences` action and rescoring publishes the new ledger. Company
  pace no longer scores from free-form “fast/slow” dossier adjectives: prompt
  v3 requires a cited 1–5 measure plus two observable operating cues, and
  unsupported pace is `Not confirmed`/zero.
- **Posting moderation:** owner archive is a soft, recoverable
  `manual_archived` marker that is carried through future crawls and keeps the
  historical job. Non-owners report expired/filled/duplicate/wrong postings
  through structured GitHub issues. `radar-report` workflow events use the
  issue author's GitHub login, deduplicate by distinct reporter, and add a
  three-distinct-reporter item to `docs/REPORTS.md` plus an owner review
  comment; they never auto-archive.
- **Automatic posting lifecycle:** `radar/lifecycle.py` classifies definitive
  dead-page evidence as `expired` or `filled`, records `closed_at`,
  `posting_status_reason`, `last_seen_at`, and bounded `lifecycle_events`, then
  suppresses terminal rows from active dashboards, RSS, alert issues, email
  batches, and the master board. Source-gap fallback is conservative: 45 days
  of active age plus a 14-day unseen grace window. Terminal state is retained
  for 730 days by default (`RADAR_HISTORY_DAYS`, minimum 365) and is visible in
  the platform's hidden-by-default **History** tab for future timeline work.
- **Tracker cleanup/notification:** the owner crawl, `enrich`, `rescrape`, `rescore`,
  `regate`, `notion-backfill`, and `python -m radar.main lifecycle` retry
  soft-archive of terminal tracked pages to the owner's Notion trash. Other
  signed-in users never touch that Notion database; `/api/tracker` syncs their
  private Google Sheet's separate `Posting Status` column and the Pipeline shows
  an in-app update while preserving application `Stage`.
- **Lifecycle knobs:** `RADAR_LIFECYCLE_ACTIVE_DAYS=45`,
  `RADAR_LIFECYCLE_UNSEEN_GRACE_DAYS=14`, `RADAR_HISTORY_DAYS=730`, and
  `RADAR_INTERNSHIP_SCRAPE_LIMIT=10` are optional overrides. A transient fetch
  failure is `unavailable`, not terminal evidence.

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
- **The platform has two Vercel doors**: Vercel
  (`job-radar-newgrad.vercel.app` — the memorable public shortcut; the
  existing `job-radar-vmj-8946s-projects.vercel.app` URL remains active for
  GitHub OAuth and old bookmarks, with instant writes). Both Vercel doors now
  exchange a short-lived encrypted session handoff in a cleared redirect
  fragment, so an existing old-host login is recognized on the shortcut even
  when cross-host cookies are blocked, and sign-out clears both hosts.
  `webapp/index.html` is canonical and served directly by Vercel. Jobs tab
  shows posting age and sorts by best-match or
  newest-first; Best Match can be limited to a selectable hour/day/week/month
  lookback window.
  Forks with an additional Vercel alias should list its hostname in the
  comma-separated `AUTH_ALIAS_HOSTS` environment variable; Victor's memorable
  alias is built in.
- **Canonical Vercel deployment:** `job-radar-newgrad.vercel.app` is the stable
  user-facing alias. `.github/workflows/vercel-production.yml` builds the
  `webapp/` project, deploys production, aliases that newest deployment to the
  friendly URL, and verifies the marker before succeeding. Required GitHub
  production settings are secret `VERCEL_TOKEN` and variables
  `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, and `VERCEL_ALIAS` (set the last to
  `job-radar-newgrad.vercel.app`). If alias assignment fails, the workflow must
  fail rather than silently leave users on an older deployment.
- **The ChemE profile is a separate production board** at
  `job-radar-cheme.vercel.app`, backed by `claude/cheme-intern-radar` and
  `RADAR_PROFILE=cheme`. The default production branch owns the three
  `cheme-*` scheduled orchestrator workflows because GitHub schedules only
  default-branch workflow files. Dispatches and checkbox/comment events route
  to the correct branch via the profile payload or `radar-cheme` label. Both
  profiles deliberately share the repository `NOTION_TOKEN`; do not create a
  second Notion database or token for ChemE.
- **Notion/tracker:** `NOTION_TOKEN` is set and working. Checkbox → entry
  with the `stage_saved` status ("Waiting for a referral" in his DB).
  `.github/workflows/tracker-sync.yml` now pulls Notion/Sheets stage edits and
  pushes local changes every 15 minutes; `reconcile-checkboxes` remains the
  twice-daily issue safety net. Canonical posting URLs collapse feed/ATS
  variants into one local tracker identity, migrate durable references, and
  queue duplicate Notion pages for reversible archival.
- **Aggregator/direct-link convergence:** Jobright aggregator URLs are never
  thrown away. The crawl follows a bounded `RADAR_LINK_RESOLVE_LIMIT` batch,
  promotes only an explicit ATS/employer application link, records the
  aggregator URL in `alternate_urls`, and shows source/fallback links in issue
  alerts and `docs/DASHBOARD.md`. A strict employer+title+compatible-location
  match can merge an aggregator row into one direct ATS row; ambiguous roles
  remain separate. A definitive Jobright page banner saying the posting has
  closed is now stored as expired evidence before direct-link promotion, so it
  cannot remain in active Jobs merely because the page returned HTTP 200.
  `python -m radar.main resolve-links` accelerates the same cached, auditable
  backfill for existing open state, uses bounded parallel requests, and reports
  closed rows separately. The scheduled repair job runs this before discovery.
- **Email lifecycle:** `email-watch` is active when
  `EMAIL_ADDRESS` / `EMAIL_APP_PASSWORD` are configured. It searches a
  bounded 21-day window, decodes ATS headers, matches employer plus title, and
  advances Applied → OA → Interview or Rejected without regressing terminal
  stages. Ambiguous messages are retained in `state/email_review.json`.
  Missing secrets produce a workflow warning and a clean skip.
- **Workflow recovery:** `workflow-recovery` listens to critical workflow
  completions, reruns failed jobs once, and opens/updates one actionable issue
  after a second failure with the exact instruction: "Tell Codex: fix workflow
  run <URL>".
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
- **Score calibration and sibling variants (rules v13, 2026-08-08):** the
  0–100 calibration now leaves useful separation through the 60s, 70s, 80s,
  and 90s instead of flattening too many raw utilities at the top. A
  level-II/L4/mid-level title receives the profile-driven `-28` locked
  eligibility contribution even when its wording also says early career; it
  stays dashboard-visible but cannot alert. Same-company/title postings are
  treated as exact location/requisition variants and normally tied to the
  strongest displayed score without a diversity penalty; posting-specific
  quality/experience/lifecycle verdicts remain authoritative on each row.
  Different but conservatively
  similar titles are compared only within the same company and role bucket;
  weaker siblings get a bounded `-1` to `-3` adjustment, then the existing
  company-concentration guard prevents a crowded employer from filling the
  whole top. All of these are visible in the score reason ledger. Do not
  hand-edit `state/jobs.json`; run `python -m radar.main rescore` after a
  scoring change.
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
  tokenless/PAT write paths, or non-owner OAuth sessions.
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
  creates one workbook in that user's own Drive with separate Applications,
  Internships, and Preferences tabs. The Google Cloud
  OAuth app must be External + In production; a test-user list is not the public
  deployment path. GitHub users can explicitly connect Google later from the
  Tutorial account center. The private Accounts registry stores only encrypted
  refresh-token ciphertext and Sheet IDs when available, but is no longer a
  prerequisite or single point of failure. The current user's encrypted
  HttpOnly session carries their own grant and Sheet ID; Drive marker/title
  discovery reconnects the same workbook after reauthentication.
  `/api/tracker` reads only the current user's workbook. `GOOGLE_ACCOUNT_SHEET_TAB` remains
  `Accounts`; `GOOGLE_PERSONAL_SHEET_TAB` defaults to `Applications`. Tokenless
  issue mode remains owner-only. The OAuth grant is Drive-only, not
  Gmail.
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
  anything derived from it. The owner-only Resume Studio control plane lives
  in production; its private engine runs from
  `.venv/bin/python scripts/resume_studio.py` or the installed login service
  at `http://127.0.0.1:4317/`. Tailor, the drawer's Resume tab, and the Studio
  tab all use the same cloud workspace. A bounded public posting snapshot is
  sent through the allowlisted loopback postMessage bridge only when matching
  or generation is requested. CV evidence, provider sessions, and source
  documents never cross that boundary; explicit owner bank sync copies
  generated artifacts to the private Google Drive bank (DECISION #123 and the
  current bank closure).
  For temporary human-feedback calibration across varied radar roles, run
  `.venv/bin/python scripts/resume_calibration.py --generate --count 6 --serve`
  and open `http://127.0.0.1:4321/`; generated PDFs and JSONL labels stay under
  `CV/.resume_studio/calibration/`.
  It uses only the installed first-party Codex CLI pinned to `gpt-5.6-luna`,
  strips API-key environment variables, and stores all output under
  `CV/.resume_studio/`.
  `CV/immutable/VictorJimenezResume.tex` / `.pdf` is the locked visual baseline;
  TLDP is only one reference artifact. Providers return source-addressed
  content plans, never full LaTeX. The renderer preserves the baseline's
  visual contract, uses company-first employer headings, and dynamically packs
  the strongest nonredundant evidence with no wrapped bullets, excessive
  bottom whitespace, or formatting drift. Reports record separate quality
  gates, exclusions, provider calls, and emitted Codex token usage;
  totals are marked incomplete when a provider omits a call's footer.
  No API or second-provider fallback is configured; a missing Codex CLI leaves
  the local engine unavailable rather than silently changing the privacy or
  billing boundary.
- **Current Stryker quality lab receipt (DECISION #154):** the merged-code
  absolute-path run at
  `CV/.resume_studio/benchmarks/stryker-latest-merged-absolute-20260821/runs/40f4734b66df`
  completed in 2,715.9 seconds. Fit was strong (97), but tailoring was
  `do_not_ship`/`blocked` with five regressions, one hard blocker, zero gains,
  and loss weight 13. Audit repair improved the preference key from `-17` to
  `-13` but still did not beat the control. The sealed evidence, recruiter,
  technical, and screening roles all completed in every round under the fixed
  contract and rubric hashes. The canonical base is therefore the primary PDF;
  the rejected candidate remains available as `tailored_candidate.pdf`.
  A fresh four-role Merck-control jury completed in 393.7 seconds: factuality
  and privacy passed across all roles, but the candidate's useful JWT,
  document-extraction/caching, and HackMIT gains did not offset lost REST,
  fallback, explainability, health/security, and portfolio evidence. This is a
  qualitative control result, not a hiring prediction or exact score.
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
  the original. The current machine exposes only the Codex CLI pinned to Luna.
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
- **Resume Studio queue/editor/usage is shipped:** Used bullets, AI tailor,
  Take-the-wheel (moderate), and Unchained generation are durable queued
  modes. The bank keeps each posting snapshot and run; the workshop embeds the
  original/latest PDF, edits every visible line, and exposes selection
  rationale plus observed weekly Codex tokens/calls. Codex Plus's weekly
  allowance is not exposed by the local CLI; `CODEX_WEEKLY_LIMIT_TOKENS` is an
  optional owner-provided comparison limit.
- **Resume Studio canonical-file lock is shipped (DECISION #98):** the local
  Studio exposes a lock status and refuses to render anywhere inside `CV/`
  except the private `CV/.resume_studio/` workspace. The canonical
  `immutable/VictorJimenezResume.tex` / `immutable/VictorJimenezResume.pdf`, the historical `og_resume` pair, and the TLDP source/PDF are never Studio write
  targets. The main UI makes Take-the-wheel the primary option, followed by AI
  tailor and Used bullets; raw review data stays behind disclosures. No legacy
  project set was restored automatically; the local CV Git history and saved
  runs remain available for explicit comparison.
- **VictorJimenezResume is now the protected default (DECISION #105):** Resume
  Studio reads `CV/immutable/VictorJimenezResume.tex` and compares against
  `VictorJimenezResume.pdf`; the old `og_resume` pair remains historical. The
  protected files use read-only permissions plus macOS `uchg` flags. Deliberate
  edits require the interactive owner-PIN command
  `.venv/bin/python scripts/resume_lock.py unlock`, followed by `lock`.
- **Resume Studio evidence review is shipped (DECISION #99):** the private
  graph indexes the Markdown corpus plus bounded public GitHub/Devpost
  corroboration, with source IDs, authority, and reversible review statuses.
  Public records remain non-authorizing until Victor confirms them; rejected or
  superseded records are removed from ranked context. The GitHub refresh keeps
  cached README evidence when the unauthenticated API is rate-limited, and
  reports that condition as stale-data metadata instead of discarding it.
- **Resume Studio harness v2 remains the historical contract (DECISION #106):**
  the candidate portfolio is adaptive; no leadership, project, or bullet-count
  floor is forced, and no weak backup bullets are added to fill a page. The
  active Codex Luna lane now performs writing plus the four-role critic jury;
  local models, Ollama, arbitrary endpoints, and API fallbacks are forbidden.
  The critic returns critique-only gates and line feedback; it cannot replace
  the plan or grade itself. A compiled draft remains `awaiting_review` until
  Victor approves a ready gate report, and approval changes only private run
  metadata. Normal bottom clearance is informational; wraps, compile errors,
  forbidden claims, duplicate evidence, and missing critic-panel review block
  readiness.
- **Resume Studio flexible portfolio reserves (DECISION #109):** when an added
  project contributes unique capability coverage, the one-page packer protects
  core experience and reclaims flexible content first in this order: coursework,
  the HackMIT acceptance-pool/selection bullet, then the aggregated Awards
  line. The HackMIT reserve removes only its prestige proof, never the
  project's implementation bullets.
- **Resume Studio measured-space follow-through (DECISION #114):** the density
  pass now reads both current and legacy capacity reports and triggers when a
  compiled page has a usable measured window, not only when a single QA line
  says it fits. Codex gets the first editorial pass; a deterministic
  source-authorized fallback then compiles additions until the next trial fails.
  A new project/experience is atomic at two bullets. Flexible front matter is
  reclaimed first; if a unique addition still needs room, only lower-value
  project or leadership bullets may be displaced, never core experience. The
  audit records additions, rejected trials, and replacement evidence.
- **Resume Studio post-edit density closure (DECISION #120):** after provider
  line editing—and again after any critique revision—the final page is
  re-measured. A deterministic, claim-authorized evidence pass fills capacity
  newly exposed by compaction, recompiling every candidate and restoring the
  previous safe plan if geometry fails. The private `post_line_density.json`
  audit records this last-mile decision.
- **Unchained portfolio judgment (DECISION #121):** generation compares a
  generic Resident Assistant/Residence Life line against unused verified
  technical project evidence. When the technical evidence is stronger and
  fits safely, generation replaces the low-signal leadership line and records
  the tradeoff; the moderate tailor is unchanged.
- **TODO — confirmed application resume snapshots (DECISION #122):** when
  Victor confirms that a specific generated resume was used, upload or attach
  that exact company-named PDF to the existing `Resume` column on the
  corresponding Notion application row. Make the confirmation explicit,
  preserve an immutable/private snapshot or durable artifact reference, and
  never infer confirmation from generation or application-stage changes. The
  UI should make this nearly seamless while keeping the action owner-controlled
  and auditable.
- **Resume Studio generation remains separate from the moderate baseline:**
  `generation` performs a posting requirement-to-evidence audit before drafting,
  searches claim-authorized Markdown records, and may synthesize grounded bullets
  or Skills lines. It labels adjacent support, records honest gaps, and blocks
  unsupported posting terms from the rendered resume. Existing `unrestricted`
  behavior is preserved and displayed as Take-the-wheel (moderate). Older runs
  may still contain their historical internal filename, but current output uses
  the owner/company format documented above.
- **Durable candidate-line memory (DECISION #110):** strong or reusable lines
  from private tailoring runs must be promoted into the relevant Markdown
  dossier/iteration log with source support and an `approved`, `bench`, or
  `superseded` status. Future evidence retrieval is Markdown-first; old PDFs
  remain audit artifacts, not authority.
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
- **2026-08-29 roadmap follow-through:** descriptive feedback now preserves
  optional owner context in the structured event and generated audit; explicit
  new-grad/program roles sort ahead of early-career-compatible roles in the
  dashboard, issue delivery, and email queue; cited company-stage evidence has
  a separate neutral-when-unavailable startup signal/filter; and opening
  details, Resume Studio, or Apply no longer adds a role to To apply. Tracking
  requires the explicit Save/To apply action.
- **Hierarchical location selector shipped:** the classic Jobs view now expands
  United States into selectable states, keeps non-US locations at country level,
  and retains every captured location in the role drawer. It is presentation
  and filtering only; scraper queries and stored location values are unchanged.
- **Radar score v8 is shipped (DECISIONS #70-71):** records retain uncapped raw
  utility, calibrated base, dimension values, reasons, and final score. Goal
  companies can reach 100, but posting wording separates nearby roles; cited
  momentum and compensation can compensate for weaker ordinary dimensions.
  Sector points have diminishing influence and hard gates remain absolute.
- **Still deliberately deferred:** semantic/vector RAG, multi-user CV/provider
  storage, and recruiter identification/asking with approval before send.
  Owner-only application autofill/submission is now shipped in the separate
  Application Agent boundary (DECISION #178); it remains deliberately
  unavailable to other users and never bypasses attestations. Email-based
  applied detection is also parked until the multi-user privacy model exists.

## Safe handoff practice

At the end of any material change, state:

1. What changed and why.
2. Files changed.
3. Validation run and its result (or why it could not run).
4. Any configuration/secrets or GitHub-side action still required.

Preserve unrelated working-tree changes. Do not assume a secret exists merely
because the code references it.

## Platform frontend/back end (Vercel)

- `webapp/index.html` is the canonical platform page and is served directly by
  Vercel alongside its `api/` functions. The former GitHub Pages publication,
  `.nojekyll` marker, and static mirror are retired.
- Never put credentials in the frontend or repo. Auth = GitHub OAuth via the
  Vercel backend (owner-only); a missing backend degrades to read-only/static
  behavior.
