import itertools
import json
import subprocess
import unittest
from pathlib import Path

from tests import test_operations
from skill_pilot import agents, operations, profiles, store
from skill_pilot.sync import visibility


class EnrollmentGroupsTests(unittest.TestCase):
    def test_every_four_reader_topology_and_managed_subset(self):
        ids = ['alpha', 'beta', 'gamma', 'delta']
        pairs = list(itertools.combinations(ids, 2))
        for edges in itertools.product([False, True], repeat=len(pairs)):
            catalog = [profiles.custom(a, a.title(), f'.{a}/skills') for a in ids]
            neighbors = {a: set() for a in ids}
            for number, ((left, right), enabled) in enumerate(zip(pairs, edges)):
                if enabled:
                    neighbors[left].add(right)
                    neighbors[right].add(left)
                    for profile in catalog:
                        if profile['id'] in (left, right):
                            profile['skill_roots'].append(f'.shared-{number}/skills')
            for flags in itertools.product([False, True], repeat=len(ids)):
                already = {a for a, enabled in zip(ids, flags) if enabled}
                with self.subTest(edges=edges, already=already):
                    groups = profiles.enrollment_groups(catalog, already)
                    self.assertEqual(sum(map(len, groups)), len({a for g in groups for a in g}))
                    for seed in set(ids) - already:
                        expected, queue = {seed}, [seed]
                        while queue:
                            for other in neighbors[queue.pop()] - already - expected:
                                expected.add(other)
                                queue.append(other)
                        actual = next((set(g) for g in groups if seed in g), {seed})
                        self.assertEqual(actual, expected)
                        profiles.require_shared(list(expected), catalog, already)
                        if len(expected) > 1:
                            with self.assertRaisesRegex(ValueError, '一起选择'):
                                profiles.require_shared([seed], catalog, already)


class AgentEnrollmentTests(unittest.TestCase):
    setUp = test_operations.OperationsTests.setUp
    skill = test_operations.OperationsTests.skill

    def readers(self, definitions, installed=None):
        local = profiles.state(self.home)
        for identifier, roots in definitions.items():
            profile = profiles.custom(identifier, identifier.title(), roots[0])
            profile['skill_roots'] = roots
            profile['detect']['config_dirs'] = ['.installed-' + identifier]
            local.setdefault('custom', []).append(profile)
            if installed is None or identifier in installed:
                (self.home / profile['detect']['config_dirs'][0]).mkdir(exist_ok=True)
        (self.home / '.skill-pilot/agents.json').write_text(json.dumps(local))

    def fingerprint(self):
        return test_operations.OperationsTests.fingerprint(self) | {
            r: store.physical_state(self.home / r)
            for p in profiles.all_profiles(self.home) for r in p['skill_roots']}

    def test_cursor_joins_without_reenrolling_existing_readers(self):
        (self.home / 'Applications/Cursor.app').mkdir(parents=True)
        operations.apply_assign([{'skill': 'other', 'agent': 'codex', 'enabled': False},
                                 {'skill': 'other', 'agent': 'dsh', 'enabled': True}],
                                confirmed=True, home=self.home)
        before, visible = self.fingerprint(), visibility(self.home)
        self.assertIn(['codex', 'dsh', 'cursor'], profiles.shared_groups(profiles.all_profiles(self.home), self.home))
        self.assertEqual(agents.overview(self.home)['must_select_together'], [])
        plan = agents.preview_join(['cursor'], self.home)
        self.assertEqual(plan['agents'], ['cursor'])
        self.assertEqual(self.fingerprint(), before)
        joined = agents.join(['cursor'], plan['plan_id'], home=self.home)
        self.assertEqual(visibility(self.home), visible)
        self.assertEqual(joined['agents'], ['cursor'])
        self.assertEqual(operations.history(self.home)[0]['message'], '接入了 Cursor')
        self.assertEqual(profiles.state(self.home)['onboarded'].count('codex'), 1)
        self.assertEqual(profiles.state(self.home)['onboarded'].count('dsh'), 1)
        operations.undo(joined['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_one_request_plans_complete_chain_but_not_independent_readers(self):
        self.readers({'alpha': ['.left/skills'], 'beta': ['.left/skills', '.right/skills'],
                      'gamma': ['.right/skills'], 'delta': ['.separate/skills']})
        self.skill('.left/skills/left', 'left')
        self.skill('.right/skills/right', 'right')
        self.skill('.separate/skills/separate', 'separate')
        local = profiles.state(self.home)
        local['ignored'].append('gamma')
        (self.home / '.skill-pilot/agents.json').write_text(json.dumps(local))
        before, visible = self.fingerprint(), visibility(self.home)
        self.assertEqual(agents.overview(self.home)['must_select_together'], [['alpha', 'beta', 'gamma']])
        plan = agents.preview_join(['alpha'], self.home)
        self.assertEqual(plan['agents'], ['alpha', 'beta', 'gamma'])
        self.assertEqual(plan['roots'], ['.left/skills', '.right/skills'])
        self.assertEqual(self.fingerprint(), before)
        joined = agents.join(['alpha'], plan['plan_id'], home=self.home)
        self.assertEqual(visibility(self.home), visible)
        self.assertNotIn('delta', profiles.state(self.home)['onboarded'])
        self.assertNotIn('gamma', profiles.state(self.home)['ignored'])
        with self.assertRaisesRegex(ValueError, '已经接入'):
            agents.preview_join(['alpha'], self.home)
        operations.undo(joined['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_managed_reader_does_not_pull_in_its_other_unmanaged_neighbors(self):
        self.readers({'beta': ['.left/skills', '.right/skills']})
        plan = agents.preview_join(['beta'], self.home)
        agents.join(['beta'], plan['plan_id'], home=self.home)
        self.readers({'alpha': ['.left/skills'], 'gamma': ['.right/skills']})
        self.assertEqual(agents.overview(self.home)['must_select_together'], [])
        before = self.fingerprint()
        plan = agents.preview_join(['alpha'], self.home)
        self.assertEqual(plan['agents'], ['alpha'])
        joined = agents.join(['alpha'], plan['plan_id'], home=self.home)
        self.assertNotIn('gamma', profiles.state(self.home)['onboarded'])
        operations.undo(joined['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_manual_reader_uses_the_same_enrollment_rules(self):
        self.readers({'alpha': ['.joint/skills']})
        request = [{'id': 'manual', 'name': '手动工具', 'skill_root': '.joint/skills'}]
        before = self.fingerprint()
        plan = agents.preview_join(request, self.home)
        self.assertEqual(plan['agents'], ['manual', 'alpha'])
        joined = agents.join(request, plan['plan_id'], home=self.home)
        self.assertEqual(operations.history(self.home)[0]['agents'], ['alpha', 'manual'])
        operations.undo(joined['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_explicit_requests_for_managed_agents_still_fail_without_writing(self):
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, 'Codex 已经接入'):
            agents.preview_join(['codex'], self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_newly_detected_shared_reader_invalidates_old_preview(self):
        self.readers({'alpha': ['.joint/skills'], 'beta': ['.joint/skills']}, installed={'alpha'})
        plan = agents.preview_join(['alpha'], self.home)
        self.assertEqual(plan['agents'], ['alpha'])
        (self.home / '.installed-beta').mkdir()
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '重新预览'):
            agents.join(['alpha'], plan['plan_id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(agents.preview_join(['alpha'], self.home)['agents'], ['alpha', 'beta'])

    def test_automatically_included_readers_still_require_safe_roots(self):
        self.readers({'alpha': ['.joint/skills'], 'beta': ['.joint/skills', '.unsafe/skills']})
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + '-outside')
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.home / '.unsafe').mkdir()
        (self.home / '.unsafe/skills').symlink_to(outside, target_is_directory=True)
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '主目录内'):
            agents.preview_join(['alpha'], self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_first_setup_uses_complete_groups_and_keeps_partial_selection_guard(self):
        self.readers({'alpha': ['.left/skills'], 'beta': ['.left/skills', '.right/skills'],
                      'gamma': ['.right/skills']})
        new_home = self.home / 'First User Home'
        (new_home / '.skill-pilot').mkdir(parents=True)
        local = {'onboarded': [], 'ignored': [], 'custom': profiles.state(self.home)['custom']}
        (new_home / '.skill-pilot/agents.json').write_text(json.dumps(local))
        for p in local['custom']:
            (new_home / p['detect']['config_dirs'][0]).mkdir()
        self.assertEqual(agents.overview(new_home)['must_select_together'], [['alpha', 'beta', 'gamma']])
        with self.assertRaisesRegex(ValueError, '一起选择'):
            store.preview(new_home, ['alpha', 'beta'])
        plan = store.preview(new_home, ['alpha', 'beta', 'gamma'])
        store.apply(plan['plan_id'], new_home, plan['agents'])
        self.assertEqual(profiles.state(new_home)['onboarded'], ['alpha', 'beta', 'gamma'])


class EnrollmentClickTests(unittest.TestCase):
    def test_preview_and_apply_preserve_manual_requests_and_use_plan_names(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('skill_pilot/web/static/app.js', 'utf8');
const elements = {}, listeners = {}, sent = [];
const $ = id => elements[id] ||= {
  addEventListener: (type, callback) => { listeners[id] = callback; },
  showModal() {}, close() {}
};
const plan = {agents: ['manual', 'alpha'], profiles: [
  {id:'manual', name:'手动工具', skill_roots:['.joint/skills']},
  {id:'alpha', name:'关联工具', skill_roots:['.joint/skills']}
], skills:[], plan_id:'plan', revision:'revision'};
const context = vm.createContext({$, writeBusy:null, document:{querySelectorAll: () => []},
  api: async (path, body) => { sent.push({path, body}); return JSON.parse(JSON.stringify(plan)); },
  runWrite: async options => options.work(), refreshEverything: async () => {},
  JOIN_ACTIONS: [], joinRows: () => [], joinChoiceRow: () => '', rootsLabel: roots => roots.join('、')});
vm.runInContext('let joinPlan = null;\n' +
  source.slice(source.indexOf('async function showJoinPreview('), source.indexOf("$('agent-add-submit')")) +
  source.slice(source.indexOf('function renderJoin('), source.indexOf("$('join-close')")) +
  source.slice(source.indexOf("$('join-apply').addEventListener"), source.indexOf('/* ==================== 技能详情')), context);
(async () => {
  await vm.runInContext("showJoinPreview([{name:'手动工具', skill_root:'.joint/skills'}])", context);
  assert.strictEqual(elements['join-message'].textContent, '0 个技能会登记给 手动工具、关联工具。');
  await listeners['join-apply']({currentTarget:{}});
  assert.strictEqual(sent[1].path, '/api/agents/join/apply');
  assert.strictEqual(JSON.stringify(sent[1].body.agents), JSON.stringify(sent[0].body.agents));
  assert.strictEqual(sent[1].body.plan_id, 'plan');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(['node', '-e', script], cwd=store.PROJECT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_join_click_only_submits_the_users_intent(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('skill_pilot/web/static/app.js', 'utf8');
const start = source.indexOf("for (const id of ['rail-notes', 'agents-suggested', 'agents-ignored'])");
const listeners = {}, submitted = [];
vm.runInNewContext(source.slice(start, source.indexOf('const JOIN_ACTIONS', start)), {
  $: id => ({addEventListener: (type, callback) => { listeners[id] = callback; }}),
  joinAgent: (id, button) => { assert.strictEqual(button.dataset.agentJoin, id); submitted.push(id); },
});
for (const [index, id] of ['cursor', 'arbitrary-tool', 'ignored-tool'].entries()) {
  listeners[['rail-notes', 'agents-suggested', 'agents-ignored'][index]]({target: {
    closest: selector => selector === '[data-agent-join]' ? {dataset: {agentJoin: id}} : null
  }});
}
assert.deepStrictEqual(submitted, ['cursor', 'arbitrary-tool', 'ignored-tool']);
"""
        result = subprocess.run(['node', '-e', script], cwd=store.PROJECT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_click_join_progress_defaults_failure_retry_and_undo(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('skill_pilot/web/static/app.js', 'utf8');
const flow = source.slice(source.indexOf('async function joinAgent('), source.indexOf('async function showJoinPreview('));
const button = source.slice(source.indexOf('function agentJoinButton('), source.indexOf('function renderRailNotes('));
const names = source.slice(source.indexOf('function nameOf('), source.indexOf('function homePath('));
const sent = [], stages = [], errors = [], notices = [];
let modal = 0, renderedPlan = null, refreshes = 0, release;
const agent = {id:'new', name:'新工具<&'};
let plan = {skills:[{action:'choose', options:[{id:'store'}, {id:'incoming'}]}], plan_id:'p', revision:'r'};
let fail = '', paused = false;
const context = vm.createContext({
  state:{table:{write_allowed:true}, agents:[]}, writeBusy:null, joiningAgent:null, joinPlan:null,
  escape: value => String(value).replace(/</g, '&lt;').replace(/&(?!(lt;))/g, '&amp;'),
  $: () => ({showModal: () => modal++}),
  api: async (path, body) => {
    sent.push({path, body});
    if (paused) { paused = false; await new Promise(resolve => { release = resolve; }); }
    if (fail === path) { fail = ''; throw new Error('读取失败'); }
    return path.endsWith('preview') ? plan : {id:'operation', message:'接入完成'};
  },
  renderRailNotes: () => stages.push(vm.runInContext('agentJoinButton', context)(agent)),
  renderAgents() {}, setTask() {},
  refreshEverything: async () => refreshes++, refreshAgents: async () => refreshes++,
  renderJoin: value => { renderedPlan = value; },
  undoOperation: (...args) => args,
  runWrite: async options => {
    assert.ok(options.taskText.includes(agent.name));
    context.writeBusy = options.busyText;
    try {
      const result = await options.work();
      const notice = options.success(result);
      if (notice) notices.push(notice);
      return result;
    } catch (error) { errors.push(error.message); return null; }
    finally { context.writeBusy = null; }
  },
});
vm.runInContext(names + '\n' + button + '\n' + flow, context);
vm.runInContext('indexCatalog(' + JSON.stringify({ignored:[agent]}) + ')', context);
const join = () => vm.runInContext("joinAgent('new', {})", context);
(async () => {
  paused = true;
  const first = join();
  await join(); // 重复点击不能发第二次请求。
  assert.strictEqual(sent.length, 1);
  assert.match(stages[0], /正在读取/);
  assert.match(stages[0], /aria-busy="true".* disabled/s);
  release();
  await first;
  assert.strictEqual(sent.length, 2);
  assert.strictEqual(sent[1].path, '/api/agents/join/apply');
  assert.strictEqual(JSON.stringify(sent[1].body), JSON.stringify({agents:['new'], plan_id:'p', revision:'r', confirmed:true}));
  assert.strictEqual(modal, 0);
  assert.ok(stages.some(html => html.includes('正在接入')));
  assert.strictEqual(refreshes, 2);
  assert.strictEqual(notices[0].undo('button')[0], 'operation');
  assert.match(vm.runInContext('agentJoinButton', context)(agent), /新工具&lt;&amp;/);
  assert.doesNotMatch(vm.runInContext('agentJoinButton', context)(agent), / disabled/);

  for (const path of ['/api/agents/join/preview', '/api/agents/join/apply']) {
    const before = sent.length;
    fail = path;
    await join();
    assert.strictEqual(context.joiningAgent, null);
    assert.strictEqual(sent.length - before, path.endsWith('preview') ? 1 : 2);
    assert.strictEqual(notices.length, path.endsWith('preview') ? 1 : 2);
    await join();
  }
  assert.strictEqual(errors.length, 2);
  assert.strictEqual(notices.length, 3);
  context.state.table.write_allowed = false;
  const before = sent.length;
  await join();
  assert.strictEqual(sent.length, before);
  assert.match(vm.runInContext('agentJoinButton', context)(agent), / disabled/);
  context.state.table.write_allowed = true;

  plan = {...plan, skills:[{action:'choose', options:[{id:'first'}, {id:'second'}]}]};
  await join();
  assert.strictEqual(sent.length, before + 1);
  assert.strictEqual(modal, 1);
  assert.strictEqual(JSON.stringify(renderedPlan.requests), '["new"]');
  assert.strictEqual(notices.length, 3);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(['node', '-e', script], cwd=store.PROJECT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
