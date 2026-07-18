# Chemical Engineering Internship Radar

An always-on US internship and co-op search for undergraduate Chemical
Engineering candidates. It favors chemical/process engineering,
manufacturing and operations, bioprocess/pharma, materials/semiconductors,
environmental/safety, and quality/validation work.

This is the `claude/cheme-intern-radar` profile of the broader Job Radar
project. Scoring is deterministic and auditable; AI is optional.

- [Production platform](https://job-radar-cheme.vercel.app)
- [Static Pages mirror](docs/platform/index.html)
- [User guide](docs/TUTORIAL.md)
- [Live generated dashboard](docs/DASHBOARD.md)
- [Roadmap](ROADMAP.md)
- [AI setup](docs/AI_SETUP.md)
- [Maintainer handoff](docs/CLI_HANDOFF.md)

## What is complete

- The source mix is internship-first. The Simplify Summer 2026 internship
  JSON feed was live-checked on 2026-07-18, and direct ATS polling starts
  from 22 ChemE-relevant employers. Workday searches use ChemE internship
  and co-op queries rather than software/new-grad terms.
- Rules v5 require internship/co-op evidence for alerts, classify seven ChemE
  role families, reject senior/PhD/clearance/non-US/3+ year roles, keep nearby
  engineering disciplines dashboard-only, and reject unrelated internship noise.
- Posting text is analyzed for minimum experience and sponsorship language.
  With `candidate.needs_sponsorship: true`, an explicit no-sponsorship posting
  is visible but cannot become an alert. Unknown remains unknown; the app does
  not pretend silence means sponsorship.
- The platform has ChemE role filters, sponsorship and experience filters,
  visible posting facts, a direct Apply action, role-specific research links,
  and outreach templates. Opening a role leads with fit and eligibility.
- Old tech-oriented registry and culture state is preserved for audit but
  excluded from ChemE polling/scoring. Rules-version migrations re-gate inherited
  open jobs without deleting history.

## Pipeline

```text
GitHub Actions (about every 30 minutes, orchestrated by the default branch)
  ├─ internship aggregator feed
  ├─ direct employer ATS searches (Workday, Greenhouse, Lever, Ashby, …)
  ├─ ATS discovery and live validation
  ├─ ChemE gates + role/sector/freshness scoring
  ├─ posting-text sponsorship and experience analysis
  └─ platform, dashboard, RSS, GitHub alerts, and optional Notion sync
```

The crawler works with no AI key. A failed source is isolated and logged; it
does not fail the whole run.

## Production topology

The new-grad tech board remains the repository's default production branch.
ChemE is a separate Vercel project at
[`job-radar-cheme.vercel.app`](https://job-radar-cheme.vercel.app), configured
with `RADAR_BRANCH=claude/cheme-intern-radar` and `RADAR_PROFILE=cheme`. It has
independent jobs, feedback, pipeline state, dashboard, and labeled GitHub board
issues, so the two searches cannot overwrite each other.

GitHub schedules workflow files only from the default branch. That branch owns
the `cheme-radar`, `cheme-daily-best`, and `cheme-reconcile-checkboxes`
orchestrators; each explicitly checks out and commits back to this branch.
Web actions carry the `cheme` profile marker, and ChemE issues carry
`radar-cheme`, so interactive events route here too. Do not make this branch
the repository default just to activate schedules.

Both boards deliberately use the same repository-level `NOTION_TOKEN` and the
same Applications database. The ChemE Vercel board starts in tokenless/PAT
mode; adding instant OAuth writes later requires a second GitHub OAuth app with
the ChemE URL as its callback, not a second Notion integration.

## Personalize before connecting accounts

The branch deliberately does not guess the student's identity or Notion
schema.

1. Edit `profile.yaml`:
   - candidate name and graduation year;
   - `needs_sponsorship` (safe default is `true`);
   - role/sector weights, locations, thresholds, and Notion stage names.
2. Edit `ME` near the top of `webapp/index.html` for outreach templates, then
   copy it to the Pages mirror:
   `cp webapp/index.html docs/platform/index.html`.
3. Optionally add verified employers to `data/companies_seed.yaml`.

## Optional connectors

No connector is required for discovery or ranking.

- `NOTION_TOKEN`: tracks saved/applied jobs in the shared Notion Applications
  database used by both boards.
  Share the database with the integration and make `profile.yaml` stage values
  exactly match its select/status options. Run `notion-verify` before relying
  on writes.
- `EMAIL_ADDRESS` + `EMAIL_APP_PASSWORD`: reads application lifecycle email
  and advances stages. Run `email-verify`; it is read-only.
- `ANTHROPIC_API_KEY`, or `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`:
  optional quality review, employer dossiers, and company scouting. See
  [AI setup](docs/AI_SETUP.md). A ChatGPT Pro subscription does not include
  OpenAI API credits; API billing and keys are separate.
- `GOOGLE_CSE_KEY` + `GOOGLE_CSE_ID`: optional public LinkedIn-post discovery.
  The project never logs in to or scrapes LinkedIn.
- GitHub OAuth variables for owner-only Vercel writes are listed in
  [the forking guide](docs/FORKING.md). Pages works without them through
  prefilled GitHub issues.

Secrets belong in GitHub Actions or Vercel settings, never in this repository.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/
.venv/bin/python -m radar.main seed
.venv/bin/python -m radar.main crawl
```

Useful commands: `regate`, `enrich`, `notion-verify`, `email-verify`,
`strategist`, `daily-best`, and `master-board`.

Useful controls: `RADAR_DISABLE_SOURCES`, `RADAR_PROBE_BUDGET`,
`RADAR_MAX_COMPANIES`, `RADAR_MAX_ALERTS`, `RADAR_WORKERS`,
`RADAR_SCRAPE_LIMIT`, and `RADAR_SCRAPE_DISABLE`.

Runtime files under `state/` and generated outputs (`docs/DASHBOARD.md`,
`docs/CULTURE.md`, `docs/feed.xml`) are written by the pipeline. Do not edit
them by hand.
