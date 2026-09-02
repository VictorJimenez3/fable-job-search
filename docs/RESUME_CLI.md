# Resume Studio CLI guide

This is the local quick-start for Victor's Resume Studio. The CLI can inspect
the bank, choose the best existing PDF for a role entirely offline, start the
browser service, approve a reviewed draft, and keep a clean Finder-friendly
copy of recent PDFs.

## Copy/paste cheat sheet

Run these from `/Users/victor/fable-job-search`:

```bash
# See every Resume Studio command
.venv/bin/python -m radar.cli resume-studio --help

# List the local bank (no Codex usage)
.venv/bin/python -m radar.cli resume-studio bank

# Find and copy the best APPROVED existing PDF for an ad-hoc role (no Codex)
.venv/bin/python -m radar.cli resume-studio offline-tailor \
  --company "Acme" --title "Machine Learning Engineer"

# Add a saved text job description for a better match
.venv/bin/python -m radar.cli resume-studio offline-tailor \
  --company "Acme" --title "Machine Learning Engineer" \
  --description-file /absolute/path/to/job-description.txt

# Match a role already in state/jobs.json
.venv/bin/python -m radar.cli resume-studio offline-tailor --job-id JOB_ID

# Inspect the best review-pending/legacy match when no approved PDF exists
.venv/bin/python -m radar.cli resume-studio offline-tailor \
  --company "Acme" --title "Backend Engineer" --include-review

# Show observed Resume Studio provider usage
.venv/bin/python -m radar.cli resume-studio usage
```

`offline-tailor` is intentionally conservative: by default it uses only an
explicitly owner-approved tailored winner. `--include-review` can copy an
unapproved match for inspection, but that file must be reviewed before an
application. The selected PDF is copied unchanged to
`CV/tailored/offline/victor_jimenez_<target-company>.pdf`; the command does not
rewrite bullets or make any Codex call.

Yes, the offline commands still work if the five-hour or weekly Codex allowance
is exhausted. `bank`, `offline-tailor`, `export`, `approve`, and `usage` are
local deterministic operations. Creating or rewriting a new tailored resume
still needs Codex and will wait until provider access is available again.

## One-time setup

From the repository root:

```bash
bash scripts/resume-studio-service/install.sh
```

That installs Resume Studio as a login service on this Mac. Afterward, the
local page should be available at <http://127.0.0.1:4317/>.

If you do not want a login service, run it only when needed:

```bash
.venv/bin/python scripts/resume_studio.py
```

Leave that terminal open while Resume Studio is working. Stop it with
`Ctrl-C`.

## Private Projects workspace

The browser service's **Projects** view is the local Overleaf-style editor. It
discovers protected and tailored references without moving them, and creates
new editable projects below `CV/.resume_studio/projects/`. Clone a reference
before editing; only managed private projects accept raw source changes. The
editor supports `.tex`, `.bib`, `.sty`, `.cls`, `.md`, and `.txt` plus PNG,
JPEG, and PDF assets, with bounded file/project sizes. Saves are SHA-checked,
and every save, build, delete, and restore is retained in append-only history.
Compile from the view with **Recompile**. The output is a local
`workspace_draft` and is never an approved/application artifact automatically.

## Everyday workflow

1. Open <http://127.0.0.1:4317/>.
2. Select a posting, or open Resume Studio from the owner view on Job Radar.
3. Choose **AI tailor**, **Take-the-wheel (moderate)**, **Used bullets**, or
   **Unchained generation**. The mode name is only a UI choice; it is never
   written into the PDF filename.
4. Wait for the run to reach review, inspect the PDF and report, then choose
   **Approve final PDF** only when it is ready.
5. Open the local folder:

   ```bash
   open CV/tailored
   ```

   Recent primary PDFs are named like `victor_jimenez_nvidia.pdf` and
   `victor_jimenez_johnson_johnson.pdf`.

AI tailor and Take-the-wheel both start with the role-and-company brief now;
Unchained adds the deepest Markdown evidence pass. The report's
`tailoring_brief` lists essential capabilities, exact ATS terms, ideal
evidence/project surfaces, company-domain priorities, and honest gaps.
`generation_strategy` is the normalized requirement → evidence map. For a
medical or healthcare employer, inspect the brief to see which verified
medical assignments were compared with generic projects. Company dossier
text is routing context only and cannot become a resume accomplishment.

## Refresh the easy-to-find folder

The service refreshes `CV/tailored/` after a usable run finishes. To refresh it
manually and keep the newest usable PDF per company from the last 14 days:

```bash
.venv/bin/python -m radar.cli resume-studio export
open CV/tailored
```

To include the newest usable run for every company in the private history:

```bash
.venv/bin/python -m radar.cli resume-studio export --all-history
```

The folder contains one primary PDF per company, a preview PNG when available,
and `index.json` with the source run, role, date, and whether owner review is
still required. `awaiting_review` files are for inspection; use an approved
PDF for an application. Offline role selections live below this same location
in `CV/tailored/offline/`, with their source run and unchanged-content receipt
in `CV/tailored/offline/index.json`.

## Platform batch and direct editing

For Victor's synced Notion queue, sign in to the platform, open **Resume
Studio**, and choose **Tailor all To tailor**. Confirm the batch, choose a mode,
and queue it. This creates one private draft per current `To tailor` role; it
does not submit applications or mark anything Applied. If the count is stale,
run the existing Notion tracker sync/backfill first, then refresh the page.

To edit a saved resume without finding source files in VS Code, open **Resume
bank**, expand the job card, and choose **Edit resume** beside the saved PDF.
That opens the protected local Workshop through the platform. Edit a line and
choose **Save line** to render a new private revision. The original PDF and
canonical resume stay untouched. Manual edits do not use Codex; **Ask AI about
this** is optional and may use Codex.

## Resume Bank and approval

The bank command hashes PDFs so duplicate run artifacts appear once, excludes
failed runs and runs whose audit selected the canonical base, and clearly marks
each remaining entry `APPROVED` or `REVIEW`:

```bash
.venv/bin/python -m radar.cli resume-studio bank --limit 50
.venv/bin/python -m radar.cli resume-studio bank --approved-only
.venv/bin/python -m radar.cli resume-studio bank --query "machine learning"
```

After personally inspecting a run's PDF and gate report, approve a ready run
from the browser or CLI:

```bash
.venv/bin/python -m radar.cli resume-studio approve 0123456789ab
```

Approval fails closed if the run is not awaiting review, its quality gates are
not ready, or its PDF is missing. The automatic application fallback only uses
an explicitly approved tailored winner selected by the same offline role-fit
scorer; review-pending historical files never silently become upload choices.

## Useful checks

```bash
# Show the command groups and options
.venv/bin/python -m radar.cli --help
.venv/bin/python -m radar.cli resume-studio --help
.venv/bin/python -m radar.cli resume-studio offline-tailor --help

# Check whether the local service is awake
curl http://127.0.0.1:4317/api/health

# See the private run library in the browser
open http://127.0.0.1:4317/
```

The durable run history, reports, prompts, and diagnostic candidates remain in
`CV/.resume_studio/`. Do not delete that folder while a run is queued or while
you want Resume Bank history and recovery to work. `CV/immutable/` contains the
protected canonical resumes and is never overwritten by tailoring.

## If something is stuck

Restart the service so queued work can recover:

```bash
bash scripts/resume-studio-service/install.sh
curl http://127.0.0.1:4317/api/health
```

If the page says the source checkout is stale, restart the service with the
same install command. If a run fails, its report and `error.log` remain under
`CV/.resume_studio/runs/<run-id>/`; export skips failed runs.

For the broader Job Radar operator CLI, see [`CLI_HANDOFF.md`](CLI_HANDOFF.md).
