const { OWNER, AUTH_MODE, session } = require("./_lib");

module.exports = (req, res) => {
  if (AUTH_MODE === "tokenless") { res.status(404).end(); return; }
  const s = session(req);
  if (!s) { res.status(401).json({ error: "not signed in" }); return; }
  res.status(200).json({ login: s.u, owner: s.u === OWNER });
};
