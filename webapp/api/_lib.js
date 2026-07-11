// Shared session + config for the platform's Vercel backend.
// Secrets live ONLY in Vercel env vars (GH_CLIENT_ID, GH_CLIENT_SECRET,
// SESSION_SECRET). The user's GitHub OAuth token is sealed into an httpOnly
// AES-256-GCM cookie — frontend JS can never read it, and only the repo
// owner's session is allowed to write.
const crypto = require("crypto");

// env values arrive via dashboard paste — strip stray whitespace/newlines
const envv = (k) => (process.env[k] || "").trim();

const OWNER = "VictorJimenez3";
const REPO = "VictorJimenez3/fable-job-search";
const BRANCH = "claude/newgrad-job-search-system-9gbj9k";

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

module.exports = { OWNER, REPO, BRANCH, envv, seal, unseal, session, needSetup, gh };
