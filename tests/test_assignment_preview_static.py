"""分配预检查的页面状态：核对中、失败、超时、旧结果不能盖住新选择。"""
import subprocess
import unittest

from tests.test_nav_static import STATIC


class AssignmentPreviewStaticTests(unittest.TestCase):
    def test_page_shows_checking_failure_timeout_and_ignores_stale_results(self):
        source = (STATIC / 'app.js').read_text()
        self.assertIn('const PREVIEW_TIMEOUT_MS = 30000;', source)
        self.assertIn('正在核对…', source)
        self.assertIn('重新核对', source)
        self.assertIn('你的选择还在', source)
        html = (STATIC / 'index.html').read_text()
        self.assertIn('id="action-note" aria-live="polite"', html)
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('skill_pilot/web/static/app.js', 'utf8');
const start = source.indexOf('function pendingChanges()');
const end = source.indexOf("$('skills-list').addEventListener", start);
const slice = source.slice(start, end);
const bars = [], toasts = [], calls = [];
let rejectOnAbort = true;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const plan = removed => ({
  marker: removed.join(','),
  base_assignments: {alpha: ['codex'], beta: ['codex']},
  assignments: {
    alpha: removed.includes('alpha') ? [] : ['codex'],
    beta: removed.includes('beta') ? [] : ['codex'],
  },
  changes: removed.map(skill => ({skill, agent: 'codex', enabled: false})),
  conflicts: [], auto_added: [], cell_notes: {},
});
const button = bar => bar.buttons.find(item => item.label.startsWith('应用') || item.label.startsWith('正在核对'));
const context = {
  bars, toasts, calls, plan, delay, button,
  state: {
    page: 'skills',
    table: {write_allowed: true, skills: [{name: 'alpha', title: '甲', requires: []}, {name: 'beta', title: '乙', requires: []}]},
    agents: [{id: 'codex', name: 'Codex'}],
    saved: {alpha: ['codex'], beta: ['codex']},
    draft: {alpha: ['codex'], beta: ['codex']},
    assignmentIntents: new Map(),
    skillIndex: new Map([['alpha', {name: 'alpha', title: '甲', requires: []}], ['beta', {name: 'beta', title: '乙', requires: []}]]),
    pending: null, autoAdded: new Map(), detail: null, jobsBlocked: '',
  },
  setTimeout, clearTimeout, AbortController,
  previewBusy: false, previewSequence: 0, previewProblem: null, previewController: null,
  assignmentSubmitting: false, submittingAssignmentKeys: new Set(), previewTimer: null,
  PREVIEW_DEBOUNCE_MS: 30, PREVIEW_TIMEOUT_MS: 500, writeBusy: null,
  document: {activeElement: null, querySelector() { return null; }},
  CSS: {escape: value => value},
  assignedNow: (name, agentId) => (context.state.saved[name] || []).includes(agentId),
  assignedAfter: (name, agentId) => (context.state.draft[name] || []).includes(agentId),
  autoAddedBy: (name, agentId) => context.state.autoAdded.get(name + '|' + agentId) || '',
  titleOf: name => context.state.skillIndex.get(name)?.title || name,
  swatch: () => '', escape: value => String(value ?? ''),
  jobsBlocked: () => Boolean(context.state.jobsBlocked),
  jobsHasActive: () => false, assignmentJobAt: () => null,
  renderSkillTable() {}, renderAgentSummary() {}, renderDetail() {},
  hideActionBar() { bars.push(null); },
  showActionBar(opts) { bars.push(opts); },
  toast(item) { toasts.push(item); },
  clone: value => JSON.parse(JSON.stringify(value)),
  api(path, body, extra) {
    return new Promise((resolve, reject) => {
      const item = {path, body, signal: extra && extra.signal, resolve, reject};
      calls.push(item);
      if (extra?.signal) extra.signal.addEventListener('abort', () => {
        item.aborted = true;
        if (rejectOnAbort) reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
      });
    });
  },
};
vm.createContext(context);
vm.runInContext(slice, context);
const fail = message => { console.error(message); process.exit(1); };
function reset() {
  const {state} = context;
  state.saved = {alpha: ['codex'], beta: ['codex']};
  state.draft = {alpha: ['codex'], beta: ['codex']};
  state.assignmentIntents = new Map();
  state.pending = null;
  state.autoAdded = new Map();
  context.previewBusy = false;
  context.previewProblem = null;
  context.previewController = null;
  clearTimeout(context.previewTimer);
  calls.length = 0; bars.length = 0; toasts.length = 0;
  rejectOnAbort = true;
}
(async () => {
  const {state} = context;
  const last = () => bars[bars.length - 1];
  reset();
  vm.runInContext("changeAssignment('alpha', 'codex', false); changeAssignment('beta', 'codex', false);", context);
  await delay(80);
  if (calls.length !== 1) fail('连着切换应合并成一次核对，实际 ' + calls.length);
  if (calls[0].path !== '/api/assignment/preview' || calls[0].body.recheck !== true) fail('核对请求不对');
  const sent = calls[0].body.changes.map(item => item.skill).sort().join(',');
  if (sent !== 'alpha,beta') fail('合并后的选择不对：' + sent);
  if (!last() || button(last()).label !== '正在核对…' || !button(last()).disabled) fail('核对过程中按钮应显示正在核对且不可点');
  if (state.assignmentIntents.size !== 2) fail('核对过程中选择被清掉了');
  calls[0].resolve(plan(['alpha', 'beta']));
  await delay(30);
  if (!button(last()).label.startsWith('应用更改') || button(last()).disabled) fail('核对成功后应可以应用');
  if (state.pending.marker !== 'alpha,beta') fail('成功结果没有留下');

  reset();
  rejectOnAbort = false;
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(80);
  const first = calls[0];
  vm.runInContext("changeAssignment('beta', 'codex', false)", context);
  await delay(80);
  if (calls.length !== 2) fail('慢核对还没结束时，新选择应再发一次');
  if (!first.signal.aborted) fail('旧核对没有停掉');
  calls[1].resolve(plan(['alpha', 'beta']));
  await delay(20);
  first.resolve(plan(['beta']));
  await delay(20);
  if (!state.pending || state.pending.marker !== 'alpha,beta') fail('旧的成功结果盖住了新核对');
  if (state.assignmentIntents.size !== 2) fail('新选择丢了');

  reset();
  rejectOnAbort = false;
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(80);
  const stale = calls[0];
  vm.runInContext("changeAssignment('beta', 'codex', false)", context);
  await delay(80);
  calls[1].resolve(plan(['alpha', 'beta']));
  await delay(20);
  stale.reject(new Error('旧的失败'));
  await delay(20);
  if (toasts.some(item => String(item.text).includes('旧的失败')) || String(last().note).includes('旧的失败')) fail('旧报错盖住了新结果');
  if (!state.pending || state.pending.marker !== 'alpha,beta') fail('旧报错清掉了新核对');

  reset();
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(80);
  calls.at(-1).reject(new Error('读取失败'));
  await delay(30);
  if (state.assignmentIntents.size !== 1 || (state.draft.alpha || []).includes('codex')) fail('失败后选择丢了');
  if (!String(last().note).includes('读取失败') || !String(last().note).includes('选择还在')) fail('失败没有写在托盘上：' + last().note);
  if (!button(last()).disabled) fail('失败后不能直接应用，要先重新核对');
  const retry = last().buttons.find(item => item.label === '重新核对');
  if (!retry) fail('失败后没有重新核对');
  retry.action();
  await delay(80);
  if (state.assignmentIntents.size !== 1) fail('重新核对把选择清掉了');
  calls.at(-1).resolve(plan(['alpha']));
  await delay(30);
  if (button(last()).disabled || !button(last()).label.startsWith('应用更改')) fail('重新核对成功后应可以应用');

  reset();
  context.PREVIEW_TIMEOUT_MS = 40;
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(140);
  if (!calls.length || !calls.at(-1).signal.aborted) fail('超时没有停掉核对');
  await delay(30);
  if (!String(last().note).includes('时间太长') || !String(last().note).includes('选择还在')) fail('超时没有说明：' + last().note);
  if (state.assignmentIntents.size !== 1 || (state.draft.alpha || []).includes('codex')) fail('超时后选择丢了');
  if (!button(last()).disabled || !last().buttons.some(item => item.label === '重新核对')) fail('超时后应能重新核对，且不能直接应用');

  context.PREVIEW_TIMEOUT_MS = 500;
  calls.length = 0;
  vm.runInContext('retryAssignmentPreview()', context);
  await delay(80);
  calls.at(-1).resolve({superseded: true});
  await delay(30);
  if (state.pending && state.pending.superseded) fail('过期核对被当成了新结果');
  if (!String(last().note).includes('打断') || state.assignmentIntents.size !== 1) fail('被打断后应留下选择并允许重新核对：' + last().note);
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run(['node', '-e', script], cwd=STATIC.parents[2], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_apply_discards_stale_success_failure_and_cancel(self):
        script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync('skill_pilot/web/static/app.js', 'utf8');
const start = source.indexOf('function pendingChanges()');
const end = source.indexOf("$('skills-list').addEventListener", start);
const slice = source.slice(start, end);
const bars = [], toasts = [], calls = [], confirms = [], jobs = [];
let rejectOnAbort = true, confirmAnswer = true;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const plan = removed => ({
  marker: removed.join(','),
  base_assignments: {alpha: ['codex'], beta: ['codex']},
  assignments: {
    alpha: removed.includes('alpha') ? [] : ['codex'],
    beta: removed.includes('beta') ? [] : ['codex'],
  },
  changes: removed.map(skill => ({skill, agent: 'codex', enabled: false})),
  conflicts: [], auto_added: [], cell_notes: {}, dependency_warnings: [], path_warnings: [],
});
const names = value => (value || []).map(item => item.skill || item.name).sort().join(',');
const context = {
  bars, toasts, calls, confirms, jobs, plan, delay,
  state: {
    page: 'skills',
    table: {write_allowed: true, skills: [{name: 'alpha', title: '甲', requires: []}, {name: 'beta', title: '乙', requires: []}]},
    agents: [{id: 'codex', name: 'Codex'}],
    saved: {alpha: ['codex'], beta: ['codex']},
    draft: {alpha: ['codex'], beta: ['codex']},
    assignmentIntents: new Map(),
    skillIndex: new Map([['alpha', {name: 'alpha', title: '甲', requires: []}], ['beta', {name: 'beta', title: '乙', requires: []}]]),
    pending: null, autoAdded: new Map(), detail: null, jobsBlocked: '',
  },
  setTimeout, clearTimeout, AbortController,
  previewBusy: false, previewSequence: 0, previewProblem: null, previewController: null,
  assignmentSubmitting: false, submittingAssignmentKeys: new Set(), previewTimer: null,
  PREVIEW_DEBOUNCE_MS: 20, PREVIEW_TIMEOUT_MS: 500, writeBusy: null,
  document: {activeElement: null, querySelector() { return null; }},
  CSS: {escape: value => value},
  $: () => ({tabIndex: 0, focus() {}}),
  assignedNow: (name, agentId) => (context.state.saved[name] || []).includes(agentId),
  assignedAfter: (name, agentId) => (context.state.draft[name] || []).includes(agentId),
  autoAddedBy: () => '', titleOf: name => name, swatch: () => '', nameOf: id => id,
  escape: value => String(value ?? ''),
  jobsBlocked: () => false, jobsHasActive: () => false, assignmentJobAt: () => null,
  renderSkillTable() {}, renderAgentSummary() {}, renderDetail() {},
  hideActionBar() {}, showActionBar(opts) { bars.push(opts); },
  toast(item) { toasts.push(item); },
  clone: value => JSON.parse(JSON.stringify(value)),
  withBusy(button, label, work) { return work(); },
  confirmDialog(options) { confirms.push(options); return Promise.resolve(confirmAnswer); },
  submitJob(parameters) { jobs.push(parameters); return Promise.resolve({job: {id: 'job'}}); },
  api(path, body, extra) {
    return new Promise((resolve, reject) => {
      const item = {path, body, signal: extra && extra.signal, resolve, reject};
      calls.push(item);
      if (extra?.signal) extra.signal.addEventListener('abort', () => {
        item.aborted = true;
        if (rejectOnAbort) reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
      });
    });
  },
};
vm.createContext(context);
vm.runInContext(slice, context);
const fail = message => { console.error(message); process.exit(1); };
function reset() {
  const {state} = context;
  state.saved = {alpha: ['codex'], beta: ['codex']};
  state.draft = {alpha: ['codex'], beta: ['codex']};
  state.assignmentIntents = new Map();
  state.pending = null;
  state.autoAdded = new Map();
  context.previewBusy = false;
  context.previewProblem = null;
  context.previewController = null;
  context.assignmentSubmitting = false;
  clearTimeout(context.previewTimer);
  calls.length = 0; bars.length = 0; toasts.length = 0; confirms.length = 0; jobs.length = 0;
  rejectOnAbort = true; confirmAnswer = true; context.PREVIEW_TIMEOUT_MS = 500;
}
const {state} = context;
const last = () => bars[bars.length - 1];
const withdrawn = skill => !(state.draft[skill] || []).includes('codex');
(async () => {
  reset();
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(60);
  calls.at(-1).resolve(plan(['alpha']));
  await delay(30);
  const applying = vm.runInContext('applyAssignment({})', context);
  await delay(20);
  const applyCall = calls.at(-1);
  vm.runInContext("changeAssignment('beta', 'codex', false)", context);
  await delay(60);
  calls.at(-1).resolve(plan(['alpha', 'beta']));
  await delay(40);
  await applying;
  if (toasts.some(item => String(item.text).includes('时间太长'))) fail('取消上一次应用被说成核对时间太长');
  if (String(last().note).includes('时间太长')) fail('新核对成功后仍留着超时说明：' + last().note);
  if (!withdrawn('alpha') || !withdrawn('beta') || state.assignmentIntents.size !== 2) fail('第二次撤下没有留住');
  if (state.pending?.marker !== 'alpha,beta') fail('新核对结果丢了');

  reset();
  rejectOnAbort = false;
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(60);
  calls.at(-1).resolve(plan(['alpha']));
  await delay(30);
  const staleApply = vm.runInContext('applyAssignment({})', context);
  await delay(20);
  const staleCall = calls.at(-1);
  vm.runInContext("changeAssignment('beta', 'codex', false)", context);
  await delay(60);
  calls.at(-1).resolve(plan(['alpha', 'beta']));
  await delay(30);
  staleCall.resolve(plan(['alpha']));
  await delay(40);
  await staleApply;
  if (state.pending?.marker !== 'alpha,beta') fail('旧的成功结果盖住了新选择：' + state.pending?.marker);
  if (!withdrawn('alpha') || !withdrawn('beta')) fail('旧计划把新的撤下改回去了');
  if (confirms.length || jobs.length) fail('过期核对仍然打开了确认或提交');

  reset();
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(60);
  calls.at(-1).resolve(plan(['alpha']));
  await delay(30);
  const failed = vm.runInContext('applyAssignment({})', context);
  await delay(20);
  calls.at(-1).reject(new Error('读取失败'));
  await delay(30);
  await failed;
  if (!toasts.some(item => String(item.text).includes('读取失败')) || !withdrawn('alpha')) fail('这次应用自己的失败没有留下选择');
  if (confirms.length || jobs.length) fail('失败后不应确认或提交');

  reset();
  context.PREVIEW_TIMEOUT_MS = 40;
  vm.runInContext("changeAssignment('alpha', 'codex', false)", context);
  await delay(60);
  calls.at(-1).resolve(plan(['alpha']));
  await delay(30);
  const timed = vm.runInContext('applyAssignment({})', context);
  await delay(80);
  await timed;
  if (!toasts.some(item => String(item.text).includes('时间太长')) || !withdrawn('alpha')) fail('这次应用自己超时后应说明并留下选择');

  reset();
  vm.runInContext("changeAssignment('alpha', 'codex', false); changeAssignment('beta', 'codex', false);", context);
  await delay(60);
  calls.at(-1).resolve(plan(['alpha', 'beta']));
  await delay(30);
  const ok = vm.runInContext('applyAssignment({})', context);
  await delay(20);
  calls.at(-1).resolve(plan(['alpha', 'beta']));
  await delay(40);
  await ok;
  if (confirms.length !== 1) fail('当前核对应确认一次，实际 ' + confirms.length);
  const confirmed = names(confirms[0].groups.flatMap(group => group.items));
  const sent = names(jobs[0] && jobs[0].changes);
  if (confirmed !== 'alpha,beta' || sent !== 'alpha,beta') fail('确认和提交不一致：' + confirmed + ' / ' + sent);
  if (state.assignmentIntents.size) fail('已经提交的选择不应留在托盘里');
})().catch(error => { console.error(error); process.exit(1); });
"""
        result = subprocess.run(['node', '-e', script], cwd=STATIC.parents[2], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
