# Chemical Engineering Internship Radar roadmap

## AI/QoL release status (2026-07-18)

1. **AI foundation — ✅ shipped.** Task-aware NVIDIA routing, hard 8-call /
   12-request ChemE nightly budget, fallback/cooldowns, schema checks, local
   Ollama bulk lane, and secret-free usage/model-health telemetry.
2. **Company research — ✅ v1 shipped.** Official posting excerpts feed cited,
   cached employer briefs with explicit unknowns, candidate relevance, and
   ChemE interview focus. Uncited culture estimates no longer affect rank.
3. **Google Sheets tracking — ✅ code shipped / OAuth pending.** Stable-ID
   upsert and stage readback are selectable with `TRACKER_BACKEND`; Notion now
   reads manual stage edits back too. See `docs/GOOGLE_SHEETS_SETUP.md`.
4. **Interview workspace — ✅ v1 shipped.** OA/interview stages have grounded
   company context and a practical ChemE prep checklist.

## Deliberately deferred

1. **RAG and vector search.** Embed postings, company dossiers, candidate
   profile/CV material, and saved decisions for semantic search and explainable
   similarity ranking. Deterministic gates remain authoritative.
2. **CV-aware target-role toggle.** When a CV exists, add a `CV` option to the
   existing “all target roles” dropdown and offer local review-only tailoring.
   CV content remains private and never enters public state.

## Shipped on this branch (2026-07-18)

- ✅ **ChemE internship profile and rules v3** — seven role families, ChemE
  sector model, internship/co-op eligibility, direct employer seed set, and
  current internship aggregator source. Adjacent engineering stays optional;
  software/business internship noise is rejected. See DECISIONS #37.
- ✅ **Eligibility-first platform** — sponsorship and experience filters,
  posting facts before research content, ChemE role filters, better application
  actions, and ChemE-specific search/outreach links.
- ✅ **International-student-safe behavior** — explicit no-sponsorship
  language demotes alerts when `needs_sponsorship` is enabled; silence stays
  `unknown` and remains inspectable.
- ✅ **Inherited-state isolation** — the old registry/history is retained,
  while irrelevant sectors and tech culture dossiers cannot drive this branch.
- ✅ Existing platform infrastructure remains: ATS discovery, posting-text
  scraping, optional LLM review, Notion/email integration, GitHub alerts,
  response-rate analytics, and fork-per-person isolation.

## Activation checklist (human/GitHub-side)

- Keep the default-branch `cheme-*` orchestrators enabled; GitHub checks out
  this branch for its crawl, reconcile, daily-best, and nightly enrich jobs.
- Fill candidate identity, graduation year, location preferences, and exact
  Notion status/position option names in `profile.yaml`.
- Run `tests`, `notion-verify`, then one manual `radar` workflow.
- Optional: complete Google OAuth to choose Sheets instead of the shared Notion
  tracker. The NVIDIA hosted layer is already configured by the repository.

## Next product work

1. **Official sponsorship evidence layer.** Join employers to public DOL LCA
   disclosure data and distinguish posting-specific language from employer
   history. Never label a specific role as sponsoring from company history.
2. **Academic-term controls.** Filters for summer/fall/spring, internship vs
   co-op, graduation window, and required enrollment/return-to-school wording.
3. **Location and relocation clarity.** Normalize plant/site location, housing,
   relocation, transportation, and on-site requirements from posting text.
4. **Application-effort view.** Surface required cover letter, transcript,
   assessment, and duplicate employer-portal account friction before applying.
5. **ChemE profile similarity.** Optional local embeddings between posting text
   and a private resume/project inventory, with the signal logged and personal
   files kept outside Git.
6. **Interview preparation.** When a job reaches Interview, create a ChemE
   checklist: process fundamentals, safety, scale-up, unit operations, STAR
   examples, and employer-specific technical themes.

## Deliberately out of scope

- Auto-submitting applications or fabricating application answers.
- Logged-in LinkedIn scraping.
- Treating missing sponsorship language as positive evidence.
- Committing resumes, transcripts, API keys, or personal application content.
