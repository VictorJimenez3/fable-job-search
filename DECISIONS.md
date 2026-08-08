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

## 23. Public repo ⇒ zero secrets in the frontend; GitHub login is the auth

Victor (correctly) refused to paste a token into a site backed by a public
repo. Two changes. First, the write path is now tokenless by default: the
platform's track/applied buttons open a prefilled GitHub issue
(`save <id>` / `applied <url>`) that he submits while logged into GitHub —
identity comes from GitHub itself, the applied-sync workflow processes it
and auto-closes the issue. Second, and independently overdue: the event
handler and workflow now obey **only the repo owner** (`github.actor ==
github.repository_owner` plus a sender check in code). Before this, any
GitHub account could comment `applied <url>` / `track <ats> <token>` on the
public repo and the radar would have obeyed — sharing the site URL is now
safe by construction; strangers get a read-only view. The optional PAT path
remains for instant one-click writes and cross-device notes (fine-grained,
single-repo, Contents-only, localStorage), with its blast radius and the
web_state.json-is-public trade-off documented in the app's Settings.
Checkbox toggling was already safe: GitHub requires write access to edit a
bot-authored issue body, and the reconcile sweep only trusts labeled issues
(strangers can't apply labels).

## 24. Automatic writes without frontend secrets: GitHub OAuth via a Vercel micro-backend

Victor wants one-click automatic writes but zero tokens in the frontend or
his browser's hands. Resolution: a six-function Vercel backend (webapp/)
serves the same platform page and adds "Sign in with GitHub" — the OAuth
code flow runs server-side, the user's GitHub token is sealed into an
httpOnly AES-256-GCM cookie (page JS can never read it), and the only
secrets (OAuth client id/secret + session key) live in Vercel's encrypted
env store, never in git. Writes go through /api/action → repository_dispatch
with the signed-in user's own token; the backend and the radar both enforce
owner-only, and non-owner sign-ins get a read-only view — GitHub would
refuse their tokens anyway (public_repo scope on a repo they can't push to).
The GitHub Pages copy of the same file self-detects the missing backend and
falls back to the tokenless prefilled-issue flow. webapp/index.html is the
canonical frontend; docs/platform/index.html is a straight copy — edit the
former, `cp` to the latter. Deploy is via the Claude↔Vercel connector once
Victor grants it project-create access; setup needs three env vars and a
GitHub OAuth app only he can create.

## 25. Multi-user = fork-per-person, never shared tenancy (2026-07-11)

Victor asked that other people's tracking never touch his Notion. It already
can't — his token lives only in his repo secrets/Vercel env, and every write
path (workflow condition, Python handler, Vercel backend) obeys only the repo
owner; visitors are read-only. So multi-user support is fork-per-person: a
fork gets its own Actions crawler, state, secrets, Pages site, and (only if
its owner connects one) its own Notion — zero shared state by construction.
Made the code fork-portable: the frontend derives owner/repo from the
<you>.github.io/<repo> URL (and default branch from the GitHub API), the
Vercel backend reads RADAR_OWNER/RADAR_REPO/RADAR_BRANCH/CANON_HOST env
overrides plus a new /api/config endpoint, personal outreach-template info
collapsed to a one-line ME constant, and docs/FORKING.md walks a friend
through the whole setup. Victor's live Vercel deployment predates this patch
(identical behavior for his instance; next webapp deploy syncs it).

The full signed-in flow was verified live this session: OAuth sign-in as
@VictorJimenez3 (GitHub's authorize form rejects synthetic clicks — automation
must submit the form with the authorize button's name/value), passkey
confirmation by Victor, then a one-click track from the platform → 202 →
repository_dispatch → web-actions workflow.

## 26. Email autopilot: high-precision patterns, forward-only, inbox is truth (2026-07-13)
The tracker now maintains its own Notion Stage from the inbox: the email
watcher classifies each application-lifecycle email (confirmation / OA /
interview / rejection) and patches the matching entry's stage, plus a
dead-application auto-close after N days of silence. Design guardrails:
- **Precision over recall.** Only unambiguous language transitions a stage
  (e.g. rejection requires "unfortunately", "move forward with other
  candidates", "regret to inform" — not the mere word "application"). A
  missed email costs nothing (the next run or a manual edit catches it); a
  false Rejected in Notion is expensive. Order matters: a post-interview
  rejection is a rejection, and OA is checked before the generic word
  "interview".
- **Forward-only.** STAGE_ORDER ranks stages; a transition only applies if it
  moves strictly forward, so a late "interview" email after a "rejected" is
  ignored. No regressions, ever.
- **Responses match against applied, not the shortlist.** An OA/interview/
  rejection is a reply to something already submitted, so it's matched only
  against `applied` entries (reusing the same token-overlap matcher, ≥0.5).
- **Notion stays authoritative-compatible.** Every target status is validated
  against the live Stage options before patching (the API can't create
  options); an unknown option is skipped with a log line, never a failed
  write. Response-bearing stages also stamp the existing "Response date"
  property. All verified against the live "2026 Applications" schema.
- **Auto-close is conservative.** Default 45 days (configurable), and only for
  entries still at "applied" with no recorded response — anything that got an
  OA/interview/rejection already has a real outcome and is never touched.

## 27. Two doors, both permanent: Pages and Vercel co-exist (2026-07-13)

The platform deliberately has two live URLs and both stay:

- **GitHub Pages** (`victorjimenez3.github.io/fable-job-search/platform/`) —
  free forever, zero infrastructure, works for every fork automatically the
  moment they enable Pages. Writes fall back to the tokenless
  prefilled-issue flow. This is the *universal* door and the fork default.
- **Vercel** (`job-radar-vmj-8946s-projects.vercel.app`) — same
  `webapp/index.html` plus the `api/` OAuth backend. Sign in with GitHub
  once → every click writes instantly with zero prompts. This is the
  *daily-driver* door for the owner ("log in, then seamless").

They are byte-identical frontends (`docs/platform/index.html` is a copy of
`webapp/index.html`); the page detects at runtime whether a backend exists
(`/api/me` → 401 = backend, 404 = Pages) and picks the best write path. So
there is nothing to keep consistent beyond the `cp`. README lists Vercel
first for daily use, Pages as the no-setup fallback. Killing either would
lose something real: killing Pages breaks forks-for-free; killing Vercel
breaks seamless writes.

## 28. Job list ordering: speed is a first-class axis (2026-07-13)

Every job now shows its posting age, and the Jobs tab has a "newest first"
sort next to "best match". `posted_at` (ATS/aggregator-reported, 99.7%
coverage) is preferred over `first_seen` (crawler discovery time) — the label
tooltip says which one you're seeing. Rationale: the whole system is built on
apply-within-24h; a sort that buries a 3-hour-old 70-score role under a
9-day-old 90-score role hides exactly the roles where speed still matters.

## 29. Personal documents never enter the public repo: `CV/` is local-only (2026-07-13)

Victor keeps his CV sources (`CV/*.tex`, PDFs, templates) inside the working
tree for the future auto-tailoring feature, but the repo is public — so
`CV/` is gitignored, permanently. Resume tailoring will therefore run where
the files are: on the Mac companion (local Ollama), writing draft picks into
a local review file. Nothing derived from the CV (bullets, tailored output,
even filenames) should ever be committed. If any CV-derived state must sync
across machines someday, it goes through Notion or a private gist — never
this repo.

## 30. Quality pass: the LLM adjusts, never deletes; the Shams rule outranks the LLM (2026-07-13)

`radar/quality.py` (runs in enrich, i.e. on the Mac's free local model)
revalidates the jobs Victor actually sees: HTTP liveness first (404/410 or
"no longer accepting applications" → `closed_at` + `alert_ok=False`, which
removes it from every surface at once), then one strict-JSON LLM verdict per
posting (`years_required` / `new_grad` / `role_family`). Guardrails:

- **Adjust, don't delete.** A verified not-new-grad or non-technical role
  gets a score penalty and loses alert eligibility, with the reason appended
  to `score_reasons` — the record and the audit trail stay.
- **The Shams rule outranks the LLM.** A marquee company's role is never
  alert-suppressed by a verdict (the verdict is stored as information, and
  the penalty still informs ranking). One missed Anthropic alert costs more
  than ten stale ones.
- **Verdicts are cached and re-applied.** Each job costs at most 2
  fetch+LLM attempts ever (`rec["quality"]`); `reapply()` restores the
  penalty after every re-score, keyed to the reason line so it can't
  compound on jobs the re-score didn't touch.
- **Budgeted and targeted.** ~15 verifications per 2-hour cycle, aggregator
  links (jobright/simplify) first — their labels are least reliable and
  their links go stale, while direct-ATS jobs are re-confirmed alive every
  poll. JS-shell hosts (Workday/Eightfold/Oracle) are skipped, not burned.
- **"Unclear" never suppresses.** A page we can't read or a verdict we
  can't parse leaves the job exactly as the rubric scored it.

Amended after the first live cycle (2026-07-14), which found 3 dead
postings and a mislabeled-senior Netflix role but exposed three gaps:
- **The budget caps attempts, not successes** — unreadable pages had let
  the loop run 131 fetches to land 15 verdicts. Default is now 25 attempts.
- **The house rule outranks the LLM's strictness too**: a verified
  1–2-years posting takes −10 and stays visible (the hard gate is 3+ yrs;
  a new grad should still see those), instead of being alert-suppressed.
- **Push races resolve by cache-merge, not git merge.** An enrich cycle
  takes minutes, a CI crawl lands mid-cycle, and both sides regenerate
  jobs.json + docs wholesale — `git pull --rebase` conflicts and used to
  wedge the clone mid-rebase. run.sh now self-heals on entry (abort any
  in-progress rebase) and recovers from a rejected push by copying only the
  LLM caches (quality verdicts, culture dossiers — dict-additive) onto a
  fresh `reset --hard`, then re-running a zero-limit enrich to rebuild
  effects and docs without new LLM calls (`scripts/mac-companion/merge_state.py`).


## 31. Field fit and seniority outrank the Shams rule (2026-07-16)

The Shams rule (#19) let any marquee posting alert once it passed the thin
hard gates — and the board flooded with roles Victor can't use: 25 Anthropic
Safeguards/policy roles, OpenAI Trust & Safety and Legal engineering, Netflix
L5s, "Software Engineer 3"s (only roman III/IV were gated). Meanwhile his
inbox trust eroded — the cried-wolf effect meant real fits sat unopened.

Rules v2 (`radar/score.py`), all title-scoped because ATS descriptions are
blank in state, all demote-don't-delete:

- **`OFF_FIELD_RE`** — safeguards / trust & safety / policy / sales /
  marketing / PM / support / recruiting / etc. titles lose alert
  eligibility on *every* path, marquee included. Dashboard only, reason
  logged ("off-field title (dashboard only)").
- **`MIDLEVEL_RE`** — II / L4 / "Engineer 2" / mid-level → dashboard only.
  `SENIOR_RE` now hard-gates numeric levels (Engineer 3+, L5+, Level 3+)
  and "Leader" alongside senior/staff/III/IV.
- **`quality.reapply()` may suppress marquee alerts** — the `not marquee`
  guard is gone. The Shams rule now bypasses exactly one thing: the
  new-grad-wording requirement. Amends #19 and #30; the reasoning that "one
  missed Anthropic alert costs more than ten stale ones" broke down when the
  stale ones hit dozens per week and were verifiably off-field or senior.
- **`FEEDBACK_STOPWORDS`** — the taste model was learning off-field tokens
  from tracked applications (business:3, product:4, marketing:2 in
  state/feedback.json) and boosting exactly the roles being demoted.
  Filtered symmetrically in `_title_tokens`, so learning stops AND stale
  entries go inert at read time; `repair-feedback` cleans the file
  cosmetically (documented repair).

## 32. Priority sectors + re-gate on rules bump (2026-07-16)

**The WHOOP lesson:** WHOOP — sensors, medtech, squarely Victor's field —
was seeded and polled from day one, yet never alerted: not marquee, and
Greenhouse postings carry no description, so new-grad evidence could never
appear. Precision-first gating silently starved the best-fit companies.

- **`priority_sectors: [healthtech]`** (profile.yaml): a strong engineering
  title (role bucket, excluding bare "<anything> Analyst" — measured: it
  admits Patient Relations / Retirement Benefits Analysts) at a
  priority-sector company is alert-eligible without new-grad wording.
  Off-field/mid-level demotions and LLM verdicts still apply on top.
- **marquee_companies += WHOOP, Oura, Dexcom, Abbott** — safe now that
  marquee no longer bypasses field fit. Keep `S.marquee` in
  webapp/index.html (both copies) in sync, as ever.
- **`regate()`** (`radar/score.py`, runs at the top of every crawl): stored
  jobs whose `rules_v` predates `score.RULES_VERSION` are re-gated in place
  — alert_ok flips both ways, reason appended ("re-gate v2: …"), closed
  jobs never resurrected, cached quality verdicts re-applied last so LLM
  suppressions always win. Rules changes now reach the ~3,100 already-open
  alert records instead of only future crawls; the first post-bump crawl
  commits a large one-time diff (every record gains `rules_v` /
  `explicit_new_grad`). Measured on 2026-07-16 state: 261 demoted, 95
  promoted, 0 closed re-opened.

## 33. Pasted-JD verdicts: the human supplies what the fetcher can't (2026-07-16)

SPA hosts (Workday/Eightfold/Oracle) and bot-walled postings can't be
fetched, so their quality verdicts never happen. The platform's Role-fit tab
now has a paste box: the JD lands in `state/web_state.json` via the existing
web-state path (truncated to 6 KB; the UI states plainly that the repo is
public), and the next enrich cycle grades it with `quality.verify_pasted()`
— same prompt, verdict stored with `source: "pasted"` plus a `jd_sha` hash
so an unchanged paste never costs a second LLM call. A pasted verdict
overwrites a fetched one (fresher, human-supplied text). Enrich reads
web_state.json but never writes it — the webapp owns that file.

## 34. SPA postings read via their JSON APIs; glyph records scrubbed (2026-07-16)

Workday/Oracle/Eightfold postings are JS shells — a plain GET returns no
text, so the quality pass skipped them, which exempted ~480 alert-worthy
jobs (109 direct-Workday alerts plus every simplify link pointing at a
Workday tenant) from new-grad/role-fit verification. `fetch_posting_spa()`
(radar/quality.py) now calls the same JSON endpoints their own frontends
use: Workday `wday/cxs/{tenant}/{site}/job/...`, Oracle ORC
`recruitingCEJobRequisitionDetails` (empty `items` = requisition gone =
closed), Eightfold `api/apply/v2/jobs/{id}` with the careers domain looked
up from the registry. Failure modes stay conservative: any error is
`alive=None` ("can't tell", never closes anything), capped at the usual 2
attempts before "unclear" (which never suppresses). Built in a sandbox
that couldn't reach ATS hosts — the first live cycle (Mac companion or CI
enrich) is the real validation; the paste-in box (#33) remains the manual
fallback.

Also: jobright's parser now drops continuation rows it can't resolve to an
employer, and `scrub_glyph_companies()` runs every crawl — the 101 stored
records with company `"↳"` (pre-continuation-fix parses, all alert_ok,
unidentifiable, and duplicated under their real employers by later crawls)
are removed rather than demoted. The adjust-don't-delete rule (#30) is for
jobs Victor might act on; a record whose employer is a glyph isn't one.

## 35. The crawl scrapes posting text; facts are extracted without an LLM (2026-07-17)

Two days of live evidence (llm_note count: 0 across 8,392 records; newest
quality verdict 47h stale) confirmed the LLM layer only runs when the Mac
happens to be awake — no ANTHROPIC_API_KEY or LLM_* secrets exist in
Actions. Everything the LLM was trusted with was therefore mostly not
happening, while Victor manually opened postings to answer two questions
the radar should answer: *does it sponsor visas* and *how many years does
it really want*. Fix: stop gating the basics on an LLM.

- **Descriptions at the source:** Greenhouse is fetched with `content=true`
  and Ashby's `descriptionPlain/HTML` is kept (Lever already had text), so
  the description hard gates (3+ yrs, clearance) fire at intake again.
- **`radar/posting.py`** — regex-only extraction of `sponsorship`
  (yes/no/unknown + the matched phrase), `years_min` (digit and word
  numbers, ranges take the floor, first-match-wins ordered by signal
  strength) and `intern_counts`. Stored as `rec["posting"]`, shown in the
  platform (row tags + a "Posting facts" card) and in alert-issue lines.
- **`scrape_pass()` runs inside every crawl** (cloud, ~30 min, zero keys):
  free inline analysis for ATS-provided text, then a budgeted fetch
  (`RADAR_SCRAPE_LIMIT`, default 20/run ≈ 960/day; `RADAR_SCRAPE_DISABLE`
  to kill) for new alert-eligible jobs and the stored alert-worthy backlog,
  using the same fetchers as the quality pass (JSON APIs for SPA hosts).
  Dead links close the job, crawl-side now, not just at enrich.
- **Effects are demote-only** and survive re-scores/re-gates via
  `posting.reapply` (mirrors quality.reapply): scraped `years_min >= 3` →
  dashboard only; `sponsorship == "no"` demotes **only when**
  `candidate.needs_sponsorship: true` in profile.yaml (default false —
  informational either way).
- The LLM quality pass still layers judgment on top when a provider
  exists, and now stores these deterministic facts from its fetched or
  pasted text too — so a Mac cycle or future cloud key enriches, but
  nothing depends on it.

## 36. Candidate-first platform flow + title-led role gates (2026-07-18)

The platform had the underlying data but made candidates work too hard to
use it: no role-family selector, sponsorship/experience facts disappeared
when they were unknown, job titles skipped the workspace and opened the
posting directly, and applying/tracking required too many separate clicks.
At the same time, description text was allowed to establish field fit. That
made company boilerplate such as "we build AI software" promote unrelated
titles at marquee companies (Safety Editor, Biology Research Associate,
Shipping & Receiving Associate) into the alert feed.

- **Rules v3 is title-led for role eligibility.** Description text may still
  establish entry-level evidence, years, clearance, and other posting facts,
  but cannot turn a non-technical title into AI/SWE/DS. Generic analyst and
  recognized off-field titles are demoted to dashboard-only rather than
  deleted; truly unrelated description-only matches fail the field gate.
  The data-science title bucket no longer treats every bare "Analyst" as DS.
- **The Jobs view filters the decisions candidates actually make:** role
  family, visa sponsorship status, experience requirement, sector, score,
  pipeline status, and sort order. Filters persist per browser and clearly
  distinguish "not stated" from "not analyzed"; unknown is never presented
  as a positive sponsorship claim.
- **Job titles open a decision-first workspace.** Fit & eligibility is the
  first tab, with role family, sponsorship, years, location, salary, score,
  and age above company research. A separate primary button opens the actual
  application so candidates can inspect the evidence before leaving.
- **Applying is shorter but remains human-controlled.** `open application`
  opens the employer link and, only when an authenticated instant-write path
  exists, quietly saves an untracked role to **To apply**. It never claims an
  application was submitted; `mark applied` remains explicit. Track/applied
  actions are idempotent to prevent duplicate local pipeline records.

Measured against production state before release: rules v3 reduced 3,250
current alert records to about 2,600, demoting roughly 650 false positives.
The normal `regate()` migration applies this on the next crawl; generated
state is not edited by hand.

## 37. ChemE is a separate internship-first profile, not a filter on the tech board (2026-07-18)

Chemical Engineering internships have different eligibility evidence, role
families, employers, search terms, and scoring weights from new-grad software
roles. Treating ChemE as another UI filter would mix discovery state and taste
feedback and make both boards harder to trust. The dedicated
`claude/cheme-intern-radar` branch therefore owns its profile, generated job
state, feedback, dashboard, and profile-tagged culture data. It retains the
same tested engine and historical state migrations, but a ChemE crawl re-gates
open records under internship-first rules instead of hand-rewriting generated
files.

## 38. Two production boards, one repository and one Notion tracker (2026-07-18)

The existing new-grad board remains the primary Vercel project and production
branch. ChemE is deployed independently at `job-radar-cheme.vercel.app`, reads
`claude/cheme-intern-radar`, and uses a `cheme` profile marker plus dedicated
GitHub labels so actions, issue edits, and comments reach the correct branch.
This avoids one site's deployment or pipeline state overwriting the other.

GitHub scheduled workflows are loaded only from the repository's default
branch, so that branch contains small `cheme-*` orchestrators which check out
and commit back to the ChemE branch. The ChemE board starts in tokenless/PAT
mode because the existing GitHub OAuth app has one callback URL; it can receive
its own OAuth app later without changing the architecture. Both boards use the
same repository-level `NOTION_TOKEN` and the same Applications database by
design. Separate board state does not mean duplicate Notion state.

## 39. AI is a bounded evidence processor, not an always-on judge (2026-07-18)

Four free NVIDIA endpoints are useful capacity, but their quotas are unknown
and live probes showed real endpoint instability (Kimi auth succeeded then its
model returned 404; DeepSeek later returned a transient 500). Blind
round-robin would both waste quota and make output quality random. The shared
LLM entrypoint therefore routes by task, tries at most two healthy providers,
retries a transient response once, cools unhealthy endpoints, validates task
schemas before accepting an answer, and records only secret-free operational
telemetry. Main and ChemE have separate conservative nightly budgets; named
keys never enter the 30-minute crawl. Explicit user input and actionable jobs
outrank background backlog. Local Ollama remains the bulk lane.

The same evidence boundary applies to company research. Model memory is not a
source: the crawler captures short relevant excerpts from official postings it
already fetches, synthesis must cite valid source IDs, unchanged evidence is
cached, and missing facts remain `Not confirmed`. The old `source: est.` culture
rows are still visible for continuity but cannot affect score; only the
human-curated seed can move ranking. AI failure always degrades to the existing
deterministic system.

## 40. Tracker stages flow both ways; Google Sheets is a selectable backend (2026-07-18)

Calling Notion the source of truth while only pushing to it was misleading:
manual status changes never reached the platform, and the UI collapsed every
advanced stage into To apply. Reconciliation now reads statuses back by the
already-owned Notion page ID (never fuzzy company/title matching), and the UI
has separate Applied/OA/Interview/Rejected/Closed states plus an Interview
workspace.

Tracking is selected with `TRACKER_BACKEND`, default `notion`. The
`google_sheets` adapter uses stable Job Radar IDs for upsert/readback so people
in Google Workspace can use a normal Sheet without a Notion account. GitHub
Actions requires an OAuth refresh token for unattended access, so code ships
ready while activation remains an explicit user authorization step. Main and
ChemE may share one tracker without cross-board corruption because both Notion
and Sheets updates match stable IDs, not company names.

## 47. New-grad-first technical role and leadership-program focus (2026-07-19)

Victor's highest-priority filter is now verified new-grad or early-career fit,
not employer prestige, aggregator presence, salary, or healthtech alone. Alert
eligibility therefore requires explicit new-grad/early-career evidence or a
technical/data graduate, rotational, or leadership program. A required floor of
1+ years is dashboard-only; 0-2 years remains compatible with new-grad hiring.

Among eligible roles, AI/ML and data science lead, followed by general software
engineering, then data engineering and systems. Marquee employers receive
an explicit competitive label and a small secondary bonus, never a gate bypass.
Technical program matches receive their own reason and bonus so opportunities
such as Johnson & Johnson's Technology Leadership Development Program and
Merck's technology/emerging-talent tracks are visible even when their titles do
not say "software engineer." Generic finance, sales, HR, and other off-field
leadership programs stay dashboard-only.
# 49. Posting evidence and degree mismatch are first-class ranking inputs (2026-07-19)

Full posting text is attempted through public ATS JSON endpoints (Greenhouse,
Ashby, Lever, Workday, Oracle, and Eightfold) before generic HTML. If a page is
blocked, empty, or only a JavaScript shell, the job records `requirements
unverified` and the UI says why. Deterministic extraction now identifies required
experience and master's/PhD degrees; positive experience floors receive large
auditable penalties, and degrees above the candidate's bachelor's profile receive
an even larger penalty while remaining visible for review. Preferred degrees do
not trigger the mismatch.

# 50. Full-board score rebuild and visible score version (2026-07-20)

Changing the role weights or scoring equation must affect existing active
postings, not only newly crawled jobs. Every crawl now rebuilds all active
stored scores before publishing `state/jobs.json` and generated dashboard
outputs; `rescore` remains the manual repair command. Records carry
`score_version`, and the platform's job drawer displays that version beside
the score and the auditable reason list.

## 51. Source coverage, notification delivery, and in-house tracking (2026-07-20)

SimplifyJobs/New-Grad-Positions is treated as trusted new-grad evidence because
its maintained board is explicitly scoped to new-grad roles; SWEList currently
uses that same public feed, so a separate fragile SWEList scraper would only
duplicate ingestion. Active rows are retained for one year even when an
aggregator's timestamp is stale; stale timestamps still suppress notification
alerts. The platform now has one assigned GitHub issue per new qualifying
posting for reliable per-posting push/email notifications, plus one unassigned
master board for all-at-once browsing. The in-house Pipeline is the primary
tracker; Notion is an optional mirror and untracking creates a tombstone so
reconcile cannot silently re-add a deliberately removed role.

## 52. Ranking policy v6: new-grad first, then role and field (2026-07-20)

New-grad/early-career evidence is the dominant score component and trusted
new-grad board sources can supply that evidence when a title is terse. Within
that eligible set, AI/ML, data science, general SWE, data engineering, and
systems are ordered in that priority; health, sports, videogames, education,
AI labs, and big tech are explicit high-value sectors. Marquee-company points
remain competitive context rather than a gate bypass. Every full rebuild stamps
rules/score version 6 so existing postings receive the same equation.

## 53. Company research is a recurring web-enrichment stage (2026-07-20)

New postings now trigger bounded public-web research for the employer before
LLM synthesis. The crawler captures company/about, careers, benefits, culture,
and discovery-board excerpts, retains their URLs, and shares one dossier across
that employer's roles. The Company tab renders the plain-English overview plus
the requested employer profile table. Exact policies are only stated when
evidence supports them; otherwise the UI says `Estimated` or `None found in
research`, and deterministic crawling continues if AI or web requests fail.

## 54. Discovery provenance is part of every posting (2026-07-20)

Aggregator jobs retain a human-readable source label and board URL in
`source_url`; alerts, the dashboard, and the web drawer expose it. Zapply's
data-science/ML board joins the existing GitHub feeds, but its rows still pass
the same new-grad and role gates because the board contains some experienced
roles. EntryLevel and JobsForNewGrad were researched as possible future inputs,
but are not scraped without a stable public feed.

## 55. Codex is the default production publisher (2026-07-20)

Victor checks the live production site for changes. For rapid prototyping, Codex
may push, merge, and deploy validated requested work without an extra approval
prompt during the active task. Other AI coding agents require Victor's explicit
permission before any production write. This is documented workflow authority;
GitHub and Vercel permissions remain the technical enforcement layer.

## 56. Alert email batches supplement per-posting issues (2026-07-20)

Individual GitHub alert issues remain durable per-posting tracking surfaces but
are unassigned and silent. A six-times-daily batch workflow emails up to 15
unsent alerts at a time. Batches rank by score and then recency, normally wait
for three roles, allow a high-score urgent exception, and release a smaller
batch after 12 hours. Overflow is retained for the next interval, and empty
intervals stay quiet. The existing nightly best-of issue remains a separate
daily summary.

## 57. Backfills checkpoint and scoring maintenance is automated (2026-07-21)

The first company-research drain committed only after the entire run. A timeout
or rate limit could therefore discard every dossier synthesized earlier in that
run. Backfill cycles are now deliberately small, prioritize the highest-score
visible employers, and commit after each cycle; the next cycle starts from the
latest branch snapshot and resumes safely. Production telemetry showed GLM was
the reliable company-synthesis provider while Nemotron frequently failed schema
validation, DeepSeek was slow, and Kimi returned 404, so the backlog lane is
GLM-first with a bounded request timeout and fallback.

Scoring policy changes must affect every stored job. CI now fails on missing or
stale score coverage, and a six-hour maintenance workflow rebuilds scores from
a fresh upstream snapshot before pushing. If a crawl races the rebuild, the
maintenance attempt discards its stale temporary commit and recalculates from
the newer state rather than merging generated JSON by hand.

## 58. Hosted providers race; benchmark decides task quality (2026-07-21)

Serial provider fallback made a slow or broken free endpoint block every later
provider. Each logical AI call now starts every configured healthy endpoint at
once; the first schema-valid response wins while slower attempts finish for
telemetry. A concurrent benchmark workflow tests the real company-research and
posting-quality schemas and records the fastest valid provider per task, so the
documented order is a measured tie-break rather than an artificial serial
queue.

## 59. Generated state publishes before idempotent delivery (2026-07-21)

Concurrent writers routinely change `state/*.json` and generated docs, so a
generic `git pull --rebase` is neither a reliable retry nor a safe merge
strategy. The crawl now rebuilds from the newest production snapshot when its
push loses a race; it makes no external GitHub writes until that state commit
succeeds. A separate delivery command then upserts tracking issues using the
embedded `<!--radar:<job-id>-->` marker (including closed issues) and refreshes
the master board. This makes delivery replay-safe without turning individual
tracking issues back into email notifications.

Cloud enrichment uses the complementary rule: its LLM quality, culture, company
research, and usage results are additive evidence caches. On a rejected push it
resets to current production, merges only those caches, and runs deterministic
`rescore` to regenerate derived jobs/docs. Neither path force-pushes, silently
drops production state, or attempts to text-merge generated JSON.

## 60. Dossier backlog uses quota-aware parallel work (2026-07-21)

Company dossiers are independent per employer. The long-running backfill now
starts a bounded concurrent batch instead of waiting for one 2,200-token
response at a time. The global request budget remains authoritative; each
logical task races the configured API providers and uses the first
schema-valid response. Provider failures are circuit-broken with exponential
cooldowns, so 429s and timeouts stop consuming subsequent company slots while
recovered or alternate providers can take the work.

## 61. Backfill traffic is smoothly paced below provider quota (2026-07-22)

The configured provider allowance is an upper bound, not a reason to send a
burst. The dossier backfill now uses a single 30-RPM gate across every actual
HTTP request, including retries and simultaneous model-race candidates. It
therefore spaces sends two seconds apart while still working continuously;
provider-specific circuit breakers handle outages without turning them into a
tight retry loop.

## 62. Manual role capture belongs in Pipeline, not the alert feed (2026-07-22)

Jobs discovered outside the radar still need one-click capture without a
separate Notion chore. The Pipeline tab therefore accepts a company, role,
live posting URL, and optional location from the authenticated owner and
dispatches it through the same saved-stage/Notion sync path as tracked radar
roles. The handler derives the normal stable job ID, makes repeated URL saves
idempotent, and stores a visible `manual_added` marker. Manual items are forced
dashboard-only (`alert_ok=false`, `explicit_new_grad=false`) so they never
become fabricated new-grad alerts or alter alert delivery; an already-crawled
posting retains its existing authoritative score and provenance.

## 63. Multi-board employers are explicit coverage, not one guessed feed (2026-07-22)

Some employers partition recruiting by business unit. Fanatics currently uses
separate official Greenhouse boards for corporate, Betting & Gaming, Commerce,
and Collectibles, so treating its Oracle board or a single Greenhouse token as
the whole employer silently misses openings. Curated registry entries may
therefore share the same employer name and sector while polling distinct,
verified ATS tokens. Greenhouse records retain their official board URL as
discovery provenance. If a user manually captures a role before the crawl sees
the same stable company/title/location identity, official ATS data replaces the
manual placeholder while preserving its Pipeline/Notion tracking marker; this
gains real description-based gates without fabricating new-grad evidence.

## 64. Delivery is bounded by the current alert window; early-career is a label, not a gate bypass (2026-07-24)

The persistent master board and silent one-issue-per-alert surfaces remain the
delivery contract, but delivery must not get slower forever as historic issues
accumulate. Per-posting idempotency now scans GitHub only from the oldest alert
in the replay window, while master-board comments are rendered then patched
only when their text changed; independent comment updates use modest bounded
parallelism and log the number of writes. The durable embedded job marker still
protects the post-then-crash case, so this is a scalability improvement rather
than a shortcut around correctness.

Some technical roles state no experience floor without actually claiming to be
new-grad. They receive a visible/filterable `early-career possible` label only
when they pass the target-role and seniority checks; it never contributes
new-grad evidence, changes the score, or permits an alert. Every record is
rebuilt under rules version 7 to carry the auditable classification.

Company-research provider/schema failures are retained as retryable evidence
records, with per-company exponential backoff capped at six hours. New evidence
clears that wait immediately. Backfill checkpoints now report ready, pending,
retry-waiting, and error counts, so a temporary provider outage cannot look
like finished research or hammer one broken endpoint.

## 65. On-demand company research is owner-only and asynchronous (2026-07-24)

The Company drawer lets Victor, while signed in through the platform's sealed
owner OAuth session, request a fresh research pass for that employer. The
Vercel endpoint only dispatches a stable job ID after the existing owner check;
the GitHub Actions worker receives provider credentials as secrets, captures
fresh public company/careers/benefits evidence, and synthesizes one dossier.
Visitors, Pages users, and browser PAT flows have no button and cannot invoke
the action, so no public page can consume Victor's API quota.

The request returns immediately and the drawer tells the owner to refresh in
roughly 1–3 minutes. This includes Actions queue/startup plus the normally
20–75-second hosted synthesis. To make a click-through session feel fast, the
opened employer is processed first and the same owner request warms up to four
distinct, not-yet-ready companies from the current Jobs ordering concurrently.
The workflow records retryable provider failure rather than making the browser
wait or claiming a result it did not obtain.

## 66. The company-research backlog uses a continuous relay until caught up (2026-07-24)

Company dossier work is a finite but long-running queue, so a six-hour gap
after a short worker exit wastes available provider capacity and leaves already
captured evidence visibly unsynthesized. The Actions backfill is therefore
scheduled every 30 minutes while retaining one shared concurrency group: one
worker may run and only the newest relay may wait. This prevents overlapping
writers or an unbounded Actions queue, while promptly resuming after an empty
batch, a provider cooldown, or the GitHub job limit.

The source-collection stage and synthesis stage share the same owner-interest
order: saved/applied or web-tracked roles first, then alert-worthy roles, then
score and freshness. This relies only on explicit persisted interest, not a
guess about an arbitrary visitor's next click. Twelve bounded concurrent
dossiers keep the existing 30-RPM global request gate busy during provider
latency; the gate, circuit breakers, and per-company exponential retry remain
the cost and reliability limits.

## 67. Resume intelligence is owner-first and CLI-native (2026-07-30)

The resume feature must meet Victor's quality bar before it becomes a
multi-user service. The public radar therefore does not receive CV content,
provider credentials, or generated resume artifacts. `scripts/resume_studio.py`
runs on Victor's Mac, reads the ignored `CV/` directory and the local job
snapshot, and writes prompts, drafts, PDFs, and review reports beneath
`CV/.resume_studio/`.

The first slice has two modes. Strict mode uses the existing human-approved
TLDP one-page artifact and performs deterministic rendering checks. Frontier
mode invokes the installed first-party Codex and Claude Code CLIs through their
local authenticated sessions, runs independent drafts, synthesizes them, and
then uses a fresh fixed reviewer pass. API-key environment variables are
removed from those subprocesses so the owner workflow cannot silently switch
to billable API traffic. Ollama remains an optional radar-enrichment fallback,
not the required writer or reviewer for resume work.

The reviewer returns observations but cannot set its own score. The harness
owns the versioned rubric, hard layout/fact checks, point weights, and
readiness calculation. This preserves human control while preventing a
generation prompt or model from weakening its test. Future multi-user support
may offer private local agents, official provider/API adapters, or another
isolated compute path, but it must not route other users through Victor's
subscription sessions.

## 68. TLDP is the visual baseline and employer headings are company-first (2026-07-31)

`CV/tldp_resume.tex` and its one-page PDF are the baseline because they are
Victor's actual TLDP application materials, not because “TLDP” is a generic
resume style. Resume Studio preserves that baseline's typography, spacing,
hierarchy, density, and owner header. Employer entries render the company on
the first line and the role on the second; Education remains school-first.

Strict mode uses that baseline with deterministic checks. Frontier mode uses it
as the layout shell while selecting target-specific evidence from the local
CV source bank. For a Johnson & Johnson target, TICC is excluded unless the
posting makes it relevant; this is a target decision, not a global deletion.
When only one authenticated frontier CLI is usable, the harness skips a
redundant same-provider synthesis call and reports that fact. Reports include
Codex's emitted token usage and explicitly mark totals incomplete when a CLI
does not provide a usage footer.

## 69. Resume Studio renders plans through `CV/resume.tex`; models never author the document (2026-07-31)

Decision #68 incorrectly treated the TLDP artifact as the generic rendering
baseline. Victor clarified that the existing general `CV/resume.tex` / `.pdf`
is the canonical format and that the methodology, example résumés, and master
CV are the content system. TLDP remains one target application artifact.

Resume Studio therefore removes complete-LaTeX generation from the provider
schema. Source-only mode selects source IDs and copies existing headings and
bullets verbatim. Enhancement mode may revise selected bullet text while
retaining a source ID, but a deterministic renderer owns the document and
copies the complete header, education, skills, preamble, margins, typography,
and spacing from `CV/resume.tex`. Employer entries use the existing
`\resumeSubheading` macro with company first and role second.

Formatting is a hard test, not a prompt preference. Generated files must have
an identical canonical prefix, no model-supplied layout commands, no font-size
increase, and no more than a 5% reduction; the current renderer makes a 0%
change. One page is insufficient by itself: the visible content must reach
within 24 points of the canonical resume's bottom-most content, preventing a
sparse one-pager from passing the fixed reviewer.

## 70. Ranking needs uncapped utility before a calibrated 0–100 display score (2026-07-31)

The additive scorer's final `min(100, ...)` compresses distinct top roles into
the same result and makes it difficult to tell a great goal-company opening
from the best possible opening at that company. NVIDIA and comparable genuine
goals must still be able to earn 100; the correction is not to suppress them,
but to let posting-specific evidence distribute excellent roles through the
90s and reserve 100 for combinations that actually reach it.

The future scorer will therefore reason in uncapped internal utility before a
versioned mapping to the displayed 0–100 range. Exceptional strength in one
dimension may partly compensate for weakness elsewhere—Victor's model is that
a dimension can effectively contribute "120" rather than disappearing at its
nominal ceiling. This compensation does not cross hard eligibility, seniority,
location, or field-fit gates.

This is a general scoring property, not a collection of employer exceptions.
Company momentum, compensation, technical intensity, role quality, learning,
mission, and prestige require distinct, auditable evidence; health affiliation
alone cannot make an otherwise ordinary small employer elite. Conversely, an
exceptional non-health role may outrank a health role without requiring a
Netflix-specific or other hand-tuned rule. Detailed implementation and
calibration acceptance criteria live in `ROADMAP.md`; behavior does not change
until the scorer, migration, reason strings, and golden regression fixtures
ship together under a new rules version.

## 71. Score v8 ships as fixed utility calibration, not percentile ranking (2026-07-31)

Rules version 8 implements decision #70. Each job now retains an uncapped raw
utility, a pre-adjustment calibrated score, named dimension totals, and the
existing reason ledger. The display mapping is a fixed monotonic piecewise
curve; it does not inspect the current job population, manufacture winners, or
move when the feed changes.

New-grad eligibility remains the dominant compensable dimension. Explicit goal
companies receive a profile-declared signal, while generic cited company
research can add technical reputation, scale, pace, and technical-work utility.
Posting wording and continuous compensation signals create role-level spread.
Health/sector preference uses diminishing influence so mission cannot carry an
otherwise ordinary employer to the ceiling. All hard eligibility, seniority,
location, and field-fit gates continue to operate outside compensation.

The local Resume Studio projects stale v7 records through the v8 equation in
memory so its sort is current before the next crawler rebuild. Production state
is still migrated only by the normal full-score rebuild; the Studio never
hand-edits generated state.

## 72. Resume Match, evidence authority, page packing, and review are separate systems (2026-07-31)

Resume Studio builds a private evidence graph beneath `CV/.resume_studio/` from
the canonical resume, master CV, example resumes, methodology, context notes,
and experience dossiers. Explicit experience source-of-truth files outrank
resume wording. Public GitHub and Devpost records may corroborate breadth but
cannot authorize a resume claim by themselves. Enhancement bullets must cite a
known claim-authorizing evidence node.

Resume Match is a fixed pre-generation rubric: required coverage 35, preferred
coverage 20, evidence strength 20, domain relevance 10, eligibility 10, and
distinctiveness 5. Its confidence and reasons are visible, and it remains
separate from the public Radar score. Best, Newest, and Resume Match are three
explicit sorts, not one blended score.

Models propose a rich, ordered evidence portfolio rather than a fixed three-
experience/four-project layout. A deterministic compile loop removes the
lowest value-density content until the canonical one-page template fits, then
tries strong exclusions back. PDF coordinates enforce horizontal saturation;
a second compile with one ordinary bullet added must overflow to prove vertical
capacity is used. The style delta is currently 0%.

Finally, the immutable Resume Craft rubric scores target fit 30, evidence 25,
clarity 15, portfolio 20, and layout 10. Factuality, eligibility, and layout
are independent gates. A polished draft cannot average away an unsupported
claim, an ineligible role, or a failed layout.

## 73. Resume density is a cognitive portfolio constraint, not a page-filling objective (2026-08-06)

Decision #72's rich-portfolio and saturation rules produced physically full
pages with 25–30 bullets, semantic duplicates, and weak backup evidence. Resume
Studio now asks models for a selective shortlist and deterministically retains
16–20 distinct bullets across at most three experiences, three projects, and
one leadership entry. Target-aware model priority remains primary; generic
metric and technical signals are tie-breakers. Safe source backups are added
only below the minimum portfolio size.

One page, unchanged style, extractable text, and one-line bullets remain hard
layout requirements. A concise line may end before the right margin. Bottom
density and the one-extra-bullet compile are diagnostics against extreme
sparsity, not reasons to pad. LaTeX compilation failures stop packing instead
of being misclassified as overflow and causing evidence deletion.

Enhancement also preserves source scope. Qualifiers such as synthetic,
prototype, proof of concept, simulation, and demo cannot silently disappear;
an affected bullet reverts to authorized source wording and records a warning.
Historical target resumes are outputs rather than evidence-bank inputs, and
the canonical `CV/resume.tex` remains authoritative for immutable contact,
education, skills, employer-heading metadata, and dates.

## 74. Human-reference fullness and applied final review supersede the sparse shortlist (2026-08-06)

Decision #73 failed its intended outcome. The 16–20-bullet limit and 120-point
bottom-gap tolerance accepted generated resumes with 17–18 content bullets,
roughly 54 points of visible unused space, and less meaningful evidence than
either immutable human-authored reference. A passing process did not make
those artifacts usable.

Resume Studio now treats `CV/resume.pdf` and `CV/tldp_resume.pdf` as immutable
quality references as well as formatting references. The final portfolio must
contain 22–26 distinct bullets, preserve all three established experiences,
retain four complementary projects and at least one leadership entry, and end
within 24 PDF points of the general reference. Every bullet remains one visual
line. Fullness must come from additional source-grounded interview evidence,
never filler, duplicate phrasings, font reduction, or margin changes.

The adversarial model call is now a final editor rather than a post-hoc
commentator. It must return a complete corrected source-addressed plan, which
is validated, packed, rendered, and applied before the run is scored. A
complete 22+-bullet plan is not automatically expanded, so deterministic
backups cannot silently restore evidence the final editor excluded. If an
enhanced line wraps, approved source wording is restored before considering
another model call. The final editor uses medium reasoning by default while
the strategist remains high. The strategist receives the governing
methodology inline, and both calls receive authorized evidence inline so they
do not spend subscription context rereading the repository. The final editor
reviews the structured plan rather than duplicating the full LaTeX document.

The corrective Mayo case rendered 23 meaningful bullets across three
experiences, four projects, and leadership; it had zero wraps, a 2.25-point
bottom gap, unchanged style, and all factual, target-fit, evidence, clarity,
portfolio, eligibility, and layout gates passed under `resume-review-v4`.

## 75. Resume Studio runs are a durable, posting-linked private library (2026-08-07)

The local Studio must make generated work inspectable before it can be judged.
Each new run therefore snapshots the selected posting and job metadata beside
its private artifacts, assigns a company-identifiable PDF name such as
`mayo_clinic_resume_ai.pdf`, and remains addressable after another posting is
selected. A derived Resume Bank indexes current runs and legacy architecture
experiments without changing the immutable `CV/resume.pdf` or
`CV/tldp_resume.pdf` references. The UI separates creating a new run from
browsing/previewing existing runs, shows source-only versus AI-enhanced mode,
and exposes the saved posting snapshot when one exists. Failed and in-progress
runs remain visible as inspectable history; no run is overwritten by a later
selection.

## 76. Resume Studio is an editable, source-grounded workshop (2026-08-07)

The first Resume Studio generation contract was too narrow: enhanced agents
were effectively asked to tighten existing source bullets, and deterministic
packing could then restore source wording to fill the page. That made a
passing process capable of producing a weaker résumé than Victor's own
reference. Enhancement mode now permits a substantive rewrite or synthesis
from multiple authorized source bullets. Each rendered line keeps a primary
source ID, supporting source IDs, evidence IDs, and protected scope
qualifiers; models still cannot author LaTeX layout or invent facts.

Completed runs expose a local Workshop with readable line-level editing across
education, technical skills, experience, projects, and leadership. AI returns
candidate rewrites without applying them automatically. Manual saves, applied
AI candidates, and reverts append a full plan revision and render into a unique
private PDF/preview directory, leaving the original company-named run artifact
untouched. The contact header and canonical visual template remain protected.
The available local provider lanes are Codex CLI and Claude Code; a Luna lane
is optional and is not claimed when no local `luna` executable exists.

## 77. Resume Studio must prove target changes and reject near-wraps (2026-08-07)

The prior generation contract could produce a polished-looking PDF whose
content was effectively the unchanged base resume, and its geometry check
called a line with only a few points of remaining width a pass. That is not a
useful tailoring result. Every current run now receives a compact authority
dossier from the CV methodology, notes, J&J source-of-truth, experience, and
knowledge documents, plus an exact-term ATS strategy extracted from the saved
posting. Enhancement prompts may replace projects and rewrite bullets around
supported terms; unsupported terms remain explicit gaps rather than invented
claims.

Reports expose rewritten source lines, project swaps, and rendered keyword
coverage so the user can see what changed without guessing from two PDFs. The
final layout gate requires every measured bullet to be one line with at least
12pt of right-edge safety. Wrapped or near-wrapped output is rejected after
source fallback and line editing, rather than being presented as a completed
resume. This remains a content-quality gate; the canonical human PDFs and
their source files are untouched.

## 78. Resume Studio uses stable filenames and excludes TICC (2026-08-07)

Generated project headings use the compact `|` delimiter even when the source
LaTeX uses `---`. PDF preview endpoints send the actual company-identifiable
filename through `Content-Disposition`, so opening or downloading a preview
does not fall back to `resume.pdf`. TICC is permanently excluded from the
source-addressable catalog, model plans, rendered output, and workshop edits;
the local historical CV files remain unchanged.

## 79. Resume Studio queues durable modes and exposes rationale/usage (2026-08-07)

Tailoring is now a durable queue rather than a single replaceable action. Each
run has its own ID, posting snapshot, status, artifacts, and workshop history;
the UI can queue Used bullets, AI tailor, or Unrestricted AI tailor runs and
keeps them in the bank. Unrestricted means freer source synthesis and original
wording, not permission to invent facts or remove factual scope qualifiers.
The workshop embeds the original or latest revision PDF, keeps every visible
resume line editable, and shows selection rationale, editorial notes, and
provider usage. The local usage ledger reports observed Codex tokens/calls for
the current week; because the Codex CLI does not expose a Plus weekly quota,
the UI states that limitation rather than fabricating a percentage. An owner
may set `CODEX_WEEKLY_LIMIT_TOKENS` to compare against a known personal limit.

## 80. Radar v9 uses early-career uncertainty and company diversity (2026-08-07)

Explicit new-grad/early-career evidence receives a stronger deterministic
utility lift. A technical role with no stated experience floor but no explicit
new-grad proof receives a smaller `early-career possible` lift and remains
dashboard-only; it is not promoted to alert eligibility. Once a company has
three or more visible roles, the strongest raw-utility role is protected and
only weaker same-company roles receive a transparent -1/-2 diversity nudge;
raw-utility ties are untouched. The calibrated range keeps 90+ as strong while
leaving room below 100. Resume Match remains a separate CV/evidence-graph
score. The platform now formats the established rating explanation as labeled
reason rows instead of a single separator-delimited string.

## 81. Recruiter discovery uses role-aware public search links, never LinkedIn scraping (2026-08-07)

The Outreach workspace now constructs bounded Google queries for five useful
lead types: university recruiters, technical recruiters, likely hiring
managers, NJIT alumni at the company, and public hiring posts. Company names
are quoted and the role family narrows technical searches to AI/ML, data,
cloud/platform, or general software work. The browser opens these searches;
the radar does not fetch, parse, rank, or persist LinkedIn people or post
results. Saved conversations remain explicit user-added links.

## 82. Resume review and workshop migrations preserve evidence and layout (2026-08-07)

Repeated reviewer blocks for the same source entry are merged before ordinary
validation, preserving distinct source-addressed bullets while keeping one
heading and an auditable warning. Reviewer-authored, source-supported ATS
wording is rendered and offered to the deterministic margin editor before the
pipeline may restore source text; factuality and final geometry gates are
unchanged.

Workshop front-matter addresses are regenerated from the current canonical
template whenever a saved draft is loaded. Existing line text and revision
notes are overlaid by stable line ID, so a renderer-index correction migrates
older private state without discarding edits or misplacing education and skills
rows. Workshop revisions remain additive and never overwrite the original run
or either human-authored reference PDF.
## 83. Official DOL sponsorship history is contextual, not a job promise (2026-08-07)

The radar now refreshes the latest quarterly U.S. Department of Labor OFLC LCA
public disclosure workbooks into a compact, committed company index. It counts
certified and certified-withdrawn H-1B, H-1B1, and E-3 cases over the covered
quarters, then conservatively matches legal-name-stripped employers and
location-qualified brand names to radar companies. Raw workbooks stay out of
the repository.

The result is deliberately a separate signal from the posting's deterministic
visa extraction. A company with historical certified cases is labeled
`likely`; a company with no matching case in the covered quarters is labeled
`no-history`; and a missing refresh is `unavailable`. None changes the score,
eligibility gate, or the candidate's `needs_sponsorship` policy. Every label
explains that company history is not evidence that this specific requisition
will sponsor or that a future petition will succeed. The official source is
the [DOL OFLC performance-data page](https://www.dol.gov/agencies/eta/foreign-labor/performance).

Semantic/vector RAG remains intentionally parked and is not part of this change.

## 84. GitHub identity can scope a private shared Google tracker (2026-08-07)

The existing Google Sheets adapter remains the owner/fork automation path, but
the Vercel platform now has a separate per-user tracker path. GitHub OAuth
authenticates identity; it does not grant Google access. A single owner-held
Google refresh token stays server-side, and the private `User Applications`
tab stores a row key of `GitHub User + Job Radar ID`. The backend filters reads
to the signed-in login and accepts only save, applied, Maybe, and archive
actions for that login. No user action commits to the public repository or
touches another user's tracker rows.

The workbook is created explicitly by `create-google-tracker`, with separate
Applications, User Applications, and Guide tabs, frozen headers, filters, and
native Google Sheets formatting. The owner must configure Google OAuth and the
Vercel/GitHub secrets; GitHub sign-in alone cannot create or authorize a Google
Sheet. The Sheet remains private, and the public Pages/tokenless path stays
owner-only. A true user-owned Google account/Sheet connection remains a later
option if shared storage becomes too large or privacy requirements change.

## 85. OAuth providers are the account system; Google Sheets is private storage (2026-08-07)

The platform now offers GitHub and Google as OAuth-only login providers. It does
not implement passwords or store provider tokens in frontend JavaScript. A user
signs in with one provider, then explicitly connects the other from the signed-in
Account center. The private `Accounts` tab stores provider-subject links and
merged-account metadata; a provider already attached elsewhere cannot be silently
reassigned. Existing tracker rows remain addressable through merged-account
aliases, preventing a linking operation from duplicating the application funnel.

The visible Google tracker follows the existing Notion Applications vocabulary
(`Company`, `Stage`, `Position`, `Apply date`, `Text`, `Job URL`, and `Location`)
with stable IDs and audit metadata beside it. A settings-style Tutorial modal is
the user-facing entry point for application workflow, account connection, tracker
behavior, and privacy boundaries. The site owner must provision the OAuth web
client and private workbook; users never need direct Sheet access.

## 86. Google OAuth provisions one user-owned tracker per connected account (2026-08-07)

The shared `User Applications` workbook was the wrong multi-user boundary for
the product goal: a user's Google sign-in must mean that their applications live
in their own Google Drive. The Vercel Google OAuth flow therefore requests
`openid email profile` plus the Sheets scope with offline access. On first Google
sign-in or explicit GitHub → Google connection, the backend creates a private
`Applications` workbook using that user's OAuth token and stores the workbook ID
plus encrypted refresh-token ciphertext in the owner-controlled private
`Accounts` registry. No new API key or OAuth client is required.

The backend chooses the Sheet from the authenticated account record, never from
browser input, and returns only that Sheet's rows. GitHub-only users can still
use repository-owner functionality where authorized, but must connect Google to
get personal Sheets storage. Existing shared-tab rows are migrated into the
owner's first personal workbook when the account can be matched, so this change
does not intentionally discard the earlier tracker funnel. The old shared tab
remains as a migration/legacy surface and is not used for new user rows.

## 87. PM roles are a dashboard-only research lane (2026-08-08)

Victor asked to let a friend use the platform for Product Manager, Technical
Product Manager, Product Owner, Project Manager, Business Analyst, UX/UI
Researcher, and Solutions Architecture new-grad searches without changing the
main radar's ranking priorities or notification trust. The implementation uses
one `pm` role bucket with weight `0`, keeps matching US postings in the board,
and forces `alert_ok=false` after all normal eligibility checks. Therefore PM
rows can be opened, filtered, saved, and applied manually, but cannot create
individual alert issues, master-board alert rows, alert batches, or RSS items.

Breadth comes from the existing SimplifyJobs New-Grad-Positions PM section and
targeted queries against the same active Workday company registry, plus a
PM-only parser for Zapply's public New-Grad-Jobs-2027 GitHub board. Zapply is
noisy globally, so only the parser's PM rows receive provisional new-grad
visibility evidence; the PM gate remains dashboard-only either way. Solutions
Architect titles are allowed into this lane only so an entry-level/new-grad
posting is visible; generic non-PM architect and manager titles remain hard
gated.

Victor also explicitly chose Google technical new-grad roles to display at
100. That is a profile-configured, auditable score override excluded for PM
roles, so the friend-facing PM lane remains low even at Google.
