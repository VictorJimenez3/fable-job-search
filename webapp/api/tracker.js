// GET/POST personal tracker rows for any authenticated account.
// This is separate from the owner-only repository state/actions.
const { session } = require("./_lib");
const tracker = require("./_google-tracker");

module.exports = async (req, res) => {
  const s = session(req);
  if (!s) { res.status(401).json({ error: "sign in first" }); return; }
  try {
    if (req.method === "GET") {
      res.status(200).json({ user: s.u, ...(await tracker.userTracker(s.keys || s.k || s.u, s.pt)) });
      return;
    }
    if (req.method === "POST") {
      const payload = req.body || {};
      const result = await tracker.updateUserTracker(s.u, s.keys || s.k || s.u, payload, s.pt);
      res.status(200).json(result);
      return;
    }
    res.status(405).end();
  } catch (error) {
    res.status(502).json({ error: String(error.message || error).slice(0, 180) });
  }
};
