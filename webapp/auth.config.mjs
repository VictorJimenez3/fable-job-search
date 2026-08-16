import {Pool} from "@neondatabase/serverless";
import {betterAuth} from "better-auth";

const databaseURL = process.env.DATABASE_URL || "postgresql://invalid:invalid@localhost/invalid";
const baseURL = process.env.BETTER_AUTH_URL || "https://job-radar-newgrad.vercel.app";
const origins = new Set([
  baseURL,
  "https://job-radar-newgrad.vercel.app",
  "https://job-radar-vmj-8946s-projects.vercel.app",
  ...String(process.env.AUTH_ALIAS_HOSTS || "")
    .split(",")
    .map((host) => host.trim())
    .filter(Boolean)
    .map((host) => host.startsWith("https://") ? host : `https://${host}`),
]);

const socialProviders = {};
if (process.env.GH_CLIENT_ID && process.env.GH_CLIENT_SECRET) {
  socialProviders.github = {
    clientId: process.env.GH_CLIENT_ID,
    clientSecret: process.env.GH_CLIENT_SECRET,
    scope: ["read:user", "user:email"],
  };
}
if (process.env.GOOGLE_AUTH_CLIENT_ID && process.env.GOOGLE_AUTH_CLIENT_SECRET) {
  socialProviders.google = {
    clientId: process.env.GOOGLE_AUTH_CLIENT_ID,
    clientSecret: process.env.GOOGLE_AUTH_CLIENT_SECRET,
    accessType: "offline",
    prompt: "select_account consent",
  };
}

export const auth = betterAuth({
  appName: "Job Radar",
  baseURL,
  basePath: "/api/v1/auth",
  secret: process.env.BETTER_AUTH_SECRET || process.env.SESSION_SECRET || "development-only-secret-change-before-runtime",
  database: new Pool({connectionString: databaseURL, max: 3}),
  trustedOrigins: [...origins],
  socialProviders,
  user: {
    modelName: "auth_users",
    fields: {
      emailVerified: "email_verified",
      createdAt: "created_at",
      updatedAt: "updated_at",
    },
  },
  session: {
    modelName: "auth_sessions",
    fields: {
      userId: "user_id",
      expiresAt: "expires_at",
      ipAddress: "ip_address",
      userAgent: "user_agent",
      createdAt: "created_at",
      updatedAt: "updated_at",
    },
    expiresIn: 30 * 24 * 60 * 60,
    updateAge: 24 * 60 * 60,
  },
  account: {
    modelName: "auth_accounts",
    fields: {
      userId: "user_id",
      accountId: "account_id",
      providerId: "provider_id",
      accessToken: "access_token",
      refreshToken: "refresh_token",
      idToken: "id_token",
      accessTokenExpiresAt: "access_token_expires_at",
      refreshTokenExpiresAt: "refresh_token_expires_at",
      createdAt: "created_at",
      updatedAt: "updated_at",
    },
    encryptOAuthTokens: true,
    accountLinking: {enabled: true, trustedProviders: ["github", "google"]},
  },
  verification: {
    modelName: "auth_verifications",
    fields: {
      expiresAt: "expires_at",
      createdAt: "created_at",
      updatedAt: "updated_at",
    },
  },
  rateLimit: {
    enabled: true,
    window: 60,
    max: 100,
    storage: "database",
    modelName: "auth_rate_limits",
    fields: {lastRequest: "last_request"},
  },
  advanced: {
    useSecureCookies: process.env.NODE_ENV === "production",
    cookiePrefix: "job-radar-v2",
    database: {generateId: "uuid"},
  },
});
