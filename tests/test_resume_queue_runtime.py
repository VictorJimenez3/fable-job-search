import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_owner_cloud_resume_queue_round_trip_and_public_snapshot_boundary():
    script = textwrap.dedent(
        r'''
        process.env.SESSION_SECRET = "test-session-secret";
        process.env.GOOGLE_CLIENT_ID = "client";
        process.env.GOOGLE_CLIENT_SECRET = "secret";
        process.env.GOOGLE_REFRESH_TOKEN = "refresh";
        const lib = require("./webapp/api/_lib");
        const tracker = require("./webapp/api/_google-tracker");
        tracker.resumeDriveAccess = async () => ({token:"drive-token", source:"owner"});
        const api = require("./webapp/api/resume-bank");

        let folderCreated = false;
        let queueBytes = null;
        let uploadCount = 0;
        const folder = {id:"folder-1", name:"Job Radar Resume Bank"};
        const response = () => {
          const out = {statusCode:200, body:null, headers:{}};
          out.status = code => { out.statusCode = code; return out; };
          out.json = body => { out.body = body; return out; };
          out.setHeader = (key, value) => { out.headers[key] = value; };
          out.end = body => { out.body = body; };
          return out;
        };
        const jsonResponse = body => ({ok:true, status:200, json:async () => body});
        global.fetch = async (url, options = {}) => {
          const parsed = new URL(String(url));
          const method = options.method || "GET";
          if (parsed.pathname === "/drive/v3/files" && method === "GET") {
            const query = parsed.searchParams.get("q") || "";
            if (query.includes("mimeType = 'application/vnd.google-apps.folder'")) {
              return jsonResponse({files: folderCreated ? [folder] : []});
            }
            if (query.includes("resume-studio-cloud-queue.json")) {
              return jsonResponse({files: queueBytes ? [{id:"queue-file", name:"resume-studio-cloud-queue.json"}] : []});
            }
            return jsonResponse({files: []});
          }
          if (parsed.pathname === "/drive/v3/files" && method === "POST") {
            folderCreated = true;
            return jsonResponse(folder);
          }
          if (parsed.pathname.startsWith("/upload/drive/v3/files") && ["POST", "PATCH"].includes(method)) {
            uploadCount += 1;
            const raw = Buffer.isBuffer(options.body) ? options.body : Buffer.from(options.body || "");
            const marker = Buffer.from("\r\n\r\n");
            const first = raw.indexOf(marker);
            const second = raw.indexOf(marker, first + marker.length);
            const ending = raw.lastIndexOf(Buffer.from("\r\n--"));
            queueBytes = raw.subarray(second + marker.length, ending);
            return jsonResponse({id:"queue-file"});
          }
          if (parsed.pathname === "/drive/v3/files/queue-file" && parsed.searchParams.get("alt") === "media") {
            const value = queueBytes || Buffer.from("{}");
            return {ok:true, status:200, arrayBuffer:async () => value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength)};
          }
          throw new Error(`unexpected Drive request: ${method} ${url}`);
        };

        const cookie = lib.sessionCookies({github:{login:"VictorJimenez3"}})[0].split(";")[0];
        const headers = {cookie, host:lib.CANON_HOST};
        const mutationHeaders = {...headers, origin:`https://${lib.CANON_HOST}`, "content-type":"application/json", "sec-fetch-site":"same-origin"};
        const call = async (method, query, body) => {
          const req = {method, headers: method === "POST" ? mutationHeaders : headers, query:query||{}};
          if (body) req.body = body;
          const out = response();
          await api(req, out);
          return out;
        };

        (async () => {
          const queued = await call("POST", {}, {action:"queue", mode:"ai", job:{
            id:"job-1", company:"Example", title:"Software Engineer", url:"https://example.com/jobs/1",
            locations:["Remote"], score:91, explicit_new_grad:true, score_reasons:["private detail must not persist"],
          }});
          if (queued.statusCode !== 200 || !queued.body.item?.queue_id || queued.body.item.job.score_reasons) {
            throw new Error(`queue boundary failed: ${JSON.stringify(queued.body)}`);
          }
          const duplicate = await call("POST", {}, {action:"queue", mode:"ai", job:{
            id:"job-1", company:"Example", title:"Software Engineer", url:"https://example.com/jobs/1",
          }});
          if (!duplicate.body.duplicate || uploadCount !== 1) throw new Error("active duplicate was not coalesced");
          const updated = await call("POST", {}, {action:"queue_update", queue_id:queued.body.item.queue_id,
            state:"running", run_id:"abc123", message:"worker started"});
          if (updated.statusCode !== 200 || updated.body.item.state !== "running" || updated.body.item.run_id !== "abc123") {
            throw new Error(`queue update failed: ${JSON.stringify(updated.body)}`);
          }
          const listed = await call("GET", {queue:"1"});
          if (listed.statusCode !== 200 || listed.body.source !== "owner" || listed.body.items[0].state !== "running") {
            throw new Error(`queue read failed: ${JSON.stringify(listed.body)}`);
          }
        })().catch(error => { console.error(error); process.exit(1); });
        '''
    )
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
