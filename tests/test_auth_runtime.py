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
        if (alreadySignedIn.headers.Location !==
            "https://job-radar-newgrad.vercel.app/?auth=already-signed-in") {
          throw new Error("existing login still returned the old 409");
        }

        const aliasLogout = response();
        logout({method:"GET", headers:{host:"job-radar-newgrad.vercel.app", cookie:bridgedCookie}, query:{}}, aliasLogout);
        if (!aliasLogout.headers.Location.includes("/api/logout?return_host=job-radar-newgrad.vercel.app")) {
          throw new Error("alias logout did not clear the canonical host");
        }
        """
    )
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
