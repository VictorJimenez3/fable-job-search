const crypto = require("crypto");
const applications = require("./_applications");
const companies = require("./_companies");
const jobDetail = require("./_job-detail");
const jobs = require("./_jobs");
const preferences = require("./_preferences");
const stats = require("./_stats");

function segments(req) {
  const value = req.query?.path;
  const parts = Array.isArray(value) ? value : String(value || "").split("/");
  return parts.map((part) => String(part).trim()).filter(Boolean);
}

function restorePublicURL(req, path) {
  const url = new URL(req.url || "/", "http://router.internal");
  url.pathname = `/api/v1/${path.map(encodeURIComponent).join("/")}`;
  url.searchParams.delete("path");
  req.url = `${url.pathname}${url.search}`;
  if (req.query) {
    const {path: _rewrittenPath, ...query} = req.query;
    req.query = query;
  }
}

async function auth(req, res) {
  const route = await import("./_better-auth.mjs");
  return route.default(req, res);
}

module.exports = async function v1Router(req, res) {
  const path = segments(req);
  restorePublicURL(req, path);
  try {
    if (path.length === 1 && path[0] === "jobs") return await jobs(req, res);
    if (path.length === 2 && path[0] === "jobs") {
      req.query = {...req.query, id: path[1]};
      return await jobDetail(req, res);
    }
    if (path.length === 1 && path[0] === "applications") return await applications(req, res);
    if (path.length === 1 && path[0] === "companies") return await companies(req, res);
    if (path.length === 1 && path[0] === "preferences") return await preferences(req, res);
    if (path.length === 1 && path[0] === "stats") return await stats(req, res);
    if (path.length >= 1 && path[0] === "auth") return await auth(req, res);
    res.status(404).json({error: "v1 endpoint not found"});
  } catch (error) {
    const requestId = crypto.randomUUID();
    console.error("v1 router failure", requestId, error);
    if (!res.headersSent) res.status(500).json({error: "request failed", request_id: requestId});
  }
};
