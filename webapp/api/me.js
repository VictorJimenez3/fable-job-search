const { OWNER, AUTH_MODE, session } = require("./_lib");

module.exports = (req, res) => {
  // A second profile can intentionally run as a static/tokenless board on
  // Vercel until it gets its own GitHub OAuth app. Returning 404 makes the
  // frontend use prefilled owner-only GitHub command issues (or an optional
  // fine-grained PAT) instead of advertising a broken sign-in button.
  if (AUTH_MODE === "tokenless") { res.status(404).end(); return; }
  const s = session(req);
  if (!s) { res.status(401).json({ error: "not signed in" }); return; }
  res.status(200).json({ login: s.u, owner: s.u === OWNER });
};
