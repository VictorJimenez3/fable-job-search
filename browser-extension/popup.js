async function send(type, value = {}) {
  return chrome.runtime.sendMessage({type, ...value});
}
const version = document.querySelector("#version");
if (version) version.textContent = `v${chrome.runtime.getManifest().version}`;
function esc(value) { return String(value || "").replace(/[&<>\"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char])); }
function formatSession(session) {
  if (!session) return `<div class="card muted">Open an application from Job Radar to attach the agent.</div>`;
  const blockerItems = session.blockers || [];
  const fields = (session.review?.fields || []).map(field => `<div><strong>${esc(field.label)}</strong><br><span class="muted">${esc(field.category)}${field.sensitive ? " · sensitive" : ""}</span><pre>${esc(field.value || "(empty / owner must review)")}</pre></div>`).join("");
  const review = session.review ? `<h2>Final review</h2><div class="card">${fields || "No proposed fields"}<button id="confirm" class="primary">confirm and allow Submit</button></div>` : "";
  const answers = blockerItems.map((item, index) => {
    const written = ["essay", "cover_letter"].includes(String(item.category || ""));
    return `<div class="card"><strong>${esc(item.label || item.question || item.category)}</strong><p class="muted">${esc(item.reason || "The answer is not in your bank yet.")}</p><label>${written ? "Facts or bullets for the writing agent" : "Your approved answer"}<textarea data-answer-index="${index}"></textarea></label><button data-save-answer="${index}">${written ? "save context and write" : "save answer and retry"}</button></div>`;
  }).join("");
  const answer = answers ? `<h2>Needs your input</h2>${answers}` : "";
  return `<div class="card"><strong>${esc(session.job?.company)} · ${esc(session.job?.title)}</strong><br><span class="muted">${esc(session.provider)} · ${esc(session.state)} · ${session.pages_seen || 0} page(s)</span></div>${answer}${review}`;
}
async function render() {
  const settings = await send("JOB_RADAR_GET_CONFIG");
  document.querySelector("#cloudUrl").value = settings.cloudUrl || "";
  document.querySelector("#agentToken").value = settings.agentToken || "";
  const data = await send("JOB_RADAR_GET_ACTIVE");
  document.querySelector("#session").innerHTML = formatSession(data?.session);
  document.querySelector("#status").textContent = data?.session ? "Local agent connected" : "Waiting for an application tab";
  const confirm = document.querySelector("#confirm");
  if (confirm) confirm.onclick = async () => {
    confirm.disabled = true;
    const session = (await send("JOB_RADAR_GET_ACTIVE"))?.session;
    const result = await send("JOB_RADAR_CONFIRM_LOCAL", {review: {review_hash: session?.review?.review_hash, nonce: session?.review?.nonce, page_fingerprint: session?.review?.page_fingerprint}});
    document.querySelector("#status").textContent = result?.error || "Confirmed; the matching page may submit.";
    setTimeout(render, 400);
  };
  document.querySelectorAll("[data-save-answer]").forEach(button => button.onclick = async () => {
    const index = Number(button.dataset.saveAnswer);
    const blocker = data?.session?.blockers?.[index];
    const value = document.querySelector(`[data-answer-index="${index}"]`)?.value.trim();
    if (!blocker || !value) { document.querySelector("#status").textContent = "Add the requested information first."; return; }
    button.disabled = true;
    const result = await send("JOB_RADAR_SAVE_ANSWER", {answer: {
      question: blocker.question || blocker.label || blocker.category,
      value, category: blocker.category || "other",
    }});
    document.querySelector("#status").textContent = result?.error || (["essay", "cover_letter"].includes(blocker.category) ? "Context saved; writing a fresh response." : "Answer saved; retrying the form.");
    await send("JOB_RADAR_RESCAN");
    setTimeout(render, 400);
  });
}
document.querySelector("#saveConfig").onclick = async () => {
  const result = await send("JOB_RADAR_SET_CONFIG", {config: {cloudUrl: document.querySelector("#cloudUrl").value, agentToken: document.querySelector("#agentToken").value}});
  document.querySelector("#status").textContent = result?.error || "Pairing saved.";
};
document.querySelector("#sync").onclick = async () => {
  const button = document.querySelector("#sync");
  button.disabled = true;
  document.querySelector("#status").textContent = "Syncing private queue…";
  const result = await send("JOB_RADAR_SYNC_NOW");
  if (result?.error) document.querySelector("#status").textContent = `Queue sync failed: ${result.error}`;
  else if (result?.ok) document.querySelector("#status").textContent =
    `Queue synced · ${result.queued || 0} waiting · ${result.active || 0} active${result.recovered ? ` · ${result.recovered} recovered` : ""}`;
  else document.querySelector("#status").textContent = "Queue sync did not complete.";
  button.disabled = false;
  setTimeout(render, 400);
};
document.querySelector("#reload").onclick = async () => {
  const button = document.querySelector("#reload");
  button.disabled = true;
  document.querySelector("#status").textContent = "Reloading extension…";
  const result = await send("JOB_RADAR_RELOAD_EXTENSION");
  if (result?.error) {
    document.querySelector("#status").textContent = `Extension reload failed: ${result.error}`;
    button.disabled = false;
    return;
  }
  document.querySelector("#status").textContent = "Reload requested; reopen the popup after it restarts.";
};
void render();
