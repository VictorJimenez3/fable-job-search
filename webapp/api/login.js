const { envv, seal, needSetup } = require("./_lib");

module.exports = (req, res) => {
  if (needSetup(res)) return;
  const state = seal({ t: Date.now() });
  const redirect = `https://${req.headers.host}/api/callback`;
  res.setHeader("Set-Cookie",
    `jr_o=${state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`);
  res.writeHead(302, {
    Location: "https://github.com/login/oauth/authorize" +
      `?client_id=${envv("GH_CLIENT_ID")}` +
      `&redirect_uri=${encodeURIComponent(redirect)}` +
      `&scope=public_repo&state=${state}`,
  });
  res.end();
};
