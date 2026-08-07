// Private per-GitHub-user tracker backed by one owner-controlled Sheet.
// GitHub OAuth authenticates the user; the Google refresh token stays only in
// Vercel env vars. The Sheet is never sent to the browser wholesale.
const { envv } = require("./_lib");

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets";
const HEADERS = ["GitHub User", "Job Radar ID", "Company", "Title", "Stage",
  "Job URL", "Location", "Saved At", "Updated At", "Source", "Profile"];
const VALID_STAGES = new Set(["saved", "applied", "oa", "interview", "rejected", "closed", "maybe", "archived"]);

const configured = () => ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
  "GOOGLE_REFRESH_TOKEN", "GOOGLE_SHEET_ID"].every(k => envv(k));
const tab = () => envv("GOOGLE_USER_SHEET_TAB") || "User Applications";
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

function rangeUrl(range, suffix = "") {
  return `${SHEETS_API}/${sheetId()}/values/${encodeURIComponent(`${tab()}!${range}`)}${suffix}`;
}

async function readRows(token) {
  const r = await fetch(rangeUrl("A:K"), { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`Google Sheet read ${r.status}`);
  return (await r.json()).values || [];
}

async function putRow(token, number, values) {
  const r = await fetch(rangeUrl(`A${number}:K${number}`), { method: "PUT",
    headers: authHeaders(token),
    body: JSON.stringify({ range: `${tab()}!A${number}:K${number}`, majorDimension: "ROWS", values: [values] }) });
  if (!r.ok) throw new Error(`Google Sheet update ${r.status}`);
}

async function appendRow(token, values) {
  const r = await fetch(rangeUrl("A:K", ":append") + "?valueInputOption=RAW&insertDataOption=INSERT_ROWS", {
    method: "POST", headers: authHeaders(token), body: JSON.stringify({ majorDimension: "ROWS", values: [values] }) });
  if (!r.ok) throw new Error(`Google Sheet append ${r.status}`);
}

function isoNow() { return new Date().toISOString().replace(/\.\d{3}Z$/, "Z"); }
function clean(value, max = 500) { return String(value || "").trim().slice(0, max); }
function stageOf(row) { return String(row[4] || "").trim().toLowerCase(); }

function entryFromRow(row) {
  return { id: row[1], company: row[2], title: row[3], stage: stageOf(row), url: row[5],
    locations: row[6] ? [row[6]] : [], saved_at: row[7], updated_at: row[8],
    source: row[9], via: "google-sheets", profile: row[10] };
}

async function userTracker(login) {
  if (!configured()) return { configured: false, entries: [], maybe: [] };
  const rows = await readRows(await accessToken());
  const mine = rows.slice(1).filter(row => row[0] === login && row[1] && VALID_STAGES.has(stageOf(row)));
  return { configured: true,
    entries: mine.filter(row => !["maybe", "archived"].includes(stageOf(row))).map(entryFromRow),
    maybe: mine.filter(row => stageOf(row) === "maybe").map(row => row[1]) };
}

async function updateUserTracker(login, payload) {
  if (!configured()) throw new Error("Google tracker is not configured");
  const action = payload.action;
  if (!["track", "applied", "maybe", "untrack"].includes(action)) throw new Error("unsupported tracker action");
  const id = clean(payload.id, 100);
  if (!id) throw new Error("missing job id");
  const token = await accessToken();
  const rows = await readRows(token);
  const rowNumber = rows.slice(1).findIndex(row => row[0] === login && row[1] === id) + 2;
  const existing = rowNumber > 1 ? rows[rowNumber - 1] : [];
  const now = isoNow();
  const stage = action === "track" ? "saved" : action === "applied" ? "applied" : action === "maybe" ? "maybe" : "archived";
  const values = [login, id, clean(payload.company, 200), clean(payload.title, 300), stage,
    clean(payload.url, 1900), clean(payload.location, 200), existing[7] || now, now,
    clean(payload.source, 100), clean(payload.profile, 50)];
  if (rowNumber > 1) await putRow(token, rowNumber, values);
  else await appendRow(token, values);
  return { ok: true, stage };
}

module.exports = { configured, userTracker, updateUserTracker, HEADERS };
