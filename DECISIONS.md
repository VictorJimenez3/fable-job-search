# Decision log

Decisions made autonomously during the build, with reasoning. (You asked for
the ambitious-but-real version and said to make judgment calls — here they are.)

## 1. Deployment: GitHub Actions cron, state committed to the repo
Considered: Supabase + edge functions, Vercel cron, a VPS, Claude Code
scheduled sessions. Chose **Actions** because it's free on this repo, has
unrestricted egress (the ATS APIs need it), requires zero new accounts or
credentials, and the repo itself becomes the database (auditable, versioned,
portable). `*/30` cron is the practical floor of GitHub's scheduler — combined
with direct ATS polling this gives minutes-to-~1h detection latency, well
inside the 24h window that matters.

## 2. Discovery: aggregators for breadth, ATS-token harvesting for growth
"Search the entire internet" decomposed into two real mechanisms:
- **Breadth**: the community aggregators (SimplifyJobs, jobright-ai,
  speedyapply, vanshb03) already crawl thousands of career pages hourly.
  Re-crawling those pages myself would be slower and strictly worse — build on
  top, don't reinvent.
- **Growth**: every job URL from any source is mined for ATS board tokens
  (Greenhouse/Lever/Ashby/Workday/SmartRecruiters/Recruitee). Each new token is
  probed and permanently joins the polling registry. So the aggregators keep
  *introducing* companies, and from then on we watch those companies directly —
  faster than the aggregator that introduced them, and covering ALL their
  roles, not just the one posting. First run harvested 200 companies on top of
  the 85 curated seeds; this compounds every run.
- HN "Who is hiring" covers startups that never touch job boards.

## 3. Speed: direct ATS polling is the fast path
Aggregators lag hours-to-days. The ATS boards' public JSON APIs update the
moment a recruiter publishes. Polling ~hundreds of boards every 30 min is
cheap (one GET each) and legal (documented public APIs). Alerts flow to GitHub
issue → mobile push, plus RSS for feed readers.

## 4. Ranking: gates → auditable rubric → learned taste → optional LLM
- Hard gates: senior/staff/intern/PhD-postdoc/clearance/3+ yrs/non-US.
- Rubric (all weights in profile.yaml): role bucket (AI/ML 26 > DS 22 > SWE 20
  > DataEng 18) + sector (healthtech 16 > ai_lab 11 > big_tech 10 > edtech 8 >
  fintech 5) + freshness + explicit new-grad language. Every point is recorded
  in `score_reasons` so a ranking is never a black box.
- Because your stated preferences are vague, the system **learns**: your real
  Notion application history (Amazon, NVIDIA, Microsoft, Google, OpenAI,
  Commure, IXL, Capital One, …) seeds company boosts, and every checkbox you
  tick / `skip` you comment updates the weights. Your taste sharpens the model
  over time without you configuring anything.
- Claude re-rank is optional (needs an API key secret) and only touches
  borderline jobs; the system is fully functional without it. Rationale: keep
  the required-credential surface at zero.

## 5. Notion: write-only integration on your existing tracker
Your Applications DB schema was read live and the payload matches it exactly
(including the `Machine Learning Enginner` option spelling in Position).
Logging is trigger-based (checkbox on the alert issue) rather than scraping
confirmation emails — deterministic, zero false positives, one tap on mobile.
**Missing credential**: a Notion internal-integration token can only be created
by you (2-minute setup in README). Until then, applied entries queue in
`state/applied.json` and backfill automatically once the secret exists —
nothing is lost.

## 6. Resume reframing: kept human, augmented
Full auto-rewriting per role was deliberately **not** built: hallucinated
experience is disqualifying, ATS parsers mangle over-optimized resumes, and
reviewers pattern-match generic LLM output. Instead, with an API key present,
each alert carries a one-line *angle* ("emphasize your clinical-data pipeline
project") — the high-leverage 20% of tailoring while keeping you as the author.
The alert also shows the posting's sector/keywords so gaps are visible at a
glance.

## 7. What was evaluated and NOT built on
- **jobright.ai / Simplify products**: good discovery UIs but closed APIs; we
  consume their public GitHub artifacts instead.
- **hiring.cafe**: excellent aggregator, no official API; scraping their
  private API is fragile + ToS-gray. Skipped; the ATS-direct layer covers the
  same ground.
- **Auto-apply tools (SpeedyApply, Simplify extension)**: complementary to this
  system (use one to fill forms faster if you like); building auto-*submission*
  was out of scope by your framing (you apply manually).
- **LinkedIn/Indeed scraping**: aggressively bot-blocked, ToS-hostile,
  ban-risk on your accounts. Not worth it.

## 8. Big tech custom ATSs
Google/Amazon/Meta/Apple/Microsoft/Netflix run bespoke career sites without
public APIs. They're covered via the aggregators (which specialize in exactly
these companies) rather than direct polling. Workday-based big cos (NVIDIA,
Salesforce, Adobe, …) ARE polled directly.

## 9. Known limitations (deliberate trade-offs)
- Greenhouse fetches skip full descriptions (content=false) to keep runs fast;
  gates that need description text (years-required, clearance) still work for
  Lever/HN and via aggregator categorization. Consequence: some direct-ATS
  postings show as "seniority unclear" and land on the dashboard instead of
  alerts. Erring toward fewer false alerts was the intent.
- Workday tenants are searched with 3 new-grad-ish queries × 3 pages rather
  than exhaustively (some tenants host 10k+ postings).
- GitHub cron jitter means worst-case ~1h detection. Acceptable vs 24h target.
- First CI run bootstraps the whole state; its alert issue is capped at the
  top 25 to avoid a notification bomb.

## 10. Checkbox = shortlist, not applied; email confirmation is ground truth
Originally the alert-issue checkbox directly logged "Applied" to Notion.
That's wrong on reflection: ticking a box only records intent, and the user
correctly pointed out they'd checked boxes to save jobs for later, not
because they'd submitted anything. Fixed by splitting the concepts:
- Checkbox → `state/shortlist.json` ("I'm interested"), small ranking boost,
  **no Notion write**.
- A confirmation email landing in the inbox → `email_watch.py` matches the
  sender/subject against the shortlist (or, failing that, anything the radar
  has ever seen) and *that* promotes the entry to `state/applied.json` +
  Notion. This is strictly more truthful: the company itself is the one
  asserting an application exists.
- `applied <url>` comment command still exists as an explicit, immediate
  override for cases email detection can't catch (jobs found outside the
  radar entirely, unusual confirmation wording).
- One-time migration (`migrate-checkbox-applied`): the 11 entries logged
  under the old (wrong) semantics were moved back to the shortlist and their
  Notion pages archived (soft-deleted, recoverable from Notion's trash) via
  the GitHub Actions bot's own `NOTION_TOKEN` — not via the interactive
  Claude↔Notion connector, which is a separate, session-scoped credential
  that isn't available to unattended CI runs.

## 11. Email monitoring needs its own credential, same pattern as Notion
The Gmail/Notion connectors available inside an interactive Claude Code
session are OAuth grants tied to *that session* — they don't exist for the
unattended GitHub Actions runner, which needs its own way in. Two realistic
options: Gmail API OAuth (requires the user to run a one-time consent flow
and mint a refresh token — multi-step, needs a Google Cloud project) vs. IMAP
with an App Password (2 minutes: enable 2-Step Verification, generate a
16-character app password, done). Chose IMAP for setup simplicity, gated on
confirming NJIT actually runs Google Workspace/Gmail (it does, per
ist.njit.edu) rather than Microsoft 365 — if it were Microsoft, IMAP basic
auth wouldn't work at all (Microsoft deprecated it in 2022) and OAuth would
be the only option. Real residual risk: some Workspace-for-Education admins
disable app passwords entirely as a policy matter; `email-verify` surfaces
that immediately with a specific error rather than a silent hang, and the
README documents a forwarding-to-personal-Gmail fallback.

Company matching from an email is inherently fuzzy (sender display name,
domain, or subject-line parsing, scored by normalized token overlap against
known company names) — deliberately biased toward the shortlist first (the
user already flagged interest in that exact posting) before falling back to
anything else the radar has ever seen, and returns no-match rather than a
low-confidence guess when nothing clears a 50% token-overlap bar.

## 12. Local LLM: the Mac is an enrichment worker, not a server
A laptop can't serve GitHub Actions (asleep, NAT'd), and Actions can't wait on
it. So the architecture is two-tier: Actions stays the always-on heuristic
layer; a launchd agent on the M1 Max runs `radar enrich` every 2h whenever the
machine is on — pull repo, run Ollama locally, push enriched state back. The
cloud never blocks on the Mac; the Mac upgrades whatever it finds when awake.
Provider abstraction (radar/llm.py) means the same code accepts an Anthropic
key, Ollama, or a free Gemini key via any OpenAI-compatible endpoint. Model
default `qwen3:14b` (fast, JSON-disciplined on M1 Max); `qwen3:32b` documented
as the quality upgrade 64GB handles.

## 13. LinkedIn: search the public web about LinkedIn, never scrape LinkedIn
Logged-in scraping risks the user's own account (bans are common and sticky)
and breaches ToS. Instead, Google Programmable Search (free tier) surfaces
public `linkedin.com/posts` hiring posts into the Monday memo as *leads*, not
scored jobs. 80% of the value, none of the account risk.

## 14. Culture data honesty
Culture claims are the easiest place to hallucinate confidently. Rules: the
~40 core dossiers are human-curated (source: `seed`); anything LLM-generated
is permanently labeled `est.` and never silently mixed with curated rows; the
fit score is a deterministic, printed rubric (prestige 25 / wlb 25 / pace 20 /
shutdowns 10 / comp 20, burnout penalty −15 when wlb ≤ 2) rather than LLM
vibes, so a ranking can always be audited. The burnout penalty exists because
"avoid toxic/high-burnout" is a stated *guardrail*, not a preference — Meta
prestige must not be able to buy back a 2/5 WLB.

## 15. Big-co bespoke endpoints: expect drift, design for it
Amazon/Netflix(Eightfold)/Merck(Phenom) verified live on first CI run;
Apple/Google/Microsoft/Tesla/J&J failed initially (WAF/UA/CSRF quirks) —
fixes: browser UA for bespoke endpoints, Apple CSRF handshake, alternate
Phenom hosts. Invalid entries now auto-retry up to 3 probes so fixes take
effect without manual state surgery. Whatever still fails stays a visible
`invalid` in the registry, not a silent gap — and aggregators still cover
those companies. First run with the new harvest patterns grew the registry
457 → 704 companies (Goldman, Amex, Ford, TI, JPMC, plus a wave of hospital
systems via Oracle/iCIMS).
