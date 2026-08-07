// Private per-account tracker backed by one owner-controlled Sheet.
// OAuth credentials stay in Vercel env vars. The browser receives only the
// current user's filtered rows, never the workbook or a Google token.
const crypto = require("crypto");
const { envv } = require("./_lib");

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets";

// The visible columns intentionally follow the existing Notion Applications
// database: Company, Stage, Position, Apply date, Text, Job URL, Location.
// The remaining columns are implementation metadata and are kept in the same
// private workbook so the user can still inspect/edit their own tracker.
const USER_HEADERS = ["Account ID", "GitHub User", "Job Radar ID", "Company", "Stage",
  "Position", "Apply date", "Text", "Job URL", "Location", "Saved At", "Updated At",
  "Source", "Profile"];
const ACCOUNT_HEADERS = ["Account ID", "GitHub ID", "GitHub Login", "Google Subject",
  "Google Email", "Created At", "Updated At", "Merged Into", "Status"];
const VALID_STAGES = new Set(["saved", "applied", "oa", "interview", "rejected", "closed", "maybe", "archived"]);

const configured = () => ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
  "GOOGLE_REFRESH_TOKEN", "GOOGLE_SHEET_ID"].every(k => envv(k));
const tab = () => envv("GOOGLE_USER_SHEET_TAB") || "User Applications";
const accountTab = () => envv("GOOGLE_ACCOUNT_SHEET_TAB") || "Accounts";
const sheetId = () => envv("GOOGLE_SHEET_ID");
const authHeaders = token => ({ Authorization: `Bearer ${token}`, "Content-Type": "application/json" });

async function accessToken() {
  const r = await fetch(TOKEN_URL, { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: envv("GOOGLE_CLIENT_ID"),
      client_secret: envv("GOOGLE_CLIENT_SECRET"), refresh_token: envv("GOOGLE_REFRESH_TOKEN"),
      grant_type: "refresh_token" }) });
  if (!r.ok) throw new Error(`Google token ${r.status}`);
  return (await r.json()).access_token;
}

function rangeUrl(range, sheetTab, suffix = "") {
  return `${SHEETS_API}/${sheetId()}/values/${encodeURIComponent(`${sheetTab}!${range}`)}${suffix}`;
}

async function readTab(token, sheetTab, range = "A:Z") {
  const r = await fetch(rangeUrl(range, sheetTab), { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`Google Sheet read ${r.status}`);
  return (await r.json()).values || [];
}

async function putRange(token, sheetTab, range, values) {
  const r = await fetch(rangeUrl(range, sheetTab), { method: "PUT", headers: authHeaders(token),
    body: JSON.stringify({ range: `${sheetTab}!${range}`, majorDimension: "ROWS", values }) });
  if (!r.ok) throw new Error(`Google Sheet update ${r.status}`);
}

async function appendRange(token, sheetTab, range, values) {
  const r = await fetch(rangeUrl(range, sheetTab, ":append") + "?valueInputOption=RAW&insertDataOption=INSERT_ROWS", {
    method: "POST", headers: authHeaders(token), body: JSON.stringify({ majorDimension: "ROWS", values }) });
  if (!r.ok) throw new Error(`Google Sheet append ${r.status}`);
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

function rowValues(login, accountId, payload, existing = {}) {
  const now = isoNow();
  const action = payload.action;
  const stage = action === "track" ? "saved" : action === "applied" ? "applied" :
    action === "maybe" ? "maybe" : "archived";
  return [accountId, clean(login, 200), clean(payload.id, 100), clean(payload.company, 200), stage,
    clean(payload.title, 300), stage === "applied" ? (existing.applyDate || now) : (existing.applyDate || ""),
    clean(payload.text, 500), clean(payload.url, 1900), clean(payload.location, 200),
    existing.savedAt || now, now, clean(payload.source, 100), clean(payload.profile, 50)];
}

function accountId() { return `acct_${crypto.randomBytes(18).toString("base64url")}`; }
function deterministicAccountId(provider, identity) {
  // Stable across simultaneous first logins, without exposing a sequential
  // user count. The ID is still only accepted from the encrypted session.
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
    status: at(row, columns, "status"), rowNumber: 0 };
}

async function readAccounts(token) {
  let rows;
  try {
    rows = await readTab(token, accountTab(), "A:I");
  } catch (error) {
    // Older workbooks created before account linking had no Accounts tab.
    // Add it once, then continue; this does not overwrite any existing tab.
    if (!String(error.message || error).includes("400") && !String(error.message || error).includes("404")) throw error;
    const add = await fetch(`${SHEETS_API}/${sheetId()}:batchUpdate`, {
      method: "POST", headers: authHeaders(token),
      body: JSON.stringify({requests: [{addSheet: {properties: {title: accountTab(),
        gridProperties: {rowCount: 10000, columnCount: ACCOUNT_HEADERS.length}}}}]}) });
    if (!add.ok && add.status !== 400) throw new Error(`Google account tab ${add.status}`);
    await putRange(token, accountTab(), "A1:I1", [ACCOUNT_HEADERS]);
    rows = [ACCOUNT_HEADERS];
  }
  if (!rows.length) return {rows: [ACCOUNT_HEADERS], columns: columnMap(ACCOUNT_HEADERS), accounts: []};
  const headers = rows[0].length ? rows[0] : ACCOUNT_HEADERS;
  const columns = columnMap(headers);
  const accounts = rows.slice(1).map((row, i) => {
    const account = accountFromRow(row, columns); account.rowNumber = i + 2; return account;
  }).filter(a => a.id);
  return {rows, columns, accounts};
}

function canonical(accounts, account) {
  let current = account;
  const seen = new Set();
  while (current && current.mergedInto && !seen.has(current.id)) {
    seen.add(current.id);
    current = accounts.find(a => a.id === current.mergedInto) || current;
  }
  return current;
}

function matchesIdentity(account, provider, identity) {
  return provider === "github"
    ? (identity.id && account.githubId === String(identity.id)) ||
      (identity.login && account.githubLogin.toLowerCase() === String(identity.login).toLowerCase())
    : Boolean(identity.sub && account.googleSub === String(identity.sub));
}

function accountRow(account) {
  return [account.id, account.githubId, account.githubLogin, account.googleSub, account.googleEmail,
    account.createdAt, account.updatedAt, account.mergedInto, account.status || "active"];
}

async function saveAccount(token, account, existing) {
  const values = accountRow(account);
  if (existing && existing.rowNumber) await putRange(token, accountTab(), `A${existing.rowNumber}:I${existing.rowNumber}`, [values]);
  else await appendRange(token, accountTab(), "A:I", [values]);
}

function identityFields(provider, identity) {
  return provider === "github"
    ? {githubId: String(identity.id || ""), githubLogin: clean(identity.login, 200)}
    : {googleSub: String(identity.sub || ""), googleEmail: clean(identity.email, 320).toLowerCase()};
}

function sessionIdentityFields(current) {
  return {githubId: String(current?.github?.id || ""), githubLogin: clean(current?.github?.login || (current?.g ? current?.u : ""), 200),
    googleSub: String(current?.google?.sub || ""), googleEmail: clean(current?.google?.email || "", 320).toLowerCase()};
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

// Resolve or link an OAuth identity. Linking is explicit and requires an
// existing authenticated session. If both providers already belong to two
// accounts, proving control of both identities permits a safe metadata merge;
// existing tracker rows remain readable through the merged-account aliases.
async function resolveAccount(provider, identity, current, mode = "login") {
  const fallback = providerKey(provider, identity);
  if (!configured()) {
    if (mode === "link") throw new Error("private tracker setup is required before linking accounts");
    return {account_id: current?.k || fallback, keys: [current?.k, current?.u, fallback].filter(Boolean),
      github: current?.github || (provider === "github" ? identity : undefined),
      google: current?.google || (provider === "google" ? identity : undefined)};
  }
  const token = await accessToken();
  const data = await readAccounts(token);
  const now = isoNow();
  let currentAccount = current?.k && data.accounts.find(a => a.id === current.k);
  const linked = data.accounts.find(a => matchesIdentity(a, provider, identity));

  if (mode === "link" && !current) throw new Error("sign in before connecting another account");
  if (!currentAccount && mode === "link") {
    currentAccount = {id: current?.k || accountId(), ...sessionIdentityFields(current), createdAt: now,
      updatedAt: now, mergedInto: "", status: "active", rowNumber: 0};
    await saveAccount(token, currentAccount);
    data.accounts.push(currentAccount);
  }

  if (linked && currentAccount && canonical(data.accounts, linked).id !== canonical(data.accounts, currentAccount).id) {
    const target = canonical(data.accounts, currentAccount);
    const source = canonical(data.accounts, linked);
    for (const field of ["githubId", "githubLogin", "googleSub", "googleEmail"]) {
      if (target[field] && source[field] && target[field] !== source[field]) {
        throw new Error("that provider is already connected to another account; sign into that account first");
      }
      if (!target[field] && source[field]) target[field] = source[field];
    }
    target.updatedAt = now;
    source.mergedInto = target.id; source.status = "merged"; source.updatedAt = now;
    await saveAccount(token, target, data.accounts.find(a => a.id === target.id));
    await saveAccount(token, source, data.accounts.find(a => a.id === source.id));
    currentAccount = target;
  } else if (linked) {
    currentAccount = canonical(data.accounts, linked);
  } else {
    if (!currentAccount) {
      currentAccount = {id: deterministicAccountId(provider, identity), githubId: "", githubLogin: "", googleSub: "", googleEmail: "",
        createdAt: now, updatedAt: now, mergedInto: "", status: "active", rowNumber: 0};
      Object.assign(currentAccount, identityFields(provider, identity));
      await saveAccount(token, currentAccount);
      data.accounts.push(currentAccount);
    } else {
      Object.assign(currentAccount, identityFields(provider, identity));
      currentAccount.updatedAt = now;
      await saveAccount(token, currentAccount, data.accounts.find(a => a.id === currentAccount.id));
    }
  }

  const github = currentAccount.githubId || currentAccount.githubLogin
    ? {id: currentAccount.githubId, login: currentAccount.githubLogin} : undefined;
  const google = currentAccount.googleSub
    ? {sub: currentAccount.googleSub, email: currentAccount.googleEmail} : undefined;
  return {account_id: currentAccount.id, keys: keysFor(currentAccount, data.accounts), github, google};
}

async function userTracker(accountKeys) {
  if (!configured()) return { configured: false, entries: [], maybe: [] };
  const keys = new Set((Array.isArray(accountKeys) ? accountKeys : [accountKeys]).filter(Boolean).map(String));
  const rows = await readTab(await accessToken(), tab());
  if (!rows.length) return { configured: true, entries: [], maybe: [] };
  const columns = columnMap(rows[0]);
  const accountColumn = columns["account id"] == null ? columns["github user"] : columns["account id"];
  const mine = rows.slice(1).filter(row => keys.has(String(row[accountColumn] || "")) &&
    at(row, columns, "job radar id") && VALID_STAGES.has(stageOf(row, columns)));
  return { configured: true,
    entries: mine.filter(row => !["maybe", "archived"].includes(stageOf(row, columns))).map(row => entryFromRow(row, columns)),
    maybe: mine.filter(row => stageOf(row, columns) === "maybe").map(row => at(row, columns, "job radar id")) };
}

async function updateUserTracker(login, accountKeys, payload) {
  if (!configured()) throw new Error("Google tracker is not configured");
  const action = payload.action;
  if (!["track", "applied", "maybe", "untrack"].includes(action)) throw new Error("unsupported tracker action");
  const id = clean(payload.id, 100);
  if (!id) throw new Error("missing job id");
  const token = await accessToken();
  const rows = await readTab(token, tab());
  const headers = rows[0]?.length ? rows[0] : USER_HEADERS;
  const columns = columnMap(headers);
  const accountColumn = columns["account id"] == null ? columns["github user"] : columns["account id"];
  const jobColumn = columns["job radar id"];
  const keys = new Set((Array.isArray(accountKeys) ? accountKeys : [accountKeys]).filter(Boolean).map(String));
  const rowIndex = rows.slice(1).findIndex(row => keys.has(String(row[accountColumn] || "")) &&
    String(row[jobColumn] || "") === id);
  const rowNumber = rowIndex + 2;
  const existingRow = rowNumber > 1 ? rows[rowNumber - 1] : [];
  const existing = {savedAt: at(existingRow, columns, "saved at"), applyDate: at(existingRow, columns, "apply date")};
  const canonicalKey = Array.isArray(accountKeys) ? accountKeys[0] : accountKeys;
  const values = rowValues(login, canonicalKey, payload, existing);
  if (headers.length === USER_HEADERS.length && headers[0] === USER_HEADERS[0]) {
    if (rowNumber > 1) await putRange(token, tab(), `A${rowNumber}:N${rowNumber}`, [values]);
    else await appendRange(token, tab(), "A:N", [values]);
  } else {
    // Tolerate a pre-existing legacy sheet by writing fields in its original order.
    const legacy = [login, id, clean(payload.company, 200), clean(payload.title, 300),
      action === "track" ? "saved" : action === "applied" ? "applied" : action === "maybe" ? "maybe" : "archived",
      clean(payload.url, 1900), clean(payload.location, 200), existing.savedAt || isoNow(), isoNow(),
      clean(payload.source, 100), clean(payload.profile, 50)];
    if (rowNumber > 1) await putRange(token, tab(), `A${rowNumber}:K${rowNumber}`, [legacy]);
    else await appendRange(token, tab(), "A:K", [legacy]);
  }
  return { ok: true, stage: values[4] };
}

module.exports = { configured, userTracker, updateUserTracker, resolveAccount, USER_HEADERS, ACCOUNT_HEADERS };
