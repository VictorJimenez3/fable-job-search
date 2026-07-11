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

## 10. Local AI: Ollama companion, on-demand model memory

The cloud crawl must remain useful when Victor's laptop is asleep, so it
continues to use deterministic ranking and queues enrichment naturally in the
committed state. The Mac companion pulls that state and performs optional LLM
enrichment every two hours while awake. Its default is `qwen3:30b`, a 19GB
mixture-of-experts model that fits comfortably on Victor's M1 Max with 64GB
unified memory. Calls use Ollama's native `keep_alive: 0` option and the
companion additionally issues `ollama stop` on exit, so model weights do not
remain resident between tasks. The lightweight local Ollama service may remain
available for the next on-demand request.

## 11. Superseded: checkbox = shortlist, not applied; email confirmation is ground truth
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

## 12. Email monitoring needs its own credential, same pattern as Notion
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

## 13. Checkbox = track in Notion now; Victor flips the status when he applies

Victor's actual workflow (2026-07-10, refining the reversal Codex made the
same day): checking an alert box must create the Notion entry *immediately*,
but with a not-yet-applied status — he then advances the status inside Notion
when he actually applies. So every tracked entry in `state/applied.json`
carries a `stage`: `saved` (checkbox) or `applied` (`applied <url>` comment,
or email detection once its credentials exist — currently shelved). An
applied signal for an already-saved job promotes the entry and patches its
existing Notion page's status rather than creating a duplicate. Notion status
options can't be created via API, so the saved status (`stage_saved`, default
"Not started") is validated against the live schema and omitted — letting the
database default apply — if it doesn't exist. The one-time
`promote-shortlist-applications` workflow moves the 47 existing checkbox
selections into Notion as saved entries.

## 14. Alerts explain the employer, not just the role

Sector scoring remains intentionally coarse because it expresses candidate
preference, but alert display is more specific. A small, auditable context map
labels known employers (such as defense & aerospace, health insurance, medical
devices, semiconductors, or financial services) and says what they do. Culture
Compass industry data fills further gaps; unknown companies are called
"general technology," never the unhelpful "other." This presentation metadata
does not affect ranking.

## 15. Local LLM: the Mac is an enrichment worker, not a server
A laptop can't serve GitHub Actions (asleep, NAT'd), and Actions can't wait on
it. So the architecture is two-tier: Actions stays the always-on heuristic
layer; a launchd agent on the M1 Max runs `radar enrich` every 2h whenever the
machine is on — pull repo, run Ollama locally, push enriched state back. The
cloud never blocks on the Mac; the Mac upgrades whatever it finds when awake.
Provider abstraction (radar/llm.py) means the same code accepts an Anthropic
key, Ollama, or a free Gemini key via any OpenAI-compatible endpoint. Model
default `qwen3:14b` (fast, JSON-disciplined on M1 Max); `qwen3:32b` documented
as the quality upgrade 64GB handles.

## 16. LinkedIn: search the public web about LinkedIn, never scrape LinkedIn
Logged-in scraping risks the user's own account (bans are common and sticky)
and breaches ToS. Instead, Google Programmable Search (free tier) surfaces
public `linkedin.com/posts` hiring posts into the Monday memo as *leads*, not
scored jobs. 80% of the value, none of the account risk.

## 17. Culture data honesty
Culture claims are the easiest place to hallucinate confidently. Rules: the
~40 core dossiers are human-curated (source: `seed`); anything LLM-generated
is permanently labeled `est.` and never silently mixed with curated rows; the
fit score is a deterministic, printed rubric (prestige 25 / wlb 25 / pace 20 /
shutdowns 10 / comp 20, burnout penalty −15 when wlb ≤ 2) rather than LLM
vibes, so a ranking can always be audited. The burnout penalty exists because
"avoid toxic/high-burnout" is a stated *guardrail*, not a preference — Meta
prestige must not be able to buy back a 2/5 WLB.

## 18. Big-co bespoke endpoints: expect drift, design for it
Amazon/Netflix(Eightfold)/Merck(Phenom) verified live on first CI run;
Apple/Google/Microsoft/Tesla/J&J failed initially (WAF/UA/CSRF quirks) —
fixes: browser UA for bespoke endpoints, Apple CSRF handshake, alternate
Phenom hosts. Invalid entries now auto-retry up to 3 probes so fixes take
effect without manual state surgery. Whatever still fails stays a visible
`invalid` in the registry, not a silent gap — and aggregators still cover
those companies. First run with the new harvest patterns grew the registry
457 → 704 companies (Goldman, Amex, Ford, TI, JPMC, plus a wave of hospital
systems via Oracle/iCIMS).

## 19. The Shams rule: blockbusters always get announced (2026-07-11)

Victor was (rightly) furious that 71 Anthropic jobs sat on the dashboard
unalerted: the precision-first gate required explicit new-grad wording, which
direct-ATS postings at elite companies rarely carry. His framing: like Shams
with NBA trades, a blockbuster is news regardless — role-players only when
they fit. So `marquee_companies` in profile.yaml (MANGA + big AI labs +
cracked pharma/medtech, user-editable) bypass the new-grad-evidence
requirement, as does any posting whose salary clears `thresholds.pay_bank`
($150k). Hard gates (senior/intern/PhD/clearance/3+yrs/non-US) and the score
threshold still apply to everyone. The one-time `marquee-backfill` workflow
alerts the strongest recent marquee jobs that the old gate held back.

## 20. Local thinking models must be forced into JSON mode

qwen3:30b (the current Ollama build is a thinking-capable MoE) spends the
entire token budget on reasoning prose — `think: false` is accepted but
ignored, and `/no_think` no longer works — so every dossier parse failed
silently ("generated 0 culture dossiers" while 330 companies lacked one).
Fix: `llm.complete(..., json_mode=True)` sets Ollama's `format: "json"`,
which constrains decoding to valid JSON regardless of the model's thinking
habits. Verified live on the M1 Max. Callers that parse JSON (culture
dossiers, rerank) use it; prose callers (strategist memo) don't.

## 21. One board, a daily best-of, a reconcile sweep, and an LLM scout (2026-07-11)

Four asks from Victor, one design thread — GitHub issues stay the only UI, so
no new credentials:
- **Master board**: weekly alert issues hit GitHub's ~64KB body cap and made
  him bounce between issues. One stable `radar-master` issue now holds every
  open alert-worthy role (≤30 days, best first), body + bot comments as
  pages, rewritten in place each crawl. Checkboxes use the same
  `<!--radar:ID-->` markers; already-tracked jobs render pre-checked;
  applied-sync now also listens to issue_comment *edited* events for ticks on
  the comment pages.
- **Daily best**: a `🏆 Best of <date>` issue (top 10 of the last 24h) posted
  each evening; GitHub's assignment notification is the daily email — zero
  mail credentials. Yesterday's daily issue is auto-closed.
- **Reconcile sweep**: event-driven checkbox sync can drop ticks (deploys,
  outages, the two semantics migrations). A twice-daily idempotent sweep
  parses every radar issue (bodies and comments) and tracks anything checked
  that isn't in applied.json/Notion. Nothing Victor checks is ever lost.
- **LLM scout**: aggregators miss random-but-great healthcare/wearables
  employers (WHOOP was the trigger; it and ~16 peers are now also seeded).
  Weekly, the Mac's local model proposes companies + ATS-token guesses,
  which enter the registry as `origin: scout` candidates for the normal live
  probe — wrong guesses die in the probe, so hallucination risk is contained.

## 22. The platform: a static single-file app, the repo stays the backend (2026-07-11)

Victor asked for "the dream system" — a website with every job ever seen,
pipeline lanes (maybe / to-apply / applied), a per-job workspace for
recruiter outreach and company research, and a second door into Notion
besides GitHub checkboxes. Architecture keeps DECISIONS #1 intact: no
servers. `docs/platform/index.html` is one self-contained page on GitHub
Pages; it reads the committed `state/*.json` from raw.githubusercontent
(public, no auth) so every crawl auto-refreshes the site. Writes use two
paths: track/applied buttons fire a `repository_dispatch` handled by the
`web-actions` workflow (the same `record_applied` path as checkboxes, so
Notion stays consistent), and workspace data (notes, outreach links,
maybe-lane) commits to `state/web_state.json` through the contents API with
a fine-grained PAT the user pastes once (localStorage; read-only without
it, with a localStorage fallback so nothing is lost). Trade-off noted in
the app: the repo is public, so workspace notes are public — flip the repo
private (losing free Pages) if that ever outweighs convenience.
