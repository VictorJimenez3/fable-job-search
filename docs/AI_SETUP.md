# AI setup and operating policy

The cloud AI layer is configured. It is optional: crawling, gates, scoring,
posting facts, alerts, and tracking all keep working when every model is down.

## Live model registry

Repository **secrets**:

- `NVIDIA_GLM_52_API_KEY`
- `NVIDIA_DEEPSEEK_V4_PRO_API_KEY`
- `NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_API_KEY`
- `NVIDIA_KIMI_K2_6_API_KEY`

Repository **variables**:

- `NVIDIA_API_BASE_URL=https://integrate.api.nvidia.com/v1`
- `NVIDIA_GLM_52_MODEL=z-ai/glm-5.2`
- `NVIDIA_DEEPSEEK_V4_PRO_MODEL=deepseek-ai/deepseek-v4-pro`
- `NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_MODEL=nvidia/nemotron-3-ultra-550b-a55b`
- `NVIDIA_KIMI_K2_6_MODEL=moonshotai/kimi-k2.6`

The model strings and base URL are variables because they are not credentials;
keys are secrets. A ChatGPT Pro subscription does not include OpenAI API
credits, and this radar does not need an OpenAI key.

## Routing and limits

`radar/llm.py` makes one logical request and tries at most two healthy
providers. It retries a transient response once, honors `Retry-After` (capped),
then falls through. 401/403/404, rate limits, empty answers, and invalid schemas
open task-local cooldowns instead of being hammered repeatedly.

| Task | Preferred order | Default output cap |
|---|---|---|
| Job quality / pasted JD | GLM → DeepSeek → Nemotron → Kimi | 240 tokens |
| Grounded company research | Nemotron → GLM → DeepSeek → Kimi | 900 tokens |
| Batch re-rank | GLM → DeepSeek → Nemotron → Kimi | 1,200 tokens |
| Scout | Nemotron → GLM → DeepSeek → Kimi | 600 tokens |
| Strategy note | Nemotron → GLM → DeepSeek → Kimi | 300 tokens |

Kimi is deliberately last because its key now authenticates but the NIM model
endpoint has returned intermittent `404 model unavailable`. It stays configured
as a canary/fallback rather than consuming every fourth request.

Cloud budgets (per enrichment run):

- main enrich: 12 logical calls, 18 provider requests, up to 10 quality jobs,
  2 company briefs (every two hours);
- ChemE nightly enrich: 8 logical calls, 12 provider requests, 3 quality jobs,
  3 company briefs;
- explicit pasted JDs and tracked roles are processed ahead of cold backlog;
- the four keys are not exposed to the 30-minute crawl, so usage cannot scale
  with the number of postings found.

The main cloud enrichment workflow runs every two hours, not just nightly. It
still hard-stops at the limits above, and rotates the first-choice NVIDIA
provider by two-hour slot so one free key does not carry the entire queue. A
provider failure, rate limit, or cooldown skips to the next configured key.
NVIDIA's remaining account allowance is not exposed to the repository; the
NVIDIA Build dashboard is the authority for remaining quota. `state/ai_usage.json`
records actual requests and failures without storing prompts or secrets.

### Measured NVIDIA quota behavior (2026-07-18)

The NVIDIA account UI reported **“Up to 40 RPM.”** That is the only explicit
quota number available to this repository; it does not say whether the number
is per account, per API key, per model, or a burst/concurrency ceiling. We ran
one controlled burst of 40 requests (10 verification runs × 4 models), then
waited 70 seconds before probing again:

- GLM: mostly successful, with one HTTP 429 during the burst;
- Nemotron: remained successful throughout;
- DeepSeek: intermittent timeouts, including after the reset;
- Kimi: repeatable HTTP 404 (`model unavailable`), not a rate-limit response.

This is not enough to claim “40 requests per model.” It does show that model
behavior differs under the same burst, so the router treats 40 RPM as an
unknown provider/account ceiling and rotates providers conservatively. Do not
run another quota stress test during normal operation. A 429 opens cooldown;
fallback requests still count against the hard per-run budget.

Override knobs are `RADAR_AI_MAX_CALLS`, `RADAR_AI_MAX_REQUESTS`,
`RADAR_AI_PROVIDER_ATTEMPTS`, `RADAR_AI_TASK_<TASK>_LIMIT`,
`RADAR_QUALITY_LIMIT`, and `RADAR_COMPANY_RESEARCH_LIMIT`.

## Verify and observe

Run **Actions → nvidia-verify → Run workflow**. It tests all four models even
when one fails and writes a matrix to the workflow summary. Authentication or
configuration failures fail the workflow; 404/429/5xx are reported as endpoint
availability warnings as long as another model is healthy.

Run **enrich** for the main board or **cheme-enrich** for ChemE. The platform's
AI tab reads `state/ai_usage.json` and shows task counts, model successes/errors,
reported tokens, and the hard budget. Telemetry never stores prompts or keys.

## Grounding rules

- Model memory is not a source. Company briefs use only source blocks captured
  from official postings and every displayed factual claim cites a source ID.
- Unsupported claims become **Not confirmed**. Posting marketing copy cannot
  prove WLB, compensation, size, or sponsorship unless it states the fact.
- Legacy `source: est.` Culture Compass entries remain visibly labeled but do
  not move ranking. Only the human-curated seed can affect culture score.
- AI can demote/annotate through audited reasons; deterministic gates remain
  authoritative and every caller has a heuristic fallback.

## Local Ollama lane

The Mac companion still runs Ollama every two hours while awake. A local
OpenAI-compatible URL is preferred automatically when present, and Ollama's
native endpoint unloads the model after each call. Company-research and AI
usage caches are included in the companion's push-race merge.

For Victor's always-on Mac setup, keep the Ollama API service available at
`127.0.0.1:11434`; the `com.jobradar.enrich` launchd agent is loaded and will
restart/continue enrichment while the laptop is awake. The service may stay
running while the `qwen3:30b` model is unloaded between requests to preserve
memory. Local enrichment may run in parallel with cloud enrichment, but do not
start two local companion cycles against the same clone at once—their state
commits must remain serialized.

```bash
launchctl list | grep jobradar
tail -30 ~/.jobradar/logs/enrich.log
ollama ps                 # normally empty between calls
```

CV-aware work and semantic/vector/RAG work remain intentionally deferred.
