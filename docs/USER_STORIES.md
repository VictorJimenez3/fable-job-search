# Job Radar user stories and acceptance gaps

This is the feature-level implementation contract for future CLIs. A roadmap
item says *what* exists or is deferred; each story below says what Victor
should be able to do, what “done” means, and what is still wrong. Update the
story when behavior changes, then update `docs/CLI_HANDOFF.md` with the next
concrete task.

Every implementation note or story update should include provenance: agent,
CLI surface, model/provider, scope, confidence, validation, and human follow-up.
This prevents a later CLI from treating an unreviewed proxy-model edit as if it
were a tested Codex or human decision.

## Active personal-board stories

### AI-assisted job review — partially shipped

**As Victor, I want each alert-worthy posting checked against my new-grad,
technical-role, and experience requirements so that weak alerts are demoted
with an explanation.**

Done means the verdict is cached, source text is identifiable, every penalty is
auditable, deterministic gates remain authoritative, and provider failure falls
back safely. The local catch-up queue is currently clear. The remaining gap is
depth: most non-alert jobs still have no AI verdict, and AI is not yet a full
semantic match to Victor’s experience. Progress: lowered minimum score threshold from 60 to 40 to increase LLM coverage beyond borderline cases; enhanced LLM rerank to process top 50 alert-worthy jobs by score for semantic ranking and application notes.

### Personal ranking and alert focus — partially shipped

**As Victor, I want alerts limited to AI/ML, data science, and AI-leaning
software roles I would actually consider, without full-stack/systems noise.**

Done means the same role policy applies to weekly, daily, and master-board
surfaces; excluded roles remain searchable on the dashboard; and every
demotion has a reason. Remaining gap: the ranking is still mostly deterministic
and needs deeper evidence from Victor’s profile and feedback.

### Company research — V1 shipped, improvement needed

**As Victor, I want to understand an unfamiliar company before opening or
applying to a role: products, customers, mission, technical work, visa context,
and interview relevance, each backed by evidence.**

Done means bounded official-posting excerpts, citations/freshness, and “Not
confirmed” instead of invented facts. Remaining gap: briefs can still be too
short or generic; improve synthesis and company-level context without turning
model memory into evidence. Progress: increased excerpt length from 4 to 5 sentences to capture more relevant company information.

### Posting eligibility facts — shipped

**As a student who may need sponsorship, I want years-of-experience,
internship-counting, sponsorship, and unknown states visible before I apply.**

Done means the facts appear in rows, filters, the drawer, and alert lines, with
“not stated” distinct from “not analyzed.” Remaining gap: hostile/empty ATS
pages still require a pasted JD or later fetch support.

### Apply and tracker flow — shipped, polish remains

**As Victor, I want one clear apply action, an explicit Applied action, and a
reliable To-apply tracker entry without duplicate rows.**

Done means authenticated apply saves idempotently, checkbox tracking remains
not-applied, and Notion/Sheets readback preserves later stage edits. Remaining
gap: Google Sheets OAuth still needs one-time activation and the UX can make
stage semantics clearer.

### Email/application autopilot — coded, credentials pending

**As Victor, I want application-confirmation, OA, interview, rejection, and
long-silence emails to advance the tracker forward-only.**

Done means the watcher is read-only to inbox content, matches companies
carefully, never regresses a stage, and auto-closes only after the configured
period. Remaining gap: `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` are not active
in the live workflow, so this cannot be considered live until the human setup
is completed.

### Local/cloud AI operations — shipped foundation

**As Victor, I want local Ollama work to use free Mac compute while bounded
cloud providers handle overflow, without one provider exhausting the account.**

Done means task routing, hard budgets, retries, cooldowns, telemetry, provider
rotation, and local/cloud parallelism are present. Remaining gap: provider
quotas are not introspectable; DeepSeek is unreliable and Kimi is unavailable,
so hosted usage must stay conservative.

## Deferred stories

### Semantic/RAG matching — parked

**As Victor, I want a semantic search that explains why a posting matches my
projects, profile, saved decisions, and eventual CV—not just title keywords.**

Acceptance: local embeddings, auditable similarity reasons, deterministic gates
still authoritative, stale vectors invalidated, and no CV content in public
state. Requires a stronger model/design pass before implementation.

### CV-aware roles and tailoring — on hold

**As Victor, I want a CV option in the target-role selector and a local,
review-only resume draft using the strongest relevant evidence for that role.**

Acceptance: CV stays in `CV/`, drafts never auto-submit, every selected bullet
is traceable to source material, and the feature is Mac-only.

### Interview preparation — V1 workspace shipped, deeper version deferred

**As Victor, I want a company packet generated from the company name and role,
including mission, technical context, likely interview themes, and a prep plan.**

Acceptance: evidence links and freshness are shown, unsupported claims are
marked unknown, and interview-specific research does not alter job ranking.

### Multi-user onboarding — second phase

**As another GitHub user, I want onboarding that collects my major, role goals,
tracker, resume/CV choice, and AI credentials, then gives me an isolated
generic or specialized radar.**

Acceptance: no user can spend Victor’s keys or see Victor’s private state;
non-owner defaults are generic; fork-per-person isolation remains intact until
the later GitHub-App architecture is deliberately designed.

## Known issues / gaps to carry forward

1. AI currently verifies and enriches selected jobs; it is not yet a semantic,
   profile-aware ranking engine for every posting.
2. Most baseline scores and alert reasons remain deterministic, so a posting
   can have no AI note even when it is correctly ranked.
3. Company briefs are evidence-grounded but can feel generic for unfamiliar
   employers.
4. DeepSeek timeouts and Kimi 404s reduce hosted-provider reliability; the
   account’s “40 RPM” number has no documented scope.
5. The platform’s AI visibility indicator counts partial quality records, so it
   can overstate completed verdict coverage.
6. Email autopilot is implemented but not live until the inbox credentials are
   configured.
7. Google Sheets is implemented but not activated until OAuth is completed.
8. RAG/vector search and CV tailoring are intentionally deferred, not missing
   by accident.
9. Multi-user onboarding is intentionally deferred; current non-owner behavior
   must not be mistaken for the final product.
