# 🎯 Job Radar

A self-expanding, always-on radar for **new-grad AI / SWE / DS roles**, tuned for
speed (apply within 24h of posting) and personalized ranking (healthtech first,
big tech second, open to everything good).

**[→ Live dashboard](docs/DASHBOARD.md)** ·
**[→ Alert issues](../../issues?q=is%3Aissue+label%3Aradar-alerts)** ·
**[→ RSS feed](docs/feed.xml)** (`https://raw.githubusercontent.com/VictorJimenez3/fable-job-search/main/docs/feed.xml` — replace `main` with the default branch name)

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

### Applied logging (the Notion loop)

You apply manually — tracking is automatic:

1. Open the week's alert issue, apply to a job, **check its checkbox**.
2. The `applied-sync` workflow logs it to `state/applied.json`, boosts similar
   roles in future ranking, and creates a page in your Notion **Applications**
   database (Company, Position, Stage=Applied, Job URL, Location, Apply date).

Comment commands on any issue: `applied <url>` (log a job found elsewhere),
`skip <job-id>` (downrank similar), `track <ats> <token> [Name]` (force-add a company).

## Setup (one-time, ~3 min)

Everything works out of the box **except the Notion write**, which needs a
credential only you can create:

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → *New integration*
   (internal), any name, your workspace. Copy the secret.
2. In Notion, open **Job Hunt Tracker → Applications** database → ⋯ menu →
   *Connections* → add your integration.
3. In this repo: *Settings → Secrets and variables → Actions → New repository
   secret*: name `NOTION_TOKEN`, value = the secret.
   Any applications you logged before adding the token sync automatically on
   the next run (or run the `radar` workflow manually).

Optional upgrades:
- `ANTHROPIC_API_KEY` secret → Claude semantically re-ranks borderline jobs and
  writes a one-line application angle into each alert.
- **Watch the repo** (Watch → All activity) and install the GitHub mobile app
  for instant pushes; issues are also assigned to you, which notifies by default.
- Subscribe to `docs/feed.xml` in a feed reader (raw URL above) for sub-minute alerts.

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
