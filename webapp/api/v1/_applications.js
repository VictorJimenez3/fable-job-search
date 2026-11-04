const crypto = require("crypto");
const {httpUrl, normalizeProfile} = require("../_lib");
const {owner} = require("./_auth");
const {configured, database, serverError} = require("./_db");

const STAGES = new Set(["saved", "to_tailor", "applied", "oa", "interview", "offer", "rejected", "withdrawn", "closed", "not_pursuing"]);
const AGGREGATOR_HOSTS = new Set([
  "jobright.ai", "www.jobright.ai", "simplify.jobs", "www.simplify.jobs",
  "indeed.com", "www.indeed.com", "linkedin.com", "www.linkedin.com",
]);

function bodyOf(req) {
  if (req.body && typeof req.body === "object") return req.body;
  try { return JSON.parse(String(req.body || "{}")); } catch { return {}; }
}

function clean(value, max = 500) {
  return String(value || "").trim().slice(0, max);
}

function urlKey(value) {
  const safe = httpUrl(value);
  if (!safe) return "";
  try {
    const parsed = new URL(safe);
    parsed.hash = "";
    return parsed.toString().replace(/\/$/, "");
  } catch { return ""; }
}

function isAggregatorUrl(value) {
  const safe = httpUrl(value);
  if (!safe) return false;
  try {
    const host = new URL(safe).hostname.toLowerCase();
    return AGGREGATOR_HOSTS.has(host)
      || [...AGGREGATOR_HOSTS].some(base => host.endsWith(`.${base}`));
  } catch { return false; }
}

function knownAlternate(row, stored) {
  const links = row.external_links && typeof row.external_links === "object"
    ? row.external_links.alternate_urls : [];
  return Array.isArray(links) && links.some(value => urlKey(value) === urlKey(stored));
}

function applicationUrl(row) {
  const stored = httpUrl(row.url) || "";
  const posting = httpUrl(row.posting_url) || "";
  if (!stored) return posting;
  if (!posting || urlKey(stored) === urlKey(posting)) return stored;
  // A resolver may safely replace only an aggregator/known alternate. A
  // different direct URL is treated as an owner/manual URL and wins.
  if ((isAggregatorUrl(stored) || knownAlternate(row, stored)) && !isAggregatorUrl(posting)) {
    return posting;
  }
  return stored;
}

async function list(req, res) {
  if (!owner(req, res)) return;
  if (!configured()) {
    res.status(503).json({error: "Postgres is not configured; use the classic tracker during migration"});
    return;
  }
  const profile = normalizeProfile(clean(req.query?.profile || "new_grad", 40));
  const result = await database().query(`
    select a.id, a.current_stage as stage, a.company, a.title, a.url,
      p.canonical_url as posting_url,
      a.external_links, a.created_at, a.updated_at, p.public_id as posting_id
    from applications a left join postings p on p.id = a.posting_id
    where a.profile_id = $1 order by a.updated_at desc limit 1000`, [profile]);
  res.status(200).json({data: result.rows.map((row) => ({...row,
    url: applicationUrl(row),
  }))});
}

async function mutate(req, res) {
  if (!owner(req, res, {mutation: true})) return;
  if (!configured()) { res.status(503).json({error: "Postgres is not configured"}); return; }
  const body = bodyOf(req);
  const profile = normalizeProfile(clean(body.profile || "new_grad", 40));
  const postingId = clean(body.posting_id, 200);
  const stage = clean(body.stage || "saved", 32).toLowerCase();
  if (!postingId || !STAGES.has(stage) || !["new_grad", "internship"].includes(profile)) {
    res.status(400).json({error: "valid posting_id, profile, and stage are required"});
    return;
  }
  const idempotency = clean(req.headers["idempotency-key"], 160)
    || crypto.createHash("sha256").update(`${profile}:${postingId}:${stage}:${clean(body.revision, 80)}`).digest("hex");
  const posting = await database().query(`
    select p.id, p.company, p.title, p.canonical_url
    from postings p left join posting_aliases a on a.posting_id = p.id
    where p.profile_id = $1 and (p.public_id = $2 or a.alias = $2) limit 1`, [profile, postingId]);
  if (!posting.rows[0]) { res.status(404).json({error: "posting not found"}); return; }
  const record = posting.rows[0];
  const applicationId = crypto.randomUUID();
  const eventId = crypto.randomUUID();
  const client = await database().connect();
  try {
    await client.query("begin");
    const saved = await client.query(`
      insert into applications
        (id, posting_id, profile_id, current_stage, company, title, url, external_links, created_at, updated_at)
      values ($1, $2, $3, $4, $5, $6, $7, '{}'::json, now(), now())
      on conflict (profile_id, posting_id) do update
        set current_stage = excluded.current_stage,
            updated_at = now(),
            url = case
              when applications.url is null or applications.url = '' then excluded.url
              when applications.url ~* '^https?://([^/]+\\.)?(?:jobright\\.ai|simplify\\.jobs|indeed\\.com|linkedin\\.com)(?:/|$)'
                and excluded.url ~* '^https?://'
                and excluded.url !~* '^https?://([^/]+\\.)?(?:jobright\\.ai|simplify\\.jobs|indeed\\.com|linkedin\\.com)(?:/|$)' then excluded.url
              else applications.url
            end
      returning id, current_stage as stage, company, title, url, updated_at`,
    [applicationId, record.id, profile, stage, record.company, record.title, record.canonical_url]);
    await client.query(`
      insert into application_events
        (id, application_id, stage, origin, idempotency_key, external_revision, metadata, occurred_at)
      values ($1, $2, $3, 'web-vnext', $4, $5, $6, now())
      on conflict (idempotency_key) do nothing`,
    [eventId, saved.rows[0].id, stage, idempotency, clean(body.revision, 200) || null,
      JSON.stringify({posting_public_id: postingId})]);
    await client.query("commit");
    res.status(200).json({data: {...saved.rows[0], url: httpUrl(saved.rows[0].url) || ""}});
  } catch (error) {
    await client.query("rollback");
    throw error;
  } finally {
    client.release();
  }
}

module.exports = async (req, res) => {
  try {
    if (req.method === "GET") return await list(req, res);
    if (req.method === "POST" || req.method === "PUT") return await mutate(req, res);
    res.status(405).end();
  } catch (error) {
    serverError(res, error);
  }
};
