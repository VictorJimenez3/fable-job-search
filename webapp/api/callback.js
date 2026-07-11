const { seal, unseal, needSetup } = require("./_lib");

module.exports = async (req, res) => {
  if (needSetup(res)) return;
  const { code, state } = req.query;
  const cookieState = (/(?:^|;\s*)jr_o=([^;]+)/.exec(req.headers.cookie || "") || [])[1];
  const opened = state && state === cookieState ? unseal(state) : null;
  if (!code || !opened || Date.now() - opened.t > 10 * 60 * 1000) {
    res.status(400).send("OAuth state mismatch or expired — go back and sign in again.");
    return;
  }
  const tr = await fetch("https://github.com/login/oauth/access_token", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      client_id: process.env.GH_CLIENT_ID,
      client_secret: process.env.GH_CLIENT_SECRET,
      code,
    }),
  });
  const tok = (await tr.json()).access_token;
  if (!tok) { res.status(400).send("GitHub did not return a token."); return; }
  const ur = await fetch("https://api.github.com/user", {
    headers: { Authorization: `Bearer ${tok}`, "User-Agent": "job-radar-platform" },
  });
  const user = await ur.json();
  const cookie = seal({ g: tok, u: user.login });
  res.setHeader("Set-Cookie", [
    `jr_s=${cookie}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${30 * 86400}`,
    "jr_o=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0",
  ]);
  res.writeHead(302, { Location: "/" });
  res.end();
};
