(() => {
  "use strict";
  let sessionId = "";
  let scanTimer = 0;
  let lastFingerprint = "";
  let banner = null;
  let pageFailureReported = false;
  let stopped = false;
  let scanRunning = false;
  let scanAgain = false;
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
    return;
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
    const container = element.closest(".ashby-application-form-field-entry, [data-field-path], fieldset, [role='group']");
    const containerLabel = [...(container?.querySelectorAll("legend, label, [class*='question-title'], [class*='question-label'], [class*='form-question'], [class*='label']") || [])]
      .map(node => text(node.textContent, 500)).find(value => value && value.toLowerCase() !== text(element.getAttribute("placeholder"), 500).toLowerCase()) || "";
    const previous = element.previousElementSibling?.textContent || element.parentElement?.querySelector("label")?.textContent || "";
    return text(ats.labelFor(element) || aria || label?.textContent || parentLabel || containerLabel || element.getAttribute("placeholder") || element.getAttribute("name") || element.id || previous, 500);
  }

  function optionGroup(element) {
    const container = element.closest("[data-field-path], .ashby-application-form-field-entry, fieldset, [role='group']");
    if (!container) return {container: null, question: "", key: "", options: []};
    const label = container.querySelector("label, legend, [class*='question-title'], [class*='label']");
    const buttons = [...container.querySelectorAll("button[data-option], [role='option']")].filter(visible);
    const options = buttons.map(button => text(button.textContent || button.getAttribute("data-option"), 180)).filter(Boolean);
    const input = container.querySelector("input[name], select[name], textarea[name]");
    const key = text(container.getAttribute("data-field-path") || input?.getAttribute("name") || label?.textContent, 180);
    return {container, question: text(label?.textContent || "", 500), key, options};
  }

  function controlGroupQuestion(element, label) {
    const container = element.closest("fieldset, [role='group'], [data-field-path], .ashby-application-form-field-entry");
    if (!container) return "";
    const candidates = [...container.querySelectorAll("legend, [class*='question-title'], [class*='question-label'], [class*='form-question']")]
      .map(node => text(node.textContent, 500)).filter(Boolean);
    return candidates.find(candidate => text(candidate).toLowerCase() !== text(label).toLowerCase()) || "";
  }

  function optionValue(element, group) {
    const pressed = element.getAttribute("aria-pressed");
    const selected = element.getAttribute("aria-selected");
    const dataSelected = element.getAttribute("data-selected");
    if (pressed === "true" || selected === "true" || dataSelected === "true") return true;
    if (pressed === "false" || selected === "false" || dataSelected === "false") return false;
    const classes = String(element.className || "").toLowerCase();
    if (/\b(selected|checked|active)\b/.test(classes)) return true;
    return false;
  }

  function optionData(element) {
    if (element.tagName === "SELECT") return [...element.options].map(option => ({value: option.value, label: text(option.textContent, 180)})).slice(0, 100);
    if (element.type === "radio" || element.type === "checkbox") return [{value: element.value, label: labelFor(element)}];
    return [];
  }

  function currentValue(element, type) {
    if (type === "checkbox" || type === "radio") return Boolean(element.checked);
    if (type === "button" || element.tagName === "BUTTON" || element.getAttribute("role") === "option") return optionValue(element, optionGroup(element));
    if (element.tagName === "SELECT") return text(element.selectedOptions?.[0]?.textContent || element.value, 500);
    if (type === "file") return text(element.files?.[0]?.name || "", 500);
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
      const groupQuestion = controlGroupQuestion(element, label);
      const baseFieldId = text(element.getAttribute("data-job-radar-field") || ats.fieldKey(element) || `${type}-${index}`, 160);
      let fieldId = baseFieldId;
      if (usedFieldIds.has(fieldId)) fieldId = `${baseFieldId}-${index}`.slice(0, 160);
      usedFieldIds.add(fieldId);
      fields.push({
        field_id: fieldId,
        id: text(element.id, 160), name: text(element.getAttribute("name"), 160),
        autocomplete: text(element.getAttribute("autocomplete"), 100),
        maxlength: Number(element.getAttribute("maxlength") || 0) || 0,
        label, question: label, group_question: groupQuestion, placeholder: text(element.getAttribute("placeholder"), 300),
        type, required: Boolean(element.required || element.getAttribute("aria-required") === "true"),
        options: optionData(element),
        value: currentValue(element, type),
      });
      element.setAttribute("data-job-radar-field", fields.at(-1).field_id);
    });
    const radioGroups = new Map();
    fields.filter(field => ["radio", "checkbox"].includes(field.type) && field.name).forEach(field => {
      const group = radioGroups.get(field.name) || [];
      group.push(field);
      radioGroups.set(field.name, group);
    });
    for (const group of radioGroups.values()) {
      const groupOptions = group.map(field => field.label).filter(Boolean);
      const groupQuestion = group.map(field => field.group_question).find(Boolean) || "";
      group.forEach(field => { field.group_options = groupOptions; if (groupQuestion) field.group_question = groupQuestion; });
      if (group.some(field => field.value === true)) {
        group.forEach(field => { if (!field.value) field.required = false; });
      }
    }
    const optionButtons = [...document.querySelectorAll("button[data-option], [role='option']")].filter(visible);
    const optionGroups = new Map();
    optionButtons.forEach((element, index) => {
      const group = optionGroup(element);
      const label = text(element.textContent || element.getAttribute("data-option"), 180);
      if (!label || !group.question) return;
      const groupKey = group.key || group.question;
      const options = group.options.length ? group.options : [label];
      const base = `option-${groupKey}-${label}`.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      let fieldId = text(base || `option-${index}`, 160);
      if (usedFieldIds.has(fieldId)) fieldId = `${fieldId}-${index}`.slice(0, 160);
      usedFieldIds.add(fieldId);
      const field = {
        field_id: fieldId,
        id: text(element.id, 160), name: text(element.getAttribute("name"), 160),
        label, question: label, group_question: group.question, group_key: groupKey,
        group_options: options, option_label: label, type: "button",
        required: Boolean(group.container?.querySelector("[required], [aria-required='true']")) || /\*/.test(group.question),
        is_option: true, options: options.map(option => ({value: option, label: option})),
        value: optionValue(element, group),
      };
      fields.push(field);
      element.setAttribute("data-job-radar-field", field.field_id);
      const siblings = optionGroups.get(groupKey) || [];
      siblings.push(field);
      optionGroups.set(groupKey, siblings);
    });
    for (const group of optionGroups.values()) {
      const groupOptions = group.map(field => field.label).filter(Boolean);
      group.forEach(field => { field.group_options = groupOptions; });
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
    return [...document.querySelectorAll("input,textarea,select,button,[role='button'],[role='option'],[contenteditable='true']")].find(element => element.id === fieldId || element.name === fieldId) || null;
  }

  function unavailablePostingReason(snapshot) {
    if (snapshot.fields.some(field => field.type !== "button")) return "";
    const body = text(document.body?.innerText || "", 8000).toLowerCase();
    if (/job not found|page (?:you are looking for )?(?:doesn't|does not) exist|(?:this )?job (?:has |is )?closed|position (?:is )?(?:filled|closed)|no longer accepting|role (?:is )?expired/.test(body)) {
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

  function chooseOption(element, value, options, file = null) {
    const wanted = String(value || "").trim().toLowerCase();
    if (element.type === "file") {
      if (!file?.base64 || !file?.name) return false;
      // React-based ATS forms can trigger a second scan after the upload
      // event. Reusing the accepted file is safe; assigning a new File object
      // restarts provider-side validation and can make the posting look
      // broken while the upload is still settling.
      if (element.files?.[0]?.name === file.name) return true;
      try {
        const binary = atob(file.base64);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
        const transfer = new DataTransfer();
        transfer.items.add(new File([bytes], file.name, {type: file.type || "application/pdf", lastModified: Date.now()}));
        element.files = transfer.files;
        element.dispatchEvent(new Event("input", {bubbles: true}));
        element.dispatchEvent(new Event("change", {bubbles: true}));
        element.dispatchEvent(new Event("blur", {bubbles: true}));
        return element.files?.[0]?.name === file.name;
      } catch (_) { return false; }
    }
    if (element.tagName === "SELECT") {
      const option = [...element.options].find(candidate => candidate.value.toLowerCase() === wanted || candidate.textContent.trim().toLowerCase() === wanted || candidate.textContent.trim().toLowerCase().includes(wanted));
      if (!option) return false;
      element.value = option.value;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
      return true;
    }
    if (element.type === "checkbox" || element.type === "radio") {
      const matchesOption = wanted && (String(element.value || "").toLowerCase() === wanted || text(labelFor(element)).toLowerCase().includes(wanted));
      const truthy = matchesOption || /^(1|true|yes|y|agree|authorized|eligible|none)$/i.test(String(value || ""));
      if (element.type === "checkbox") element.checked = truthy;
      else if (matchesOption || truthy) element.checked = true;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
      return true;
    }
    if (element.tagName === "BUTTON" || element.getAttribute("role") === "option" || element.getAttribute("data-option")) {
      if (currentValue(element, "button")) return true;
      const label = text(element.textContent || element.getAttribute("data-option"), 300).toLowerCase();
      if (wanted === "true" || wanted === "1" || wanted === "yes" || wanted === "y" || label === wanted || label.includes(wanted)) {
        element.click();
        return true;
      }
      return false;
    }
    if (element.isContentEditable) { element.textContent = value; element.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: String(value)})); return true; }
    setNativeValue(element, value);
    return true;
  }

  function showBanner(title, body, kind = "info", actions = "") {
    // Extension reloads can leave an older content-script instance alive in an
    // already-open tab. Reuse the canonical node and remove any duplicates so
    // status updates never render on top of one another.
    const existingBanners = [...document.querySelectorAll("#job-radar-application-agent")];
    if (existingBanners.length) {
      banner = existingBanners[0];
      existingBanners.slice(1).forEach(node => node.remove());
    }
    if (!banner || !banner.isConnected) {
      banner = document.createElement("div");
      banner.id = "job-radar-application-agent";
      banner.style.cssText = "box-sizing:border-box;display:block;position:fixed;z-index:2147483647;right:16px;top:16px;width:min(390px,calc(100vw - 32px));max-height:70vh;overflow:auto;background:#101820;color:#eef5f2;border:1px solid #4caf8a;border-radius:10px;box-shadow:0 10px 35px #0008;padding:14px;font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;white-space:normal;overflow-wrap:anywhere;word-break:break-word";
      document.documentElement.appendChild(banner);
    }
    banner.setAttribute("aria-live", "polite");
    banner.innerHTML = `<strong style="box-sizing:border-box;display:block;margin:0 0 7px;line-height:1.3;white-space:normal;overflow-wrap:anywhere;word-break:break-word">${title}</strong><div style="box-sizing:border-box;display:block;color:#bdccc7;line-height:1.45;white-space:normal;overflow-wrap:anywhere;word-break:break-word">${body}</div>${actions ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">${actions}</div>` : ""}`;
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
    scanTimer = setTimeout(() => void runScan(), delay);
  }
  async function runScan() {
    if (stopped) return;
    if (scanRunning) { scanAgain = true; return; }
    scanRunning = true;
    try { await scan(); }
    finally {
      scanRunning = false;
      if (scanAgain) { scanAgain = false; scheduleScan(0); }
    }
  }
  async function scan() {
    if (stopped) return;
    const snapshot = extract();
    const unavailable = unavailablePostingReason(snapshot);
    let ready = null;
    if (!sessionId) {
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
    if (unavailable && !pageFailureReported) {
      const blocked = await say({type: "JOB_RADAR_PAGE_BLOCKED", reason: unavailable});
      if (blocked?.ok) {
        pageFailureReported = true;
        stopped = true;
        showBanner("Application stopped · posting unavailable", unavailable, "warn");
        return;
      }
    }
    if (!sessionId) return;
    const shape = JSON.stringify(snapshot.fields.map(field => [field.field_id, field.label, field.group_question, field.type, field.required, field.value, field.options]));
    if (shape === lastFingerprint) return;
    lastFingerprint = shape;
    const plan = await say({type: "JOB_RADAR_FORM", pageUrl: location.href, fields: snapshot.fields, final: snapshot.final});
    if (!plan || plan.error) {
      const message = text(plan?.error || "The local agent is unavailable.", 500);
      if (/application review is ready/i.test(message)) {
        showBanner("Review ready · no changes applied", "The page already matches the saved review. Rescan only after you intentionally change an answer.", "info", '<button data-action="retry">re-scan</button>');
      } else {
        showBanner("Job Radar Agent", message, "bad", '<button data-action="retry">retry</button>');
      }
      return;
    }
    const failedFiles = [];
    let uploadedResume = false;
    (plan.fills || []).forEach(fill => {
      const element = findField(fill.field_id);
      if (!element) return;
      const filled = chooseOption(element, fill.value, fill.options, fill.file);
      if (fill.file && !filled) failedFiles.push(fill);
      if (fill.file && filled) uploadedResume = true;
    });
    if (failedFiles.length) {
      const reason = `Resume Studio selected ${text(failedFiles[0].file?.name || "a PDF", 180)}, but this page rejected the automatic upload.`;
      await say({type: "JOB_RADAR_EVENT", state: "blocked", message: reason, error: reason});
      showBanner("Application paused · resume upload failed", reason, "warn", '<button data-action="retry">retry upload</button>');
      return;
    }
    if (uploadedResume) {
      // Do not click Next in the same turn as a file assignment. Let the ATS
      // render its accepted-file state, then let the next scan continue with
      // the already-selected file and no second upload.
      await say({type: "JOB_RADAR_EVENT", state: "filling", message: "Resume uploaded; waiting for the employer form to validate the PDF before continuing."});
      showBanner("Resume uploaded · waiting for the posting", "The selected PDF is in the employer form. Waiting for the upload validation to finish before continuing.", "info", '<button data-action="hide">hide</button>');
      lastFingerprint = "";
      scheduleScan(1400);
      return;
    }
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
        resume.status === "fallback" ? "Resume Studio selected a fallback resume" : "Resume Studio prepared this role",
        text(resume.message || "A role-specific resume is ready and will be uploaded automatically when the form asks for it.", 600),
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
