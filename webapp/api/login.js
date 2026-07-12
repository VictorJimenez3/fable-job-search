const { envv, seal, needSetup } = require("./_lib");

// The app answers on several vercel.app aliases, but a GitHub OAuth app
// accepts exactly one callback URL — so login always runs on the canonical
// host (hop there first if needed; cookies are host-scoped).
const CANON = require("./_lib").envv("CANON_HOST") || "job-radar-vmj-8946s-projects.vercel.app";

module.exports = (req, res) => {
  if (needSetup(res)) return;
  if (req.headers.host !== CANON) {
    res.writeHead(302, { Location: `https://${CANON}/api/login` });
    res.end();
    return;
  }
  const id = envv("GH_CLIENT_ID");
  if (!/^[A-Za-z0-9._-]{10,}$/.test(id)) {
    res.status(500).json({ error:
      `GH_CLIENT_ID looks malformed after sanitizing (length ${id.length}). ` +
      "Re-paste just the Client ID value from the GitHub OAuth app page, then redeploy." });
    return;
  }
  const state = seal({ t: Date.now() });
  const redirect = `https://${CANON}/api/callback`;
  res.setHeader("Set-Cookie",
    `jr_o=${state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`);
  res.writeHead(302, {
    Location: "https://github.com/login/oauth/authorize" +
      `?client_id=${id}` +
      `&redirect_uri=${encodeURIComponent(redirect)}` +
      `&scope=public_repo&state=${state}`,
  });
  res.end();
};
