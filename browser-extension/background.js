import {resumeFileAccepted, resumeFieldsNeedingUpload} from "./application-fields.mjs";
const LOCAL_ORIGIN = "http://127.0.0.1:4317";
const DEFAULT_CLOUD_URL = "https://job-radar-newgrad.vercel.app";
const BATCH_RUNNING_STATES = new Set(["opening", "filling", "submitting"]);
const RESUME_TERMINAL_STATES = new Set(["complete", "awaiting_review", "failed"]);
const TERMINAL_QUEUE_STATES = new Set(["submitted", "failed", "skipped", "cancelled"]);
const RADAR_PAGE_ORIGINS = new Set(["https://job-radar-newgrad.vercel.app", "https://victorjimenez3.github.io"]);
const tabs = new Map();
let syncPromise = null;
let syncBlockedUntil = 0;
let reloadScheduled = false;

function extensionPopupSender(sender) {
  const popupURL = chrome.runtime.getURL("popup.html");
  return String(sender?.url || "").startsWith(popupURL);
}

function radarPageSender(sender) {
  if (!sender?.tab?.id || !sender?.url) return false;
  try { return RADAR_PAGE_ORIGINS.has(new URL(sender.url).origin); }
  catch (_) { return false; }
}

function extensionControlSender(sender, message = {}) {
  return extensionPopupSender(sender) || message.source === "radar-page" && radarPageSender(sender);
}

function requestExtensionReload(sender, message = {}) {
  if (!extensionControlSender(sender, message)) {
    return {ok: false, error: "Only the Job Radar popup or owner Job Radar page can request a reload."};
  }
  if (typeof chrome.runtime.reload !== "function") {
    return {ok: false, error: "This Chrome version cannot reload the extension from the extension UI."};
  }
  if (reloadScheduled) return {ok: true, reloading: true, extensionVersion: chrome.runtime.getManifest().version};
  reloadScheduled = true;
  // Let the popup receive the acknowledgement before the worker terminates.
  setTimeout(() => {
    try { chrome.runtime.reload(); }
    catch (error) {
      reloadScheduled = false;
      console.warn("Job Radar extension reload failed", error);
    }
  }, 75);
  return {ok: true, reloading: true, extensionVersion: chrome.runtime.getManifest().version};
}

async function requestQueueSync(sender, message = {}) {
  if (!extensionControlSender(sender, message)) {
    return {ok: false, error: "Only the Job Radar popup or owner Job Radar page can sync the queue."};
  }
  const result = await syncCloudQueue();
  return {...result, extensionVersion: chrome.runtime.getManifest().version};
}

function noteQuota(error) {
  const message = String(error?.message || error || "");
  if (/quota exceeded|rate limit|too many requests|\b429\b/i.test(message)) {
    syncBlockedUntil = Math.max(syncBlockedUntil, Date.now() + 30_000);
    return "Google Sheets is rate-limited; sync is paused briefly and will retry automatically.";
  }
  return message || "Queue sync failed";
}

function safeURL(value) {
  try {
    const url = new URL(String(value || ""));
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return "";
    return url.href;
  } catch (_) { return ""; }
}

function applicationIdentity(value) {
  const job = value?.job && typeof value.job === "object" ? value.job : value;
  const href = safeURL(job?.url || value?.url || value);
  if (href) {
    const url = new URL(href);
    url.hash = "";
    for (const key of [...url.searchParams.keys()]) {
      if (/^(?:utm_.+|embed|source|ref|referrer)$/i.test(key)) url.searchParams.delete(key);
    }
    const path = url.pathname.replace(/\/$/, "") || "/";
    return `url:${url.origin.toLowerCase()}${path}${url.search}`;
  }
  const id = String(job?.id || "").trim();
  return id ? `id:${id}` : "";
}

function trackerChoiceTime(entry) {
  for (const key of ["choice_at", "tailored_at", "stage_changed_at"]) {
    const raw = Number(entry?.[key] || 0);
    if (raw > 0) return raw > 1e12 ? raw : raw * 1000;
  }
  return 0;
}

function queueItemCoversTrackerChoice(entry, items) {
  const identity = applicationIdentity(entry);
  const matches = (Array.isArray(items) ? items : []).filter(item =>
    item?.job?.id === entry?.id || identity && applicationIdentity(item) === identity);
  if (!matches.length) return false;
  if (matches.some(item => !TERMINAL_QUEUE_STATES.has(item.state))) return true;
  const decisionAt = trackerChoiceTime(entry);
  if (!decisionAt) return true;
  return matches.some(item => (Date.parse(item.created_at || "") || 0) >= decisionAt);
}

function trackerChoiceJob(entry) {
  const url = safeURL(entry?.url);
  if (!entry?.id || !entry?.company || !entry?.title || !url) return null;
  return {
    id: String(entry.id), company: String(entry.company), title: String(entry.title), url,
    locations: Array.isArray(entry.locations) ? entry.locations.slice(0, 12).map(String) : [],
    score: Number(entry.score) || 0, posted_at: entry.posted_at ?? null,
    posting_status: String(entry.posting_status || ""), description: String(entry.description || "").slice(0, 20_000),
  };
}

async function importTrackerChoices(items = []) {
  const settings = await config();
  const configured = await fetch(`${settings.cloudUrl}/api/config`, {cache: "no-store"});
  if (!configured.ok) throw new Error(`Tracker configuration returned ${configured.status}`);
  const metadata = await configured.json();
  const repo = String(metadata.repo || "VictorJimenez3/fable-job-search");
  const branch = String(metadata.branch || "claude/newgrad-job-search-system-9gbj9k");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) || !/^[A-Za-z0-9_./-]+$/.test(branch)) {
    throw new Error("Tracker configuration is invalid");
  }
  const response = await fetch(`https://raw.githubusercontent.com/${repo}/${branch}/state/autopilot_choices.json`, {cache: "no-store"});
  if (!response.ok) throw new Error(`Tracker choices returned ${response.status}`);
  const applied = await response.json();
  if (!Array.isArray(applied)) throw new Error("Tracker choices are not a list");
  const jobs = applied
    .filter(entry => ["saved", "to_tailor"].includes(String(entry?.stage || entry?.status || "").toLowerCase().replace(/[\s-]+/g, "_")))
    .filter(entry => !queueItemCoversTrackerChoice(entry, items))
    .map(trackerChoiceJob).filter(Boolean).slice(0, 80);
  if (!jobs.length) return {items, queued: 0, duplicates: 0};
  const result = await cloud("/api/application-agent", {
    method: "POST", body: JSON.stringify({action: "queue_many", jobs}),
  });
  const merged = new Map((Array.isArray(items) ? items : []).map(item => [item.queue_id, item]));
  for (const row of result.items || []) if (row?.item?.queue_id) merged.set(row.item.queue_id, row.item);
  return {items: [...merged.values()], queued: result.queued || 0, duplicates: result.duplicates || 0};
}

async function config() {
  const stored = await chrome.storage.local.get({cloudUrl: DEFAULT_CLOUD_URL, agentToken: "", autoContinue: true, maxConcurrentApplications: 3});
  return {...stored, cloudUrl: String(stored.cloudUrl || DEFAULT_CLOUD_URL).replace(/\/$/, "")};
}

async function local(path, init = {}) {
  const response = await fetch(`${LOCAL_ORIGIN}${path}`, {
    ...init,
    headers: {"Content-Type": "application/json", ...(init.headers || {})},
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Local agent returned ${response.status}`);
  return body;
}

async function localFile(path, init = {}) {
  const response = await fetch(`${LOCAL_ORIGIN}${path}`, {
    ...init,
    headers: {"Content-Type": "application/json", ...(init.headers || {})},
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Local agent returned ${response.status}`);
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

async function cloud(path, init = {}) {
  const settings = await config();
  if (!settings.agentToken) throw new Error("Pair this Mac with Job Radar before using the cloud queue");
  const response = await fetch(`${settings.cloudUrl}${path}`, {
    ...init,
    headers: {"Content-Type": "application/json", "X-Job-Radar-Agent": settings.agentToken, ...(init.headers || {})},
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Cloud agent returned ${response.status}`);
  return body;
}

async function createSession(tabId, job, mode = "per_role", queueId = "") {
  const session = await local("/api/application/session", {
    method: "POST", body: JSON.stringify({job, mode, queue_id: queueId}),
  });
  tabs.set(tabId, {...tabs.get(tabId), job, mode, queueId, sessionId: session.session_id,
    state: session.state, essayAttempts: new Set()});
  return session;
}

function wait(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function ensureResumeForApplication(tabId, row) {
  if (!row?.job) throw new Error("A job is required before Resume Studio can tailor it");
  if (row.resumeInfo) return row.resumeInfo;
  if (row.resumePromise) return row.resumePromise;
  row.resumePromise = (async () => {
    const queueId = row.queueId || "";
    const resumeRequest = {method: "POST", body: JSON.stringify({
      job_id: row.job.id, job: row.job, queue_id: queueId,
    })};
    let status = await local("/api/application/resume", resumeRequest);
    if (status.status === "missing") {
      if (queueId) await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
        action: "queue_update", queue_id: queueId, state: "opening",
        message: "Resume Studio is tailoring this role before form filling.",
      })});
      status = await local("/api/run", {method: "POST", body: JSON.stringify({
        job_id: row.job.id, mode: "ai", queue_id: `application-${queueId}`, job_snapshot: row.job,
      })});
    }
    if (status.status === "running" || status.status === "queued") {
      const runId = status.run_id;
      if (!runId) throw new Error("Resume Studio did not return a run id");
      const deadline = Date.now() + 20 * 60 * 1000;
      while (Date.now() < deadline) {
        await wait(1800);
        status = await local(`/api/run?id=${encodeURIComponent(runId)}`);
        if (RESUME_TERMINAL_STATES.has(status.status)) break;
      }
      if (!RESUME_TERMINAL_STATES.has(status.status)) throw new Error("Resume Studio is still working; retry this role after it finishes");
      if (status.status === "failed") throw new Error(status.message || "Resume Studio could not produce a draft");
      status = await local("/api/application/resume", {method: "POST", body: JSON.stringify({
        job_id: row.job.id, job: row.job, queue_id: queueId, allow_fallback: true,
      })});
    }
    if (!["ready", "fallback"].includes(status.status)) throw new Error(status.message || "Resume Studio did not produce a usable resume result");
    if (!status.file_ready || !status.pdf_filename) throw new Error(status.message || "Resume Studio selected a resume but its PDF is unavailable");
    row.resumeFile = {
      name: status.pdf_filename,
      type: "application/pdf",
      base64: await localFile("/api/application/resume-file", {method: "POST", body: JSON.stringify({
        job_id: row.job.id, job: row.job, queue_id: queueId,
      })}),
    };
    row.resumeInfo = status;
    if (queueId) await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
      action: "queue_update", queue_id: queueId, state: "opening",
      resume: status,
      message: status.message || "Resume Studio finished the application resume check.",
    })});
    await send(tabId, {type: "JOB_RADAR_RESUME_STATUS", resume: status});
    return status;
  })();
  try { return await row.resumePromise; }
  finally { row.resumePromise = null; }
}

async function ensureApplicationSession(tabId, row) {
  if (row?.sessionId && row?.resumeFile) return row;
  if (row?.sessionPromise) return row.sessionPromise;
  row.sessionPromise = (async () => {
    await send(tabId, {type: "JOB_RADAR_AGENT_STATUS", title: "Agent preparing this role", message: "Resume Studio is checking for a tailored resume before the form is filled. Submit remains untouched."});
    await ensureResumeForApplication(tabId, row);
    const current = tabs.get(tabId);
    if (!current) throw new Error("The paired application tab detached while Resume Studio was working");
    if (!current.sessionId) await createSession(tabId, current.job, current.mode, current.queueId);
    return tabs.get(tabId);
  })();
  try { return await row.sessionPromise; }
  finally {
    const current = tabs.get(tabId);
    if (current) current.sessionPromise = null;
  }
}

async function send(tabId, message) {
  try { return await chrome.tabs.sendMessage(tabId, message); } catch (_) { return null; }
}

async function navigateQueuedTab(tabId, url, active = true) {
  const target = safeURL(url);
  if (!target) throw new Error("A safe application URL is required");
  await chrome.tabs.update(tabId, {url: target, active});
  const deadline = Date.now() + 8_000;
  while (Date.now() < deadline) {
    const current = await chrome.tabs.get(tabId);
    if (safeURL(current?.url)) return current;
    await wait(250);
  }
  throw new Error("Chrome did not navigate the paired tab to the queued application URL");
}

async function startJob(job, mode = "per_role", queueId = "") {
  const url = safeURL(job?.url);
  if (!url || !job?.id || !job?.company || !job?.title) throw new Error("A complete safe job snapshot is required");
  const active = mode !== "batch";
  const existing = (await chrome.tabs.query({}))
    .filter(candidate => !tabs.has(candidate.id) && applicationIdentity(candidate) === applicationIdentity(job))
    .sort((left, right) => Number(right.lastAccessed || 0) - Number(left.lastAccessed || 0))[0];
  if (existing) {
    tabs.set(existing.id, {job, mode, queueId, sessionId: "", createdAt: Date.now(), state: "opening"});
    if (active) await chrome.tabs.update(existing.id, {active: true});
    await send(existing.id, {type: "JOB_RADAR_RESCAN"});
    return {tab_id: existing.id, url: existing.url || url, reused: true};
  }
  const tab = await chrome.tabs.create({active});
  try {
    const opened = await navigateQueuedTab(tab.id, url, active);
    tabs.set(tab.id, {job, mode, queueId, sessionId: "", createdAt: Date.now()});
    return {tab_id: tab.id, url: opened.url || url};
  } catch (error) {
    try { await chrome.tabs.remove(tab.id); } catch (_) {}
    throw error;
  }
}

async function repairTrackedTabs(items) {
  for (const [tabId, row] of tabs) {
    const target = safeURL(row.job?.url);
    if (!row.queueId || !target) continue;
    let current;
    try { current = await chrome.tabs.get(tabId); } catch (_) { tabs.delete(tabId); continue; }
    if (safeURL(current?.url)) continue;
    try {
      await navigateQueuedTab(tabId, target);
    } catch (error) {
      tabs.delete(tabId);
      const item = items.find(value => value.queue_id === row.queueId);
      const state = row.sessionId ? "failed" : "queued";
      if (item) {
        item.state = state;
        item.message = row.sessionId
          ? "The paired application tab disappeared before the agent could continue."
          : "Requeued because Chrome did not open the paired application tab.";
      }
      try { await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
        action: "queue_update", queue_id: row.queueId,
        state,
        message: item?.message || "The paired application tab could not be repaired.",
        error: error.message,
      })}); } catch (_) {}
    }
  }
}

async function recoverOrphanedQueue(items) {
  const openTabs = await chrome.tabs.query({});
  const claimedTabIds = new Set(tabs.keys());
  for (const item of items) {
    const activelyRunning = ["opening", "filling"].includes(item.state);
    const parkedButAttachable = ["blocked", "awaiting_confirmation"].includes(item.state);
    const recoveredQueueWithSession = item.state === "queued" && Boolean(item.session_id);
    if (!activelyRunning && !parkedButAttachable && !recoveredQueueWithSession) continue;
    const tracked = [...tabs.entries()].find(([, row]) => row.queueId === item.queue_id);
    if (tracked) {
      const [tabId, row] = tracked;
      let current;
      try { current = await chrome.tabs.get(tabId); } catch (_) { current = null; }
      const livePage = Boolean(safeURL(current?.url));
      const withinAttachGrace = livePage && !row.sessionId && Date.now() - Number(row.createdAt || 0) < 20_000;
      if (livePage && (row.sessionId || withinAttachGrace)) continue;
      tabs.delete(tabId);
    }
    const target = safeURL(item.job?.url);
    const existing = openTabs
      .filter(tab => !claimedTabIds.has(tab.id) && target && applicationIdentity(tab) === applicationIdentity(item.job))
      .sort((left, right) => Number(right.lastAccessed || 0) - Number(left.lastAccessed || 0))[0];
    if (existing) {
      let sessionId = "";
      if (item.session_id) {
        try {
          const session = await local(`/api/application/session?session_id=${encodeURIComponent(item.session_id)}`);
          if (session?.session_id === item.session_id) sessionId = item.session_id;
        } catch (_) {}
      }
      tabs.set(existing.id, {job: item.job, mode: "batch", queueId: item.queue_id, sessionId, createdAt: Date.now()});
      claimedTabIds.add(existing.id);
      await send(existing.id, {type: "JOB_RADAR_RESCAN"});
      continue;
    }
    // A blocked/review-ready role is intentionally parked. Reattach it only
    // to its exact existing page; never open a surprise duplicate tab or let
    // it hold up later queued roles.
    if (parkedButAttachable) continue;
    try {
      await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
        action: "queue_update", queue_id: item.queue_id, state: "queued",
        message: "Requeued after the paired agent restarted before an application tab attached.",
      })});
      item.state = "queued";
      item.message = "Requeued after the paired agent restarted before an application tab attached.";
    } catch (error) { console.warn("Job Radar orphan recovery", error); }
  }
}

async function reconcileTerminalTabs(items) {
  const states = new Map(items.map(item => [item.queue_id, item.state]));
  for (const [tabId, row] of tabs) {
    if (!row.queueId || !TERMINAL_QUEUE_STATES.has(states.get(row.queueId))) continue;
    await send(tabId, {type: "JOB_RADAR_AGENT_STOP", message: "This queue item is no longer active. The page will not be filled or advanced."});
    tabs.delete(tabId);
  }
}

async function syncSession(tabId) {
  const row = tabs.get(tabId);
  if (!row?.sessionId || !row.queueId) return;
  try {
    const session = await local(`/api/application/session?session_id=${encodeURIComponent(row.sessionId)}`);
    row.state = session.state;
    await cloud("/api/application-agent", {
      method: "POST",
      body: JSON.stringify({action: "queue_update", queue_id: row.queueId, session_id: row.sessionId,
        state: session.state, blockers: session.blockers || [], review: session.review || null,
        message: session.last_message || session.last_error || session.state, error: session.last_error || ""}),
    });
    const item = (await cloud("/api/application-agent?view=queue")).items?.find(value => value.queue_id === row.queueId);
    if (item?.confirmation && session.state === "awaiting_confirmation") {
      await local("/api/application/confirm", {method: "POST", body: JSON.stringify({
        session_id: row.sessionId, review_hash: item.confirmation.review_hash,
        nonce: item.review?.nonce || "", page_fingerprint: item.confirmation.page_fingerprint,
        owner_approved_at: item.confirmation.approved_at || "",
        approval_expires_at: item.confirmation.expires_at || "",
      })});
      row.state = "submitting";
      await send(tabId, {type: "JOB_RADAR_SUBMISSION_APPROVED"});
    }
  } catch (error) {
    console.warn("Job Radar cloud sync", error);
  }
}

const RECOVERABLE_QUEUE_STATES = new Set(["queued", "opening", "filling", "blocked", "awaiting_confirmation", "submitting"]);
const AUTO_RECOVERABLE_BLOCKERS = new Set(["resume_file", "essay", "cover_letter"]);

function recoveredSessionState(session) {
  const blockers = Array.isArray(session?.blockers) ? session.blockers : [];
  // Display-card expiry is not a reason to make the owner reopen the ATS.
  // A fresh Confirm action is valid only for the exact stored hash, nonce,
  // and fingerprint, and the live page is still reverified before Submit.
  if (session?.state === "awaiting_confirmation") return session.state;
  if (session?.state !== "blocked" || !blockers.length) return session?.state;
  return blockers.every(blocker => AUTO_RECOVERABLE_BLOCKERS.has(String(blocker?.category || "")))
    ? "queued"
    : session.state;
}

function localQueueRecoveryItems(value, cloudItems = []) {
  const sessions = Array.isArray(value?.sessions) ? value.sessions : [];
  const cloudByQueue = new Map((Array.isArray(cloudItems) ? cloudItems : []).map(item => [item?.queue_id, item]));
  const latest = new Map();
  for (const session of sessions) {
    if (!session || !RECOVERABLE_QUEUE_STATES.has(session.state)) continue;
    const queueId = String(session.queue_id || "").trim();
    const job = session.job;
    if (!queueId || !job?.id || !job?.company || !job?.title || !job?.url) continue;
    const current = latest.get(queueId);
    const stamp = Date.parse(String(session.updated_at || "")) || 0;
    const currentStamp = Date.parse(String(current?.updated_at || "")) || 0;
    if (!current || stamp >= currentStamp) latest.set(queueId, session);
  }
  const statePriority = {submitting: 6, awaiting_confirmation: 5, filling: 4, opening: 3, blocked: 2, queued: 1};
  const uniqueSessions = [...latest.values()].sort((left, right) => {
    const stateDelta = (statePriority[right?.state] || 0) - (statePriority[left?.state] || 0);
    if (stateDelta) return stateDelta;
    return (Date.parse(String(right?.updated_at || "")) || 0) - (Date.parse(String(left?.updated_at || "")) || 0);
  }).filter((session, index, all) => {
    const identity = applicationIdentity(session.job);
    return identity && all.findIndex(candidate => applicationIdentity(candidate.job) === identity) === index;
  });
  return uniqueSessions.map(session => {
    const cloudItem = cloudByQueue.get(session.queue_id);
    const confirmedSameReview = Boolean(
      cloudItem?.confirmation
      && session?.review
      && cloudItem.confirmation.review_hash === session.review.review_hash
      && cloudItem.confirmation.page_fingerprint === session.review.page_fingerprint
    );
    const state = confirmedSameReview ? session.state : recoveredSessionState(session);
    const recoveryReset = state === "queued" && session.state !== "queued";
    return ({
    queue_id: session.queue_id,
    session_id: session.session_id || "",
    state,
    created_at: session.created_at || "",
    updated_at: session.updated_at || "",
    recovery_reset: recoveryReset,
    message: recoveryReset
      ? session.state === "awaiting_confirmation"
        ? "Requeued because the saved page-bound confirmation expired. A fresh review will be created from the live page."
        : "Requeued because Resume Studio or the writing agent can now resolve the saved blockers."
      : session.last_message || session.last_error || `Recovered local ${session.state} session`,
    error: session.last_error || "",
    job: session.job,
    blockers: recoveryReset ? [] : Array.isArray(session.blockers) ? session.blockers : [],
    resume: session.resume || null,
    review: recoveryReset ? null : session.review || null,
    confirmation: recoveryReset ? null : session.confirmation || null,
    });
  });
}

async function recoverLocalQueue(cloudItems = []) {
  let localSessions;
  try { localSessions = await local("/api/application/sessions"); }
  catch (error) { return {items: [], error: noteQuota(error)}; }
  const items = localQueueRecoveryItems(localSessions, cloudItems);
  if (!items.length) return {items: [], error: ""};
  try {
    const result = await cloud("/api/application-agent", {
      method: "POST", body: JSON.stringify({action: "recover", items}),
    });
    for (const item of items.filter(value => value.recovery_reset && value.session_id)) {
      try {
        await local("/api/application/event", {method: "POST", body: JSON.stringify({
          session_id: item.session_id, state: "filling",
          message: "Recovered from a stale blocker or expired page-bound review; rebuilding from the live page.",
        })});
      } catch (error) { console.warn("Job Radar local recovery reset", error); }
    }
    return {items: Array.isArray(result.items) ? result.items : [], error: "", recovered: result.recovered || []};
  } catch (error) {
    return {items: [], error: noteQuota(error)};
  }
}

function reusableAnswerSignature(answer = {}) {
  const list = value => (Array.isArray(value) ? value : [])
    .map(item => String(item || "").trim()).filter(Boolean).sort();
  return JSON.stringify({
    question: String(answer.question || "").trim(),
    value: String(answer.value || "").trim(),
    category: String(answer.category || "").trim(),
    variants: list(answer.variants),
    fallback_for: list(answer.fallback_for),
    reusable: answer.reusable !== false,
    sensitive: Boolean(answer.sensitive),
    select_all: Boolean(answer.select_all),
  });
}

function localAnswerShouldSync(answer, cloudAnswer) {
  if (!answer?.answer_id) return false;
  // Role-specific generated writing stays with its local application session.
  // Publishing it as a global cloud-bank answer can put one company's essay
  // into another company's form when the prompt wording happens to match.
  if (answer.reusable === false) return false;
  if (!cloudAnswer) return true;
  if ((answer.evidence_ids || []).includes("canonical-resume") && cloudAnswer.value !== answer.value) return true;
  const localAt = Date.parse(String(answer.updated_at || "")) || 0;
  const cloudAt = Date.parse(String(cloudAnswer.updated_at || "")) || 0;
  return localAt > cloudAt && reusableAnswerSignature(answer) !== reusableAnswerSignature(cloudAnswer);
}

async function syncCloudQueueImpl() {
  let data;
  try { data = await cloud("/api/application-agent?view=queue"); }
  catch (error) {
    console.warn("Job Radar queue", error);
    return {ok: false, error: noteQuota(error)};
  }
  let importedChoices = 0;
  try {
    try {
      await local("/api/application/tracker-sync", {method: "POST", body: "{}"});
    } catch (error) {
      console.warn("Job Radar tracker sync nudge", error);
    }
    const imported = await importTrackerChoices(data.items || []);
    data = {...data, items: imported.items};
    importedChoices = imported.queued || 0;
  } catch (error) {
    console.warn("Job Radar tracker choice import", error);
  }
  let recoveryError = "";
  let recoveredCount = 0;
  try {
    const recovery = await recoverLocalQueue(data.items || []);
    recoveryError = recovery.error || "";
    recoveredCount = Array.isArray(recovery.recovered) ? recovery.recovered.length : 0;
    if (recovery.items.length) data = {...data, items: recovery.items};
  } catch (error) {
    recoveryError = noteQuota(error);
  }
  let syncError = "";
  try {
    const context = await cloud("/api/application-agent?view=context");
    // The local bank can contain deterministic owner-authored profile fields
    // derived from the canonical resume. Seed those into the private cloud
    // bank once so the production page and every paired Mac share the same
    // safe baseline; essays and attestations still require the owner.
    try {
      const localContext = await local("/api/application/context");
      const cloudAnswers = new Map((context.context?.answers || []).filter(answer => answer?.answer_id).map(answer => [answer.answer_id, answer]));
      const localChanges = (localContext.answers || []).filter(answer => {
        return localAnswerShouldSync(answer, cloudAnswers.get(answer?.answer_id));
      });
      if (localChanges.length) {
        const synced = await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({action: "answers", answers: localChanges})});
        if (synced.context) context.context = synced.context;
      }
    } catch (error) {
      syncError = noteQuota(error);
      console.warn("Job Radar profile context sync", error);
    }
    const stored = await chrome.storage.local.get({contextUpdatedAt: ""});
    if (context.context?.updated_at && context.context.updated_at !== stored.contextUpdatedAt) {
      const localBeforeImport = await local("/api/application/context");
      const localById = new Map((localBeforeImport.answers || []).filter(answer => answer?.answer_id).map(answer => [answer.answer_id, answer]));
      for (const answer of context.context.answers || []) {
        // Generated application writing is intentionally session-local. A
        // historical cloud copy must never overwrite a fresh answer for the
        // current company, even when the normalized prompt is identical.
        if (answer.reusable === false) continue;
        const localAnswer = localById.get(answer.answer_id);
        const localAt = Date.parse(String(localAnswer?.updated_at || "")) || 0;
        const cloudAt = Date.parse(String(answer.updated_at || "")) || 0;
        if (localAnswer && localAt > cloudAt) continue;
        await local("/api/application/answer", {method: "POST", body: JSON.stringify(answer)});
      }
      await chrome.storage.local.set({contextUpdatedAt: context.context.updated_at});
    }
  } catch (error) {
    syncError = noteQuota(error);
    console.warn("Job Radar context sync", error);
  }
  if (recoveryError && !syncError) syncError = recoveryError;
  const items = Array.isArray(data.items) ? data.items : [];
  await reconcileTerminalTabs(items);
  await repairTrackedTabs(items);
  await recoverOrphanedQueue(items);
  for (const item of items) {
    const tracked = [...tabs.entries()].find(([, row]) => row.queueId === item.queue_id);
    if (!tracked || !item.retry_requested_at) continue;
    const [tabId, row] = tracked;
    if (row.lastRetryRequestedAt === item.retry_requested_at) continue;
    row.lastRetryRequestedAt = item.retry_requested_at;
    row.essayAttempts = new Set();
    await send(tabId, {type: "JOB_RADAR_RESCAN"});
  }
  const settings = await config();
  const maxConcurrent = Math.max(1, Math.min(5, Number(settings.maxConcurrentApplications) || 3));
  const runningQueueIds = new Set([
    ...items.filter(item => BATCH_RUNNING_STATES.has(item.state)).map(item => item.queue_id),
    ...[...tabs.values()].filter(row => row.queueId && BATCH_RUNNING_STATES.has(row.state || "opening")).map(row => row.queueId),
  ]);
  let slots = Math.max(0, maxConcurrent - runningQueueIds.size);
  for (const item of items) {
    if (slots <= 0) break;
    if (item.state === "queued" && ![...tabs.values()].some(row => row.queueId === item.queue_id)) {
      try {
        const started = await startJob(item.job, "batch", item.queue_id);
        slots -= 1;
        await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
          action: "queue_update", queue_id: item.queue_id, state: "opening", message: "Opening on the paired Mac",
        })});
        console.info("Job Radar opened queued application", started.tab_id);
      } catch (error) { console.warn("Job Radar could not open queue item", error); }
    }
  }
  for (const tabId of tabs.keys()) await syncSession(tabId);
  return {ok: !syncError, error: syncError, recovered: recoveredCount,
    imported: importedChoices,
    queued: items.filter(item => item.state === "queued").length,
    active: items.filter(item => BATCH_RUNNING_STATES.has(item.state)).length};
}

async function syncCloudQueue() {
  if (syncPromise) return syncPromise;
  if (Date.now() < syncBlockedUntil) {
    return {ok: false, error: "Google Sheets is rate-limited; sync is paused briefly and will retry automatically."};
  }
  syncPromise = syncCloudQueueImpl();
  try { return await syncPromise; }
  finally { syncPromise = null; }
}

function scheduleQueueSync() {
  chrome.alarms.create("job-radar-application-sync", {periodInMinutes: 1});
  void syncCloudQueue();
}
chrome.runtime.onInstalled.addListener(scheduleQueueSync);
chrome.runtime.onStartup.addListener(scheduleQueueSync);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "job-radar-application-sync") void syncCloudQueue();
});
chrome.tabs.onRemoved.addListener((tabId) => tabs.delete(tabId));
scheduleQueueSync();

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  const tabId = sender.tab?.id;
  (async () => {
    if (message?.type === "JOB_RADAR_START") return startJob(message.job, message.mode, message.queueId);
    if (message?.type === "JOB_RADAR_CONTENT_READY" && tabId) {
      let row = tabs.get(tabId);
      if (row && message.pageBoundary) {
        const reason = String(message.pageBoundary).slice(0, 800);
        const directUrl = safeURL(message.directApplicationUrl);
        if (directUrl) {
          row.job = {...row.job, url: directUrl};
          row.state = "opening";
          if (row.queueId) await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
            action: "queue_update", queue_id: row.queueId, state: "opening",
            message: "Following the aggregator's direct employer Apply link automatically.", error: "",
          })});
          await navigateQueuedTab(tabId, directUrl, Boolean(sender.tab?.active));
          return {session_id: "", configured: true, boundary: true, navigating: true};
        }
        row.state = "blocked";
        if (row.queueId) await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
          action: "queue_update", queue_id: row.queueId, state: "blocked", message: reason, error: "",
        })});
        return {session_id: "", configured: true, boundary: true};
      }
      if (row && message.pageFailure) {
        const reason = String(message.pageFailure).slice(0, 800);
        if (row.queueId) await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
          action: "queue_update", queue_id: row.queueId, state: "failed", message: reason, error: reason,
        })});
        tabs.delete(tabId);
        return {session_id: "", configured: true, blocked: true};
      }
      if (row && (!row.sessionId || !row.resumeFile)) {
        try {
          row = await ensureApplicationSession(tabId, row);
        } catch (error) {
          if (row.queueId) {
            try { await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
              action: "queue_update", queue_id: row.queueId, state: "blocked",
              message: "Application paused before filling because Resume Studio needs attention.", error: error.message,
            })}); } catch (_) {}
          }
          return {session_id: "", configured: Boolean(row), error: error.message};
        }
      }
      row = tabs.get(tabId);
      return {session_id: row?.sessionId || "", configured: Boolean(row)};
    }
    if (message?.type === "JOB_RADAR_FORM" && tabId) {
      const row = tabs.get(tabId);
      if (!row?.sessionId) return {error: "Start this application from Job Radar first."};
      // Keep the employer page's real file values. Pretending every file input
      // already contains the resume makes the later upload selector return an
      // empty list and can also mistake a cover-letter control for Resume.
      const fields = (message.fields || []).map(field => ({...field}));
      let plan = await local("/api/application/form", {method: "POST", body: JSON.stringify({
        session_id: row.sessionId, page_url: message.pageUrl, fields, final: Boolean(message.final),
      })});
      const writtenBlockers = (plan.blockers || []).filter(blocker => ["essay", "cover_letter"].includes(blocker.category));
      row.essayAttempts = row.essayAttempts || new Set();
      let generatedAnswer = false;
      for (const blocker of writtenBlockers) {
        const attemptKey = `${row.sessionId}:${blocker.category}:${blocker.label}`;
        if (row.essayAttempts.has(attemptKey)) continue;
        row.essayAttempts.add(attemptKey);
        const sourceField = fields.find(field => field.field_id === blocker.field_id) || {};
        const limitText = `${sourceField.label || ""} ${sourceField.placeholder || ""}`;
        const limitMatch = limitText.match(/([\d,]+)\s*(?:character|char)s?/i);
        const characterLimit = Number(String(limitMatch?.[1] || "").replace(/,/g, "")) || Number(sourceField.maxlength) || 0;
        const wordLimitMatch = limitText.match(/([\d,]+)\s*words?/i);
        const wordLimit = Number(String(wordLimitMatch?.[1] || "").replace(/,/g, "")) || 0;
        try {
          if (row.queueId) await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
            action: "queue_update", queue_id: row.queueId, state: "filling",
            message: "Drafting a role-specific written response with warm-scholarship-essay.",
          })});
          const generated = await local("/api/application/essay", {method: "POST", body: JSON.stringify({
            session_id: row.sessionId, job: row.job, question: blocker.label,
            category: blocker.category, character_limit: characterLimit, word_limit: wordLimit,
          })});
          if (generated?.answer) {
            generatedAnswer = true;
            if (generated.answer.reusable !== false) {
              try { await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
                action: "answer", ...generated.answer,
              })}); } catch (_) {}
            }
          }
        } catch (error) {
          blocker.reason = `The warm writing skill needs your input: ${error.message}`;
        }
      }
      if (generatedAnswer) {
        plan = await local("/api/application/form", {method: "POST", body: JSON.stringify({
          session_id: row.sessionId, page_url: message.pageUrl, fields, final: Boolean(message.final),
        })});
      }
      if (row.resumeFile) {
        // A few ATSs render a blank, optional file input beside the required
        // resume control. Upload exactly once to the required/named control;
        // never overwrite an input that already has a file after a rescan.
        const resumeTargets = resumeFieldsNeedingUpload(fields);
        const resumeAccepted = resumeFileAccepted(fields);
        for (const field of resumeTargets) {
          plan.fills = [...(plan.fills || []), {
            field_id: field.field_id, value: row.resumeFile.name, category: "resume_file",
            sensitive: false, options: [], file: row.resumeFile,
          }];
        }
        if (resumeAccepted || resumeTargets.length) {
          plan.blockers = (plan.blockers || []).filter(blocker => blocker.category !== "resume_file");
          plan.optional_review = (plan.optional_review || []).filter(item => item.category !== "resume_file");
          if (plan.state === "blocked" && !(plan.blockers || []).length) plan.state = "filling";
        }
      }
      row.state = plan.state;
      if (row.queueId) void syncSession(tabId);
      return plan;
    }
    if (message?.type === "JOB_RADAR_PAGE_BLOCKED" && tabId) {
      const row = tabs.get(tabId);
      if (!row?.queueId) return {ok: false, error: "Start this application from Job Radar first."};
      const reason = String(message.reason || "The opened posting does not contain an active application form.").slice(0, 800);
      row.state = "failed";
      await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
        action: "queue_update", queue_id: row.queueId, state: "failed", message: reason, error: reason,
      })});
      tabs.delete(tabId);
      return {ok: true};
    }
    if (message?.type === "JOB_RADAR_EVENT" && tabId) {
      const row = tabs.get(tabId);
      if (!row?.sessionId) return {ok: false};
      const result = await local("/api/application/event", {method: "POST", body: JSON.stringify({
        session_id: row.sessionId, state: message.state, message: message.message, error: message.error,
      })});
      row.state = result.state || message.state;
      if (row.queueId) void syncSession(tabId);
      return result;
    }
    if (message?.type === "JOB_RADAR_ISSUE" && tabId) {
      const row = tabs.get(tabId);
      const issue = await local("/api/application/issue", {method: "POST", body: JSON.stringify({
        ...(message.issue || {}), session_id: row?.sessionId || "", page_url: message.pageUrl,
      })});
      if (row?.queueId && issue?.issue) {
        try { await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({action: "report_issue", ...issue.issue})}); } catch (_) {}
      }
      return issue;
    }
    if (message?.type === "JOB_RADAR_GET_ACTIVE") {
      const current = tabId ? tabs.get(tabId) : null;
      if (!current?.sessionId) return {session: null, tab_id: tabId || null};
      return {session: await local(`/api/application/session?session_id=${encodeURIComponent(current.sessionId)}`), tab_id: tabId};
    }
    if (message?.type === "JOB_RADAR_SAVE_ANSWER") {
      const row = tabId ? tabs.get(tabId) : null;
      const answer = {...message.answer, session_id: row?.sessionId || ""};
      const result = await local("/api/application/answer", {method: "POST", body: JSON.stringify(answer)});
      try { await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({action: "answer", ...answer})}); } catch (_) {}
      return result;
    }
    if (message?.type === "JOB_RADAR_CONFIRM_LOCAL") {
      const row = tabId ? tabs.get(tabId) : null;
      if (!row?.sessionId) throw new Error("No active application session");
      const result = await local("/api/application/confirm", {method: "POST", body: JSON.stringify({
        session_id: row.sessionId, ...(message.review || {}),
      })});
      row.state = result.state || "submitting";
      await send(tabId, {type: "JOB_RADAR_SUBMISSION_APPROVED"});
      return result;
    }
    if (message?.type === "JOB_RADAR_RESCAN" && tabId) {
      const row = tabs.get(tabId);
      if (row?.sessionId) {
        try {
          await local("/api/application/event", {method: "POST", body: JSON.stringify({
            session_id: row.sessionId, state: "filling", message: "Owner requested a fresh application-page scan.",
          })});
          row.state = "filling";
        } catch (_) {}
      }
      await send(tabId, {type: "JOB_RADAR_RESCAN"});
      return {ok: true};
    }
    if (message?.type === "JOB_RADAR_VERIFY_SUBMISSION" && tabId) {
      const row = tabs.get(tabId);
      if (!row?.sessionId) throw new Error("No active application session");
      return local("/api/application/verify", {method: "POST", body: JSON.stringify({
        session_id: row.sessionId, page_url: message.pageUrl, fields: message.fields || [],
      })});
    }
    if (message?.type === "JOB_RADAR_GET_CONFIG") return config();
    if (message?.type === "JOB_RADAR_RELOAD_EXTENSION") return requestExtensionReload(sender, message);
    if (message?.type === "JOB_RADAR_SET_CONFIG") {
      if (!extensionControlSender(sender, message)) return {ok: false, error: "Pairing can only be written by the Job Radar popup or owner page."};
      const next = {...message.config, cloudUrl: String(message.config?.cloudUrl || DEFAULT_CLOUD_URL).replace(/\/$/, "")};
      await chrome.storage.local.set(next);
      return {ok: true, ...next, agentToken: next.agentToken ? "saved" : "missing", extensionVersion: chrome.runtime.getManifest().version};
    }
    if (message?.type === "JOB_RADAR_SYNC_NOW") return requestQueueSync(sender, message);
    return {ok: false};
  })().then(value => respond(value)).catch(error => respond({error: error.message || String(error)}));
  return true;
});

// A service worker can sleep between alarms; a manual popup action should
// always be able to pull a queue immediately.
void syncCloudQueue();
