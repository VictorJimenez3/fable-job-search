// Private, owner-only Resume Bank storage.
//
// The public repository remains CV-free. The bank is synced into an
// app-created folder in Victor's Google Drive, then this API proxies only the
// authenticated owner's index and artifacts. Drive is used here because the
// platform already has a least-privilege drive.file OAuth path for the owner;
// no public blob URL or repository commit is created.
const crypto = require("crypto");
const { OWNER, session } = require("./_lib");
const tracker = require("./_google-tracker");

const DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files";
const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files";
const BANK_VERSION = "v1";
// Keep the decoded artifacts for one sync request below the Vercel function
// body limit after base64 expansion and the surrounding JSON are accounted for.
const MAX_ENTRY_ARTIFACT_BYTES = 2_750_000;
const MAX_ENTRIES = 500;
const INDEX_FILENAME = "resume-bank-index.json";

function bodyOf(req) {
  if (req.body && typeof req.body === "object") return req.body;
  if (typeof req.body === "string") {
    try { return JSON.parse(req.body || "{}"); } catch { return {}; }
  }
  return {};
}

function sendJson(res, status, value) {
  res.status(status).json(value);
}

function ownerSession(req) {
  const current = session(req);
  if (!current) return {error: "sign in first"};
  const githubLogin = current.github?.login || (current.g ? current.u : "");
  if (githubLogin !== OWNER) return {error: "Resume Bank is private to the repository owner"};
  return {current};
}

function clean(value, max = 500) {
  return String(value ?? "").trim().slice(0, max);
}

function safeName(value) {
  const name = clean(value, 180).split("/").pop().split("\\").pop();
  if (!name) return "";
  return /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name) ? name : "artifact.bin";
}

function entryKey(entry) {
  return crypto.createHash("sha256")
    .update(`${clean(entry?.source, 40)}:${clean(entry?.entry_id, 120)}`)
    .digest("hex").slice(0, 28);
}

function entryId(entry) {
  return `${clean(entry?.source, 40)}:${clean(entry?.entry_id, 120)}`;
}

function driveHeaders(token, extra = {}) {
  return {Authorization: `Bearer ${token}`, ...extra};
}

async function driveJson(url, token, options = {}) {
  const response = await fetch(url, {...options, headers: driveHeaders(token, options.headers || {})});
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) {
    const message = data.error?.message || data.error || `Google Drive ${response.status}`;
    throw new Error(String(message).slice(0, 220));
  }
  return data;
}

async function listFiles(token, query, fields = "files(id,name,mimeType,appProperties,parents,size,modifiedTime)") {
  const params = new URLSearchParams({q: query, spaces: "drive", pageSize: "1000", fields});
  const data = await driveJson(`${DRIVE_FILES_API}?${params.toString()}`, token);
  return Array.isArray(data.files) ? data.files : [];
}

async function ensureFolder(token) {
  const files = await listFiles(token,
    "trashed = false and mimeType = 'application/vnd.google-apps.folder' " +
    "and appProperties has { key='jobRadarResumeBank' and value='v1' }");
  if (files[0]?.id) return files[0];
  return driveJson(DRIVE_FILES_API, token, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: "Job Radar Resume Bank", mimeType: "application/vnd.google-apps.folder",
      appProperties: {jobRadarResumeBank: BANK_VERSION}}),
  });
}

async function uploadFile(token, {id = "", metadata, content, contentType}) {
  const boundary = `jobradar-${crypto.randomBytes(12).toString("hex")}`;
  const preamble = Buffer.from(
    `--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${JSON.stringify(metadata)}\r\n` +
    `--${boundary}\r\nContent-Type: ${contentType}\r\n\r\n`, "utf8");
  const ending = Buffer.from(`\r\n--${boundary}--\r\n`, "utf8");
  const data = Buffer.isBuffer(content) ? content : Buffer.from(content);
  const url = `${DRIVE_UPLOAD_API}${id ? `/${encodeURIComponent(id)}` : ""}?uploadType=multipart`;
  return driveJson(url, token, {
    method: id ? "PATCH" : "POST",
    headers: {"Content-Type": `multipart/related; boundary=${boundary}`},
    body: Buffer.concat([preamble, data, ending]),
  });
}

async function readFile(token, id) {
  const response = await fetch(`${DRIVE_FILES_API}/${encodeURIComponent(id)}?alt=media`,
    {headers: driveHeaders(token)});
  if (!response.ok) throw new Error(`Google Drive artifact ${response.status}`);
  return Buffer.from(await response.arrayBuffer());
}

async function bankIndex(token, folder) {
  const files = await listFiles(token,
    `'${folder.id}' in parents and trashed = false and name = '${INDEX_FILENAME}' and ` +
    "appProperties has { key='jobRadarResumeBankIndex' and value='v1' }");
  if (!files[0]?.id) return {file: null, index: {version: BANK_VERSION, updated_at: "", entries: []}};
  let index = {};
  try { index = JSON.parse((await readFile(token, files[0].id)).toString("utf8")); } catch {}
  if (!index || typeof index !== "object") index = {};
  if (!Array.isArray(index.entries)) index.entries = [];
  return {file: files[0], index: {version: BANK_VERSION, updated_at: clean(index.updated_at, 80), entries: index.entries.slice(0, MAX_ENTRIES)}};
}

async function writeIndex(token, folder, current, index) {
  const metadata = {name: INDEX_FILENAME, mimeType: "application/json",
    appProperties: {jobRadarResumeBankIndex: BANK_VERSION}};
  if (!current.file?.id) metadata.parents = [folder.id];
  const content = Buffer.from(JSON.stringify({...index, version: BANK_VERSION, updated_at: new Date().toISOString()}));
  return uploadFile(token, {id: current.file?.id || "", metadata, content, contentType: "application/json"});
}

function normalizedEntry(value) {
  const entry = value && typeof value === "object" ? value : {};
  const job = entry.job && typeof entry.job === "object" ? entry.job : {};
  const artifacts = Array.isArray(entry.artifacts) ? entry.artifacts.map(safeName).filter(Boolean).slice(0, 30) : [];
  return {
    entry_id: clean(entry.entry_id, 120), source: clean(entry.source, 40), legacy: Boolean(entry.legacy),
    run_id: clean(entry.run_id, 120), status: clean(entry.status, 40), step: clean(entry.step, 160),
    message: clean(entry.message, 500), mode: clean(entry.mode, 40), created_at: clean(entry.created_at, 80),
    updated_at: clean(entry.updated_at, 80), pdf_filename: safeName(entry.pdf_filename),
    preview_filename: entry.preview_filename ? safeName(entry.preview_filename) : "",
    has_pdf: Boolean(entry.has_pdf), has_posting_snapshot: Boolean(entry.has_posting_snapshot),
    has_workshop: Boolean(entry.has_workshop), craft_score: entry.craft_score ?? null,
    ready: entry.ready === true, validation_warnings: Array.isArray(entry.validation_warnings)
      ? entry.validation_warnings.map(item => clean(item, 280)).slice(0, 30) : [],
    artifacts,
    job: {
      id: clean(job.id, 140), company: clean(job.company, 220), title: clean(job.title, 320),
      url: clean(job.url, 2000), locations: Array.isArray(job.locations)
        ? job.locations.map(item => clean(item, 160)).slice(0, 12) : [],
      sector: clean(job.sector, 120), score: Number.isFinite(Number(job.score)) ? Number(job.score) : 0,
      alert_ok: Boolean(job.alert_ok), early_career_possible: Boolean(job.early_career_possible),
      posted_at: job.posted_at ?? null,
      resume_match: job.resume_match && typeof job.resume_match === "object" ? {
        score: Number(job.resume_match.score || 0), confidence: clean(job.resume_match.confidence, 20),
        version: clean(job.resume_match.version, 80),
      } : null,
    },
  };
}

function publicEntries(index) {
  return index.entries.map(item => {
    const entry = normalizedEntry(item);
    const refs = item.artifact_refs && typeof item.artifact_refs === "object" ? item.artifact_refs : {};
    const urlFor = name => refs[name]?.file_id
      ? `/api/resume-bank?artifact=${encodeURIComponent(refs[name].file_id)}` : "";
    entry.storage = "cloud";
    entry.synced_at = clean(item.synced_at, 80);
    entry.artifact_status = {};
    entry.urls = {
      pdf: urlFor(entry.pdf_filename),
      preview: urlFor(entry.preview_filename),
      report: urlFor("report.json"),
      posting: urlFor("posting.json"),
      workshop: "",
    };
    for (const [name, ref] of Object.entries(refs)) {
      if (ref?.file_id) entry.artifact_status[name] = {size: Number(ref.size || 0), content_type: clean(ref.content_type, 120), sha256: clean(ref.sha256, 80)};
    }
    return entry;
  });
}

function findArtifact(index, id) {
  for (const entry of index.entries) {
    for (const ref of Object.values(entry.artifact_refs || {})) {
      if (ref?.file_id === id) return ref;
    }
  }
  return null;
}

async function contextFor(req) {
  const auth = ownerSession(req);
  if (auth.error) throw Object.assign(new Error(auth.error), {statusCode: auth.error === "sign in first" ? 401 : 403});
  return tracker.resumeDriveAccess(auth.current.pt, true);
}

async function getBank(token) {
  const folder = await ensureFolder(token);
  const current = await bankIndex(token, folder);
  return {folder, current};
}

async function syncEntry(token, value, artifact, artifactList = []) {
  const entry = normalizedEntry(value);
  if (!entry.entry_id || !entry.source || !entry.job.id) throw new Error("invalid Resume Bank entry");
  const folder = await ensureFolder(token);
  const current = await bankIndex(token, folder);
  const key = entryId(entry);
  let stored = current.index.entries.find(item => entryId(item) === key);
  if (!stored) { stored = {...entry, artifact_refs: {}}; current.index.entries.unshift(stored); }
  else {
    const refs = stored.artifact_refs || {};
    Object.assign(stored, entry, {artifact_refs: refs});
  }
  stored.artifact_refs = stored.artifact_refs || {};
  const uploads = [];
  if (artifact && typeof artifact === "object") uploads.push(artifact);
  if (Array.isArray(artifactList)) uploads.push(...artifactList.slice(0, 12));
  let totalArtifactBytes = 0;
  for (const candidate of uploads) {
    if (!candidate?.name || !candidate?.data_base64) continue;
    const name = safeName(candidate.name);
    if (!name) continue;
    const data = Buffer.from(String(candidate.data_base64), "base64");
    totalArtifactBytes += data.length;
    if (!data.length || totalArtifactBytes > MAX_ENTRY_ARTIFACT_BYTES) throw new Error("Resume Bank artifacts are too large");
    const contentType = clean(candidate.content_type, 120) || "application/octet-stream";
    const keyHash = entryKey(entry);
    const existing = stored.artifact_refs[name];
    const metadata = {name: `resume-bank-${keyHash}-${name}`, parents: [folder.id], mimeType: contentType,
      appProperties: {jobRadarResumeBankArtifact: BANK_VERSION, resumeBankEntry: keyHash, resumeBankArtifact: name}};
    const file = await uploadFile(token, {id: existing?.file_id || "", metadata, content: data, contentType});
    stored.artifact_refs[name] = {file_id: file.id, name, content_type: contentType, size: data.length,
      sha256: crypto.createHash("sha256").update(data).digest("hex")};
  }
  stored.synced_at = new Date().toISOString();
  current.index.entries = current.index.entries.slice(0, MAX_ENTRIES);
  await writeIndex(token, folder, current, current.index);
  return normalizedEntry(stored);
}

module.exports = async (req, res) => {
  try {
    const access = await contextFor(req);
    if (req.method === "GET") {
      const {current} = await getBank(access.token);
      const artifactId = clean(req.query?.artifact, 160);
      if (artifactId) {
        if (!/^[A-Za-z0-9_-]+$/.test(artifactId) || !findArtifact(current.index, artifactId)) {
          sendJson(res, 404, {error: "artifact not found"}); return;
        }
        const ref = findArtifact(current.index, artifactId);
        const data = await readFile(access.token, artifactId);
        res.setHeader("Content-Type", ref.content_type || "application/octet-stream");
        res.setHeader("Content-Length", String(data.length));
        res.setHeader("Cache-Control", "private, no-store");
        res.setHeader("Content-Disposition", /^(application\/pdf|image\/png)$/.test(ref.content_type || "") ? "inline" : "attachment");
        res.status(200).end(data); return;
      }
      res.status(200).json({configured: true, connected: true, source: access.source,
        updated_at: current.index.updated_at, resumes: publicEntries(current.index)}); return;
    }
    if (req.method === "POST") {
      const payload = bodyOf(req);
      const entry = await syncEntry(access.token, payload.entry, payload.artifact, payload.artifacts);
      res.status(200).json({ok: true, entry: publicEntries({entries: [{...entry, artifact_refs: {}}]})[0]}); return;
    }
    res.status(405).end();
  } catch (error) {
    const status = Number(error.statusCode) || (/Connect Google/.test(String(error.message || "")) ? 503 : 502);
    res.status(status).json({error: String(error.message || error).slice(0, 240), needs_google: status === 503});
  }
};

module.exports.config = {api: {bodyParser: {sizeLimit: "4mb"}}};
