const LOCAL_ORIGIN = "http://127.0.0.1:4317";
const DEFAULT_CLOUD_URL = "https://job-radar-newgrad.vercel.app";
const BATCH_RUNNING_STATES = new Set(["opening", "filling", "submitting"]);
const RESUME_TERMINAL_STATES = new Set(["complete", "awaiting_review", "failed"]);
const tabs = new Map();
let syncPromise = null;
let syncBlockedUntil = 0;

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

async function config() {
  const stored = await chrome.storage.local.get({cloudUrl: DEFAULT_CLOUD_URL, agentToken: "", autoContinue: true});
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
  tabs.set(tabId, {...tabs.get(tabId), job, mode, queueId, sessionId: session.session_id, state: session.state});
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

async function send(tabId, message) {
  try { return await chrome.tabs.sendMessage(tabId, message); } catch (_) { return null; }
}

async function startJob(job, mode = "per_role", queueId = "") {
  const url = safeURL(job?.url);
  if (!url || !job?.id || !job?.company || !job?.title) throw new Error("A complete safe job snapshot is required");
  const tab = await chrome.tabs.create({url, active: true});
  tabs.set(tab.id, {job, mode, queueId, sessionId: "", createdAt: Date.now()});
  return {tab_id: tab.id, url};
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
        message: session.last_error || session.state, error: session.last_error || ""}),
    });
    const item = (await cloud("/api/application-agent?view=queue")).items?.find(value => value.queue_id === row.queueId);
    if (item?.confirmation && session.state === "awaiting_confirmation") {
      await local("/api/application/confirm", {method: "POST", body: JSON.stringify({
        session_id: row.sessionId, review_hash: item.confirmation.review_hash,
        nonce: item.review?.nonce || "", page_fingerprint: item.confirmation.page_fingerprint,
      })});
      await send(tabId, {type: "JOB_RADAR_SUBMISSION_APPROVED"});
    }
  } catch (error) {
    console.warn("Job Radar cloud sync", error);
  }
}

async function syncCloudQueueImpl() {
  let data;
  try { data = await cloud("/api/application-agent?view=queue"); }
  catch (error) {
    console.warn("Job Radar queue", error);
    return {ok: false, error: noteQuota(error)};
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
      const cloudIds = new Set((context.context?.answers || []).map(answer => answer.answer_id).filter(Boolean));
      const missing = (localContext.answers || []).filter(answer => answer?.answer_id && !cloudIds.has(answer.answer_id));
      if (missing.length) {
        const synced = await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({action: "answers", answers: missing})});
        if (synced.context) context.context = synced.context;
      }
    } catch (error) {
      syncError = noteQuota(error);
      console.warn("Job Radar profile context sync", error);
    }
    const stored = await chrome.storage.local.get({contextUpdatedAt: ""});
    if (context.context?.updated_at && context.context.updated_at !== stored.contextUpdatedAt) {
      for (const answer of context.context.answers || []) {
        await local("/api/application/answer", {method: "POST", body: JSON.stringify(answer)});
      }
      await chrome.storage.local.set({contextUpdatedAt: context.context.updated_at});
    }
  } catch (error) {
    syncError = noteQuota(error);
    console.warn("Job Radar context sync", error);
  }
  const items = Array.isArray(data.items) ? data.items : [];
  let batchRunning = items.some(item => BATCH_RUNNING_STATES.has(item.state)) ||
    [...tabs.values()].some(row => row.queueId && BATCH_RUNNING_STATES.has(row.state || "opening"));
  for (const item of items) {
    if (batchRunning) break;
    if (item.state === "queued" && ![...tabs.values()].some(row => row.queueId === item.queue_id)) {
      try {
        const started = await startJob(item.job, "batch", item.queue_id);
        batchRunning = true;
        await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
          action: "queue_update", queue_id: item.queue_id, state: "opening", message: "Opening on the paired Mac",
        })});
        console.info("Job Radar opened queued application", started.tab_id);
      } catch (error) { console.warn("Job Radar could not open queue item", error); }
    }
  }
  for (const tabId of tabs.keys()) await syncSession(tabId);
  return {ok: !syncError, error: syncError, queued: items.filter(item => item.state === "queued").length,
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

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("job-radar-application-sync", {periodInMinutes: 1});
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "job-radar-application-sync") void syncCloudQueue();
});
chrome.tabs.onRemoved.addListener((tabId) => tabs.delete(tabId));

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  const tabId = sender.tab?.id;
  (async () => {
    if (message?.type === "JOB_RADAR_START") return startJob(message.job, message.mode, message.queueId);
    if (message?.type === "JOB_RADAR_CONTENT_READY" && tabId) {
      let row = tabs.get(tabId);
      if (row && !row.sessionId) {
        try {
          await ensureResumeForApplication(tabId, row);
        } catch (error) {
          if (row.queueId) {
            try { await cloud("/api/application-agent", {method: "POST", body: JSON.stringify({
              action: "queue_update", queue_id: row.queueId, state: "blocked",
              message: "Application paused before filling because Resume Studio needs attention.", error: error.message,
            })}); } catch (_) {}
          }
          return {session_id: "", configured: Boolean(row), error: error.message};
        }
        await createSession(tabId, row.job, row.mode, row.queueId);
      }
      row = tabs.get(tabId);
      return {session_id: row?.sessionId || "", configured: Boolean(row)};
    }
    if (message?.type === "JOB_RADAR_FORM" && tabId) {
      const row = tabs.get(tabId);
      if (!row?.sessionId) return {error: "Start this application from Job Radar first."};
      const plan = await local("/api/application/form", {method: "POST", body: JSON.stringify({
        session_id: row.sessionId, page_url: message.pageUrl, fields: message.fields || [], final: Boolean(message.final),
      })});
      row.state = plan.state;
      if (row.queueId) void syncSession(tabId);
      return plan;
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
    if (message?.type === "JOB_RADAR_SET_CONFIG") {
      const next = {...message.config, cloudUrl: String(message.config?.cloudUrl || DEFAULT_CLOUD_URL).replace(/\/$/, "")};
      await chrome.storage.local.set(next);
      return {ok: true, ...next, agentToken: next.agentToken ? "saved" : "missing"};
    }
    if (message?.type === "JOB_RADAR_SYNC_NOW") return syncCloudQueue();
    return {ok: false};
  })().then(value => respond(value)).catch(error => respond({error: error.message || String(error)}));
  return true;
});

// A service worker can sleep between alarms; a manual popup action should
// always be able to pull a queue immediately.
void syncCloudQueue();
