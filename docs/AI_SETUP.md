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

`radar/llm.py` makes one logical request and races every configured healthy
endpoint concurrently. The first valid answer wins; slower calls still finish
long enough to record latency, validity, and failure telemetry. It retries a
transient response once, honors `Retry-After` (capped), then applies task-local
cooldowns to 401/403/404, rate limits, empty answers, and invalid schemas.

| Task | Preferred order | Default output cap |
|---|---|---|
| Job quality / pasted JD | GLM → DeepSeek → Nemotron → Kimi | 240 tokens |
| Grounded company research | GLM → DeepSeek → Nemotron → Kimi (or local Ollama) | 2200 tokens |
| Batch re-rank | GLM → DeepSeek → Nemotron → Kimi | 1,200 tokens |
| Scout | Nemotron → GLM → DeepSeek → Kimi | 600 tokens |
| Strategy note | Nemotron → GLM → DeepSeek → Kimi | 300 tokens |

All four configured NVIDIA providers are now launched for each API request;
the table is the benchmark/tie-break order, not a serial fallback chain. Kimi
is still expected to lose until its intermittent `404 model unavailable` issue
is fixed.

Cloud budgets:

- main nightly enrich: 12 logical calls, 18 provider requests, 6 quality jobs,
  3 company briefs;
- ChemE nightly enrich: 8 logical calls, 12 provider requests, 3 quality jobs,
  3 company briefs;
- local two-lane runs default to 24 provider requests so all 12 logical calls
  can probe both Ollama and hosted/API when both are configured;
- explicit pasted JDs and tracked roles are processed ahead of cold backlog;
- the four keys are not exposed to the 30-minute crawl, so usage cannot scale
  with the number of postings found.

Override knobs are `RADAR_AI_MAX_CALLS`, `RADAR_AI_MAX_REQUESTS`,
`RADAR_AI_TASK_<TASK>_LIMIT`,
`RADAR_QUALITY_LIMIT`, and `RADAR_COMPANY_RESEARCH_LIMIT`.

## Verify and observe

Run **Actions → nvidia-verify → Run workflow**. It tests all four models even
when one fails and writes a matrix to the workflow summary. Authentication or
configuration failures fail the workflow; 404/429/5xx are reported as endpoint
availability warnings as long as another model is healthy.

Run **Actions → AI provider benchmark → Run workflow** to test all four models
concurrently on the real company-research and posting-quality schemas. The
latency/validity report is stored in `state/ai_benchmark.json` and the fastest
valid model per task is recorded as the measured winner.

Run **enrich** for the main board or **cheme-enrich** for ChemE. The platform's
AI tab reads `state/ai_usage.json` and shows task counts, model successes/errors,
reported tokens, and the hard budget. Each attempt records its lane (`local` or
`api`), endpoint, status, latency, and whether it was selected; per-run attempt
logs are retained in usage history. Telemetry never stores prompts or keys.

## Grounding rules

- Model memory is not a source. Before synthesis, the crawler captures bounded
  excerpts from public company/about, careers, benefits, culture, and monitored
  board pages. Every sourced claim cites a source ID; non-public PTO, WLB, pace,
  and pay are conservative values labeled `Estimated` rather than confirmed.
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
