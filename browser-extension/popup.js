async function send(type, value = {}) {
  return chrome.runtime.sendMessage({type, ...value});
}
function esc(value) { return String(value || "").replace(/[&<>\"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char])); }
function formatSession(session) {
  if (!session) return `<div class="card muted">Open an application from Job Radar to attach the agent.</div>`;
  const blockers = (session.blockers || []).map(item => `<li><strong>${esc(item.label)}</strong><br>${esc(item.reason)}</li>`).join("");
  const fields = (session.review?.fields || []).map(field => `<div><strong>${esc(field.label)}</strong><br><span class="muted">${esc(field.category)}${field.sensitive ? " · sensitive" : ""}</span><pre>${esc(field.value || "(empty / owner must review)")}</pre></div>`).join("");
  const review = session.review ? `<h2>Final review</h2><div class="card">${fields || "No proposed fields"}<button id="confirm" class="primary">confirm and allow Submit</button></div>` : "";
  const answer = blockers ? `<h2>Needs your answer</h2><div class="card"><ul>${blockers}</ul><label>Answer the selected question<textarea id="answer"></textarea></label><label>Question / field key<input id="question" placeholder="copy the field label above"></label><button id="saveAnswer">save answer and retry</button></div>` : "";
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
  const saveAnswer = document.querySelector("#saveAnswer");
  if (saveAnswer) saveAnswer.onclick = async () => {
    saveAnswer.disabled = true;
    const result = await send("JOB_RADAR_SAVE_ANSWER", {answer: {question: document.querySelector("#question").value, value: document.querySelector("#answer").value}});
    document.querySelector("#status").textContent = result?.error || "Answer saved; retrying the form.";
    await send("JOB_RADAR_RESCAN");
    setTimeout(render, 400);
  };
}
document.querySelector("#saveConfig").onclick = async () => {
  const result = await send("JOB_RADAR_SET_CONFIG", {config: {cloudUrl: document.querySelector("#cloudUrl").value, agentToken: document.querySelector("#agentToken").value}});
  document.querySelector("#status").textContent = result?.error || "Pairing saved.";
};
document.querySelector("#sync").onclick = async () => { await send("JOB_RADAR_SYNC_NOW"); document.querySelector("#status").textContent = "Queue sync requested."; setTimeout(render, 400); };
void render();
