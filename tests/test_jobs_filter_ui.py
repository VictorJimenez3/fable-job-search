"""Exercise the classic Jobs filter UI and paging in its browser JavaScript."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_jobs_filter_updates_preserve_controls_and_render_bounded_pages():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const match = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/);
assert.ok(match, 'classic Jobs script exists');
const source = match[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const elements = new Map();
const storage = new Map();
for (const id of ['#jobsList', '#jobsCount', '#activeFilters', '#moreFilterCount']) {
  elements.set(id, {innerHTML:'', textContent:'', dataset:{}});
}
const context = {
  window: {addEventListener() {}},
  document: {
    querySelector: selector => elements.get(selector) || {addEventListener() {}},
    querySelectorAll: () => [], activeElement:null,
    addEventListener() {},
  },
  localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  clearTimeout() {},
  Date, URL, console,
};
vm.createContext(context);
vm.runInContext(source, context);
context.fixtureJobs = Array.from({length:123}, (_, index) => ({
  id:`job-${index}`, company:`Company ${index}`, title:'Software Engineer', score:70,
  sector:index % 2 ? 'ai_lab' : 'big_tech', posted_at:Date.now()/1000-3600,
  locations:index % 2 ? ['San Francisco, CA'] : ['New York, NY'], posting:{},
}));
vm.runInContext(`
  S.jobs = Object.fromEntries(fixtureJobs.map(job => [job.id, job]));
  S.applied = [];
  S.web = {maybe:[], excluded:[], jobs:{}};
  S.filter = {...DEFAULT_FILTER, min:0, experience:'', bestWindow:'all'};
  S.jobsPage = 1;
`, context);
context.rowHTML = job => `<article>${job.id}</article>`;

const index = vm.runInContext('jobIndex()', context);
assert.equal(index.entries.length, 123);
assert.equal(vm.runInContext('jobIndex() === jobIndex()', context), true, 'index is reused for the same lane snapshot');
assert.equal(vm.runInContext('locationGroups().states.length', context), 2);

vm.runInContext('renderJobsList()', context);
let list = elements.get('#jobsList').innerHTML;
assert.equal((list.match(/<article>/g) || []).length, 50, 'first paint is bounded to 50 rows');
assert.match(elements.get('#jobsCount').textContent, /123 roles · 1–50/);
vm.runInContext('changeJobsPage(1)', context);
list = elements.get('#jobsList').innerHTML;
assert.equal((list.match(/<article>/g) || []).length, 50, 'second page also paints 50 rows');
assert.match(list, /job-50/);
assert.doesNotMatch(list, /job-0<\/article>/);

vm.runInContext("setFilter('q', 'Company 12')", context);
assert.equal(vm.runInContext('S.jobsPage', context), 1, 'a filter change returns to the first page');
assert.equal(JSON.parse(storage.get('jr_filters')).q, 'Company 12', 'filter selections remain saved');
assert.match(elements.get('#jobsCount').textContent, /4 roles/);
assert.match(elements.get('#activeFilters').innerHTML, /Company 12/);
vm.runInContext("clearFilterChip({dataset:{filterKey:'q',filterValue:'Company 12'}})", context);
assert.equal(vm.runInContext('S.filter.q', context), '');

context.render = () => { throw new Error('filtering must not rebuild the page shell'); };
vm.runInContext("setFilter('location', 'state:CA')", context);
assert.match(elements.get('#jobsCount').textContent, /61 roles/);
assert.match(elements.get('#activeFilters').innerHTML, /Location: California/);
vm.runInContext("clearFilterChip({dataset:{filterKey:'location',filterValue:'state:CA'}})", context);
assert.match(elements.get('#jobsCount').textContent, /123 roles/);
vm.runInContext("setFilter('bestWindow', 'all')", context);
assert.doesNotMatch(elements.get('#activeFilters').innerHTML, /Time:/, 'all time is not shown as a removable active filter');
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_jobs_toolbar_keeps_advanced_filters_collapsible_and_search_debounced():
    html = (ROOT / "webapp" / "index.html").read_text()
    assert 'primary.id = "jobsPrimaryFilters"' in html
    assert 'details.id = "jobsMoreFilters"' in html
    assert 'active.id = "activeFilters"' in html
    assert 'search.setAttribute("aria-label", "Search jobs")' in html
    assert "oninput=\"queueSearch(this.value)\"" in html
    assert "const JOBS_PAGE_SIZE = 50" in html
    jobs_list = html[html.index("function renderJobsList("):html.index("const PIPELINE_STAGES")]
    assert "show more (" not in jobs_list


def test_lane_jobs_load_is_deduplicated_and_can_start_with_internships():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const match = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/);
const source = match[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const calls = [];
const context = {
  window: {addEventListener() {}},
  document: {querySelector: () => ({addEventListener() {}}), querySelectorAll: () => [], addEventListener() {}},
  localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  clearTimeout() {},
  fetch: async url => {
    calls.push(url);
    return {ok:true, status:200, json:async () => ({})};
  },
  Date, URL, console,
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`
  S.lane = 'internship';
  S.jobs = S.lanes.internship.jobs;
`, context);
Promise.all([
  vm.runInContext("ensureLaneJobs('internship', true)", context),
  vm.runInContext("ensureLaneJobs('internship', true)", context),
]).then(([result]) => {
  assert.equal(result.ok, true);
  assert.equal(calls.length, 1, 'concurrent lane requests share one fetch');
  assert.match(calls[0], /state\/intern_jobs\.json$/);
  assert.equal(vm.runInContext("laneState('internship').ready", context), true);
  assert.equal(vm.runInContext("laneState('new_grad').ready", context), false, 'the inactive lane stays unloaded');
}).catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_closed_posting_detail_loads_existing_history_shard_once():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const match = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/);
const source = match[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const calls = [];
const context = {
  window:{addEventListener() {}},
  document:{querySelector:() => ({addEventListener() {}}), querySelectorAll:() => [], addEventListener() {}},
  localStorage:{getItem:() => null, setItem() {}, removeItem() {}},
  clearTimeout() {}, Date, URL, console,
  fetch:async url => { calls.push(url); return {ok:true, status:200, json:async () => ({
    closed:{id:'closed', company:'Acme', title:'Engineer', posting_status:'expired',
      score_reasons:['role fit +20'], lifecycle_events:[{status:'expired', at:300}]}
  })}; },
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`S.jobs = {closed:{id:'closed', company:'Acme', title:'Engineer', posting_status:'expired'}};
  S.lanes.new_grad.jobs = S.jobs;`, context);
Promise.all([
  vm.runInContext("ensureHistoryJobs('new_grad')", context),
  vm.runInContext("ensureHistoryJobs('new_grad')", context),
]).then(() => {
  assert.equal(calls.length, 1);
  assert.match(calls[0], /state\/jobs_history\.json$/);
  assert.equal(vm.runInContext("S.jobs.closed.score_reasons[0]", context), 'role fit +20');
  assert.equal(vm.runInContext("S.jobs.closed.lifecycle_events.length", context), 1);
}).catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
