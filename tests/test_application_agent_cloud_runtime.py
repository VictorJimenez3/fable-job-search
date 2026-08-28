import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_role_specific_writing_context_stays_scoped_to_one_queue():
    script = textwrap.dedent(
        r'''
        process.env.SESSION_SECRET = "test-session-secret";
        const lib = require("./webapp/api/_lib");
        const database = require("./webapp/api/v1/_db");
        let stored = {
          version:"application-agent-v1", updated_at:"",
          context:{version:"application-agent-v1", updated_at:"", answers:[{
            answer_id:"seed", question:"Seed", normalized_question:"seed", variants:[], fallback_for:[],
            select_all:false, category:"other", value:"ready", reusable:true, sensitive:false,
            queue_ids:[], evidence_ids:[], updated_at:"2026-01-01T00:00:00Z",
          }], mappings:{}},
          queue:{version:"application-agent-v1", updated_at:"", items:[]},
          issues:{version:"application-agent-v1", updated_at:"", issues:[]},
          pairing:{version:"application-agent-v1", updated_at:"", tokens:[]},
        };
        database.configured = () => true;
        database.database = () => ({query: async (sql, params = []) => {
          if (/select payload/i.test(sql)) return {rows: stored ? [{payload: stored}] : []};
          if (/insert into automation_runs/i.test(sql)) {
            stored = JSON.parse(params[2]);
            return {rows: []};
          }
          if (/update automation_runs/i.test(sql)) {
            stored = JSON.parse(params[0]);
            return {rows: []};
          }
          throw new Error(`unexpected database query: ${sql}`);
        }});
        const api = require("./webapp/api/_application-agent");

        const response = () => {
          const out = {statusCode:200, body:null, headers:{}};
          out.status = code => { out.statusCode = code; return out; };
          out.json = body => { out.body = body; return out; };
          out.setHeader = (key, value) => { out.headers[key] = value; };
          out.end = body => { out.body = body; };
          return out;
        };
        const cookie = lib.sessionCookies({github:{login:"VictorJimenez3"}})[0].split(";")[0];
        const headers = {cookie, host:lib.CANON_HOST};
        const mutationHeaders = {...headers, origin:`https://${lib.CANON_HOST}`, "content-type":"application/json", "sec-fetch-site":"same-origin"};
        const call = async (method, query, body) => {
          const req = {method, headers:method === "POST" ? mutationHeaders : headers, query:query || {}};
          if (body) req.body = body;
          const out = response();
          await api(req, out);
          return out;
        };

        (async () => {
          const first = await call("POST", {}, {action:"answer", question:"Context for: Why us?", value:"First role fact", category:"essay_context", reusable:false, queue_ids:["queue-1"]});
          const second = await call("POST", {}, {action:"answer", question:"Context for: Why us?", value:"Second role fact", category:"essay_context", reusable:false, queue_ids:["queue-2"]});
          const global = await call("POST", {}, {action:"answer", question:"Authorized to work?", value:"Yes", category:"work_authorization", reusable:true});
          if (first.statusCode !== 200 || second.statusCode !== 200 || global.statusCode !== 200) throw new Error(`answer save failed: ${JSON.stringify({first, second, global})}`);
          if (first.body.answer.answer_id === second.body.answer.answer_id) throw new Error("queue-scoped answers collided");
          const listed = await call("GET", {view:"context"});
          const answers = listed.body.context.answers;
          const one = answers.find(item => item.answer_id === first.body.answer.answer_id);
          const two = answers.find(item => item.answer_id === second.body.answer.answer_id);
          const reusable = answers.find(item => item.answer_id === global.body.answer.answer_id);
          if (JSON.stringify(one.queue_ids) !== JSON.stringify(["queue-1"]) || one.reusable !== false) throw new Error("first queue scope was lost");
          if (JSON.stringify(two.queue_ids) !== JSON.stringify(["queue-2"]) || two.reusable !== false) throw new Error("second queue scope was lost");
          if (reusable.reusable !== true || reusable.queue_ids.length) throw new Error("reusable profile answer was incorrectly scoped");
        })().catch(error => { console.error(error); process.exit(1); });
        '''
    )
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
