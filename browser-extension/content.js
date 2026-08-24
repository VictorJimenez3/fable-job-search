(() => {
  "use strict";
  let sessionId = "";
  let scanTimer = 0;
  let lastFingerprint = "";
  let banner = null;
  let pageFailureReported = false;
  let stopped = false;
  const isRadar = /(^|\.)job-radar-newgrad\.vercel\.app$|(^|\.)vercel\.app$/.test(location.hostname) ||
    location.hostname === "victorjimenez3.github.io" && location.pathname.startsWith("/fable-job-search/");

  function text(value, limit = 500) { return String(value || "").replace(/\s+/g, " ").trim().slice(0, limit); }
  function safeURL(value) { try { const url = new URL(String(value || "")); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : ""; } catch (_) { return ""; } }
  function say(message) { return chrome.runtime.sendMessage(message).catch(() => null); }
  const ats = window.JobRadarATS || {providerForURL: () => "generic", adapter: () => ({}), fieldKey: () => "", labelFor: () => "", isSubmitLabel: value => /apply|submit|finish|complete/i.test(value), isNextLabel: value => /next|continue|review|save/i.test(value)};
  const provider = ats.providerForURL(location.href);

  if (isRadar) {
    window.addEventListener("message", (event) => {
      if (event.source !== window || event.data?.type !== "job-radar:start-application") return;
      const job = event.data.job;
      if (!job || !safeURL(job.url)) return;
      void say({type: "JOB_RADAR_START", job, mode: event.data.mode || "per_role", queueId: event.data.queueId || ""})
        .then(result => window.postMessage({type: "job-radar:agent-started", job_id: job.id, ok: !result?.error}, "*"));
    });
  }

  function visible(element) {
    if (!(element instanceof HTMLElement)) return false;
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
  }

  function labelFor(element) {
    const id = element.id;
    const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
    const aria = element.getAttribute("aria-label") || (element.getAttribute("aria-labelledby") || "").split(/\s+/).map(value => document.getElementById(value)?.textContent || "").join(" ");
    const parentLabel = element.closest("label")?.textContent || "";
    const previous = element.previousElementSibling?.textContent || element.parentElement?.querySelector("label")?.textContent || "";
    return text(ats.labelFor(element) || aria || label?.textContent || parentLabel || element.getAttribute("placeholder") || element.getAttribute("name") || element.id || previous, 500);
  }

  function optionData(element) {
    if (element.tagName === "SELECT") return [...element.options].map(option => ({value: option.value, label: text(option.textContent, 180)})).slice(0, 100);
    if (element.type === "radio" || element.type === "checkbox") return [{value: element.value, label: labelFor(element)}];
    return [];
  }

  function currentValue(element, type) {
    if (type === "checkbox" || type === "radio") return Boolean(element.checked);
    if (element.tagName === "SELECT") return text(element.selectedOptions?.[0]?.textContent || element.value, 500);
    return text(element.value || element.textContent, 500);
  }

  function extract() {
    const fields = [];
    const usedFieldIds = new Set();
    const controls = [...document.querySelectorAll("input, textarea, select, [contenteditable='true']")].filter(visible);
    controls.forEach((element, index) => {
      const type = element.getAttribute("contenteditable") === "true" ? "textarea" : text(element.getAttribute("type") || element.tagName, 32).toLowerCase();
      if (["hidden", "button", "submit", "reset", "image"].includes(type)) return;
      const label = labelFor(element);
      const baseFieldId = text(element.getAttribute("data-job-radar-field") || ats.fieldKey(element) || `${type}-${index}`, 160);
      let fieldId = baseFieldId;
      if (usedFieldIds.has(fieldId)) fieldId = `${baseFieldId}-${index}`.slice(0, 160);
      usedFieldIds.add(fieldId);
      fields.push({
        field_id: fieldId,
        id: text(element.id, 160), name: text(element.getAttribute("name"), 160),
        autocomplete: text(element.getAttribute("autocomplete"), 100),
        label, question: label, placeholder: text(element.getAttribute("placeholder"), 300),
        type, required: Boolean(element.required || element.getAttribute("aria-required") === "true"),
        options: optionData(element),
        value: currentValue(element, type),
      });
      element.setAttribute("data-job-radar-field", fields.at(-1).field_id);
    });
    const radioGroups = new Map();
    fields.filter(field => field.type === "radio" && field.name).forEach(field => {
      const group = radioGroups.get(field.name) || [];
      group.push(field);
      radioGroups.set(field.name, group);
    });
    for (const group of radioGroups.values()) {
      if (group.some(field => field.value === true)) {
        group.forEach(field => { if (!field.value) field.required = false; });
      }
    }
    const buttons = [...document.querySelectorAll("button, input[type='submit'], [role='button']")].filter(visible);
    buttons.forEach((element, index) => {
      const label = text(element.textContent || element.value || element.getAttribute("aria-label"), 300);
      if (!label || !ats.isSubmitLabel(label) && !ats.isNextLabel(label)) return;
      fields.push({field_id: `button-${index}-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`, label, question: label,
        type: "button", required: false, is_submit: ats.isSubmitLabel(label),
        is_next: ats.isNextLabel(label), options: []});
    });
    return {fields, final: fields.some(field => field.is_submit)};
  }

  function findField(fieldId) {
    const exact = document.querySelector(`[data-job-radar-field="${CSS.escape(fieldId)}"]`);
    if (exact) return exact;
    return [...document.querySelectorAll("input,textarea,select,[contenteditable='true']")].find(element => element.id === fieldId || element.name === fieldId) || null;
  }

  function unavailablePostingReason(snapshot) {
    if (snapshot.fields.some(field => field.type !== "button")) return "";
    const body = text(document.body?.innerText || "", 8000).toLowerCase();
    if (/job not found|position (?:is )?(?:filled|closed)|no longer accepting|role (?:is )?expired/.test(body)) {
      return "The opened posting is no longer available, so the agent stopped before filling anything.";
    }
    return "";
  }

  function setNativeValue(element, value) {
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor?.set) descriptor.set.call(element, value); else element.value = value;
    element.dispatchEvent(new Event("input", {bubbles: true}));
    element.dispatchEvent(new Event("change", {bubbles: true}));
    element.dispatchEvent(new Event("blur", {bubbles: true}));
  }

  function chooseOption(element, value, options) {
    const wanted = String(value || "").trim().toLowerCase();
    if (element.tagName === "SELECT") {
      const option = [...element.options].find(candidate => candidate.value.toLowerCase() === wanted || candidate.textContent.trim().toLowerCase() === wanted || candidate.textContent.trim().toLowerCase().includes(wanted));
      if (!option) return false;
      element.value = option.value;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
      return true;
    }
    if (element.type === "checkbox" || element.type === "radio") {
      const truthy = /^(1|true|yes|y|agree|authorized|eligible)$/i.test(String(value || ""));
      if (element.type === "checkbox") element.checked = truthy;
      else if (String(element.value || "").toLowerCase() === wanted || text(labelFor(element)).toLowerCase().includes(wanted)) element.checked = true;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
      return true;
    }
    if (element.isContentEditable) { element.textContent = value; element.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: String(value)})); return true; }
    setNativeValue(element, value);
    return true;
  }

  function showBanner(title, body, kind = "info", actions = "") {
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "job-radar-application-agent";
      banner.style.cssText = "position:fixed;z-index:2147483647;right:16px;top:16px;width:min(390px,calc(100vw - 32px));max-height:70vh;overflow:auto;background:#101820;color:#eef5f2;border:1px solid #4caf8a;border-radius:10px;box-shadow:0 10px 35px #0008;padding:14px;font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif";
      document.documentElement.appendChild(banner);
    }
    banner.innerHTML = `<strong style="display:block;margin-bottom:5px">${title}</strong><div style="color:#bdccc7">${body}</div>${actions ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">${actions}</div>` : ""}`;
    banner.querySelectorAll("button").forEach(button => button.onclick = () => {
      if (button.dataset.action === "retry") scheduleScan(0, true);
      if (button.dataset.action === "popup") chrome.action?.openPopup?.();
      if (button.dataset.action === "hide") banner.remove();
    });
  }

  function scheduleScan(delay = 350, force = false) {
    if (stopped) return;
    if (force) lastFingerprint = "";
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scan, delay);
  }
  async function scan() {
    if (stopped) return;
    const snapshot = extract();
    let ready = null;
    if (!sessionId) {
      const unavailable = unavailablePostingReason(snapshot);
      ready = await say({type: "JOB_RADAR_CONTENT_READY", url: location.href, pageFailure: unavailable});
      if (ready?.error) {
        showBanner("Application paused", text(ready.error, 600), "warn", '<button data-action="retry">retry</button>');
        return;
      }
      if (ready?.blocked && unavailable) {
        pageFailureReported = true;
        stopped = true;
        showBanner("Application stopped · posting unavailable", unavailable, "warn");
        return;
      }
      sessionId = ready?.session_id || "";
    }
    if (!sessionId) {
      const unavailable = unavailablePostingReason(snapshot);
      if (ready?.configured && unavailable && !pageFailureReported) {
        pageFailureReported = true;
        showBanner("Application stopped · posting unavailable", unavailable, "warn");
        void say({type: "JOB_RADAR_PAGE_BLOCKED", reason: unavailable});
      }
      return;
    }
    const shape = JSON.stringify(snapshot.fields.map(field => [field.field_id, field.label, field.type, field.required, field.options]));
    if (shape === lastFingerprint) return;
    lastFingerprint = shape;
    const plan = await say({type: "JOB_RADAR_FORM", pageUrl: location.href, fields: snapshot.fields, final: snapshot.final});
    if (!plan || plan.error) { showBanner("Job Radar Agent", text(plan?.error || "The local agent is unavailable.", 500), "bad", '<button data-action="retry">retry</button>'); return; }
    (plan.fills || []).forEach(fill => { const element = findField(fill.field_id); if (element) chooseOption(element, fill.value, fill.options); });
    const blockers = plan.blockers || [];
    if (blockers.length) {
      const items = blockers.map(item => `<li><strong>${text(item.label, 200)}</strong><br>${text(item.reason, 300)}</li>`).join("");
      showBanner("Action needed · not a system error", `<div style="margin-bottom:6px">The agent filled what was already approved, then stopped safely for owner-only information.</div><ul style="margin:5px 0 0 18px;padding:0">${items}</ul>`, "warn", '<button data-action="popup">open answer panel</button><button data-action="retry">retry after answering</button>');
      return;
    }
    if (plan.review) {
      const sensitive = (plan.review.fields || []).filter(field => field.sensitive).length;
      showBanner("Agent ready · review before submitting", `${plan.fills?.length || 0} fields filled${sensitive ? ` · ${sensitive} sensitive field${sensitive === 1 ? "" : "s"} shown in review` : ""}. The Submit button remains untouched.`, "info", '<button data-action="popup">open full review</button><button data-action="hide">dismiss</button>');
      return;
    }
    showBanner("Agent filled this page", `${plan.fills?.length || 0} approved field${plan.fills?.length === 1 ? "" : "s"} filled${plan.optional_review?.length ? ` · ${plan.optional_review.length} optional field${plan.optional_review.length === 1 ? "" : "s"} left for review` : ""}.`, "info", '<button data-action="hide">hide</button>');
    if (plan.state === "filling" && snapshot.fields.some(field => field.is_next)) {
      const next = [...document.querySelectorAll("button,[role='button'],input[type='submit']")].find(element => visible(element) && /next|continue/i.test(text(element.textContent || element.value || element.getAttribute("aria-label"))));
      if (next) setTimeout(() => next.click(), 450);
    }
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === "JOB_RADAR_AGENT_STOP") {
      stopped = true;
      sessionId = "";
      clearTimeout(scanTimer);
      showBanner("Application stopped", text(message.message || "This queue item is no longer active.", 600), "warn");
      return;
    }
    if (message?.type === "JOB_RADAR_AGENT_STATUS") {
      showBanner(text(message.title || "Agent working", 120), text(message.message || "The paired agent is working on this role.", 600), "info");
      return;
    }
    if (message?.type === "JOB_RADAR_RESCAN") { lastFingerprint = ""; scheduleScan(0); return; }
    if (message?.type === "JOB_RADAR_RESUME_STATUS") {
      const resume = message.resume || {};
      showBanner(
        resume.status === "fallback" ? "Resume Studio used the canonical resume" : "Resume Studio prepared this role",
        text(resume.message || "A role-specific resume result is ready. The browser will still pause before a resume-file upload.", 600),
        resume.status === "fallback" ? "warn" : "info",
        '<button data-action="hide">dismiss</button>',
      );
      scheduleScan(0, true);
      return;
    }
    if (message?.type !== "JOB_RADAR_SUBMISSION_APPROVED") return;
    void (async () => {
      const snapshot = extract();
      const verified = await say({type: "JOB_RADAR_VERIFY_SUBMISSION", pageUrl: location.href, fields: snapshot.fields});
      if (!verified || verified.error) {
        showBanner("Submission paused · page changed", text(verified?.error || "The approved page no longer matches.", 500), "warn", '<button data-action="retry">re-scan</button>');
        return;
      }
      const submit = [...document.querySelectorAll("button,[role='button'],input[type='submit']")].find(element => visible(element) && /apply|submit|finish|complete/i.test(text(element.textContent || element.value || element.getAttribute("aria-label"))));
      if (!submit) {
        showBanner("Submission paused", "The matching page has no visible Submit control.", "warn");
        return;
      }
      await say({type: "JOB_RADAR_EVENT", state: "submitting", message: "Owner-confirmed Submit clicked"});
      submit.click();
      setTimeout(() => void say({type: "JOB_RADAR_EVENT", state: "submitted", message: "The owner-confirmed Submit control was clicked"}), 1200);
    })();
  });
  const observer = new MutationObserver(() => scheduleScan(500));
  observer.observe(document.documentElement, {childList: true, subtree: true});
  window.addEventListener("popstate", () => scheduleScan(0));
  window.addEventListener("hashchange", () => scheduleScan(0));
  scheduleScan(800);
})();
