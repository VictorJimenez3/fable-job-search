const {Pool} = require("@neondatabase/serverless");
const crypto = require("crypto");

let pool;

function configured() {
  return Boolean(process.env.DATABASE_URL);
}

function database() {
  if (!configured()) throw new Error("DATABASE_URL is not configured");
  if (!pool) pool = new Pool({connectionString: process.env.DATABASE_URL, max: 3});
  return pool;
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

module.exports = {configured, database, noStore, publicCache, serverError};
