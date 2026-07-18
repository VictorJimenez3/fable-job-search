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

Cloud budgets:

- main nightly enrich: 12 logical calls, 18 provider requests, 6 quality jobs,
  3 company briefs;
- ChemE nightly enrich: 8 logical calls, 12 provider requests, 3 quality jobs,
  3 company briefs;
- explicit pasted JDs and tracked roles are processed ahead of cold backlog;
- the four keys are not exposed to the 30-minute crawl, so usage cannot scale
  with the number of postings found.

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

```bash
launchctl list | grep jobradar
tail -30 ~/.jobradar/logs/enrich.log
ollama ps                 # normally empty between calls
```

CV-aware work and semantic/vector/RAG work remain intentionally deferred.
