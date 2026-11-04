// Public list and detail endpoints must agree with the crawler's persisted
// assessment. Missing geography is reviewable, never inferred from remote.
const boundedScore = (value) => {
  const score = Number(value);
  return Number.isFinite(score) ? Math.max(0, Math.min(100, score)) : 0;
};

function locationEvidence(job) {
  const evidence = job.location_eligibility;
  if (evidence && ["us", "mixed", "non_us", "unknown"].includes(evidence.status)) {
    return {status: evidence.status,
      evidence: Array.isArray(evidence.evidence) ? evidence.evidence.slice(0, 8).map(String) : [],
      reason: String(evidence.reason || "")};
  }
  return {status: "unknown", evidence: [], reason: "US location has not been verified"};
}

function eligibilityOf(job) {
  const location = locationEvidence(job).status;
  if (location === "non_us") return "excluded";
  const stored = ["eligible", "review", "excluded"].includes(job.eligibility) ? job.eligibility :
    job.alert_ok ? "eligible" : job.early_career_possible || job.explicit_new_grad || Number(job.score || 0) >= 45 ? "review" : "excluded";
  if (location === "unknown" && stored === "eligible") return "review";
  return stored;
}

module.exports = {boundedScore, locationEvidence, eligibilityOf};
