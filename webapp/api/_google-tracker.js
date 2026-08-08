// Per-account Google Sheets tracker.
//
// User application rows live in a separate workbook created in each user's
// own Google Drive. The user's refresh grant and Sheet ID travel only inside
// the server-sealed, httpOnly session cookie, so the owner registry is an
// optional account-linking enhancement rather than a single point of failure.
// Legacy registry tokens remain encrypted and never enter frontend JavaScript.
const crypto = require("crypto");
const { envv, seal, unseal, GOOGLE_AUTH_CLIENT_ID, GOOGLE_AUTH_CLIENT_SECRET,
  googleAuthConfigured } = require("./_lib");

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets";
const DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files";
const TRACKER_PROPERTY = "jobRadarTracker";
const TRACKER_PROPERTY_VALUE = "v1";
const formattedPersonalTrackers = new Set();

// The personal workbook is intentionally Notion-shaped but does not expose
// internal account keys because the user owns and can open this Sheet.
const PERSONAL_HEADERS = ["Job Radar ID", "Company", "Stage", "Position", "Apply date",
  "Text", "Job URL", "Location", "Saved At", "Updated At", "Source", "Profile"];
const PREFERENCE_HEADERS = ["Preference", "Value", "Updated At"];
const ACCOUNT_HEADERS = ["Account ID", "GitHub ID", "GitHub Login", "Google Subject",
  "Google Email", "Created At", "Updated At", "Merged Into", "Status",
  "Google Token Ciphertext", "Google Sheet ID", "Google Connected At"];
const VALID_STAGES = new Set(["saved", "applied", "oa", "interview", "rejected", "closed", "maybe", "archived"]);

// These credentials are the owner-only metadata registry credentials. They
// are still required so the backend can persist account links safely.
const configured = () => ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN",
  "GOOGLE_SHEET_ID", "SESSION_SECRET"].every(k => envv(k));
const personalConfigured = () => Boolean(googleAuthConfigured() && envv("SESSION_SECRET"));
const accountTab = () => envv("GOOGLE_ACCOUNT_SHEET_TAB") || "Accounts";
const sharedUserTab = () => envv("GOOGLE_USER_SHEET_TAB") || "User Applications";
const personalTab = () => envv("GOOGLE_PERSONAL_SHEET_TAB") || "Applications";
const internshipTab = () => "Internships";
const preferencesTab = () => "Preferences";
const tabForProfile = profile => String(profile || "new_grad").toLowerCase() === "internship"
  ? internshipTab() : personalTab();
const ownerSheetId = () => envv("GOOGLE_SHEET_ID");
const authHeaders = token => ({ Authorization: `Bearer ${token}`, "Content-Type": "application/json" });

function columnName(number) {
  let n = number;
  let result = "";
  while (n > 0) {
    const remainder = (n - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    n = Math.floor((n - 1) / 26);
  }
  return result;
}

function sheetUrl(id) { return `https://docs.google.com/spreadsheets/d/${id}/edit`; }

async function accessTokenFor(clientId, clientSecret, refreshToken) {
  const r = await fetch(TOKEN_URL, { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: clientId, client_secret: clientSecret,
      refresh_token: refreshToken, grant_type: "refresh_token" }) });
  if (!r.ok) throw new Error(`Google token ${r.status}`);
  const body = await r.json();
  if (!body.access_token) throw new Error("Google did not return an access token");
  return body.access_token;
}

async function accessToken() {
  return accessTokenFor(envv("GOOGLE_CLIENT_ID"), envv("GOOGLE_CLIENT_SECRET"), envv("GOOGLE_REFRESH_TOKEN"));
}

async function personalAccessToken(account) {
  const stored = unseal(account.googleTokenCiphertext || "");
  if (!stored?.refreshToken) throw new Error("Google is not connected for this account");
  const clientId = GOOGLE_AUTH_CLIENT_ID() || envv("GOOGLE_CLIENT_ID");
  const clientSecret = GOOGLE_AUTH_CLIENT_SECRET() || envv("GOOGLE_CLIENT_SECRET");
  return accessTokenFor(clientId, clientSecret, stored.refreshToken);
}

function hasPersonalSession(personal) {
  return Boolean(personal?.r && personal?.s && personal?.sub);
}

async function personalSessionAccessToken(personal) {
  if (!hasPersonalSession(personal)) throw new Error("Connect Google first to create your personal tracker");
  return accessTokenFor(GOOGLE_AUTH_CLIENT_ID(), GOOGLE_AUTH_CLIENT_SECRET(), personal.r);
}

function rangeUrl(spreadsheetId, range, sheetTab, suffix = "") {
  return `${SHEETS_API}/${spreadsheetId}/values/${encodeURIComponent(`${sheetTab}!${range}`)}${suffix}`;
}

async function readTab(token, spreadsheetId, sheetTab, range = "A:Z") {
  const r = await fetch(rangeUrl(spreadsheetId, range, sheetTab),
    { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`Google Sheet read ${r.status}`);
  return (await r.json()).values || [];
}

async function putRange(token, spreadsheetId, sheetTab, range, values) {
  const r = await fetch(rangeUrl(spreadsheetId, range, sheetTab) + "?valueInputOption=RAW", {
    method: "PUT", headers: authHeaders(token),
    body: JSON.stringify({ range: `${sheetTab}!${range}`, majorDimension: "ROWS", values }) });
  if (!r.ok) throw new Error(`Google Sheet update ${r.status}`);
}

async function appendRange(token, spreadsheetId, sheetTab, range, values) {
  const r = await fetch(rangeUrl(spreadsheetId, range, sheetTab, ":append") +
    "?valueInputOption=RAW&insertDataOption=INSERT_ROWS", {
      method: "POST", headers: authHeaders(token),
      body: JSON.stringify({ majorDimension: "ROWS", values }) });
  if (!r.ok) throw new Error(`Google Sheet append ${r.status}`);
}

async function formatPersonalTracker(token, spreadsheetId, sheets) {
  const apps = sheets.filter(sheet => [personalTab(), internshipTab()].includes(sheet.title));
  const guide = sheets.find(sheet => sheet.title === "Guide");
  const requests = [...apps, guide].filter(Boolean).flatMap(sheet => [
    { repeatCell: { range: { sheetId: sheet.id, startRowIndex: 0, endRowIndex: 1 },
      cell: { userEnteredFormat: { backgroundColor: { red: 0.92, green: 0.92, blue: 0.92 },
        textFormat: { bold: true } } }, fields: "userEnteredFormat(backgroundColor,textFormat)" } },
    { updateSheetProperties: { properties: { sheetId: sheet.id, gridProperties: { frozenRowCount: 1 } },
      fields: "gridProperties.frozenRowCount" } },
    { autoResizeDimensions: { dimensions: { sheetId: sheet.id, dimension: "COLUMNS", startIndex: 0,
      endIndex: sheet.columns } } },
  ]);
  for (const app of apps) {
    requests.push({ setBasicFilter: { filter: { range: { sheetId: app.id, startRowIndex: 0,
      endRowIndex: 10000, endColumnIndex: PERSONAL_HEADERS.length } } } });
    requests.push({ setDataValidation: { range: { sheetId: app.id, startRowIndex: 1,
      endRowIndex: 10000, startColumnIndex: 2, endColumnIndex: 3 },
      rule: { condition: { type: "ONE_OF_LIST", values: [...VALID_STAGES].map(value => ({ userEnteredValue: value })) },
        showCustomUi: true, strict: false } } });
  }
  const r = await fetch(`${SHEETS_API}/${spreadsheetId}:batchUpdate`, {
    method: "POST", headers: authHeaders(token), body: JSON.stringify({ requests }) });
  if (!r.ok) throw new Error(`Google Sheet format ${r.status}`);
}

async function ensurePersonalTabs(token, spreadsheetId) {
  const r = await fetch(`${SHEETS_API}/${encodeURIComponent(spreadsheetId)}?fields=sheets(properties(sheetId,title,gridProperties(columnCount)))`, {
    headers: {Authorization: `Bearer ${token}`},
  });
  if (!r.ok) return;
  const body = await r.json();
  const sheets = body.sheets || [];
  const titles = new Set(sheets.map(sheet => sheet.properties?.title));
  const missing = [internshipTab(), preferencesTab()].filter(title => !titles.has(title));
  if (!missing.length) return;
  const requests = missing.map(title => ({addSheet: {properties: {
    title, gridProperties: {rowCount: title === preferencesTab() ? 20 : 10000,
      columnCount: title === preferencesTab() ? PREFERENCE_HEADERS.length : PERSONAL_HEADERS.length},
  }}}));
  const created = await fetch(`${SHEETS_API}/${encodeURIComponent(spreadsheetId)}:batchUpdate`, {
    method: "POST", headers: authHeaders(token), body: JSON.stringify({requests}),
  });
  if (!created.ok && created.status !== 400) throw new Error(`Google Sheet tabs ${created.status}`);
  if (missing.includes(internshipTab())) {
    await putRange(token, spreadsheetId, internshipTab(), `A1:${columnName(PERSONAL_HEADERS.length)}1`, [PERSONAL_HEADERS]);
  }
  if (missing.includes(preferencesTab())) {
    await putRange(token, spreadsheetId, preferencesTab(), `A1:${columnName(PREFERENCE_HEADERS.length)}1`, [PREFERENCE_HEADERS]);
  }
}

async function repairPersonalTrackerFormat(token, spreadsheetId) {
  if (formattedPersonalTrackers.has(spreadsheetId)) return;
  try {
    const r = await fetch(`${SHEETS_API}/${encodeURIComponent(spreadsheetId)}?fields=sheets(properties(sheetId,title,gridProperties(columnCount)))`, {
      headers: {Authorization: `Bearer ${token}`},
    });
    if (!r.ok) return;
    const body = await r.json();
    const sheets = (body.sheets || []).map(sheet => {
      const properties = sheet.properties || {};
      return {id: properties.sheetId, title: properties.title,
        columns: properties.gridProperties?.columnCount || PERSONAL_HEADERS.length};
    });
    await formatPersonalTracker(token, spreadsheetId, sheets);
    formattedPersonalTrackers.add(spreadsheetId);
  } catch (error) {
    console.warn(`Google tracker formatting deferred: ${String(error.message || error).slice(0, 120)}`);
  }
}

function driveQueryUrl(query) {
  const params = new URLSearchParams({q: query, spaces: "drive", pageSize: "10",
    fields: "files(id,name,createdTime,modifiedTime)"});
  return `${DRIVE_FILES_API}?${params}`;
}

function driveQueryValue(value) {
  return String(value || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

async function markPersonalTracker(token, spreadsheetId) {
  const r = await fetch(`${DRIVE_FILES_API}/${encodeURIComponent(spreadsheetId)}?fields=id`, {
    method: "PATCH", headers: authHeaders(token),
    body: JSON.stringify({appProperties: {[TRACKER_PROPERTY]: TRACKER_PROPERTY_VALUE}}),
  });
  if (!r.ok) console.warn(`Google tracker marker unavailable (${r.status}); title discovery remains enabled`);
}

async function validTrackerCandidate(token, spreadsheetId) {
  try {
    const rows = await readTab(token, spreadsheetId, personalTab(),
      `A1:${columnName(PERSONAL_HEADERS.length)}1`);
    return PERSONAL_HEADERS.every((header, index) => String(rows[0]?.[index] || "") === header);
  } catch { return false; }
}

async function findPersonalTracker(token, label) {
  const title = `Job Radar — ${String(label || "Applications").slice(0, 100)}`;
  const queries = [
    `trashed = false and appProperties has { key='${TRACKER_PROPERTY}' and value='${TRACKER_PROPERTY_VALUE}' }`,
    `trashed = false and mimeType = 'application/vnd.google-apps.spreadsheet' and name = '${driveQueryValue(title)}'`,
  ];
  for (const query of queries) {
    const r = await fetch(driveQueryUrl(query), {headers: {Authorization: `Bearer ${token}`}});
    if (!r.ok) continue;
    const files = (await r.json()).files || [];
    files.sort((a, b) => String(b.modifiedTime || "").localeCompare(String(a.modifiedTime || "")));
    for (const file of files) {
      if (file.id && await validTrackerCandidate(token, file.id)) return file.id;
    }
  }
  return "";
}

async function createPersonalTracker(token, label) {
  const title = `Job Radar — ${String(label || "Applications").slice(0, 100)}`;
  const r = await fetch(SHEETS_API, { method: "POST", headers: authHeaders(token),
    body: JSON.stringify({ properties: { title }, sheets: [
      { properties: { title: personalTab(), gridProperties: { rowCount: 10000, columnCount: PERSONAL_HEADERS.length } } },
      { properties: { title: internshipTab(), gridProperties: { rowCount: 10000, columnCount: PERSONAL_HEADERS.length } } },
      { properties: { title: preferencesTab(), gridProperties: { rowCount: 20, columnCount: PREFERENCE_HEADERS.length } } },
      { properties: { title: "Guide", gridProperties: { rowCount: 20, columnCount: 2 } } },
    ] }) });
  if (!r.ok) throw new Error(`Google Sheet create ${r.status}`);
  const created = await r.json();
  const spreadsheetId = created.spreadsheetId;
  const sheets = (created.sheets || []).map(sheet => ({
    id: sheet.properties.sheetId, title: sheet.properties.title,
    columns: [personalTab(), internshipTab()].includes(sheet.properties.title)
      ? PERSONAL_HEADERS.length : sheet.properties.title === preferencesTab() ? PREFERENCE_HEADERS.length : 2,
  }));
  await putRange(token, spreadsheetId, personalTab(), `A1:${columnName(PERSONAL_HEADERS.length)}1`, [PERSONAL_HEADERS]);
  await putRange(token, spreadsheetId, internshipTab(), `A1:${columnName(PERSONAL_HEADERS.length)}1`, [PERSONAL_HEADERS]);
  await putRange(token, spreadsheetId, preferencesTab(), `A1:${columnName(PREFERENCE_HEADERS.length)}1`, [PREFERENCE_HEADERS]);
  await putRange(token, spreadsheetId, "Guide", "A1:B9", [
    ["Job Radar personal tracker", "How it works"],
    ["Applications", "Your private new-grad application funnel."],
    ["Internships", "Your separate private internship funnel."],
    ["Stage", "saved → applied → oa → interview → rejected/closed."],
    ["Job Radar ID", "Stable ID used to update a row instead of duplicating it."],
    ["Apply date", "Filled when you mark a role applied; safe to edit manually."],
    ["Text", "Short role context carried from the radar; add personal notes if useful."],
    ["Expected graduation", "Set in Preferences to match freshman/sophomore/junior/senior eligibility."],
    ["Privacy", "This workbook is created in your Google Drive and is not shared with other users."],
  ]);
  await formatPersonalTracker(token, spreadsheetId, sheets);
  await markPersonalTracker(token, spreadsheetId);
  return spreadsheetId;
}

async function ensurePersonalTracker(identity, oauth, currentPersonal = null) {
  const sameIdentity = currentPersonal?.sub === String(identity.sub || "");
  const refreshToken = String(oauth.refreshToken || (sameIdentity ? currentPersonal?.r : "") || "").trim();
  if (!refreshToken) throw new Error("Google did not provide durable tracker access. Reconnect and approve Drive access.");
  const token = oauth.accessToken || await accessTokenFor(
    GOOGLE_AUTH_CLIENT_ID(), GOOGLE_AUTH_CLIENT_SECRET(), refreshToken);
  let spreadsheetId = sameIdentity ? String(currentPersonal?.s || "") : "";
  if (spreadsheetId && !await validTrackerCandidate(token, spreadsheetId)) spreadsheetId = "";
  if (!spreadsheetId) spreadsheetId = await findPersonalTracker(token, identity.email);
  const created = !spreadsheetId;
  if (created) spreadsheetId = await createPersonalTracker(token, identity.email);
  if (!created) {
    await ensurePersonalTabs(token, spreadsheetId);
    await repairPersonalTrackerFormat(token, spreadsheetId);
  }
  return {created, personal: {r: refreshToken, s: spreadsheetId,
    e: clean(identity.email, 320).toLowerCase(), sub: String(identity.sub || "")}};
}

function isoNow() { return new Date().toISOString().replace(/\.\d{3}Z$/, "Z"); }
function clean(value, max = 500) { return String(value || "").trim().slice(0, max); }
function stageOf(row, columns) { return String(row[columns.stage] || "").trim().toLowerCase(); }
function columnMap(headers) {
  const map = {};
  (headers || []).forEach((name, i) => { map[String(name || "").trim().toLowerCase()] = i; });
  return map;
}
function at(row, columns, name) {
  const i = columns[String(name).toLowerCase()];
  return i == null ? "" : String(row[i] || "");
}

function entryFromRow(row, columns) {
  const jobId = at(row, columns, "job radar id");
  return { id: jobId, company: at(row, columns, "company"), title: at(row, columns, "position") || at(row, columns, "title"),
    stage: stageOf(row, {stage: columns.stage}), url: at(row, columns, "job url"),
    locations: at(row, columns, "location") ? [at(row, columns, "location")] : [],
    saved_at: at(row, columns, "saved at"), updated_at: at(row, columns, "updated at"),
    source: at(row, columns, "source"), via: "google-sheets", profile: at(row, columns, "profile") };
}

function accountId() { return `acct_${crypto.randomBytes(18).toString("base64url")}`; }
function deterministicAccountId(provider, identity) {
  const source = providerKey(provider, identity);
  return `acct_${crypto.createHash("sha256").update(source).digest("hex").slice(0, 32)}`;
}
function providerKey(provider, identity) {
  return provider === "github" ? `github:${identity.id || identity.login}` : `google:${identity.sub}`;
}

function accountFromRow(row, columns) {
  return { id: at(row, columns, "account id"), githubId: at(row, columns, "github id"),
    githubLogin: at(row, columns, "github login"), googleSub: at(row, columns, "google subject"),
    googleEmail: at(row, columns, "google email"), createdAt: at(row, columns, "created at"),
    updatedAt: at(row, columns, "updated at"), mergedInto: at(row, columns, "merged into"),
    status: at(row, columns, "status"), googleTokenCiphertext: at(row, columns, "google token ciphertext"),
    googleSheetId: at(row, columns, "google sheet id"), googleConnectedAt: at(row, columns, "google connected at"),
    rowNumber: 0 };
}

async function readAccounts(token) {
  let rows;
  try {
    rows = await readTab(token, ownerSheetId(), accountTab(), "A:Z");
  } catch (error) {
    if (!String(error.message || error).includes("400") && !String(error.message || error).includes("404")) throw error;
    const add = await fetch(`${SHEETS_API}/${ownerSheetId()}:batchUpdate`, {
      method: "POST", headers: authHeaders(token),
      body: JSON.stringify({ requests: [{ addSheet: { properties: { title: accountTab(),
        gridProperties: { rowCount: 10000, columnCount: ACCOUNT_HEADERS.length } } } }] }) });
    if (!add.ok && add.status !== 400) throw new Error(`Google account tab ${add.status}`);
    await putRange(token, ownerSheetId(), accountTab(), `A1:${columnName(ACCOUNT_HEADERS.length)}1`, [ACCOUNT_HEADERS]);
    rows = [ACCOUNT_HEADERS];
  }
  if (!rows.length || !rows[0].length) {
    rows = [ACCOUNT_HEADERS];
    await putRange(token, ownerSheetId(), accountTab(), `A1:${columnName(ACCOUNT_HEADERS.length)}1`, [ACCOUNT_HEADERS]);
  }
  const existingHeaders = rows[0].slice();
  const missing = ACCOUNT_HEADERS.filter(header => !existingHeaders.some(item =>
    String(item || "").trim().toLowerCase() === header.toLowerCase()));
  if (missing.length) {
    const headers = existingHeaders.concat(missing);
    await putRange(token, ownerSheetId(), accountTab(), `A1:${columnName(headers.length)}1`, [headers]);
    rows[0] = headers;
  }
  const headers = rows[0].length ? rows[0] : ACCOUNT_HEADERS;
  const columns = columnMap(headers);
  const accounts = rows.slice(1).map((row, i) => {
    const account = accountFromRow(row, columns); account.rowNumber = i + 2; return account;
  }).filter(account => account.id);
  return {rows, columns, accounts};
}

function canonical(accounts, account) {
  let current = account;
  const seen = new Set();
  while (current && current.mergedInto && !seen.has(current.id)) {
    seen.add(current.id);
    current = accounts.find(item => item.id === current.mergedInto) || current;
  }
  return current;
}

function matchesIdentity(account, provider, identity) {
  return provider === "github"
    ? (identity.id && account.githubId === String(identity.id)) ||
      (identity.login && account.githubLogin.toLowerCase() === String(identity.login).toLowerCase())
    : Boolean(identity.sub && account.googleSub === String(identity.sub));
}

function matchesKey(account, key) {
  const value = String(key || "");
  return [account.id, account.githubId, account.githubLogin, account.googleSub, account.googleEmail,
    account.githubId ? `github:${account.githubId}` : "", account.googleSub ? `google:${account.googleSub}` : ""]
    .filter(Boolean).some(candidate => candidate.toLowerCase() === value.toLowerCase());
}

function accountForKeys(data, keys) {
  const wanted = Array.isArray(keys) ? keys : [keys];
  const found = data.accounts.find(account => wanted.some(key => matchesKey(account, key)));
  return found ? canonical(data.accounts, found) : null;
}

function accountRow(account) {
  return [account.id, account.githubId, account.githubLogin, account.googleSub, account.googleEmail,
    account.createdAt, account.updatedAt, account.mergedInto, account.status || "active",
    account.googleTokenCiphertext || "", account.googleSheetId || "", account.googleConnectedAt || ""];
}

async function saveAccount(token, account) {
  const values = accountRow(account);
  const range = `A${account.rowNumber}:${columnName(ACCOUNT_HEADERS.length)}${account.rowNumber}`;
  if (account.rowNumber) {
    await putRange(token, ownerSheetId(), accountTab(), range, [values]);
    return;
  }
  await appendRange(token, ownerSheetId(), accountTab(), `A:${columnName(ACCOUNT_HEADERS.length)}`, [values]);
}

function identityFields(provider, identity) {
  return provider === "github"
    ? {githubId: String(identity.id || ""), githubLogin: clean(identity.login, 200)}
    : {googleSub: String(identity.sub || ""), googleEmail: clean(identity.email, 320).toLowerCase()};
}

function sessionIdentityFields(current) {
  return {githubId: String(current?.github?.id || ""),
    githubLogin: clean(current?.github?.login || (current?.g ? current?.u : ""), 200),
    googleSub: String(current?.google?.sub || ""),
    googleEmail: clean(current?.google?.email || "", 320).toLowerCase()};
}

function keysFor(account, accounts) {
  const keys = new Set([account.id]);
  for (const item of accounts) {
    if (canonical(accounts, item)?.id !== account.id) continue;
    if (item.id) keys.add(item.id);
    if (item.githubLogin) keys.add(item.githubLogin);
    if (item.githubId) keys.add(`github:${item.githubId}`);
    if (item.googleSub) keys.add(`google:${item.googleSub}`);
    if (item.googleEmail) keys.add(item.googleEmail);
  }
  return [...keys];
}

async function migrateLegacyRows(ownerToken, account, accounts, targetToken, targetSheetId) {
  // Preserve rows already saved through the previous shared-tracker version.
  // New users simply have no legacy rows to migrate.
  try {
    const rows = await readTab(ownerToken, ownerSheetId(), sharedUserTab(), "A:N");
    if (!rows.length) return;
    const columns = columnMap(rows[0]);
    const keys = new Set(keysFor(account, accounts).map(String).map(value => value.toLowerCase()));
    const migrated = rows.slice(1).filter(row => keys.has(at(row, columns, "account id").toLowerCase()) ||
      keys.has(at(row, columns, "github user").toLowerCase())).map(row => [
        at(row, columns, "job radar id"), at(row, columns, "company"), stageOf(row, columns),
        at(row, columns, "position") || at(row, columns, "title"), at(row, columns, "apply date"),
        at(row, columns, "text"), at(row, columns, "job url"), at(row, columns, "location"),
        at(row, columns, "saved at"), at(row, columns, "updated at"), at(row, columns, "source"),
        at(row, columns, "profile"),
      ]).filter(row => row[0] && VALID_STAGES.has(row[2]));
    if (migrated.length) await appendRange(targetToken, targetSheetId, personalTab(), "A:L", migrated);
  } catch {
    // A missing/legacy shared tab must not prevent a user's own Sheet from
    // being created. The app remains usable and no user data is lost.
  }
}

async function attachGoogleTracker(ownerToken, account, accounts, identity, oauth, currentPersonal = null) {
  const stored = unseal(account.googleTokenCiphertext || "");
  const refreshToken = String(oauth.refreshToken || stored?.refreshToken || "").trim();
  const knownPersonal = currentPersonal || (account.googleSheetId && account.googleSub && refreshToken
    ? {r: refreshToken, s: account.googleSheetId, e: account.googleEmail, sub: account.googleSub}
    : null);
  const {personal, created} = await ensurePersonalTracker(identity,
    {...oauth, refreshToken}, knownPersonal);
  account.googleTokenCiphertext = seal({version: 1, refreshToken: personal.r});
  account.googleSheetId = personal.s;
  account.googleConnectedAt = account.googleConnectedAt || isoNow();
  // Persist the new Sheet ID before migration so a retry cannot silently
  // create a second personal workbook if the legacy copy is interrupted.
  await saveAccount(ownerToken, account);
  if (created) {
    const userToken = oauth.accessToken || await personalSessionAccessToken(personal);
    await migrateLegacyRows(ownerToken, account, accounts, userToken, personal.s);
  }
}

function personalTrackerFromAccount(account, oauth = {}) {
  const stored = unseal(account?.googleTokenCiphertext || "");
  const refreshToken = String(oauth.refreshToken || stored?.refreshToken || "").trim();
  if (!refreshToken || !account?.googleSheetId || !account?.googleSub) return null;
  return {r: refreshToken, s: account.googleSheetId, e: account.googleEmail, sub: account.googleSub};
}

// Resolve or link an OAuth identity. Linking is explicit and requires an
// existing authenticated session. Google OAuth also provisions that user's
// own Sheet during the same server-side callback.
async function resolveAccountWithRegistry(provider, identity, current, mode = "login", oauth = {}) {
  const token = await accessToken();
  const data = await readAccounts(token);
  const now = isoNow();
  let currentAccount = current?.k && data.accounts.find(account => account.id === current.k);
  const linked = data.accounts.find(account => matchesIdentity(account, provider, identity));

  if (mode === "link" && !current) throw new Error("sign in before connecting another account");
  if (!currentAccount && mode === "link") {
    currentAccount = {id: current?.k || accountId(), ...sessionIdentityFields(current), createdAt: now,
      updatedAt: now, mergedInto: "", status: "active", rowNumber: data.rows.length + 1};
    await saveAccount(token, currentAccount);
    data.accounts.push(currentAccount);
    data.rows.push(accountRow(currentAccount));
  }

  if (linked && currentAccount && canonical(data.accounts, linked).id !== canonical(data.accounts, currentAccount).id) {
    const target = canonical(data.accounts, currentAccount);
    const source = canonical(data.accounts, linked);
    if (target.googleSheetId && source.googleSheetId && target.googleSheetId !== source.googleSheetId) {
      throw new Error("These accounts already have different personal Google Sheets; connect them from one account instead of merging.");
    }
    for (const field of ["githubId", "githubLogin", "googleSub", "googleEmail", "googleTokenCiphertext", "googleSheetId", "googleConnectedAt"]) {
      if (["googleTokenCiphertext", "googleSheetId", "googleConnectedAt"].includes(field)) {
        if (!target[field] && source[field]) target[field] = source[field];
        continue;
      }
      if (target[field] && source[field] && target[field] !== source[field]) {
        throw new Error("that provider is already connected to another account; sign into that account first");
      }
      if (!target[field] && source[field]) target[field] = source[field];
    }
    target.updatedAt = now;
    source.mergedInto = target.id; source.status = "merged"; source.updatedAt = now;
    await saveAccount(token, target);
    await saveAccount(token, source);
    currentAccount = target;
  } else if (linked) {
    currentAccount = canonical(data.accounts, linked);
  } else if (!currentAccount) {
    currentAccount = {id: deterministicAccountId(provider, identity), githubId: "", githubLogin: "", googleSub: "", googleEmail: "",
      createdAt: now, updatedAt: now, mergedInto: "", status: "active", rowNumber: data.rows.length + 1};
    Object.assign(currentAccount, identityFields(provider, identity));
    await saveAccount(token, currentAccount);
    data.accounts.push(currentAccount);
    data.rows.push(accountRow(currentAccount));
  } else {
    Object.assign(currentAccount, identityFields(provider, identity));
    currentAccount.updatedAt = now;
    await saveAccount(token, currentAccount);
  }

  if (provider === "google") {
    await attachGoogleTracker(token, currentAccount, data.accounts, identity, oauth, current?.pt);
    currentAccount.updatedAt = now;
    await saveAccount(token, currentAccount);
  }

  const github = currentAccount.githubId || currentAccount.githubLogin
    ? {id: currentAccount.githubId, login: currentAccount.githubLogin} : undefined;
  const google = currentAccount.googleSub
    ? {sub: currentAccount.googleSub, email: currentAccount.googleEmail} : undefined;
  return {account_id: currentAccount.id, keys: keysFor(currentAccount, data.accounts), github, google,
    google_sheet_url: currentAccount.googleSheetId ? sheetUrl(currentAccount.googleSheetId) : "",
    personal_tracker: personalTrackerFromAccount(currentAccount, oauth)};
}

function registryUnavailable(error) {
  return /^(Google token|Google did not return an access token|Google Sheet (read|update|append)|Google account tab|Google account registry)/
    .test(String(error?.message || error || ""));
}

async function resolveAccountWithoutRegistry(provider, identity, current, mode, oauth) {
  if (mode === "link" && !current) throw new Error("sign in before connecting another account");
  if (provider === "google" && current?.google?.sub && current.google.sub !== String(identity.sub || "")) {
    throw new Error("A different Google account is already connected. Sign out before switching accounts.");
  }
  if (provider === "github" && current?.github?.id &&
      String(current.github.id) !== String(identity.id || "")) {
    throw new Error("A different GitHub account is already connected. Sign out before switching accounts.");
  }
  const fallback = providerKey(provider, identity);
  const personal = provider === "google"
    ? (await ensurePersonalTracker(identity, oauth, current?.pt)).personal
    : (current?.pt || null);
  const github = provider === "github" ? identity : current?.github;
  const google = provider === "google" ? identity : current?.google;
  return {account_id: current?.k || deterministicAccountId(provider, identity),
    keys: [...new Set([current?.k, current?.u, fallback].filter(Boolean))], github, google,
    google_sheet_url: personal?.s ? sheetUrl(personal.s) : "", personal_tracker: personal};
}

async function resolveAccount(provider, identity, current, mode = "login", oauth = {}) {
  if (configured()) {
    try {
      return await resolveAccountWithRegistry(provider, identity, current, mode, oauth);
    } catch (error) {
      if (!registryUnavailable(error)) throw error;
      console.warn("Google account registry unavailable; continuing with the user's own tracker session");
    }
  }
  return resolveAccountWithoutRegistry(provider, identity, current, mode, oauth);
}

async function trackerContext(accountKeys) {
  const ownerToken = await accessToken();
  const data = await readAccounts(ownerToken);
  return {ownerToken, data, account: accountForKeys(data, accountKeys)};
}

function preferenceValue(rows, name) {
  const columns = columnMap(rows[0] || PREFERENCE_HEADERS);
  const row = rows.slice(1).find(item => at(item, columns, "preference") === name);
  return row ? at(row, columns, "value") : "";
}

async function userTracker(accountKeys, personal = null, profile = "new_grad") {
  let token;
  let spreadsheetId;
  let googleEmail;
  if (hasPersonalSession(personal)) {
    token = await personalSessionAccessToken(personal);
    spreadsheetId = personal.s;
    googleEmail = personal.e;
  } else {
    if (!configured()) return {configured: false, connected: false, needs_google: true, entries: [], maybe: []};
    const {account} = await trackerContext(accountKeys);
    if (!account?.googleSheetId || !account.googleTokenCiphertext) {
      return {configured: false, connected: false, needs_google: true, entries: [], maybe: []};
    }
    token = await personalAccessToken(account);
    spreadsheetId = account.googleSheetId;
    googleEmail = account.googleEmail;
  }
  await ensurePersonalTabs(token, spreadsheetId);
  await repairPersonalTrackerFormat(token, spreadsheetId);
  const tab = tabForProfile(profile);
  const rows = await readTab(token, spreadsheetId, tab, `A:${columnName(PERSONAL_HEADERS.length)}`);
  let preferences = [];
  try { preferences = await readTab(token, spreadsheetId, preferencesTab(), `A:${columnName(PREFERENCE_HEADERS.length)}`); } catch {}
  const expectedGraduation = preferenceValue(preferences, "expected_graduation");
  if (!rows.length) return {configured: true, connected: true, needs_google: false,
    google_email: googleEmail, sheet_url: sheetUrl(spreadsheetId), profile,
    expected_graduation: expectedGraduation, entries: [], maybe: []};
  const columns = columnMap(rows[0]);
  const lane = String(profile || "new_grad").toLowerCase() === "internship" ? "internship" : "new_grad";
  const mine = rows.slice(1).filter(row => {
    const rowProfile = at(row, columns, "profile").toLowerCase();
    const sameLane = lane === "internship" ? rowProfile === "internship" || tab === internshipTab()
      : !rowProfile || rowProfile === "new_grad" || rowProfile === "default";
    return at(row, columns, "job radar id") && VALID_STAGES.has(stageOf(row, columns)) && sameLane;
  });
  return {configured: true, connected: true, needs_google: false, google_email: googleEmail,
    sheet_url: sheetUrl(spreadsheetId), profile, expected_graduation: expectedGraduation,
    entries: mine.filter(row => !["maybe", "archived"].includes(stageOf(row, columns))).map(row => entryFromRow(row, columns)),
    maybe: mine.filter(row => stageOf(row, columns) === "maybe").map(row => at(row, columns, "job radar id"))};
}

function rowValues(payload, existing = {}) {
  const now = isoNow();
  const action = payload.action;
  const stage = action === "track" ? "saved" : action === "applied" ? "applied" :
    action === "maybe" ? "maybe" : action === "stage" ? clean(payload.stage, 20).toLowerCase() : "archived";
  return [clean(payload.id, 100), clean(payload.company, 200), stage, clean(payload.title, 300),
    stage === "applied" ? (existing.applyDate || now) : (existing.applyDate || ""), clean(payload.text, 500),
    clean(payload.url, 1900), clean(payload.location, 200), existing.savedAt || now, now,
    clean(payload.source, 100), clean(payload.profile || "new_grad", 50)];
}

async function updateUserTracker(login, accountKeys, payload, personal = null) {
  const action = payload.action;
  if (!["track", "applied", "maybe", "stage", "untrack", "preferences"].includes(action)) throw new Error("unsupported tracker action");
  const profile = String(payload.profile || "new_grad").toLowerCase() === "internship" ? "internship" : "new_grad";
  if (action === "preferences") {
    const value = clean(payload.expected_graduation, 20);
    if (value && !/^20\d{2}-(0[1-9]|1[0-2])$/.test(value)) throw new Error("expected graduation must be YYYY-MM");
  }
  if (action === "stage" && !VALID_STAGES.has(clean(payload.stage, 20).toLowerCase())) {
    throw new Error("unsupported application stage");
  }
  const id = clean(payload.id, 100);
  if (action !== "preferences" && !id) throw new Error("missing job id");
  let token;
  let spreadsheetId;
  if (hasPersonalSession(personal)) {
    token = await personalSessionAccessToken(personal);
    spreadsheetId = personal.s;
  } else {
    if (!configured()) throw new Error("Connect Google first to create your personal tracker");
    const {account} = await trackerContext(accountKeys);
    if (!account?.googleSheetId || !account.googleTokenCiphertext) {
      throw new Error("Connect Google first to create your personal tracker");
    }
    token = await personalAccessToken(account);
    spreadsheetId = account.googleSheetId;
  }
  await ensurePersonalTabs(token, spreadsheetId);
  if (action === "preferences") {
    const rows = await readTab(token, spreadsheetId, preferencesTab(), `A:${columnName(PREFERENCE_HEADERS.length)}`);
    const headers = rows[0]?.length ? rows[0] : PREFERENCE_HEADERS;
    const columns = columnMap(headers);
    const rowIndex = rows.slice(1).findIndex(row => at(row, columns, "preference") === "expected_graduation");
    const values = ["expected_graduation", clean(payload.expected_graduation, 20), isoNow()];
    if (rowIndex >= 0) {
      const rowNumber = rowIndex + 2;
      await putRange(token, spreadsheetId, preferencesTab(),
        `A${rowNumber}:${columnName(PREFERENCE_HEADERS.length)}${rowNumber}`, [values]);
    } else {
      await appendRange(token, spreadsheetId, preferencesTab(), `A:${columnName(PREFERENCE_HEADERS.length)}`, [values]);
    }
    return {ok: true, profile, expected_graduation: values[1], sheet_url: sheetUrl(spreadsheetId)};
  }
  const tab = tabForProfile(profile);
  const laneRows = await readTab(token, spreadsheetId, tab, `A:${columnName(PERSONAL_HEADERS.length)}`);
  const laneHeaders = laneRows[0]?.length ? laneRows[0] : PERSONAL_HEADERS;
  const laneColumns = columnMap(laneHeaders);
  const laneRowIndex = laneRows.slice(1).findIndex(row => String(row[laneColumns["job radar id"]] || "") === id);
  const laneRowNumber = laneRowIndex + 2;
  const laneExistingRow = laneRowNumber > 1 ? laneRows[laneRowNumber - 1] : [];
  const laneExisting = {savedAt: at(laneExistingRow, laneColumns, "saved at"), applyDate: at(laneExistingRow, laneColumns, "apply date")};
  const laneValues = rowValues(payload, laneExisting);
  if (laneRowNumber > 1) await putRange(token, spreadsheetId, tab,
    `A${laneRowNumber}:${columnName(PERSONAL_HEADERS.length)}${laneRowNumber}`, [laneValues]);
  else await appendRange(token, spreadsheetId, tab, `A:${columnName(PERSONAL_HEADERS.length)}`, [laneValues]);
  return {ok: true, stage: laneValues[2], profile, sheet_url: sheetUrl(spreadsheetId)};
}

module.exports = { configured, personalConfigured, hasPersonalSession, userTracker,
  updateUserTracker, resolveAccount, PERSONAL_HEADERS, ACCOUNT_HEADERS, PREFERENCE_HEADERS,
  tabForProfile, internshipTab, preferencesTab };
