// PUT: commit the workspace state (notes, outreach links, maybe-lane) to
// state/web_state.json using the owner's session. The file lives in a public
// repo — the frontend says so next to the save switch.
const { OWNER, REPO, BRANCH, session, gh } = require("./_lib");

module.exports = async (req, res) => {
  if (req.method !== "PUT") { res.status(405).end(); return; }
  const s = session(req);
  if (!s) { res.status(401).json({ error: "sign in first" }); return; }
  if (s.u !== OWNER) { res.status(403).json({ error: "owner only" }); return; }
  const path = `/repos/${REPO}/contents/state/web_state.json`;
  let sha;
  const cur = await gh(`${path}?ref=${BRANCH}`, s.g);
  if (cur.ok) sha = (await cur.json()).sha;
  const content = Buffer.from(JSON.stringify(req.body || {}, null, 1)).toString("base64");
  const r = await gh(path, s.g, {
    method: "PUT",
    body: JSON.stringify({ message: "platform: workspace update", content, sha, branch: BRANCH }),
  });
  if (r.ok) res.status(200).json({ ok: true });
  else res.status(r.status).json({ error: await r.text() });
};
