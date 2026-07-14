# CLI handoff notes

This repository is maintained across more than one coding CLI (including Claude
and Codex). Keep the repository—not a chat transcript—as the shared source of
truth.

## Read first

Before changing the radar, read these in order:

1. [`README.md`](../README.md) for the system's purpose and operational flow
   (and [`TUTORIAL.md`](TUTORIAL.md) for how Victor actually uses it — keep it
   current when user-facing behavior changes).
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

## Current operational facts (2026-07-13)

- GitHub Actions is the production runtime; it uses Python 3.12. On Victor's
  Mac, system Python is 3.9 but the repo's `.venv` has the dependencies —
  run tests with `.venv/bin/python -m pytest tests/`. CI commits state every
  ~30 min, so always `git pull --rebase` before pushing.
- **Delivery surfaces, all live:** weekly alert issue (checkbox = track to
  Notion as not-yet-applied), 📌 master board issue (every open alert-worthy
  role, rewritten each crawl), 🏆 daily best-of issue, docs/DASHBOARD.md,
  RSS, and the platform website. Twice-daily reconcile sweep guarantees no
  checked box is ever lost.
- **The platform has two permanent doors** (DECISIONS #27): Vercel
  (job-radar-vmj-8946s-projects.vercel.app — GitHub OAuth, instant writes,
  Victor's daily driver) and GitHub Pages
  (victorjimenez3.github.io/fable-job-search/platform/ — tokenless, what
  forks get). `webapp/index.html` is canonical; `docs/platform/index.html`
  is a byte copy. Jobs tab shows posting age and sorts by best-match or
  newest-first.
- **Notion:** `NOTION_TOKEN` is set and working. Checkbox → entry with the
  `stage_saved` status ("Waiting for a referral" in his DB); Victor promotes
  manually, OR the **email autopilot** (DECISIONS #26, shipped 2026-07-13 by
  an Opus session) advances Stage from lifecycle emails (applied/OA/
  interview/rejected, forward-only, auto-close after 45 d silence) — that
  path is coded and tested but **awaits the `EMAIL_ADDRESS` /
  `EMAIL_APP_PASSWORD` secrets** (Gmail app password, see README §2).
- **Scoring:** hard gates + Shams rule — `marquee_companies` in profile.yaml
  (MANGA, AI labs, elite pharma/medtech incl. Merck) always alert; $150k+
  (`pay_bank`) also bypasses new-grad-wording requirements. The marquee list
  is duplicated in `webapp/index.html` (`S.marquee`) — keep both in sync.
- **Local AI:** Mac companion installed (`~/.jobradar`, launchd
  `com.jobradar.enrich`, every 2 h while awake) running Ollama `qwen3:30b`
  with `format:"json"` forced (DECISIONS #20 — thinking models otherwise
  burn the token budget). Each cycle: culture dossiers, re-score recent
  jobs, weekly LLM company scout, and the **quality pass**
  (`radar/quality.py`, DECISIONS #30): link liveness + LLM new-grad/role-fit
  verification, ~15 jobs/cycle, aggregator links first, verdicts cached on
  the job record and re-applied after every re-score. Knobs:
  `RADAR_QUALITY_LIMIT`, `RADAR_QUALITY_DISABLE`.
- **Multi-user = fork-per-person** (DECISIONS #25, docs/FORKING.md). Owner
  gates exist in three layers: workflow condition, Python handler, Vercel
  backend. The Mac companion is fork-portable too: `JOBRADAR_REPO=<you>/<repo>`
  on install.sh; run.sh derives the branch from the clone.
- **`CV/` is local-only and gitignored** (DECISIONS #29) — never commit it
  or anything derived from it; CV auto-tailoring is a Mac-companion feature.
- **Next up** (see ROADMAP.md): SPA-host posting text via ATS JSON APIs so
  the quality pass covers Workday/Eightfold/Oracle; the jobright `"↳"`
  company-name repair; email autopilot secrets; CV auto-tailor once
  `CV/cv_full.tex` is fleshed out.

## Safe handoff practice

At the end of any material change, state:

1. What changed and why.
2. Files changed.
3. Validation run and its result (or why it could not run).
4. Any configuration/secrets or GitHub-side action still required.

Preserve unrelated working-tree changes. Do not assume a secret exists merely
because the code references it.

## Platform frontend/back end (added 2026-07-11)

- `webapp/index.html` is the canonical platform page; `docs/platform/index.html`
  must stay a byte-for-byte copy (`cp webapp/index.html docs/platform/index.html`)
  — Pages serves the copy, Vercel serves webapp/ plus its `api/` functions.
- Never put credentials in the frontend or repo. Auth = GitHub OAuth via the
  Vercel backend (owner-only), or the tokenless prefilled-issue flow on Pages.
