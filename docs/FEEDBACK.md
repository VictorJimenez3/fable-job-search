# Ranking taste feedback

> Owner-only write path: only the repository owner’s GitHub identity can submit feedback.
> This public repository can still be read by anyone; do not put secrets or private notes here.

## How ranking changes

- Saved roles are the implicit positive sample. They add small, capped company/title signals.
- Explicit **more like this** feedback adds a capped company and/or title signal.
- Explicit **less like this** feedback only downranks a company or title when that reason is selected.
- Eligibility and location feedback is recorded for review but never overrides deterministic gates.
- Every change remains visible in each job’s score-reason ledger after the next rescore.

## Current learned signals

### Companies

| company | boost |
| --- | ---: |
| amazon | +8 |
| anthropic | +8 |
| apple | +8 |
| cvs health | +8 |
| google | +8 |
| medtronic | +8 |
| neuralink | +8 |
| notion | +8 |
| nvidia | +8 |
| openai | +8 |
| quora | +8 |
| rtx | +8 |
| intuit | +7 |
| whoop | +7 |
| capital one | +6 |
| fuze health | +6 |
| ge healthcare | +6 |
| microsoft | +6 |
| pinterest | +6 |
| spacex | +6 |
| stubhub | +6 |
| the walt disney company | +6 |
| cisco | +5 |
| roblox | +5 |
| altera digital health | +4 |
| cadence | +4 |
| capricor therapeutics | +4 |
| fanatics | +4 |
| generate biomedicines | +4 |
| hp | +4 |

### Title tokens

| token | signal |
| --- | ---: |
| 2026 | +4 |
| 2027 | +4 |
| amazon | +4 |
| analyst | +4 |
| analytics | +4 |
| applications | +4 |
| applied | +4 |
| associate | +4 |
| backend | +4 |
| cloud | +4 |
| college | +4 |
| compiler | +4 |
| data | +4 |
| deployment | +4 |
| developer | +4 |
| development | +4 |
| digital | +4 |
| embedded | +4 |
| engineering | +4 |
| graduate | +4 |
| inference | +4 |
| infrastructure | +4 |
| junior | +4 |
| learning | +4 |
| leo | +4 |
| machine | +4 |
| performance | +4 |
| platform | +4 |
| research | +4 |
| scientist | +4 |
| stack | +4 |
| systems | +4 |
| advanced | +3 |
| bci | +3 |
| campus | +3 |
| design | +3 |
| healthcare | +3 |
| intelligence | +3 |
| neuro | +3 |
| optimization | +3 |

### Downranked companies

None.

## Recent explicit feedback

No explicit feedback yet. The saved-role sample is still active.
