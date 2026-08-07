# Google Sheets tracker setup

The tracker supports two modes. The existing **Applications** tab is the
owner/fork automation tracker. The **User Applications** tab powers the shared
Vercel platform: any user who signs in with GitHub gets rows keyed to their
GitHub login, while the backend keeps the Google credentials and filters rows
before returning them. Users never receive the Sheet URL or the Google token.
GitHub Pages cannot provide this private multi-user mode; use the Vercel
platform URL.

## What the adapter does

- stable-ID upsert, so reruns do not duplicate applications;
- columns for company, role, stage, URL, location, dates, source, and board;
- stage readback for `saved`, `applied`, `oa`, `interview`, `rejected`, and
  `closed`;
- the same main/ChemE application model; no second Notion database is created;
- a private shared-user tab keyed by `GitHub User` + `Job Radar ID`, with
  `maybe`, `saved`, and `applied` actions isolated per GitHub login.

## One-time authorization

1. Create or select a project in Google Cloud Console.
2. Enable **Google Sheets API** and **Google Drive API**.
3. Configure the OAuth consent screen. Add the Google account that will own the
   tracker as a test user if the app remains in testing.
4. Create an OAuth client (Desktop app is simplest for a one-person setup).
5. Complete one consent flow with scope
   `https://www.googleapis.com/auth/spreadsheets`, then exchange the returned
   authorization code for a refresh token.
6. Create the workbook automatically after exporting the refresh token:

   ```bash
   .venv/bin/python -m radar.main create-google-tracker
   ```

   The command creates `Applications`, `User Applications`, and `Guide` tabs,
   freezes and formats the headers, adds filters, and prints the spreadsheet
   ID/URL. It never overwrites an existing workbook. If you prefer to create
   one manually, keep those two tab names and copy the spreadsheet ID from the
   URL between `/d/` and `/edit`.

Add GitHub Actions **secrets**:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `GOOGLE_SHEET_ID`

Add repository **variables**:

- `TRACKER_BACKEND=google_sheets`
- `GOOGLE_SHEET_TAB=Applications`

For the shared GitHub-user tracker, add the same Google values to the Vercel
project environment and set:

- `GOOGLE_USER_SHEET_TAB=User Applications`

The Vercel backend accepts any authenticated GitHub user for `/api/tracker`,
but only reads/writes rows whose first column matches that user's GitHub login.
The owner continues using the repository/Notion or `Applications` flow; other
users use Google Sheets directly through the secure backend and do not create
GitHub commits or change the owner's `state/applied.json`.

Then run **reconcile-checkboxes** and **cheme-reconcile-checkboxes** manually.
The adapter creates the header row and syncs each board's own entries by Job
Radar ID. To return to the existing tracker, set `TRACKER_BACKEND=notion`;
the Notion token and pages are left intact.

Never paste OAuth values into `profile.yaml`, `.env.local`, an issue, the
frontend, or a public spreadsheet. They belong only in GitHub encrypted secrets
and Vercel environment variables. Keep the created workbook private; the
platform provides the per-user view.
