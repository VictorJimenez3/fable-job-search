# Job Radar — user guide

This is the manual for *using* the radar day to day. For how it's built, read
[README.md](../README.md); for why it's built that way, [DECISIONS.md](../DECISIONS.md).

## What it is, in one paragraph

Every ~30 minutes, GitHub Actions crawls ~700 company job boards and the big
new-grad aggregators, scores each posting against your preferences
(profile.yaml), and delivers anything high-scoring as a checkbox line on a
weekly GitHub issue that pushes to your phone. Checking a box puts the job in
your Notion Applications tracker instantly (status "Not started"); you flip it
to Applied in Notion when you actually apply. Your MacBook, when awake, runs a
local AI every 2 hours to enrich what the cloud found. No servers, no fees —
the repo itself is the database.

## The four places you look

| Where | What you see | When to look |
|---|---|---|
| **"📌 Master board"** issue ([Issues tab](https://github.com/VictorJimenez3/fable-job-search/issues)) | Every open alert-worthy role in ONE place, best first — no bouncing between issues. Extra pages are in its comments; checkboxes work everywhere, and already-tracked jobs show pre-checked | When you sit down to browse/check jobs |
| GitHub issue **"🎯 Job Radar alerts — week N"** | New high-scoring roles as they're found | When your phone buzzes (GitHub app push / email) |
| **"🏆 Best of \<date\>"** issue | The day's top 10, posted each evening — GitHub emails it to you | Evening email |
| **Notion "2026 Applications"** | Every job you checked, plus your real pipeline | When applying / updating statuses |
| [docs/DASHBOARD.md](DASHBOARD.md) | Everything decent the radar has seen, ranked — not just alert-worthy | Browsing for more options |
| [docs/feed.xml](feed.xml) | Same alerts as RSS | Only if you use a feed reader |

Plus a **Monday strategy memo** (its own GitHub issue): pipeline stats,
follow-up nudges for week-old applications, and LinkedIn hiring-post leads.

## The core loop

1. Phone buzzes → open the week's alert issue.
2. A line looks like:
   > ☐ 🔥 **Tempus** — [ML Engineer, New Grad](…) · Chicago, IL · `88` · **health technology** — precision-medicine data platform
3. Interested? **Tap the checkbox.** Within a minute a GitHub Action fires and
   the job appears in your Notion Applications database with status
   **Not started**. (It also teaches the ranker you like companies like this.)
4. When you actually apply, open the entry in Notion and **change its status
   yourself** (Applied, etc.). The radar never guesses whether you applied.
5. Not interested in a company? Comment `skip Acme Corp` on the issue —
   similar roles get downranked.
6. Marquee employers (MANGA, big AI labs, elite pharma/medtech — the
   `marquee_companies` list in profile.yaml) and $150k+ postings always
   alert; add or remove names in that list anytime. A twice-daily sweep
   re-reads every issue so no checked box is ever missed, and once a week
   the local AI scouts new healthcare/wearables employers to track.

## Comment commands (on any radar issue)

| Command | Effect |
|---|---|
| `applied <url>` | Log an application immediately as Applied — works for jobs the radar never saw. If you'd checkbox-saved it, the same Notion page is updated, not duplicated. |
| `skip <company or job id>` | Downrank this company's roles in future scoring |
| `culture <company>` | Get a reply with the company's culture dossier (WLB, pace, prestige, fit score) |
| `track <ats> <token> [Name]` | Force a company into the crawl registry, e.g. `track greenhouse stripe Stripe` |

## The AI layer (what's "smart" and what isn't)

- **Scoring is deterministic, not AI.** Gates (no senior/intern/clearance/3+yrs
  roles, US-only) then an auditable point rubric — every score has printed
  reasons. This runs in the cloud with zero API keys and is always on.
- **Your MacBook is the AI worker.** A background job (launchd, every 2 hours
  while the laptop is awake) pulls the latest state, runs **qwen3:30b through
  Ollama locally** (free, private, ~19 GB on disk), and pushes back:
  - a one-line *angle* per alert ("emphasize your clinical-data project"),
  - culture dossiers for unknown companies (always labeled `est.`),
  - re-ranking of borderline jobs.
  The cloud never waits on the Mac; enrichment upgrades alerts when it lands.
  Model memory is released after each run (`keep_alive: 0` + `ollama stop`).
- **Optional:** an `ANTHROPIC_API_KEY` repo secret would let the cloud do LLM
  re-ranking itself; not required.
- **Shelved:** Gmail confirmation detection (auto-flip to Applied) — needs an
  email App Password secret; see README §Setup.

## Running things manually

**In the cloud (github.com → Actions tab → pick a workflow → "Run workflow"):**

| Workflow | What it does |
|---|---|
| `radar` | Full crawl + alert cycle now (also runs on its own every ~30 min) |
| `notion-verify` | Read-only check that the Notion connection works |
| `promote-shortlist-applications` | One-time: move the ~47 boxes you checked under the old semantics into Notion as "Not started" |
| `strategist` | Build the Monday memo now |
| `tests` | Run the test suite |

**On the Mac (from the repo, for development):**

```bash
.venv/bin/python -m pytest tests/          # run the test suite
tail -f ~/.jobradar/logs/enrich.log        # watch the AI companion work
launchctl kickstart -k gui/$(id -u)/com.jobradar.enrich   # force an enrichment cycle now
```

## 5-minute demo

1. **Alerts:** open the [Issues tab](https://github.com/VictorJimenez3/fable-job-search/issues)
   → open this week's "🎯 Job Radar alerts" issue. Read a few lines — note the
   score, salary, and the bolded industry + company description.
2. **Track:** tick one checkbox on a job you might actually want.
3. **Notion:** open your 2026 Applications database → the job is there within
   ~1 minute, status "Not started", with position, link, and location filled.
4. **Feedback:** comment `skip <some company you dislike>` on the issue.
5. **Culture:** comment `culture Anduril` and wait for the bot's reply.
6. **Dashboard:** open [docs/DASHBOARD.md](DASHBOARD.md) for the long tail.
7. **AI (Mac):** `tail -f ~/.jobradar/logs/enrich.log` while forcing a cycle
   (command above) — watch it annotate jobs with the local model.
