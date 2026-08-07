const crypto = require("crypto");
const { envv, seal, needSetup, needSessionSetup, session, CANON_HOST,
  GOOGLE_AUTH_CLIENT_ID, googleAuthConfigured } = require("./_lib");

function b64url(buffer) { return buffer.toString("base64url"); }
function pkce() {
  const verifier = b64url(crypto.randomBytes(32));
  const challenge = b64url(crypto.createHash("sha256").update(verifier).digest());
  return {verifier, challenge};
}
function modeOf(req) { return req.query?.mode === "link" ? "link" : "login"; }

// The app answers on several Vercel aliases, but each OAuth provider accepts
// one callback URL. Login always begins on the canonical host.
module.exports = (req, res) => {
  if (needSessionSetup(res)) return;
  if (req.headers.host !== CANON_HOST) {
    const suffix = req.url || "/api/login";
    res.writeHead(302, { Location: `https://${CANON_HOST}${suffix}` });
    res.end();
    return;
  }
  const provider = String(req.query?.provider || "github").toLowerCase();
  const mode = modeOf(req);
  if (mode === "login" && session(req)) {
    res.status(409).send("Already signed in. Open Account center to connect another provider.");
    return;
  }
  if (mode === "link" && !session(req)) {
    res.status(401).send("Sign in first, then choose Connect account.");
    return;
  }
  if (provider === "google") {
    if (!googleAuthConfigured()) {
      res.status(503).send("Google login is not configured on this platform yet.");
      return;
    }
    const {verifier, challenge} = pkce();
    const state = seal({ t: Date.now(), mode, verifier });
    const redirect = `https://${CANON_HOST}/api/google-callback`;
    res.setHeader("Set-Cookie",
      `jr_go=${state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`);
    const params = new URLSearchParams({client_id: GOOGLE_AUTH_CLIENT_ID(),
      redirect_uri: redirect, response_type: "code", scope: "openid email profile",
      state, code_challenge: challenge, code_challenge_method: "S256",
      prompt: mode === "link" ? "consent select_account" : "select_account"});
    res.writeHead(302, {Location: `https://accounts.google.com/o/oauth2/v2/auth?${params}`});
    res.end();
    return;
  }
  if (provider !== "github") { res.status(400).send("Unsupported login provider."); return; }
  if (needSetup(res)) return;
  const id = envv("GH_CLIENT_ID");
  if (!/^[A-Za-z0-9._-]{10,}$/.test(id)) {
    res.status(500).json({ error:
      `GH_CLIENT_ID looks malformed after sanitizing (length ${id.length}). ` +
      "Re-paste just the Client ID value from the GitHub OAuth app page, then redeploy." });
    return;
  }
  const state = seal({ t: Date.now(), mode });
  const redirect = `https://${CANON_HOST}/api/callback`;
  res.setHeader("Set-Cookie",
    `jr_o=${state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`);
  const params = new URLSearchParams({client_id: id, redirect_uri: redirect,
    scope: "public_repo", state});
  res.writeHead(302, { Location: `https://github.com/login/oauth/authorize?${params}` });
  res.end();
};
