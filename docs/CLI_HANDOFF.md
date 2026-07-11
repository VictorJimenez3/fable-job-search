# CLI handoff notes

This repository is maintained across more than one coding CLI (including Claude
and Codex). Keep the repository—not a chat transcript—as the shared source of
truth.

## Read first

Before changing the radar, read these in order:

1. [`README.md`](../README.md) for the system's purpose and operational flow.
2. [`DECISIONS.md`](../DECISIONS.md) for the deliberate architecture and trade-offs.
3. [`profile.yaml`](../profile.yaml) for Victor's active search preferences and ranking thresholds.
4. The relevant module and test under `radar/` and `tests/`.

## Keep these current

- Update `README.md` when user-facing setup, commands, sources, delivery
  channels, or operating behavior changes.
- Add a dated entry to `DECISIONS.md` when making a non-obvious product or
  architecture choice, especially one that trades recall for precision.
- Update `profile.yaml` only for candidate preferences and ranking policy—not
  implementation behavior.
- Add or update tests for scoring gates, source parsing, state migration, or
  output behavior that changes.
- Do not hand-edit generated runtime outputs (`state/*.json`,
  `docs/DASHBOARD.md`, or `docs/feed.xml`) except for a deliberate repair with
  its reason documented in the commit/message. Crawls generate them.

## Current operational facts (2026-07-10)

- GitHub Actions is the production runtime; it uses Python 3.12. Local macOS
  Python is currently 3.9 and does not have the project dependencies installed.
- The radar runs every ~30–60 minutes, with a Monday strategy memo. Its state
  is committed by CI.
- `NOTION_TOKEN` enables application writes. A checked alert item means Victor
  confirms he applied and creates the Notion entry; email confirmation
  detection is only a backup. Run the read-only `notion-verify` GitHub workflow
  when diagnosing the connection.
- `ANTHROPIC_API_KEY` is optional. The Mac companion uses Ollama locally and
  runs enrichment every two hours while the laptop is awake; it should release
  the model from memory when each task finishes.
- Ranking quality is the highest-priority improvement: reduce false alerts by
  requiring clearer entry-level evidence, improving US-only location handling,
  and distinguishing technical data roles from business/operations analyst
  positions. Re-score or retire stale state if the policy changes so generated
  outputs reflect it promptly.

## Safe handoff practice

At the end of any material change, state:

1. What changed and why.
2. Files changed.
3. Validation run and its result (or why it could not run).
4. Any configuration/secrets or GitHub-side action still required.

Preserve unrelated working-tree changes. Do not assume a secret exists merely
because the code references it.
