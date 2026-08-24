// Owner-only application-agent control plane.
//
// The Mac browser remains the execution boundary.  This route stores only
// the private application context, queue state, issue ledger, and an
// explicit short-lived review card in the owner's app-created Drive folder.
// It never stores a CV, browser cookie, provider session, or DOM dump.
const crypto = require("crypto");
const { OWNER, session, requireMutationRequest } = require("./_lib");
const tracker = require("./_google-tracker");

const DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files";
const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files";
const VERSION = "application-agent-v1";
const FOLDER_PROPERTY = "jobRadarApplicationAgent";
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
  return {
    queue_id: clean(value.queue_id, 100), state: QUEUE_STATES.has(value.state) ? value.state : "queued",
    session_id: clean(value.session_id, 100), message: clean(value.message, 800), error: clean(value.error, 800),
    created_at: clean(value.created_at, 80), updated_at: clean(value.updated_at, 80),
    job: safeJob(value.job), blockers: Array.isArray(value.blockers) ? value.blockers.slice(0, 80) : [],
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
  const access = await tracker.resumeDriveAccess(auth.current.pt, true);
  return {access, current: auth.current, owner: true};
}

async function tokenAccess(req) {
  const token = clean(req.headers["x-job-radar-agent"] || req.query?.agent_token, 180);
  if (!token) return null;
  const access = await tracker.resumeDriveAccess(null, true);
  const folder = await ensureFolder(access.token);
  const pairing = await readDocument(access.token, folder, "pairing");
  const valid = (pairing.value.tokens || []).find(item => item?.revoked_at ? false : tokenMatches(token, item?.token_hash));
  if (!valid) throw Object.assign(new Error("Mac pairing token is invalid or revoked"), {statusCode: 403});
  return {access, folder, owner: false, agent: true};
}

async function accessFor(req, mutation = false) {
  const paired = await tokenAccess(req);
  if (paired) return paired;
  return ownerAccess(req);
}

async function createPairing(req, res, access) {
  const token = crypto.randomBytes(32).toString("base64url");
  const folder = await ensureFolder(access.token);
  const current = await readDocument(access.token, folder, "pairing");
  const now = new Date().toISOString();
  const tokens = (Array.isArray(current.value.tokens) ? current.value.tokens : []).map(item => item && !item.revoked_at ? {...item, revoked_at: now} : item);
  tokens.push({token_hash: hashToken(token), created_at: now, label: "Mac application agent"});
  await writeDocument(access.token, folder, current, "pairing", {tokens});
  res.status(200).json({ok: true, agent_token: token, message: "Copy this token into the private Mac extension setup. It is shown once and can be revoked from Job Radar."});
}

async function updateQueue(access, payload) {
  const folder = access.folder || await ensureFolder(access.access?.token || access.token);
  const token = access.access?.token || access.token;
  const current = await readDocument(token, folder, "queue");
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
    if (Array.isArray(payload.blockers)) item.blockers = payload.blockers.slice(0, 80);
    if (payload.review !== undefined) item.review = publicReview(payload.review);
    if (payload.confirmation !== undefined) item.confirmation = payload.confirmation;
    if (payload.state && ["submitted", "failed", "skipped", "cancelled"].includes(payload.state)) {
      item.review = null;
      item.confirmation = null;
    }
    item.updated_at = now;
  }
  const written = await writeDocument(token, folder, current, "queue", {items: queue.items});
  return {item: publicQueueItem(written.value.items.find(candidate => candidate.queue_id === item.queue_id) || item), duplicate: false};
}

function reviewExpired(review) {
  const expiry = Date.parse(String(review?.expires_at || ""));
  return !Number.isFinite(expiry) || expiry < Date.now();
}

async function confirmQueue(access, payload) {
  const token = access.access?.token || access.token;
  const folder = access.folder || await ensureFolder(token);
  const current = await readDocument(token, folder, "queue");
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
  await writeDocument(token, folder, current, "queue", {items: queue.items});
  return {ok: true, item: publicQueueItem(item)};
}

async function saveContext(access, payload) {
  const token = access.access?.token || access.token;
  const folder = access.folder || await ensureFolder(token);
  const current = await readDocument(token, folder, "context");
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
  const written = await writeDocument(token, folder, current, "context", context);
  await writeTextDocument(token, folder, "contextMarkdown", contextMarkdown(written.value));
  return {ok: true, answer: next, context: contextDocument(written.value)};
}

async function saveMapping(access, payload) {
  const token = access.access?.token || access.token;
  const folder = access.folder || await ensureFolder(token);
  const current = await readDocument(token, folder, "context");
  const context = contextDocument(current.value);
  const key = clean(payload.field_key, 240), answerId = clean(payload.answer_id, 100);
  if (!key || !context.answers.some(item => item.answer_id === answerId)) throw new Error("valid field mapping required");
  context.mappings[key] = answerId;
  const written = await writeDocument(token, folder, current, "context", context);
  await writeTextDocument(token, folder, "contextMarkdown", contextMarkdown(written.value));
  return {ok: true, field_key: key, answer_id: answerId};
}

async function saveIssue(access, payload) {
  const token = access.access?.token || access.token;
  const folder = access.folder || await ensureFolder(token);
  const current = await readDocument(token, folder, "issues");
  const issue = {
    issue_id: clean(payload.issue_id, 100) || crypto.randomBytes(10).toString("hex"),
    session_id: clean(payload.session_id, 100), type: clean(payload.issue_type || payload.type, 80) || "unknown",
    message: clean(payload.message, 800), field: clean(payload.field_label || payload.field, 300),
    provider: clean(payload.provider, 40), page: clean(payload.page, 240), fingerprint: clean(payload.fingerprint, 100),
    selector_kind: clean(payload.selector_kind, 80), status: "open", created_at: new Date().toISOString(),
  };
  const issues = [{...issue}, ...(Array.isArray(current.value.issues) ? current.value.issues : [])].slice(0, MAX_ISSUES);
  const written = await writeDocument(token, folder, current, "issues", {issues});
  await writeTextDocument(token, folder, "issuesMarkdown", issuesMarkdown(written.value));
  return {ok: true, issue};
}

async function readView(access, view, sessionId = "") {
  const token = access.access?.token || access.token;
  const folder = access.folder || await ensureFolder(token);
  if (view === "context") {
    const result = await readDocument(token, folder, "context");
    return {version: VERSION, connected: true, context: contextDocument(result.value)};
  }
  if (view === "issues") {
    const result = await readDocument(token, folder, "issues");
    return {version: VERSION, connected: true, issues: Array.isArray(result.value.issues) ? result.value.issues.slice(0, MAX_ISSUES) : []};
  }
  const result = await readDocument(token, folder, "queue");
  const queue = queueDocument(result.value);
  if (view === "session") {
    return {version: VERSION, connected: true, session: queue.items.find(item => item.session_id === clean(sessionId, 100)) || null};
  }
  return {version: VERSION, connected: true, items: queue.items.map(publicQueueItem)};
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
      await createPairing(req, res, access.access);
      return;
    }
    if (action === "revoke_pair") {
      const token = access.access.token;
      const folder = await ensureFolder(token);
      const current = await readDocument(token, folder, "pairing");
      const tokenHash = clean(payload.token_hash, 128);
      const tokens = (current.value.tokens || []).map(item => item.token_hash === tokenHash ? {...item, revoked_at: new Date().toISOString()} : item);
      await writeDocument(token, folder, current, "pairing", {tokens});
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
