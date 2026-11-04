"""Exercise the US-first location scope and verified employer links in the board runtime."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_location_scope_excludes_foreign_only_and_keeps_unknown_in_review():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const match = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/);
assert.ok(match, 'classic Jobs script exists');
const source = match[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const now = 1800000000;
const context = {
  window:{addEventListener() {}},
  document:{querySelector:() => ({addEventListener() {}}), querySelectorAll:() => [], addEventListener() {}},
  localStorage:{getItem:() => null, setItem() {}, removeItem() {}},
  clearTimeout() {}, Date:class extends Date { static now() { return now * 1000; } }, URL, console, fixtureNow:now,
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`
  S.lane = 'new_grad';
  S.jobs = {
    us:{id:'us', company:'USCo', title:'Software Engineer', score:80, posted_at:${now-86400}, location_eligibility:{status:'us', evidence:['New York, NY'], reason:'US employer location'}},
    mixed:{id:'mixed', company:'MixedCo', title:'Software Engineer', score:70, posted_at:${now-3600}, location_eligibility:{status:'mixed', evidence:['New York, NY','London, UK'], reason:'US and foreign options'}},
    foreign:{id:'foreign', company:'ForeignCo', title:'Software Engineer', score:95, posted_at:${now}, location_eligibility:{status:'non_us', evidence:['London, UK'], reason:'Only non-US locations'}},
    unknown:{id:'unknown', company:'UnknownCo', title:'Software Engineer', score:10, posted_at:${now-86400*400}, location_eligibility:{status:'unknown', evidence:[], reason:'No location supplied'}},
  };
  S.lanes.new_grad.jobs = S.jobs; S.applied = []; S.web = {maybe:[], excluded:[], jobs:{}};
  S.filter = {...DEFAULT_FILTER, min:45, sort:'best', bestWindow:'all', locationScope:'us', roles:['swe'], experience:''};
`, context);
assert.equal(vm.runInContext("locationEligibility(S.jobs.foreign).status", context), 'non_us');
assert.equal(vm.runInContext("locationEligibility(S.jobs.unknown).status", context), 'unknown');
assert.equal(vm.runInContext("JSON.stringify(jobList().map(job => job.id))", context), JSON.stringify(['us','mixed']), 'US default keeps US and mixed choices only');
vm.runInContext("S.filter.locationScope='review'", context);
assert.equal(vm.runInContext("JSON.stringify(jobList().map(job => job.id))", context), JSON.stringify(['us','mixed','unknown']), 'location review keeps unknown even below score and freshness filters');
vm.runInContext("S.filter.locationScope='us'; S.jobs={fallback:{id:'fallback',company:'Fallback',title:'Software Engineer',score:80,posted_at:fixtureNow,locations:['Toronto, CA']}, india:{id:'india',company:'India',title:'Software Engineer',score:80,posted_at:fixtureNow,locations:['Bengaluru, IN']}, state:{id:'state',company:'State',title:'Software Engineer',score:80,posted_at:fixtureNow,locations:['San Francisco, CA']}}; S.lanes.new_grad.jobs=S.jobs;", context);
assert.equal(vm.runInContext("locationParts('Toronto, CA').countryKey", context), 'Canada');
assert.equal(vm.runInContext("locationParts('Bengaluru, IN').countryKey", context), 'India');
assert.equal(vm.runInContext("locationParts('San Francisco, CA').countryKey", context), 'United States');
"""
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_tracker_payload_uses_verified_employer_alternate_without_touching_stage():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const source = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/)[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const calls = [];
const context = {
  window:{addEventListener() {}}, document:{querySelector:() => ({addEventListener() {}}), querySelectorAll:() => [], addEventListener() {}},
  localStorage:{getItem:() => null, setItem() {}, removeItem() {}}, clearTimeout() {}, Date, URL, console,
  fetch:async (_url, options) => { calls.push(JSON.parse(options.body)); return {ok:true, json:async () => ({ok:true})}; },
};
vm.createContext(context); vm.runInContext(source, context);
vm.runInContext(`S.lane='new_grad'; S.applied=[{id:'r1',stage:'interview',url:'https://old.example/role'}];`, context);
vm.runInContext("trackerAction('track', {id:'r1',company:'Example',title:'Software Engineer',url:'https://jobright.ai/jobs/r1',alternate_urls:['https://jobs.greenhouse.io/example/jobs/r1']})", context).then(() => {
  assert.equal(calls[0].url, 'https://jobs.greenhouse.io/example/jobs/r1');
  assert.equal(vm.runInContext("S.applied[0].stage", context), 'interview');
}).catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
