# Job Radar — user guide

This is the manual for *using* the radar day to day. For how it's built, read
[README.md](../README.md); for why it's built that way, [DECISIONS.md](../DECISIONS.md).

## What it is, in one paragraph

Every ~30 minutes, GitHub Actions crawls ~700 company job boards and the big
new-grad aggregators, scores each posting against your preferences
(profile.yaml), and delivers each new high-scoring posting as its own assigned
GitHub issue that pushes to your phone. A separate unassigned master board keeps
everything together. Checking a box puts the job in the in-house Pipeline and
mirrors it to your selected tracker. For Victor's `VictorJimenez3` account,
Notion is the default primary tracker; Google Sheets is an optional personal
mirror in the expanded Tracker options. On the Vercel platform, each user can connect Google to create a private
Google-backed tracker in their own Drive without touching the repository owner's pipeline. GitHub-only users can connect Google later. Your MacBook
handles local bulk AI when awake, while a tightly budgeted NVIDIA cloud pass
handles time-sensitive enrichment nightly. The repo itself is the database.

## 🖥️ The Platform (start here)

**https://victorjimenez3.github.io/fable-job-search/platform/** — the whole
system as a website, refreshed automatically by every crawl:

The production Vercel door also exposes the staged **vNext** workspace at
[`/vnext/`](https://job-radar-newgrad.vercel.app/vnext/). It is the faster,
mobile-first path for Jobs, Applications, and Companies: Jobs are fetched in
cursor-sized pages, long lists are virtualized, and API responses are checked
before they reach the UI. Until the Postgres migration is activated, public
Jobs fall back to the current repository snapshot and private/Resume/Settings
workflows link to the classic platform. Nothing in the existing workflow is
removed during the cutover.

vNext names three signals that the combined classic score can make easy to
confuse: **evidence score** (what the posting proves), **eligibility** (whether
deterministic gates allow it), and **priority** (Victor-specific
goal/recommended ordering). The classic `score` remains available during
migration for compatibility.

- **Jobs**: every role the radar has ever seen, with persistent dropdowns for
  role family—including the low-priority **Product / project management**
  lane—posting sponsorship, official DOL sponsor history, experience,
  sector, and pipeline status. Each row shows visa/years badges—including the
  important difference between "not stated" and "not analyzed"—plus historical
  sponsor context, score, salary, culture, and age. DOL history is company-level
  context only; the posting's own visa wording remains primary and no-history
  does not mean a company will not sponsor. The
  **apply ↗** button starts the owner Application Agent when the Chrome
  extension is paired; otherwise it opens the employer posting normally. When
  you are signed in it also saves a new role to **To apply** without claiming
  that it was submitted.
  You can also click a Jobs row once to save it (green), then click that saved
  row again to exclude it (red). Turn on **show excluded** to restore a red row;
  this is a reversible view preference, not a score or notification change.
  New-grad Jobs opens to a **Fresh action queue** with entry-compatible or
  unclear experience and postings from the last month. Tracked roles stay
  reviewable even when they are older or experienced; use the explicit filters
  for the full research board. Expired and filled roles are kept in **History**
  instead of active Jobs.
- **Progressive loading**: the shell and Jobs board appear first, then optional
  repository panels load independently. If a state source fails, the rest of
  the site remains usable. Only the signed-in `VictorJimenez3` owner sees the
  in-app developer notice and retry details; no email or automatic issue is sent.
- **Pipeline**: separate Maybe, To apply, To tailor, Applied, OA, Interview,
  Rejected, and Closed lanes. The selected tracker is read back twice daily;
  "Maybe" remains a platform-only scratch lane. Queueing a Resume Studio batch
  moves successful drafts into To tailor; it never marks them Applied.
- **History**: expired and filled postings are removed from active Jobs and
  alert surfaces but remain here with their last source sighting, close reason,
  and lifecycle events. Application-history cards also show how long the
  posting was up (in days, months, or years) instead of a lifecycle date. This
  retained dataset supports future posting-timeline analysis; it is not a
  second active application board. The scheduled radar first checks a bounded
  batch of still-open aggregator pages in parallel, so a Jobright page that
  visibly says “This job has closed.” is moved here even if the larger
  discovery crawl is slow.
- **Per-job workspace** (click the title or details ▸), four tabs: **Fit &
  eligibility** opens first with role family, posting sponsorship, DOL sponsor
  history, required years, location, salary, posting age, score reasons, a short
  why for each score section, and the LLM verdict; it also
  has a paste box for descriptions the radar cannot scrape (graded on the next
  enrich cycle). **Company** has a claim-level cited employer brief, its
  official evidence, a separately labeled culture card, and external research
  links; **Outreach** has role-aware public Google searches for university and
  technical recruiters, likely hiring managers, NJIT alumni, and public hiring
  posts, plus message templates and saved conversations. These links never
  scrape LinkedIn; **Notes** holds your working notes
  (keep them non-sensitive because synced web state is public).
- **Companies**: open-role employers, prioritized by actionable role score,
  with sourced summaries, research freshness, culture signals, and coverage.
- **Interview**: OA/interview applications with cited company context and a
  role-prep checklist.
- **My job preferences** (owner only): a readable view of the saved/applied-role sample,
  explainable learned contributions inside the Radar score, similar roles to
  inspect, and explicit ranking feedback. The sample changes only the bounded
  personal-signal dimension; it never overrides eligibility gates.
- **AI**: per-run budget, task mix, provider/model health, grounded-research
  coverage, scout status, and registry stats.

### New-grad and internship lanes

Use the lane switch at the top of the platform to choose **New-grad** or
**Internships**. New-grad is the default and remains the priority compute
lane. Internship jobs, Pipeline entries, web notes, alert history, master
boards, and generated dashboards use their own `intern_*` state and never
appear in the active new-grad list.

In **Settings**, enter an expected graduation month such as `May 2029`. The
internship lane uses that date together with an internship's start term to
label likely freshman, sophomore, junior, or senior fit. A posting with no
clear class-year or graduation evidence remains visible as **unknown/open**;
it is not silently filtered out. The cohort dropdown in Jobs lets you filter
match, mismatch, open, or unknown roles.

If Google is connected, the private workbook has separate **Applications**
and **Internships** tabs plus **Preferences**. Switching lanes changes which
tab is read and written. The app requests Drive file access only; it does not
request Gmail scope or read internship email.

Internship scores are intentionally neutral 0–100 scores for friends rather
than personalized to Victor. Technical role families start evenly; the rubric then
uses normalized published pay, recognized or cited employer signal,
mentorship, ownership, technical depth, production/user impact, return-offer
evidence, student eligibility, and freshness. Saved roles, sectors, remote
preference, feedback, applied history, and new-grad role weights are ignored in
this lane. Missing pay, work evidence, or employer recognition contributes zero
instead of a penalty, and **Why it scored** exposes the exact reasons.

Prestige is its own general-opportunity dimension: Google, NVIDIA, Microsoft,
OpenAI, Anthropic, and comparable big-tech or AI-lab employers receive a
strong top-end signal. It measures broad technical "crackedness," not Victor's
saved company preferences.

Full-time protection is deliberately one-sided: clear full-time-only wording
without internship or student evidence becomes review-only and cannot alert,
but remains in the data so an incorrectly labeled internship is not silently
lost. The normal internship list is cleaned to positive title/body evidence;
source-only, unknown, and senior-style outliers stay available through the
**include review-only / no-title rows** filter.

Internship email batches are off by default. The owner can enable
**internship batches** in Settings; **new-grad batches** have a separate
toggle and default to on. GitHub issue/board surfaces remain available without
email delivery.

Reading needs nothing. On Vercel, use **Tutorial → Accounts & login** to sign
in with GitHub or Google. The memorable shortcut and the original Vercel URL
stay signed in together; OAuth may briefly use the original callback host, but
the account center returns you to the URL you started from. Google consent includes least-privilege Drive file
access and creates your personal workbook with separate Applications,
Internships, and Preferences tabs; if you started with GitHub, use **Connect Google + create my
Sheet** from the same signed-in session. There is no password login. On the
Pages mirror, actions use owner-reviewed prefilled GitHub issues and the site
never asks the browser to store a repository token.
The private Google-backed tracker is one workbook per connected Google account;
the Sheet is never sent to the browser wholesale. GitHub checkboxes keep working
exactly as before; Vercel writes to the connected user’s Sheet.

Inside a role drawer, the repository owner can submit fixed-category ranking
feedback or archive an expired/filled posting after GitHub sign-in. Feedback is
written to the structured state and the generated `docs/FEEDBACK.md` audit;
archive is recoverable and does not erase the crawler history. The crawler also
marks definitive dead links as **expired** or **filled** automatically. For
Victor, a tracked terminal page is soft-archived to Notion's recoverable trash.
Other signed-in users receive the same status in the **Posting Status** column
of their private Google Sheet and an in-app notice; their application Stage is
left unchanged. Other GitHub users can report a stale posting through a
prefilled issue. Reports are deduplicated by the issue author's GitHub login,
and three distinct reporters bring the posting to the owner's review queue
without automatic deletion.

The separate [ChemE internship board](https://job-radar-cheme.vercel.app)
uses its own jobs, scoring profile, pipeline state, and labeled GitHub issues.
It shares this repository's Notion integration, so tracked ChemE roles enter
the same Applications database instead of creating a second Notion system.

## Private Application Autopilot

The owner-only **Autopilot** tab is the phone/control surface for the paired
Mac Chrome extension. It supports one-role launch from a Jobs card and a
sequential batch queue. The first adapters are Workday, Greenhouse, Lever,
Ashby, and SmartRecruiters; other pages use a conservative generic fallback.

Set it up once:

1. Sign in as `@VictorJimenez3`, open **Autopilot**, and choose **pair Mac**.
2. Copy the one-time token into the unpacked `browser-extension/` popup after
   enabling Developer mode at `chrome://extensions`.
3. Keep the private Resume Studio service running at
   `http://127.0.0.1:4317/` (the launchd installer can keep it alive).

The extension sends visible field labels, control types, options, and a
page-shape fingerprint to the loopback agent. It never sends raw page HTML,
cookies, passwords, or the CV to Drive. The local JSON bank is mirrored to the
owner's existing private Job Radar Google Sheet in an **Application Agent** tab
when Drive storage quota is full. If the Sheet is unavailable, the existing
app-created Drive folder remains the JSON/Markdown fallback. Use the Radar UI
to edit answers; stored Sheet payloads and Markdown are readable mirrors, not
the authoritative editor.

Before opening a form, the extension checks Resume Studio for a safe tailored
PDF for that exact posting. If none exists, it starts the existing AI tailor
run and waits. A rejected or not-yet-approved tailor falls back to the
immutable canonical resume, while the browser still pauses at the local
resume-file picker because Chrome cannot silently choose a file for a page.
All other approved repetitive fields and ordinary **Next** pages can continue
until a real answer, attestation, sensitive field, or final confirmation needs
you.

Approved answers—including approved sensitive answers—may be reused, but the
full proposed values appear on the final review card. The agent stops for a
new essay, unknown required/sensitive field, file upload, attestation, selector
failure, or changed page. Phone confirmation is a single-use, 15-minute
approval tied to the review hash and page fingerprint. A blocked role stays in
the queue while later roles can continue. Repeated adapter problems go into
the private issue ledger; ask Codex to repair them so each fix includes a
fixture and regression test.

## Private Resume Studio

Resume Studio has one user-facing cloud control plane. In production, sign in
as `@VictorJimenez3`, choose a role, then use **Tailor**, the drawer's
**Resume** tab, or the owner-only **Resume Studio** tab. The cloud workspace
keeps the posting, match, queue, run status, and resume bank together. It opens
a small loopback bridge to the Mac engine only when private matching or
generation is requested. The source CV/evidence graph and provider execution
remain local, while the owner can sync generated bank artifacts into a private
Google Drive folder for cloud viewing.

From the repository on the Mac, start:

```bash
.venv/bin/python scripts/resume_studio.py
```

Or install the login service once so production links always work:

```bash
bash scripts/resume-studio-service/install.sh
```

You can also stay in the production platform: sign in as `VictorJimenez3`,
open **Resume Studio**, or press **tailor** beside any Jobs posting. That is
the canonical cloud control plane for the same private engine. It shows the
posting, connection state, match, queue, run status, and resume bank in the
main platform UI. If the Mac service is asleep, the cloud page remains a safe
read-only posting/apply surface and keeps generation disabled until the engine
is reachable again. **Resume bank** still shows artifacts already synced to
the private cloud; use **sync local bank** while the Mac is awake to copy all
entries in the selected scope. The default **Google onward** scope starts at
the first post-overhaul Google run; choose **all history** before syncing if
you deliberately want legacy, failed, or interrupted experiments too. The
bank shows one job card; click it to see every version for that posting.

Use **Tailor today** in the Resume Studio header for a batch pass. It finds
roles added to your Pipeline on the current local calendar day, preselects up
to 12, and lets you choose one mode before queueing. You can review or clear
selections first. The private engine runs at most two at once, and the batch
only creates reviewable Resume Bank drafts—it moves successful roles into the
Pipeline's **To tailor** lane, does not change them to Applied, and never
submits an application. If you use Victor's Notion tracker, add a `To tailor`
status option manually; Notion's API cannot create status options.

Inside an expanded job card, **Objective ranking** sorts the finished variants
for that posting and marks the current winner. Open **show rubric sources** to
see the target-fit, evidence-safety, layout, and portfolio inputs. It is a
private comparison aid, not an automatic application choice or a prediction of
what a hiring manager will do; missing independent review is shown as a limit.

The bank's **Permanent role controls** section is deliberately opt-in. After
you inspect and approve a tailored PDF, sync that entry to the cloud bank and
promote it for the matching family—General SWE / Cloud, Healthcare / Scientific
AI, ML / Research, or Data / Analytics. The next posting in that family can
show the approved version as a reusable comparison reference. It does not
replace the locked canonical resume, change evidence authority, or bypass the
tailoring audit. If a control is missing, revoked, stale, or not actually
approved, Studio uses the immutable default automatically. Promoting another
control for the same family retires the old one but keeps its history; **revoke
control** returns that family to the immutable default.

Open **Context & Q&A** to inspect what the tailoring engine can actually use.
The right side lists authorized facts and their exact source. The left side
lists capability gaps raised by any posting. Answer **I used this** only with
where/when plus what you did; that answer becomes reusable owner-confirmed
evidence for later jobs. **I have not used this** is remembered as a known
absence and can never authorize a resume claim. These answers stay in the
private Mac evidence workspace, so the context panel requires the engine even
when generated PDFs have been synced to the cloud. Expand **Possible places to
investigate** to see project/role/course suggestions derived from neighboring
evidence, or add a custom place and optional repository URL. Clicking a hint
prefills a specific personal-contribution question. A hint is never evidence
until you submit a concrete affirmative answer. Use **not in this place** when
a suggested project or role is wrong; the broader capability remains open.

Expand **ATS keyword map** beneath any saved version to grade the tailor. Green
means the exact supported posting term is visible in that resume, yellow means
the evidence bank supports it but this version omitted it, and red means the
evidence does not currently support it. Red terms link back to Context & Q&A.
For newer drafts, the embedded preview highlights the exact rendered lines;
green is a supported posting term, blue is a rewritten line, and purple is
both. This overlay is review-only—the linked PDF is unchanged. Treat coverage
as a diagnostic comparison, not a promise about any employer's ATS.

Also open **Tailoring audit** on a saved version. **Fit** describes the
candidate's supported match to the role; **Tailoring** describes whether this
version improved the evidence selection and communication relative to the
original resume. Review the supported gains, dropped or omitted evidence,
redundancy warnings, explained tradeoffs, and blockers. The audit also gives a
recommendation: **prefer tailored**, **prefer base**, or **needs review**. An
explicit source-backed project replacement is not automatically a regression,
and omitting low-priority coursework is not treated like losing core technical
evidence. If material loss remains, one bounded repair pass tries to improve the
plan and keeps it only when the compiled comparison improves. `ready`, `review`,
and `blocked` are readiness states, not a single hiring score: unsupported
claims, eligibility conflicts, layout failures, privacy failures, and missing
independent review remain visible as hard constraints.

- **Unchained generation** — performs a requirement-by-requirement evidence
  audit before drafting, searches the authorized Markdown graph, and may create
  new grounded bullets or Skills lines to close supported gaps. Unsupported
  requirements remain visible instead of being stretched into claims.
- **Take-the-wheel (moderate)** — the preserved adaptive mode. It can make a substantial portfolio
  change, surface deeper unused evidence, or write a new role-specific line
  when the expected hiring-value gain is real.
- **AI tailor** — the same evidence graph and review process with a higher bar
  for replacing an already-strong line; useful when you want adaptive tailoring
  with less creative variance.
- **Used bullets** — selects target-relevant approved source IDs and wording
  without creative rewriting; use it as the clean comparison baseline.

Unchained is not an unguarded writer: all modes share source authority,
factual and qualifier checks, chronological job order, one-page compilation,
one-line geometry, and owner approval. The normal screen puts Take-the-wheel
alongside the separate unchained option; the mode guide explains the
tradeoff in place. All modes render through the exact
`CV/immutable/VictorJimenezResume.tex` structure. Models cannot write the
document, alter its margins/typography/spacing, or bypass factual, duplicate,
one-line, and compile gates. Codex writes/synthesizes with the `gpt-5.6-luna`
model pin; Claude independently critiques when its first-party subscription CLI
is installed. There is no local-model or API fallback. The report includes separate gates,
target-specific omissions, and observed provider usage; it has no composite
craft score. A run stays `awaiting_review` until Victor approves a ready draft.
Drafts, prompts, source context, and provider sessions live only under
`CV/.resume_studio/`. An explicit owner sync copies generated PDFs, previews,
reports, and posting snapshots to the private cloud bank; it does not upload
the source CV or evidence graph. Review the generated PDF and report before
using any application material. The system preserves the master CV as the
evidence bank and never auto-submits a resume. Use **Resume bank** in the
header to revisit any saved run or legacy experiment; selecting another
posting does not remove the previous result. Select **View audit** on a saved
run to rehydrate the same visual audit used for new runs. New PDFs use a company-identifiable name such as
`mayo_clinic_resume_ai.pdf`, and the preview response preserves that name when
the file is opened or downloaded. Project headings use `|` separators. TICC is
never emitted by generation or workshop editing, even when it appears in a
local historical source; the source files themselves are not changed. Each new
run stores a private posting snapshot beside its artifacts.

For an AI-enhanced run, the report's **What changed** block lists meaningful
rewritten source lines and project swaps; near-copy paraphrases are suppressed
and the authorized source wording is retained. **ATS terms** shows exact supported terms
that made it into the rendered draft, supported terms still missing, and
requirements your evidence cannot support. Space QA labels unused width as
roomy lines instead of confusing it with whitespace or a wrap. If the posting
text was not captured, the report says so instead of pretending the draft was
keyword-tailored. A completed draft must also leave at least 12pt of right-edge
safety on every bullet; near-wraps are rejected and remain visible as failed
run diagnostics.

### Workshop editing

Open **Workshop** from a saved run in Resume bank. Education and Technical
Skills are available alongside experience, project, and leadership lines; the
contact header and LaTeX layout remain protected. The editor shows readable
résumé wording, while unchanged lines retain their existing emphasis. **Save
line** renders a new revision in a unique private folder, **Ask AI about this**
returns candidates without applying them, and **Revert** creates a new revision
from any earlier saved version. The original company-named PDF remains intact.

To create a temporary varied-role calibration set for labeling bullets, run:

```bash
.venv/bin/python scripts/resume_calibration.py --generate --count 8 --serve
```

The page at `http://127.0.0.1:4321/` displays the rendered PDFs, not the LaTeX
source. Labels and explanations are stored in the same ignored
`CV/.resume_studio/calibration/` batch as the generated cases.

Use the sort menu to switch among Radar score, newest posting, and private
Resume Match. The title-only match is deliberately labeled low-confidence;
after selecting a role, **Analyze full posting match** fetches the job page and
recomputes the fixed rubric when readable. The result shows capability
coverage, gaps, evidence confidence, and private source-backed score.

On the Jobs tab, Best Match also has a lookback selector for hours, days, weeks,
or months. It limits which recent postings are ranked while preserving the same
match scoring.
The default new-grad Jobs queue uses the last month plus entry-compatible or
unclear experience; older/experienced tracked roles remain available through
explicit filter choices.

The generator does not force every role to use the same evidence. It creates a
strongest-first adaptive pool, preserves reverse chronology, and packs only
verified lines that fit the locked one-page geometry. If measured capacity
remains, a separate evidence pass may add unused source lines or document a
low-value project/leadership tradeoff; a new project or experience must earn
its heading with two bullets, and core experience is never trimmed. The final
pass compiles every candidate and keeps a draft in review when any gate fails.
If an enhancement drops a scope-limiting source fact such as `synthetic`,
`prototype`, or `POC`, that bullet reverts to approved source wording. Resume
Craft measures the argument and writing; factuality, eligibility, and layout
remain separate gates that cannot be averaged away.

## The places you look

| Where | What you see | When to look |
|---|---|---|
| **"📌 Master board"** issue ([Issues tab](https://github.com/VictorJimenez3/fable-job-search/issues)) | Every open alert-worthy role in ONE place, best first — no bouncing between issues. Extra pages are in its comments; checkboxes work everywhere, and already-tracked jobs show pre-checked | When you sit down to browse/check jobs |
| Individual **"🎯" alert issue** | One new high-scoring role | When your phone buzzes (GitHub app push / email) |
| **"📌 Master board"** issue | All open alert-worthy roles in one place; intentionally unassigned | When you want the full browseable list |
| **"🏆 Best of \<date\>"** issue | The day's top 10, posted each evening — GitHub emails it to you | Evening email |
| **Notion "2026 Applications"** | Every job you checked, plus your real pipeline | When applying / updating statuses |
| [docs/DASHBOARD.md](DASHBOARD.md) | Everything decent the radar has seen, ranked — not just alert-worthy | Browsing for more options |
| [docs/feed.xml](feed.xml) | Same alerts as RSS | Only if you use a feed reader |

Plus a **Monday strategy memo** (its own GitHub issue): pipeline stats,
follow-up nudges for week-old applications, and LinkedIn hiring-post leads.

## The core loop

1. Phone buzzes → open the individual alert issue.
2. A line looks like:
   > ☐ 🔥 **Tempus** — [ML Engineer, New Grad](…) · Chicago, IL · `88` · **health technology** — precision-medicine data platform
3. Interested? **Tap the checkbox.** Within a minute a GitHub Action fires and
   the job appears in your Notion Applications database with status
   **Not started**. Victor can expand **Tracker options** to enable a Google
   Sheets mirror; it is never enabled merely because Google is connected.
   (The action also teaches the ranker you like companies like this.)
4. When you use Autopilot, the extension fills approved repetitive fields and
   pauses on missing answers, essays, unknown sensitive fields, attestations,
   or a final review. A Submit click requires your full-card confirmation. A
   successful confirmed click is reported back to the private queue; email and
   tracker reconciliation remain the final lifecycle safety net.
   For a live posting you find outside the radar, open the platform's
   **Pipeline** tab and use **Add a role you found yourself**. It creates a
   saved To apply item in the in-house tracker and Notion, clearly marked as a
   manual entry; it does not generate an alert or claim new-grad eligibility.
5. Not interested in a company? Comment `skip Acme Corp` on the issue —
   similar roles get downranked.
6. New-grad evidence is the first gate. Among eligible roles, AI/ML and data
   science lead, then general SWE, then data engineering/systems; health,
   sports, video games, education, big tech, and AI labs receive strong field
   fit. Marquee employers add competitive context but never bypass new-grad or
   role fit. A twice-daily sweep re-reads every issue so no checked box is ever
   missed, and the local AI scouts new healthcare/wearables employers to track.

## Comment commands (on any radar issue)

| Command | Effect |
|---|---|
| `applied <url>` | Log an application immediately as Applied — works for jobs the radar never saw. If you'd checkbox-saved it, the same Notion page is updated, not duplicated. |
| `skip <company or job id>` | Downrank this company's roles in future scoring |
| `culture <company>` | Get a reply with the company's culture dossier (WLB, pace, prestige, fit score) |
| `track <ats> <token> [Name]` | Force a company into the crawl registry, e.g. `track greenhouse stripe Stripe` |

## The AI layer (what's "smart" and what isn't)

- **Scoring is deterministic, not AI.** Gates require verified new-grad or
  early-career evidence, reject required 1+ years, and exclude senior/intern/
  clearance/off-field roles (US-only). Technical graduate and rotational
  leadership programs are a dedicated exception. Eligible roles then use an
  auditable point rubric prioritizing AI/ML and data science above SWE/systems;
  PM-family rows have role weight `0`, remain dashboard-only, and never enter
  alert email/RSS delivery. Google technical new-grad roles have an explicit
  profile-driven `100` favorite override with a printed reason. Every score has
  printed reasons. This runs in the cloud with zero API keys.
- **Your MacBook is the bulk AI worker for radar enrichment.** A background job (launchd, every 2 hours
  while the laptop is awake) pulls the latest state, runs **qwen3:30b through
  Ollama locally** (free, private, ~19 GB on disk), and pushes back:
  - a one-line *angle* per alert ("emphasize your clinical-data project"),
  - source-grounded company briefs when official evidence exists,
  - re-ranking of borderline jobs.
  The cloud never waits on the Mac; a nightly NVIDIA router also processes a
  small priority queue with hard call/request limits and cross-model fallback.
  Model memory is released after each run (`keep_alive: 0` + `ollama stop`).
- **Observable and bounded:** the AI tab shows task/model successes, errors,
  reported tokens, and the run cap. Prompts/keys are never stored.
- **Owner-only email lifecycle connector:** the preferred Gmail integration
  uses the read-only Gmail History API and advances its cursor only after a
  complete successful pass. IMAP remains available for another provider or an
  existing App Password setup. Ambiguous matches go to review, transitions are
  forward-only, and neither backend sends mail.

## Running things manually

**In the cloud (github.com → Actions tab → pick a workflow → "Run workflow"):**

| Workflow | What it does |
|---|---|
| `radar` | Full crawl + alert cycle now (also runs on its own every ~30 min) |
| `notion-verify` | Read-only check that the Notion connection works |
| `promote-shortlist-applications` | One-time: move the ~47 boxes you checked under the old semantics into Notion as "Not started" |
| `strategist` | Build the Monday memo now |
| `tests` | Run the test suite |

**On the Mac (from the repo, for development):**

```bash
.venv/bin/python -m pytest tests/          # run the test suite
.venv/bin/python -m radar.main lifecycle  # reconcile stale postings + Notion archives
tail -f ~/.jobradar/logs/enrich.log        # watch the AI companion work
launchctl kickstart -k gui/$(id -u)/com.jobradar.enrich   # force an enrichment cycle now
```

## 5-minute demo

1. **Alerts:** open the [Issues tab](https://github.com/VictorJimenez3/fable-job-search/issues)
   → open this week's "🎯 Job Radar alerts" issue. Read a few lines — note the
   score, salary, and the bolded industry + company description.
2. **Track:** tick one checkbox on a job you might actually want.
3. **Notion:** open your 2026 Applications database → the job is there within
   ~1 minute, status "Not started", with position, link, and location filled.
4. **Feedback:** comment `skip <some company you dislike>` on the issue.
5. **Culture:** comment `culture Anduril` and wait for the bot's reply.
6. **Dashboard:** open [docs/DASHBOARD.md](DASHBOARD.md) for the long tail.
7. **AI (Mac):** `tail -f ~/.jobradar/logs/enrich.log` while forcing a cycle
   (command above) — watch it annotate jobs with the local model.
