import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_alias_session_handoff_and_login_logout_redirects():
    script = textwrap.dedent(
        """
        process.env.SESSION_SECRET = "test-session-secret";
        process.env.GH_CLIENT_ID = "Ov23liumDbZIXDFoHMXH";
        process.env.GH_CLIENT_SECRET = "client-secret";
        const lib = require("./webapp/api/_lib");
        const login = require("./webapp/api/login");
        const handoff = require("./webapp/api/session-handoff");
        const logout = require("./webapp/api/logout");

        function response() {
          const out = {headers:{}, statusCode:200, body:null};
          out.setHeader = (key, value) => { out.headers[key] = value; };
          out.status = code => { out.statusCode = code; return out; };
          out.json = value => { out.body = value; return out; };
          out.send = value => { out.body = value; return out; };
          out.writeHead = (code, headers) => { out.statusCode = code; Object.assign(out.headers, headers); };
          out.end = () => {};
          return out;
        }

        const payload = {u:"VictorJimenez3", g:"opaque-github-token", k:"acct-1"};
        const cookie = lib.sessionCookies(payload)[0].split(";")[0];
        const issued = response();
        handoff({method:"GET", headers:{host:lib.CANON_HOST,
          origin:"https://job-radar-newgrad.vercel.app", cookie}}, issued);
        if (issued.statusCode !== 200 || !issued.body.ticket) throw new Error("handoff ticket missing");

        const applied = response();
        handoff({method:"POST", headers:{host:"job-radar-newgrad.vercel.app"},
          body:{ticket:issued.body.ticket}}, applied);
        if (applied.statusCode !== 200) throw new Error("handoff was not accepted");
        const bridgedCookie = applied.headers["Set-Cookie"][0].split(";")[0];
        if (lib.session({headers:{cookie:bridgedCookie}}).u !== payload.u) {
          throw new Error("bridged session cannot be read");
        }

        const aliasLogin = response();
        login({method:"GET", headers:{host:"job-radar-newgrad.vercel.app", cookie:""},
          query:{provider:"github"}}, aliasLogin);
        if (!aliasLogin.headers.Location.includes("return_host=job-radar-newgrad.vercel.app")) {
          throw new Error("alias login lost its return host");
        }

        const alreadySignedIn = response();
        login({method:"GET", headers:{host:lib.CANON_HOST, cookie},
          query:{provider:"github", return_host:"job-radar-newgrad.vercel.app"}}, alreadySignedIn);
        const returnLocation = new URL(alreadySignedIn.headers.Location);
        if (returnLocation.origin !== "https://job-radar-newgrad.vercel.app" ||
            returnLocation.search !== "?auth=already-signed-in" ||
            !returnLocation.hash.startsWith("#session_handoff=")) {
          throw new Error("existing login did not return a browser handoff");
        }
        if (returnLocation.href.includes("opaque-github-token")) {
          throw new Error("provider token leaked into the redirect");
        }
        const directHandoff = response();
        handoff({method:"POST", headers:{host:"job-radar-newgrad.vercel.app"},
          body:{ticket:new URLSearchParams(returnLocation.hash.slice(1)).get("session_handoff")}}, directHandoff);
        if (directHandoff.statusCode !== 200) {
          throw new Error("fragment handoff was not accepted without a canonical cookie");
        }

        const aliasLogout = response();
        logout({method:"GET", headers:{host:"job-radar-newgrad.vercel.app", cookie:bridgedCookie}, query:{}}, aliasLogout);
        if (!aliasLogout.headers.Location.includes("/api/logout?return_host=job-radar-newgrad.vercel.app")) {
          throw new Error("alias logout did not clear the canonical host");
        }
        """
    )
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_github_and_google_callbacks_return_fragment_handoffs():
    script = textwrap.dedent(
        """
        process.env.SESSION_SECRET = "test-session-secret";
        process.env.GH_CLIENT_ID = "Ov23liumDbZIXDFoHMXH";
        process.env.GH_CLIENT_SECRET = "client-secret";
        process.env.GOOGLE_AUTH_CLIENT_ID = "google-client";
        process.env.GOOGLE_AUTH_CLIENT_SECRET = "google-secret";
        const lib = require("./webapp/api/_lib");
        const tracker = require("./webapp/api/_google-tracker");
        const githubCallback = require("./webapp/api/callback");
        const googleCallback = require("./webapp/api/google-callback");

        function response() {
          const out = {headers:{}, statusCode:200, body:null};
          out.setHeader = (key, value) => { out.headers[key] = value; };
          out.status = code => { out.statusCode = code; return out; };
          out.json = value => { out.body = value; return out; };
          out.send = value => { out.body = value; return out; };
          out.writeHead = (code, headers) => { out.statusCode = code; Object.assign(out.headers, headers); };
          out.end = () => {};
          return out;
        }
        tracker.resolveAccount = async (provider, identity) => provider === "github"
          ? {account_id:"acct", keys:["github:1"], github:{id:String(identity.id), login:identity.login}}
          : {account_id:"acct", keys:["google:sub"], google:{sub:identity.sub, email:identity.email}};

        global.fetch = async (url) => {
          if (String(url).includes("github.com/login/oauth/access_token")) {
            return {ok:true, status:200, json:async () => ({access_token:"github-token"})};
          }
          if (String(url) === "https://api.github.com/user") {
            return {ok:true, status:200, json:async () => ({id:1, login:"VictorJimenez3"})};
          }
          throw new Error("unexpected GitHub request " + url);
        };
        const githubState = lib.seal({t:Date.now(), mode:"login", return_host:"job-radar-newgrad.vercel.app"});
        const githubResponse = response();
        (async () => {
          await githubCallback({query:{code:"github-code", state:githubState},
            headers:{cookie:"jr_o=" + githubState}}, githubResponse);
          const githubLocation = new URL(githubResponse.headers.Location);
          if (!githubLocation.hash.startsWith("#session_handoff=") ||
              githubLocation.href.includes("github-token")) throw new Error("GitHub callback lost sealed fragment handoff");

          global.fetch = async (url) => {
            if (String(url) === "https://oauth2.googleapis.com/token") {
              return {ok:true, status:200, json:async () => ({access_token:"google-token", refresh_token:"google-refresh"})};
            }
            if (String(url) === "https://openidconnect.googleapis.com/v1/userinfo") {
              return {ok:true, status:200, json:async () => ({sub:"sub", email:"user@example.com", email_verified:true})};
            }
            throw new Error("unexpected Google request " + url);
          };
          const googleState = lib.seal({t:Date.now(), mode:"login", verifier:"verifier", return_host:"job-radar-newgrad.vercel.app"});
          const googleResponse = response();
          await googleCallback({query:{code:"google-code", state:googleState},
            headers:{cookie:"jr_go=" + googleState}}, googleResponse);
          const googleLocation = new URL(googleResponse.headers.Location);
          if (!googleLocation.hash.startsWith("#session_handoff=") ||
              googleLocation.href.includes("google-token") || googleLocation.href.includes("google-refresh")) {
            throw new Error("Google callback lost sealed fragment handoff");
          }
        })().catch(error => { console.error(error); process.exit(1); });
        """
    )
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
