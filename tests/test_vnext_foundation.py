import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from radar.db.import_legacy import public_id
from radar.db.schema import metadata

ROOT = Path(__file__).resolve().parents[1]


def test_public_posting_id_is_stable_and_namespaced():
    job = {"source": "greenhouse", "external_id": "123", "url": "https://example.com/123"}
    assert public_id("new_grad", job) == public_id("new_grad", dict(job))
    assert public_id("new_grad", job).startswith("job_")
    assert public_id("internship", job) != public_id("new_grad", job)


def test_vnext_schema_covers_transactional_subsystems():
    expected = {
        "profiles",
        "companies",
        "source_boards",
        "auth_users",
        "auth_sessions",
        "auth_accounts",
        "auth_verifications",
        "auth_rate_limits",
        "source_runs",
        "postings",
        "posting_sightings",
        "posting_aliases",
        "posting_status_events",
        "score_snapshots",
        "applications",
        "application_events",
        "preferences",
        "feedback_events",
        "work_items",
        "notification_outbox",
        "automation_runs",
        "prompt_versions",
        "llm_runs",
        "company_research_versions",
    }
    assert expected <= set(metadata.tables)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_cursor_api_filters_action_queue_and_blocks_unsafe_urls():
    script = textwrap.dedent(
        """
        delete process.env.DATABASE_URL;
        process.env.RADAR_REPO = "owner/repo";
        process.env.RADAR_BRANCH = "main";
        const Module = require("module");
        const originalLoad = Module._load;
        Module._load = function(request, parent, isMain) {
          if (request === "@neondatabase/serverless") throw new Error("Neon must be lazy");
          return originalLoad.call(this, request, parent, isMain);
        };
        const api = require("./webapp/api/v1/router");
        const now = Math.floor(Date.now()/1000);
        global.fetch = async () => ({ok:true, json:async () => ({
          fresh:{company:"Fresh", title:"Engineer", url:"https://example.com/job", score:80,
            location_eligibility:{status:"us"},
            alert_ok:true, posted_at:now-3600, last_seen_at:now, score_reasons:["role fit"]},
          hostile:{company:"Hostile", title:"Engineer", url:"javascript:alert(1)", score:90,
            location_eligibility:{status:"us"},
            alert_ok:true, posted_at:now-3600, last_seen_at:now, score_reasons:[]},
          old:{company:"Old", title:"Engineer", url:"https://example.com/old", score:99,
            location_eligibility:{status:"us"},
            alert_ok:true, posted_at:now-40*86400, last_seen_at:now, score_reasons:[]}
        })});
        const req = {
          method:"GET", url:"/api/v1/router?path=jobs",
          query:{path:"jobs", profile:"new_grad", freshness:"action", eligibility:"eligible", limit:"1"}
        };
        const out = {headers:{}, statusCode:0, body:null,
          setHeader(k,v){this.headers[k]=v;}, status(code){this.statusCode=code;return this;},
          json(value){this.body=value;return this;}, end(){}};
        (async () => {
          await api(req,out);
          if(out.statusCode!==200 || out.body.data.length!==1 || !out.body.next_cursor) {
            throw new Error(JSON.stringify(out.body));
          }
          if(req.url!=="/api/v1/jobs") throw new Error(`public URL was not restored: ${req.url}`);
          const first=out.body.data[0];
          if(first.company!=="Hostile" || first.url!=="") throw new Error("unsafe URL was exposed");
          const next={...req,url:"/api/v1/router?path=jobs",
            query:{...req.query,path:"jobs",cursor:out.body.next_cursor}};
          const nextOut={...out,headers:{},statusCode:0,body:null};
          await api(next,nextOut);
          if(nextOut.body.data[0].company!=="Fresh") throw new Error("cursor did not advance");
          const authReq={method:"GET",url:"/api/v1/router?path=auth/get-session",query:{path:"auth/get-session"}};
          const authOut={...out,headers:{},statusCode:0,body:null};
          await api(authReq,authOut);
          if(authReq.url!=="/api/v1/auth/get-session" || authOut.statusCode===0 || authOut.statusCode===404) {
            throw new Error(`nested auth route was not preserved: ${authOut.statusCode}`);
          }
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    )
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_legacy_closed_job_detail_reads_full_history_record():
    script = textwrap.dedent(
        """
        const assert = require('node:assert/strict');
        delete process.env.DATABASE_URL;
        process.env.RADAR_REPO = 'owner/repo';
        process.env.RADAR_BRANCH = 'main';
        const Module = require('module');
        const originalLoad = Module._load;
        Module._load = function(request, parent, isMain) {
          if (request === '@neondatabase/serverless') throw new Error('Neon must be lazy');
          return originalLoad.call(this, request, parent, isMain);
        };
        const detail = require('./webapp/api/v1/_job-detail');
        const calls = [];
        global.fetch = async url => {
          calls.push(url);
          return {ok:true, json:async () => url.endsWith('/jobs_history.json')
            ? {closed:{score_reasons:['role fit +20'], posting:{years_min:0},
              posting_status:'expired', company:'Acme', title:'Engineer'}}
            : {closed:{posting_status:'expired', company:'Acme', title:'Engineer'}}};
        };
        const req = {method:'GET', query:{profile:'new_grad', id:'closed'}};
        const res = {statusCode:0, body:null, setHeader() {},
          status(code){this.statusCode=code;return this;},
          json(value){this.body=value;return this;}, end() {}};
        detail(req,res).then(() => {
          assert.equal(res.statusCode, 200);
          assert.deepEqual(res.body.data.score_reasons, ['role fit +20']);
          assert.equal(res.body.data.posting_facts.years_min, 0);
          assert.equal(calls.length, 2);
        }).catch(error => {console.error(error);process.exitCode=1;});
        """
    )
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Vercel API runtime checks")
def test_applications_list_prefers_safe_manual_urls_over_posting_fallbacks():
    script = textwrap.dedent(
        """
        const assert = require("node:assert/strict");
        const Module = require("module");
        const originalLoad = Module._load;
        const rows = [
          {id:"manual", url:"https://victor.example/manual",
            posting_url:"https://boards.greenhouse.io/acme/jobs/1", external_links:{}},
          {id:"jobright", url:"https://jobright.ai/jobs/info/2",
            posting_url:"https://jobs.lever.co/acme/2", external_links:{}},
          {id:"simplify", url:"https://simplify.jobs/p/3",
            posting_url:"https://jobs.lever.co/acme/3", external_links:{}},
          {id:"direct", url:"https://victor.example/direct",
            posting_url:"https://jobright.ai/jobs/info/3", external_links:{}},
          {id:"unsafe", url:"javascript:alert(1)", posting_url:"https://jobs.lever.co/acme/4", external_links:{}},
          {id:"both-unsafe", url:"javascript:alert(1)", posting_url:"javascript:alert(2)", external_links:{}},
          {id:"known-alt", url:"https://careers.acme.example/old", posting_url:"https://jobs.lever.co/acme/6",
            external_links:{alternate_urls:["https://careers.acme.example/old"]}},
        ];
        Module._load = function(request, parent, isMain) {
          if (request === "./_auth") return {owner: () => true};
          if (request === "./_db") return {
            configured: () => true,
            database: () => ({query: async () => ({rows})}),
            serverError: (res, error) => res.status(500).json({error: String(error)}),
          };
          return originalLoad.call(this, request, parent, isMain);
        };
        const applications = require("./webapp/api/v1/_applications");
        const res = {statusCode:0, body:null,
          status(code){this.statusCode=code;return this;}, json(value){this.body=value;}};
        applications({method:"GET", query:{profile:"new_grad"}}, res).then(() => {
          assert.equal(res.statusCode, 200);
          const byId = Object.fromEntries(res.body.data.map(row => [row.id, row.url]));
          assert.equal(byId.manual, "https://victor.example/manual");
          assert.equal(byId.jobright, "https://jobs.lever.co/acme/2");
          assert.equal(byId.simplify, "https://jobs.lever.co/acme/3");
          assert.equal(byId.direct, "https://victor.example/direct");
          assert.equal(byId.unsafe, "https://jobs.lever.co/acme/4");
          assert.equal(byId["both-unsafe"], "");
          assert.equal(byId["known-alt"], "https://jobs.lever.co/acme/6");
        }).catch(error => {console.error(error);process.exitCode=1;});
        """
    )
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
