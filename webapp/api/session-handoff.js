// Keep the memorable Vercel alias and the OAuth callback host signed in
// together without putting provider tokens in localStorage, the DOM, or a URL.
// The canonical host issues an opaque, encrypted ticket to an allowlisted
// alias; that alias immediately exchanges it for its own httpOnly cookie.
const {
  CANON_HOST, requestHost, authReturnHost, session,
  seal, unseal, sessionCookies,
} = require("./_lib");

const MAX_AGE_MS = 60 * 1000;

function noStore(res) {
  res.setHeader("Cache-Control", "no-store, private");
  res.setHeader("Vary", "Origin");
}

function allowOrigin(res, origin) {
  const target = authReturnHost(origin);
  if (!target) return "";
  res.setHeader("Access-Control-Allow-Origin", `https://${target}`);
  res.setHeader("Access-Control-Allow-Credentials", "true");
  return target;
}

module.exports = (req, res) => {
  noStore(res);

  if (req.method === "OPTIONS") {
    if (!allowOrigin(res, req.headers.origin || "")) { res.status(403).end(); return; }
    res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
    res.status(204).end();
    return;
  }

  if (req.method === "GET") {
    if (requestHost(req) !== CANON_HOST) { res.status(404).end(); return; }
    const target = allowOrigin(res, req.headers.origin || "");
    if (!target) { res.status(403).json({error: "unsupported auth handoff target"}); return; }
    const current = session(req);
    if (!current) { res.status(401).json({error: "not signed in"}); return; }
    const ticket = seal({kind: "job-radar-session-handoff", t: Date.now(), target, session: current});
    res.status(200).json({ticket, expires_in: MAX_AGE_MS / 1000});
    return;
  }

  if (req.method === "POST") {
    const target = authReturnHost(requestHost(req));
    const opened = unseal(req.body?.ticket || "");
    if (!target || !opened || opened.kind !== "job-radar-session-handoff" ||
        opened.target !== target || !opened.session ||
        !Number.isFinite(opened.t) || Date.now() - opened.t > MAX_AGE_MS) {
      res.status(400).json({error: "expired or invalid auth handoff"});
      return;
    }
    res.setHeader("Set-Cookie", sessionCookies(opened.session));
    res.status(200).json({ok: true});
    return;
  }

  res.status(405).end();
};
