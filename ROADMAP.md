# Roadmap — designed, deliberately deferred

The dream-system backlog. Each item is scoped enough to build on request;
none block anything currently running.

## 2026-11-04 Dot integration — portable foundation, activation pending

Victor wants **Dot inside ChatGPT** to be the preferred companion because the
Mac is not always available. This applies to the whole project, including both
job lanes. Dot's uptime is user-reported; browser access, durable storage,
command execution, scheduling, and authentication must each be verified.
Current access evidence and the next-session procedure are in
[`CLI_HANDOFF.md`](docs/CLI_HANDOFF.md#dot-integration-2026-11-04-activation-pending).
This section is the implementation plan; do not create a separate handoff.

**Architecture:** keep the cloud source of truth and existing delivery owners.
Use capability-based companions for browser collection, enrichment, and later
private application work. Dot is the preferred companion; Mac remains an
explicit fallback. A Dot chat/browser connection alone cannot run a Python
daemon. Use native Dot scheduling if actually exposed; install an OS service
only if a persistent host and supported service manager are verified.

Alternatives considered: moving every workflow to Dot would duplicate stable
cloud delivery and require unverified capabilities; keeping the Mac as the
only companion would not meet the availability goal. The selected direction
adds Dot without making the deterministic radar depend on it.

| Area | Current owner | Dot integration and completion evidence |
| --- | --- | --- |
| New-grad, technical internships, ChemE | GitHub Actions / distinct profiles | Same deterministic identity, eligibility, scoring and profile isolation; Dot can request bounded existing operations, never create a parallel scoring policy. |
| Instagram and other approved public social feeds | Mac OpenCLI; cloud consumes source-only leads | Hourly Dot task using a verified browser/backend; retain exact public-content proof, bounded excerpts, cooldowns, provenance and post IDs. Verify real collection and a scheduled second run while Mac collection is stopped. LinkedIn remains search links only. |
| Quality/company research | Cloud AI plus optional Mac Ollama | Portable companion runner; benchmark an actually available Dot provider before selection. Do not assume a 19 GB local model fits the VM or that a chat subscription provides API credentials. |
| Application links / dedupe | Shared Python state and importer | Dot sightings feed existing canonical identities and URL promotion; repeat submissions and provider retries must not create jobs or alerts twice. |
| Notion, Sheets, email, RSS and GitHub alerts | Existing cloud workflows / owner-authorized API | Reuse stable IDs, receipts and owner checks. Dot requests work through a bounded authenticated interface; it does not send duplicate notifications or get all cloud secrets. |
| Jobs, Pipeline and owner controls | Vercel classic UI / vNext API | Show actual worker capabilities, last successful operation and stale/offline state. Dot reads the same results; no second website or datastore. |
| Resume Studio / tailoring | Private local engine and ignored `CV/` | Later private storage/access design and supported provider/PDF tools. No automatic CV, session or credential copying. Tailoring quality work remains lower priority. |
| Browser application assistance | Extension, worker leases, Mac native host | Preserve user confirmation before submission. The `osascript` Simplify shortcut is Mac-only; Dot needs a verified supported browser action, never a renamed Mac command. |
| Reliability / release / recovery | GitHub CI, Vercel and companion logs | Capability report, per-operation outcomes, bounded retry, owner-visible stale status and explicit primary companion. Prove restart recovery and safe rollback before cutover. |

### Phase 1 — portable entry point and honest capability report

Files: `radar/companion.py`, `tests/test_portable_companion.py`; retain the
existing `scripts/mac-companion/` runners and shared checkout lock.

- [x] Add read-only `python -m radar.companion doctor --json`, reporting only
  allowlisted checks. Installed binaries must not imply working login, model,
  push credentials, or scheduler; never emit credentials, profile IDs or raw
  environment values.
- [x] Add explicit plan/execute modes for `social` and `enrich` only. An
  ordinary invocation must not collect, mutate state, push, or send anything.
  Execution needs an explicit dedicated clone, expected branch and primary
  runtime assignment; validate under the shared checkout lock. Preserve dirty
  work, unpublished commits and manual Git operations; propagate failures.
- [ ] Verify read-only default, redaction, branch/worktree guards, lock
  contention, child failure and supported action boundaries with isolated
  test repositories. Local validation passes: 19 focused tests and 936 full
  regression tests (3 skipped), compilation, lint and doctor/dry-run smoke
  checks. Linux CI remains pending publication.

### Phase 2 — connect Dot and prove one bounded social run

First establish what Dot actually exposes. If it has a persistent shell and
browser, use the portable runner and the existing OpenCLI/public-proof path.
If it exposes only native browser tasks, build a separate observation adapter
feeding the existing validated ingestion path; do not treat arbitrary pasted
captions as verified public evidence. Files: `radar/social.py`,
`tests/test_social.py`, and a transport adapter selected after that inspection.

- [ ] Verify browser access, public Zero2Sudo content, persistent session,
  task execution, durable private storage and scheduler independently.
- [ ] Verify GitHub publication or a narrowly authorized ingestion endpoint;
  redact session material and use existing stable posting identities.
- [ ] Replay the same observed post twice and prove no duplicate lead/alert;
  expire the browser session and prove a truthful failed/degraded result.
- [ ] Register the hourly task through the actual supported scheduler;
  record its identifier and observed run, not just the configured interval.

### Phase 3 — one companion owner and reliable cutover

The current `fcntl` lock is per checkout, **not distributed**. An environment
label saying `dot` is operator configuration, not a lease. Initially use a
controlled single-companion cutover: finish any Mac run, disable its matching
schedule, sync the Dot clone, then run Dot. Re-enable Mac only after Dot is
stopped. Never enable automatic takeover based on a stale heartbeat alone.

- [ ] Publish private operation status with `attempted`, `succeeded`,
  `skipped`, `failed` and `published` distinguished; a process exit of zero
  or an installed binary alone does not establish success.
- [ ] Prove two scheduled runs and restart recovery; retain the exact revision,
  source counts, publication evidence and last-success timestamps.
- [ ] If automatic Mac/Dot failover is needed, implement a shared transactional
  lease with fencing before enabling it. Test expired-worker writes, double
  claims, network partitions, push rejection and crash after publication.

### Phase 4 — Dot controls across the product

Reuse `webapp/api/_application-agent.js`, `webapp/api/resume-bank.js`,
`radar/worker.py` and the existing database lease implementation. First verify
which production datastore is active; do not activate the staged Postgres
queue just to label Dot integrated. Add narrow capability-based operations and
worker status through existing owner authentication, with bounded inputs and
idempotency. No generic remote shell exposed through the web API.

- [ ] Read job details, explain ranking/location, inspect saved applications
  and request supported refreshes through the same contracts as the website.
- [ ] Test unauthorized access, duplicate delivery, stale worker completion,
  lane isolation and preservation of manual tracker edits.
- [ ] Add owner-visible source/worker health in classic and vNext only once
  backed by real telemetry. Warn once on meaningful failure/recovery; avoid
  routine hourly notifications.

### Phase 5 — private Resume Studio and browser assistance

Files: `scripts/resume_studio.py`, `scripts/resume-studio-service/`,
`browser-extension/native_host.py`, `webapp/api/_application-agent.js`, and
their existing tests. Keep these separate from the initial public collector.

- [ ] Establish private persistent storage, owner-authenticated access,
  supported provider and PDF generation before moving any private artifacts.
  Preserve canonical-resume locks and the existing quality/ownership rules.
- [ ] Add a supported Dot browser action adapter; retain field review and
  submission confirmation. Test disconnected browser, expired lease, partial
  form completion and user cancellation without submitting a real application.
- [ ] Demonstrate end-to-end owner use while the Mac is unavailable, and
  document any feature still tied to Mac. Update `docs/RESUME_CLI.md` and
  `docs/TUTORIAL.md` when those behaviors actually change.

**Review focus:** local locks are not cross-host ownership; browser access is
not scheduling; zero exit is not publication; public leads are not employer
proof; an authenticated cloud page does not make a private local engine safely
reachable. Each phase's acceptance checks must exercise its corresponding
failure, not just its successful path. Do not mark the integration live until
the operational evidence is recorded in `docs/CLI_HANDOFF.md`.

## 2026-11-03 opportunity quality and social discovery — published

**SHIPPED in [PR #6644](https://github.com/VictorJimenez3/fable-job-search/pull/6644)**
(DECISION #234), code `04f089f2aabdabe48dfdb2b4dce85ceac23bc92b`.
New-grad Rules v17 and internship Rules v9 provide durable cross-source IDs,
verified direct-link promotion, conservative US evidence, and consistent
score-first ordering. Tracker edits and receipts are preserved. Ambiguous
requisitions stay separate; historical sent emails and issue bodies stay historical.

Healthcare adds three points for company or actual team evidence. Missing pay
is neutral. Unknown locations remain reviewable without alerts; foreign-only
roles leave the active board. Nonpersonal evidence remains separate from
personal ranking preferences.

The hourly Mac Instagram agent is active for Zero2Sudo. Its first scheduled-agent
run read 12 posts and retained 8 career excerpts, with no actionable employer
URLs and no invented jobs or alerts. Other researched accounts remain disabled.
LinkedIn is search-links-only. The Mac enrichment environment now uses Python
3.12, and both companions share a lock that preserves unpublished local work.

The final migration replay retained 96,376 new-grad and 37,808 internship records,
with zero canonical posting-key duplicates and zero location-assessment drift.
All saved applications and upstream receipts were preserved. Local validation:
917 tests passed, 3 skipped; production CI passed. Exact deployment, browser,
and runtime evidence is maintained in `docs/CLI_HANDOFF.md`.
Private offer details remain owner-only. Resume tailoring remains out of scope.

## 2026-11-03 reliability and discovery overhaul — historical verified release

**✅ SHIPPED in PRs #6566, #6590, and #6625 (DECISIONS #228–231).** The
current implementation makes the requested reliability and coverage changes
without changing the Resume Studio tailoring contract:

1. **Bounded job detail state.** Open and terminal detail is stored behind
   deterministic SHA-256 prefix references in 256 versioned, checksummed shards per
   lane (`jobs_details/` and `intern_jobs_details/`). The primary snapshot stays
   lightweight; the existing drawer fetches and verifies only one shard. This
   bounds individual files, not the repository forever, so a future datastore
   boundary remains explicit.
2. **Conservative identity and evidence.** Provider-aware posting identities
   preserve distinct real requisitions and provider URLs when same-label fuzzy
   matches are ambiguous. Rules v16 separates required from preferred
   experience and keeps source-only/cohort leads review-only until employer
   evidence is available. No fuzzy dedupe promise is treated as a 100% duplicate
   guarantee.
3. **Fairer discovery and delivery.** Registry rotation uses the oldest
   polling attempt, and delivery is current-state, alias-, and crash-aware with
   latest evidence. New-grad and internship delivery remain separate. Batch
   checks are hourly while the existing policy remains minimum 3, maximum wait
   12 hours, and urgent score 92.
4. **Additional public discovery.** Headcount engineering and data/AI feeds and
   YubHub graduate engineering (Data by YubHub, up to 100) are bounded sources.
   The anonymous public zero2sudo Notion source is enabled only for review-only,
   source-only leads (up to 25). Instagram login is unavailable; automatic
   Instagram and LinkedIn scraping remain out of scope. Optional public
   LinkedIn search uses the dynamic candidate graduation year (currently 2027).
5. **Migration and verification boundary.** Advancing the main snapshot from
   v15 to v16 requires a generated-state rescore as a separate migration commit;
   generated files are not hand-edited. The generated migration retains 81,961
   new-grad records after consolidating 2,589 duplicates; 907 meet alert rules.
   The primary is 83,166,228 bytes and both lanes pass score-health. The frontend
   dependency patch (Better Auth 1.7.7, Vite 7.3.7, Zod 4.6.6, Vitest 4.1.12)
   leaves zero high or critical audit findings. The full Python suite passes
   768 tests with 3 skips on the final local release. Production CI, Vercel,
   and the first crawl passed: 10,011 discoveries and 23 alert candidates.
   The verified rescore snapshot covers 91,972 records at 76,563,949 bytes,
   with about 23 MB below the existing guard. More than 50,000 actual records
   passed frontend/API behavior parity checks. Internship ingestion now shares
   canonical identity and employer-evidence handling, all scheduled snapshot
   writers publish detail shards, and internship push retries preserve the
   completed discovery cycle. The live internship run also passed, finding
   2,614 records and selecting 35 alert candidates. Malformed application links
   remain review-only under DECISION #231. Live run evidence is in the CLI handoff.
   Resume tailoring is unchanged except for the shared snapshot-reader/cache
   correctness needed to read compacted state.

## 2026-08-16 vNext implementation status

1. **Normalized persistence and migration path — ✅ IMPLEMENTED, activation
   pending.** SQLAlchemy/Alembic now define postings, exact source boards and
   sightings, aliases, score snapshots, applications/events, preferences,
   research/LLM telemetry, durable work items, and Better Auth storage. The
   legacy importer is idempotent and reports count/hash parity. JSON remains
   the deterministic production fallback until `DATABASE_URL` is provisioned,
   migrated, imported, and verified.
2. **Cursor API and React workspace — ✅ STAGED.** `/vnext/` has responsive
   Jobs, Applications, Companies, Resume, and Settings routes; cursor fetching,
   list virtualization, runtime response validation, and explicit loading/error
   states. Private workflows keep a visible classic-platform bridge until the
   database/auth identity cutover is proven.
3. **Crawler correctness and ranking semantics — ✅ SHIPPED IN CODE.** ATS
   connectors paginate within bounded caps, every posting retains its exact
   board identity, and source-gap expiry requires that exact board to have
   succeeded. Evidence score, eligibility, and personal priority are separate
   audited fields while legacy score remains compatible.
4. **Established infrastructure instead of bespoke substitutes — ✅
   IMPLEMENTED.** The project uses uv lockfiles, Alembic/SQLAlchemy, staged
   Better Auth, optional LiteLLM routing, Gmail History API, TanStack Query and
   Virtual, and Zod. Existing deterministic budgets, owner checks, and
   repository fallbacks remain authoritative.
5. **Production activation — ⏳ CONTROLLED CUTOVER.** Provision Postgres,
   run migration/import/parity, configure Better Auth and provider callbacks,
   then verify private API ownership before replacing the legacy session. The
   durable worker stays dormant until its database and operational schedule are
   configured; this prevents a partial migration from disrupting the live
   crawler.

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
3. **Google collaboration and pluggable tracking — ✅ SHIPPED.**
   `TRACKER_BACKEND=notion|google_sheets` selects a first-class tracker with
   stable-ID upsert and stage readback. On Vercel, Google OAuth provisions one
   private Applications workbook per connected user; GitHub-only users can
   connect Google later. Notion remains the default owner/fork backend.
4. **Interview workspace — ✅ V1 SHIPPED.** OA/Interview applications now have
   their own workspace with cited company context, likely focus, and a prep
   checklist. A manual company-name packet and independent interview-process
   sources remain later enhancements.
5. **Taste and moderation loop — ✅ SHIPPED 2026-08-08.** Saved/applied roles
   form an explainable owner sample; the platform exposes similar roles as an
   advisory discovery surface and fixed-category feedback as capped,
   auditable company/title signals. Owner archive is recoverable and survives
   crawls. Other GitHub users can report stale postings through issues, with
   distinct-login deduplication and a three-reporter owner review threshold;
   automatic deletion remains deliberately deferred until scale warrants it.
6. **Technical internship lane — ✅ SHIPPED 2026-08-08 (DECISION #104).** The
   main platform now switches between isolated new-grad and internship lanes.
   Internship sources, ATS queries, scoring/gates, graduation-aware
   freshman/sophomore/junior/senior matching, state, dashboards, GitHub
   surfaces, and Google tracker tabs are separate. New-grad remains the
   priority compute path; internship email batches are separate and default on.
   Rules v8 keeps the prestige-led 0–100 ranking, dynamic internship terms,
   additional ApplyGuy, Aprameyak, and Headcount internship sources, modest
   healthcare/finance preferences, and a bottom score cap for defense work.
   The DECISION #232 follow-up requires position-specific evidence, handles
   past terms and required graduate-only degrees, and re-evaluates legacy
   verdicts. Current/upcoming, all-active, and review scopes expose the right
   term and provenance; active internships are no longer silently hidden by
   the default 30-day discovery window. Bounded concurrent employer checks
   work through new leads and the review backlog. Publication evidence is
   tracked in `docs/CLI_HANDOFF.md`.

## New TODO requests — 2026-08-28

1. **Descriptive rating feedback — ✅ SHIPPED 2026-08-29.** Extend the existing feedback feature so
   that when Victor suggests a rating change, he can add a free-text
   description explaining why. Preserve the description with the feedback
   record and show it in the auditable feedback/reason history; it should be
   optional and must not replace the existing fixed feedback categories.

2. **Prioritize the highest-value new-grad opportunities — ✅ SHIPPED 2026-08-29.** Rework the
   programming/ranking policy so explicit new-grad programs and new-grad
   roles are ordered above everything else. Next should be roles that appear
   compatible with a new graduate, including postings with no stated years or
   other early-career-compatible wording. Keep required experience and other
   eligibility facts auditable, and do not let this preference bypass hard
   field-fit or eligibility gates.

3. **Startup-stage filter and ranking — ✅ SHIPPED 2026-08-29.** Add a filter that lets Victor
   prioritize startup opportunities, including a meaningful stage grouping
   such as late-stage startups. Give startup roles a distinct, explainable
   ranking signal or score so startup priority can be evaluated separately
   from the general company/radar score. Company-stage evidence should be
   labeled with its source and remain neutral when unavailable.

4. **Stop automatic Notion additions from browsing — ✅ SHIPPED 2026-08-29.** Opening a job's details
   or external application link should not automatically add the role to
   Notion or move it into an application stage. Victor often checks eligibility
   before deciding whether to track a role, so adding it to Notion must require
   an explicit owner action such as **To apply** or **Add to Notion**.

5. **Resolve Codex issues caused by the restored five-hour window.** After the
   five-hour Codex usage window returns, identify and fix any resulting issues
   with task continuity, rate limits, scheduling, handoffs, or usage visibility.
   Preserve durable work and avoid assuming that a restored window alone means
   interrupted or queued work has recovered correctly.

## Victor + chat TODOs

These are work items for Victor to handle through a focused chat, separate from
feature requests to implement in the application.

1. **Rework tailoring substance.** Review every iteration of the tailoring
   workflow, including its strengths and flaws, and improve the substance of
   what it produces. The review should cover the actual content and reasoning
   across iterations—not just formatting, polish, or whether the workflow
   completes successfully.

2. **Rework the scoring system entirely.** Use a focused chat to reassess the
   scoring model from first principles, including its goals, dimensions,
   weights, gates, ranking behavior, and explanations. Review what is working,
   what is producing misleading priorities, and what should be removed or
   replaced before defining a new auditable scoring approach.

3. **Overleaf-style Resume Studio workspace — ✅ SHIPPED (DECISION #206).**
   Resume Studio now exposes a dependency-free project tree, source editor,
   autosave/conflict handling, local Tectonic previews, and append-only history
   in both local and owner production surfaces. Existing CV paths remain
   untouched; only private managed projects are editable and workspace PDFs
   remain `workspace_draft` artifacts outside Resume Bank and application flows.

## Deliberately deferred by Victor

1. **Scoring/state maintenance hardening — ✅ foundation shipped.** CI now
   fails when any stored job lacks the current `score_version`, and a scheduled
   six-hour rescore rebuilds from a fresh upstream snapshot before publishing.
   Current new-grad Rules v16 also classifies (but never alerts) no-experience-floor technical
   roles as `early-career possible`. The remaining long-term improvement is a
   shared state transaction layer for all generated writers, but scoring now
   has an automated repair path.

2. **RAG and vector search.** Embed job descriptions, company dossiers,
   candidate profile/CV material, and saved decisions; support semantic search
   and similarity-based ranking with explainable evidence. Keep deterministic
   gates authoritative and log retrieval/similarity reasons. This supersedes
   the currently parked posting↔profile RAG spike below.
3. **CV-aware target-role toggle.** When a CV is available, add a `CV` option
   to the existing “all target roles” dropdown. It should show roles that can
   be meaningfully tailored to the selected CV, then offer a local, review-only
   tailored draft. Personal CV content stays local and never enters public
   state.
4. **Hierarchical location selector — ✅ SHIPPED 2026-08-29.** Add a Jobs-view location control
   where **United States** can be expanded into selectable states, while every
   non-US country is selectable at the country level without requiring a
   state/province choice. This is a presentation/filtering feature over the
   location fields already captured by the current pipeline; do not change the
   scraper or its source queries for this work. Keep the existing location
   values and make the hierarchy auditable when a posting has multiple
   locations.

## North star: a platform anyone can log into (direction, 2026-07-13)

Today the multi-user answer is fork-per-person (DECISIONS #25,
docs/FORKING.md): perfect isolation, ~10 min of setup. The long-term
direction is **"log in with GitHub, then seamless"** for anyone:

- **Phase 1 (now):** fork + enable Actions + paste `NOTION_TOKEN`; deploy the
  website to Vercel when a web UI is needed. Works, documented, zero shared
  infrastructure.
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

The classic Jobs board received a responsive filter and browsing pass on
2026-09-28 (DECISIONS #221): primary controls stay visible, advanced filters
collapse, active choices can be cleared individually, and results are paged.

## Canonical posting families + batch tailoring — ✅ SHIPPED 2026-08-19 (DECISION #141)

Exact posting URLs remain the strongest identity boundary, with a conservative
company/title-family repair for official-versus-aggregator variants and a
same-board/same-day guard for marked location fan-outs. Surviving rows retain
locations, alternate links, aliases, and auditable match reasons while
ambiguous same-title requisitions remain separate. The owner-only **Tailor
today** flow now moves successfully queued drafts into a real **To tailor**
lane between **To apply** and **Applied**, across the classic UI, email
ordering, Notion mapping, Google trackers, and staged API.

## Posting ↔ profile RAG (to-do, parked — Victor 2026-07-17: "to do list that for sure")

Embed posting text and Victor's profile/CV bullets, use similarity as a
ranking signal ("how related is this role to what I've actually done").
Local-first: Ollama embedding models on the Mac are free (e.g.
nomic-embed-text), store vectors next to the quality cache, keep the score
deterministic-auditable by logging the similarity as a reason line. Needs
the scraped posting text (now shipping) + `CV/` content (Mac-only,
DECISIONS #29). Park until the scrape pass has filled a few weeks of text.

## Radar score calibration v8 — ✅ SHIPPED 2026-07-31 (DECISIONS #70-71)

The current additive score clamps at 100, so meaningfully different excellent
roles collapse into the same ceiling. NVIDIA should remain capable of earning
100 because it is a genuine goal company, but company identity alone should
not make every NVIDIA posting identical: strong roles should occupy the 90s
and reach 100 when the posting's actual work, level, compensation, and wording
justify it.

Victor's constraint is deliberately broader than a prestige exception:

> "a lot of companies like nvidia get 100s. and i love that, that should happen
> nvidia is a goal. however, I think that there should be in the 90s range and
> can get to 100 depending on the role wording"

> "if its 120 in one area and shitty in another, that 120 should
> overcompsitate a bit"

The scorer now uses an **uncapped internal utility** plus a calibrated
0–100 display score. A genuine superpower—for example extraordinary role fit,
pay, company momentum, technical learning, or mission—may compensate for a
weaker dimension. Do not implement that as a Netflix/NVIDIA/health-company
special case or as enough small bonuses to guarantee a favorite outcome.
Hard eligibility and field-fit gates remain non-compensable.

Shipped behavior:

- Replace the hard ceiling as the aggregation mechanism. Preserve per-dimension
  raw utility, then map the total to a meaningful 0–100 range without percentile
  quotas or forced winners.
- Separate company quality into evidence-backed dimensions such as technical
  intensity, scale/trajectory ("motion"), compensation, learning opportunity,
  prestige/selectivity, and mission. Missing evidence stays neutral and dated
  dossier evidence must remain inspectable.
- Apply diminishing returns within one ordinary dimension so a small employer
  does not become elite merely because it is health-related. Allow truly
  exceptional evidence in one dimension to exceed its normal band and partly
  offset weaknesses elsewhere.
- Make posting-specific evidence matter enough that roles at the same company
  spread across the 90s instead of inheriting the same 100. Role family,
  responsibilities, scope, level, requirements, compensation, and candidate
  wording alignment should create that separation.
- Keep the deterministic reason ledger: expose raw dimension values,
  compensating strengths, weaknesses, calibration version, and final mapping.
  AI/company research may supply cited evidence but cannot be required or set
  the final score directly.
- Calibrate against a checked-in golden fixture set spanning goal-company
  roles, strong non-health roles, high-pay/high-motion outliers, small
  health-company roles, ordinary eligible roles, and clear mismatches. Inspect
  score histograms by company, sector, and role family, but do not tune to make
  a named company win a contrived example.

Acceptance examples are directional, not hard-coded fixtures: an excellent
NVIDIA role may still be 100; nearby NVIDIA roles should plausibly be 92–99
based on the posting; a modest health employer should not rank near the top on
sector alone; and an exceptional non-health role may outrank it through a real
superpower. This Radar score remains separate from the planned private Resume
Match and post-generation Resume Craft scores.

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

## CV auto-tailoring — ✅ OWNER-FIRST V1 SHIPPED 2026-07-31 (DECISIONS #67-69, #72-74)

`CV/` remains local-only (DECISIONS #29). The first implementation is the
owner-only local Resume Studio in `scripts/resume_studio.py`. Source-only mode
selects existing evidence IDs; enhancement mode permits reviewable substantive
rewrites and multi-source synthesis anchored to those IDs. Both render through
the exact `CV/resume.tex` format
with company-first headings, immutable typography/margins/spacing, and a hard
page-density test (DECISIONS #69). The installed Codex CLI, pinned to
`gpt-5.6-luna`, performs writing plus a four-role, same-model critic jury.
Human stays
the author (DECISIONS §6): drafts are reviewed, never auto-submitted. Reports
expose known Codex token usage and do not imply an exact total when a provider
omits usage metadata.

The owner workflow now includes the private structured evidence graph, bounded
GitHub/Devpost corroboration, Resume Match scoring and Best/Newest/Resume Match
sorting, full-posting re-analysis, dynamic compile-measured page packing, and
source-addressed custom bullets. The methodology curator keeps a full
22–26-bullet portfolio across three experiences and four projects, removes
semantic duplicates, and hard-fails wrapping or excessive bottom whitespace
against the immutable human reference. The adversarial pass returns an applied
corrected plan instead of review notes for an unchanged PDF. Protected scope
qualifiers survive enhancement, and compile errors cannot trigger content
deletion. Resume Craft is a fixed weighted rubric; factuality, eligibility,
and layout are separate non-negotiable gates.

The current enhancement contract also receives the bounded CV authority dossier
and exact-term ATS strategy from the captured posting. It may swap projects and
rewrite supported evidence around target language; reports expose rewritten
lines, project swaps, and rendered coverage. Near-wrap lines (under 12pt of
right-edge safety) are rejected along with actual wraps, so historical PDFs do
not masquerade as output from the current pipeline.

Unchained generation — ✅ SHIPPED 2026-08-11. The prior Take-the-wheel behavior
remains available as the moderate checkpoint. Unchained adds a separate
requirement-to-evidence pass across the captured posting and authorized Markdown
graph, then permits source-addressed bullet synthesis and Skills tailoring for
supported gaps. Its audit distinguishes direct, adjacent, and unsupported
evidence; unsupported terms cannot pass the factual gate merely because they
appear in the posting. Outputs use a distinct company `_unchained.pdf` name.

The marginal hiring-value refinement (2026-08-08) preserves adaptive and
take-the-wheel tailoring while adding a canonical/current comparison point.
Substantive swaps, exclusions, rewrites, and unusual reorders are recorded in a
decision_ledger; the independent critic can return decision_feedback when a
change loses stronger evidence, adds only keyword similarity, creates
redundancy, or breaks chronology without a strong reason. This is an audit
layer, not a rigid numeric craft score or a blanket preserve-base rule.

The portfolio-first refinement checks composition before line polish. It flags
project overlap, unused technical alternatives, and leadership competing for
technical page space; it also lets the report explain when coursework or the
aggregated Awards line was reclaimed before strong evidence.

The evidence maintenance loop is now shipped locally (DECISION #99). Resume
Markdown files are indexed as source-addressed records, public GitHub README
material is bounded corroboration, and the collapsed Studio Evidence review
panel records explicit Confirm/Public-safe/Disputed/Private/Reject/Supersede
decisions. Public material stays blocked until a human confirmation; rejected
or private records are excluded from ranked context and future generation.
Refreshes preserve cached README records through GitHub rate limits.
Resume Studio workshop — ✅ SHIPPED (DECISION #76). Completed runs now expose
editable education, skills, experience, project, and leadership lines; AI may
return multiple source-grounded candidates for an explicit user request; and
each saved/reverted draft renders to a unique private revision artifact. The
original run and human reference PDFs remain intact. Contact/header metadata,
layout, evidence authority, and factuality guards remain protected.
Reviewer duplicate selections now merge without evidence loss, and workshop
template metadata migrates on load while preserving edits so renderer fixes do
not corrupt older private drafts.
Multi-user provider/CV storage remains later; no email/account integration is
part of the owner-first system.

Production-to-local Resume Studio — ✅ SHIPPED 2026-08-11 (DECISION #114).
Only the authenticated `@VictorJimenez3` owner UI exposes Tailor controls. A
selected production job is saved to **To apply** and passed through a URL
fragment to the loopback-only Studio, including a manually saved job
description when available. The source CV/evidence graph and generation
execution never cross the local privacy boundary; an explicit owner sync may
copy generated bank artifacts into the private cloud bank. A login service
keeps the Studio available on Victor's Mac.

Resume library/preview surface — ✅ SHIPPED (DECISION #75). Resume Studio now
indexes saved runs and legacy architecture experiments, separates “create a
new run” from “Resume bank,” snapshots the selected posting, preserves prior
results when switching roles, and exposes company-identifiable PDF names for
new runs. The editable workshop and revision history are now the next layer on
top of that durable bank.

Cloud Resume Studio control plane — ✅ SHIPPED 2026-08-14 (DECISION #123).
The production owner view is now the single Resume Studio UI: Jobs rows and
role drawers open directly into it, with posting selection, private-engine
health, full-match handoff, four tailoring modes, queued-run status, and bank
links in one workspace. The 2026-08-15 bank closure adds a private,
owner-authenticated Google Drive artifact bank and local-to-cloud sync for all
current and legacy entries, including PDFs, previews, reports, and posting
snapshots. The Mac engine remains the private fail-safe for source CV/evidence,
provider calls, rendering, and workshop edits; when it is offline, already
synced artifacts remain viewable but new generation and workshop actions stay
disabled.

Grouped Resume Bank and durable evidence Q&A — ✅ SHIPPED 2026-08-15
(DECISION #127). The cloud bank now groups all variants under one posting and
defaults to the post-overhaul Google-onward quality era. The private engine
turns unsupported posting terms into deduplicated global questions, stores
owner answers locally, projects only affirmative concrete answers into the
evidence graph, and exposes source/authority provenance in the cloud UI.

Visual ATS audit and evidence-place Q&A — ✅ SHIPPED 2026-08-16 (DECISION
#129). Every report-backed bank version separates covered terms,
supported-but-omitted terms, and unsupported gaps; newer previews position
review-only highlights over the exact PDF lines. Context questions can propose
or save a role, project, course, or repository to investigate without turning
that clue into claim-authorizing evidence.

Owner-only objective Resume Bank comparison — ✅ SHIPPED 2026-08-16
(DECISION #128). Each posting card now ranks its finished variants with a
transparent target-fit, evidence-safety, layout-safety, and portfolio rubric.
The result is explicitly a deterministic shortlist aid rather than a hidden
AI/hiring prediction; unavailable independent review is surfaced as a
confidence limitation.

Comparative tailoring quality control — ✅ SHIPPED 2026-08-20 (DECISION #144).
Each current run now preserves a deterministic job-intelligence snapshot and a
base → tailored audit. Fit remains separate from tailoring uplift; change-level
findings expose supported gains, regressions, missed evidence, questionable
rewrites, and unsupported claims. Factuality, eligibility, privacy, layout,
and independent-review failures produce visible `blocked`/`review` states
instead of disappearing inside an overall ATS score. The cloud surface receives
only a sanitized summary because the full evidence-grounded audit remains on
the private Mac.

Comparative audit adjudication and bounded repair — ✅ SHIPPED 2026-08-20
(DECISION #145). Source-aware explanations prevent deliberate, evidence-backed
project swaps from being misclassified as regressions; the report now chooses
`prefer_tailored`, `prefer_base`, or `needs_review`. A single repair pass is
allowed only when the deterministic comparison finds material loss, and its
compiled candidate must beat the prior preference key. Independent-provider
absence remains a visible review limitation.

Permanent role-family comparison controls — ✅ SHIPPED 2026-08-23 (DECISION
#179). The owner can explicitly promote a synced, approved tailored winner to
one reusable General SWE / Cloud, Healthcare / Scientific AI, ML / Research,
or Data / Analytics control. The cloud registry keeps history and revokes the
previous active control for that family. The local engine verifies the private
run, approval, tailored winner, and source artifacts before using it as a
secondary comparison reference; every invalid or unavailable reference falls
back to the immutable canonical resume, which remains the hard audit floor.

Evidence-preserving tailoring lab — ✅ SHIPPED 2026-08-22 (DECISIONS #166-173).
Ordinary High-effort lanes no longer trade away distinctive mechanism or
validation evidence to consume microscopic measured page capacity; geometry
recovery remains hard-gated and source-preserving. A project-level role-evidence
floor is retained as an explicit lab helper but disabled by default until it
earns a sealed positive comparison. Fresh-open post-fix tests recorded a real
`prefer_tailored` Uber result and correctly held Nucleus/ByteDance at the base
when application or evidence blockers dominated. Source-level provenance lint
now rejects uncited technical claim merges before the jury, and canonical-loss
explanations require exact source-level accountability.
Generation also receives a compact evidence-backed supported-skills checklist;
the first Nucleus replay improved defensible API/access-control recall but still
failed the comparative audit, so the checklist remains a measured hypothesis
rather than a keyword-completion rule.
Planner-denial normalization now prevents negative mentions such as
“AWS is unsupported” from becoming supported generation opportunities; the
corrected Nucleus replay moved tailoring from regressed to improved but still
failed closed on eligibility and portfolio quality.
Canonical project swaps now require every omitted bullet source ID in their
ledger, closing the remaining project-level explanation loophole found in the
Stryker replay.
Requirement-term association is now checked at normalization so the planning
map cannot turn generic ATS vocabulary into false role-specific evidence.
Portfolio search also honors the sealed panel's hard-failure state: a relative
`prefer_tailored` result cannot replace the base when any non-averagable critic
gate fails. The post-fix Stryker replay explored three complete candidates in
parallel and kept the base when none produced a material positive win.

## Pipeline intelligence
- **Resume UAT and outcome learning** — future idea: if enough applications,
  interviews, and eventual outcomes accumulate, compare resume variants and
  evidence choices against response/interview rates. This would be an
  observational success-rate loop, not a substitute for human review; it
  needs sufficient volume and consistent application tracking before the
  results mean anything.
- **Response-rate analytics** — per-sector/per-source conversion rates in the
  strategist memo: "healthtech replies 3× more than big tech for you — shift
  volume." Becomes meaningful after ~30 tracked applications.
- **Interview-loop dossiers** — when Stage hits Interview, auto-generate the
  company's known loop structure, question themes, and prep checklist
  (local-LLM job, so it's free on the Mac).
- **Offer-comparison calculator** — feeds the Offer Playbook scorecard with
  real numbers: equity discounting, COL adjustment, side-by-side memo.

## Reach
- **Role-aware recruiter search — ✅ SHIPPED.** Every job's Outreach tab builds
  targeted public Google queries for university recruiters, technical
  recruiters, likely hiring managers, NJIT alumni, and public LinkedIn hiring
  posts. They are outbound links only; the radar does not scrape LinkedIn or
  persist search results.
- **Referral-finder** — cross NJIT alumni signals against target companies;
  drafts the outreach note into the Networking CRM. (Needs a data source that
  isn't LinkedIn scraping — evaluating SHPE directory + GitHub org members.)
- **Recruiter asking/outreach assistant — ⏸️ DEFERRED.** Build on the shipped
  public recruiter searches by identifying a reviewable contact candidate,
  drafting a role-specific ask, and requiring Victor's approval before any
  send. Never scrape LinkedIn and never auto-message people.
- **H-1B/sponsorship cross-check — ✅ SHIPPED 2026-08-07 (DECISION #83).** A
  weekly refresh joins radar companies to the latest official DOL OFLC LCA
  disclosure quarters and adds `likely`, `no-history`, or `unavailable`
  company context to job records, dashboard rows, filters, and the Fit drawer.
  Posting wording remains the primary per-role evidence; DOL history never
  adds score points and is explicitly not a promise that a requisition sponsors.
- **Meta careers adapter** — auth-gated GraphQL today; revisit if they ship a
  public search endpoint. Covered by aggregators meanwhile.
- **SHPE deep mode** — rep CRM per booth, session planner, live exhibitor sync
  from careercenter.shpe.org, SHPExchange resume-book optimizer. Light mode
  (exhibitor boost + battle plan) shipped in `docs/SHPE.md`.

## Content
- **Auto cover-letter skeletons** for S-tier roles only, same review-file
  flow as CV tailoring above.
- **Hybrid Simplify + Job Radar Autopilot — ✅ SHIPPED 2026-09-02
  (DECISION #212).** Simplify owns the fast first pass through its supported
  shortcut; the paired Mac finisher owns deterministic reconciliation, the
  byte-verified mass-apply resume (with opt-in tailoring fallback), role-scoped
  writing, exact provider-specific submission validation, and phone receipts.
  Up to three tabs run concurrently with a serialized Simplify trigger. Queueing
  authorizes one exact-once Submit by default, while **Review first** creates a
  durable page-bound review. Managed Postgres stores per-run state, events,
  revisions, leases, and worker heartbeats; Sheet/Drive remains a compatibility
  fallback. Missing facts, unusual legal disclosures, CAPTCHA/MFA, and
  submission uncertainty remain visible owner blockers.

## Ops
- **Company-research backlog throughput — ✅ checkpointing + retry visibility shipped.** The
  manual backfill now prioritizes saved/tracked employers, then alert-worthy,
  high-score, and fresh roles; it uses GLM-first synthesis with a bounded
  timeout, commits each small cycle before moving on, and has a 30-minute
  continuous relay until the queue is empty. It reports retry-waiting/error
  records separately. A cancelled or rate-limited run resumes from its last
  checkpoint instead of losing the entire process or hot-looping one
  unavailable provider.
- **Calendar sync** — interview emails → Google Calendar holds.
- **Weekly Notion rollup** — mirror the Monday memo into Job Search HQ.
- **Registry hygiene job** — ✅ SHIPPED 2026-07-16 (`discovery.hygiene`,
  monthly inside enrich): dead boards get a fresh probe cycle every 30 d,
  non-seed 90-day invalids are pruned, and duplicate employer entries that
  stopped producing while a sibling still does are parked as `dup`
  (producers are never touched).
