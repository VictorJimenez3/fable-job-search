// Owner-only application-agent control plane.
//
// The Mac browser remains the execution boundary.  This route stores only
// the private application context, queue state, issue ledger, and an
// explicit short-lived review card in the owner's existing private workbook
// when possible, with the legacy app-created Drive folder as a fallback.
// It never stores a CV, browser cookie, provider session, or DOM dump.
const crypto = require("crypto");
const { OWNER, session, requireMutationRequest } = require("./_lib");
const tracker = require("./_google-tracker");

const DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files";
const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files";
const SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets";
const VERSION = "application-agent-v1";
const FOLDER_PROPERTY = "jobRadarApplicationAgent";
const APPLICATION_SHEET = "Application Agent";
const APPLICATION_SHEET_HEADERS = ["Record Type", "Record ID", "Payload JSON"];
const APPLICATION_SHEET_MAX_ROWS = 2200;
const FILES = {
  context: ["application-context.json", "jobRadarApplicationContext"],
  contextMarkdown: ["application-context.md", "jobRadarApplicationContextMarkdown"],
  queue: ["application-queue.json", "jobRadarApplicationQueue"],
  issues: ["application-issues.json", "jobRadarApplicationIssues"],
  issuesMarkdown: ["application-issues.md", "jobRadarApplicationIssuesMarkdown"],
  pairing: ["application-agent-pairing.json", "jobRadarApplicationPairing"],
};
const MAX_QUEUE = 200;
const MAX_ANSWERS = 500;
const MAX_ISSUES = 300;
const REVIEW_TTL_MS = 15 * 60 * 1000;
const SENSITIVE_CATEGORIES = new Set(["work_authorization", "sponsorship", "disability", "veteran_status", "gender", "race_ethnicity", "demographic", "salary", "address", "phone"]);
const ACTIVE_QUEUE_STATES = new Set(["queued", "opening", "filling", "blocked", "awaiting_confirmation", "submitting"]);
const QUEUE_STATES = new Set([...ACTIVE_QUEUE_STATES, "submitted", "failed", "skipped", "cancelled"]);

function clean(value, max = 500) {
  return String(value ?? "").replace(/\x00/g, "").trim().slice(0, max);
}

function bodyOf(req) {
  if (req.body && typeof req.body === "object") return req.body;
  try { return JSON.parse(String(req.body || "{}")); } catch { return {}; }
}

function ownerSession(req) {
  const current = session(req);
  if (!current) return {error: "sign in first"};
  const login = current.github?.login || (current.g ? current.u : "");
  if (String(login).toLowerCase() !== String(OWNER).toLowerCase()) {
    return {error: "Application Agent is private to the repository owner"};
  }
  return {current};
}

function driveHeaders(token, extra = {}) {
  return {Authorization: `Bearer ${token}`, ...extra};
}

async function driveJson(url, token, options = {}) {
  const response = await fetch(url, {...options, headers: driveHeaders(token, options.headers || {})});
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) {
    const detail = data.error?.message || data.error || `Google Drive ${response.status}`;
    throw new Error(String(detail).slice(0, 240));
  }
  return data;
}

async function listFiles(token, query) {
  const params = new URLSearchParams({
    q: query, spaces: "drive", pageSize: "100",
    fields: "files(id,name,mimeType,appProperties,parents,size,modifiedTime)",
  });
  const data = await driveJson(`${DRIVE_FILES_API}?${params}`, token);
  return Array.isArray(data.files) ? data.files : [];
}

async function ensureFolder(token) {
  const files = await listFiles(token,
    `trashed = false and mimeType = 'application/vnd.google-apps.folder' and ` +
    `appProperties has { key='${FOLDER_PROPERTY}' and value='${VERSION}' }`);
  if (files[0]?.id) return files[0];
  return driveJson(DRIVE_FILES_API, token, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: "Job Radar Application Agent", mimeType: "application/vnd.google-apps.folder",
      appProperties: {[FOLDER_PROPERTY]: VERSION}}),
  });
}

async function readFile(token, id) {
  const response = await fetch(`${DRIVE_FILES_API}/${encodeURIComponent(id)}?alt=media`, {headers: driveHeaders(token)});
  if (!response.ok) throw new Error(`Google Drive application file ${response.status}`);
  return Buffer.from(await response.arrayBuffer()).toString("utf8");
}

async function uploadFile(token, {id = "", metadata, content, contentType}) {
  const boundary = `jobradar-application-${crypto.randomBytes(10).toString("hex")}`;
  const preamble = Buffer.from(
    `--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${JSON.stringify(metadata)}\r\n` +
    `--${boundary}\r\nContent-Type: ${contentType}\r\n\r\n`, "utf8");
  const ending = Buffer.from(`\r\n--${boundary}--\r\n`, "utf8");
  const data = Buffer.isBuffer(content) ? content : Buffer.from(String(content), "utf8");
  const url = `${DRIVE_UPLOAD_API}${id ? `/${encodeURIComponent(id)}` : ""}?uploadType=multipart`;
  return driveJson(url, token, {
    method: id ? "PATCH" : "POST",
    headers: {"Content-Type": `multipart/related; boundary=${boundary}`},
    body: Buffer.concat([preamble, data, ending]),
  });
}

function storageToken(access) {
  return access?.access?.token || access?.token || "";
}

function spreadsheetId(access) {
  return String(access?.access?.spreadsheetId || access?.spreadsheetId || "").trim();
}

function sheetRange(spreadsheet, range) {
  return `${SHEETS_API}/${encodeURIComponent(spreadsheet)}/values/${encodeURIComponent(range)}`;
}

async function sheetJson(url, token, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {})},
  });
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) {
    const message = data.error?.message || data.error || `Google Sheet ${response.status}`;
    throw new Error(String(message).slice(0, 260));
  }
  return data;
}

async function ensureApplicationSheet(token, spreadsheet) {
  const metadata = await sheetJson(
    `${SHEETS_API}/${encodeURIComponent(spreadsheet)}?fields=sheets(properties(sheetId,title))`, token,
  );
  let sheet = (metadata.sheets || []).find(item => item.properties?.title === APPLICATION_SHEET);
  if (!sheet) {
    const created = await sheetJson(`${SHEETS_API}/${encodeURIComponent(spreadsheet)}:batchUpdate`, token, {
      method: "POST",
      body: JSON.stringify({requests: [{addSheet: {properties: {
        title: APPLICATION_SHEET,
        gridProperties: {rowCount: APPLICATION_SHEET_MAX_ROWS, columnCount: APPLICATION_SHEET_HEADERS.length},
      }}}]}),
    });
    sheet = (created.replies || []).find(item => item.addSheet?.properties)?.addSheet;
  }
  if (!sheet?.properties?.title) throw new Error("Google Sheet could not create the Application Agent tab");
  const header = await sheetJson(sheetRange(spreadsheet, `${APPLICATION_SHEET}!A1:C1`), token);
  const values = header.values || [];
  const matches = APPLICATION_SHEET_HEADERS.every((value, index) => String(values[0]?.[index] || "") === value);
  if (!matches) {
    await sheetJson(`${sheetRange(spreadsheet, `${APPLICATION_SHEET}!A1:C1`)}?valueInputOption=RAW`, token, {
      method: "PUT", body: JSON.stringify({range: `${APPLICATION_SHEET}!A1:C1`, majorDimension: "ROWS", values: [APPLICATION_SHEET_HEADERS]}),
    });
  }
  return sheet;
}

function emptySheetStore() {
  return {
    version: VERSION, updated_at: "",
    context: {version: VERSION, updated_at: "", answers: [], mappings: {}},
    queue: {version: VERSION, updated_at: "", items: []},
    issues: {version: VERSION, updated_at: "", issues: []},
    pairing: {version: VERSION, updated_at: "", tokens: []},
  };
}

function parseSheetPayload(value) {
  try {
    const parsed = JSON.parse(String(value || ""));
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch { return null; }
}

async function readSheetStore(token, spreadsheet) {
  await ensureApplicationSheet(token, spreadsheet);
  const data = await sheetJson(sheetRange(spreadsheet, `${APPLICATION_SHEET}!A2:C${APPLICATION_SHEET_MAX_ROWS}`), token);
  const store = emptySheetStore();
  for (const row of data.values || []) {
    const kind = String(row[0] || "").trim();
    const id = String(row[1] || "").trim();
    const value = parseSheetPayload(row[2]);
    if (!kind || !value) continue;
    if (kind === "queue" && id) store.queue.items.push(value);
    else if (kind === "context" && id) store.context.answers.push(value);
    else if (kind === "context_meta" && id === "mappings" && value.mappings && typeof value.mappings === "object") store.context.mappings = value.mappings;
    else if (kind === "issue" && id) store.issues.issues.push(value);
    else if (kind === "pairing" && id) store.pairing.tokens.push(value);
  }
  return store;
}

function sheetStoreRows(store) {
  const rows = [APPLICATION_SHEET_HEADERS];
  for (const item of (store.queue?.items || []).slice(0, MAX_QUEUE)) {
    if (item?.queue_id) rows.push(["queue", item.queue_id, JSON.stringify(item)]);
  }
  for (const item of (store.context?.answers || []).slice(0, MAX_ANSWERS)) {
    if (item?.answer_id) rows.push(["context", item.answer_id, JSON.stringify(item)]);
  }
  rows.push(["context_meta", "mappings", JSON.stringify({mappings: store.context?.mappings || {}})]);
  for (const item of (store.issues?.issues || []).slice(0, MAX_ISSUES)) {
    if (item?.issue_id) rows.push(["issue", item.issue_id, JSON.stringify(item)]);
  }
  for (const item of (store.pairing?.tokens || []).slice(0, MAX_ANSWERS)) {
    if (item?.token_hash) rows.push(["pairing", item.token_hash, JSON.stringify(item)]);
  }
  return rows;
}

async function writeSheetStore(token, spreadsheet, store) {
  const rows = sheetStoreRows(store);
  if (rows.length > APPLICATION_SHEET_MAX_ROWS) throw new Error("Application Agent Sheet reached its row limit");
  await ensureApplicationSheet(token, spreadsheet);
  await sheetJson(`${sheetRange(spreadsheet, `${APPLICATION_SHEET}!A2:C${APPLICATION_SHEET_MAX_ROWS}`)}:clear`, token, {
    method: "POST", body: JSON.stringify({}),
  });
  await sheetJson(`${sheetRange(spreadsheet, `${APPLICATION_SHEET}!A1:C${rows.length}`)}?valueInputOption=RAW`, token, {
    method: "PUT", body: JSON.stringify({range: `${APPLICATION_SHEET}!A1:C${rows.length}`, majorDimension: "ROWS", values: rows}),
  });
  return {version: VERSION, updated_at: new Date().toISOString(), ...store};
}

async function appDocument(access, kind) {
  const token = storageToken(access);
  const spreadsheet = spreadsheetId(access);
  if (spreadsheet) {
    const store = await readSheetStore(token, spreadsheet);
    return {storage: "sheet", token, spreadsheet, store, value: store[kind] || emptyFor(kind)};
  }
  const folder = access.folder || await ensureFolder(token);
  const current = await readDocument(token, folder, kind);
  return {storage: "drive", token, folder, ...current};
}

async function writeAppDocument(state, kind, value) {
  if (state.storage === "sheet") {
    state.store[kind] = value;
    await writeSheetStore(state.token, state.spreadsheet, state.store);
    return {file: null, value};
  }
  return writeDocument(state.token, state.folder, state, kind, value);
}

async function findFile(token, folder, descriptor) {
  const [name, property] = descriptor;
  const files = await listFiles(token,
    `'${folder.id}' in parents and trashed = false and name = '${name}' and ` +
    `appProperties has { key='${property}' and value='${VERSION}' }`);
  return files[0] || null;
}

function emptyFor(kind) {
  if (kind === "context") return {version: VERSION, updated_at: "", answers: [], mappings: {}};
  if (kind === "queue") return {version: VERSION, updated_at: "", items: []};
  if (kind === "issues" || kind === "pairing") return kind === "issues"
    ? {version: VERSION, updated_at: "", issues: []}
    : {version: VERSION, updated_at: "", tokens: []};
  return {};
}

async function readDocument(token, folder, kind) {
  const file = await findFile(token, folder, FILES[kind]);
  if (!file?.id) return {file: null, value: emptyFor(kind)};
  try {
    const parsed = JSON.parse(await readFile(token, file.id));
    return {file, value: parsed && typeof parsed === "object" ? parsed : emptyFor(kind)};
  } catch {
    return {file, value: emptyFor(kind)};
  }
}

async function writeDocument(token, folder, current, kind, value) {
  const [name, property] = FILES[kind];
  const metadata = {name, mimeType: "application/json", appProperties: {[property]: VERSION}};
  if (!current.file?.id) metadata.parents = [folder.id];
  const document = {...value, version: VERSION, updated_at: new Date().toISOString()};
  const file = await uploadFile(token, {
    id: current.file?.id || "", metadata,
    content: JSON.stringify(document), contentType: "application/json",
  });
  return {file, value: document};
}

async function writeTextDocument(token, folder, kind, text) {
  const current = await readDocument(token, folder, kind);
  const [name, property] = FILES[kind];
  const metadata = {name, mimeType: "text/markdown", appProperties: {[property]: VERSION}};
  if (!current.file?.id) metadata.parents = [folder.id];
  return uploadFile(token, {id: current.file?.id || "", metadata, content: text, contentType: "text/markdown"});
}

function safeJob(value) {
  const job = value && typeof value === "object" ? value : {};
  let url = "";
  try {
    const parsed = new URL(String(job.url || ""));
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return null;
    url = parsed.toString().slice(0, 2500);
  } catch { return null; }
  const id = clean(job.id, 160), company = clean(job.company, 240), title = clean(job.title, 360);
  if (!id || !company || !title || !url) return null;
  return {
    id, company, title, url,
    locations: Array.isArray(job.locations) ? job.locations.map(item => clean(item, 160)).filter(Boolean).slice(0, 12) : [],
    score: Number.isFinite(Number(job.score)) ? Number(job.score) : 0,
    posted_at: job.posted_at ?? null,
  };
}

function publicReview(value) {
  if (!value || typeof value !== "object") return null;
  const review = {...value};
  review.fields = Array.isArray(review.fields) ? review.fields.slice(0, 250).map(field => ({
    field_id: clean(field?.field_id, 160), label: clean(field?.label, 500),
    category: clean(field?.category, 80), type: clean(field?.type, 32),
    required: Boolean(field?.required), sensitive: Boolean(field?.sensitive),
    value: clean(field?.value, 20000), answer_id: clean(field?.answer_id, 100),
  })) : [];
  review.blockers = Array.isArray(review.blockers) ? review.blockers.slice(0, 80) : [];
  review.nonce = clean(review.nonce, 100);
  review.review_hash = clean(review.review_hash, 100);
  review.expires_at = clean(review.expires_at, 80);
  return review;
}

function publicQueueItem(item) {
  const value = item && typeof item === "object" ? item : {};
  const resume = value.resume && typeof value.resume === "object" ? value.resume : null;
  return {
    queue_id: clean(value.queue_id, 100), state: QUEUE_STATES.has(value.state) ? value.state : "queued",
    session_id: clean(value.session_id, 100), message: clean(value.message, 800), error: clean(value.error, 800),
    created_at: clean(value.created_at, 80), updated_at: clean(value.updated_at, 80),
    job: safeJob(value.job), blockers: Array.isArray(value.blockers) ? value.blockers.slice(0, 80) : [],
    resume: resume ? {
      status: clean(resume.status, 30), source: clean(resume.source, 40), run_id: clean(resume.run_id, 100),
      mode: clean(resume.mode, 40), resume_status: clean(resume.resume_status, 40),
      approval_state: clean(resume.approval_state, 40), winner_version: clean(resume.winner_version, 40),
      pdf_filename: clean(resume.pdf_filename, 180), message: clean(resume.message, 500),
      needs_owner_review: Boolean(resume.needs_owner_review),
    } : null,
    review: publicReview(value.review), confirmation: value.confirmation ? {
      review_hash: clean(value.confirmation.review_hash, 100),
      page_fingerprint: clean(value.confirmation.page_fingerprint, 100),
      expires_at: clean(value.confirmation.expires_at, 80),
    } : null,
  };
}

function queueDocument(value) {
  const items = Array.isArray(value?.items) ? value.items.map(publicQueueItem).filter(item => item.queue_id && item.job) : [];
  return {version: VERSION, updated_at: clean(value?.updated_at, 80), items};
}

function contextDocument(value) {
  const answers = Array.isArray(value?.answers) ? value.answers.map(answer => ({
    answer_id: clean(answer?.answer_id, 100), question: clean(answer?.question, 1200),
    normalized_question: clean(answer?.normalized_question, 1200),
    variants: Array.isArray(answer?.variants) ? answer.variants.map(item => clean(item, 1200)).slice(0, 30) : [],
    category: clean(answer?.category, 80), value: clean(answer?.value, 20000),
    reusable: answer?.reusable !== false, sensitive: Boolean(answer?.sensitive),
    evidence_ids: Array.isArray(answer?.evidence_ids) ? answer.evidence_ids.map(item => clean(item, 140)).slice(0, 20) : [],
    updated_at: clean(answer?.updated_at, 80),
  })).filter(answer => answer.answer_id && answer.value).slice(0, MAX_ANSWERS) : [];
  const mappings = value?.mappings && typeof value.mappings === "object" ? Object.fromEntries(
    Object.entries(value.mappings).slice(0, 500).map(([key, answerId]) => [clean(key, 240), clean(answerId, 100)])) : {};
  return {version: VERSION, updated_at: clean(value?.updated_at, 80), answers, mappings};
}

function contextMarkdown(value) {
  const context = contextDocument(value);
  const lines = ["# Job Radar application context", "", "This file is a readable mirror. Use Job Radar to edit answers so matching and approval metadata remain intact.", "", `Updated: ${context.updated_at || new Date().toISOString()}`, ""];
  for (const answer of context.answers) {
    lines.push(`## ${answer.question || answer.category}`, "", `- Category: \`${answer.category || "other"}\`${answer.sensitive ? " · sensitive" : ""}`, `- Reusable: \`${answer.reusable}\``, "", answer.value, "");
  }
  return lines.join("\n");
}

function issuesMarkdown(value) {
  const issues = Array.isArray(value?.issues) ? value.issues : [];
  const lines = ["# Job Radar application issues", "", "Sanitized adapter observations. A code change is made only when Victor explicitly asks for repair.", ""];
  for (const issue of issues) {
    lines.push(`## ${clean(issue.created_at, 80)} · ${clean(issue.type, 80)}`, "", `- Status: ${clean(issue.status, 30)}`, `- Provider: ${clean(issue.provider, 40)}`, `- Field: ${clean(issue.field, 300)}`, `- Page: ${clean(issue.page, 240)}`, `- Fingerprint: ${clean(issue.fingerprint, 100)}`, "", clean(issue.message, 800), "");
  }
  return lines.join("\n");
}

function hashToken(token) {
  return crypto.createHash("sha256").update(String(token || "")).digest("hex");
}

function tokenMatches(token, hash) {
  const left = Buffer.from(hashToken(token));
  const right = Buffer.from(clean(hash, 128));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

async function ownerAccess(req) {
  const auth = ownerSession(req);
  if (auth.error) throw Object.assign(new Error(auth.error), {statusCode: auth.error === "sign in first" ? 401 : 403});
  const access = await tracker.resumeStorageAccess(auth.current.pt, true);
  return {...access, current: auth.current, owner: true};
}

async function tokenAccess(req) {
  const token = clean(req.headers["x-job-radar-agent"] || req.query?.agent_token, 180);
  if (!token) return null;
  const access = await tracker.resumeStorageAccess(null, true);
  const pairing = await appDocument(access, "pairing");
  const valid = (pairing.value.tokens || []).find(item => item?.revoked_at ? false : tokenMatches(token, item?.token_hash));
  if (!valid) throw Object.assign(new Error("Mac pairing token is invalid or revoked"), {statusCode: 403});
  return {...access, folder: pairing.folder, owner: false, agent: true};
}

async function accessFor(req, mutation = false) {
  const paired = await tokenAccess(req);
  if (paired) return paired;
  return ownerAccess(req);
}

async function createPairing(req, res, access) {
  const token = crypto.randomBytes(32).toString("base64url");
  const current = await appDocument(access, "pairing");
  const now = new Date().toISOString();
  const tokens = (Array.isArray(current.value.tokens) ? current.value.tokens : []).map(item => item && !item.revoked_at ? {...item, revoked_at: now} : item);
  tokens.push({token_hash: hashToken(token), created_at: now, label: "Mac application agent"});
  await writeAppDocument(current, "pairing", {tokens});
  res.status(200).json({ok: true, agent_token: token, message: "Copy this token into the private Mac extension setup. It is shown once and can be revoked from Job Radar."});
}

async function updateQueue(access, payload) {
  const current = await appDocument(access, "queue");
  const queue = queueDocument(current.value);
  const now = new Date().toISOString();
  let item = null;
  if (payload.action === "queue") {
    const job = safeJob(payload.job);
    if (!job) throw new Error("valid job snapshot required");
    item = queue.items.find(candidate => candidate.job?.id === job.id && ACTIVE_QUEUE_STATES.has(candidate.state));
    if (item) return {item, duplicate: true};
    item = publicQueueItem({queue_id: crypto.randomBytes(14).toString("hex"), state: "queued", session_id: "", message: "Waiting for the paired Mac browser", error: "", created_at: now, updated_at: now, job, blockers: [], review: null, confirmation: null});
    queue.items = [item, ...queue.items].slice(0, MAX_QUEUE);
  } else {
    const queueId = clean(payload.queue_id, 100);
    item = queue.items.find(candidate => candidate.queue_id === queueId);
    if (!item) throw new Error("application queue item not found");
    if (payload.state && !QUEUE_STATES.has(payload.state)) throw new Error("invalid application queue state");
    if (payload.state) item.state = payload.state;
    if (payload.session_id !== undefined) item.session_id = clean(payload.session_id, 100);
    if (payload.message !== undefined) item.message = clean(payload.message, 800);
    if (payload.error !== undefined) item.error = clean(payload.error, 800);
    if (payload.resume !== undefined) item.resume = publicQueueItem({resume: payload.resume}).resume;
    if (Array.isArray(payload.blockers)) item.blockers = payload.blockers.slice(0, 80);
    if (payload.review !== undefined) item.review = publicReview(payload.review);
    if (payload.confirmation !== undefined) item.confirmation = payload.confirmation;
    if (payload.state && ["submitted", "failed", "skipped", "cancelled"].includes(payload.state)) {
      item.review = null;
      item.confirmation = null;
    }
    item.updated_at = now;
  }
  const written = await writeAppDocument(current, "queue", {items: queue.items});
  return {item: publicQueueItem(written.value.items.find(candidate => candidate.queue_id === item.queue_id) || item), duplicate: false};
}

function reviewExpired(review) {
  const expiry = Date.parse(String(review?.expires_at || ""));
  return !Number.isFinite(expiry) || expiry < Date.now();
}

async function confirmQueue(access, payload) {
  const current = await appDocument(access, "queue");
  const queue = queueDocument(current.value);
  const item = queue.items.find(candidate => candidate.queue_id === clean(payload.queue_id, 100));
  if (!item || !item.review) throw new Error("application review card not found");
  if (item.confirmation || item.state === "submitting") throw new Error("application review confirmation has already been consumed");
  if (reviewExpired(item.review)) throw new Error("application review expired; reopen the page");
  const storedNonce = Buffer.from(clean(item.review.nonce, 100));
  const suppliedNonce = Buffer.from(clean(payload.nonce, 100));
  const nonceMatches = storedNonce.length === suppliedNonce.length && crypto.timingSafeEqual(storedNonce, suppliedNonce);
  if (clean(item.review.review_hash, 100) !== clean(payload.review_hash, 100) || !nonceMatches) {
    throw new Error("application review token is invalid");
  }
  if (payload.page_fingerprint && clean(item.review.page_fingerprint, 100) !== clean(payload.page_fingerprint, 100)) {
    throw new Error("the application page changed; reopen the review card");
  }
  item.confirmation = {
    review_hash: item.review.review_hash,
    nonce: item.review.nonce,
    page_fingerprint: clean(item.review.page_fingerprint, 100),
    expires_at: item.review.expires_at,
    approved_at: new Date().toISOString(),
  };
  item.state = "submitting";
  item.message = "Owner confirmed. The paired Mac may submit only on the matching page.";
  item.updated_at = new Date().toISOString();
  await writeAppDocument(current, "queue", {items: queue.items});
  return {ok: true, item: publicQueueItem(item)};
}

async function saveContext(access, payload) {
  const current = await appDocument(access, "context");
  const context = contextDocument(current.value);
  const answer = payload.answer && typeof payload.answer === "object" ? payload.answer : payload;
  const question = clean(answer.question || answer.label, 1200);
  const value = clean(answer.value || answer.answer, 20000);
  if (!question || !value) throw new Error("question and answer are required");
  const normalized = clean(answer.normalized_question || question.toLowerCase().replace(/[^a-z0-9@+_.?/-]+/g, " ").replace(/\s+/g, " "), 1200);
  const answerId = clean(answer.answer_id, 100) || hashToken(`${clean(answer.category, 80) || "other"}:${normalized}`).slice(0, 24);
  const existing = context.answers.find(item => item.answer_id === answerId) || {};
  const category = clean(answer.category, 80) || "other";
  const next = {
    answer_id: answerId, question, normalized_question: normalized,
    variants: [...new Set([...(Array.isArray(answer.variants) ? answer.variants : []), ...(existing.variants || [])].map(item => clean(item, 1200)).filter(Boolean))].slice(0, 30),
    category, value, reusable: answer.reusable !== false,
    sensitive: Boolean(answer.sensitive) || SENSITIVE_CATEGORIES.has(category), evidence_ids: Array.isArray(answer.evidence_ids) ? answer.evidence_ids.map(item => clean(item, 140)).slice(0, 20) : [],
    updated_at: new Date().toISOString(),
  };
  context.answers = [next, ...context.answers.filter(item => item.answer_id !== answerId)].slice(0, MAX_ANSWERS);
  if (answer.field_key) context.mappings[clean(answer.field_key, 240)] = answerId;
  const written = await writeAppDocument(current, "context", context);
  if (current.storage !== "sheet") await writeTextDocument(current.token, current.folder, "contextMarkdown", contextMarkdown(written.value));
  return {ok: true, answer: next, context: contextDocument(written.value)};
}

async function saveMapping(access, payload) {
  const current = await appDocument(access, "context");
  const context = contextDocument(current.value);
  const key = clean(payload.field_key, 240), answerId = clean(payload.answer_id, 100);
  if (!key || !context.answers.some(item => item.answer_id === answerId)) throw new Error("valid field mapping required");
  context.mappings[key] = answerId;
  const written = await writeAppDocument(current, "context", context);
  if (current.storage !== "sheet") await writeTextDocument(current.token, current.folder, "contextMarkdown", contextMarkdown(written.value));
  return {ok: true, field_key: key, answer_id: answerId};
}

async function saveIssue(access, payload) {
  const current = await appDocument(access, "issues");
  const issue = {
    issue_id: clean(payload.issue_id, 100) || crypto.randomBytes(10).toString("hex"),
    session_id: clean(payload.session_id, 100), type: clean(payload.issue_type || payload.type, 80) || "unknown",
    message: clean(payload.message, 800), field: clean(payload.field_label || payload.field, 300),
    provider: clean(payload.provider, 40), page: clean(payload.page, 240), fingerprint: clean(payload.fingerprint, 100),
    selector_kind: clean(payload.selector_kind, 80), status: "open", created_at: new Date().toISOString(),
  };
  const issues = [{...issue}, ...(Array.isArray(current.value.issues) ? current.value.issues : [])].slice(0, MAX_ISSUES);
  const written = await writeAppDocument(current, "issues", {issues});
  if (current.storage !== "sheet") await writeTextDocument(current.token, current.folder, "issuesMarkdown", issuesMarkdown(written.value));
  return {ok: true, issue};
}

async function readView(access, view, sessionId = "") {
  if (view === "context") {
    const result = await appDocument(access, "context");
    return {version: VERSION, connected: true, storage: result.storage, context: contextDocument(result.value)};
  }
  if (view === "issues") {
    const result = await appDocument(access, "issues");
    return {version: VERSION, connected: true, storage: result.storage, issues: Array.isArray(result.value.issues) ? result.value.issues.slice(0, MAX_ISSUES) : []};
  }
  const result = await appDocument(access, "queue");
  const queue = queueDocument(result.value);
  if (view === "session") {
    return {version: VERSION, connected: true, storage: result.storage, session: queue.items.find(item => item.session_id === clean(sessionId, 100)) || null};
  }
  return {version: VERSION, connected: true, storage: result.storage, items: queue.items.map(publicQueueItem)};
}

module.exports = async (req, res) => {
  try {
    if (req.method === "GET") {
      const access = await accessFor(req, false);
      const view = clean(req.query?.view || "queue", 40);
      res.status(200).json(await readView(access, view, req.query?.session_id));
      return;
    }
    if (req.method !== "POST") { res.status(405).end(); return; }
    const payload = bodyOf(req);
    const agentAccess = await tokenAccess(req);
    if (!agentAccess && !requireMutationRequest(req, res)) return;
    const access = agentAccess || await ownerAccess(req);
    const action = clean(payload.action, 40);
    if (action === "pair") {
      if (agentAccess) throw Object.assign(new Error("owner session required to pair a Mac"), {statusCode: 403});
      await createPairing(req, res, access);
      return;
    }
    if (action === "revoke_pair") {
      const current = await appDocument(access, "pairing");
      const tokenHash = clean(payload.token_hash, 128);
      const tokens = (current.value.tokens || []).map(item => item.token_hash === tokenHash ? {...item, revoked_at: new Date().toISOString()} : item);
      await writeAppDocument(current, "pairing", {tokens});
      res.status(200).json({ok: true}); return;
    }
    if (action === "queue" || action === "queue_update") {
      res.status(200).json({ok: true, ...(await updateQueue(access, {...payload, action: action === "queue" ? "queue" : "queue_update"}))}); return;
    }
    if (action === "confirm") {
      if (agentAccess) throw Object.assign(new Error("owner session required to confirm an application"), {statusCode: 403});
      res.status(200).json(await confirmQueue(access, payload)); return;
    }
    if (action === "answer") { res.status(200).json(await saveContext(access, payload)); return; }
    if (action === "save_mapping") { res.status(200).json(await saveMapping(access, payload)); return; }
    if (action === "report_issue") { res.status(200).json(await saveIssue(access, payload)); return; }
    throw Object.assign(new Error("unsupported application-agent action"), {statusCode: 400});
  } catch (error) {
    const status = Number(error.statusCode) || (/Connect Google/i.test(String(error.message || "")) ? 503 : 502);
    if (!res.headersSent) res.status(status).json({error: String(error.message || error).slice(0, 300), needs_google: status === 503});
  }
};

module.exports.config = {api: {bodyParser: {sizeLimit: "2mb"}}};
