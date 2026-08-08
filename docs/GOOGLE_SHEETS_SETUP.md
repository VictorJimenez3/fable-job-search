# Google Sheets tracker setup

The tracker supports two modes. The existing **Applications** tab is the
owner/fork automation tracker. On Vercel, each user who connects Google gets a
separate **Applications** workbook created in that user's Google Drive. The
private owner-controlled **Accounts** tab stores identity links, an encrypted
refresh-token ciphertext, and each user's Sheet ID so the backend can reconnect
without exposing tokens to the browser. GitHub-only users can connect Google
later; GitHub Pages cannot provide this private multi-user mode.

## What the adapter does

- stable-ID upsert, so reruns do not duplicate applications;
- columns for company, role, stage, URL, location, dates, source, and board;
- stage readback for `saved`, `applied`, `oa`, `interview`, `rejected`, and
  `closed`;
- the same main/ChemE application model; no second Notion database is created;
- one private workbook per connected Google account, with `maybe`, `saved`, and
  `applied` actions isolated by ownership in Google Drive;
- a Notion-shaped visible schema: `Company`, `Stage`, `Position`, `Apply date`,
  `Text`, `Job URL`, and `Location`, followed by tracker metadata;
- an `Accounts` tab that prevents the same GitHub/Google identity from becoming
  two linked accounts, records explicit OAuth-proven merges, and stores only
  encrypted Google token material plus Sheet IDs for reconnecting users.

## One-time authorization

1. Create or select a project in Google Cloud Console.
2. Enable **Google Sheets API** and **Google Drive API**.
3. Configure the OAuth consent screen as an **External** app. Under **Data
   Access**, add only `https://www.googleapis.com/auth/drive.file`; do not add
   the broader `spreadsheets` scope. `drive.file` is Google's recommended
   non-sensitive, per-file scope for an app that creates and manages its own
   Sheets workbook.
4. Set the publishing status to **In production**. This is what makes the
   OAuth client available to arbitrary Google accounts; a test-user list is
   only for development. Google may still request basic brand verification if
   you want the app's name/logo shown on the consent screen, but this narrow
   scope avoids sensitive-scope verification.
5. Create a **Web application** OAuth client and register
   `https://<your-canon-host>/api/google-callback` as an authorized redirect URI.
6. The Vercel login flow requests `openid email profile` plus
   `https://www.googleapis.com/auth/drive.file` with offline access. Each user
   consents once; the resulting refresh token is used to create and maintain
   only that user's own workbook. No user runs the local creator command.
7. The owner-only local command remains available for the legacy automation
   workbook and metadata registry:

   ```bash
   .venv/bin/python -m radar.main create-google-tracker
   ```

   The command creates `Applications`, `User Applications`, `Accounts`, and `Guide` tabs,
   freezes and formats the headers, adds filters, and prints the metadata
   workbook ID/URL. It never overwrites an existing workbook. User-owned
   workbooks are created automatically by Google OAuth during sign-in.

Add GitHub Actions **secrets**:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `GOOGLE_SHEET_ID`

Add repository **variables**:

- `TRACKER_BACKEND=google_sheets`
- `GOOGLE_SHEET_TAB=Applications`

For the owner metadata registry, add the same Google values to the Vercel
project environment and set:

- `GOOGLE_USER_SHEET_TAB=User Applications`
- optionally `GOOGLE_ACCOUNT_SHEET_TAB=Accounts`
- optionally `GOOGLE_PERSONAL_SHEET_TAB=Applications` (the per-user workbook tab)

For OAuth login, add a Google **Web application** OAuth client with this
callback URL and add its values to Vercel:

- `GOOGLE_AUTH_CLIENT_ID`
- `GOOGLE_AUTH_CLIENT_SECRET`

The callback is `https://<your-canon-host>/api/google-callback`. If the auth
variables are absent, the backend falls back to `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET`, but those credentials must also have the callback URL
registered. The existing GitHub OAuth callback remains
`https://<your-canon-host>/api/callback`.

The Vercel backend accepts authenticated GitHub or Google users for
`/api/tracker`. A person signs in with one provider, then opens **Tutorial →
Accounts & login → Connect Google + create my Sheet** to add Google access.
Google sign-in itself also requests Sheets access. The app never asks for a
password, and the user can open their personal Sheet from the Account center.
If a provider is already attached to another account, it refuses a silent
reassignment and only merges after both identities have been proven by OAuth.
Other users do not create GitHub commits or change the owner's
`state/applied.json`.

Then run **reconcile-checkboxes** and **cheme-reconcile-checkboxes** manually.
The adapter creates the header row and syncs each board's own entries by Job
Radar ID. To return to the existing tracker, set `TRACKER_BACKEND=notion`;
the Notion token and pages are left intact.

Never paste OAuth values into `profile.yaml`, `.env.local`, an issue, the
frontend, or a public spreadsheet. They belong only in GitHub encrypted secrets
and Vercel environment variables. Keep the created workbook private; the
platform provides the per-user view.
