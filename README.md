# 🎯 Job Radar

A self-expanding, always-on radar for **new-grad AI / SWE / DS roles** plus a
separate technical **internship lane**, with a low-priority PM-family research
lane inside new-grad. New-grad remains the default and highest-priority
compute path; internships have their own sources, state, cadence, tracker tab,
and notification setting.

**[→ 🖥️ The Platform](https://job-radar-newgrad.vercel.app)**
(easy public shortcut; existing Vercel URL remains active and sign-in stays in sync) ·
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

The friendly Vercel URL is the canonical production door. Production deploys
run from `claude/newgrad-job-search-system-9gbj9k` and promote the newest build
to `job-radar-newgrad.vercel.app`; users should never need a deployment-specific
URL.

The staged vNext workspace is available at
[`/vnext/`](https://job-radar-newgrad.vercel.app/vnext/). It loads a cursor-
paginated action queue instead of downloading the full repository datastore,
virtualizes long lists, validates every API response, and has responsive Jobs,
Applications, and Companies routes. Until Postgres cutover, public Jobs use a
server-side legacy fallback and private workflows link back to the classic UI;
no current capability is removed. vNext displays an objective evidence score,
eligibility, and Victor's goal/recommended priority as separate signals instead
of hiding personal priority inside one number.

The platform is now decision-first: filter by role family, sponsorship,
required experience, and minimum degree; see honest eligibility facts before opening a posting;
then use one primary apply link with explicit To apply/To tailor/Applied tracking. A
required master's or PhD is shown directly on the role, receives a substantial
auditable score dock, and becomes dashboard-only rather than alertable; strong
matches remain visible at the dashboard floor for human review in case the
posting is mistaken. In the
Jobs list, click a row once to save it (green); click the saved row again to
exclude it from active Jobs (red). Exclusions are view-only and reversible, so
they do not change the score, crawler, tracker history, or notifications.
The site boots progressively: the shell and Jobs milestone render before
optional research, tracker, and culture panels hydrate, so one stale state file
does not blank the whole page. If a load fails, only the signed-in
`VictorJimenez3` owner view receives a compact in-app developer notice with a
retry action; it never sends an email or creates an issue automatically.
The optional Google Sheets mirror is isolated from that boot path: if its OAuth
grant or workbook read is unavailable, the backend keeps the dashboard usable
and presents the tracker as disconnected until Google is reauthorized. Tracker
writes and unexpected backend errors still remain visible.
In Jobs, role-field buttons stay visible and cycle neutral → selected → red
excluded; click the red state again to clear that exclusion.
Company-level DOL sponsorship history is also visible as separate context: the
Jobs filter and Fit drawer can show likely historical sponsor, no certified
history in the covered quarters, or unavailable. It never overrides the
posting's own visa wording or adds ranking points. Refresh it locally with
`.venv/bin/python -m radar.main sponsorship-refresh`; the scheduled workflow
does this weekly from the [official DOL OFLC data page](https://www.dol.gov/agencies/eta/foreign-labor/performance).

The Jobs sort menu also includes a Best Match lookback: all time, the last hour,
6 hours, 24 hours, 3 or 7 days, 2 weeks, or 1, 3, 6, or 12 months.
For the new-grad lane, the default Jobs view is a **Fresh action queue**: it
starts with entry-compatible or unclear experience and postings from the last
month. Tracked or Maybe roles remain available even when older or experienced;
choose an explicit experience or lookback filter when researching the full
board. Definitively expired or filled postings leave active Jobs and stay in
History with their evidence and close reason.

The ChemE internship board is intentionally separate from this new-grad
AI/SWE/DS board. It reads the `claude/cheme-intern-radar` branch and keeps its
own generated state and GitHub board labels, while both profiles use the one
repository-level `NOTION_TOKEN` and therefore the same Notion Applications
database.

The main platform's **New-grad / Internships** switch is a second, isolated
technical lane for friends. Internship postings come from curated public
GitHub boards (Simplify, SpeedyApply, Zapply, and Dreamwork) plus internship
searches on the existing ATS registry. A viewer can set an expected graduation
month in Settings; the lane derives freshman/sophomore/junior/senior fit from
the posting's internship start term and keeps unclear eligibility visible as
unknown instead of silently rejecting it. The default internship list uses
positive title or posting-body evidence (intern, co-op, seasonal terms, or
student/graduation language); uncertain source-only rows remain behind a
review toggle. New-grad and internship Jobs,
Pipeline, web state, alert history, and GitHub surfaces never share a list.

Internships use a separate neutral, friend-facing 0–100 score. It starts
technical role families evenly and compares public opportunity evidence:
normalized pay,
recognized or cited employer signal, mentorship and structured learning,
hands-on ownership, technical depth, production/user impact, return-offer
path, student evidence, and freshness. It does not use Victor's saved roles,
sectors, remote preference, feedback, applied history, or new-grad weights;
unknown employers and missing pay/work evidence receive zero for that signal,
not a penalty. The drawer shows the dimensions and exact reasons behind each
score.

Prestige is an explicit general-opportunity dimension in this lane: Google,
NVIDIA, Microsoft, OpenAI, Anthropic, and comparable big-tech or AI-lab
employers intentionally occupy the top end of the friend-facing chart. This
is a broad "crackedness" signal, not Victor's personal company preference.

The lane is conservative about employment type. Explicit full-time-only
wording with no internship, student, or graduation evidence is marked
**full-time wording · review only**, cannot alert, and is capped below the
normal internship board threshold. Senior/staff-style titles and rows without
positive internship evidence stay available through the review toggle because
missing an oddly labeled internship is worse than reviewing an outlier.

Internship email batches are **off by default**. The owner can opt into them
from Settings; the same preference controls new-grad batches (new-grad starts
enabled). This is GitHub notification delivery, not inbox access: Google OAuth
does not request Gmail scope and the platform never reads internship emails.

The shortcut and the original Vercel URL are two doors to the same platform.
OAuth still uses the original callback host for provider compatibility, then a
short-lived encrypted handoff gives the other door its own secure session
cookie. The handoff travels in a redirect fragment, is cleared immediately,
and does not depend on cross-site cookie settings. If a browser was already signed in through the old URL, opening the
shortcut now carries that session over and opens **Account center** instead of
showing an “already signed in” error. Signing out clears both doors.

Subscribe to the RSS feed at:
`https://raw.githubusercontent.com/VictorJimenez3/fable-job-search/claude/newgrad-job-search-system-9gbj9k/docs/feed.xml`

The SimplifyJobs feed is retained as active-source coverage for up to a year;
its stale timestamps keep old listings out of alert email, but do not silently
remove them from the in-house board. SWEList uses that same New-Grad-Positions
feed, so it is covered by the same ingestion path.

The PM-family lane uses the existing SimplifyJobs New-Grad-Positions PM section,
Jobright's dedicated [Product Management new-grad board](https://github.com/jobright-ai/2026-Product-Management-New-Grad),
the public [Zapply New-Grad Jobs 2027 board](https://github.com/zapplyjobs/New-Grad-Jobs-2027),
and direct title searches across the same Workday/Phenom and bespoke big-company
career APIs used by the technical radar. PM-originated official ATS links are
prioritized for company backfill; the expensive full PM synonym fan-out is
bounded to 200 Workday/Phenom companies by default
(`RADAR_PM_BACKFILL_COMPANIES`), so the lane can grow into direct company
coverage without starving the normal crawl.
Product manager, technical product manager, product owner, project manager,
business analyst, UX/UI researcher, and solutions architecture titles are
dashboard-visible with role weight `0`, never enter alert eligibility, and never
enter alert email/RSS delivery.

## How it works

```
every ~30 min (GitHub Actions cron)
│
├─ 1. BREADTH: pull new-grad aggregators
│     [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) · vanshb03 · jobright-ai (SWE + Data + [PM](https://github.com/jobright-ai/2026-Product-Management-New-Grad)) · speedyapply · [Zapply DS/ML](https://github.com/zapplyjobs/New-Grad-Data-Science-Jobs-2027) · [Zapply PM breadth](https://github.com/zapplyjobs/New-Grad-Jobs-2027) · HN Who-is-Hiring
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
│     freshness, and learned job preferences refine the order. Technical graduate,
│     rotational, and leadership programs (J&J TLDP, Merck IT/emerging-talent,
│     BMS digital programs, etc.) are a dedicated high-priority path. Marquee
│     companies, salary, aggregators, and healthtech no longer bypass the
│     new-grad gate. FIELD FIT still outranks everything: description boilerplate
│     cannot promote unrelated roles; off-field titles and PM-family roles go
│     dashboard-only; generic analysts, mid-level titles (II/L4), and
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

The internship lane runs independently every two hours on its own
`internship-radar.yml` workflow, with a smaller deterministic ATS budget so it
cannot starve the new-grad crawl. It writes `state/intern_*.json` and
`docs/internships/`; its alert issues, master board, checkbox reconcile, and
opt-in email batch use internship-specific labels. The normal new-grad crawl
and its ranking remain the priority whenever compute is constrained.

### Tracking and applied logging

1. **Choose a lane, then check a box on its alert issue or master board to track
   a job.** It
   appears in the in-house Pipeline immediately with the not-yet-applied status
   and is mirrored to your selected external tracker. The internship lane is
   never mixed into the new-grad Jobs or Pipeline view. For Victor's
   `VictorJimenez3` account, Notion is the default primary tracker; Google
   Sheets is an optional personal mirror under the expanded Tracker options.
   Other Vercel users can use their own Google-backed tracker without touching
   the repository owner's Notion pipeline.
   (`stage_saved` in profile.yaml, default "Not started") and improves future
   ranking.
   The crawler and tracker use the canonical posting URL as the strongest
   duplicate boundary, then run a conservative posting-family repair for
   providers that mint a different URL per location. A family needs the same
   employer and work-oriented title core, a hiring-program marker when titles
   differ, and either a compatible official/aggregator location match or the
   same aggregator board plus the same posting day. One surviving row carries
   all known locations, alternate provider links, and an audit reason; generic
   same-title roles with ambiguous direct postings stay separate. Existing
   duplicates are merged on the next crawl or `resolve-links` repair; owner
   notes and tracker history are migrated, and duplicate Notion pages are
   soft-archived rather than deleted.
   Jobright-style aggregator pages are handled conservatively: each crawl
   follows a bounded number of new/high-value aggregator links, promotes an
   explicit employer/ATS application URL when one is verifiable, and keeps the
   aggregator URL as a labeled fallback when it is not. Exact employer/title
   matches with one compatible direct ATS row are merged; ambiguous same-title
   roles remain separate so coverage is not lost. Alerts and the generated
   dashboard list the discovery source(s) and fallback links.
   A separate pre-crawl repair job checks up to 800 still-open aggregator rows
   in parallel (16 bounded workers) and publishes closed-page verdicts before
   the expensive discovery crawl starts. This keeps stale Jobright pages
   moving into History even when a full crawl reaches its execution limit.
2. **When you apply, the inbox becomes the source of truth.** When the optional
   email secrets are configured, the watcher reads application-lifecycle
   emails every 30 minutes and drives the tracker **Stage** for you:
   - "Thank you for applying…" → promotes the tracked entry to **Applied**
   - online-assessment / coding-challenge invite → **OA**
   - interview / "schedule a call" / "next steps" → **Interview**
   - rejection / "another direction" / "not selected" → **Rejected** (+ Response date)
   - applied with no reply for autoclose_days (default 45) → **CLOSED**

   Gmail can use its read-only incremental History API (`EMAIL_BACKEND=gmail_api`),
   which advances a cursor only after a successful pass. Other inboxes and
   existing installations keep the read-only IMAP connector. Both paths use a
   bounded 21-day recovery search, match employer plus role title, and only
   advance a stage. A late email cannot undo a later stage; ambiguous matches
   are recorded in the email review queue instead of silently changing the
   wrong application. A separate tracker-sync runs every 15 minutes, so
   Notion/Sheets stage edits and local changes converge without waiting for an
   issue event. The twice-daily checkbox sweep remains as a second safety net.

   Internship notification batches are disabled unless the owner explicitly
   enables **internship batches** in Settings. New-grad batches have their own
   toggle and default to enabled. Neither toggle grants Gmail access or
   changes the posting crawler.
3. For a job found outside the radar, use **Pipeline → Add a role you found
   yourself** to save its company, title, live link, and optional location to
   the in-house **To apply** lane and Notion. From there, move it to **To
   tailor** when a Resume Studio draft is queued, then to **Applied** only
   after you actually submit. It is explicitly marked manual, never creates
   an alert, and is not mislabeled as new-grad. You can also
   comment `applied <url>` on any issue to log it as Applied immediately.
4. A twice-daily reconcile sweep re-reads every radar issue and tracks any
   checked box the event pipeline missed — a tick is never lost.

### My job preferences, similar roles, and posting review

The owner-only **My job preferences** tab uses saved and applied roles as a
transparent positive sample. The Radar now learns a bounded preference lift
from that sample: repeated employers, role-family mix, recognized sector mix,
and recurring meaningful title language. Each lift is included in the
`personal_signal` dimension and written into the job's exact reason ledger;
hard eligibility gates and configured score overrides still win. The tab
explains the learned contributions and offers **Similar jobs to inspect** as a
discovery aid.

When a score feels wrong, the role drawer has fixed-category **more like this**
and **less like this** feedback. Owner feedback is idempotent, capped, and
stored in `state/feedback.json`; the generated `docs/FEEDBACK.md` makes the
learned company/title signals and recent events auditable. Eligibility and
location feedback is logged without overriding deterministic eligibility gates.
The repository is public, so this is an audit trail rather than a private
journal. No email is sent by this feature.

The owner-only **Settings → Radar score controls** panel can switch optional
score sections on or off for the whole board. Baseline and early-career
eligibility remain on; role fit, sector/mission, company quality,
compensation, personal signals, and timing/access are reversible owner
preferences stored in `state/score_preferences.json`. Every disabled section
is named in the score ledger and the board is fully rescored after saving.

AI/ML remains the strongest configured role lane; a larger volume of generic
SWE postings does not teach the sample that AI is unwanted. Company pace is
also kept separate from preference: new research uses a cited 1–5 operating
pace measure with at least two observable indicators. Unsupported pace remains
**Not confirmed** and contributes zero rather than following startup,
corporate, or candidate-supplied labels.

The owner can **archive** an expired, filled, duplicate, or wrong posting from
the role drawer after GitHub sign-in. Archive is a soft, recoverable status that
survives future crawls and keeps the historical record. Other GitHub users get
a **Report this posting** control that opens a structured issue; the workflow
uses the issue author's GitHub login, counts each person once, and adds the
posting to the owner's review queue after three distinct reporters. Reports
never auto-delete or auto-archive a role. The generated queue is
`docs/REPORTS.md`.

### Automatic posting lifecycle and history

The deterministic crawler now marks a posting **filled** when its dead page
explicitly says the role was filled; other definitive dead links and safely
expired source gaps become **expired**. Terminal postings leave active Jobs,
dashboard, RSS, master-board, and alert delivery, but are retained in the
platform's **History** tab with `closed_at`, `last_seen_at`, lifecycle events,
and the exact reason ledger. Application-history cards also show how long the
posting was up in days, months, or years instead of a lifecycle date. State
retention is two years by default
(`RADAR_HISTORY_DAYS`, minimum one year), so the record can support future
seasonal posting-timeline analysis. `RADAR_LIFECYCLE_ACTIVE_DAYS` defaults to
45 and `RADAR_LIFECYCLE_UNSEEN_GRACE_DAYS` to 14; a transient fetch failure
never closes a role. Source-gap expiry requires a successful run of that
posting's exact aggregator or company ATS board, so one healthy Greenhouse
tenant cannot expire another company's jobs.

For Victor, tracked terminal roles are soft-archived to the personal Notion
Applications database (Notion trash remains recoverable), while the local
Pipeline/history record stays. Other signed-in users never touch Victor's
Notion: their private Google Sheet gets a separate **Posting Status** column
(`open`, `expired`, or `filled`) and the platform shows an in-app tracker
notice. Application **Stage** is preserved, so an expired posting does not
erase an OA/interview record. `python -m radar.main lifecycle` runs the same
reconciliation and Notion-archive repair without crawling sources.

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
reasons plus a short plain-English why for every score section and the scoring
version; expand **Exact reason ledger** for the rule strings. Every crawl now rebuilds all active stored
postings before publishing those files; `python -m radar.main rescore` is the
manual repair command. CI also checks score coverage, and a six-hour scheduled
maintenance workflow repairs the generated snapshot if a writer missed a
version stamp.

The score scale intentionally has room between the extremes: 100 is reserved
for configured favorites or a genuinely exceptional raw match, while the
60s–90s distinguish plausible, strong, and standout roles. Level-II/L4-style
postings remain visible for research but receive a locked score demotion and
never become alerts. Exact same-company/title variants (often the same role in
different locations) normally tie with the strongest variant; a posting-level
quality, experience, or lifecycle verdict still lowers that specific row.
Non-identical near-sibling roles receive only a small, reasoned deduction when
a stronger sibling exists.

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

**Optional email lifecycle automation (separate from OAuth login)**

This is an owner-only GitHub Actions connector. To enable automatic Applied,
OA, Interview, Rejected, and CLOSED updates, choose either the Gmail API or
IMAP. Neither path sends mail.

For Gmail, the preferred path is the read-only History API:

1. Create/choose a Google OAuth web client and authorize only
   `https://www.googleapis.com/auth/gmail.readonly` (Google's OAuth Playground
   can create the one-time refresh grant when **Use your own OAuth credentials**
   is enabled).
2. Add secrets `GMAIL_REFRESH_TOKEN`, `GOOGLE_AUTH_CLIENT_ID`, and
   `GOOGLE_AUTH_CLIENT_SECRET`.
3. Add repository variable `EMAIL_BACKEND=gmail_api` and run
   **Actions → email-verify**. The cursor is stored in generated
   `state/email_watch_api.json` only after a complete pass.

For another IMAP provider, or an existing App Password installation, add
`EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD`. The password must be an IMAP/App
Password, never the normal account password.

For an NJIT Google Workspace/Gmail inbox, use IMAP with an App Password
(Google's supported way to let a non-browser client log in — this is not
the normal account password and can be revoked independently):
1. On the Google account that owns the inbox, enable **2-Step Verification** at
   [myaccount.google.com/security](https://myaccount.google.com/security) if not already on
   (required before Google will issue app passwords).
2. Generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   — name it "Job Radar", copy the 16-character password.
3. Add two repo secrets: `EMAIL_ADDRESS` = the inbox address,
   `EMAIL_APP_PASSWORD` = the 16-character app password.

This connector reads that owner inbox for application lifecycle messages; it is
not the outbound alert channel.

Verify anytime (read-only, marks nothing as read): *Actions → email-verify → Run workflow*.

If an automated workflow fails, it retries failed jobs once. A second failure
opens or updates one repair issue with the exact run link and the instruction
**Tell Codex: fix workflow run <link>**. This prevents repeated silent failures
while avoiding an infinite retry loop.

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
re-checks alert-worthy postings' links (dead → expired/filled in active views,
with the record retained in history) and has
the LLM verify each is really new-grad and really a technical role
(verified-bad → demoted with the reason logged, marquee included since
DECISIONS #31), grades any job descriptions you pasted into the platform's
Role-fit tab — then pushes the enriched state back. The cloud crawler never depends on the Mac — the Mac
just upgrades whatever it finds when awake. Requires `git push` auth on the
Mac (`brew install gh && gh auth login`). Logs: `~/.jobradar/logs/enrich.log`.
The companion releases the model from memory after every request; confirm with
`ollama ps` (it should show no loaded model between enrichment cycles).

## Owner-only Application Autopilot

The owner-only **Autopilot** tab and the companion Chrome extension turn the
Radar's official apply link into a supervised, reusable form workflow. The
extension covers Workday, Greenhouse, Lever, Ashby, and SmartRecruiters first,
with a generic DOM fallback for other ATS pages. It extracts visible form
structure only, asks the local deterministic agent for decisions, fills
approved repetitive fields, and advances ordinary multi-page **Next** steps.

It pauses instead of guessing when a required answer is missing, an essay or
cover-letter question is new, a sensitive field has no approved match, a resume
file must be selected, or the page no longer matches the approved fingerprint.
Owner-approved choice answers can be reused for radio, checkbox, select, and
ATS button controls; optional demographic fields do not interrupt the queue
when no approved answer exists. Every proposed value is shown again in the
final review card. **Submit is never clicked until Victor confirms that card.**
A confirmation from the phone is
single-use, expires after 15 minutes, and is rejected if the Mac is no longer
on the same page.

Before the extension creates an application session, it checks the local
Resume Studio library for a safe tailored PDF for that exact posting. If none
exists, it starts the existing AI tailor run and waits for its terminal result.
If the run does not publish a safe tailored winner, it records that decision
and uses the immutable canonical resume as the controlled fallback. Chrome
still pauses at the resume-file picker because a normal extension cannot
silently select a local file; the rest of the visible repetitive form can
continue automatically.

The workflow supports both one-role launch from a Jobs card and sequential
batch queueing from the phone. Blocked roles park in the private queue while
other roles continue. Answers and field mappings are durable context: the
owner can add them in the Autopilot tab, from the extension popup, or while
answering a blocker. Local JSON remains the machine source of truth under
`CV/.resume_studio/application_agent.json`. The owner-only cloud control plane
currently uses the connected user's private Google Sheet's **Application Agent**
tab for queue/context state; production is intentionally using Victor's
editable `vmj@njit.edu` workbook rather than the quota-locked legacy owner
mirror. The legacy app-created Drive JSON/Markdown path remains a compatibility
fallback, while the staged Postgres adapter is dormant until `DATABASE_URL` is
configured. Resume Studio Markdown and generated files remain local unless the
owner explicitly syncs the Resume Bank. The app UI is authoritative; Sheet,
database, and Markdown payloads are storage/export mirrors, not arbitrary edit
interfaces.

On first use, the local agent seeds only deterministic profile fields already
present in the canonical local resume—name, contact details, school, and public
profile links—and the paired extension mirrors those answers into the private
Sheet. Victor can also keep explicit owner-approved choices in that private
bank, so repeat questions such as work authorization, sponsorship, relocation,
and demographic controls, work schedule, and LLM experience are filled
without repeated manual entry. Exact owner-approved written answers can also
be banked for recurring prompts; employer-specific responses stay tied to that
question and are not reused as generic claims. A
preferred choice can have an explicit fallback that is used only when the
preferred option is absent from a question group. This removes repetitive
blank-field work without inventing claims. LaTeX comments and template
credits are excluded from canonical extraction, and a stale canonical-derived
value is repaired locally and in the private Sheet. Only genuinely unknown
required fields, new essays without an approved answer, resume-file selection,
and final submission stop for Victor. Fields with generic placeholders, such as
a location combobox or date textbox, are matched using the nearest employer
question label and approved variants.
If Chrome or the extension restarts, opening/filling queue items reattach to a
matching application tab or return to the queue instead of being left falsely
active; an item already at Submit is never requeued automatically. Blocked and
review-ready items also reattach to their most recently used exact-match tab
without opening duplicates or preventing later queued roles from continuing.
Queue tab startup is verified as well: the extension explicitly navigates a
new tab,
waits for a real web URL, repairs tracked blank tabs, and requeues a role when
Chrome cannot open it. While Resume Studio is preparing a role, the employer
page shows a live “Agent preparing this role” banner so a long tailoring wait
is distinguishable from a broken or unpaired tab. Roles already marked expired
or filled cannot be newly queued; a legacy queue item that opens an explicit
“job not found,” “job has closed,” or equivalent closed page fails visibly
before any form field is filled. Workday's “page you are looking for doesn't
exist” tombstone is handled the same way.
The unavailable-page check runs again on later DOM mutations, so an ATS that
renders its tombstone after initial page load still stops before a form plan.
Expired/filled saved roles are omitted from Autopilot's candidate picker; they
remain visible in History but cannot be selected into a new application batch.
The availability check runs before Resume Studio work begins. Queue controls
show an in-progress state to prevent duplicate clicks, and opening/filling rows
have a Stop control that also detaches the paired browser executor.
The worker recreates its one-minute alarm and performs an immediate queue sync
whenever Chrome or the extension starts, so queued work does not depend on
opening the popup after a restart.
On Job Radar itself, the content script only relays explicit start commands; it
does not scan the dashboard DOM as though it were an employer application.
Employer-page scans and session creation are serialized per tab. A long Resume
Studio build therefore produces one application session even when the ATS emits
many DOM mutations while the build is running.
If that queue item's completed tailoring run selects the base resume, recovery
uses the immutable canonical resume instead of launching another identical
tailoring run after a worker restart.
Startup recovery also marks any already-queued duplicate as superseded when a
terminal run exists for the same application queue item.

To connect the Mac once:

1. Sign in as `VictorJimenez3`, open **Autopilot**, choose **pair Mac**, and
   copy the one-time token.
2. In Chrome, load the unpacked `browser-extension/` directory at
   `chrome://extensions` with Developer mode enabled, open the extension
   popup, and paste the token.
3. Start the existing private service with
   `.venv/bin/python scripts/resume_studio.py` or install
   `scripts/resume-studio-service/install.sh`. Click **Apply with Agent** from
   a saved role, or queue a batch from Autopilot.

The pairing token grants only the private application-agent control-plane
records and can be replaced/revoked. Raw CV files, provider sessions, browser cookies,
DOM dumps, and passwords never sync to Drive. The issue ledger records page,
provider, field, and fingerprint observations without pretending that the
system repaired itself; ask Codex to repair a repeated issue so the fix can
include a fixture and adapter test.

## Owner-only Resume Studio

Resume Studio is the private Victor-first application harness. It reads the
radar's local job snapshot plus the ignored `CV/` directory and writes all
prompts, drafts, PDFs, and review reports under `CV/.resume_studio/`.

The production platform now exposes the same workflow as one owner-only
**Resume Studio** workspace. From any Jobs row or role drawer, choose
**tailor** to open the posting in that workspace; the cloud page keeps the
posting selection, private-engine connection, queue, run status, and resume
bank together. It calls the Mac engine over a loopback-only bridge when the
Mac is awake, and falls back to Radar/title matching plus posting and apply
links when it is not. The bridge accepts browser requests only from the two
production Vercel doors and the Pages mirror. The source CV, evidence graph,
provider sessions, and generation/workshop execution remain under the ignored
local `CV/.resume_studio/` boundary. When Victor chooses **sync local bank**,
the owner-only cloud API copies bank metadata plus generated PDFs, previews,
reports, and posting snapshots into an app-created private Google Drive folder;
the cloud UI never publishes a public artifact URL or pretends an offline run
is complete. The bank groups every version under one posting card. Its default
scope begins with the first post-overhaul Google run on 2026-08-08; **all
history** keeps older experiments available, and sync follows the selected
scope.

The workspace has two safe operating modes. If the Mac engine is awake, a
tailoring request runs immediately through the loopback bridge. If it is
asleep, single tailoring and **Tailor today** batches can still be saved to
the owner-only private cloud queue; the next open production Studio tab with
the Mac companion connected dispatches up to two items and mirrors their
status. The queue carries only public posting metadata and the requested mode,
never CV text, provider credentials, evidence, or generated artifacts. This
keeps the local execution path fully available while giving the cloud
workspace durable work selection instead of pretending a hosted function can
render the private resume.

The cloud workspace also exposes **Context & Q&A** while the private engine is
awake. Exact unsupported terms found in postings become one durable question
per capability, not one question per run. Victor can document where/when and
how he used the capability, state that he has not used it, or leave it open.
Only a concrete **I used this** answer becomes claim-authorizing evidence; a
negative answer is remembered and never becomes resume text. The context view
lists every usable fact with its source, source kind, authority, and owner
confirmation. Answers and source-level context remain local under the ignored
`CV/.resume_studio/evidence_review.json` boundary and are never included in
cloud bank sync. For an open gap, Studio also proposes specific roles,
projects, courses, or repositories whose neighboring evidence makes them worth
checking. These are labeled investigation hints, not facts. Victor can add a
custom place or GitHub URL, use its tailored question as an answer prompt, and
still must confirm his personal work before it becomes evidence. **Not in this
place** suppresses one bad lead without falsely closing the capability gap.

The canonical `CV/immutable/VictorJimenezResume.tex`,
`CV/immutable/VictorJimenezResume.pdf`, and the historical
`CV/immutable/og_resume.*` and `CV/immutable/tldp_resume.*` pairs are locked
from Studio writes. Editing any protected artifact requires the owner PIN
through the local lock command. Every generated draft and
every Workshop revision is rendered in its own private directory, so selecting
another posting or editing a draft cannot overwrite a canonical resume. The
Studio header exposes this protection state.

Start it from the repository:

```bash
.venv/bin/python scripts/resume_studio.py
```

For the production button to work without starting it manually after every
login, install the owner Mac service once:

```bash
bash scripts/resume-studio-service/install.sh
```

The service defaults to two concurrent full tailoring runs and persists each
job snapshot under `CV/.resume_studio/runs/`. If the Mac service is restarted,
queued work and runs abandoned mid-step are requeued automatically; the bank
keeps the truthful `queued`/`running` state and exposes a separate attention
warning when a status has gone quiet for 30 minutes. The health endpoint also
reports the loaded source fingerprint and asks for a restart when the checkout
changed, preventing an old daemon from accepting new work with an outdated
evaluator contract. For a controlled batch, set `RESUME_STUDIO_WORKERS=1` to
`4` when installing the service; two remains the safe default for provider
usage and Mac resources.
The service shutdown path also terminates its tracked Codex process groups, so
restarting the companion does not leave orphaned provider calls consuming the
subscription in the background. Its launchd job has a 30-second graceful exit
window, and reinstall uses a non-forcing start so that cleanup can finish.

Open `http://127.0.0.1:4317/`. **Unchained generation** is the deepest mode: it
maps every material posting requirement to the complete authorized evidence
graph, then may synthesize new grounded bullets or Skills lines to close
supported gaps. Unsupported requirements remain explicit gaps. **Take-the-wheel
(moderate)** preserves the adaptive mode; **AI tailor** uses a more conservative
change threshold; **Used bullets** is the clean comparison baseline. All four
modes share the same evidence graph, factual gates, chronological job order,
one-page contract, and owner review. In all modes a deterministic renderer
uses the locked `CV/immutable/VictorJimenezResume.tex` visual contract;
models cannot author the LaTeX document, alter the margins or typography, or
overwrite the canonical resume. Employer headings are company-first. Usable installed
first-party Codex CLI pinned to `gpt-5.6-luna` is the sole provider lane. Each
enhanced run launches four role-separated Luna critic calls—evidence,
recruiter, technical, and screening—as a same-model jury. This gives the
evaluator distinct responsibilities without pretending it is vendor-independent;
a missing critic result remains visible and blocks readiness. The local harness
removes API-key environment variables so this owner workflow does not silently
spend API credits. Nothing from `CV/` is sent to GitHub
Actions or committed to the public repository.

The role list can be sorted by **Best Radar score**, **Newest**, or the private
**Resume Match** rubric. Match analysis reports requirement coverage, evidence
strength, domain relevance, eligibility, distinctiveness, confidence, gaps,
and source IDs; selected roles can be rechecked against the full posting. Before
you queue a draft, the cloud workspace shows a **Posting → evidence map** with
the capabilities already supported, preferred matches, and explicit gaps. A
title-only cloud preview is labeled low confidence; the private engine is what
fetches the posting and performs the source-level map.
Generation asks for a ranked evidence portfolio and lets the deterministic
page packer choose how much verified evidence the target can honestly carry;
there is no fixed entry or bullet quota. Actual PDF line widths hard-fail
wrapping, and bottom density is compared with the immutable human-authored
reference. Unsupported inline
LaTeX and lost scope qualifiers such as
`synthetic`, `prototype`, or `POC` are repaired or rejected before packing. The
critique is advisory and cannot silently mutate or grade its own plan. Factual,
target-fit, evidence, distinctiveness, clarity, privacy, eligibility, and layout
remain separate gates rather than one composite craft score.

Enhancement prompts also receive the CV authority dossier and an exact-term ATS
strategy from the captured posting. They may swap projects and rewrite bullets
around supported posting language, but unsupported requirements remain visible
gaps rather than invented claims. Completed reports show rewritten lines,
project swaps, rendered keyword coverage, and the supported terms that remain
missing versus terms the evidence does not support. Space QA distinguishes
roomy lines from near-wraps; a bullet with less than 12pt of right-edge safety
is treated as a near-wrap and rejected, even if the PDF extractor technically
reports one line.

Use **Resume bank** in the Studio header to browse one card per job and expand
it to compare every saved version. **Google onward** is the default current
quality era, while **all history** reveals legacy experiments. Each new run
keeps a private snapshot of the selected posting,
remains visible after switching to another role, and names its PDF with the
company (for example, `mayo_clinic_resume_ai.pdf`). Preview responses also send
that filename to the browser/Preview app, so downloads do not fall back to
`resume.pdf`. Project metadata uses `|` as its compact separator. TICC is
permanently excluded from every generated or workshop-edited resume, while
the local source files remain untouched. Open a saved posting snapshot from
its card when the source text was captured. Failed and in-progress runs remain
listed so an interrupted attempt is inspectable rather than silently
disappearing. The cloud bank shows the same cards after an owner sync, with
private PDF/preview/report links that continue to work while the Mac is asleep;
connect Google in Accounts if the existing owner grant is not available.

Inside each posting card, **Objective ranking** marks the strongest saved
variant for that posting. It uses a fixed, auditable shortlist rubric: target
fit, evidence safety, layout safety, and portfolio signal. It compares only
variants for the same canonical posting, keeps failed/interrupted runs out of
the winner slot, and shows the component sources, strengths, and limits. This
is an owner-only decision aid—not a claim that an outside ChatGPT session or a
hiring manager would choose the same resume. If no critic-panel result exists,
the UI says so and lowers confidence rather than inventing a verdict.
The card preview is the objective winner's preview when a rankable winner
exists, even if a newer draft was saved afterward; the expanded list still
shows the latest draft and every other version so the visual and the label
cannot disagree.

The bank also has optional **Permanent role controls** for reusable comparison
baselines: **General SWE / Cloud**, **Healthcare / Scientific AI**, **ML /
Research**, and **Data / Analytics**. A control is created only when Victor has
inspected the PDF, approved the run, and explicitly promotes a tailored winner
from the cloud bank. Promoting a new control for a family revokes the previous
active control while retaining its history. These controls are secondary
references that show supported term and signal-family gains/losses; the locked
immutable resume remains the universal audit floor and automatic fallback. A
missing, stale, revoked, or non-approved reference therefore cannot block a
run or silently become the base. Historical Google/Merck drafts remain
provisional controls until this same approval and promotion step.
Each saved version also exposes an **ATS keyword map**. Green terms occur in
the rendered resume, yellow terms are supported by the private evidence bank
but omitted from that version, and red terms are unsupported. When PDF geometry
was preserved, the clean preview receives a review-only line overlay showing
exact placement and meaningful rewrites; the downloadable PDF remains clean.
The percentages are diagnostic posting comparisons, not scores produced by a
recruiter's ATS.

Every current run also exposes a **Tailoring audit**. It separates **Fit**
(what the candidate actually has for the role) from **Tailoring** (whether the
run selected and communicated that evidence better than the locked baseline).
The audit compares base → tailored changes, surfaces supported gains, dropped
or unused evidence, redundancy/regression warnings, and unsupported claims,
and shows a `ready`, `review`, or `blocked` decision. It also makes the
recommendation explicit: **prefer tailored**, **prefer base**, or **needs
review**. An explained project swap is not counted as a regression, and a
low-priority context term such as coursework is not treated like lost core
evidence. The deeper profile can run an evidence-bounded repair pass and
accepts a replacement only if the compiled comparison gets better. Factuality,
eligibility, layout, privacy, and critic-panel failures
cannot be averaged away. This is a quality-control and comparison
report—not an ATS score or a prediction of an employer's decision. The full
`job_intelligence.json` and `tailoring_audit.json` artifacts stay local; cloud
sync exposes only their sanitized summary.

The four tailoring buttons queue independent runs, so switching postings or
starting another mode does not replace the current draft. The header shows
observed Codex tokens/calls for the current UTC week and the bank shows queued,
running, interrupted, and completed runs. Codex Plus's weekly allowance is not
available through the local CLI; set `CODEX_WEEKLY_LIMIT_TOKENS` only if you
want the UI to calculate a percentage against a known personal limit.

For a focused application session, **Tailor today** selects up to 12 roles you
added to the Pipeline on the current local calendar day. Review the selection,
choose one tailoring mode for the batch, and queue the runs together; the
private Mac engine still limits active work to two runs, and the action only
creates drafts in Resume Bank. Successful queueing moves each role from **To
apply** into the **To tailor** pipeline lane. It never marks a role Applied or
submits an application. Individual roles can be removed from the batch before
queueing.

For Victor's Notion tracker, add a `To tailor` option to the Stage status
column if it is not already present. Notion's API cannot create status options;
until that option exists, the local Pipeline and Google tracker still record
the stage and Notion leaves the local sync state auditable rather than sending
an invalid update.

The normal **balanced** and **Unchained** quality lanes use one consistent
**Codex Luna High** effort level for planning, authoring, line editing, and
every independent critic recheck. The deliberately deeper `deep` profile
retains Max for an explicit quality-frontier comparison. Each critic runs in a
fresh, critique-only subprocess
under the sealed `resume-evaluator-v2-sealed` contract. Its packet contains the
base resume, candidate resume, job snapshot, authorized evidence, and
deterministic checks—but no writer prompt, prior review, score, or readiness
control. Four role calls (evidence, recruiter, technical, and screening) must
all return an attested result; a partial panel cannot become ready. A repair
candidate is re-rendered and re-evaluated from scratch before it can replace
the prior candidate. Only one frontier line-repair pass runs; the deterministic
source-authorized compactor remains the fallback. If a later audit repair still
leaves a wrapped or near-wrapped line, a final deterministic recovery may restore
the exact authorized source wording and apply bounded source-preserving
compaction; the exact recovered artifact then receives a fresh complete sealed
panel before it can be selected. Run reports record the
stage, model, effort, latency, contract fingerprint, rubric hash, and observed
tokens in **Provider flow, model, effort, and usage**. If Max times out or a
contract attestation fails, the run remains review/blocked rather than silently
falling back to an optimistic score.

The parent audit normalizes the panel's prose before making a decision:
near-identical concerns are retained once with their supporting critic roles,
while honest candidate-role gaps (for example, testing not present in the
evidence bank) remain fit warnings rather than fabricated resume blockers.
Unsupported claims, eligibility conflicts, parsing/layout failures, and real
tailoring regressions retain their stronger gates. This keeps consensus visible
without letting four phrasings of one concern masquerade as four independent
failures.

Audit repairs now receive an explicit control-loss packet: unexplained
canonical bullets, supported terms lost from the base, project swaps, and
portfolio-overlap warnings are listed before the repair writer acts. The
repair rules prioritize restoring high-value control evidence or explaining a
real replacement before adding another role-keyword line.

The normal application lane is the bounded **balanced** authoring profile. It
keeps the same sealed critic panel and hard gates, but lets the
deterministic compiler handle measured page packing and control recovery first,
skips model space expansion and critic-driven revision/audit-repair rounds, and
uses at most one conditional Luna High line-edit pass when geometry is unsafe,
with a three-minute fallback timeout. Its post-edit density search is capped at
two rounds and is disabled for content swaps in the ordinary lane, so a
microscopic measured gap cannot trade away a stronger mechanism or validation
result. The deterministic source-preserving compactor remains available for
geometry safety, while content replacements must come from the authored role
thesis or an explicitly judged search candidate. Max is not used by this normal path;
the original two-round frontier remains available for a deliberately deeper run
with `--quality-profile deep`.
Changing profiles never changes the evaluator contract or turns a rejected
candidate into a pass.

Before writing, Resume Studio emits a deterministic role-focus receipt with a
primary track, adjacent tracks, confidence, and matched posting signals. This
lets a broad networking/performance posting prioritize systems evidence over
generic web or ATS wording. It is a routing aid, not evidence that the
candidate has a missing skill; unsupported requirements remain gaps and hard
eligibility blockers remain outside the tailoring tradeoff.

The compiler also treats quantified, validated, integration, and ownership
proof from the locked base resume as control evidence during overflow packing.
An added line counts as a positive tailoring gain only when the sealed panel
independently confirms the new target-relevant strength. This prevents both
keyword-only gains and a bookkeeping blind spot from driving the comparison.

The lab caught a subtle version of this failure: a `0.03--0.06pt` capacity
signal caused deterministic density recovery to replace distinctive evidence
with unused but weaker lines. Ordinary `balanced`, `search`, and
`search_single` runs now preserve the authored portfolio and use density logic
only as a geometry guard. A post-fix fresh-open Uber run completed in 631.9
seconds with Luna High at every quality-critical stage, a complete four-role
panel, no blockers, and `prefer_tailored`; the final artifact had no blind
density swap.

The role-evidence floor remains a lab-only hypothesis. It can identify an
omitted primary-track project, but the Anduril experiment showed that a
plausible project-level swap can still displace more distinctive evidence.
The ordinary profiles therefore record it as disabled rather than silently
mutating every authored portfolio; a future positive sealed comparison must
justify enabling it. A same-job ByteDance replay improved the failure mode:
the floor-enabled candidate had loss weight 24 plus an unsupported-claim
blocker, while the no-floor High replay had loss weight 14, no unsupported-claim
hard failure, and still correctly stayed base for eligibility and genuine
portfolio redundancy.

The post-fix Unchained Anduril stress test also stayed fail-closed: its
601-second High run found a supported Python/simulation opportunity, but the
writer broadened SynapSense into unsupported asynchronous/Python-module and
behavioral-monitoring claims. The sealed panel caught those claims and the
clearance/experience blockers, so the immutable base remained primary.

For broad validation, `scripts/resume_studio_benchmark.py` fetches and matches
many live postings concurrently, then runs a smaller full-tailoring sample
balanced across sectors and companies. By default it selects roles first listed
within the last seven days, excludes terminal radar records, and rejects a
fetched page with a definitive closed/filled banner. Its manifest preserves the
posting fetch/open check, match control, selected full-run cohort, per-run
checkpoint, latency, model, reasoning effort, panel completeness, comparative
audit outcome, quality rejections, and execution failures. This is a lab
harness, not a claim that a local evaluator predicts hiring outcomes.

The fresh-open eight-role validation at
`CV/.resume_studio/benchmarks/20260822T041401Z-0b2ad7/manifest.json` completed
8/8 runs with complete four-role panels: 5 comparative tailored preferences and
3 honest base/blocked decisions. Every provider call used `gpt-5.6-luna` at
High effort; the manifest records per-stage latency and total run time. This
demonstrates a quality-control system, not a universal win rate: the blocked
cases exposed eligibility, unsupported-claim, and redundancy failures, while
the five preferred-tailored results remain human review rather than automatic
approval.

The post-fix spot check then ran two additional fresh open roles (Neuralink and
Qualcomm) in parallel. Both completed all four High critic roles in about 11
minutes each; Neuralink preferred tailored, while Qualcomm was blocked because
the panel found an unsupported Skills technology and a damaging project swap.
That rejection is an intended quality result. The writer-side guards now catch
same-entry repeated proof anchors and newly introduced Skills technologies
without matching claim-authorized evidence before a candidate reaches the
panel.

The final artifact also receives a last source-aware portfolio guard after all
revision, density, and repair passes. This closes the case where a later writer
reintroduced a duplicate metric/mechanism story after an earlier curation step.
If the guard removes or reorders evidence, the exact resulting PDF is compiled
and judged again by a complete four-role Luna High panel; an old panel is never
reused for a changed artifact. The receipt is `final_portfolio_guard.json`.

Rewritten technical claims also pass a narrow provenance lint before judging:
terms such as C++, Python, React, APIs, streaming, asynchronous processing, or
backend machinery must appear in the primary source line or a cited supporting
source. Otherwise the exact authoritative source wording is restored. This
prevents a writer from merging a mechanism from one bullet into another while
keeping only the first bullet's citation. A fresh Anduril replay completed all
four High critic roles in 551.4 seconds, surfaced no added unsupported bullet,
and correctly kept the canonical base because eligibility and evidence gaps
still dominated.

Generation also receives a compact supported-skills checklist built from the
job-intelligence evidence map. It includes only direct/adjacent requirements
that are authorized for a `tailor_skills` action, with the exact terms and
evidence IDs needed to support them. The checklist asks the writer to surface
those terms in meaningful cited body evidence or one existing Skills rewrite;
it is not a keyword quota and does not override the sealed audit. The Nucleus
Biologics replay is documented as a negative experiment: it gained supported
REST/access-control evidence without unsupported claims, but still lost a
distinctive project and omitted several authorized signals, so the immutable
base remained the recommendation.

The normalization boundary also rejects planner-denied terms in postfix form
(for example, “AWS is unsupported”) before they are promoted into supported
ATS or generation opportunities. This prevents a gap-analysis explanation from
accidentally turning a negative mention into a checklist item. The corrected
Nucleus replay moved the comparative tailoring state from `regressed` to
`improved`, but remained blocked for eligibility and a remaining portfolio
regression.

Enhanced project swaps also pass a source-level preflight: if a canonical
project is dropped, the decision ledger must name every omitted canonical
bullet ID. This preserves creative, evidence-backed swaps while preventing a
single high-information mechanism from disappearing behind a project-level
explanation. The Stryker replay demonstrated the gate: its healthcare/security
swap omitted Quantum's historical-market pipeline ID, so the candidate would
now fail closed before reaching the sealed comparison.

Gap-analysis terms are also semantically checked before promotion: a provider
cannot attach a generic supported term to an unrelated requirement unless the
term appears in that requirement, its rationale, or the cited authorized
evidence. Deterministic inventory terms are still retained separately. This
keeps the requirement-to-evidence map useful for reasoning instead of letting
an ATS vocabulary item masquerade as role understanding.

Portfolio search applies one additional fail-closed rule: a candidate cannot
be promoted merely because its comparative audit says `prefer_tailored`. The
sealed review must also report no hard failure and all four critic roles must
complete. This matters because a candidate can be relatively better than its
base while still failing a non-averagable quality gate such as
distinctiveness. The search receipt records that gate explicitly as
`critic_hard_fail`; a partial `review` remains visible for human inspection
instead of being converted into a false ready state.

The post-fix Stryker replay completed three concurrent candidates in 388.5
seconds wall time with complete four-role Luna High panels. All three were
rejected as material improvements and the canonical base stayed primary.
That is a useful negative result: the search explored alternatives, but did
not turn a relative or cosmetic change into an application-ready winner.

The duplicate detector also recognizes a small set of repeated mechanisms such
as resampling/stratified validation and calibration, including across an
experience and project when two distinctive terms and a shared metric support
the match. Generic overlap alone cannot delete evidence; the affected real-role
cases are rerun in the lab before this is considered a measured improvement.

Geometry recovery has the same evidence boundary: after the bounded High line
editor times out, it may restore an authoritative shorter source line or use a
source-preserving Skills abbreviation, then compile again. It cannot lower the
one-line threshold or invent wording. The exact Anduril failure was rescued in
an offline compile check and is being rerun through the full evaluator.

For healthcare-role experiments, a prior saved draft can be used as a
provisional pairwise control without becoming evidence authority. The completed
Stryker-vs-Merck control used the best saved Merck AI PDF as the baseline and
four fresh sealed Luna Max critics; the qualitative verdict was
`prefer_base_merck` because the Stryker candidate lost distinctive evidence and
had a consensus near-wrap/readability defect. The Merck PDF remains
owner-unapproved and the control reports no exact hiring score.

The primary artifact now follows that comparison decision. When the audit
returns `base`, `blocked`, or `review`, Resume Studio publishes the immutable
canonical PDF as the run's winner and preserves the rejected or unreviewed
generated artifact as `tailored_candidate.pdf` (with `base_control.tex` and the
reason recorded in `winner_artifact`). A compiling tailored PDF is therefore
never silently presented as the best version when the control comparison is
negative or incomplete.

Open **Workshop** on a completed run to edit education, skills, experience,
projects, and leadership lines without touching the original PDF. Saving a
line creates a unique rendered revision; the AI writing partner returns
source-grounded candidates for approval, and revision history can revert to an
earlier draft. The current Mac exposes one approved lane: the Codex CLI pinned
to `gpt-5.6-luna`. The critic panel uses four role-labeled Codex calls; it is
deliberately not described as an independent vendor review.

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
  or **Connect Google + create my Sheet** requests the least-privilege
  `drive.file` permission and creates a separate workbook in that user's
  Google Drive with distinct **Applications**, **Internships**, and
  **Preferences** tabs. The encrypted HttpOnly session carries that user's
  tracker grant and Sheet ID to the backend; the private Accounts registry is
  an optional account-linking enhancement, not a dependency for public users.
  Existing trackers are rediscovered in Drive after reauthentication, and the
  backend never exposes another user's rows or token.
  OAuth requests Drive file access only—never Gmail access. The internship
  email toggle controls GitHub alert batches, not inbox reading.
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
narrative the scoring system uses, and explicit favorite score overrides. PM-family weight
is intentionally zero and its no-alert behavior is a gate, not a notification
setting. Edit and push — next run picks it up. Seed companies:
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
uv sync --frozen --extra dev
uv run pytest tests/ -q            # unit tests (real captured fixtures)
uv run radar doctor --json
uv run radar crawl run --dry-run   # typed grouped CLI
uv run radar score explain JOB_ID
uv run radar db migrate --sql      # review Postgres DDL without applying it
```

Python 3.12 is pinned in `.python-version`; `requirements.txt` remains a thin
compatibility entry point. Production workflows share the repository's local
setup action and frozen `uv.lock`. Optional maintained provider translation is
installed with `uv sync --extra ai` and enabled with
`RADAR_LLM_ADAPTER=litellm`; AI is still unnecessary for crawling or scoring.

Useful env vars: `RADAR_DISABLE_SOURCES=ats,hn`, `RADAR_PROBE_BUDGET`,
`RADAR_MAX_COMPANIES`, `RADAR_PM_BACKFILL_COMPANIES`, `RADAR_MAX_ALERTS`,
`RADAR_WORKERS`, `RADAR_LIFECYCLE_ACTIVE_DAYS=45`,
`RADAR_LIFECYCLE_UNSEEN_GRACE_DAYS=14`, `RADAR_HISTORY_DAYS=730`,
`RADAR_LINK_RESOLVE_LIMIT=25`, `RADAR_LINK_RESOLVE_TTL_DAYS=30`,
`RADAR_LINK_RESOLVE_WORKERS=12`,
`RADAR_WORKDAY_MAX_RESULTS=200`, `RADAR_PHENOM_MAX_RESULTS=100`,
`RADAR_EIGHTFOLD_MAX_RESULTS=100`, and
`RADAR_SMARTRECRUITERS_MAX_RESULTS=1000`.

The normalized Postgres path is opt-in through `DATABASE_URL`: `radar db
migrate`, `radar db import-legacy`, and `radar db parity` provide an idempotent
cutover with stable public IDs, aliases, sightings, score snapshots,
application events, preferences, prompt/LLM audit rows, and leased work items.
The deterministic crawler and current JSON production datastore continue to
work with no database or new secret.

Other one-off CLI commands: `notion-verify`, `email-verify` (connectivity checks,
create nothing), `email-watch` (run one detection cycle manually),
`tracker-sync` (pull and push tracker stages), and
`resolve-links` (bounded, auditable aggregator-link backfill; set
`RADAR_LINK_RESOLVE_LIMIT` for the batch size; it defaults to 200 open rows and
uses bounded parallel requests), and
`lifecycle` (reconcile stale state plus retry terminal Notion archives without
source discovery).
