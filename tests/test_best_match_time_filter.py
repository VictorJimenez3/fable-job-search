"""Exercise the classic Jobs filter in its actual browser JavaScript."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_best_match_window_uses_the_visible_posting_age():
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
  window: {addEventListener() {}},
  document: {querySelector: () => ({addEventListener() {}}), addEventListener() {}},
  localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  clearTimeout() {},
  Date: class extends Date { static now() { return now * 1000; } },
  URL, console,
};
vm.createContext(context);
vm.runInContext(source, context);
const job = (id, posted_at, first_seen, score = 60) => ({
  id, company: 'Example', title: 'Software Engineer', score,
  posted_at, first_seen, career_priority: 2,
});
const jobs = [
  job('recent-post', now - 1800, now - 7200),
  job('old-post-new-discovery', now - 3 * 86400, now - 1800, 99),
  job('old-saved', now - 3 * 86400, now - 1800, 98),
  job('missing-post', null, now - 1800),
  job('millisecond-post', (now - 3600) * 1000, now - 7200),
  job('iso-post', new Date((now - 7200) * 1000).toISOString(), now - 7200),
  job('future-post', now + 86400, now - 1800),
  job('ancient-post', now - 2 * 365 * 86400, now - 2 * 365 * 86400),
];
context.fixtureJobs = jobs;
vm.runInContext(`
  S.jobs = Object.fromEntries(fixtureJobs.map(j => [j.id, j]));
  S.applied = [{id: 'old-saved', stage: 'saved'}];
  S.web = {maybe: [], excluded: [], jobs: {}};
  S.filter = {...DEFAULT_FILTER, min: 0, experience: '', bestWindow: '86400', sort: 'best'};
`, context);
const best = vm.runInContext('jobList().map(j => j.id)', context);
assert.deepEqual(new Set(best), new Set(['recent-post', 'missing-post', 'millisecond-post', 'iso-post']));
vm.runInContext("S.filter.bestWindow = 'all'", context);
const all = vm.runInContext('jobList().map(j => j.id)', context);
assert.ok(all.includes('ancient-post'), 'all time includes old open postings');
assert.equal(vm.runInContext('postedTs(S.jobs["missing-post"])', context), now - 1800);
"""
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
