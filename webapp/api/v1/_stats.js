const {configured, database, publicCache, serverError} = require("./_db");

module.exports = async (req, res) => {
  publicCache(res);
  if (req.method !== "GET") { res.status(405).end(); return; }
  if (!configured()) { res.status(200).json({source: "legacy-fallback", postgres: false}); return; }
  try {
    const result = await database().query(`select profile_id as profile, count(*)::int as total,
      count(*) filter (where status='open')::int as open,
      max(last_seen_at) as latest_sighting from postings group by profile_id order by profile_id`);
    res.status(200).json({source: "postgres", postgres: true, profiles: result.rows, generated_at: new Date().toISOString()});
  } catch (error) {
    serverError(res, error);
  }
};
