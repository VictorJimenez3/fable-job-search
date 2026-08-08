# 🎯 Job Radar

A self-expanding, always-on radar for **new-grad AI / SWE / DS roles**, tuned for
speed (apply within 24h of posting) and personalized ranking (healthtech first,
big tech second, open to everything good).

**[→ 🖥️ The Platform](https://job-radar-vmj-8946s-projects.vercel.app)**
(sign in with GitHub once → every click writes instantly) ·
**[→ 🧪 ChemE internship board](https://job-radar-cheme.vercel.app)**
(independent jobs and pipeline; same Notion Applications database) ·
**[→ Pages mirror](https://victorjimenez3.github.io/fable-job-search/platform/)**
(same app, zero backend — writes via prefilled issues; what forks get for free) ·
**[→ User guide / tutorial](docs/TUTORIAL.md)** ·
**[→ Live dashboard](docs/DASHBOARD.md)** ·
**[→ Culture Compass](docs/CULTURE.md)** ·
**[→ SHPE 2026 plan](docs/SHPE.md)** ·
**[→ Alert issues](../../issues?q=is%3Aissue+label%3Aradar-alerts)** ·
**[→ Roadmap](ROADMAP.md)** ·
**[→ RSS feed](docs/feed.xml)** ·
**[→ Cross-CLI handoff notes](docs/CLI_HANDOFF.md)**

The platform is now decision-first: filter by role family, sponsorship, and
required experience; see honest eligibility facts before opening a posting;
then use one primary apply link with explicit To apply/Applied tracking.
Company-level DOL sponsorship history is also visible as separate context: the
Jobs filter and Fit drawer can show likely historical sponsor, no certified
history in the covered quarters, or unavailable. It never overrides the
posting's own visa wording or adds ranking points. Refresh it locally with
`.venv/bin/python -m radar.main sponsorship-refresh`; the scheduled workflow
does this weekly from the [official DOL OFLC data page](https://www.dol.gov/agencies/eta/foreign-labor/performance).

The ChemE internship board is intentionally separate from this new-grad
AI/SWE/DS board. It reads the `claude/cheme-intern-radar` branch and keeps its
own generated state and GitHub board labels, while both profiles use the one
repository-level `NOTION_TOKEN` and therefore the same Notion Applications
database.

Subscribe to the RSS feed at:
`https://raw.githubusercontent.com/VictorJimenez3/fable-job-search/claude/newgrad-job-search-system-9gbj9k/docs/feed.xml`

The SimplifyJobs feed is retained as active-source coverage for up to a year;
its stale timestamps keep old listings out of alert email, but do not silently
remove them from the in-house board. SWEList uses that same New-Grad-Positions
feed, so it is covered by the same ingestion path.

## How it works

```
every ~30 min (GitHub Actions cron)
│
├─ 1. BREADTH: pull new-grad aggregators
│     [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) · vanshb03 · jobright-ai (SWE + Data) · speedyapply · [Zapply DS/ML](https://github.com/zapplyjobs/New-Grad-Data-Science-Jobs-2027) · HN Who-is-Hiring
│
├─ 2. DISCOVERY: mine every job URL for ATS tokens
│     (Greenhouse / Lever / Ashby / Workday / SmartRecruiters / Recruitee)
│     new tokens → probed live → join the company registry FOREVER
│     registry starts from data/companies_seed.yaml (~85 curated healthtech /
│     AI-lab / big-tech / edtech companies) and grows on its own
│
├─ 3. SPEED: poll every active registry company's ATS API directly
│     → catches postings minutes-to-hours after they go live,
│       days before they show up anywhere else
│     · Fanatics is covered board-by-board (corporate, Betting & Gaming,
│       Commerce, and Collectibles), because it does not use one universal feed
│
├─ 4. RANK: verified new-grad/early-career fit first, then AI/ML and data
│     science, then general SWE, then data engineering and systems; sector, company tier,
│     freshness, and learned taste refine the order. Technical graduate,
│     rotational, and leadership programs (J&J TLDP, Merck IT/emerging-talent,
│     BMS digital programs, etc.) are a dedicated high-priority path. Marquee
│     companies, salary, aggregators, and healthtech no longer bypass the
│     new-grad gate. FIELD FIT still outranks everything: description boilerplate
│     cannot promote unrelated roles; off-field titles (safeguards/policy/sales/PM/...)
│     and generic analysts go dashboard-only; mid-level titles (II/L4) and
│     required 1+ years do too. None become alerts. Rule bumps
│     re-gate the stored jobs automatically (score.RULES_VERSION).
│     Optional configured-LLM re-rank + per-job application angle.
│
└─ 5. DELIVER
      · one silent 🎯 GitHub issue per new alert for tracking/checking boxes
      · 📌 master board issue — every open alert-worthy role in ONE place,
        deliberately unassigned/no extra notification (body + comment pages;
        changed pages refresh each crawl; checkboxes work there too)
      · 🏆 "Best of <date>" issue each evening — the daily top-10, emailed to
        you via GitHub's own notification
      · 📬 alert batches every 4h — normally waits for 3 roles, or 12h for a
        smaller batch; urgent scores can still send immediately; this is the
        only normal alert email surface
      · docs/DASHBOARD.md — everything decent, sorted
      · docs/feed.xml — RSS for instant notifications in any feed reader
```

### Tracking and applied logging

1. **Check a box on an alert issue or the master board to track a job.** It
   appears in the in-house Pipeline immediately with the not-yet-applied status
   and is mirrored to your selected external tracker (Notion by default;
   Google Sheets is available as a per-user Vercel tracker)
   (`stage_saved` in profile.yaml, default "Not started") and improves future
   ranking.
2. **When you apply, the inbox becomes the source of truth.** The email-based
   watcher is currently parked as a future multi-user capability. When that
   capability is enabled, it reads application-lifecycle emails
   and drives the tracker **Stage** for you, end to end — no manual updates:
   - "Thank you for applying…" → promotes the tracked entry to **Applied**
   - online-assessment / coding-challenge invite → **OA**
   - interview / "schedule a call" / "next steps" → **Interview**
   - "unfortunately… other candidates" → **Rejected** (+ Response date)
   - applied with no reply for `autoclose_days` (default 45) → **CLOSED**

   It only ever moves a job *forward*, so a stray late email can't undo a
   later stage. You can still change anything in Notion/Sheets by hand; the
   twice-daily readback now brings those stage edits into the radar.
3. For a job found outside the radar, use **Pipeline → Add a role you found
   yourself** to save its company, title, live link, and optional location to
   the in-house **To apply** lane and Notion. It is explicitly marked manual,
   never creates an alert, and is not mislabeled as new-grad. You can also
   comment `applied <url>` on any issue to log it as Applied immediately.
4. A twice-daily reconcile sweep re-reads every radar issue and tracks any
   checked box the event pipeline missed — a tick is never lost.

The weekly strategy memo now includes an **auto-tracked funnel** (applied → OA
→ interview → rejected → auto-closed) and your response rate, overall and by
sector — built entirely from what the watcher reads, so you can see which
sectors actually reply.

The platform now builds evidence-first company briefs from bounded excerpts of
public company/about, careers, benefits, culture, and monitored-board pages in
addition to postings. Products, customers, mission, business context, technical
work, locations, visa context, candidate relevance, and interview focus carry
source links/dates. Non-public employer-profile values are visibly labeled
**Estimated**, and every posting retains the board or discovery URL that found
it. New postings trigger this research automatically; the manual company-
research backfill is resumable and commits bounded checkpoints, so a long run
can be interrupted without losing completed dossiers. Until the backlog is
empty, a 30-minute relay keeps that worker restarting after short exits or
provider cooldowns; it prioritizes companies attached to saved/tracked,
alert-worthy, high-score, and fresh roles.

Roles with a target technical title and **no stated experience floor**, but no
actual new-grad proof, are labeled **early-career possible** in Jobs and can be
filtered separately. This is an application-research cue (for example, a
non-campus AI Engineer role), never an alert or a substitute for verified
new-grad eligibility.

Each job's **Outreach** tab builds role-aware public Google searches for
university recruiters, technical recruiters, likely hiring managers, NJIT
alumni, and public hiring posts. These are outbound links only: the radar never
scrapes LinkedIn or stores the people/search results it opens.

When signed in as the radar owner, the **Company** tab also offers **research
+ prefetch companies**. It runs a fresh public-source pass plus hosted-model
synthesis for the opened employer, then concurrently warms up to four likely
next companies from the current Jobs ordering. The first result normally takes
**1–3 minutes** including workflow startup; later drawers in the same browsing
session should already be prepared. This control is intentionally unavailable
to visitors and does not expose or spend provider credentials in the browser.

Scoring is published in three places: the live Vercel dashboard reads
`state/jobs.json`, the generated [dashboard](docs/DASHBOARD.md) lists the current
score, and each job's **Why it scored** drawer shows the auditable equation
reasons plus the scoring version. Every crawl now rebuilds all active stored
postings before publishing those files; `python -m radar.main rescore` is the
manual repair command. CI also checks score coverage, and a six-hour scheduled
maintenance workflow repairs the generated snapshot if a writer missed a
version stamp.

Other comment commands: `skip <company>` (downrank similar roles),
`track <ats> <token> [Name]` (force-add a company to the crawl registry).

## Setup (one-time, ~5 min total)

Two independent credentials, each createable only by you. Everything else
works with zero setup.

**1. Notion write access (~2 min)**
1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → *New integration*
   (internal), any name, your workspace. Copy the secret.
2. In Notion, open your Applications database → ⋯ menu → *Connections* →
   add your integration. (The database is found automatically by title
   search — rename or recreate it anytime, nothing else needs to change.)
3. In this repo: *Settings → Secrets and variables → Actions → New repository
   secret*: name `NOTION_TOKEN`, value = the secret.

Verify anytime without creating test data: *Actions → notion-verify → Run workflow*.

**Future multi-user email-based applied-detection (separate from OAuth login)**

This path remains parked because OAuth login and private per-account tracking do
not require inbox access. Victor's current owner workflow does not need
`EMAIL_ADDRESS` or `EMAIL_APP_PASSWORD`.

The ChemE candidate uses NJIT Google Workspace/Gmail, so this uses IMAP with an App Password
(Google's supported way to let a non-browser client log in — this is not
your NJIT password and can be revoked anytime independent of it):
1. On the `ak2943@njit.edu` Google account, enable **2-Step Verification** at
   [myaccount.google.com/security](https://myaccount.google.com/security) if not already on
   (required before Google will issue app passwords).
2. Generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   — name it "Job Radar", copy the 16-character password.
3. Add two repo secrets: `EMAIL_ADDRESS` = `ak2943@njit.edu`, `EMAIL_APP_PASSWORD` = the 16-char password.

This connector reads her inbox for application lifecycle messages; it is not
the outbound alert channel. ChemE GitHub issues mention `@ak2943` for board
notifications without granting repository access.

Verify anytime (read-only, marks nothing as read): *Actions → email-verify → Run workflow*.

*If NJIT's Workspace admin has disabled app passwords* (some university IT
policies do), `email-verify` will fail with a clear "login rejected" message
instead of hanging. Fallback: set up a Gmail filter on the njit.edu account
to auto-forward application-confirmation emails to a personal Gmail you
control, then point `EMAIL_ADDRESS`/`EMAIL_APP_PASSWORD` at that account instead
— same setup, one extra forwarding rule.

**3. Free local AI on your MacBook (~5 min, recommended)**
No API key needed — your M1 Max runs the intelligence layer via Ollama.
One command on the Mac:
```bash
curl -fsSL https://raw.githubusercontent.com/VictorJimenez3/fable-job-search/claude/newgrad-job-search-system-9gbj9k/scripts/mac-companion/install.sh | bash
```
This installs Ollama + `qwen3:30b` (a 19GB mixture-of-experts model that is a
strong fit for your M1 Max with 64GB unified memory; override with
`JOBRADAR_MODEL=<name>` if you want to experiment) and a
launchd agent that, **whenever the laptop is on**, every 2 hours: pulls the
repo, then locally (free, private) generates culture dossiers, re-ranks
recent jobs, runs the weekly company scout, and runs the **quality pass** —
re-checks alert-worthy postings' links (dead → closed everywhere) and has
the LLM verify each is really new-grad and really a technical role
(verified-bad → demoted with the reason logged, marquee included since
DECISIONS #31), grades any job descriptions you pasted into the platform's
Role-fit tab — then pushes the enriched state back. The cloud crawler never depends on the Mac — the Mac
just upgrades whatever it finds when awake. Requires `git push` auth on the
Mac (`brew install gh && gh auth login`). Logs: `~/.jobradar/logs/enrich.log`.
The companion releases the model from memory after every request; confirm with
`ollama ps` (it should show no loaded model between enrichment cycles).

## Owner-only Resume Studio

Resume Studio is the private Victor-first application harness. It reads the
radar's local job snapshot plus the ignored `CV/` directory and writes all
prompts, drafts, PDFs, and review reports under `CV/.resume_studio/`.

Start it from the repository:

```bash
.venv/bin/python scripts/resume_studio.py
```

Open `http://127.0.0.1:4317/`. **Used bullets** selects a target-aware subset
from the master CV and example résumés without rewriting it. **AI tailor** may
substantially rewrite or synthesize bullets from multiple authorized source
lines. **Unrestricted AI tailor** is freer to make an original role-specific
argument across the evidence bank; it still cannot invent facts, remove scope
qualifiers, or bypass layout review. In all modes a deterministic renderer
uses `CV/resume.tex` unchanged;
models cannot author the LaTeX document, alter the margins or typography, or
pass a sparse or bloated portfolio. Employer headings are company-first. Usable installed
first-party Codex and Claude Code sessions provide planning and fixed review;
one may fail independently and the run degrades to the other without API keys.
The local harness removes API-key environment variables so this owner workflow
does not silently spend API credits. Nothing from `CV/` is sent to GitHub
Actions or committed to the public repository.

The role list can be sorted by **Best Radar score**, **Newest**, or the private
**Resume Match** rubric. Match analysis reports requirement coverage, evidence
strength, domain relevance, eligibility, distinctiveness, confidence, gaps,
and source IDs; selected roles can be rechecked against the full posting.
Generation asks for a full ranked evidence portfolio, then keeps 22–26 distinct
bullets across three experiences, four projects, and one or two leadership
entries. Deterministic backups are used only when a draft falls below that
acceptance floor; they do not reintroduce evidence omitted from a complete
plan. Actual PDF line widths hard-fail wrapping, and bottom density is a hard
comparison against the immutable human-authored reference. Unsupported inline
LaTeX and lost scope qualifiers such as
`synthetic`, `prototype`, or `POC` are repaired or rejected before packing. The
adversarial pass must return an applied corrected plan rather than a complaint
about an already-rendered draft. The reported Resume Craft score is separate
from hard factuality, eligibility, and layout gates.

Enhancement prompts also receive the CV authority dossier and an exact-term ATS
strategy from the captured posting. They may swap projects and rewrite bullets
around supported posting language, but unsupported requirements remain visible
gaps rather than invented claims. Completed reports show rewritten lines,
project swaps, rendered keyword coverage, and the supported terms that remain
missing versus terms the evidence does not support. Space QA distinguishes
roomy lines from near-wraps; a bullet with less than 12pt of right-edge safety
is treated as a near-wrap and rejected, even if the PDF extractor technically
reports one line.

Use **Resume bank** in the Studio header to browse every saved run and legacy
experiment. Each new run keeps a private snapshot of the selected posting,
remains visible after switching to another role, and names its PDF with the
company (for example, `mayo_clinic_resume_ai.pdf`). Preview responses also send
that filename to the browser/Preview app, so downloads do not fall back to
`resume.pdf`. Project metadata uses `|` as its compact separator. TICC is
permanently excluded from every generated or workshop-edited resume, while
the local source files remain untouched. Open a saved posting snapshot from
its card when the source text was captured. Failed and in-progress runs remain
listed so an interrupted attempt is inspectable rather than silently
disappearing.

The three tailoring buttons queue independent runs, so switching postings or
starting another mode does not replace the current draft. The header shows
observed Codex tokens/calls for the current UTC week and the bank shows queued,
running, interrupted, and completed runs. Codex Plus's weekly allowance is not
available through the local CLI; set `CODEX_WEEKLY_LIMIT_TOKENS` only if you
want the UI to calculate a percentage against a known personal limit.

Open **Workshop** on a completed run to edit education, skills, experience,
projects, and leadership lines without touching the original PDF. Saving a
line creates a unique rendered revision; the AI writing partner returns
source-grounded candidates for approval, and revision history can revert to an
earlier draft. The current Mac exposes Codex CLI and Claude Code lanes; a Luna
lane is only available when a local `luna` executable is installed.

Reviewer output is normalized before rendering: repeated selections for one
source entry are merged so distinct evidence is not silently lost, and
source-backed ATS wording receives the normal line-compression pass before any
safe fallback. Workshop metadata is refreshed from the canonical template on
load while preserving line edits, so older private drafts cannot misplace or
duplicate education and skills rows after a renderer update.

For a disposable human-feedback batch across varied roles, use the calibration
lab:

```bash
.venv/bin/python scripts/resume_calibration.py --generate --count 8 --serve
```

It selects distinct data, SWE, AI/ML, specialized-AI, and cloud roles from the
local job snapshot, writes each run under `CV/.resume_studio/calibration/`, and
opens a small review page at `http://127.0.0.1:4321/`. The review surface shows
the rendered PDF and saves good/revise/bad labels plus notes to that ignored
batch folder. It is a temporary calibration tool, not another application
workflow.

Optional upgrades:
- The four NVIDIA NIM keys are wired into a task-aware, budgeted cloud router
  that races all configured providers concurrently; the first schema-valid
  response wins while every attempt logs latency, validity, and errors. Main
  and ChemE enrichment runs every two hours and is deliberately not exposed to
  every 30-minute crawl. Configuration and operating policy:
  [docs/AI_SETUP.md](docs/AI_SETUP.md).
- Run **Actions → AI provider benchmark** to test all four providers against
  the real company-research and posting-quality schemas. Measured winners and
  latency are stored in `state/ai_benchmark.json`; current production results
  favor GLM for both tasks.
- Run **Actions → company research backfill** to drain older employer dossiers.
  It prioritizes high-score visible companies, uses bounded API calls, and
  checkpoints research/telemetry safely when production changes concurrently.
  Provider/schema failures are marked retryable with exponential backoff, and
  each checkpoint reports ready, pending, retry-waiting, and error counts.
- Prefer Google Workspace to Notion? The Google Sheets tracker adapter is
  complete. Run `python -m radar.main create-google-tracker` once only for the
  owner metadata/automation workbook. On the Vercel platform, Google sign-in
  or **Connect Google + create my Sheet** requests Sheets permission and creates
  a separate Applications workbook in that user's Google Drive. The backend
  stores only encrypted token metadata in the private Accounts registry and
  never exposes another user's rows or token.
  Setup is documented in [docs/GOOGLE_SHEETS_SETUP.md](docs/GOOGLE_SHEETS_SETUP.md).
- `GOOGLE_CSE_KEY` + `GOOGLE_CSE_ID` (free: [programmablesearchengine.google.com](https://programmablesearchengine.google.com)
  → create engine searching `linkedin.com/posts`, then get an API key at
  [developers.google.com/custom-search](https://developers.google.com/custom-search/v1/introduction))
  → Monday memos gain a "Heard on LinkedIn" section of public hiring posts.
- **Watch the repo** (Watch → All activity) and install the GitHub mobile app
  for instant pushes; issues are also assigned to you, which notifies by default.
- Subscribe to `docs/feed.xml` in a feed reader (raw URL above) for sub-minute alerts.

## Culture Compass

Every alert is checked against your stated criteria (prestige + pay + fast
culture + real WLB/shutdowns + mission, no burnout): companies get a 0–100
**fit score** (deterministic rubric, burnout-penalized) shown as `fit NN` on
alert lines and tabulated in
[docs/CULTURE.md](docs/CULTURE.md) — prestige tier, pace, WLB, PTO,
shutdowns, new-grad TC, rotational programs. ~40 dossiers are human-curated
and may affect ranking (±6); older model-memory dossiers are labeled `est.` and
are display-only because they have no evidence. Ask about any
company by commenting `culture <company>` on an alert issue.

## Tuning

Everything subjective lives in [`profile.yaml`](profile.yaml): sector weights,
role weights, alert threshold, freshness bonuses, location policy, the
narrative Claude uses. Edit and push — next run picks it up. Seed companies:
[`data/companies_seed.yaml`](data/companies_seed.yaml).

## Operating notes

- **State** (`state/*.json`) is committed back by CI after each run: seen jobs,
  the company registry, learned taste, applied log, run stats, company research,
  AI usage, and provider benchmark results.
- **Score maintenance** runs every six hours and verifies that every stored job
  carries the current score/rules version before publishing a repaired snapshot.
- GitHub cron is best-effort: `*/30` in practice fires every 30–60 min.
- Scheduled workflows pause after 60 days without repo activity; any commit
  (including CI's own) resets the clock, so this is only relevant if the radar
  is disabled. GitHub also emails you before pausing.
- A source failing (site down, format change) never kills a run — it's logged
  in `state/runs.json` and skipped. Companies failing 5 consecutive polls are
  marked dead and stop being polled.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest tests/            # unit tests (real captured fixtures)
python -m radar.main seed          # initialize registry + taste priors
python -m radar.main crawl         # full cycle (respects RADAR_* env vars)
```

Useful env vars: `RADAR_DISABLE_SOURCES=ats,hn`, `RADAR_PROBE_BUDGET`,
`RADAR_MAX_COMPANIES`, `RADAR_MAX_ALERTS`, `RADAR_WORKERS`.

Other one-off CLI commands: `notion-verify`, `email-verify` (connectivity checks,
create nothing), `email-watch` (run one detection cycle manually).
