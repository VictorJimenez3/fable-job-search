# Chemical Engineering Radar — user guide

The radar finds US internships and co-ops, ranks them for an undergraduate
Chemical Engineering profile, and explains why each role is or is not a fit.
It is designed to reduce opening ten tabs just to discover that a role wants
three years of experience or explicitly refuses sponsorship.

## Start in the platform

The **Jobs** page is the daily workspace:

- filter by ChemE role family, sponsorship signal, minimum-experience signal,
  status, sector, source, and score;
- click a job title or **Details** to open fit and eligibility information;
- use **Apply** to open the employer posting and track the application intent;
- use **Save** when the role is interesting but not ready to apply;
- search unfamiliar employers and find recruiter/alumni outreach links from
  the job workspace.

Sponsorship has three honest values:

- **sponsors**: the posting contains positive sponsorship/CPT/OPT language;
- **no sponsorship**: the posting explicitly rules it out;
- **unknown**: no reliable statement was found. Unknown is not treated as yes.

Experience works the same way. A visible number comes from posting text; an
unknown value means the parser did not find a trustworthy requirement.

## A practical application loop

1. Filter to a role family such as **Chemical / Process** or
   **Bioprocess / Pharma**.
2. Review sponsorship, experience, location, freshness, and score before
   opening the posting.
   The board hides explicit non-US locations, including ambiguous ATS labels
   such as `CA, Ontario`; unknown locations remain visible for manual review.
3. Open **Details** for score reasons and the source evidence.
4. Click **Apply**, tailor the resume yourself, and submit on the employer site.
   The radar never auto-submits applications.
5. Mark the job Applied in the platform or Notion. Notes and outreach remain
   attached to the job record.

The GitHub issue surfaces remain available: weekly alerts, the master board,
and daily best-of. Checking a box tracks the role. A twice-daily reconciliation
workflow catches checkbox events that webhooks miss.

## Why a good-looking job may not alert

The dashboard has higher recall than alerts. A job may remain visible but be
demoted when it:

- lacks internship/co-op evidence;
- is clearly another discipline (for example software or civil engineering);
- asks for 3+ years, a PhD, clearance, or non-US work;
- explicitly offers no sponsorship while the profile says sponsorship is
  required;
- is a generic engineering internship outside a priority ChemE sector.

Every score and demotion is recorded as a reason. Edit `profile.yaml` to change
candidate preferences; edit code/tests only when changing system behavior.

## Connectors

- **Notion:** add `NOTION_TOKEN`, share the Applications database with the
  integration, align the status option names in `profile.yaml`, and run the
  `notion-verify` workflow.
- **Email:** add `EMAIL_ADDRESS` and a revocable Gmail App Password as
  `EMAIL_APP_PASSWORD`, then run `email-verify`.
- **Optional AI:** follow [AI_SETUP.md](AI_SETUP.md). Deterministic discovery,
  scoring, sponsorship, and experience extraction work without it.

## Run it now

In GitHub Actions, manually run:

- `tests` after changing code or configuration;
- `radar` for a full crawl;
- `enrich` for optional AI quality/dossier work;
- `notion-verify` or `email-verify` before trusting a connector.

Scheduled runs are launched by the default branch's `cheme-*` workflows, which
check out this branch explicitly. This branch should remain separate so its
jobs and generated state do not replace the new-grad board.
