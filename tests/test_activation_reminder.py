"""Tests for Issue #218: config-driven new-session activation reminders."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from tests import set_up_home
from tests.test_nav_static import STATIC, extract_function
from tests.test_team_static import block
from skill_pilot import agents as agent_management
from skill_pilot import cleanup, operations, profiles, store, updates
from skill_pilot.platform_support import rmtree
from skill_pilot.scan import content_digest, scan
from skill_pilot.web.server import Dashboard


class ActivationProfileTests(unittest.TestCase):
    def test_catalog_and_profiles_activation_unified(self):
        all_builtin = profiles.PROFILES + profiles.ADDITIONAL_PROFILES
        expected_notes = {'immediate': '即时生效', 'new_session': '需新开对话生效'}
        for profile in all_builtin:
            mode = profile['activation']
            self.assertIn(mode, expected_notes)
            self.assertEqual(profile['activation_note'], expected_notes[mode], profile['id'])
            self.assertEqual(profiles.activation_text(profile), expected_notes[mode], profile['id'])
            self.assertEqual(profiles.activation_note(profile), expected_notes[mode], profile['id'])

        custom_profile = profiles.custom('mytool', '自定义助手', '.mytool/skills')
        self.assertEqual(custom_profile['activation'], 'new_session')
        self.assertEqual(custom_profile['activation_note'], '需新开对话生效')
        self.assertEqual(profiles.activation_text(custom_profile), '需新开对话生效')

        legacy_custom = {**custom_profile, 'activation_note': ''}
        self.assertEqual(profiles.activation_note(legacy_custom), '需新开对话生效')

        self.assertEqual(
            profiles.new_session_reminder([{'id': 'antigravity', 'name': 'Antigravity'}]),
            'Antigravity 需新开对话才会加载新技能',
        )
        self.assertEqual(
            profiles.new_session_reminder(
                [{'id': 'codex', 'name': 'Codex'}, {'id': 'antigravity', 'name': 'Antigravity'}],
                action='update',
            ),
            'Codex、Antigravity 需新开对话才会加载新版技能',
        )
        self.assertEqual(profiles.new_session_reminder([]), '')


class ActivationReminderJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot activation ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'User Home With Spaces')
        curated = patch.object(operations, 'curated_dependencies', return_value=({}, {}))
        curated.start()
        self.addCleanup(curated.stop)
        for name in ('acme-a', 'acme-b', 'acme-c'):
            self.skill(self.home / '.codex/skills' / name, name)
        store.apply(store.preview(self.home)['plan_id'], self.home)
        self.registry(lambda r: [r['skills'][n].update(source={
            'source': 'https://github.com/Acme/skills.git', 'repository': 'Acme/skills',
            'source_path': 'skills/' + n + '/SKILL.md'}) for n in r['skills']])
        operations._commit(self.home, operations.load(self.home)[1], [], [], 'Activation fixture')
        self.server = Dashboard(scan(self.home))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        for review in [*self.server.install_reviews.values(), *self.server.update_reviews.values()]:
            if review.get('temporary'):
                rmtree(review['temporary'], ignore_errors=True)

    def skill(self, path, name, body='原内容'):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例技能\n---\n{body}\n')
        return path

    def registry(self, action):
        path = self.home / '.skill-pilot/registry.json'
        data = json.loads(path.read_text())
        action(data)
        path.write_text(json.dumps(data))

    def request(self, path, body=None, timeout=15 if os.name == 'nt' else 5):
        headers = {
            'X-Skill-Pilot-Token': self.server.token,
            'Origin': self.server.origin,
            'Content-Type': 'application/json',
        }
        req = urllib.request.Request(
            self.server.origin + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def ok(self, path, body=None):
        status, result = self.request(path, body)
        self.assertEqual(status, 200, result)
        return result

    def submit(self, kind, **parameters):
        return self.ok('/api/jobs', {'kind': kind, 'confirmed': True, **parameters})['job']

    def wait_jobs(self):
        deadline = time.monotonic() + (30 if os.name == 'nt' else 8)
        while time.monotonic() < deadline:
            result = self.ok('/api/jobs')
            if all(i['status'] not in {'waiting', 'running'} for i in result['items']) and not self.server.jobs_busy():
                return result['items']
            time.sleep(.01)
        self.fail('任务没有按时完成：' + str(result))

    def assignment_params(self, changes):
        plan = operations.preview_assign(changes, self.home, recheck=True)
        return {'changes': changes, 'seen': {k: plan[k] for k in ('changes', 'dependency_warnings', 'path_warnings')}}

    def install_review(self, name='acme-new'):
        temporary = self.root / ('install ' + name)
        path = self.skill(temporary / name, name)
        review = {
            'id': 'review-' + name,
            'temporary': str(temporary),
            'skills': [{
                'name': name,
                'path': str(path),
                'digest': content_digest(path),
                'description': '新安装技能',
                'repository_path': 'skills/' + name,
                'source': {'source': 'https://github.com/Acme/skills.git', 'repository': 'Acme/skills',
                           'source_path': 'skills/' + name + '/SKILL.md'},
            }],
        }
        self.server.install_reviews[review['id']] = review
        return review

    def update_review(self, *names):
        review = updates.create(self.home)
        self.server.update_reviews[review['id']] = review
        registry = operations.load(self.home)[1]['skills']
        rows = []
        for name in names:
            upstream = self.skill(Path(review['temporary']) / name, name, body='上游新版内容')
            tree_sha = store.git_tree(upstream)
            rows.append({
                'name': name,
                'has_update': True,
                'status': '有更新',
                'local_modified': False,
                'your_changes': {},
                'source': {'kind': 'github', 'repository': 'Acme/skills', 'path': 'skills/' + name},
                'commit': '1' * 40,
                'tree_sha': tree_sha,
                'normalized_tree_sha': tree_sha,
                '_upstream_path': str(upstream),
                '_state': updates.skill_state(self.home, name, registry[name]),
            })
        review.update(running=False, skills=rows, done=len(rows), update_count=len(rows), remaining=0,
                      _groups={'Acme/skills': [(n, registry[n], {}) for n in names]})
        return review

    def test_table_and_agents_expose_activation_mode_and_unified_text(self):
        table = self.ok('/api/table')
        by_id = {agent['id']: agent for agent in table['agents']}
        self.assertEqual(by_id['claude']['activation_mode'], 'immediate')
        self.assertEqual(by_id['claude']['activation_text'], '即时生效')
        self.assertEqual(by_id['dsh']['activation_mode'], 'immediate')
        self.assertEqual(by_id['antigravity']['activation_mode'], 'new_session')
        self.assertEqual(by_id['antigravity']['activation_text'], '需新开对话生效')
        self.assertEqual(by_id['antigravity']['activation'], '需新开对话生效')

        overview = self.ok('/api/agents')
        managed = {agent['id']: agent for agent in overview['managed']}
        self.assertEqual(managed['claude']['activation_mode'], 'immediate')
        self.assertEqual(managed['antigravity']['activation_mode'], 'new_session')
        self.assertEqual(managed['antigravity']['activation_text'], '需新开对话生效')

    def test_assignment_job_immediate_new_session_mixed_and_withdraw(self):
        # 1. Pure immediate (Claude + DSH): no new_session_agents reminder.
        self.submit('assignment', **self.assignment_params([
            {'skill': 'acme-a', 'agent': 'claude', 'enabled': True},
            {'skill': 'acme-a', 'agent': 'dsh', 'enabled': True},
        ]))
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job.get('new_session_agents', []), [])

        # 2. Pure new_session (Antigravity): lists Antigravity only.
        self.submit('assignment', **self.assignment_params([
            {'skill': 'acme-a', 'agent': 'antigravity', 'enabled': True},
        ]))
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual([a['id'] for a in job.get('new_session_agents', [])], ['antigravity'])
        self.assertEqual(job['new_session_agents'][0]['name'], 'Antigravity')

        # 3. Mixed enable (Claude + Antigravity) + withdraw from Codex: only Antigravity.
        self.submit('assignment', **self.assignment_params([
            {'skill': 'acme-b', 'agent': 'claude', 'enabled': True},
            {'skill': 'acme-b', 'agent': 'antigravity', 'enabled': True},
            {'skill': 'acme-b', 'agent': 'codex', 'enabled': False},
        ]))
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual([a['id'] for a in job.get('new_session_agents', [])], ['antigravity'])

        # 4. Withdraw-only on new_session agent: no reminder.
        self.submit('assignment', **self.assignment_params([
            {'skill': 'acme-c', 'agent': 'codex', 'enabled': False},
        ]))
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job.get('new_session_agents', []), [])

    def test_install_job_store_only_immediate_and_mixed(self):
        r1 = self.install_review('acme-store-only')
        self.submit('install', review_id=r1['id'], skills=['acme-store-only'], agents=[])
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job.get('new_session_agents', []), [])

        r2 = self.install_review('acme-immediate')
        self.submit('install', review_id=r2['id'], skills=['acme-immediate'], agents=['claude'])
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job.get('new_session_agents', []), [])

        r3 = self.install_review('acme-mixed')
        self.submit('install', review_id=r3['id'], skills=['acme-mixed'], agents=['claude', 'antigravity', 'codex'])
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual([a['id'] for a in job.get('new_session_agents', [])], ['codex', 'antigravity'])

    def test_update_job_immediate_mixed_and_keep_choice(self):
        # acme-a: move to claude only (immediate)
        operations.apply_assign([
            {'skill': 'acme-a', 'agent': 'codex', 'enabled': False},
            {'skill': 'acme-a', 'agent': 'claude', 'enabled': True},
        ], confirmed=True, home=self.home)
        # acme-b: assign to claude + antigravity (mixed)
        operations.apply_assign([
            {'skill': 'acme-b', 'agent': 'codex', 'enabled': False},
            {'skill': 'acme-b', 'agent': 'claude', 'enabled': True},
            {'skill': 'acme-b', 'agent': 'antigravity', 'enabled': True},
        ], confirmed=True, home=self.home)
        # acme-c: codex only, but user chooses 'keep'
        review = self.update_review('acme-a', 'acme-b', 'acme-c')

        self.submit('update', review_id=review['id'], skills=['acme-a'], choices={'acme-a': 'upstream'})
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job.get('new_session_agents', []), [])

        self.submit('update', review_id=review['id'], skills=['acme-b'], choices={'acme-b': 'upstream'})
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual([a['id'] for a in job.get('new_session_agents', [])], ['antigravity'])

        self.submit('update', review_id=review['id'], skills=['acme-c'], choices={'acme-c': 'keep'})
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job.get('new_session_agents', []), [])

    def test_cleanup_withdraw_and_restore_jobs(self):
        operations.apply_assign([
            {'skill': 'acme-a', 'agent': 'claude', 'enabled': True},
            {'skill': 'acme-b', 'agent': 'codex', 'enabled': False},
            {'skill': 'acme-b', 'agent': 'claude', 'enabled': True},
        ], confirmed=True, home=self.home)

        for skill_name, expected_ids in (('acme-a', ['codex']), ('acme-b', [])):
            plan = cleanup.preview([skill_name], self.home)
            self.submit('cleanup_withdraw', skills=[skill_name],
                        seen={k: plan[k] for k in ('dependency_warnings', 'path_warnings')})
            withdraw_job = self.wait_jobs()[-1]
            self.assertEqual(withdraw_job['status'], 'done')
            self.assertEqual(withdraw_job.get('new_session_agents', []), [])

            self.submit('restore', skills=[skill_name])
            restore_job = self.wait_jobs()[-1]
            self.assertEqual(restore_job['status'], 'done')
            self.assertEqual([a['id'] for a in restore_job.get('new_session_agents', [])], expected_ids)

    def test_custom_and_additional_profiles_drive_reminder_without_hardcoding(self):
        # Add Cursor (additional profile, new_session) and two custom profiles (immediate vs new_session).
        (self.home / 'Applications/Cursor.app').mkdir(parents=True)
        plan = self.ok('/api/agents/join/preview', {'agents': ['cursor']})
        self.ok('/api/agents/join/apply', {
            'agents': ['cursor'], 'plan_id': plan['plan_id'], 'revision': plan['revision'], 'confirmed': True,
        })

        agents_path = self.home / '.skill-pilot/agents.json'
        data = json.loads(agents_path.read_text())
        live_custom = {
            **profiles.custom('custom-live', '即时工具', 'Custom Live/skills'),
            'activation': 'immediate',
            'activation_note': '即时生效',
        }
        fresh_custom = profiles.custom('custom-fresh', '新对话工具', 'Custom Fresh/skills')
        data.setdefault('custom', []).extend([live_custom, fresh_custom])
        data['onboarded'].extend(['custom-live', 'custom-fresh'])
        agents_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        (self.home / 'Custom Live/skills').mkdir(parents=True, exist_ok=True)
        (self.home / 'Custom Fresh/skills').mkdir(parents=True, exist_ok=True)

        table = self.ok('/api/table')
        by_id = {agent['id']: agent for agent in table['agents']}
        self.assertEqual(by_id['cursor']['activation_mode'], 'new_session')
        self.assertEqual(by_id['custom-live']['activation_mode'], 'immediate')
        self.assertEqual(by_id['custom-live']['activation_text'], '即时生效')
        self.assertEqual(by_id['custom-fresh']['activation_mode'], 'new_session')
        self.assertEqual(by_id['custom-fresh']['activation_text'], '需新开对话生效')

        # Custom immediate only -> no reminder.
        self.submit('assignment', **self.assignment_params([
            {'skill': 'acme-a', 'agent': 'custom-live', 'enabled': True},
        ]))
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job.get('new_session_agents', []), [])

        # Mixed custom immediate + custom new_session + additional Cursor -> only Cursor and 新对话工具.
        self.submit('assignment', **self.assignment_params([
            {'skill': 'acme-b', 'agent': 'custom-live', 'enabled': True},
            {'skill': 'acme-b', 'agent': 'custom-fresh', 'enabled': True},
            {'skill': 'acme-b', 'agent': 'cursor', 'enabled': True},
        ]))
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done')
        self.assertEqual([a['id'] for a in job.get('new_session_agents', [])], ['cursor', 'custom-fresh'])
        self.assertEqual([a['name'] for a in job['new_session_agents']], ['Cursor', '新对话工具'])

    def test_partial_assignment_and_retry_merge_new_session_agents(self):
        # Withdraw acme-b from codex first so enabling acme-b on codex is a real enable.
        operations.apply_assign([{'skill': 'acme-b', 'agent': 'codex', 'enabled': False}], confirmed=True, home=self.home)
        changes = [
            {'skill': 'acme-a', 'agent': 'antigravity', 'enabled': True},
            {'skill': 'acme-b', 'agent': 'codex', 'enabled': True},
        ]
        params = self.assignment_params(changes)
        # Introduce a blocking non-symlink file at the target path after preview.
        conflict_entry = self.home / '.codex/skills/acme-b'
        conflict_entry.write_text('blocking file')

        self.submit('assignment', **params)
        partial = self.wait_jobs()[-1]
        self.assertEqual(partial['status'], 'attention', partial)
        self.assertEqual(partial['completed'], ['acme-a'])
        self.assertEqual(partial['remaining'], ['acme-b'])
        self.assertEqual([a['id'] for a in partial.get('new_session_agents', [])], ['antigravity'])

        # Remove the blocking entry and retry the remaining item.
        conflict_entry.unlink()
        self.ok('/api/jobs/retry', {'id': partial['id']})
        retried = self.wait_jobs()[-1]
        self.assertEqual(retried['status'], 'done', retried)
        self.assertEqual([a['id'] for a in retried.get('new_session_agents', [])], ['codex', 'antigravity'])


class ActivationReminderStaticTests(unittest.TestCase):
    def test_frontend_reminder_logic_uses_config_and_renders_toast_and_job_item(self):
        app = (STATIC / 'app.js').read_text()
        reminder_fns = '\n'.join([
            extract_function(app, 'function agentProfileOf('),
            extract_function(app, 'function newSessionAgentsFrom('),
            extract_function(app, 'function newSessionReminderText('),
            extract_function(app, 'function jobReminderText('),
            extract_function(app, 'function jobReminderHtml('),
        ])
        for tool_name in ('antigravity', 'codex', 'cursor', 'claude', 'dsh', 'doubaowork', 'workbuddy'):
            self.assertNotIn(tool_name, reminder_fns.lower(), f'前端提醒逻辑不得硬编码 {tool_name}')

        parts = [
            "const JOB_ACTIVE = new Set(['waiting', 'running']);",
            "const jobActive = job => JOB_ACTIVE.has(job.status);",
            "const escape = s => String(s ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));",
            "const icon = name => `<svg class=\"ico\"><use href=\"#sp-${name}\"/></svg>`;",
            "const nameOf = id => (state.agents.find(a => a.id === id) || {}).name || id;",
            "const feedbackButton = () => '';",
            "const jobConflictHtml = () => '';",
            "const jobFailedHtml = () => '';",
            "const jobImpactHtml = () => '';",
            "const jobActions = () => [];",
            "const jobStatusInfo = job => ({done: {tone: 'tag-ok', label: '已完成'}, failed: {tone: 'tag-danger', label: '没完成'}}[job.status] || {tone: 'tag-quiet', label: job.status});",
            extract_function(app, 'function pastTense('),
            extract_function(app, 'function jobUndoAvailable('),
            reminder_fns,
            extract_function(app, 'function jobStatusLine('),
            extract_function(app, 'function jobItemHtml('),
            block(app, 'function toast('),
            block(app, 'function finishJobs('),
        ]
        script = '\n'.join(parts) + r'''
const assert = require('node:assert/strict');
const state = {
  agents: [
    {id: 'live-a', name: '即时工具', activation_mode: 'immediate', activation_text: '即时生效'},
    {id: 'fresh-b', name: '新对话工具', activation_mode: 'new_session', activation_text: '需新开对话生效'},
    {id: 'fresh-c', name: '扩展工具', activation_mode: 'new_session', activation_text: '需新开对话生效'},
  ],
  catalog: new Map(),
  logs: [],
  jobs: [],
  table: {llm_configured: false},
};

// 1. Config-driven filtering by agent IDs and mixed profile objects
assert.deepEqual(newSessionAgentsFrom(['live-a']), []);
assert.deepEqual(newSessionAgentsFrom(['live-a', 'fresh-b', 'fresh-c']), ['新对话工具', '扩展工具']);
assert.deepEqual(newSessionAgentsFrom(state.agents), ['新对话工具', '扩展工具']);
assert.equal(newSessionReminderText(['live-a']), '');
assert.equal(newSessionReminderText(['live-a', 'fresh-b']), '新对话工具 需新开对话才会加载新技能');
assert.equal(newSessionReminderText(['fresh-b', 'fresh-c'], 'update'), '新对话工具、扩展工具 需新开对话才会加载新版技能');

// 2. Job reminder: pure immediate vs new_session vs update vs undone vs failed vs partial
const immediateJob = {id: 'j1', kind: 'assignment', status: 'done', summary: '调整分配', message: '将给 即时工具 加 1 个', operations: ['op1'], new_session_agents: []};
assert.equal(jobReminderText(immediateJob), '');
assert.doesNotMatch(jobItemHtml(immediateJob), /job-reminder/);

const newSessionJob = {id: 'j2', kind: 'assignment', status: 'done', summary: '调整分配', message: '将给 新对话工具 加 1 个', operations: ['op2'], new_session_agents: [{id: 'fresh-b', name: '新对话工具'}]};
assert.equal(jobReminderText(newSessionJob), '新对话工具 需新开对话才会加载新技能');
assert.match(jobItemHtml(newSessionJob), /class="job-reminder"[^>]*role="note".*新对话工具 需新开对话才会加载新技能/);

const updateJob = {id: 'j3', kind: 'update', status: 'done', summary: '更新', message: '已更新：示例甲。', operations: ['op3'], new_session_agents: [{id: 'fresh-b', name: '新对话工具'}, {id: 'fresh-c', name: '扩展工具'}]};
assert.equal(jobReminderText(updateJob), '新对话工具、扩展工具 需新开对话才会加载新版技能');

const failedJob = {id: 'j4', kind: 'assignment', status: 'failed', summary: '调整分配', message: '冲突', completed: [], new_session_agents: [{id: 'fresh-b', name: '新对话工具'}]};
assert.equal(jobReminderText(failedJob), '');

const partialJob = {id: 'j5', kind: 'assignment', status: 'failed', summary: '调整分配', message: '部分完成', completed: ['acme-a'], operations: ['op5'], new_session_agents: [{id: 'fresh-b', name: '新对话工具'}]};
assert.equal(jobReminderText(partialJob), '新对话工具 需新开对话才会加载新技能');

state.logs = [{id: 'op2', undone: true}];
assert.equal(jobReminderText(newSessionJob), '');
state.logs = [];

// 3. Toast DOM & autoDismiss timing
const toastsContainer = {items: [], prepend(el) { this.items.unshift(el); }};
const dismissDelays = [];
function makeEl(tag) {
  return {
    tag, className: '', textContent: '', innerHTML: '', children: [], attrs: {},
    append(...nodes) { this.children.push(...nodes); },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    remove() {},
  };
}
const document = {createElement: makeEl};
const $ = id => id === 'toasts' ? toastsContainer : makeEl('div');
function autoDismiss(item, delay) { dismissDelays.push(delay); }
function appendFeedback() {}

toast({text: '普通完成'});
assert.equal(toastsContainer.items[0].className, 'toast');
assert.equal(dismissDelays.at(-1), 6000);

toast({text: '给 新对话工具 加了 1 个', reminder: '新对话工具 需新开对话才会加载新技能'});
assert.equal(toastsContainer.items[0].className, 'toast toast-remind');
assert.equal(dismissDelays.at(-1), 20000);
const body = toastsContainer.items[0].children[0];
assert.equal(body.className, 'toast-body');
assert.equal(body.children[0].textContent, '给 新对话工具 加了 1 个');
assert.match(body.children[1].innerHTML, /新对话工具 需新开对话才会加载新技能/);

// 4. finishJobs passes reminder into toast for done and partial, omits for immediate-only
const packBatches = new Map();
const refreshTable = () => Promise.resolve();
const refreshLog = () => Promise.resolve();
const loadCleanup = () => Promise.resolve();
const renderJobs = () => {};
const jobsHasActive = () => false;
const assignmentSubmitting = false;
state.assignmentIntents = new Map();
const recordedToasts = [];
const origToast = toast;
toast = item => { recordedToasts.push(item); origToast(item); };
finishJobs([immediateJob, newSessionJob, partialJob]);
assert.equal(recordedToasts[0].reminder, '');
assert.equal(recordedToasts[1].reminder, '新对话工具 需新开对话才会加载新技能');
assert.equal(recordedToasts[2].reminder, '新对话工具 需新开对话才会加载新技能');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
