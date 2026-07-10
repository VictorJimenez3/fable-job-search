# Roadmap — designed, deliberately deferred

The dream-system backlog. Each item is scoped enough to build on request;
none block anything currently running.

## Pipeline intelligence
- **Dead-application auto-close** — applied 30+ days with no response email →
  flip Notion Stage to CLOSED and note it in the Monday memo. (Email watcher
  already sees responses; this is one rule away.)
- **Response-rate analytics** — per-sector/per-source conversion rates in the
  strategist memo: "healthtech replies 3× more than big tech for you — shift
  volume." Becomes meaningful after ~30 tracked applications.
- **Interview-loop dossiers** — when Stage hits Interview, auto-generate the
  company's known loop structure, question themes, and prep checklist
  (local-LLM job, so it's free on the Mac).
- **Offer-comparison calculator** — feeds the Offer Playbook scorecard with
  real numbers: equity discounting, COL adjustment, side-by-side memo.

## Reach
- **Referral-finder** — cross NJIT alumni signals against target companies;
  drafts the outreach note into the Networking CRM. (Needs a data source that
  isn't LinkedIn scraping — evaluating SHPE directory + GitHub org members.)
- **H-1B/sponsorship cross-check** — join companies against the public DOL
  LCA disclosure data; add a `sponsors: likely/no-history` column. Cheap,
  official data. Build if sponsorship ever becomes a filter.
- **Meta careers adapter** — auth-gated GraphQL today; revisit if they ship a
  public search endpoint. Covered by aggregators meanwhile.
- **SHPE deep mode** — rep CRM per booth, session planner, live exhibitor sync
  from careercenter.shpe.org, SHPExchange resume-book optimizer. Light mode
  (exhibitor boost + battle plan) shipped in `docs/SHPE.md`.

## Content
- **Opt-in resume tailoring** — the Mac companion drafts a per-role bullet
  rewrite into a review file; never auto-submitted (DECISIONS §6 stands: the
  human stays the author, the machine drafts).
- **Auto cover-letter skeletons** for S-tier roles only, same review-file flow.

## Ops
- **Calendar sync** — interview emails → Google Calendar holds.
- **Weekly Notion rollup** — mirror the Monday memo into Job Search HQ.
- **Registry hygiene job** — monthly: retry long-dead boards, prune 90-day
  invalids, dedupe companies that appear under two ATSs.
