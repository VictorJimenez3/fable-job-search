// Shared session + config for the platform's Vercel backend.
// Secrets live only in Vercel env vars. Provider tokens never enter frontend
// JavaScript: provider credentials and per-user tracker metadata are sealed
// into an httpOnly AES-256-GCM cookie. The private account registry is an
// optional durable identity-linking layer. Repository writes remain owner-only.
const crypto = require("crypto");

// env values arrive via dashboard paste — strip anything non-printable
// (newlines, zero-width unicode from rich-text copies, spaces)
const envv = (k) => (process.env[k] || "").replace(/[^\x21-\x7E]/g, "");

// Forks self-hosting this backend set RADAR_OWNER / RADAR_REPO /
// RADAR_BRANCH in their Vercel env; these defaults are Victor's instance.
const OWNER = envv("RADAR_OWNER") || "VictorJimenez3";
const REPO = envv("RADAR_REPO") || "VictorJimenez3/fable-job-search";
const BRANCH = envv("RADAR_BRANCH") || "claude/newgrad-job-search-system-9gbj9k";
const PROFILE = envv("RADAR_PROFILE") || "default";
const AUTH_MODE = envv("AUTH_MODE") || "oauth";
const CANON_HOST = envv("CANON_HOST") || "job-radar-vmj-8946s-projects.vercel.app";

// Google login uses a separate OAuth web client when available. Falling back
// to the Sheets client keeps one-person deployments simple, but that client
// must have this app's HTTPS callback registered in Google Cloud first.
const GOOGLE_AUTH_CLIENT_ID = () => envv("GOOGLE_AUTH_CLIENT_ID") || envv("GOOGLE_CLIENT_ID");
const GOOGLE_AUTH_CLIENT_SECRET = () => envv("GOOGLE_AUTH_CLIENT_SECRET") || envv("GOOGLE_CLIENT_SECRET");
const googleAuthConfigured = () => Boolean(GOOGLE_AUTH_CLIENT_ID() && GOOGLE_AUTH_CLIENT_SECRET());

const key = () => crypto.createHash("sha256").update(envv("SESSION_SECRET")).digest();

function seal(obj) {
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv("aes-256-gcm", key(), iv);
  const enc = Buffer.concat([c.update(JSON.stringify(obj)), c.final()]);
  return Buffer.concat([iv, c.getAuthTag(), enc]).toString("base64url");
}

function unseal(tok) {
  try {
    const b = Buffer.from(tok, "base64url");
    const d = crypto.createDecipheriv("aes-256-gcm", key(), b.subarray(0, 12));
    d.setAuthTag(b.subarray(12, 28));
    return JSON.parse(Buffer.concat([d.update(b.subarray(28)), d.final()]).toString());
  } catch { return null; }
}

function session(req) {
  const m = /(?:^|;\s*)jr_s=([^;]+)/.exec(req.headers.cookie || "");
  return m ? unseal(m[1]) : null;
}

function needSetup(res) {
  const missing = ["GH_CLIENT_ID", "GH_CLIENT_SECRET", "SESSION_SECRET"]
    .filter((k) => !envv(k));
  if (missing.length) {
    res.status(503).json({ error: "setup needed", missing });
    return true;
  }
  return false;
}

function needSessionSetup(res) {
  const missing = ["SESSION_SECRET"].filter((k) => !envv(k));
  if (missing.length) {
    res.status(503).json({ error: "setup needed", missing });
    return true;
  }
  return false;
}

async function gh(path, token, opts = {}) {
  const r = await fetch("https://api.github.com" + path, {
    ...opts,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "job-radar-platform",
      ...(opts.headers || {}),
    },
  });
  return r;
}

module.exports = { OWNER, REPO, BRANCH, PROFILE, AUTH_MODE, envv, seal, unseal,
                   CANON_HOST, GOOGLE_AUTH_CLIENT_ID, GOOGLE_AUTH_CLIENT_SECRET,
                   googleAuthConfigured, session, needSetup, needSessionSetup, gh };
