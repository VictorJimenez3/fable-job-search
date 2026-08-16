const {httpUrl, normalizeProfile} = require("../_lib");
const {configured, database, publicCache, serverError} = require("./_db");

module.exports = async (req, res) => {
  publicCache(res);
  if (req.method !== "GET") { res.status(405).end(); return; }
  if (!configured()) {
    res.status(200).json({data: [], source: "legacy-fallback", message: "Open company research in the classic UI"});
    return;
  }
  try {
    const profile = normalizeProfile(String(req.query?.profile || "new_grad").slice(0, 40));
    if (!["new_grad", "internship"].includes(profile)) {
      res.status(400).json({error: "unsupported profile"});
      return;
    }
    const result = await database().query(`
      select c.display_name as company, c.website, c.metadata,
        count(*) filter (where p.status = 'open')::int as open_postings,
        max(p.last_seen_at) as last_seen_at
      from companies c join postings p on p.company_id = c.id
      where p.profile_id = $1
      group by c.id order by open_postings desc, c.display_name limit 1000`, [profile]);
    res.status(200).json({
      data: result.rows.map((row) => ({...row, website: httpUrl(row.website) || ""})),
      source: "postgres",
    });
  } catch (error) {
    serverError(res, error);
  }
};
