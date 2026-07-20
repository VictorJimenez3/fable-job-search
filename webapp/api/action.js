// POST {action: "track"|"applied"|"untrack", id, url, company} → repository_dispatch.
// Auth: the sealed session cookie. Only the repo owner may write; GitHub
// would refuse anyone else anyway (their OAuth token lacks repo write), but
// we reject early for a clear error.
const { OWNER, REPO, PROFILE, session, gh } = require("./_lib");

module.exports = async (req, res) => {
  if (req.method !== "POST") { res.status(405).end(); return; }
  const s = session(req);
  if (!s) { res.status(401).json({ error: "sign in first" }); return; }
  if (s.u !== OWNER) {
    res.status(403).json({ error: `read-only view: this radar belongs to ${OWNER} — fork the repo to run your own (docs/FORKING.md)` });
    return;
  }
  const { action, id, url, company } = req.body || {};
  if (!["track", "applied", "untrack"].includes(action) || !id) {
    res.status(400).json({ error: "bad payload" });
    return;
  }
  const r = await gh(`/repos/${REPO}/dispatches`, s.g, {
    method: "POST",
    body: JSON.stringify({
      event_type: "radar-web",
      client_payload: { action, id, url, company, profile: PROFILE },
    }),
  });
  if (r.status === 204) res.status(202).json({ ok: true });
  else res.status(r.status).json({ error: await r.text() });
};
