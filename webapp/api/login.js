const crypto = require("crypto");
const { envv, seal, needSetup, needSessionSetup, session, CANON_HOST,
  requestHost, authReturnHost, sessionHandoffLocation,
  authLog,
  GOOGLE_AUTH_CLIENT_ID, googleAuthConfigured } = require("./_lib");

function b64url(buffer) { return buffer.toString("base64url"); }
function pkce() {
  const verifier = b64url(crypto.randomBytes(32));
  const challenge = b64url(crypto.createHash("sha256").update(verifier).digest());
  return {verifier, challenge};
}
function modeOf(req) { return req.query?.mode === "link" ? "link" : "login"; }

// The app answers on several Vercel aliases, but each OAuth provider accepts
// one callback URL. Login still begins on the canonical host; return_host
// lets the callback send the browser back to the URL the user chose.
module.exports = (req, res) => {
  if (needSessionSetup(res)) return;
  const provider = String(req.query?.provider || "github").toLowerCase();
  const mode = modeOf(req);
  const currentHost = requestHost(req);
  const returnHost = currentHost === CANON_HOST
    ? authReturnHost(req.query?.return_host)
    : authReturnHost(currentHost);
  const current = currentHost === CANON_HOST ? session(req) : null;
  authLog("login", {host: currentHost, provider, mode, returnHost: returnHost || "",
    hasSession: Boolean(current), redirectedToCanonical: currentHost !== CANON_HOST});
  if (currentHost !== CANON_HOST) {
    const forwarded = new URLSearchParams({provider});
    if (mode === "link") forwarded.set("mode", "link");
    if (returnHost) forwarded.set("return_host", returnHost);
    const suffix = `/api/login?${forwarded.toString()}`;
    res.writeHead(302, { Location: `https://${CANON_HOST}${suffix}` });
    res.end();
    return;
  }
  if (mode === "login" && current) {
    const destination = returnHost
      ? sessionHandoffLocation(returnHost, current, "already-signed-in")
      : "/?auth=already-signed-in";
    res.writeHead(302, {Location: destination});
    res.end();
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
    const state = seal({ t: Date.now(), mode, verifier, return_host: returnHost });
    const redirect = `https://${CANON_HOST}/api/google-callback`;
    res.setHeader("Set-Cookie",
      `jr_go=${state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`);
    const params = new URLSearchParams({client_id: GOOGLE_AUTH_CLIENT_ID(),
      redirect_uri: redirect, response_type: "code",
      // Google identity and the user's per-file Sheets access are one explicit
      // consent flow. drive.file is the least-privilege scope: the backend
      // creates and maintains only this app's tracker workbook, rather than
      // requesting access to every spreadsheet in the user's Drive.
      scope: "openid email profile https://www.googleapis.com/auth/drive.file",
      access_type: "offline", include_granted_scopes: "true", state,
      code_challenge: challenge, code_challenge_method: "S256",
      prompt: "consent select_account"});
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
  const state = seal({ t: Date.now(), mode, return_host: returnHost });
  const redirect = `https://${CANON_HOST}/api/callback`;
  res.setHeader("Set-Cookie",
    `jr_o=${state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`);
  const params = new URLSearchParams({client_id: id, redirect_uri: redirect,
    scope: "public_repo", state});
  res.writeHead(302, { Location: `https://github.com/login/oauth/authorize?${params}` });
  res.end();
};
