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
