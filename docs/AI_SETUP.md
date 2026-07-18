# Optional AI setup

The radar does not need AI for discovery, ChemE scoring, sponsorship parsing,
experience parsing, alerts, or the platform. AI adds a second-pass internship
verdict, employer dossiers, application angles, and a weekly employer scout.

## Important account distinction

A ChatGPT Pro subscription and OpenAI API usage are separate products. A
ChatGPT login/API-looking token should not be placed in this repo. To use the
OpenAI API, create a Platform project key and enable API billing separately,
then use the OpenAI-compatible configuration below. Keep all keys in GitHub
Actions/Vercel secrets or a local environment, never committed files.

## Provider choices

Provider resolution in `radar/llm.py` is:

1. `ANTHROPIC_API_KEY` → Anthropic using the profile model;
2. `LLM_BASE_URL` → an OpenAI-compatible endpoint using `LLM_API_KEY` and
   `LLM_MODEL`;
3. no provider → deterministic features only.

Examples:

```text
# OpenAI API
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=<platform project key>
LLM_MODEL=<model available to that project>

# Local Ollama
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen3:30b
```

Model availability and pricing change, so choose a currently available model
in the provider dashboard rather than treating a model name in this document
as permanent.

## Cloud setup

Add `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` under GitHub **Settings →
Secrets and variables → Actions**. The nightly `enrich` workflow already
wires them. Alternatively, add only `ANTHROPIC_API_KEY`.

Then run **Actions → enrich → Run workflow** on the active ChemE branch.
The log should identify a provider and report dossier/quality counts rather
than `no LLM provider configured`.

## Free local setup

The Mac companion uses Ollama and runs enrichment every two hours while the
machine is awake:

```bash
JOBRADAR_BRANCH=claude/cheme-intern-radar \
  bash scripts/mac-companion/install.sh
```

It requires authenticated Git push access because enrichment updates state.
Useful checks:

```bash
launchctl list | grep jobradar
tail -20 ~/.jobradar/logs/enrich.log
ollama ps
```

Local and cloud providers may coexist. Verdicts are cached by job-description
hash so unchanged content is not repeatedly charged.

## What to validate

- A clearly relevant Chemical Engineering internship receives a relevant role
  family, not a software/new-grad verdict.
- A non-internship or unrelated-discipline role is demoted, not deleted.
- Sponsorship remains based on posting evidence; the LLM must not convert
  `unknown` to `yes` without text evidence.
- Generated employer dossiers say `est.` and report internship pay rather than
  annual new-grad compensation.
