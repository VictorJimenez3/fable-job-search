const { envv, seal, needSetup } = require("./_lib");

module.exports = (req, res) => {
  if (needSetup(res)) return;
  const id = envv("GH_CLIENT_ID");
  if (!/^[A-Za-z0-9._-]{10,}$/.test(id)) {
    res.status(500).json({ error:
      `GH_CLIENT_ID looks malformed after sanitizing (length ${id.length}). ` +
      "Re-paste just the Client ID value from the GitHub OAuth app page, then redeploy." });
    return;
  }
  const state = seal({ t: Date.now() });
  const redirect = `https://${req.headers.host}/api/callback`;
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
