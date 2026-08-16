import {toNodeHandler} from "better-auth/node";

export default async function handler(req, res) {
  const secret = process.env.BETTER_AUTH_SECRET || process.env.SESSION_SECRET || "";
  if (!process.env.DATABASE_URL || secret.length < 32 || !process.env.BETTER_AUTH_URL) {
    res.status(503).json({error: "database-backed auth is not configured"});
    return;
  }
  const {auth} = await import("../../auth.config.mjs");
  return toNodeHandler(auth)(req, res);
}
