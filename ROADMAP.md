# Roadmap — designed, deliberately deferred

The dream-system backlog. Each item is scoped enough to build on request;
none block anything currently running.

## Active backlog — ordered priority (2026-07-18)

These are the next product decisions, in order. They are intentionally written
as a pause point so future implementation starts from the same priorities.

1. **AI functionality foundation / knowledge layer.** Move beyond the current
   mostly deterministic radar: define the AI service boundary, provider/model
   configuration, grounded retrieval, citations, structured outputs, caching,
   evaluation cases, and privacy rules. The AI must know the candidate profile,
   job evidence, company sources, and prior decisions instead of producing
   unsupported summaries.
2. **Company research overhaul.** Replace thin one-line company descriptions
   with useful, source-backed dossiers: what the company makes, who it serves,
   industry context, products, mission, business model, size/stage, technical
   work, location context, sponsorship history, and why the company may matter
   to this candidate. Show source dates/links and clearly label estimates.
3. **Google collaboration and pluggable tracking.** Add Google account OAuth
   and a setup choice between Notion and Google Sheets as the tracking backend.
   Sheets should be a first-class, template-backed option for people already
   living in Google Workspace; the application model must remain backend-
   neutral so users can switch without duplicating entries.
4. **RAG and vector search.** Embed job descriptions, company dossiers,
   candidate profile/CV material, and saved decisions; support semantic search
   and similarity-based ranking with explainable evidence. Keep deterministic
   gates authoritative and log retrieval/similarity reasons. This supersedes
   the currently parked posting↔profile RAG spike below.
5. **CV-aware target-role toggle.** When a CV is available, add a `CV` option
   to the existing “all target roles” dropdown. It should show roles that can
   be meaningfully tailored to the selected CV, then offer a local, review-only
   tailored draft. Personal CV content stays local and never enters public
   state.
6. **Interview workspace (far future).** Add an Interview tab that accepts a
   company name and builds a grounded preparation packet: company mission,
   products, current context, role-specific expectations, likely interview
   stages, question themes, and candidate questions. This depends on the AI
   knowledge layer and company-research overhaul.

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
