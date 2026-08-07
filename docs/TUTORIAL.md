# Job Radar — user guide

This is the manual for *using* the radar day to day. For how it's built, read
[README.md](../README.md); for why it's built that way, [DECISIONS.md](../DECISIONS.md).

## What it is, in one paragraph

Every ~30 minutes, GitHub Actions crawls ~700 company job boards and the big
new-grad aggregators, scores each posting against your preferences
(profile.yaml), and delivers each new high-scoring posting as its own assigned
GitHub issue that pushes to your phone. A separate unassigned master board keeps
everything together. Checking a box puts the job in the in-house Pipeline and
mirrors it to your selected tracker (Notion by default; Google Sheets optional).
On the Vercel platform, any GitHub-signed-in user can use the private
Google-backed tracker without touching the repository owner's pipeline. Your MacBook
handles local bulk AI when awake, while a tightly budgeted NVIDIA cloud pass
handles time-sensitive enrichment nightly. The repo itself is the database.

## 🖥️ The Platform (start here)

**https://victorjimenez3.github.io/fable-job-search/platform/** — the whole
system as a website, refreshed automatically by every crawl:

- **Jobs**: every role the radar has ever seen, with persistent dropdowns for
  role family, posting sponsorship, official DOL sponsor history, experience,
  sector, and pipeline status. Each row shows visa/years badges—including the
  important difference between "not stated" and "not analyzed"—plus historical
  sponsor context, score, salary, culture, and age. DOL history is company-level
  context only; the posting's own visa wording remains primary and no-history
  does not mean a company will not sponsor. The
  **apply ↗** button opens the employer posting; when you are signed in it also
  saves a new role to **To apply** without pretending you submitted it.
- **Pipeline**: separate Maybe, To apply, Applied, OA, Interview, Rejected, and
  Closed lanes. The selected tracker is read back twice daily; "Maybe" remains
  a platform-only scratch lane.
- **Per-job workspace** (click the title or details ▸), four tabs: **Fit &
  eligibility** opens first with role family, posting sponsorship, DOL sponsor
  history, required years, location, salary, posting age, score reasons, and the
  LLM verdict; it also
  has a paste box for descriptions the radar cannot scrape (graded on the next
  enrich cycle). **Company** has a claim-level cited employer brief, its
  official evidence, a separately labeled culture card, and external research
  links; **Outreach** has role-aware public Google searches for university and
  technical recruiters, likely hiring managers, NJIT alumni, and public hiring
  posts, plus message templates and saved conversations. These links never
  scrape LinkedIn; **Notes** holds your working notes
  (keep them non-sensitive because synced web state is public).
- **Companies**: open-role employers, prioritized by actionable role score,
  with sourced summaries, research freshness, culture signals, and coverage.
- **Interview**: OA/interview applications with cited company context and a
  role-prep checklist.
- **AI**: per-run budget, task mix, provider/model health, grounded-research
  coverage, scout status, and registry stats.

Reading needs nothing. On Vercel, use **Tutorial → Accounts & login** to sign
in with GitHub or Google, then connect the other provider from the same signed-in
session. There is no password login. On the Pages mirror, buttons use prefilled GitHub issues by default;
the optional Settings token enables instant writes and cross-device notes.
The private Google-backed tracker is filtered per linked account; the Sheet is
never sent to the browser wholesale. GitHub checkboxes keep working exactly as
before; both doors lead to the same tracker.

The separate [ChemE internship board](https://job-radar-cheme.vercel.app)
uses its own jobs, scoring profile, pipeline state, and labeled GitHub issues.
It shares this repository's Notion integration, so tracked ChemE roles enter
the same Applications database instead of creating a second Notion system.

## Private Resume Studio

Victor's CV workflow runs locally because `CV/` is personal and gitignored.
From the repository on the Mac, start:

```bash
.venv/bin/python scripts/resume_studio.py
```

Then open `http://127.0.0.1:4317/`. Search for a radar role and choose either:

- **Used bullets** — selects target-relevant source IDs from the
  master CV and example résumés and copies their text without rewriting.
- **AI tailor** — permits substantive source-grounded rewrites
  and synthesis from multiple authorized lines, then runs the same fixed
  review rubric.
- **Unrestricted AI tailor** — permits a more original role-specific argument
  across the authorized evidence bank while preserving factuality and scope
  qualifiers; it is intentionally marked for human review.

All three modes render through the exact `CV/resume.tex` structure. Models cannot
write the document, alter its margins/typography/spacing, enlarge text, or pass
a sparse or bloated evidence portfolio. Their existing local subscription sessions are
used; the radar does not receive or store their credentials. The report
includes the fixed score, target-specific omissions, and known Codex token
usage. The Studio header shows observed weekly local Codex calls/tokens; the
Plus weekly allowance is not exposed by the local CLI. Set
`CODEX_WEEKLY_LIMIT_TOKENS` only when you know the comparison limit.

Drafts, prompts, source context, PDFs, and review reports live only under
`CV/.resume_studio/`. Review the generated PDF and report before using any
application material. The system preserves the master CV as the evidence bank
and never auto-submits a resume. Use **Resume bank** in the header to revisit
any saved run or legacy experiment; selecting another posting does not remove
the previous result. New PDFs use a company-identifiable name such as
`mayo_clinic_resume_ai.pdf`, and the preview response preserves that name when
the file is opened or downloaded. Project headings use `|` separators. TICC is
never emitted by generation or workshop editing, even when it appears in a
local historical source; the source files themselves are not changed. Each new
run stores a private posting snapshot beside its artifacts.

For an AI-enhanced run, the report's **What changed** block lists rewritten
source lines and project swaps, while **ATS terms** shows exact supported terms
that made it into the rendered draft, supported terms still missing, and
requirements your evidence cannot support. Space QA labels unused width as
roomy lines instead of confusing it with whitespace or a wrap. If the posting
text was not captured, the report says so instead of pretending the draft was
keyword-tailored. A completed draft must also leave at least 12pt of right-edge
safety on every bullet; near-wraps are rejected and remain visible as failed
run diagnostics.

### Workshop editing

Open **Workshop** from a saved run in Resume bank. Education and Technical
Skills are available alongside experience, project, and leadership lines; the
contact header and LaTeX layout remain protected. The editor shows readable
résumé wording, while unchanged lines retain their existing emphasis. **Save
line** renders a new revision in a unique private folder, **Ask AI about this**
returns candidates without applying them, and **Revert** creates a new revision
from any earlier saved version. The original company-named PDF remains intact.

To create a temporary varied-role calibration set for labeling bullets, run:

```bash
.venv/bin/python scripts/resume_calibration.py --generate --count 8 --serve
```

The page at `http://127.0.0.1:4321/` displays the rendered PDFs, not the LaTeX
source. Labels and explanations are stored in the same ignored
`CV/.resume_studio/calibration/` batch as the generated cases.

Use the sort menu to switch among Radar score, newest posting, and private
Resume Match. The title-only match is deliberately labeled low-confidence;
after selecting a role, **Analyze full posting match** fetches the job page and
recomputes the fixed rubric when readable. The result shows capability
coverage, gaps, evidence confidence, and private source-backed score.

The generator does not force every role to use the same evidence. It creates a
strongest-first pool, then enforces a 22–26-bullet interview portfolio with
three experiences, four projects, and one or two leadership entries. Every
bullet must stay on one visual line, and the page must reach the same bottom
region as the immutable human reference. The adversarial final pass applies a
corrected source-addressed plan before the PDF is accepted; it cannot fix a
weak result by deleting its way to a sparse page.
If an enhancement drops a scope-limiting source fact such as `synthetic`,
`prototype`, or `POC`, that bullet reverts to approved source wording. Resume
Craft measures the argument and writing; factuality, eligibility, and layout
remain separate gates that cannot be averaged away.

## The places you look

| Where | What you see | When to look |
|---|---|---|
| **"📌 Master board"** issue ([Issues tab](https://github.com/VictorJimenez3/fable-job-search/issues)) | Every open alert-worthy role in ONE place, best first — no bouncing between issues. Extra pages are in its comments; checkboxes work everywhere, and already-tracked jobs show pre-checked | When you sit down to browse/check jobs |
| Individual **"🎯" alert issue** | One new high-scoring role | When your phone buzzes (GitHub app push / email) |
| **"📌 Master board"** issue | All open alert-worthy roles in one place; intentionally unassigned | When you want the full browseable list |
| **"🏆 Best of \<date\>"** issue | The day's top 10, posted each evening — GitHub emails it to you | Evening email |
| **Notion "2026 Applications"** | Every job you checked, plus your real pipeline | When applying / updating statuses |
| [docs/DASHBOARD.md](DASHBOARD.md) | Everything decent the radar has seen, ranked — not just alert-worthy | Browsing for more options |
| [docs/feed.xml](feed.xml) | Same alerts as RSS | Only if you use a feed reader |

Plus a **Monday strategy memo** (its own GitHub issue): pipeline stats,
follow-up nudges for week-old applications, and LinkedIn hiring-post leads.

## The core loop

1. Phone buzzes → open the individual alert issue.
2. A line looks like:
   > ☐ 🔥 **Tempus** — [ML Engineer, New Grad](…) · Chicago, IL · `88` · **health technology** — precision-medicine data platform
3. Interested? **Tap the checkbox.** Within a minute a GitHub Action fires and
   the job appears in your Notion Applications database with status
   **Not started**. (It also teaches the ranker you like companies like this.)
4. When you actually apply, open the entry in Notion and **change its status
   yourself** (Applied, etc.). The radar never guesses whether you applied.
   For a live posting you find outside the radar, open the platform's
   **Pipeline** tab and use **Add a role you found yourself**. It creates a
   saved To apply item in the in-house tracker and Notion, clearly marked as a
   manual entry; it does not generate an alert or claim new-grad eligibility.
5. Not interested in a company? Comment `skip Acme Corp` on the issue —
   similar roles get downranked.
6. New-grad evidence is the first gate. Among eligible roles, AI/ML and data
   science lead, then general SWE, then data engineering/systems; health,
   sports, video games, education, big tech, and AI labs receive strong field
   fit. Marquee employers add competitive context but never bypass new-grad or
   role fit. A twice-daily sweep re-reads every issue so no checked box is ever
   missed, and the local AI scouts new healthcare/wearables employers to track.

## Comment commands (on any radar issue)

| Command | Effect |
|---|---|
| `applied <url>` | Log an application immediately as Applied — works for jobs the radar never saw. If you'd checkbox-saved it, the same Notion page is updated, not duplicated. |
| `skip <company or job id>` | Downrank this company's roles in future scoring |
| `culture <company>` | Get a reply with the company's culture dossier (WLB, pace, prestige, fit score) |
| `track <ats> <token> [Name]` | Force a company into the crawl registry, e.g. `track greenhouse stripe Stripe` |

## The AI layer (what's "smart" and what isn't)

- **Scoring is deterministic, not AI.** Gates require verified new-grad or
  early-career evidence, reject required 1+ years, and exclude senior/intern/
  clearance/off-field roles (US-only). Technical graduate and rotational
  leadership programs are a dedicated exception. Eligible roles then use an
  auditable point rubric prioritizing AI/ML and data science above SWE/systems;
  every score has printed reasons. This runs in the cloud with zero API keys.
- **Your MacBook is the bulk AI worker for radar enrichment.** A background job (launchd, every 2 hours
  while the laptop is awake) pulls the latest state, runs **qwen3:30b through
  Ollama locally** (free, private, ~19 GB on disk), and pushes back:
  - a one-line *angle* per alert ("emphasize your clinical-data project"),
  - source-grounded company briefs when official evidence exists,
  - re-ranking of borderline jobs.
  The cloud never waits on the Mac; a nightly NVIDIA router also processes a
  small priority queue with hard call/request limits and cross-model fallback.
  Model memory is released after each run (`keep_alive: 0` + `ollama stop`).
- **Observable and bounded:** the AI tab shows task/model successes, errors,
  reported tokens, and the run cap. Prompts/keys are never stored.
- **Parked for future multi-user support:** Gmail confirmation detection
  (auto-flip to Applied). Victor's current owner workflow does not need the
  email App Password secret.

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
