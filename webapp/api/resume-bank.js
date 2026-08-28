// Private, owner-only Resume Bank storage.
//
// The public repository remains CV-free. The bank is synced into an
// app-created folder in Victor's Google Drive, then this API proxies only the
// authenticated owner's index and artifacts. Drive is used here because the
// platform already has a least-privilege drive.file OAuth path for the owner;
// no public blob URL or repository commit is created.
const crypto = require("crypto");
const { OWNER, session, requireMutationRequest } = require("./_lib");
const tracker = require("./_google-tracker");
const applicationAgent = require("./_application-agent");

const DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files";
const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files";
const BANK_VERSION = "v1";
// Keep the decoded artifacts for one sync request below the Vercel function
// body limit after base64 expansion and the surrounding JSON are accounted for.
const MAX_ENTRY_ARTIFACT_BYTES = 2_750_000;
const MAX_ENTRIES = 500;
const INDEX_FILENAME = "resume-bank-index.json";
const QUEUE_VERSION = "v1";
const QUEUE_FILENAME = "resume-studio-cloud-queue.json";
const CONTROL_VERSION = "v1";
const CONTROL_FILENAME = "resume-studio-control-profiles.json";
const CONTROL_ROLE_FAMILIES = new Set([
  "general_swe_cloud", "healthcare_scientific_ai", "ml_research", "data_analytics", "other",
]);
const MAX_QUEUE_ITEMS = 500;
const QUEUE_MODES = new Set(["used", "ai", "unrestricted", "generation"]);
const QUEUE_STATES = new Set(["queued", "dispatching", "running", "awaiting_review", "complete", "failed", "cancelled"]);

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

function auditSummary(value) {
  const audit = value && typeof value === "object" ? value : {};
  const counts = audit.finding_counts && typeof audit.finding_counts === "object" ? audit.finding_counts : {};
  const fit = audit.fit && typeof audit.fit === "object" ? audit.fit.band : audit.fit;
  const available = audit.available === true || Boolean(audit.version && (audit.readiness || audit.findings || fit));
  const list = (field, limit) => Array.isArray(audit[field])
    ? audit[field].map(item => clean(item, 240)).filter(Boolean).slice(0, limit)
    : [];
  const control = audit.comparison_control && typeof audit.comparison_control === "object" ? audit.comparison_control : {};
  const controlList = (field, limit) => Array.isArray(control[field])
    ? control[field].map(item => clean(item, 240)).filter(Boolean).slice(0, limit)
    : [];
  return {
    version: clean(audit.version, 80), available,
    status: clean(audit.status, 24), readiness: clean(audit.readiness, 24),
    fit: clean(fit, 24), tailoring: clean(audit.tailoring, 24),
    confidence: clean(audit.confidence, 24),
    run_id: clean(audit.run_id, 80), queue_id: clean(audit.queue_id, 80),
    finding_counts: Object.fromEntries(Object.entries(counts).slice(0, 8).map(([key, value]) => [clean(key, 60), Number(value || 0)])),
    blockers: list("blockers", 6), gains: list("gains", 4), losses: list("losses", 6),
    comparison_control: {
      id: clean(control.id, 120), label: clean(control.label, 180), role_family: clean(control.role_family, 80),
      source: clean(control.source, 40), entry_id: clean(control.entry_id, 120), run_id: clean(control.run_id, 80),
      artifact: clean(control.artifact, 220), available: control.available === true,
      approved: control.approved === true, reference_only: control.reference_only === true,
      scope: clean(control.scope, 40), warning: clean(control.warning, 280),
      lost_terms: controlList("lost_terms", 30), gained_terms: controlList("gained_terms", 30),
      lost_signal_families: controlList("lost_signal_families", 20),
      candidate_covered_count: Number(control.candidate_covered_count || 0),
      baseline_covered_count: Number(control.baseline_covered_count || 0),
      candidate_coverage_percent: Number.isFinite(Number(control.candidate_coverage_percent)) ? Number(control.candidate_coverage_percent) : null,
      baseline_coverage_percent: Number.isFinite(Number(control.baseline_coverage_percent)) ? Number(control.baseline_coverage_percent) : null,
    },
    hash: clean(audit.hash, 80),
  };
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

function normalizedControl(value) {
  const control = value && typeof value === "object" ? value : {};
  const family = control.id === "immutable-default" ? "all" : String(control.role_family || "");
  return {
    id: clean(control.id, 120), label: clean(control.label, 180),
    role_family: family === "all" || CONTROL_ROLE_FAMILIES.has(family) ? family : "other",
    source: clean(control.source, 40), entry_id: clean(control.entry_id, 120),
    run_id: clean(control.run_id || control.entry_id, 80),
    artifact: clean(control.artifact, 220), status: ["active", "revoked"].includes(control.status)
      ? control.status : "active",
    approved_at: clean(control.approved_at, 80), revoked_at: clean(control.revoked_at, 80),
    approved_by: clean(control.approved_by, 80),
  };
}

function immutableControl() {
  return {
    id: "immutable-default", label: "Immutable default", role_family: "all",
    source: "immutable", entry_id: "", run_id: "",
    artifact: "immutable canonical resume", status: "active",
    approved_at: "implicit", revoked_at: "", approved_by: "system",
    immutable: true, always_available: true,
  };
}

async function controlIndex(token, folder) {
  const files = await listFiles(token,
    `'${folder.id}' in parents and trashed = false and name = '${CONTROL_FILENAME}' and ` +
    "appProperties has { key='resumeStudioControlProfiles' and value='v1' }",
    "files(id,name,mimeType,appProperties,parents,size,modifiedTime)");
  const file = files[0] || null;
  let profiles = [];
  let updated_at = "";
  if (file?.id) {
    try {
      const parsed = JSON.parse((await readFile(token, file.id)).toString("utf8"));
      profiles = Array.isArray(parsed?.profiles) ? parsed.profiles.map(normalizedControl).filter(item => item.id) : [];
      updated_at = clean(parsed?.updated_at, 80);
    } catch {}
  }
  return {file, index: {version: CONTROL_VERSION, updated_at, profiles: profiles.slice(0, 100)}};
}

async function writeControlIndex(token, folder, current, profiles) {
  const metadata = {name: CONTROL_FILENAME, mimeType: "application/json",
    appProperties: {resumeStudioControlProfiles: CONTROL_VERSION}};
  if (!current.file?.id) metadata.parents = [folder.id];
  const content = Buffer.from(JSON.stringify({version: CONTROL_VERSION,
    updated_at: new Date().toISOString(), profiles: profiles.slice(0, 100).map(normalizedControl)}));
  return uploadFile(token, {id: current.file?.id || "", metadata, content, contentType: "application/json"});
}

function publicControls(current) {
  const index = current?.index || {};
  return [immutableControl(), ...(Array.isArray(index.profiles) ? index.profiles : [])]
    .map(normalizedControl)
    .map(item => ({...item, immutable: item.id === "immutable-default", always_available: item.id === "immutable-default"}));
}

function controlReference(value) {
  const control = value && typeof value === "object" ? value : {};
  const id = clean(control.id, 120);
  if (!id || id === "immutable-default") return null;
  return {
    id, label: clean(control.label, 180), role_family: CONTROL_ROLE_FAMILIES.has(String(control.role_family || ""))
      ? String(control.role_family) : "other",
    source: clean(control.source, 40) || "run", entry_id: clean(control.entry_id, 120),
    run_id: clean(control.run_id || control.entry_id, 80),
  };
}

async function activeControlReference(token, folder, value) {
  const reference = controlReference(value);
  if (!reference) return null;
  const current = await controlIndex(token, folder);
  const profile = current.index.profiles.find(item => item.id === reference.id && item.status === "active");
  if (!profile) throw new Error("selected role-family control is no longer active");
  return controlReference(profile);
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
    run_id: clean(entry.run_id, 120), queue_id: clean(entry.queue_id, 80), status: clean(entry.status, 40), step: clean(entry.step, 160),
    message: clean(entry.message, 500), mode: clean(entry.mode, 40), created_at: clean(entry.created_at, 80),
    updated_at: clean(entry.updated_at, 80), pdf_filename: safeName(entry.pdf_filename),
    preview_filename: entry.preview_filename ? safeName(entry.preview_filename) : "",
    has_pdf: Boolean(entry.has_pdf), has_posting_snapshot: Boolean(entry.has_posting_snapshot),
    has_workshop: Boolean(entry.has_workshop), craft_score: entry.craft_score ?? null,
    approval_state: clean(entry.approval_state, 40) || "awaiting_review",
    winner_version: clean(entry.winner_version, 24),
    ready: entry.ready === true, validation_warnings: Array.isArray(entry.validation_warnings)
      ? entry.validation_warnings.map(item => clean(item, 280)).slice(0, 30) : [],
    objective: entry.objective && typeof entry.objective === "object" ? {
      version: clean(entry.objective.version, 80),
      score: Number.isFinite(Number(entry.objective.score)) ? Number(entry.objective.score) : null,
      confidence: clean(entry.objective.confidence, 20), rankable: entry.objective.rankable === true,
      breakdown: Array.isArray(entry.objective.breakdown) ? entry.objective.breakdown.slice(0, 8).map(item => ({
        name: clean(item?.name, 80), weight: Number(item?.weight || 0), score: Number(item?.score || 0),
        source: clean(item?.source, 160), detail: clean(item?.detail, 280),
      })) : [],
      strengths: Array.isArray(entry.objective.strengths) ? entry.objective.strengths.map(item => clean(item, 280)).slice(0, 5) : [],
      risks: Array.isArray(entry.objective.risks) ? entry.objective.risks.map(item => clean(item, 280)).slice(0, 6) : [],
      note: clean(entry.objective.note, 280),
    } : null,
    tailoring_audit: entry.tailoring_audit && typeof entry.tailoring_audit === "object"
      ? auditSummary(entry.tailoring_audit) : auditSummary(null),
    keyword_audit: entry.keyword_audit && typeof entry.keyword_audit === "object" ? {
      posting_available: Boolean(entry.keyword_audit.posting_available),
      detected_count: Number(entry.keyword_audit.detected_count || 0),
      supported_count: Number(entry.keyword_audit.supported_count || 0),
      covered_count: Number(entry.keyword_audit.covered_count || 0),
      supported_coverage_percent: Number.isFinite(Number(entry.keyword_audit.supported_coverage_percent)) ? Number(entry.keyword_audit.supported_coverage_percent) : null,
      overall_coverage_percent: Number.isFinite(Number(entry.keyword_audit.overall_coverage_percent)) ? Number(entry.keyword_audit.overall_coverage_percent) : null,
      required_coverage_percent: Number.isFinite(Number(entry.keyword_audit.required_coverage_percent)) ? Number(entry.keyword_audit.required_coverage_percent) : null,
      terms: Array.isArray(entry.keyword_audit.terms) ? entry.keyword_audit.terms.slice(0, 80).map(item => ({
        term: clean(item?.term, 160), importance: clean(item?.importance, 40), required: Boolean(item?.required),
        preferred: Boolean(item?.preferred), supported: Boolean(item?.supported), rendered: Boolean(item?.rendered),
        status: clean(item?.status, 40), support_kind: clean(item?.support_kind, 120),
        source_ids: Array.isArray(item?.source_ids) ? item.source_ids.map(value => clean(value, 180)).slice(0, 8) : [],
      })).filter(item => item.term) : [],
      overlay: entry.keyword_audit.overlay && typeof entry.keyword_audit.overlay === "object" ? {
        available: entry.keyword_audit.overlay.available === true,
        boxes: Array.isArray(entry.keyword_audit.overlay.boxes) ? entry.keyword_audit.overlay.boxes.slice(0, 160).map(box => ({
          left_percent: Number(box?.left_percent || 0), top_percent: Number(box?.top_percent || 0),
          width_percent: Number(box?.width_percent || 0), height_percent: Number(box?.height_percent || 0),
          terms: Array.isArray(box?.terms) ? box.terms.map(value => clean(value, 160)).slice(0, 12) : [],
          text: clean(box?.text, 500), changed_source_id: clean(box?.changed_source_id, 180),
          kind: ["ats", "changed", "both"].includes(box?.kind) ? box.kind : "ats",
        })) : [],
      } : {available:false, boxes:[]},
    } : null,
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

function queueJob(value) {
  const job = value && typeof value === "object" ? value : {};
  let url = "";
  try {
    const parsed = new URL(String(job.url || ""));
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return null;
    url = parsed.toString().slice(0, 2000);
  } catch { return null; }
  const id = clean(job.id, 140);
  const company = clean(job.company, 220);
  const title = clean(job.title, 320);
  if (!id || !company || !title || !url) return null;
  return {
    id, company, title, url,
    locations: Array.isArray(job.locations) ? job.locations.map(item => clean(item, 160)).filter(Boolean).slice(0, 12) : [],
    sector: clean(job.sector, 120),
    score: Number.isFinite(Number(job.score)) ? Number(job.score) : 0,
    alert_ok: Boolean(job.alert_ok),
    early_career_possible: Boolean(job.early_career_possible),
    explicit_new_grad: Boolean(job.explicit_new_grad),
    posted_at: job.posted_at ?? null,
  };
}

function publicQueueItem(value) {
  const item = value && typeof value === "object" ? value : {};
  return {
    queue_id: clean(item.queue_id, 80), mode: QUEUE_MODES.has(item.mode) ? item.mode : "ai",
    state: QUEUE_STATES.has(item.state) ? item.state : "queued",
    run_id: clean(item.run_id, 80), message: clean(item.message, 500), error: clean(item.error, 500),
    created_at: clean(item.created_at, 80), updated_at: clean(item.updated_at, 80),
    control_profile: controlReference(item.control_profile),
    job: queueJob(item.job),
  };
}

async function queueIndex(token, folder) {
  const files = await listFiles(token,
    `'${folder.id}' in parents and trashed = false and name = '${QUEUE_FILENAME}' and ` +
    "appProperties has { key='resumeStudioCloudQueue' and value='v1' }",
    "files(id,name,mimeType,appProperties,parents,size,modifiedTime)");
  const file = files[0] || null;
  let items = [];
  if (file?.id) {
    try {
      const parsed = JSON.parse((await readFile(token, file.id)).toString("utf8"));
      items = Array.isArray(parsed?.items) ? parsed.items.map(publicQueueItem).filter(item => item.queue_id && item.job) : [];
    } catch {}
  }
  return {file, items};
}

async function writeQueue(token, folder, current, items) {
  const metadata = {name: QUEUE_FILENAME, mimeType: "application/json",
    appProperties: {resumeStudioCloudQueue: QUEUE_VERSION}};
  if (!current.file?.id) metadata.parents = [folder.id];
  const value = {version: QUEUE_VERSION, updated_at: new Date().toISOString(),
    items: items.slice(0, MAX_QUEUE_ITEMS).map(publicQueueItem)};
  await uploadFile(token, {
    id: current.file?.id || "", metadata,
    content: Buffer.from(JSON.stringify(value)), contentType: "application/json",
  });
  return value.items;
}

async function getQueue(token) {
  const folder = await ensureFolder(token);
  const current = await queueIndex(token, folder);
  return {folder, current};
}

async function enqueueCloudRun(token, value) {
  const mode = String(value?.mode || "").trim().toLowerCase();
  if (!QUEUE_MODES.has(mode)) throw new Error("invalid Resume Studio queue mode");
  const job = queueJob(value?.job);
  if (!job) throw new Error("invalid public posting snapshot");
  const {folder, current} = await getQueue(token);
  const control_profile = await activeControlReference(token, folder, value?.control_profile);
  const duplicate = current.items.find(item => item.job?.id === job.id && item.mode === mode &&
    ["queued", "dispatching", "running", "awaiting_review"].includes(item.state));
  if (duplicate) return {item: publicQueueItem(duplicate), duplicate: true};
  const now = new Date().toISOString();
  const item = {
    queue_id: crypto.randomBytes(12).toString("hex"), mode, state: "queued", run_id: "",
    message: "Saved in the private cloud queue; waiting for the Mac worker.", error: "",
    created_at: now, updated_at: now, job, control_profile,
  };
  const items = [item, ...current.items].slice(0, MAX_QUEUE_ITEMS);
  await writeQueue(token, folder, current, items);
  return {item: publicQueueItem(item), duplicate: false};
}

async function updateCloudRun(token, value) {
  const queueId = clean(value?.queue_id, 80);
  const state = String(value?.state || "").trim().toLowerCase();
  if (!queueId || !QUEUE_STATES.has(state)) throw new Error("invalid Resume Studio queue update");
  const {folder, current} = await getQueue(token);
  const item = current.items.find(candidate => candidate.queue_id === queueId);
  if (!item) throw new Error("cloud queue item not found");
  item.state = state;
  item.run_id = clean(value?.run_id || item.run_id, 80);
  item.message = clean(value?.message || item.message, 500);
  item.error = clean(value?.error || (state === "failed" ? item.message : ""), 500);
  item.updated_at = new Date().toISOString();
  await writeQueue(token, folder, current, current.items);
  return {item: publicQueueItem(item)};
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

async function promoteControl(token, value) {
  const source = clean(value?.source, 40);
  const entry_id = clean(value?.entry_id, 120);
  const role_family = String(value?.role_family || "").trim();
  if (source !== "run" || !entry_id || !CONTROL_ROLE_FAMILIES.has(role_family) || role_family === "other") {
    throw new Error("choose a valid role-family control from an owner-approved run");
  }
  const {folder, current: bank} = await getBank(token);
  const entry = bank.index.entries.find(item => entryId(item) === `${source}:${entry_id}`);
  if (!entry) throw new Error("sync this Resume Bank entry to the cloud before promoting it");
  if (entry.status !== "complete" || entry.approval_state !== "approved" || entry.winner_version !== "tailored" || !entry.has_pdf) {
    throw new Error("only an owner-approved tailored winner can become a permanent role-family control");
  }
  const controls = await controlIndex(token, folder);
  const existing = controls.index.profiles.find(item => item.source === source && item.entry_id === entry_id && item.role_family === role_family && item.status === "active");
  if (existing) return normalizedControl(existing);
  const profileId = `control-${crypto.createHash("sha256").update(`${source}:${entry_id}:${role_family}`).digest("hex").slice(0, 20)}`;
  const profile = normalizedControl({
    id: profileId,
    label: clean(value?.label, 180) || `${role_family} control`,
    role_family, source, entry_id, run_id: entry.run_id || entry_id,
    artifact: `${entry_id}/${entry.pdf_filename}`,
    status: "active", approved_at: new Date().toISOString(), approved_by: OWNER,
  });
  const replacedAt = new Date().toISOString();
  const next = [profile, ...controls.index.profiles
    .filter(item => item.id !== profile.id)
    .map(item => item.role_family === role_family && item.status === "active"
      ? {...item, status: "revoked", revoked_at: replacedAt}
      : item)];
  await writeControlIndex(token, folder, controls, next);
  return profile;
}

async function revokeControl(token, value) {
  const id = clean(value?.id, 120);
  if (!id || id === "immutable-default") throw new Error("the immutable default cannot be revoked");
  const {folder} = await getBank(token);
  const controls = await controlIndex(token, folder);
  const profile = controls.index.profiles.find(item => item.id === id);
  if (!profile) throw new Error("role-family control not found");
  profile.status = "revoked";
  profile.revoked_at = new Date().toISOString();
  await writeControlIndex(token, folder, controls, controls.index.profiles);
  return normalizedControl(profile);
}

module.exports = async (req, res) => {
  if (String(req.query?.application_agent || "") === "1") return applicationAgent(req, res);
  try {
    const access = await contextFor(req);
    if (req.method === "GET") {
      if (String(req.query?.queue || "") === "1") {
        const {current} = await getQueue(access.token);
        res.status(200).json({configured: true, connected: true, source: access.source,
          queue_version: QUEUE_VERSION, items: current.items.map(publicQueueItem)}); return;
      }
      const {folder, current} = await getBank(access.token);
      const controls = await controlIndex(access.token, folder);
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
        updated_at: current.index.updated_at, resumes: publicEntries(current.index),
        controls: publicControls(controls), controls_updated_at: controls.index.updated_at}); return;
    }
    if (req.method === "POST") {
      if (!requireMutationRequest(req, res)) return;
      const payload = bodyOf(req);
      if (payload.action === "queue") {
        res.status(200).json({ok: true, ...(await enqueueCloudRun(access.token, payload))}); return;
      }
      if (payload.action === "queue_update") {
        res.status(200).json({ok: true, ...(await updateCloudRun(access.token, payload))}); return;
      }
      if (payload.action === "control_promote") {
        res.status(200).json({ok: true, control: await promoteControl(access.token, payload)}); return;
      }
      if (payload.action === "control_revoke") {
        res.status(200).json({ok: true, control: await revokeControl(access.token, payload)}); return;
      }
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
