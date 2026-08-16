const crypto = require("crypto");
const {BRANCH, REPO, normalizeProfile, VALID_PROFILES, httpUrl} = require("../_lib");
const {configured, database, publicCache, serverError} = require("./_db");

const LEGACY_TTL_MS = 60_000;
let legacyCache = new Map();

function queryValue(req, name, fallback = "") {
  const value = req.query?.[name];
  return Array.isArray(value) ? String(value[0] || fallback) : String(value || fallback);
}

function filters(req) {
  const profile = normalizeProfile(queryValue(req, "profile", "new_grad"));
  const freshness = ["action", "7d", "30d", "all"].includes(queryValue(req, "freshness")) ? queryValue(req, "freshness") : "action";
  const eligibility = ["eligible", "review", "all"].includes(queryValue(req, "eligibility")) ? queryValue(req, "eligibility") : "eligible";
  const limit = Math.min(100, Math.max(1, Number.parseInt(queryValue(req, "limit", "50"), 10) || 50));
  if (!VALID_PROFILES.has(profile) || profile === "cheme") throw new Error("unsupported profile");
  return {profile, freshness, eligibility, limit, q: queryValue(req, "q").trim().slice(0, 120)};
}

function fingerprint(value) {
  return crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex").slice(0, 16);
}

function cursorOffset(cursor, filterHash) {
  if (!cursor) return 0;
  try {
    const parsed = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8"));
    if (parsed.f !== filterHash || !Number.isSafeInteger(parsed.o) || parsed.o < 0) throw new Error("cursor");
    return parsed.o;
  } catch {
    throw new Error("invalid cursor");
  }
}

function nextCursor(offset, filterHash) {
  return Buffer.from(JSON.stringify({o: offset, f: filterHash})).toString("base64url");
}

function eligibilityOf(job) {
  if (job.alert_ok) return "eligible";
  if (job.early_career_possible || job.explicit_new_grad || Number(job.score || 0) >= 45) return "review";
  return "excluded";
}

function priorityOf(job) {
  const reasons = Array.isArray(job.score_reasons) ? job.score_reasons.join(" ").toLowerCase() : "";
  if (reasons.includes("explicit goal company")) return "goal";
  return Number(job.score || 0) >= 66 ? "recommended" : "explore";
}

function publicJob(id, job, profile) {
  return {
    public_id: job.public_id || `legacy_${id}`,
    legacy_id: id,
    profile,
    company: String(job.company || "Unknown"),
    title: String(job.title || "Untitled"),
    url: httpUrl(job.url) || "",
    locations: Array.isArray(job.locations) ? job.locations.slice(0, 12).map(String) : [],
    remote: Boolean(job.remote),
    posted_at: Number(job.posted_at) || null,
    last_seen_at: Number(job.last_seen_at) || null,
    salary: String(job.salary || ""),
    sector: String(job.sector || ""),
    evidence_score: Math.max(0, Math.min(100, Number(job.evidence_score ?? job.score ?? 0))),
    eligibility: ["eligible", "review", "excluded"].includes(job.eligibility) ? job.eligibility : eligibilityOf(job),
    priority_tier: ["goal", "recommended", "explore"].includes(job.priority_tier) ? job.priority_tier : priorityOf(job),
    score_reasons: Array.isArray(job.score_reasons) ? job.score_reasons.slice(0, 30).map(String) : [],
    status: ["open", "expired", "filled", "archived"].includes(job.posting_status) ? job.posting_status : "open",
  };
}

async function legacyJobs(profile) {
  const cached = legacyCache.get(profile);
  if (cached && Date.now() - cached.at < LEGACY_TTL_MS) return cached.value;
  const filename = profile === "internship" ? "intern_jobs.json" : "jobs.json";
  const response = await fetch(`https://raw.githubusercontent.com/${REPO}/${BRANCH}/state/${filename}`);
  if (!response.ok) throw new Error(`legacy state returned ${response.status}`);
  const raw = await response.json();
  const value = Object.entries(raw).map(([id, job]) => publicJob(id, job, profile));
  legacyCache.set(profile, {at: Date.now(), value});
  return value;
}

function visible(job, f, now) {
  if (job.status !== "open") return false;
  if (f.eligibility !== "all" && job.eligibility !== f.eligibility) return false;
  const text = `${job.company} ${job.title} ${job.locations.join(" ")}`.toLowerCase();
  if (f.q && !text.includes(f.q.toLowerCase())) return false;
  const age = job.posted_at ? now - job.posted_at : null;
  if (f.freshness === "7d" && (age === null || age > 7 * 86400)) return false;
  if (f.freshness === "30d" && (age === null || age > 30 * 86400)) return false;
  if (f.freshness === "action") {
    if (job.eligibility !== "eligible") return false;
    const recentlyPosted = age !== null && age <= 7 * 86400;
    const recentlyVerified = age === null && job.last_seen_at && now - job.last_seen_at <= 86400;
    if (!recentlyPosted && !recentlyVerified) return false;
  }
  return true;
}

async function fromLegacy(f, offset) {
  const now = Math.floor(Date.now() / 1000);
  const all = (await legacyJobs(f.profile)).filter((job) => visible(job, f, now));
  const tier = {goal: 3, recommended: 2, explore: 1};
  all.sort((a, b) => tier[b.priority_tier] - tier[a.priority_tier] || b.evidence_score - a.evidence_score || (b.posted_at || b.last_seen_at || 0) - (a.posted_at || a.last_seen_at || 0) || a.public_id.localeCompare(b.public_id));
  return {rows: all.slice(offset, offset + f.limit + 1), total: all.length};
}

async function fromPostgres(f, offset) {
  const values = [f.profile];
  const where = ["p.profile_id = $1", "p.status = 'open'"];
  if (f.q) {
    values.push(`%${f.q}%`);
    where.push(`(p.company ilike $${values.length} or p.title ilike $${values.length} or cast(p.locations as text) ilike $${values.length})`);
  }
  if (f.eligibility !== "all") {
    values.push(f.eligibility);
    where.push(`coalesce(s.eligibility, 'review') = $${values.length}`);
  }
  if (f.freshness === "action") where.push("(p.posted_at >= now() - interval '7 days' or (p.posted_at is null and p.last_seen_at >= now() - interval '24 hours')) and coalesce(s.eligibility, 'review') = 'eligible'");
  if (f.freshness === "7d") where.push("p.posted_at >= now() - interval '7 days'");
  if (f.freshness === "30d") where.push("p.posted_at >= now() - interval '30 days'");
  values.push(f.limit + 1, offset);
  const query = `
    select p.public_id, coalesce(a.alias, '') as legacy_id, p.profile_id as profile,
      p.company, p.title, p.canonical_url as url, p.locations, p.remote,
      extract(epoch from p.posted_at)::bigint as posted_at,
      extract(epoch from p.last_seen_at)::bigint as last_seen_at,
      p.salary, p.sector, coalesce(s.evidence_score, 0) as evidence_score,
      coalesce(s.eligibility, 'review') as eligibility,
      coalesce(s.priority_tier, 'explore') as priority_tier,
      coalesce(s.reasons, '[]'::json) as score_reasons, p.status
    from postings p
    left join lateral (
      select evidence_score, eligibility, priority_tier, reasons from score_snapshots
      where posting_id = p.id order by created_at desc limit 1
    ) s on true
    left join lateral (
      select alias from posting_aliases where posting_id = p.id and kind = 'legacy_id' limit 1
    ) a on true
    where ${where.join(" and ")}
    order by case coalesce(s.priority_tier, 'explore') when 'goal' then 3 when 'recommended' then 2 else 1 end desc,
      coalesce(s.evidence_score, 0) desc, coalesce(p.posted_at, p.last_seen_at) desc, p.public_id
    limit $${values.length - 1} offset $${values.length}`;
  const result = await database().query(query, values);
  return {rows: result.rows.map((job) => ({...job, url: httpUrl(job.url) || "", posted_at: Number(job.posted_at) || null, last_seen_at: Number(job.last_seen_at) || null, evidence_score: Number(job.evidence_score)}))};
}

module.exports = async (req, res) => {
  publicCache(res);
  if (req.method !== "GET") { res.status(405).end(); return; }
  try {
    const f = filters(req);
    const hash = fingerprint(f);
    const offset = cursorOffset(queryValue(req, "cursor"), hash);
    const source = configured() ? "postgres" : "legacy-fallback";
    const result = configured() ? await fromPostgres(f, offset) : await fromLegacy(f, offset);
    const hasNext = result.rows.length > f.limit;
    const data = result.rows.slice(0, f.limit);
    res.status(200).json({data, next_cursor: hasNext ? nextCursor(offset + data.length, hash) : null,
      generated_at: new Date().toISOString(), source, ...(result.total === undefined ? {} : {total: result.total})});
  } catch (error) {
    const message = String(error.message || error);
    if (message.includes("cursor") || message.includes("profile")) {
      res.status(400).json({error: message.slice(0, 180)});
    } else {
      serverError(res, error);
    }
  }
};
