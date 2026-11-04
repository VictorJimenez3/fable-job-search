"""Exercise internship scope, term, cohort, and failure states in the real board JavaScript."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_internship_scope_terms_and_mixed_cohorts_are_runtime_behaviors():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const match = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/);
assert.ok(match, 'classic Jobs script exists');
const source = match[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const now = Date.parse('2026-11-03T12:00:00Z');
const storage = new Map();
const context = {
  window: {addEventListener() {}},
  document: {querySelector: () => ({addEventListener() {}}), querySelectorAll: () => [], addEventListener() {}},
  localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  clearTimeout() {}, Date: class extends Date { static now() { return now; } }, URL, console, fixtureNow: now,
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`
  S.lane = 'internship';
  S.lanes.internship.expectedGraduation = '2027-05';
  S.jobs = {
        future: {id:'future', company:'A', title:'Software Engineering Intern', score:70, explicit_new_grad:true, early_career_possible:true, posted_at:fixtureNow, locations:['United States'], location_eligibility:{status:'us'}, internship_eligibility:{internship_signal:'title', term_options:['2027-01-01','2027-06-01'], class_years:['senior']}, source_board:'ats:a'},
        past: {id:'past', company:'B', title:'Data Scientist Intern', score:80, posted_at:fixtureNow, locations:['United States'], location_eligibility:{status:'us'}, internship_eligibility:{internship_signal:'title', term_options:['2026-06-01']}, source_board:'ats:b'},
        unknown: {id:'unknown', company:'C', title:'ML Intern', score:80, posted_at:fixtureNow, locations:['United States'], location_eligibility:{status:'us'}, internship_eligibility:{internship_signal:'body'}, source_board:'aggregator:c'},
        review: {id:'review', company:'D', title:'Software Engineer', score:80, posted_at:fixtureNow, locations:['United States'], location_eligibility:{status:'us'}, internship_eligibility:{internship_signal:'source_only', term_options:['2027-06-01']}, source_board:'aggregator:d'},
  };
  S.lanes.internship.jobs = S.jobs;
  S.applied = []; S.web = {maybe:[], excluded:[], jobs:{}};
  S.filter = {...DEFAULT_FILTER, min:0, experience:'', sort:'new', bestWindow:'all', internshipScope:'current'};
`, context);
// The mixed Spring/Summer terms match a May 2027 expected graduation on Spring.
assert.equal(vm.runInContext("internshipCohortMatch(S.jobs.future)", context), 'match');
assert.equal(vm.runInContext("internshipCohortMatch({internship_eligibility:{class_years:['junior'],term_options:['2027-06-01']}})", context), 'mismatch', 'a future term cannot classify an already graduated candidate as junior');
assert.equal(vm.runInContext("internshipEvidenceClean({internship_eligibility:{internship_signal:'title',employment_signal:'full_time_only'}})", context), false);
assert.equal(vm.runInContext("internshipEvidenceClean({internship_eligibility:{internship_signal:'title'},link_resolution:{status:'source_only'}})", context), false);
assert.equal(vm.runInContext("internshipEvidenceClean({...S.jobs.future,eligibility:'review'})", context), false);
assert.equal(vm.runInContext("JSON.stringify(internshipTermOptions().map(option => option.value))", context), JSON.stringify(['2026-06','2027-01','2027-06','unknown']));
assert.equal(vm.runInContext("JSON.stringify(jobList().map(job => job.id))", context), JSON.stringify(['unknown','future']), 'internship best match is score-led even when a row carries new-grad flags');
vm.runInContext("S.filter.internshipScope = 'all'", context);
assert.equal(vm.runInContext("JSON.stringify(jobList().map(job => job.id).sort())", context), JSON.stringify(['future','past','unknown']));
vm.runInContext("S.filter.internshipScope = 'review'", context);
assert.equal(vm.runInContext("JSON.stringify(jobList().map(job => job.id).sort())", context), JSON.stringify(['past','review']));
storage.set('jr_filters_internship', JSON.stringify({sort:'new', internshipScope:'all'}));
assert.equal(vm.runInContext("storedFilter('internship').internshipScope", context), 'all');
vm.runInContext("S.filter={...DEFAULT_FILTER,term:'2027-06',internshipScope:'all'}", context);
assert.equal(vm.runInContext("hasAdvancedJobFilters()", context), false, 'visible View and Term controls must not automatically expand More filters');
"""
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_internship_feed_failure_has_visible_retry_copy_for_every_viewer():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webapp/index.html', 'utf8');
const match = html.match(/<script>\s*"use strict";([\s\S]*?)<\/script>/);
const source = match[0].replace(/^<script>/, '').replace(/<\/script>$/, '').replace(/boot\(\);\s*$/, '');
const elements = new Map([['#main', {innerHTML:'', addEventListener() {}}]]);
const context = {
  window: {addEventListener() {}},
  document: {querySelector: selector => elements.get(selector) || {addEventListener() {}}, querySelectorAll: () => [], addEventListener() {}},
  localStorage: {getItem: () => null, setItem() {}, removeItem() {}}, clearTimeout() {}, Date, URL, console,
};
vm.createContext(context);
vm.runInContext(source, context);
vm.runInContext(`S.lane='internship'; S.boot.phase='jobs'; S.lanes.internship.ready=false; S.lanes.internship.loadError='503 Service Unavailable'; rJobs();`, context);
const output = elements.get('#main').innerHTML;
assert.match(output, /Internships are unavailable right now/);
assert.match(output, /retry internship board/);
assert.match(output, /503 Service Unavailable/);
"""
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
