const {OWNER, needSessionSetup, requireMutationRequest, session} = require("../_lib");
const {noStore} = require("./_db");

function owner(req, res, {mutation = false} = {}) {
  noStore(res);
  if (mutation) {
    if (!requireMutationRequest(req, res)) return null;
  } else if (needSessionSetup(res)) {
    return null;
  }
  const current = session(req);
  const login = current?.github?.login || (current?.g ? current.u : "");
  if (!current || login !== OWNER) {
    res.status(current ? 403 : 401).json({error: current ? "owner access required" : "sign in first"});
    return null;
  }
  return current;
}

module.exports = {owner};
