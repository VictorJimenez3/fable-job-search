"""Regression coverage for the private Google Sheets URL reconciliation path."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_tracker_link_refresh_is_bounded_idempotent_and_manual_url_safe():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const match = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/);
assert.ok(match, 'classic Jobs script exists');
const source = match[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const storage = new Map();
const calls = [];
const context = {
  window: {addEventListener() {}},
  document: {querySelector: () => ({addEventListener() {}}), querySelectorAll: () => [], addEventListener() {}},
  localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  clearTimeout() {}, setTimeout() {}, Date, URL, console,
  fetch: async (url, options = {}) => {
    calls.push(JSON.parse(options.body));
    return {ok:true, status:200, json:async () => ({ok:true, changed:true})};
  },
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`
  S.auth = {owner:false};
  S.userTracker = true;
  S.lane = 'new_grad';
  S.lanes.new_grad.jobs = {
    job1: {id:'job1', url:'https://jobright.ai/jobs/info/1', alternate_urls:['https://boards.greenhouse.io/acme/jobs/1']},
    manual: {id:'manual', url:'https://jobright.ai/jobs/info/2', alternate_urls:['https://boards.greenhouse.io/acme/jobs/2']},
    direct: {id:'direct', url:'https://boards.greenhouse.io/acme/jobs/3'},
  };
  S.lanes.new_grad.applied = [
    {id:'job1', url:'https://jobright.ai/jobs/info/1', stage:'interview', saved_at:'keep-date'},
    {id:'manual', url:'https://jobright.ai/jobs/info/2', stage:'saved', source:'manual'},
    {id:'direct', url:'https://boards.greenhouse.io/acme/jobs/3', stage:'saved'},
  ];
`, context);
const first = vm.runInContext("syncTrackerLinkRefresh('new_grad')", context);
first.then(async result => {
  assert.equal(result.count, 1);
  assert.equal(result.attempted, 1);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].action, 'link-refresh');
  assert.equal(calls[0].canonical_job_id, 'job1');
  assert.equal(calls[0].previous_url, 'https://jobright.ai/jobs/info/1');
  assert.equal(calls[0].canonical_url, 'https://boards.greenhouse.io/acme/jobs/1');
  assert.equal(calls[0].profile, 'new_grad');
  assert.equal(vm.runInContext("S.lanes.new_grad.applied[0].stage", context), 'interview');
  await vm.runInContext("syncTrackerLinkRefresh('new_grad')", context);
  assert.equal(calls.length, 1, 'hydration reconciliation is one-shot and idempotent');
}).catch(error => { console.error(error); process.exit(1); });
"""
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
