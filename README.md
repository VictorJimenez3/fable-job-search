# 🎯 Job Radar

A self-expanding, always-on radar for **new-grad AI / SWE / DS roles**, tuned for
speed (apply within 24h of posting) and personalized ranking (healthtech first,
big tech second, open to everything good).

**[→ Live dashboard](docs/DASHBOARD.md)** ·
**[→ Culture Compass](docs/CULTURE.md)** ·
**[→ SHPE 2026 plan](docs/SHPE.md)** ·
**[→ Alert issues](../../issues?q=is%3Aissue+label%3Aradar-alerts)** ·
**[→ Roadmap](ROADMAP.md)** ·
**[→ RSS feed](docs/feed.xml)** ·
**[→ Cross-CLI handoff notes](docs/CLI_HANDOFF.md)**

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
├─ 4. RANK: hard gates (no senior/intern/PhD/non-US/3+ yrs) then a scored,
│     auditable rubric: role fit + sector fit + freshness + learned taste.
│     Optional Claude re-rank + per-job application angle (add ANTHROPIC_API_KEY).
│
└─ 5. DELIVER
      · GitHub issue "Job Radar alerts — week N" (assigned to you → push/email)
      · docs/DASHBOARD.md — everything decent, sorted
      · docs/feed.xml — RSS for instant notifications in any feed reader
```

### Shortlisting vs. applied — how logging actually works

A checkbox can only record intent, not truth — you can tick a box without
ever hitting submit. So the two are deliberately separate:

1. **Check a box on an alert issue** = "save this for later." It records a
   shortlist entry and gives ranking a small nudge. **Nothing is sent to
   Notion at this point.**
2. **When you actually apply and the company's confirmation email lands**
   (e.g. "Thank you for applying to..."), the `email-watch` workflow detects
   it, matches the company against your shortlist (or anything the radar has
   ever seen), and *that's* what gets logged to Notion as Stage=Applied — no
   further action from you.
3. If email detection ever misses one (unusual confirmation wording, or a job
   you found outside the radar entirely), comment `applied <url>` on any
   issue to log it immediately.

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

**2. Email-based applied-detection (~3 min)**
NJIT uses Google Workspace/Gmail, so this uses IMAP with an App Password
(Google's supported way to let a non-browser client log in — this is not
your NJIT password and can be revoked anytime independent of it):
1. On the `vmj@njit.edu` Google account, enable **2-Step Verification** at
   [myaccount.google.com/security](https://myaccount.google.com/security) if not already on
   (required before Google will issue app passwords).
2. Generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   — name it "Job Radar", copy the 16-character password.
3. Add two repo secrets: `EMAIL_ADDRESS` = `vmj@njit.edu`, `EMAIL_APP_PASSWORD` = the 16-char password.

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
repo, generates culture dossiers + rerank passes locally, and pushes the
enriched state back. The cloud crawler never depends on the Mac — the Mac
just upgrades whatever it finds when awake. Requires `git push` auth on the
Mac (`brew install gh && gh auth login`). Logs: `~/.jobradar/logs/enrich.log`.
The companion releases the model from memory after every request; confirm with
`ollama ps` (it should show no loaded model between enrichment cycles).

Optional upgrades:
- `ANTHROPIC_API_KEY` secret → Claude does the enrichment in the cloud too
  (works alongside or instead of the Mac). Free-tier alternative: a Google AI
  Studio key via `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` secrets pointed at
  Gemini's OpenAI-compatible endpoint.
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
alert lines, feeding ranking (±6), and tabulated in
[docs/CULTURE.md](docs/CULTURE.md) — prestige tier, pace, WLB, PTO,
shutdowns, new-grad TC, rotational programs. ~40 dossiers are human-curated;
the rest are LLM-generated on your Mac and labeled `est.`. Ask about any
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
