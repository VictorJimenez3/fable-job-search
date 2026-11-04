"""Exercise the real public API's ranking and geography boundaries in Node."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for API checks")
def test_public_lists_use_final_score_and_require_us_evidence_in_both_lanes():
    script = r"""
const assert = require('node:assert/strict');
delete process.env.DATABASE_URL;
const api = require('./webapp/api/v1/_jobs');
const detail = require('./webapp/api/v1/_job-detail');
const now = Math.floor(Date.now()/1000);
const common = {title:'Software Engineer, New Grad', url:'https://jobs.example.test/1',
  posted_at:now-100, alert_ok:true, eligibility:'eligible', priority_tier:'recommended',
  locations:['United States'], location_eligibility:{status:'us'}};
const data = {
  quality:{...common, company:'Strong opportunity', score:90, evidence_score:88},
  goal:{...common, company:'Goal label', score:40, evidence_score:95, priority_tier:'goal'},
  unknown:{...common, company:'Unknown location', score:99, locations:['Remote'], remote:true,
    location_eligibility:{status:'unknown'}},
  foreign:{...common, company:'Foreign', score:100, locations:['Remote, Canada'], remote:true,
    location_eligibility:{status:'non_us'}},
  mixed:{...common, company:'US option', score:85, locations:['London, UK','New York, NY'],
    location_eligibility:{status:'mixed'}},
};
global.fetch = async () => ({ok:true, json:async()=>data});
function response() {return {statusCode:0, body:null, setHeader(){},
  status(v){this.statusCode=v;return this;},json(v){this.body=v;return this;},end(){}};}
(async()=>{
  for(const profile of ['new_grad','internship']) {
    const req={method:'GET',query:{profile,freshness:'all',eligibility:'eligible'}};
    const res=response(); await api(req,res);
    assert.equal(res.statusCode,200);
    assert.deepEqual(res.body.data.map(j=>j.legacy_id),['quality','mixed','goal']);
    assert.equal(res.body.data[2].ranking_score,40);
    assert.equal(res.body.data[2].evidence_score,95);
    const reviewed=response();
    await api({method:'GET',query:{profile,freshness:'all',eligibility:'review'}},reviewed);
    assert.deepEqual(reviewed.body.data.map(j=>j.legacy_id),['unknown']);
    const all=response();
    await api({method:'GET',query:{profile,freshness:'all',eligibility:'all'}},all);
    assert(!all.body.data.some(j=>j.legacy_id==='foreign'));
    const opened=response();
    await detail({method:'GET',query:{profile,id:'unknown'}},opened);
    assert.equal(opened.body.data.eligibility,'review');
    assert.equal(opened.body.data.location_eligibility.status,'unknown');
    const goalDetail=response();
    await detail({method:'GET',query:{profile,id:'goal'}},goalDetail);
    assert.equal(goalDetail.body.data.ranking_score,40);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
