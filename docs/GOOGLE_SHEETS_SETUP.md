# Google Sheets tracker setup

The tracker supports two modes. The existing **Applications** tab is the
owner/fork automation tracker. The **User Applications** tab powers the shared
Vercel platform: any user who signs in with GitHub or Google gets rows keyed to
one linked account, while the backend keeps the Google credentials and filters
rows before returning them. The private **Accounts** tab stores only OAuth
identity links and merge metadata. Users never receive the Sheet URL or the
Google token. GitHub Pages cannot provide this private multi-user mode; use the
Vercel platform URL.

## What the adapter does

- stable-ID upsert, so reruns do not duplicate applications;
- columns for company, role, stage, URL, location, dates, source, and board;
- stage readback for `saved`, `applied`, `oa`, `interview`, `rejected`, and
  `closed`;
- the same main/ChemE application model; no second Notion database is created;
- a private shared-user tab keyed by `Account ID` + `Job Radar ID`, with
  `maybe`, `saved`, and `applied` actions isolated per linked account;
- a Notion-shaped visible schema: `Company`, `Stage`, `Position`, `Apply date`,
  `Text`, `Job URL`, and `Location`, followed by tracker metadata;
- an `Accounts` tab that prevents the same GitHub/Google identity from becoming
  two linked accounts and records explicit, OAuth-proven merges.

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

   The command creates `Applications`, `User Applications`, `Accounts`, and `Guide` tabs,
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

For the shared tracker, add the same Google values to the Vercel project
environment and set:

- `GOOGLE_USER_SHEET_TAB=User Applications`
- optionally `GOOGLE_ACCOUNT_SHEET_TAB=Accounts`

For OAuth login, add a Google **Web application** OAuth client with this
callback URL and add its values to Vercel:

- `GOOGLE_AUTH_CLIENT_ID`
- `GOOGLE_AUTH_CLIENT_SECRET`

The callback is `https://<your-canon-host>/api/google-callback`. If the auth
variables are absent, the backend falls back to `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET`, but those credentials must also have the callback URL
registered. The existing GitHub OAuth callback remains
`https://<your-canon-host>/api/callback`.

The Vercel backend accepts any authenticated GitHub or Google user for
`/api/tracker`. A person signs in with one provider, then opens **Tutorial →
Accounts & login → Connect** to add the other. The app never asks for a
password. If a provider is already attached to another account, it refuses a
silent reassignment and only merges after both identities have been proven by
OAuth. Users do not need to open Sheets: the app is the filtered front door.
The owner continues using the repository/Notion or `Applications` flow; other
users do not create GitHub commits or change the owner's `state/applied.json`.

Then run **reconcile-checkboxes** and **cheme-reconcile-checkboxes** manually.
The adapter creates the header row and syncs each board's own entries by Job
Radar ID. To return to the existing tracker, set `TRACKER_BACKEND=notion`;
the Notion token and pages are left intact.

Never paste OAuth values into `profile.yaml`, `.env.local`, an issue, the
frontend, or a public spreadsheet. They belong only in GitHub encrypted secrets
and Vercel environment variables. Keep the created workbook private; the
platform provides the per-user view.
