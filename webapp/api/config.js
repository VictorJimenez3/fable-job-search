const { OWNER, REPO, BRANCH, PROFILE, CANON_HOST, envv, googleAuthConfigured } = require("./_lib");
const tracker = require("./_google-tracker");

module.exports = (req, res) => {
  res.status(200).json({ owner: OWNER, repo: REPO, branch: BRANCH, profile: PROFILE,
    auth_host: CANON_HOST,
    profiles: ["new_grad", "internship"],
    auth_providers: {github: Boolean(envv("GH_CLIENT_ID") && envv("GH_CLIENT_SECRET") && envv("SESSION_SECRET")),
      google: Boolean(googleAuthConfigured() && envv("SESSION_SECRET"))},
    tracker_configured: tracker.personalConfigured() });
};
