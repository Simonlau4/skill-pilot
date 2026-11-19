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
from skill_pilot import cleanup, operations, profiles, sharing, store, team, updates
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
        self.assertFalse(hasattr(profiles, 'new_session_reminder'))


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
        self.assertEqual(len(retried['operations']), 2)

        # Partial undo: undo the latest operation (codex) first; /api/jobs must narrow to antigravity only.
        second_op = retried['operations'][1]
        table = self.ok('/api/table')
        self.ok('/api/undo', {'id': second_op, 'revision': table['revision'], 'confirmed': True})
        after_partial_undo = self.ok('/api/jobs')['items'][-1]
        self.assertEqual([a['id'] for a in after_partial_undo.get('new_session_agents', [])], ['antigravity'])

    def test_update_skips_disabled_agent_and_identical_content_and_batch_partial_retry(self):
        # 1. Codex has acme-a externally disabled while Claude uses it -> updating acme-a must NOT flag Codex.
        operations.apply_assign([{'skill': 'acme-a', 'agent': 'claude', 'enabled': True}], confirmed=True, home=self.home)
        (self.home / '.codex/config.toml').write_text(
            '[[skills.config]]\nname = "acme-a"\nenabled = false\n', encoding='utf-8'
        )
        review_disabled = self.update_review('acme-a')
        self.submit('update', review_id=review_disabled['id'], skills=['acme-a'], choices={'acme-a': 'upstream'})
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job.get('new_session_agents', []), [])

        # 2. Identical upstream content on Antigravity -> even with choice='upstream', must NOT flag Antigravity.
        operations.apply_assign([
            {'skill': 'acme-b', 'agent': 'codex', 'enabled': False},
            {'skill': 'acme-b', 'agent': 'antigravity', 'enabled': True},
        ], confirmed=True, home=self.home)
        same_review = self.update_review('acme-b')
        same_upstream = Path(same_review['skills'][0]['_upstream_path'])
        (same_upstream / 'SKILL.md').write_text((self.home / '.skill-pilot/store/acme-b/SKILL.md').read_text())
        same_sha = store.git_tree(same_upstream)
        same_review['skills'][0].update(tree_sha=same_sha, normalized_tree_sha=same_sha)
        self.submit('update', review_id=same_review['id'], skills=['acme-b'], choices={'acme-b': 'upstream'})
        job = self.wait_jobs()[-1]
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job.get('new_session_agents', []), [])

        # 3. Batch "update all" partial success (acme-b on Antigravity succeeds, acme-c on Codex fails) + retry merge.
        batch_review = self.update_review('acme-b', 'acme-c')
        # Corrupt acme-c's review state so group acme-c fails while acme-b succeeds.
        batch_review['skills'][1]['_state'] = 'stale-state'
        self.submit('update', review_id=batch_review['id'], skills=['acme-b', 'acme-c'],
                    choices={'acme-b': 'upstream', 'acme-c': 'upstream'})
        partial = self.wait_jobs()[-1]
        self.assertEqual(partial['status'], 'failed', partial)
        self.assertEqual(partial['updated'], ['acme-b'])
        self.assertEqual([a['id'] for a in partial.get('new_session_agents', [])], ['antigravity'])

        # Retry failed acme-c with a fresh review -> merges Antigravity + Codex.
        retry_review = self.update_review('acme-c')
        self.ok('/api/jobs/retry', {'id': partial['id'], 'review_id': retry_review['id']})
        merged = self.wait_jobs()[-1]
        self.assertEqual(merged['status'], 'done', merged)
        self.assertEqual(sorted(merged['updated']), ['acme-b', 'acme-c'])
        self.assertEqual([a['id'] for a in merged.get('new_session_agents', [])], ['codex', 'antigravity'])
        self.assertEqual(len(merged['operations']), 2)

        # Partial undo then full undo on the merged batch update job: /api/jobs narrows and then clears.
        first_op, second_op = merged['operations'][0], merged['operations'][1]
        table = self.ok('/api/table')
        self.ok('/api/undo', {'id': second_op, 'revision': table['revision'], 'confirmed': True})
        self.assertEqual([a['id'] for a in self.ok('/api/jobs')['items'][-1].get('new_session_agents', [])], ['antigravity'])
        table = self.ok('/api/table')
        self.ok('/api/undo', {'id': first_op, 'revision': table['revision'], 'confirmed': True})
        self.assertEqual(self.ok('/api/jobs')['items'][-1].get('new_session_agents', []), [])

    def test_import_config_dependency_and_restore_ignore_already_available_skills(self):
        # Move acme-a to Antigravity only in registry, then place a live copy for Codex so Codex already reads it.
        operations.apply_assign([
            {'skill': 'acme-a', 'agent': 'codex', 'enabled': False},
            {'skill': 'acme-a', 'agent': 'antigravity', 'enabled': True},
        ], confirmed=True, home=self.home)
        (self.home / '.codex/skills/acme-a').symlink_to(self.home / '.skill-pilot/store/acme-a', target_is_directory=True)
        # Config import recording Codex assignment for already-visible acme-a -> no new_session reminder!
        config = sharing.export_config(self.home)
        row = next(s for s in config['skills'] if s['name'] == 'acme-a')
        row['assigned_agents'] = ['antigravity', 'codex']
        imported = sharing.import_config(config, home=self.home)
        self.assertEqual(imported.get('new_session_agents', []), [])

        # Adding a dependency on acme-b (already visible on Codex) for acme-c -> no new_session reminder.
        dep_same = operations.decide_dependencies(
            [{'skill': 'acme-c', 'requires': 'acme-b', 'confirmed': True}], home=self.home)
        self.assertEqual(dep_same.get('new_session_agents', []), [])

        # Adding a dependency on acme-a after withdrawing acme-a from Codex -> truly newly enables on Codex!
        operations.apply_assign([{'skill': 'acme-a', 'agent': 'codex', 'enabled': False}], confirmed=True, home=self.home)
        dep_new = operations.decide_dependencies(
            [{'skill': 'acme-c', 'requires': 'acme-a', 'confirmed': True}], home=self.home)
        self.assertEqual([a['id'] for a in dep_new.get('new_session_agents', [])], ['codex'])

        # Cleanup withdraw then place external identical symlink before restore -> restore must not falsely remind.
        cleanup.apply(['acme-b'], True, self.home)
        (self.home / '.codex/skills/acme-b').symlink_to(self.home / '.skill-pilot/store/acme-b', target_is_directory=True)
        restored = cleanup.restore(['acme-b'], self.home)
        self.assertEqual(restored.get('new_session_agents', []), [])

    def test_absorb_and_agent_join_remind_only_when_visible_version_changes(self):
        # 1. Absorb identical external copy -> no reminder.
        self.skill(self.home / '.gemini/config/skills/acme-a', 'acme-a', body='原内容')
        table = self.ok('/api/table')
        same_absorb = self.ok('/api/absorb', {'all': True, 'revision': table['revision'], 'confirmed': True})
        self.assertEqual(same_absorb.get('new_session_agents', []), [])

        # 2. Absorb different external copy on Antigravity:
        #    (a) Choosing incoming copy replaces store -> Codex (reading store) changed -> reminds Codex only.
        (self.home / '.gemini/config/skills/acme-b').unlink(missing_ok=True)
        self.skill(self.home / '.gemini/config/skills/acme-b', 'acme-b', body='外部新版本')
        table = self.ok('/api/table')
        replaced_store = self.ok('/api/absorb', {
            'all': True, 'revision': table['revision'],
            'choices': {'acme-b': '.gemini/config/skills/acme-b'}, 'confirmed': True,
        })
        self.assertEqual([a['id'] for a in replaced_store.get('new_session_agents', [])], ['codex'])

        #    (b) Choosing store over Antigravity's different copy -> Antigravity changed -> reminds Antigravity only.
        self.skill(self.home / '.gemini/config/skills/acme-c', 'acme-c', body='另一份外部版本')
        table = self.ok('/api/table')
        kept_store = self.ok('/api/absorb', {
            'all': True, 'revision': table['revision'],
            'choices': {'acme-c': 'store'}, 'confirmed': True,
        })
        self.assertEqual([a['id'] for a in kept_store.get('new_session_agents', [])], ['antigravity'])

        # 3. Agent join (Cursor, new_session) with a different version of acme-a:
        (self.home / 'Applications/Cursor.app').mkdir(parents=True)
        self.skill(self.home / '.cursor/skills/acme-a', 'acme-a', body='Cursor 独立版本')
        plan = self.ok('/api/agents/join/preview', {'agents': ['cursor']})
        incoming_opt = next(o['id'] for o in plan['skills'][0]['options'] if o['id'] != 'store')
        # Choosing Cursor's copy replaces store -> Codex & Antigravity (store readers) reminded, not Cursor.
        joined = self.ok('/api/agents/join/apply', {
            'agents': ['cursor'], 'plan_id': plan['plan_id'], 'revision': plan['revision'],
            'choices': {'acme-a': incoming_opt}, 'confirmed': True,
        })
        self.assertEqual([a['id'] for a in joined.get('new_session_agents', [])], ['codex', 'antigravity'])

        # Undo join and re-join choosing 'store' -> only Cursor's visible version changed!
        table = self.ok('/api/table')
        self.ok('/api/undo', {'id': joined['id'], 'revision': table['revision'], 'confirmed': True})
        plan2 = self.ok('/api/agents/join/preview', {'agents': ['cursor']})
        joined2 = self.ok('/api/agents/join/apply', {
            'agents': ['cursor'], 'plan_id': plan2['plan_id'], 'revision': plan2['revision'],
            'choices': {'acme-a': 'store'}, 'confirmed': True,
        })
        self.assertEqual([a['id'] for a in joined2.get('new_session_agents', [])], ['cursor'])

    def test_team_store_install_then_enable_and_queue_duplicate_and_rollback_preserve_reminders(self):
        # 1. Team install saves to store only -> no premature reminder; subsequent enable reminds Antigravity only.
        def fake_team_install(home, key):
            r = self.install_review('acme-team')
            res = operations.install(r, ['acme-team'], assigned=[], home=home)
            return {'id': res['id'], 'installed': ['acme-team'], 'reuse': [], 'message': '已从团队商店装上「acme-team」'}

        with patch.object(team, 'install', side_effect=fake_team_install):
            self.server._add_job({'kind': 'team_install', 'key': 'team:acme-team'},
                                 team_info={'name': 'acme-team', 'title': '团队技能'})
            tjob = self.wait_jobs()[-1]
            self.assertEqual(tjob['status'], 'done', tjob)
            self.assertEqual(tjob.get('new_session_agents', []), [])

        self.submit('assignment', **self.assignment_params([
            {'skill': 'acme-team', 'agent': 'claude', 'enabled': True},
            {'skill': 'acme-team', 'agent': 'antigravity', 'enabled': True},
        ]))
        enable_job = self.wait_jobs()[-1]
        self.assertEqual([a['id'] for a in enable_job.get('new_session_agents', [])], ['antigravity'])

        # 2. Duplicate active intent on retry carries previous partial reminder & operations.
        prev = {
            'id': 'prev-partial', 'kind': 'assignment', 'status': 'failed',
            'skills': ['acme-a'], 'agents': ['antigravity'], 'operations': ['op-prev'],
            'completed': ['acme-a'],
            'new_session_agents': [{'id': 'antigravity', 'name': 'Antigravity', 'activation_note': '需新开对话生效'}],
            'operation_new_session_agents': {'op-prev': [{'id': 'antigravity', 'name': 'Antigravity', 'activation_note': '需新开对话生效'}]},
            '_parameters': {'kind': 'assignment', 'changes': [{'skill': 'acme-b', 'agent': 'codex', 'enabled': True}], 'seen': {}},
            '_intent': 'prev-intent',
        }
        dup_params = {'kind': 'assignment', 'changes': [{'skill': 'acme-b', 'agent': 'codex', 'enabled': True}], 'seen': {}}
        dup_intent = json.dumps({k: v for k, v in dup_params.items() if k != 'seen'}, sort_keys=True)
        active = {
            'id': 'active-dup', 'kind': 'assignment', 'status': 'running',
            'skills': ['acme-b'], 'agents': ['codex'], 'operations': [],
            '_parameters': dup_params, '_intent': dup_intent,
        }
        with self.server.jobs_lock:
            self.server.jobs.extend([prev, active])
        merged_dup = self.server._add_job(dup_params, previous=prev)
        self.assertTrue(merged_dup['duplicate'])
        self.assertEqual([a['id'] for a in merged_dup['job'].get('new_session_agents', [])], ['antigravity'])
        self.assertIn('op-prev', merged_dup['job']['operations'])
        with self.server.jobs_lock:
            self.server.jobs = [j for j in self.server.jobs if j['id'] != 'active-dup']

        # 3. RollbackError on a later group preserves earlier succeeded operation's reminder.
        op_done = operations.apply_assign([{'skill': 'acme-c', 'agent': 'antigravity', 'enabled': True}], confirmed=True, home=self.home)
        err = operations.RollbackError('这项操作没能完全恢复，后续更新已停止，请先用备份还原。')
        err.operations = [{'id': op_done['id']}]
        err.completed = ['acme-c']
        with patch.object(self.server, 'execute_job', side_effect=err):
            self.submit('assignment', **self.assignment_params([{'skill': 'acme-a', 'agent': 'codex', 'enabled': False}]))
            stopped = self.wait_jobs()[-1]
        self.assertEqual(stopped['status'], 'failed')
        self.assertIn(op_done['id'], stopped['operations'])
        self.assertEqual([a['id'] for a in stopped.get('new_session_agents', [])], ['antigravity'])
        self.server.jobs_blocked = ''


class ActivationReminderStaticTests(unittest.TestCase):
    def test_frontend_reminder_logic_uses_config_and_renders_toast_and_job_item(self):
        app = (STATIC / 'app.js').read_text()
        reminder_fns = '\n'.join([
            extract_function(app, 'function agentProfileOf('),
            extract_function(app, 'function newSessionAgentsFrom('),
            extract_function(app, 'function newSessionReminderText('),
            extract_function(app, 'function jobNewSessionAgents('),
            extract_function(app, 'function jobReminderText('),
            extract_function(app, 'function jobReminderHtml('),
        ])
        for tool_name in ('antigravity', 'codex', 'cursor', 'claude', 'dsh', 'doubaowork', 'workbuddy'):
            self.assertNotIn(tool_name, reminder_fns.lower(), f'前端提醒逻辑不得硬编码 {tool_name}')

        # Ensure absorb, agent join (direct & dialog), config import, and dependency add wire reminders.
        self.assertIn("newSessionReminderText(written.new_session_agents || [], 'update')", extract_function(app, 'async function applyAbsorb('))
        self.assertIn("newSessionReminderText(result.new_session_agents || [], 'update')", app)
        self.assertIn("syncToastReminders();", extract_function(app, 'async function refreshLog('))

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
            block(app, 'function setToastReminder('),
            block(app, 'function syncToastReminders('),
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

// 2. Job reminder: pure immediate vs new_session vs update vs partial undo vs full undo vs failed
const immediateJob = {id: 'j1', kind: 'assignment', status: 'done', summary: '调整分配', message: '将给 即时工具 加 1 个', operations: ['op1'], new_session_agents: []};
assert.equal(jobReminderText(immediateJob), '');
assert.doesNotMatch(jobItemHtml(immediateJob), /job-reminder/);

const multiOpJob = {
  id: 'j2', kind: 'assignment', status: 'done', summary: '调整分配',
  message: '将给 新对话工具 加 1 个、将给 扩展工具 加 1 个',
  operations: ['op2a', 'op2b'],
  new_session_agents: [{id: 'fresh-b', name: '新对话工具'}, {id: 'fresh-c', name: '扩展工具'}],
  operation_new_session_agents: {
    op2a: [{id: 'fresh-b', name: '新对话工具'}],
    op2b: [{id: 'fresh-c', name: '扩展工具'}],
  },
};
assert.equal(jobReminderText(multiOpJob), '新对话工具、扩展工具 需新开对话才会加载新技能');
assert.match(jobItemHtml(multiOpJob), /class="job-reminder"[^>]*role="note".*新对话工具、扩展工具 需新开对话才会加载新技能/);

// Partial undo: undo op2a -> only 扩展工具 remains!
state.logs = [{id: 'op2a', undone: true}, {id: 'op2b', undone: false}];
assert.equal(jobReminderText(multiOpJob), '扩展工具 需新开对话才会加载新技能');
assert.match(jobItemHtml(multiOpJob), /扩展工具 需新开对话才会加载新技能/);
assert.doesNotMatch(jobItemHtml(multiOpJob), /新对话工具 需新开对话/);

// Full undo: undo both -> reminder disappears completely!
state.logs = [{id: 'op2a', undone: true}, {id: 'op2b', undone: true}];
assert.equal(jobReminderText(multiOpJob), '');
assert.doesNotMatch(jobItemHtml(multiOpJob), /job-reminder/);
state.logs = [];

const updateJob = {id: 'j3', kind: 'update', status: 'done', summary: '更新', message: '已更新：示例甲。', operations: ['op3'], new_session_agents: [{id: 'fresh-b', name: '新对话工具'}, {id: 'fresh-c', name: '扩展工具'}]};
assert.equal(jobReminderText(updateJob), '新对话工具、扩展工具 需新开对话才会加载新版技能');

const failedJob = {id: 'j4', kind: 'assignment', status: 'failed', summary: '调整分配', message: '冲突', completed: [], new_session_agents: [{id: 'fresh-b', name: '新对话工具'}]};
assert.equal(jobReminderText(failedJob), '');

const partialJob = {
  id: 'j5', kind: 'assignment', status: 'failed', summary: '调整分配', message: '部分完成',
  completed: ['acme-a'], operations: ['op5'],
  new_session_agents: [{id: 'fresh-b', name: '新对话工具'}],
  operation_new_session_agents: {op5: [{id: 'fresh-b', name: '新对话工具'}]},
};
assert.equal(jobReminderText(partialJob), '新对话工具 需新开对话才会加载新技能');

// 3. Minimal DOM mock supporting classList, querySelector, replaceWith, remove
function makeEl(tag) {
  const el = {
    tag, _classes: new Set(), textContent: '', innerHTML: '', children: [], attrs: {}, parent: null,
    get className() { return [...this._classes].join(' '); },
    set className(val) { this._classes = new Set(String(val || '').split(/\s+/).filter(Boolean)); },
    classList: {
      add(...cs) { for (const c of cs) el._classes.add(c); },
      remove(...cs) { for (const c of cs) el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
    },
    append(...nodes) { for (const n of nodes) { n.parent = this; this.children.push(n); } },
    prepend(n) { n.parent = this; this.children.unshift(n); },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; },
    replaceWith(other) {
      if (!this.parent) return;
      const idx = this.parent.children.indexOf(this);
      other.parent = this.parent;
      this.parent.children[idx] = other;
      this.parent = null;
    },
    querySelector(sel) {
      for (const child of this.children) {
        if ((sel.startsWith('.') && child._classes.has(sel.slice(1))) || child.tag === sel) return child;
        const found = child.querySelector(sel);
        if (found) return found;
      }
      return null;
    },
  };
  return el;
}

const toastsContainer = makeEl('div');
const dismissDelays = [];
const document = {createElement: makeEl};
const $ = id => id === 'toasts' ? toastsContainer : makeEl('div');
function autoDismiss(item, delay) { dismissDelays.push(delay); }
function appendFeedback() {}

toast({text: '普通完成'});
assert.equal(toastsContainer.children[0].className, 'toast');
assert.equal(dismissDelays.at(-1), 6000);

// 4. finishJobs + live syncToastReminders on partial & full undo (for both done and sticky partial-failure toasts!)
const packBatches = new Map();
const refreshTable = () => Promise.resolve();
const refreshLog = () => Promise.resolve();
const loadCleanup = () => Promise.resolve();
const renderJobs = () => {};
const jobsHasActive = () => false;
const assignmentSubmitting = false;
state.assignmentIntents = new Map();
state.jobs = [immediateJob, multiOpJob, partialJob];
toastsContainer.children = [];

finishJobs([immediateJob, multiOpJob, partialJob]);
// Prepend order: [0] partialJob, [1] multiOpJob, [2] immediateJob
const partialToast = toastsContainer.children[0];
const multiToast = toastsContainer.children[1];
const immediateToast = toastsContainer.children[2];
assert.equal(immediateToast.querySelector('.toast-reminder'), null);
assert.match(multiToast.querySelector('.toast-reminder').innerHTML, /新对话工具、扩展工具 需新开对话才会加载新技能/);
assert.match(partialToast.querySelector('.toast-reminder').innerHTML, /新对话工具 需新开对话才会加载新技能/);

// Undo op2a (partial) & op5 (partial-failure job's only completed op) -> syncToastReminders updates live toasts!
state.logs = [{id: 'op2a', undone: true}, {id: 'op2b', undone: false}, {id: 'op5', undone: true}];
syncToastReminders();
assert.match(multiToast.querySelector('.toast-reminder').innerHTML, /扩展工具 需新开对话才会加载新技能/);
assert.doesNotMatch(multiToast.querySelector('.toast-reminder').innerHTML, /新对话工具/);
assert.equal(partialToast.querySelector('.toast-reminder'), null);
assert.equal(partialToast.classList.contains('toast-remind'), false);

// Undo op2b too -> multiToast loses reminder AND undo button!
state.logs = [{id: 'op2a', undone: true}, {id: 'op2b', undone: true}, {id: 'op5', undone: true}];
syncToastReminders();
assert.equal(multiToast.querySelector('.toast-reminder'), null);
assert.equal(multiToast.classList.contains('toast-remind'), false);
assert.equal(multiToast._undoButton, null);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
