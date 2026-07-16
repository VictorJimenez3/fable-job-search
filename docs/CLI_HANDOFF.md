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
- **Scoring (rules v2, 2026-07-16, DECISIONS #31-32):** hard gates (now incl.
  numeric levels: Engineer 3+/L5+/Level 3+/"Leader") + alert-eligibility
  paths: aggregator listing, explicit new-grad wording, marquee
  (`marquee_companies` incl. WHOOP/Oura/Dexcom/Abbott), $150k+ `pay_bank`,
  or `priority_sectors` (healthtech + strong engineering title). Then
  demotions that outrank ALL of those: `OFF_FIELD_RE` (safeguards/policy/
  sales/PM/support/...) and `MIDLEVEL_RE` (II/L4/Engineer 2) → dashboard
  only. LLM quality verdicts may suppress marquee alerts now. `regate()`
  runs at the top of every crawl and re-applies rule bumps
  (`score.RULES_VERSION`, records carry `rules_v` + `explicit_new_grad`)
  to stored jobs; manual commands: `python -m radar.main regate` /
  `repair-feedback`. The taste model filters `FEEDBACK_STOPWORDS`. The
  marquee list is duplicated in `webapp/index.html` (`S.marquee`) — keep
  both copies in sync.
- **Local AI:** Mac companion installed (`~/.jobradar`, launchd
  `com.jobradar.enrich`, every 2 h while awake) running Ollama `qwen3:30b`
  with `format:"json"` forced (DECISIONS #20 — thinking models otherwise
  burn the token budget). Each cycle: culture dossiers, re-score recent
  jobs, weekly LLM company scout, the **quality pass**
  (`radar/quality.py`, DECISIONS #30): link liveness + LLM new-grad/role-fit
  verification, ~15 jobs/cycle, aggregator links first, verdicts cached on
  the job record and re-applied after every re-score — and **pasted-JD
  grading** (DECISIONS #33): JDs pasted into the platform's Role-fit tab
  land in `state/web_state.json` and get a verdict next cycle. Knobs:
  `RADAR_QUALITY_LIMIT`, `RADAR_QUALITY_DISABLE`, `RADAR_PASTED_LIMIT`.
- **Free cloud LLM fallback (when the Mac sleeps):** `enrich.yml` (nightly)
  already wires `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` secrets, and
  `llm.complete()` retries 429/500/503 with Retry-After, so free
  rate-limited tiers are safe. Two known-good configs:
  NVIDIA NIM — `LLM_BASE_URL=https://integrate.api.nvidia.com/v1`,
  `LLM_API_KEY=nvapi-…`, `LLM_MODEL=` any hosted instruct model (e.g.
  `meta/llama-3.3-70b-instruct`); Google AI Studio —
  `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`,
  `LLM_API_KEY=<AI Studio key>`, `LLM_MODEL=gemini-2.5-flash`. **Needs a
  human:** only Victor can create a key and add the three repo secrets;
  optionally bump enrich.yml's cron from nightly to every 6 h after that.
- **Multi-user = fork-per-person** (DECISIONS #25, docs/FORKING.md). Owner
  gates exist in three layers: workflow condition, Python handler, Vercel
  backend. The Mac companion is fork-portable too: `JOBRADAR_REPO=<you>/<repo>`
  on install.sh; run.sh derives the branch from the clone.
- **`CV/` is local-only and gitignored** (DECISIONS #29) — never commit it
  or anything derived from it; CV auto-tailoring is a Mac-companion feature.
- **Next up — doable from any CLI/cloud session** (see ROADMAP.md):
  run `python -m radar.main repair-feedback` once (cosmetic — the stopwords
  already neutralize stale boosts at read time; do it in a checkout where
  CI isn't racing you, i.e. right after a merge). Watch the first Mac/CI
  enrich cycle after 2026-07-16 for the SPA-host quality pass
  (`fetch_posting_spa` — built where ATS egress was blocked, so its first
  live contact is that cycle; failures degrade to "unclear").
- **Next up — needs Victor / the Mac:** email autopilot secrets
  (`EMAIL_ADDRESS`/`EMAIL_APP_PASSWORD`); free-LLM fallback secrets
  (`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`, see above); CV auto-tailoring
  is **ON HOLD** (Victor's call, 2026-07-16) until `CV/cv_full.tex` is
  fleshed out — Mac-only when it resumes (DECISIONS #29).

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
