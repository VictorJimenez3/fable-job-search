const { CANON_HOST, requestHost, authReturnHost, clearSessionCookies } = require("./_lib");

module.exports = (req, res) => {
  const host = requestHost(req);
  const localOnly = req.query?.local_only === "1";
  const returnHost = authReturnHost(req.query?.return_host);
  res.setHeader("Set-Cookie", clearSessionCookies());

  if (host !== CANON_HOST && !localOnly) {
    const alias = authReturnHost(host);
    const location = alias
      ? `https://${CANON_HOST}/api/logout?return_host=${encodeURIComponent(alias)}`
      : "/";
    res.writeHead(302, { Location: location });
    res.end();
    return;
  }

  if (host === CANON_HOST && !returnHost && !localOnly) {
    // Clear the default public shortcut too when a user signs out from the
    // old bookmark. The alias request is local-only, so it cannot bounce back.
    const defaultAlias = authReturnHost("job-radar-newgrad.vercel.app");
    if (defaultAlias) {
      res.writeHead(302, {Location: `https://${defaultAlias}/api/logout?local_only=1`});
      res.end();
      return;
    }
  }

  const location = returnHost ? `https://${returnHost}/?auth=signed-out` : "/?auth=signed-out";
  res.writeHead(302, { Location: location });
  res.end();
};
