# Roadmap — designed, deliberately deferred

The dream-system backlog. Each item is scoped enough to build on request;
none block anything currently running.

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

## Deliberately deferred by Victor

1. **Scoring/state maintenance hardening — ✅ foundation shipped.** CI now
   fails when any stored job lacks the current `score_version`, and a scheduled
   six-hour rescore rebuilds from a fresh upstream snapshot before publishing.
   Rules v7 also classifies (but never alerts) no-experience-floor technical
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
page-density test (DECISIONS #69). Victor's installed first-party Codex and
Claude Code clients perform planning and fixed adversarial review. Human stays
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

Resume Studio workshop — ✅ SHIPPED (DECISION #76). Completed runs now expose
editable education, skills, experience, project, and leadership lines; AI may
return multiple source-grounded candidates for an explicit user request; and
each saved/reverted draft renders to a unique private revision artifact. The
original run and human reference PDFs remain intact. Contact/header metadata,
layout, evidence authority, and factuality guards remain protected.
Multi-user provider/CV storage remains later; no email/account integration is
part of the owner-first system.

Resume library/preview surface — ✅ SHIPPED (DECISION #75). Resume Studio now
indexes saved runs and legacy architecture experiments, separates “create a
new run” from “Resume bank,” snapshots the selected posting, preserves prior
results when switching roles, and exposes company-identifiable PDF names for
new runs. The editable workshop and revision history are now the next layer on
top of that durable bank.

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
