const { OWNER, REPO, BRANCH, PROFILE } = require("./_lib");

module.exports = (req, res) => {
  res.status(200).json({ owner: OWNER, repo: REPO, branch: BRANCH, profile: PROFILE });
};
