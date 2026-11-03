const crypto = require("crypto");
const {BRANCH, REPO, httpUrl, normalizeProfile} = require("../_lib");
const {configured, database, publicCache, serverError} = require("./_db");

const legacyCache = new Map();
const LEGACY_TTL_MS = 60_000;
const DETAIL_SCHEMA_VERSION = 1;

function detailError(message, status = 502) {
  const error = new Error(message);
  error.detailStatus = status;
  error.detailCode = "job_detail_unavailable";
  return error;
}

function detailDirectory(profile) {
  return profile === "internship" ? "intern_jobs_details" : "jobs_details";
}

function detailShardRef(profile, primaryKey, ref) {
  if (!ref || typeof ref !== "object") return null;
  if (ref.version !== DETAIL_SCHEMA_VERSION) {
    throw detailError("job detail pointer uses an unsupported schema version");
  }
  if (ref.key !== primaryKey) {
    throw detailError("job detail pointer does not match the requested posting");
  }
  if (typeof ref.sha256 !== "string" || !/^[a-f0-9]{64}$/i.test(ref.sha256)) {
    throw detailError("job detail pointer has an invalid checksum");
  }
  const keyDigest = crypto.createHash("sha256").update(primaryKey, "utf8").digest("hex");
  const expected = `${detailDirectory(profile)}/${keyDigest.slice(0, 2)}.json`;
  if (ref.shard !== expected) {
    throw detailError("job detail pointer targets the wrong lane or shard");
  }
  return ref;
}

async function hydrateDetail(profile, primaryKey, job) {
  const ref = job?.detail_ref;
  if (!ref) return job;
  const validRef = detailShardRef(profile, primaryKey, ref);
  const cached = legacyCache.get(profile)?.details?.[validRef.shard];
  // A new primary snapshot can point at the same bucket with a new record
  // revision. Keep the cache, but never reuse a bucket for a different ref
  // checksum.
  let shard = cached?.sha256 === validRef.sha256 ? cached.shard : null;
  if (!shard) {
    const response = await fetch(`https://raw.githubusercontent.com/${REPO}/${BRANCH}/state/${validRef.shard}`);
    if (!response.ok) throw detailError(`job detail shard returned ${response.status}`);
    try { shard = await response.json(); }
    catch { throw detailError("job detail shard is not valid JSON"); }
    const cache = legacyCache.get(profile) || {at: 0, records: {}};
    cache.details = {...(cache.details || {}), [validRef.shard]: {sha256:validRef.sha256, shard}};
    legacyCache.set(profile, cache);
  }
  if (!shard || shard.schema_version !== DETAIL_SCHEMA_VERSION ||
      !shard.records || typeof shard.records !== "object" ||
      !shard.signatures || typeof shard.signatures !== "object") {
    throw detailError("job detail shard has an unsupported shape");
  }
  const detail = shard.records[validRef.key];
  const signature = shard.signatures[validRef.key];
  if (!detail || typeof detail !== "object" || Array.isArray(detail) ||
      signature !== validRef.sha256) {
    // Python is the producer of the canonical payload hash. Consumers use
    // the signed map entry here instead of reserializing numeric fields in JS.
    throw detailError("job detail shard failed its revision signature");
  }
  return {...job, ...detail};
}

function queryValue(req, name, fallback = "") {
  const value = req.query?.[name];
  return Array.isArray(value) ? String(value[0] || fallback) : String(value || fallback);
}

function safeId(value) {
  const id = String(value || "").trim();
  return /^[A-Za-z0-9_-]{1,200}$/.test(id) ? id : "";
}

function publicLegacy(id, job, profile) {
  return {
    public_id: job.public_id || `legacy_${id}`,
    legacy_id: id,
    profile,
    company: String(job.company || "Unknown"),
    title: String(job.title || "Untitled"),
    url: httpUrl(job.url) || "",
    source_url: httpUrl(job.source_url) || "",
    locations: Array.isArray(job.locations) ? job.locations.slice(0, 20).map(String) : [],
    remote: Boolean(job.remote),
    posted_at: Number(job.posted_at) || null,
    last_seen_at: Number(job.last_seen_at) || null,
    salary: String(job.salary || ""),
    sector: String(job.sector || ""),
    evidence_score: Math.max(0, Math.min(100, Number(job.evidence_score ?? job.score ?? 0))),
    personalized_score: Math.max(0, Math.min(100, Number(job.score || 0))),
    eligibility: ["eligible", "review", "excluded"].includes(job.eligibility) ? job.eligibility : (job.alert_ok ? "eligible" : "review"),
    priority_tier: ["goal", "recommended", "explore"].includes(job.priority_tier) ? job.priority_tier : "explore",
    score_dimensions: job.score_dimensions && typeof job.score_dimensions === "object" ? job.score_dimensions : {},
    score_reasons: Array.isArray(job.score_reasons) ? job.score_reasons.slice(0, 80).map(String) : [],
    posting_facts: job.posting && typeof job.posting === "object" ? job.posting : {},
    status: ["open", "expired", "filled", "archived"].includes(job.posting_status) ? job.posting_status : "open",
    status_reason: String(job.posting_status_reason || ""),
  };
}

async function legacyRecord(profile, identifier) {
  const cached = legacyCache.get(profile);
  let records;
  if (cached && Date.now() - cached.at < LEGACY_TTL_MS) {
    records = cached.records;
  } else {
    const filename = profile === "internship" ? "intern_jobs.json" : "jobs.json";
    const response = await fetch(`https://raw.githubusercontent.com/${REPO}/${BRANCH}/state/${filename}`);
    if (!response.ok) throw new Error(`legacy state returned ${response.status}`);
    records = await response.json();
    legacyCache.set(profile, {at: Date.now(), records});
  }
  const legacyId = identifier.startsWith("legacy_") ? identifier.slice(7) : identifier;
  let foundKey = records[legacyId] ? legacyId : "";
  let found = records[legacyId];
  if (!found) {
    const entry = Object.entries(records).find(([, job]) => job.public_id === identifier);
    foundKey = entry?.[0] || "";
    found = entry?.[1];
  }
  if (found && found.detail_ref) {
    // A detail pointer is authoritative for generated state.  Do not quietly
    // turn a missing or mixed-revision shard into a misleading empty ledger.
    found = await hydrateDetail(profile, foundKey, found);
  }
  if (found && !found.detail_ref && (["expired", "filled", "archived"].includes(found.posting_status) || found.closed_at)) {
    let archived = legacyCache.get(profile)?.history;
    if (!archived) {
      const historyName = profile === "internship" ? "intern_jobs_history.json" : "jobs_history.json";
      const response = await fetch(`https://raw.githubusercontent.com/${REPO}/${BRANCH}/state/${historyName}`);
      if (response.ok) archived = await response.json();
      else if (response.status === 404) archived = {};
      else throw new Error(`legacy history returned ${response.status}`);
      legacyCache.get(profile).history = archived;
    }
    const full = archived[legacyId];
    if (full && full.posting_status === found.posting_status &&
        full.posting_status_changed_at === found.posting_status_changed_at) {
      found = {...full, ...found};
    }
  }
  return found ? publicLegacy(legacyId, found, profile) : null;
}

async function postgresRecord(identifier, profile) {
  const result = await database().query(`
    select p.public_id, coalesce(a.alias, '') as legacy_id, p.profile_id as profile,
      p.company, p.title, p.canonical_url as url, p.locations, p.remote,
      extract(epoch from p.posted_at)::bigint as posted_at,
      extract(epoch from p.last_seen_at)::bigint as last_seen_at,
      p.salary, p.sector, p.status, p.status_reason, p.posting_facts,
      coalesce(s.evidence_score, 0) as evidence_score,
      coalesce(s.eligibility, 'review') as eligibility,
      coalesce(s.priority_tier, 'explore') as priority_tier,
      coalesce(s.dimensions, '{}'::json) as score_dimensions,
      coalesce(s.reasons, '[]'::json) as score_reasons
    from postings p
    left join posting_aliases a on a.posting_id = p.id and a.kind = 'legacy_id'
    left join lateral (
      select evidence_score, eligibility, priority_tier, dimensions, reasons
      from score_snapshots where posting_id = p.id order by created_at desc limit 1
    ) s on true
    where p.profile_id = $1 and (p.public_id = $2 or a.alias = $2)
    limit 1`, [profile, identifier]);
  const row = result.rows[0];
  if (!row) return null;
  return {
    ...row,
    url: httpUrl(row.url) || "",
    posted_at: Number(row.posted_at) || null,
    last_seen_at: Number(row.last_seen_at) || null,
    evidence_score: Number(row.evidence_score),
  };
}

module.exports = async (req, res) => {
  publicCache(res);
  if (req.method !== "GET") { res.status(405).end(); return; }
  try {
    const identifier = safeId(queryValue(req, "id"));
    const profile = normalizeProfile(queryValue(req, "profile", "new_grad"));
    if (!identifier || !["new_grad", "internship"].includes(profile)) {
      res.status(400).json({error: "invalid posting identifier or profile"});
      return;
    }
    const data = configured()
      ? await postgresRecord(identifier, profile)
      : await legacyRecord(profile, identifier);
    if (!data) { res.status(404).json({error: "posting not found"}); return; }
    res.status(200).json({data, source: configured() ? "postgres" : "legacy-fallback"});
  } catch (error) {
    if (error?.detailCode === "job_detail_unavailable") {
      res.status(error.detailStatus || 502).json({error: error.message, code: error.detailCode});
      return;
    }
    serverError(res, error);
  }
};
