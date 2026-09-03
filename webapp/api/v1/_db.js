const crypto = require("crypto");

let pool;
let applicationSchemaPromise;

function configured() {
  return Boolean(process.env.DATABASE_URL);
}

function database() {
  if (!configured()) throw new Error("DATABASE_URL is not configured");
  if (!pool) {
    // Keep the JSON fallback genuinely dependency-free. CI, forks, and the
    // deterministic crawler can load public APIs before frontend packages are
    // installed; Neon is needed only after DATABASE_URL activates Postgres.
    const {Pool} = require("@neondatabase/serverless");
    pool = new Pool({connectionString: process.env.DATABASE_URL, max: 3});
  }
  return pool;
}

async function ensureApplicationSchema() {
  if (!configured()) throw new Error("DATABASE_URL is not configured");
  if (!applicationSchemaPromise) {
    const client = database();
    applicationSchemaPromise = (async () => {
      await client.query(`
        create table if not exists application_runs (
          queue_id text primary key,
          owner_key text not null,
          payload jsonb not null,
          state text not null default 'queued',
          revision integer not null default 0,
          lease_owner text,
          lease_expires_at timestamptz,
          updated_at timestamptz not null default now(),
          created_at timestamptz not null default now()
        )`);
      await client.query("create index if not exists application_runs_owner_updated on application_runs(owner_key, updated_at desc)");
      await client.query(`
        create table if not exists application_run_events (
          id bigserial primary key,
          owner_key text not null,
          queue_id text not null,
          event_id text not null unique,
          revision integer,
          type text not null,
          payload jsonb not null,
          created_at timestamptz not null default now()
        )`);
      await client.query("create index if not exists application_run_events_queue on application_run_events(owner_key, queue_id, created_at desc)");
      await client.query(`
        create table if not exists application_workers (
          owner_key text primary key,
          payload jsonb not null,
          last_seen_at timestamptz not null default now()
        )`);
      return true;
    })().catch(error => { applicationSchemaPromise = null; throw error; });
  }
  return applicationSchemaPromise;
}

function noStore(res) {
  res.setHeader("Cache-Control", "private, no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");
}

function publicCache(res) {
  res.setHeader("Cache-Control", "public, s-maxage=60, stale-while-revalidate=300");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Vary", "Accept-Encoding");
}

function serverError(res, error) {
  const requestId = crypto.randomUUID();
  console.error(`[job-radar v1] ${requestId}`, error);
  res.status(502).json({error: "service temporarily unavailable", request_id: requestId});
}

module.exports = {configured, database, ensureApplicationSchema, noStore, publicCache, serverError};
