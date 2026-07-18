# CLI handoff — Chemical Engineering branch

The repository, not chat history, is the shared source of truth across Codex,
Claude Code, and local development.

## Current state (2026-07-18)

- Active worktree/branch: `claude/cheme-intern-radar`.
- The branch was stale and contained no ChemE implementation. It was merged
  with the current production branch before the ChemE conversion.
- Rules are now v6 and internship-first. Stored jobs remain intact; after each
  rules-version bump, the next crawl re-gates old open records and prevents
  tech/new-grad records from continuing as alerts.
- `profile.yaml` has a generic Chemical Engineering candidate and intentionally
  sets `needs_sponsorship: true`. Identity and Notion option names require a
  human review before account connection.
- Aggregation uses the live Simplify Summer 2026 internship JSON feed. A stale
  Summer 2027 community feed is defined for compatibility but disabled in the
  profile. Direct polling is filtered to ChemE sectors and starts from the
  curated seed file.
- Workday endpoints in the seed were live-probed on 2026-07-18. Failed guessed
  endpoints were not added.
- The platform is ChemE/eligibility-first. `webapp/index.html` is canonical;
  `docs/platform/index.html` must remain a byte-for-byte copy.
- Learned taste is stored separately in `state/feedback_cheme.json`, so old
  software-search clicks cannot boost ChemE results.
- Generated state and docs still show the last production crawl until this
  branch is activated and crawled. Do not hand-edit them to make screenshots
  look current.
- The independent production site is `https://job-radar-cheme.vercel.app`.
  It reads this branch with `RADAR_PROFILE=cheme` and currently uses
  tokenless/PAT writes. Both profiles share the repository `NOTION_TOKEN` and
  one Notion Applications database.

## Production activation outside the code

GitHub schedules only workflow files on the default branch. Keep the current
new-grad branch as default: its `cheme-radar`, `cheme-daily-best`, and
`cheme-reconcile-checkboxes` workflows explicitly check out this branch. Run
those workflows from the default branch when verifying production. Interactive
dispatches include `profile=cheme`; ChemE issues use the `radar-cheme` label,
which routes checkbox edits and comments back here.

Vercel is a separate `job-radar-cheme` project configured with
`RADAR_BRANCH=claude/cheme-intern-radar`, `RADAR_PROFILE=cheme`, and
`AUTH_MODE=tokenless`. A second GitHub OAuth app can enable instant owner-only
writes later. It does not need a second Notion token or database.

No connector was assumed to be configured. Verify rather than infer:

- `notion-verify` after adding `NOTION_TOKEN` and sharing the database;
- `email-verify` after adding `EMAIL_ADDRESS`/`EMAIL_APP_PASSWORD`;
- `enrich` after adding an LLM provider.

## Implementation map

- `radar/score.py`: ChemE gates, role buckets, rules-version re-gating.
- `radar/sector.py`: ChemE sector classification.
- `radar/main.py`: source selection and registry-sector filtering.
- `radar/posting.py`: deterministic sponsorship/experience facts.
- `radar/quality.py`: optional LLM internship/role-family verification.
- `radar/sources/aggregators.py`: internship feeds.
- `radar/sources/ats.py`: internship-mode ATS behavior.
- `profile.yaml`: candidate/ranking policy.
- `data/companies_seed.yaml`: direct-employer starting registry.
- `webapp/index.html`: platform UI; mirror after every edit.

Internal keys such as `explicit_new_grad` and `quality.new_grad` remain only for
backward-compatible state reads. New records also carry
`explicit_internship`; user-facing language must say internship.

## Non-negotiable maintenance rules

- Preserve deterministic reasons and demote uncertain-but-possibly-useful jobs
  rather than silently deleting them.
- Never infer sponsorship from silence.
- Never scrape logged-in LinkedIn.
- Never commit secrets or personal resume/application data.
- Never hand-edit generated `state/*.json`, `docs/DASHBOARD.md`,
  `docs/CULTURE.md`, or `docs/feed.xml`.
- Run the full test suite before handoff.
- Preserve unrelated working-tree changes and use a separate worktree when
  branches have concurrent work.

## Next work

The code branch is feature-complete and separately deployed. Remaining work is
the human profile review plus the prioritized product backlog
in `ROADMAP.md`, led by an official employer sponsorship-history evidence layer
and academic-term/enrollment filters.
