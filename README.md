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

The ChemE internship board is intentionally separate from this new-grad
AI/SWE/DS board. It reads the `claude/cheme-intern-radar` branch and keeps its
own generated state and GitHub board labels, while both profiles use the one
repository-level `NOTION_TOKEN` and therefore the same Notion Applications
database.

Subscribe to the RSS feed at:
`https://raw.githubusercontent.com/VictorJimenez3/fable-job-search/claude/newgrad-job-search-system-9gbj9k/docs/feed.xml`

## How it works

```
every ~30 min (GitHub Actions cron)
│
├─ 1. BREADTH: pull 5 aggregators
│     SimplifyJobs · vanshb03 · jobright-ai (SWE + Data) · speedyapply · HN Who-is-Hiring
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
│
├─ 4. RANK: verified new-grad/early-career fit first, then AI/ML and data
│     science, then AI-oriented/general SWE and systems; sector, company tier,
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
      · GitHub issue "Job Radar alerts — week N" (assigned to you → push/email)
      · 📌 master board issue — every open alert-worthy role in ONE place
        (body + comment pages; rewritten each crawl; checkboxes work there too)
      · 🏆 "Best of <date>" issue each evening — the daily top-10, emailed to
        you via GitHub's own notification
      · docs/DASHBOARD.md — everything decent, sorted
      · docs/feed.xml — RSS for instant notifications in any feed reader
```

### Tracking and applied logging

1. **Check a box on an alert issue to track a job.** It appears in your selected
   tracker (Notion by default; Google Sheets is also supported) immediately with the not-yet-applied status
   (`stage_saved` in profile.yaml, default "Not started") and improves future
   ranking.
2. **When you apply, the inbox becomes the source of truth.** With the email
   credentials set up (below), the watcher reads application-lifecycle emails
   and drives the tracker **Stage** for you, end to end — no manual updates:
   - "Thank you for applying…" → promotes the tracked entry to **Applied**
   - online-assessment / coding-challenge invite → **OA**
   - interview / "schedule a call" / "next steps" → **Interview**
   - "unfortunately… other candidates" → **Rejected** (+ Response date)
   - applied with no reply for `autoclose_days` (default 45) → **CLOSED**

   It only ever moves a job *forward*, so a stray late email can't undo a
   later stage. You can still change anything in Notion/Sheets by hand; the
   twice-daily readback now brings those stage edits into the radar.
3. For a job found outside the radar, comment `applied <url>` on any issue to
   log it as Applied immediately.
4. A twice-daily reconcile sweep re-reads every radar issue and tracks any
   checked box the event pipeline missed — a tick is never lost.

The weekly strategy memo now includes an **auto-tracked funnel** (applied → OA
→ interview → rejected → auto-closed) and your response rate, overall and by
sector — built entirely from what the watcher reads, so you can see which
sectors actually reply.

The platform now builds evidence-first company briefs from bounded excerpts of
official postings the crawler already reads. Products, customers, mission,
business context, technical work, locations, visa context, candidate relevance,
and interview focus carry source links/dates; absent facts say **Not confirmed**.

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

**2. Optional email-based applied-detection (~3 min)**
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

Optional upgrades:
- The four NVIDIA NIM keys are now wired into a task-aware, budgeted cloud
  router with fallback, cooldowns, schema validation, and usage telemetry.
  Main and ChemE enrich nightly; they are deliberately not exposed to every
  30-minute crawl. Configuration and operating policy:
  [docs/AI_SETUP.md](docs/AI_SETUP.md).
- Prefer Google Workspace to Notion? The Google Sheets tracker adapter is
  complete; its one-time OAuth activation is documented in
  [docs/GOOGLE_SHEETS_SETUP.md](docs/GOOGLE_SHEETS_SETUP.md).
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
  the company registry, learned taste, applied log, run stats.
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
