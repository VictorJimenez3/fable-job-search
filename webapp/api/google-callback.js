const { envv, seal, unseal, session, CANON_HOST,
  GOOGLE_AUTH_CLIENT_ID, GOOGLE_AUTH_CLIENT_SECRET, googleAuthConfigured,
  needSessionSetup } = require("./_lib");
const tracker = require("./_google-tracker");

function writeSession(res, payload) {
  const cookie = seal(payload);
  res.setHeader("Set-Cookie", [
    `jr_s=${cookie}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${30 * 86400}`,
    "jr_go=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0",
  ]);
}

module.exports = async (req, res) => {
  if (needSessionSetup(res)) return;
  if (!googleAuthConfigured()) { res.status(503).send("Google login is not configured."); return; }
  const { code, state } = req.query;
  const cookieState = (/(?:^|;\s*)jr_go=([^;]+)/.exec(req.headers.cookie || "") || [])[1];
  const opened = state && state === cookieState ? unseal(state) : null;
  if (!code || !opened || Date.now() - opened.t > 10 * 60 * 1000 || !opened.verifier) {
    res.status(400).send("Google OAuth state mismatch or expired — go back and sign in again.");
    return;
  }
  const current = session(req);
  if (opened.mode === "link" && !current) {
    res.status(401).send("The original account session expired. Sign in again, then connect accounts.");
    return;
  }
  try {
    const redirect = `https://${CANON_HOST}/api/google-callback`;
    const tokenResponse = await fetch("https://oauth2.googleapis.com/token", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({client_id: GOOGLE_AUTH_CLIENT_ID(), client_secret: GOOGLE_AUTH_CLIENT_SECRET(),
        code, code_verifier: opened.verifier, redirect_uri: redirect, grant_type: "authorization_code"}),
    });
    const tokenBody = await tokenResponse.json();
    if (!tokenResponse.ok || !tokenBody.access_token) { res.status(400).send("Google did not return a usable token."); return; }
    const identityResponse = await fetch("https://openidconnect.googleapis.com/v1/userinfo", {
      headers: { Authorization: `Bearer ${tokenBody.access_token}` },
    });
    if (!identityResponse.ok) { res.status(502).send("Google identity lookup failed."); return; }
    const identity = await identityResponse.json();
    if (!identity.sub || !identity.email || identity.email_verified !== true) {
      res.status(403).send("Google did not provide a verified email identity."); return;
    }
    const googleIdentity = {sub: identity.sub, email: identity.email};
    const linked = await tracker.resolveAccount("google", googleIdentity, current, opened.mode, {
      accessToken: tokenBody.access_token, refreshToken: tokenBody.refresh_token,
    });
    const github = linked.github || current?.github;
    const google = linked.google || googleIdentity;
    writeSession(res, {g: current?.g, u: github?.login || google.email, k: linked.account_id,
      keys: linked.keys, github, google});
    res.writeHead(302, { Location: "/" });
    res.end();
  } catch (error) {
    res.status(502).send(`Account sign-in failed: ${String(error.message || error).slice(0, 180)}`);
  }
};
