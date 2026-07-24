// POST {action: "track"|"applied"|"untrack"|"manual-add"|"research-company", ...}
// → repository_dispatch.
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
  const { action, id, ids, url, company, title, location } = req.body || {};
  const manual = action === "manual-add";
  const research = action === "research-company";
  if (!["track", "applied", "untrack", "manual-add", "research-company"].includes(action)
      || (!manual && !id) || (manual && (!company || !title || !url))) {
    res.status(400).json({ error: "bad payload" });
    return;
  }
  if (manual) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" && parsed.protocol !== "http:") throw new Error("protocol");
    } catch (_) {
      res.status(400).json({ error: "manual role needs a valid http(s) posting URL" });
      return;
    }
  }
  const researchIds = research && Array.isArray(ids)
    ? [...new Set(ids.filter((value) => typeof value === "string" && value.length > 0))].slice(0, 5)
    : [];
  const r = await gh(`/repos/${REPO}/dispatches`, s.g, {
    method: "POST",
    body: JSON.stringify({
      event_type: "radar-web",
      client_payload: { action, id, ids: researchIds, url, company, title, location, profile: PROFILE },
    }),
  });
  if (r.status === 204) res.status(202).json({ ok: true });
  else res.status(r.status).json({ error: await r.text() });
};
