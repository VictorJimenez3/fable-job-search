// Small, deterministic ATS adapter hints.  The form decision engine remains
// provider-agnostic; adapters only improve labels, stable field keys, and
// navigation detection for the initial production coverage set.
(function () {
  "use strict";
  const hosts = {
    workday: [/workday/i, /myworkdayjobs\.com$/i, /myworkdaysite\.com$/i],
    greenhouse: [/greenhouse\.io$/i, /greenhouse\.com$/i],
    lever: [/jobs\.lever\.co$/i, /lever\.co$/i],
    ashby: [/ashbyhq\.com$/i],
    smartrecruiters: [/smartrecruiters\.com$/i],
  };
  const hints = {
    workday: {automation: "[data-automation-id]", forms: "form, [role='form']"},
    greenhouse: {automation: "[data-qa], [data-automation-id]", forms: "form, #application_form"},
    lever: {automation: "[data-qa], [data-testid]", forms: "form, #application-form"},
    ashby: {automation: "[data-testid], [data-qa]", forms: "form, [role='form']"},
    smartrecruiters: {automation: "[data-test], [data-testid]", forms: "form, [role='form']"},
    generic: {automation: "[data-automation-id], [data-testid], [data-qa], [data-test]", forms: "form, [role='form']"},
  };
  function providerForURL(value) {
    let host = "";
    try { host = new URL(String(value || "")).hostname; } catch (_) {}
    return Object.entries(hosts).find(([, patterns]) => patterns.some(pattern => pattern.test(host)))?.[0] || "generic";
  }
  function adapter(value) { return hints[providerForURL(value)] || hints.generic; }
  function fieldKey(element) {
    return element.getAttribute("data-automation-id") || element.getAttribute("data-testid") || element.getAttribute("data-qa") || element.getAttribute("data-test") || element.id || element.getAttribute("name") || "";
  }
  function labelFor(element) {
    return element.getAttribute("data-automation-label") || element.getAttribute("data-label") || "";
  }
  function isSubmitLabel(value) { return /apply|submit|finish|complete/i.test(String(value || "")); }
  function isNextLabel(value) { return /next|continue|review|save/i.test(String(value || "")); }
  window.JobRadarATS = {providerForURL, adapter, fieldKey, labelFor, isSubmitLabel, isNextLabel};
})();
