# Run your own Job Radar (forking guide)

This system is single-owner by design: one repo = one person's radar, state,
and Notion. **Nothing a visitor does on someone else's radar can touch the
owner's data** — the backend and workflows obey only the repo owner. To get
your own, fork it. Your fork shares zero state, zero secrets, and zero Notion
access with the original.

## 1. Fork (2 min)

1. Fork this repo on GitHub (keep it **public** — that's what makes Pages and
   raw-state reads free).
2. In your fork: *Actions* tab → enable workflows. The `radar` crawl starts
   running every ~30 min; the first run bootstraps your own `state/`.

## 2. Make it yours (5 min)

- `profile.yaml` — your preferences: role/sector weights, thresholds,
  `marquee_companies` (your always-alert list), Notion stage names.
- `webapp/index.html` — edit the one-line `ME` constant (name/school/class);
  it fills your outreach templates and alumni-search links.
- `data/companies_seed.yaml` — optionally add companies you care about.
- `radar/main.py` `seed_cmd` contains the original owner's taste seeds —
  ignore or replace; your checkbox history builds your own taste model.

## 3. Notion (optional, ~2 min)

Your tracking only reaches **your** Notion if you connect it:
1. [notion.so/my-integrations](https://www.notion.so/my-integrations) → new
   internal integration → copy the secret.
2. Share your applications database (title must contain "application") with
   the integration (⋯ → Connections).
3. Fork → *Settings → Secrets and variables → Actions* → add `NOTION_TOKEN`.

Without it, tracked jobs queue in `state/applied.json` and sync whenever you
add the secret later. Check your Stage status options — set `stage_saved` in
profile.yaml to one that exists in your database.

## 4. Website (2 min)

Fork → *Settings → Pages* → Deploy from branch → your default branch,
`/docs` folder. Your platform appears at
`https://<you>.github.io/<repo>/platform/` and **self-configures from the
URL** — no code edits needed. Writes work tokenlessly (buttons open a
prefilled issue; the radar obeys you, the fork owner).

## 5. Optional: signed-in one-click writes (Vercel, ~10 min)

Deploy the `webapp/` directory to your own Vercel project, then set env vars:

| var | value |
|---|---|
| `GH_CLIENT_ID` / `GH_CLIENT_SECRET` | your own GitHub OAuth app (callback: `https://<your-app>/api/callback`) |
| `SESSION_SECRET` | any long random string |
| `RADAR_OWNER` | your GitHub username |
| `RADAR_REPO` | `<you>/<repo>` |
| `RADAR_BRANCH` | your default branch |
| `CANON_HOST` | your Vercel hostname |

Then "Sign in with GitHub" on your Vercel URL gives you instant writes; the
backend only obeys `RADAR_OWNER`.

## 6. Optional: free local AI (any Mac with ≥32GB, ~10 min)

The radar works without any AI — deterministic scoring, auditable reasons.
A local model adds culture dossiers, an LLM company scout, and the quality
pass (dead-link pruning + verifying postings are really internships). On your
Mac:

```bash
JOBRADAR_REPO="<you>/<repo>" bash -c \
  "$(curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/<branch>/scripts/mac-companion/install.sh)"
```

It installs Ollama + a local model (default `qwen3:30b`, ~19GB — override
with `JOBRADAR_MODEL=` for smaller machines, e.g. `qwen3:8b`) and a launchd
agent that enriches every 2 hours while the laptop is awake, pushing state
back to **your** fork. Requires `git push` auth on the machine
(`brew install gh && gh auth login`). The branch is auto-detected from your
fork's default branch. No Mac? Set an `ANTHROPIC_API_KEY` secret or point
`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` secrets at any OpenAI-compatible
endpoint (a free Google AI Studio key works) and the nightly `enrich`
workflow does the same in the cloud (it no-ops in seconds when no LLM is
configured).

## Isolation guarantees

- Secrets (`NOTION_TOKEN`, OAuth) live per-fork/per-Vercel-project; forks
  never see the original's.
- All state is files in your fork; the crawler runs in your fork's Actions.
- The original owner's platform treats you as read-only, and yours treats
  everyone but you as read-only — enforced in the workflow condition, the
  Python handler, and the Vercel backend.
