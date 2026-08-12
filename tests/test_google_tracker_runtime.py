import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_expired_owner_registry_does_not_block_a_users_personal_tracker():
    script = textwrap.dedent(
        """
        process.env.GOOGLE_CLIENT_ID = "owner-client";
        process.env.GOOGLE_CLIENT_SECRET = "owner-secret";
        process.env.GOOGLE_REFRESH_TOKEN = "expired-owner-refresh";
        process.env.GOOGLE_SHEET_ID = "owner-sheet";
        process.env.GOOGLE_AUTH_CLIENT_ID = "public-client";
        process.env.GOOGLE_AUTH_CLIENT_SECRET = "public-secret";
        process.env.SESSION_SECRET = "test-session-secret";
        const tracker = require("./webapp/api/_google-tracker");
        let tokenCalls = 0;

        global.fetch = async (url, options = {}) => {
          const method = options.method || "GET";
          if (String(url).includes("oauth2.googleapis.com/token")) {
            tokenCalls += 1;
            if (tokenCalls === 1) return {ok: false, status: 401, json: async () => ({})};
            return {ok: true, status: 200, json: async () => ({access_token: "user-token"})};
          }
          if (String(url).startsWith("https://www.googleapis.com/drive/v3/files?")) {
            return {ok: true, status: 200, json: async () => ({files: []})};
          }
          if (String(url).includes("/drive/v3/files/")) {
            return {ok: true, status: 200, json: async () => ({id: "personal-sheet"})};
          }
          if (String(url) === "https://sheets.googleapis.com/v4/spreadsheets" && method === "POST") {
            return {ok: true, status: 200, json: async () => ({
              spreadsheetId: "personal-sheet",
              sheets: [
                {properties: {title: "Applications", sheetId: 0}},
                {properties: {title: "Guide", sheetId: 1}},
              ],
            })};
          }
          if (String(url).includes("/values/Applications") && method === "GET") {
            return {ok: true, status: 200, json: async () => ({values: [tracker.PERSONAL_HEADERS]})};
          }
          return {ok: true, status: 200, json: async () => ({})};
        };

        (async () => {
          const linked = await tracker.resolveAccount(
            "google", {sub: "user-sub", email: "user@example.com"}, null, "login",
            {accessToken: "fresh-user-token", refreshToken: "user-refresh"},
          );
          if (!tracker.hasPersonalSession(linked.personal_tracker)) throw new Error("missing personal session");
          if (linked.personal_tracker.s !== "personal-sheet") throw new Error("wrong personal Sheet");
          const result = await tracker.userTracker(linked.keys, linked.personal_tracker);
          if (!result.connected || !result.sheet_url.includes("personal-sheet")) {
            throw new Error("personal tracker did not reconnect");
          }

          // A later login with no owner registry must discover the existing
          // app-created workbook instead of creating another one.
          delete process.env.GOOGLE_REFRESH_TOKEN;
          delete process.env.GOOGLE_SHEET_ID;
          let createCalls = 0;
          global.fetch = async (url, options = {}) => {
            const method = options.method || "GET";
            if (String(url).startsWith("https://www.googleapis.com/drive/v3/files?")) {
              return {ok: true, status: 200, json: async () => ({
                files: [{id: "existing-sheet", modifiedTime: "2026-08-08T00:00:00Z"}],
              })};
            }
            if (String(url).includes("/values/Applications") && method === "GET") {
              return {ok: true, status: 200, json: async () => ({values: [tracker.PERSONAL_HEADERS]})};
            }
            if (String(url) === "https://sheets.googleapis.com/v4/spreadsheets" && method === "POST") {
              createCalls += 1;
            }
            return {ok: true, status: 200, json: async () => ({})};
          };
          const reconnected = await tracker.resolveAccount(
            "google", {sub: "user-sub", email: "user@example.com"}, null, "login",
            {accessToken: "fresh-user-token", refreshToken: "new-user-refresh"},
          );
          if (reconnected.personal_tracker.s !== "existing-sheet" || createCalls !== 0) {
            throw new Error("existing personal tracker was duplicated");
          }
        })().catch(error => { console.error(error); process.exit(1); });
        """
    )
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_optional_tracker_read_failure_degrades_without_a_boot_502():
    script = textwrap.dedent(
        """
        process.env.SESSION_SECRET = "test-session-secret";
        const {seal} = require("./webapp/api/_lib");
        const tracker = require("./webapp/api/_google-tracker");
        const api = require("./webapp/api/tracker");
        tracker.userTracker = async () => { throw new Error("Google token 401"); };
        let status = 0;
        let payload;
        const res = {
          status(code) { status = code; return this; },
          json(body) { payload = body; },
          end() {},
        };
        const req = {
          method: "GET",
          headers: {cookie: `jr_s=${seal({u: "example", k: "acct_test", keys: ["acct_test"]})}`},
          query: {},
        };
        (async () => {
          await api(req, res);
          if (status !== 200 || payload.tracker_unavailable !== true || payload.needs_google !== true) {
            throw new Error(`unexpected fallback: ${status} ${JSON.stringify(payload)}`);
          }
        })().catch(error => { console.error(error); process.exit(1); });
        """
    )
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
