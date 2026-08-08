const { envv, unseal, needSetup, session, authReturnHost, sessionCookies } = require("./_lib");
const tracker = require("./_google-tracker");

function writeSession(res, payload) {
  res.setHeader("Set-Cookie", sessionCookies(payload));
}
function callbackLocation(opened) {
  const host = authReturnHost(opened?.return_host);
  return host ? `https://${host}/?auth=connected` : "/?auth=connected";
}

module.exports = async (req, res) => {
  if (needSetup(res)) return;
  const { code, state } = req.query;
  const cookieState = (/(?:^|;\s*)jr_o=([^;]+)/.exec(req.headers.cookie || "") || [])[1];
  const opened = state && state === cookieState ? unseal(state) : null;
  if (!code || !opened || Date.now() - opened.t > 10 * 60 * 1000) {
    res.status(400).send("OAuth state mismatch or expired — go back and sign in again.");
    return;
  }
  const current = session(req);
  if (opened.mode === "link" && !current) {
    res.status(401).send("The original account session expired. Sign in again, then connect accounts.");
    return;
  }
  try {
    const tr = await fetch("https://github.com/login/oauth/access_token", {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({client_id: envv("GH_CLIENT_ID"), client_secret: envv("GH_CLIENT_SECRET"), code}),
    });
    const tokenBody = await tr.json();
    const tok = tokenBody.access_token;
    if (!tr.ok || !tok) { res.status(400).send("GitHub did not return a usable token."); return; }
    const ur = await fetch("https://api.github.com/user", {
      headers: { Authorization: `Bearer ${tok}`, Accept: "application/vnd.github+json", "User-Agent": "job-radar-platform" },
    });
    if (!ur.ok) { res.status(502).send("GitHub identity lookup failed."); return; }
    const user = await ur.json();
    if (!user.id || !user.login) { res.status(502).send("GitHub returned an incomplete identity."); return; }
    const linked = await tracker.resolveAccount("github", {id: user.id, login: user.login}, current, opened.mode);
    const github = linked.github || {id: String(user.id), login: user.login};
    const google = linked.google || current?.google;
    writeSession(res, {g: tok, u: github.login || google?.email, k: linked.account_id, keys: linked.keys,
      github, google, pt: linked.personal_tracker || current?.pt});
    res.writeHead(302, { Location: callbackLocation(opened) });
    res.end();
  } catch (error) {
    res.status(502).send(`Account sign-in failed: ${String(error.message || error).slice(0, 180)}`);
  }
};
