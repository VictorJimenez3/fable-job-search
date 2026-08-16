const crypto = require("crypto");
const {normalizeProfile} = require("../_lib");
const {owner} = require("./_auth");
const {configured, database, serverError} = require("./_db");

function bodyOf(req) {
  if (req.body && typeof req.body === "object") return req.body;
  try { return JSON.parse(String(req.body || "{}")); } catch { return {}; }
}

module.exports = async (req, res) => {
  try {
    if (!owner(req, res, {mutation: req.method !== "GET"})) return;
    if (!configured()) { res.status(503).json({error: "Postgres is not configured"}); return; }
    const profile = normalizeProfile(String(req.query?.profile || bodyOf(req).profile || "new_grad").slice(0, 40));
    if (!["new_grad", "internship"].includes(profile)) {
      res.status(400).json({error: "unsupported profile"});
      return;
    }
    if (req.method === "GET") {
      const result = await database().query(`
        select payload, created_at from preferences
        where profile_id = $1 and kind = 'user_preferences'
        order by created_at desc limit 1`, [profile]);
      res.status(200).json({data: result.rows[0] || {payload: {}, created_at: null}});
      return;
    }
    if (req.method !== "PUT" && req.method !== "POST") { res.status(405).end(); return; }
    const payload = bodyOf(req).preferences;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      res.status(400).json({error: "preferences object required"});
      return;
    }
    const serialized = JSON.stringify(payload);
    if (Buffer.byteLength(serialized) > 32_000) {
      res.status(413).json({error: "preferences payload is too large"});
      return;
    }
    const hash = crypto.createHash("sha256").update(`${profile}:${serialized}`).digest("hex");
    await database().query(`
      insert into preferences (id, profile_id, kind, payload, idempotency_key, created_at)
      values ($1, $2, 'user_preferences', $3, $4, now())
      on conflict (idempotency_key) do nothing`, [crypto.randomUUID(), profile, serialized, hash]);
    res.status(200).json({data: {payload, saved: true}});
  } catch (error) {
    serverError(res, error);
  }
};
