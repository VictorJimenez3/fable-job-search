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
  let loopPaused = false;
  let structuralScans = [];
  let simplifyHandoff = "pending";
  let simplifyStartedAt = 0;
  let simplifyLastActivityAt = Date.now();
  let simplifyBaselineSignature = "";
  let simplifyTriggerRequested = false;
  let submissionAttempted = false;
  const pageLoadedAt = Date.now();
  const uploadedFiles = new Map();
  const LOOP_SCAN_LIMIT = 4;
  const LOOP_SCAN_WINDOW_MS = 45_000;
  const SIMPLIFY_DISCOVERY_WINDOW_MS = 900;
  const SIMPLIFY_SETTLE_MS = 2_500;
  const SIMPLIFY_MAX_WAIT_MS = 20_000;
  const isRadar = location.hostname === "job-radar-newgrad.vercel.app";

  function text(value, limit = 500) { return String(value || "").replace(/\s+/g, " ").trim().slice(0, limit); }
  function wait(milliseconds) { return new Promise(resolve => setTimeout(resolve, milliseconds)); }
  function safeURL(value) { try { const url = new URL(String(value || "")); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : ""; } catch (_) { return ""; } }
  function say(message) { return chrome.runtime.sendMessage(message).catch(() => null); }
  const ats = window.JobRadarATS || {providerForURL: () => "generic", adapter: () => ({}), fieldKey: () => "", labelFor: () => "", isSubmitLabel: value => /apply|submit|finish|complete/i.test(value), isNextLabel: value => /next|continue|review|save/i.test(value)};
  const provider = ats.providerForURL(location.href);

  if (isRadar) {
    window.addEventListener("message", (event) => {
      if (event.source !== window || event.origin !== location.origin) return;
      if (event.data?.type === "job-radar:extension-command") {
        const command = String(event.data.command || "");
        const messageType = {status: "JOB_RADAR_STATUS", reload_extension: "JOB_RADAR_RELOAD_EXTENSION", sync_queue: "JOB_RADAR_SYNC_NOW", set_pairing: "JOB_RADAR_SET_CONFIG"}[command];
        if (!messageType) return;
        const requestId = text(event.data.requestId, 120);
        const message = {type: messageType, source: "radar-page", requestId};
        if (command === "set_pairing") {
          message.config = {
            cloudUrl: text(event.data.cloudUrl, 300),
            agentToken: text(event.data.agentToken, 500),
          };
        }
        void say(message).then(result => window.postMessage({
          type: "job-radar:extension-command-result", requestId, command, result,
        }, location.origin));
        return;
      }
      if (event.data?.type !== "job-radar:start-application") return;
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

  function simplifyIsBusy() {
    const nodes = [...document.querySelectorAll("[id*='simplify' i], [class*='simplify' i], [id*='copilot' i], [class*='copilot' i]")];
    return nodes.some(node => visible(node) && /\b(?:autofill(?:ing)?|processing|loading|working)\b/i.test(text(node.textContent, 500)));
  }

  function simplifyFormSignature() {
    return [...document.querySelectorAll("input, textarea, select, [contenteditable='true']")]
      .filter(visible).map(element => [element.name || element.id || element.type, currentValue(element, element.type || element.tagName)]).slice(0, 400);
  }

  async function handoffToSimplify() {
    const now = Date.now();
    const signature = JSON.stringify(simplifyFormSignature());
    if (!simplifyBaselineSignature) simplifyBaselineSignature = signature;
    if (signature !== simplifyBaselineSignature) {
      simplifyBaselineSignature = signature;
      simplifyLastActivityAt = now;
    }
    if (simplifyHandoff === "started") {
      const quiet = now - simplifyLastActivityAt >= SIMPLIFY_SETTLE_MS;
      if (now - simplifyStartedAt < SIMPLIFY_MAX_WAIT_MS && (!quiet || simplifyIsBusy())) {
        showBanner("Simplify Copilot filling first", "The official Simplify pass is running. Job Radar will finish the remaining fields after the page is quiet.", "info");
        scheduleScan(500);
        return true;
      }
      simplifyHandoff = "settled";
      await say({type: "JOB_RADAR_EVENT", state: "finishing", message: "Simplify settled; Job Radar is reconciling remaining fields."});
      return false;
    }
    if (simplifyHandoff === "unavailable") return false;
    if (simplifyHandoff === "settled") return false;
    if (now - pageLoadedAt < SIMPLIFY_DISCOVERY_WINDOW_MS) {
      showBanner("Waiting for Simplify Copilot", "Job Radar is giving the official Simplify extension a moment to start before it finishes the remaining fields.", "info");
      scheduleScan(350);
      return true;
    }
    if (!simplifyTriggerRequested) {
      simplifyTriggerRequested = true;
      simplifyHandoff = "started";
      simplifyStartedAt = now;
      const result = await say({type: "JOB_RADAR_SIMPLIFY_REQUEST", pageUrl: location.href});
      if (!result?.triggered) {
        simplifyHandoff = "unavailable";
        await say({type: "JOB_RADAR_EVENT", state: "finishing", message: result?.error || "Simplify Copilot was unavailable; Job Radar is finishing safely."});
        return false;
      }
      await say({type: "JOB_RADAR_EVENT", state: "simplify_filling", message: "Simplify Copilot started its supported Autofill pass through the keyboard command; Job Radar will finish only fields it leaves behind."});
      showBanner("Simplify Copilot filling first", "The official Simplify pass is running. Job Radar will wait for it, then fill missing approved fields, write role-specific responses, and handle the selected resume.", "info");
      return true;
    }
    return true;
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

  function uploadedFileKey(element, fieldId = "") {
    return `${location.origin}${location.pathname}:${fieldId || element.getAttribute("data-job-radar-field") || element.id || element.name || "file"}`;
  }

  function rememberedUploadedFile(element, fieldId = "") {
    const exactKey = uploadedFileKey(element, fieldId);
    const remembered = uploadedFiles.get(exactKey);
    if (!remembered?.name) return "";
    const container = element.closest(".ashby-application-form-field-entry, [data-field-path], fieldset, label, [class*='upload'], [class*='file']")
      || element.parentElement?.parentElement || element.parentElement;
    const nearby = text(container?.innerText || container?.textContent || "", 2400).toLowerCase();
    const filename = text(remembered.name, 500).toLowerCase();
    if (/upload failed|invalid file|unsupported file|file too large|could not upload|try again/i.test(nearby)) return "";
    // Some React ATS controls consume the File and clear input.files before
    // their accepted-file chip renders. The successful assignment is valid
    // short-lived evidence; after that, require the employer UI to retain the
    // filename so a genuinely rejected upload can be retried safely.
    if (filename && nearby.includes(filename)) return remembered.name;
    return Date.now() - Number(remembered.at || 0) < 20_000 ? remembered.name : "";
  }

  function currentValue(element, type, fieldId = "") {
    if (type === "checkbox" || type === "radio") return Boolean(element.checked);
    if (type === "button" || element.tagName === "BUTTON" || element.getAttribute("role") === "option") return optionValue(element, optionGroup(element));
    if (element.tagName === "SELECT") return text(element.selectedOptions?.[0]?.textContent || element.value, 500);
    if (type === "file") return text(element.files?.[0]?.name || rememberedUploadedFile(element, fieldId), 500);
    const limit = type === "textarea" || element.isContentEditable ? 20_000 : 4_000;
    return text(element.value || element.textContent, limit);
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
        value: currentValue(element, type, fieldId),
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
      const fieldId = `button-${index}-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
      element.setAttribute("data-job-radar-field", fieldId);
      fields.push({field_id: fieldId, label, question: label,
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

  async function submitFinalControl(fields) {
    if (submissionAttempted) return;
    const final = (fields || []).find(field => field?.is_submit);
    const submit = final ? findField(final.field_id) : null;
    if (!final || !submit || !visible(submit)) {
      await say({type: "JOB_RADAR_EVENT", state: "needs_input", message: "The exact employer Submit control is not visible; the application is paused safely."});
      showBanner("Submission paused", "The matching application page has no visible final Submit control. Nothing was clicked.", "warn", '<button data-action="retry">rescan</button>');
      return;
    }
    const verified = await say({type: "JOB_RADAR_VERIFY_SUBMISSION", pageUrl: location.href, fields: extract().fields});
    if (!verified || verified.error) {
      await say({type: "JOB_RADAR_EVENT", state: "needs_input", message: verified?.error || "The application page changed before Submit."});
      showBanner("Submission paused · page changed", text(verified?.error || "The approved page no longer matches.", 500), "warn", '<button data-action="retry">rescan</button>');
      return;
    }
    submissionAttempted = true;
    await say({type: "JOB_RADAR_EVENT", state: "submitting", message: "The exact employer Submit control passed validation; clicking once."});
    submit.click();
    await wait(1400);
    const body = text(document.body?.innerText || "", 6000);
    const success = /application (?:submitted|received)|thank you for applying|successfully submitted|we received your application|confirmation number/i.test(body)
      || !submit.isConnected || !extract().final;
    if (success) {
      const receipt = `jobradar-${sessionId}-${Date.now()}`;
      await say({type: "JOB_RADAR_EVENT", state: "submitted", message: `Application submitted once · receipt ${receipt}`});
      showBanner("Application submitted", "The employer page confirmed the final action. The receipt is saved in Job Radar.", "info");
    } else {
      await say({type: "JOB_RADAR_EVENT", state: "submission_uncertain", message: "Submit was clicked once, but the employer did not expose a confirmation page. It will not be retried automatically."});
      showBanner("Submission uncertain", "The final control was clicked once, but the employer did not show a confirmation. Job Radar will not click again automatically.", "warn");
    }
  }

  function unavailablePostingReason(snapshot) {
    if (snapshot.fields.some(field => field.type !== "button")) return "";
    const body = text(document.body?.innerText || "", 8000).toLowerCase();
    if (/job not found|page (?:you are looking for )?(?:doesn't|does not) exist|(?:this )?job (?:has |is )?closed|position (?:is )?(?:filled|closed)|no longer accepting|role (?:is )?expired/.test(body)) {
      return "The opened posting is no longer available, so the agent stopped before filling anything.";
    }
    return "";
  }

  function applicationBoundaryReason() {
    const host = location.hostname.toLowerCase();
    if (/\b(jobright\.ai|simplify\.jobs|linkedin\.com|indeed\.com)\b/.test(host)) {
      return "This is an aggregator detail page, not the employer application. Job Radar paused before touching its search or sign-in fields. Open the direct employer Apply link in this same tab to continue automatically.";
    }
    return "";
  }

  function directApplicationURL() {
    const currentHost = location.hostname.toLowerCase();
    const candidates = [...document.querySelectorAll("a[href]")].map((anchor, index) => {
      const label = text(anchor.textContent || anchor.getAttribute("aria-label") || anchor.title, 240);
      if (!/\b(apply|application|continue to job|company site)\b/i.test(label)) return null;
      const href = safeURL(anchor.href);
      if (!href) return null;
      const url = new URL(href);
      const host = url.hostname.toLowerCase();
      if (host === currentHost || /\b(jobright\.ai|simplify\.jobs|linkedin\.com|indeed\.com)\b/.test(host)) return null;
      const direct = /\b(ashbyhq\.com|greenhouse\.(?:io|com)|lever\.co|smartrecruiters\.com|myworkdayjobs\.com|myworkdaysite\.com)\b/.test(host)
        || /\b(careers?|jobs?)\b/.test(host);
      return {href, index, score: direct ? 3 : 1};
    }).filter(Boolean).sort((left, right) => right.score - left.score || left.index - right.index);
    return candidates[0]?.href || "";
  }

  function setNativeValue(element, value, {blur = true} = {}) {
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor?.set) descriptor.set.call(element, value); else element.value = value;
    element.dispatchEvent(new Event("input", {bubbles: true}));
    element.dispatchEvent(new Event("change", {bubbles: true}));
    if (blur) element.dispatchEvent(new Event("blur", {bubbles: true}));
  }

  function choiceGroupKey(field) {
    const type = String(field?.type || "").toLowerCase();
    if (!['radio', 'button'].includes(type)) return "";
    const identity = type === "button"
      ? field.group_key || field.group_question
      : field.name || field.group_question;
    return String(identity || "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function conflictingChoiceFills(fills, fields) {
    const byId = new Map((fields || []).map(field => [field.field_id, field]));
    const groups = new Map();
    for (const fill of fills || []) {
      const field = byId.get(fill.field_id);
      const group = choiceGroupKey(field);
      if (!group) continue;
      const ids = groups.get(group) || new Set();
      ids.add(fill.field_id);
      groups.set(group, ids);
    }
    return [...groups.entries()].filter(([, ids]) => ids.size > 1).map(([group]) => group);
  }

  async function chooseOption(element, value, options, file = null) {
    const wanted = String(value || "").trim().toLowerCase();
    if (element.type === "file") {
      if (!file?.base64 || !file?.name) return false;
      // React-based ATS forms can trigger a second scan after the upload
      // event. Reusing the accepted file is safe; assigning a new File object
      // restarts provider-side validation and can make the posting look
      // broken while the upload is still settling.
      if (element.files?.[0]?.name === file.name || rememberedUploadedFile(element) === file.name) return true;
      try {
        const binary = atob(file.base64);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
        const transfer = new DataTransfer();
        transfer.items.add(new File([bytes], file.name, {type: file.type || "application/pdf", lastModified: Date.now()}));
        element.files = transfer.files;
        if (element.files?.[0]?.name !== file.name) return false;
        // Remember the accepted assignment before dispatching provider events.
        // An event handler may synchronously replace the input or clear its
        // FileList after consuming it.
        uploadedFiles.set(uploadedFileKey(element), {name: file.name, at: Date.now()});
        element.dispatchEvent(new Event("input", {bubbles: true}));
        element.dispatchEvent(new Event("change", {bubbles: true}));
        element.dispatchEvent(new Event("blur", {bubbles: true}));
        return true;
      } catch (_) { return false; }
    }
    if (element.tagName === "SELECT") {
      const option = [...element.options].find(candidate => candidate.value.toLowerCase() === wanted || candidate.textContent.trim().toLowerCase() === wanted || candidate.textContent.trim().toLowerCase().includes(wanted));
      if (!option) return false;
      if (element.value === option.value) return true;
      element.value = option.value;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
      return true;
    }
    if (element.type === "checkbox" || element.type === "radio") {
      const matchesOption = wanted && (String(element.value || "").toLowerCase() === wanted || text(labelFor(element)).toLowerCase().includes(wanted));
      const truthy = matchesOption || /^(1|true|yes|y|agree|authorized|eligible|none)$/i.test(String(value || ""));
      if (element.checked === truthy) return true;
      if (element.type === "checkbox") element.checked = truthy;
      else if (matchesOption || truthy) element.checked = true;
      element.dispatchEvent(new Event("input", {bubbles: true}));
      element.dispatchEvent(new Event("change", {bubbles: true}));
      return true;
    }
    if (element.tagName === "BUTTON" || element.getAttribute("role") === "option" || element.getAttribute("data-option")) {
      const label = text(element.textContent || element.getAttribute("data-option"), 300).toLowerCase();
      const matchesWanted = wanted === "true" || wanted === "1" || wanted === "yes" || wanted === "y" || label === wanted || label.includes(wanted);
      if (!matchesWanted) return false;
      if (currentValue(element, "button")) return true;
      element.click();
      return true;
    }
    if (element.isContentEditable) {
      if (text(element.textContent) === text(value)) return true;
      element.textContent = value;
      element.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: String(value)}));
      return true;
    }
    const isCombobox = element.getAttribute("role") === "combobox" || Boolean(element.getAttribute("aria-autocomplete"));
    if (isCombobox) {
      if (String(element.value || "") === String(value || "") && element.getAttribute("aria-expanded") !== "true") return true;
      element.focus();
      setNativeValue(element, value, {blur: false});
      await wait(140);
      const controlsId = element.getAttribute("aria-controls");
      const controlled = controlsId ? document.getElementById(controlsId) : null;
      const candidates = [...(controlled || document).querySelectorAll("[role='option']")].filter(visible);
      const normalizedWanted = text(value, 500).toLowerCase();
      const exact = candidates.find(option => {
        const label = text(option.textContent, 500).toLowerCase();
        return label === normalizedWanted || label.includes(normalizedWanted) || normalizedWanted.includes(label);
      });
      const highlighted = candidates.find(option => option.getAttribute("aria-selected") === "true");
      const selected = exact || highlighted;
      if (selected) {
        selected.click();
        await wait(100);
      } else {
        element.dispatchEvent(new Event("blur", {bubbles: true}));
      }
      return String(element.value || "").trim() === String(value || "").trim()
        && element.getAttribute("aria-invalid") !== "true";
    }
    if (String(element.value || "") === String(value || "")) return true;
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
      if (button.dataset.action === "retry") {
        loopPaused = false;
        structuralScans = [];
        scheduleScan(0, true);
      }
      if (button.dataset.action === "popup") chrome.action?.openPopup?.();
      if (button.dataset.action === "hide") banner.remove();
    });
  }

  function scheduleScan(delay = 350, force = false) {
    if (stopped) return;
    if (loopPaused && !force) return;
    if (force) loopPaused = false;
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
  function scanLoopDetected(snapshot) {
    const now = Date.now();
    const structure = JSON.stringify(snapshot.fields.map(field => [
      field.field_id, field.label, field.group_question, field.type,
      field.required, field.options, field.value, Boolean(field.is_next), Boolean(field.is_submit),
    ]));
    structuralScans = structuralScans.filter(item => now - item.at <= LOOP_SCAN_WINDOW_MS);
    structuralScans.push({at: now, structure});
    // Count the complete live value signature, not just the static form
    // structure. A long form can legitimately need many passes; it is only a
    // loop when the same values recur without net progress.
    return structuralScans.filter(item => item.structure === structure).length >= LOOP_SCAN_LIMIT;
  }
  async function scan() {
    if (stopped) return;
    const snapshot = extract();
    const unavailable = unavailablePostingReason(snapshot);
    const pageBoundary = applicationBoundaryReason();
    let ready = null;
    if (!sessionId) {
      ready = await say({
        type: "JOB_RADAR_CONTENT_READY", url: location.href, pageFailure: unavailable,
        pageBoundary, directApplicationUrl: pageBoundary ? directApplicationURL() : "",
      });
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
      if (ready?.boundary && pageBoundary) {
        stopped = true;
        showBanner(
          ready?.navigating ? "Opening the direct employer application" : "Application paused · direct employer link needed",
          ready?.navigating ? "The aggregator exposed a safe external Apply link. Job Radar is continuing in this tab automatically." : pageBoundary,
          ready?.navigating ? "info" : "warn",
        );
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
    if (await handoffToSimplify()) return;
    const shape = JSON.stringify(snapshot.fields.map(field => [field.field_id, field.label, field.group_question, field.type, field.required, field.value, field.options]));
    if (shape === lastFingerprint) return;
    lastFingerprint = shape;
    if (scanLoopDetected(snapshot)) {
      const reason = "The form changed choices repeatedly without making progress. The agent stopped this tab before it could loop again.";
      loopPaused = true;
      await say({type: "JOB_RADAR_EVENT", state: "blocked", message: reason, error: reason});
      showBanner("Application paused · repeated form cycle", reason, "warn", '<button data-action="retry">retry one fresh scan</button>');
      return;
    }
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
    const choiceConflicts = conflictingChoiceFills(plan.fills, snapshot.fields);
    if (choiceConflicts.length) {
      const reason = "The agent returned conflicting choices for one question, so no choice was clicked. The page is paused safely.";
      await say({type: "JOB_RADAR_EVENT", state: "blocked", message: reason, error: reason});
      showBanner("Application paused · conflicting choices", reason, "warn", '<button data-action="retry">retry after refresh</button>');
      return;
    }
    const fileFills = (plan.fills || []).filter(fill => fill.file);
    const fillsThisPass = fileFills.length ? fileFills : (plan.fills || []);
    const failedFiles = [];
    const deferredFills = [];
    let uploadedResume = false;
    let appliedFills = 0;
    for (const fill of fillsThisPass) {
      const element = findField(fill.field_id);
      if (!element) {
        if (fill.file) failedFiles.push(fill); else deferredFills.push(fill);
        continue;
      }
      const filled = await chooseOption(element, fill.value, fill.options, fill.file);
      if (fill.file && !filled) failedFiles.push(fill);
      if (fill.file && filled) uploadedResume = true;
      if (!fill.file && filled) appliedFills += 1;
      if (!fill.file && !filled) deferredFills.push(fill);
    }
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
    if (deferredFills.length) {
      showBanner("Agent continuing after the form changed", `${deferredFills.length} field${deferredFills.length === 1 ? "" : "s"} moved while the page was updating. Rescanning the live form now.`, "info");
      lastFingerprint = "";
      scheduleScan(650);
      return;
    }
    if (plan.resume_pending) {
      showBanner("Simplify finished · resume still preparing", text(plan.message || "Resume Studio is preparing the selected PDF. The form will continue automatically when it is ready.", 700), "info");
      lastFingerprint = "";
      scheduleScan(1200);
      return;
    }
    if (appliedFills) {
      await say({type: "JOB_RADAR_EVENT", state: "filling", message: `Applied ${appliedFills} approved field${appliedFills === 1 ? "" : "s"}; verifying the live page before continuing.`});
      showBanner("Agent verifying this page", `${appliedFills} approved field${appliedFills === 1 ? "" : "s"} applied. The agent is checking that the employer page kept each value before moving on.`, "info");
      lastFingerprint = "";
      scheduleScan(650);
      return;
    }
    const blockers = plan.blockers || [];
    if (blockers.length) {
      const items = blockers.map(item => `<li><strong>${text(item.label, 200)}</strong><br>${text(item.reason, 300)}</li>`).join("");
      showBanner("Action needed · not a system error", `<div style="margin-bottom:6px">The agent filled what was already approved, then stopped safely for owner-only information.</div><ul style="margin:5px 0 0 18px;padding:0">${items}</ul>`, "warn", '<button data-action="popup">open answer panel</button><button data-action="retry">retry after answering</button>');
      return;
    }
    if (plan.state === "submitting" && !plan.review) {
      await submitFinalControl(snapshot.fields);
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
      await submitFinalControl(extract().fields);
    })();
  });
  const observer = new MutationObserver(() => scheduleScan(500));
  observer.observe(document.documentElement, {childList: true, subtree: true});
  window.addEventListener("popstate", () => scheduleScan(0));
  window.addEventListener("hashchange", () => scheduleScan(0));
  scheduleScan(800);
})();
