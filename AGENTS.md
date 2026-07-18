# Codex repository instructions

This repository is both the application and its production datastore. GitHub
Actions and the Mac companion commit generated state frequently, so begin by
fetching before drawing conclusions and preserve unrelated state changes.

## Read before changing code

1. `CLAUDE.md` — despite the legacy filename, this is the current tool-neutral
   map, documentation contract, and set of architectural house rules.
2. `README.md` — system purpose, setup, and operating flow.
3. `DECISIONS.md` — append-only product and architecture decisions.
4. `docs/CLI_HANDOFF.md` — live operational facts and next work.
5. `profile.yaml`, then the relevant module under `radar/` and its tests.

Do not create a competing handoff document. Update the existing owner named in
`CLAUDE.md` and `docs/CLI_HANDOFF.md` when behavior changes.

## Repository invariants

- Generated files are `state/*.json`, `docs/DASHBOARD.md`, `docs/CULTURE.md`,
  and `docs/feed.xml`. Do not hand-edit them except for a documented repair.
- `webapp/index.html` is canonical and `docs/platform/index.html` must remain a
  byte-for-byte copy after frontend changes.
- Scoring and demotions stay auditable through reason strings. AI may enrich or
  adjust results; it must degrade gracefully and must not become required for
  the deterministic crawler.
- Never scrape LinkedIn. The application may construct LinkedIn search links.
- `CV/` is personal, local-only, and gitignored. Never commit it or derivatives.
- Preserve the owner checks around issue events, OAuth, and web actions. This is
  a public repository whose automation has write access.

## Validate proportionally

Use the repository virtual environment on this Mac:

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m compileall -q radar tests
cmp webapp/index.html docs/platform/index.html
```

Production uses Python 3.12; the checked-in Mac virtual environment currently
uses Python 3.9, so syntax or dependency changes should also be allowed to run
through the GitHub Actions test workflow before production deployment.

## Branch and production safety

The default production branch is currently
`claude/newgrad-job-search-system-9gbj9k`. Its name is historical, not a signal
that Codex should work elsewhere. Scheduled workflows execute from that branch,
and CI/Mac jobs continuously commit state to it.

Do not rename the default branch piecemeal. A coordinated rename must update:

- hard-coded refs in `.github/workflows/daily-best.yml`, `reconcile.yml`, and
  `web-actions.yml`;
- the fallback branch in `webapp/api/_lib.js` and the deployed Vercel
  `RADAR_BRANCH` value;
- raw GitHub links in `README.md` and the installer comment/fallback in
  `scripts/mac-companion/install.sh`;
- the installed Mac companion clone/launch agent, then both platform doors.

For ordinary work, branch from the latest remote default, keep generated state
out of feature commits, and rebase immediately before handoff because production
may advance every 30–60 minutes.
