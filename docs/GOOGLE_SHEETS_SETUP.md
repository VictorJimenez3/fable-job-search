# Optional Google Sheets tracker setup

The code is ready, but the live board still uses Notion until you authorize a
Google account. This is a one-time Google-side step; GitHub Actions needs a
refresh token because it runs unattended with no browser.

## What the adapter does

- stable-ID upsert, so reruns do not duplicate applications;
- columns for company, role, stage, URL, location, dates, source, and board;
- stage readback for `saved`, `applied`, `oa`, `interview`, `rejected`, and
  `closed`;
- the same main/ChemE application model; no second Notion database is created.

## One-time authorization

1. Create or select a project in Google Cloud Console.
2. Enable **Google Sheets API** and **Google Drive API**.
3. Configure the OAuth consent screen. Add the Google account that will own the
   tracker as a test user if the app remains in testing.
4. Create an OAuth client (Desktop app is simplest for a one-person setup).
5. Complete one consent flow with scope
   `https://www.googleapis.com/auth/spreadsheets`, then exchange the returned
   authorization code for a refresh token.
6. Create a spreadsheet and an `Applications` tab. Copy the spreadsheet ID
   from the URL between `/d/` and `/edit`.

Add GitHub Actions **secrets**:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `GOOGLE_SHEET_ID`

Add repository **variables**:

- `TRACKER_BACKEND=google_sheets`
- `GOOGLE_SHEET_TAB=Applications`

Then run **reconcile-checkboxes** and **cheme-reconcile-checkboxes** manually.
The adapter creates the header row and syncs each board's own entries by Job
Radar ID. To return to the existing tracker, set `TRACKER_BACKEND=notion`;
the Notion token and pages are left intact.

Never paste OAuth values into `profile.yaml`, `.env.local`, an issue, or the
public spreadsheet. They belong only in GitHub encrypted secrets.
