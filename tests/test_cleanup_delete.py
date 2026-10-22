"""Reversible deletion and cleanup APIs, using only synthetic homes with spaces."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import cleanup, labels, operations, store, tags, updates
from skill_pilot.sources import github
from skill_pilot.sync import visibility
from skill_pilot.web.server import Dashboard


class DeleteFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot cleanup delete ')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, Path(temporary.name) / 'home with spaces')
        self.names = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'skill-pilot', 'zeta']
        for name in self.names:
            path = self.home / '.codex/skills' / name
            path.mkdir(parents=True)
            (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例技能\n---\n正文\n')
        (self.home / '.codex/config.toml').write_text('# keep\n[skills]\nconfig=[{name="plugin:own",enabled=false}]\n')
        store.apply(store.preview(self.home)['plan_id'], self.home)
        operations.apply_assign([
            {'skill': 'beta', 'agent': 'codex', 'enabled': False},
            {'skill': 'beta', 'agent': 'dsh', 'enabled': True},
            {'skill': 'gamma', 'agent': 'codex', 'enabled': False},
        ], confirmed=True, home=self.home)
        registry = operations.load(self.home)[1]
        for name, skill in registry['skills'].items():
            skill['source'] = {'repository': 'acme/skills', 'source_path': name}
        registry['skills']['delta']['source'] = {'source': '自制'}
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        (self.home / '.skill-pilot/labels.json').write_text(json.dumps({'groups': [
            {'name': '写作', 'skills': {'alpha': {'title': '整理笔记', 'summary': '整理材料', 'description': '中文说明'},
                                    'beta': {'title': '检查材料', 'summary': '检查材料', 'description': '中文说明'}}},
            {'name': '独立分组', 'skills': {'gamma': {'title': '辅助材料', 'summary': '辅助材料', 'description': '中文说明'}}},
        ]}))
        tags.update(self.home, {'skills': ['alpha', 'gamma'], 'add': '笔记'})

    def fingerprint(self):
        paths = ['.skill-pilot/registry.json', '.skill-pilot/agents.json', '.skill-pilot/bases',
                 '.codex/config.toml', '.codex/skills', '.agents/skills', '.claude/skills',
                 '.gemini/config/skills', '.dsh/skills', '.skill-pilot/labels.json', '.skill-pilot/tags.json']
        return {p: store.physical_state(self.home / p) for p in paths} | {
            'store': store.content_digest(self.home / '.skill-pilot/store')}

    def local_edits(self, name):
        root = self.home / '.skill-pilot/store' / name
        (root / 'SKILL.md').write_text((root / 'SKILL.md').read_text() + '未记录的本地改动\n')
        (root / 'extra.txt').write_text('额外文件')
        (root / 'task.sh').write_text('echo example\n')
        (root / 'task.sh').chmod(0o755)
        (root / 'protected.txt').write_text('只读文件')
        (root / 'protected.txt').chmod(0o444)
        (root / 'private').mkdir(mode=0o700)
        (root / 'private/notes.txt').write_text('目录内的文件')
        (self.home / '.skill-pilot/bases' / name / 'SKILL.md').write_text('独立的更新基线\n')


class DeleteTests(DeleteFixture):
    def test_table_source_kind_covers_all_sources_and_invalid_records(self):
        registry = operations.load(self.home)[1]
        registry['skills']['beta']['source'] = {'record': {'sourceType': 'well-known',
            'sourceUrl': 'https://open.feishu.cn/lark-cli/skills/regular/.well-known/agent-skills/beta.tar.gz'}}
        registry['skills']['gamma']['source'] = {'source': '团队商店', 'team': {
            'member': 'a' * 32, 'member_name': '示例同事', 'name': 'gamma',
            'version': 'b' * 64, 'key': 'team:' + 'a' * 32 + ':gamma'}}
        registry['skills']['epsilon']['source'] = {'repository': 'acme/skills', 'source_path': '../invalid'}
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        rows = {row['name']: row for row in operations.table(self.home)['skills']}
        self.assertEqual({name: row['source_kind'] for name, row in rows.items()}, {
            'alpha': 'github', 'beta': 'lark', 'gamma': 'team', 'delta': 'skip',
            'epsilon': 'skip', 'skill-pilot': 'github', 'zeta': 'github'})
        self.assertEqual(rows['epsilon']['source_label'], '没查清楚')
        self.assertEqual(rows['epsilon']['source'], registry['skills']['epsilon']['source'])

    def test_table_withdrawn_fields_filter_agents_and_missing_group_members(self):
        rows = {row['name']: row for row in operations.table(self.home)['skills']}
        self.assertEqual((rows['alpha']['withdrawn'], rows['alpha']['withdrawn_at'], rows['alpha']['previous_agents']),
                         (False, None, []))
        cleanup.apply(['alpha'], True, self.home)
        registry = operations.load(self.home)[1]
        registry['skills']['alpha']['withdrawn_agents'].append('disconnected')
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        row = next(r for r in operations.table(self.home)['skills'] if r['name'] == 'alpha')
        self.assertTrue(row['withdrawn'])
        self.assertIsInstance(row['withdrawn_at'], str)
        self.assertEqual(row['previous_agents'], ['codex'])
        cleanup.delete(['gamma'], True, self.home)
        table = operations.table(self.home)
        self.assertNotIn('独立分组', [g['name'] for g in table['groups']])
        self.assertTrue(all('gamma' not in g['skills'] for g in table['groups']))
        self.assertEqual(table['labels_pending'], 4)
        self.assertTrue(all('笔记' not in r['tags'] for r in table['skills'] if r['name'] != 'alpha'))

    def test_delete_mixed_states_cleans_entries_files_baselines_and_commit(self):
        cleanup.apply(['alpha'], True, self.home)
        before = self.fingerprint()
        entries = [entry for name, skill in operations.load(self.home)[1]['skills'].items()
                   if name in {'alpha', 'beta', 'gamma'} for entry in skill['entries']]
        result = cleanup.delete(['gamma', 'alpha', 'beta', 'beta'], True, self.home)
        self.assertEqual(result['deleted'], ['alpha', 'beta', 'gamma'])
        self.assertEqual(result['message'], '删除了 3 个技能')
        registry = operations.load(self.home)[1]['skills']
        for name in result['deleted']:
            self.assertNotIn(name, registry)
            for relative in ('store/' + name, 'bases/' + name, 'store/.skill-pilot-state/' + name + '.json'):
                self.assertFalse((self.home / '.skill-pilot' / relative).exists())
            self.assertTrue(all(name not in view['available'] for view in visibility(self.home).values()))
        for entry in entries:
            self.assertFalse((self.home / entry).exists() or (self.home / entry).is_symlink())
        self.assertNotIn('beta', (self.home / '.codex/config.toml').read_text())
        self.assertIn('plugin:own', (self.home / '.codex/config.toml').read_text())
        self.assertEqual(github.git(self.home / '.skill-pilot/store', 'status', '--porcelain'), '')
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)
        self.assertTrue(operations.load(self.home)[1]['skills']['alpha']['withdrawn'])
        cleanup.restore(['alpha'], self.home)
        self.assertIn('alpha', visibility(self.home)['codex']['available'])

    def test_preview_is_readonly_deduplicates_and_reports_dependency_and_file_risks(self):
        registry = operations.load(self.home)[1]
        registry['skills']['beta']['requires'] = ['alpha']
        registry['skills']['gamma']['requires'] = ['alpha']
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        self.local_edits('alpha')
        before = self.fingerprint()
        warning = [{'file': '~/workflow.md', 'entries': ['.codex/skills/alpha']}]
        with patch.object(operations, 'path_warnings', return_value=warning) as paths:
            view = cleanup.delete_preview(['delta', 'alpha', 'alpha'], self.home)
        self.assertEqual(set(view), {'skills', 'message', 'revision', 'dependency_warnings', 'irreplaceable', 'path_warnings'})
        self.assertEqual(view['skills'], ['alpha', 'delta'])
        self.assertEqual(view['message'], '删除 2 个技能')
        self.assertEqual(view['dependency_warnings'], ['「检查材料」还要用「整理笔记」，删除后它可能用不了。'])
        self.assertEqual(view['irreplaceable'], ['alpha', 'delta'])
        self.assertEqual(view['path_warnings'], warning)
        paths.assert_called_once_with(['.codex/skills/alpha', '.codex/skills/delta'], self.home)
        self.assertEqual(cleanup.delete_preview(['alpha', 'beta'], self.home)['dependency_warnings'], [])
        self.assertEqual(self.fingerprint(), before)

    def test_delete_dependency_allows_later_assignment_and_undo_restores_requires(self):
        registry = operations.load(self.home)[1]
        registry['skills']['delta']['requires'] = ['alpha']
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        result = cleanup.delete(['alpha'], True, self.home)
        self.assertEqual(operations.load(self.home)[1]['skills']['delta']['requires'], [])
        assigned = operations.apply_assign([{'skill': 'delta', 'agent': 'claude', 'enabled': True}],
                                           confirmed=True, home=self.home)
        self.assertIn('delta', visibility(self.home)['claude']['available'])
        operations.undo(assigned['id'], home=self.home)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(operations.load(self.home)[1]['skills']['delta']['requires'], ['alpha'])

    def test_immediate_undo_restores_unrecorded_content_baselines_and_modes(self):
        self.local_edits('alpha')
        before = self.fingerprint()
        exact = store.physical_state(self.home / '.skill-pilot/store/alpha')
        result = cleanup.delete(['alpha'], True, self.home)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(store.physical_state(self.home / '.skill-pilot/store/alpha'), exact)

    def test_undo_after_independent_operation_keeps_that_operation_and_restores_all_states(self):
        cleanup.apply(['alpha'], True, self.home)
        self.local_edits('alpha')
        self.local_edits('beta')
        before = operations.load(self.home)[1]
        exact = {name: [store.physical_state(self.home / '.skill-pilot' / folder / name) for folder in ('store', 'bases')]
                 for name in ('alpha', 'beta', 'gamma')}
        config = (self.home / '.codex/config.toml').read_bytes()
        result = cleanup.delete(['alpha', 'beta', 'gamma'], True, self.home)
        operations.apply_assign([{'skill': 'epsilon', 'agent': 'claude', 'enabled': True}], confirmed=True, home=self.home)
        operations.undo(result['id'], home=self.home)
        registry = operations.load(self.home)[1]
        for name in exact:
            self.assertEqual(registry['skills'][name], before['skills'][name])
            self.assertEqual([store.physical_state(self.home / '.skill-pilot' / folder / name) for folder in ('store', 'bases')], exact[name])
        self.assertIn('epsilon', visibility(self.home)['claude']['available'])
        self.assertEqual((self.home / '.codex/config.toml').read_bytes(), config)
        rows = {r['name']: r for r in operations.table(self.home)['skills']}
        self.assertEqual((rows['alpha']['title'], rows['alpha']['tags'], rows['alpha']['withdrawn']), ('整理笔记', ['笔记'], True))
        cleanup.restore(['alpha'], self.home)
        self.assertIn('alpha', visibility(self.home)['codex']['available'])

    def test_invalid_selection_confirmation_revision_and_readonly_do_not_write(self):
        before = self.fingerprint()
        for names in (None, '', {}, [], [None], [1], [[]], ['missing'], ['alpha', 'missing']):
            with self.subTest(names=names):
                with self.assertRaises(ValueError):
                    cleanup.delete_preview(names, self.home)
                with self.assertRaises(ValueError):
                    cleanup.delete(names, True, self.home)
        with self.assertRaisesRegex(ValueError, '「missing」不在登记表里，请刷新后再选择。'):
            cleanup.delete_preview(['missing'], self.home)
        with self.assertRaisesRegex(ValueError, 'Skill Pilot 自带的技能只能撤下，不能删除。请取消勾选后再删除。'):
            cleanup.delete(['alpha', 'skill-pilot'], True, self.home)
        for confirmed in (False, None, 1, 'true'):
            self.assertTrue(cleanup.delete(['alpha'], confirmed, self.home)['needs_confirmation'])
        with self.assertRaisesRegex(ValueError, '目录内容已经变了'):
            cleanup.delete(['alpha'], True, self.home, expected='stale')
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            cleanup.delete_preview(['alpha'], self.home)
            with self.assertRaisesRegex(ValueError, '只读'):
                cleanup.delete(['alpha'], True, self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_failed_delete_rolls_back_and_pruned_backup_uses_existing_message(self):
        self.local_edits('alpha')
        before = self.fingerprint()
        with patch.object(operations, '_commit', side_effect=ValueError('injected')):
            with self.assertRaisesRegex(ValueError, 'injected'):
                cleanup.delete(['alpha', 'beta'], True, self.home)
        self.assertEqual(self.fingerprint(), before)
        result = cleanup.delete(['alpha'], True, self.home)
        shutil.rmtree(self.home / '.skill-pilot/backups' / result['backup_id'])
        after = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '^这次操作太早了，备份已经清掉，不能再撤销。$'):
            operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), after)

    def test_concurrent_deletes_share_the_existing_write_lock(self):
        real_preview = cleanup.delete_preview
        ready = threading.Barrier(2)
        def preview(names, home):
            view = real_preview(names, home)
            ready.wait(5)
            return view
        with patch.object(cleanup, 'delete_preview', side_effect=preview), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(cleanup.delete, [name], True, self.home) for name in ('alpha', 'gamma')]
            results, errors = [], []
            for future in futures:
                try:
                    results.append(future.result(timeout=10))
                except ValueError as error:
                    errors.append(str(error))
        self.assertEqual(len(results), 1)
        self.assertEqual(errors, ['目录内容已经变了，请刷新后重新核对。'])
        self.assertEqual(len(operations.load(self.home)[1]['skills']), len(self.names) - 1)

    def test_batch_restore_and_mixed_invalid_selection(self):
        cleanup.apply(['alpha', 'beta'], True, self.home)
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '^请选择已撤下的技能。$'):
            cleanup.restore(['alpha', 'delta'], self.home)
        self.assertEqual(self.fingerprint(), before)
        result = cleanup.restore(['alpha', 'beta'], self.home)
        self.assertEqual(result['restored'], ['alpha', 'beta'])
        self.assertIn('alpha', visibility(self.home)['codex']['available'])
        self.assertIn('beta', visibility(self.home)['dsh']['available'])
        self.assertNotIn('beta', visibility(self.home)['codex']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_cached_updates_hide_deleted_names_and_restore_after_undo(self):
        review = updates.create(self.home)
        self.addCleanup(shutil.rmtree, review['temporary'])
        review['skills'] = [{'name': n, 'has_update': True, 'status': '有更新', 'source': source,
                             'error': '示例错误' if n == 'alpha' else ''}
                            for group in review['_groups'].values() for n, _, source in group]
        review.update(running=False, done=review['total'], remaining=0, update_count=len(review['skills']))
        review['errors'] = [{'name': 'alpha', 'error': '示例错误'}]
        count = len(review['skills'])
        result = cleanup.delete(['alpha', 'gamma', 'delta'], True, self.home)
        public = updates.public(review)
        self.assertNotIn('alpha', [r['name'] for r in public['skills']])
        self.assertEqual(public['errors'], [])
        self.assertEqual((public['total'], public['done'], public['remaining'], public['update_count']),
                         (count - 1, count - 1, 0, count - 1))
        self.assertEqual(public['unchecked'], [])
        with self.assertRaises(ValueError):
            updates.selected_row(review, 'alpha')
        with self.assertRaises(ValueError):
            updates.apply(review, ['alpha'], confirmed=True)
        with patch.object(updates, '_apply_group', return_value={'id': 'example', 'normalization': {}}) as apply_group:
            self.assertEqual(updates.apply(review, ['alpha', 'beta'], confirmed=True)['updated'], ['beta'])
        self.assertEqual(apply_group.call_args.args[1], ['beta'])
        operations.undo(result['id'], home=self.home)
        self.assertIn('alpha', [r['name'] for r in updates.public(review)['skills']])

    def test_label_runs_preserve_hidden_labels_and_do_not_send_deleted_names(self):
        result = cleanup.delete(['alpha'], True, self.home)
        sent = []
        def complete(config, system, body):
            sent.extend(row['name'] for row in body['skills'])
            return {'skills': [{**row, 'title': '示例名称', 'summary': '说明', 'description': '说明', 'group': '整理'}
                               for row in body['skills']]}
        with patch.object(labels, 'completion', side_effect=complete):
            for mode in ('missing', 'all'):
                progress = {'running': True}
                labels.run(self.home, mode, progress)
                self.assertEqual(progress['error'], '')
        self.assertNotIn('alpha', sent)
        self.assertTrue(all('alpha' not in group['skills'] for group in operations.table(self.home)['groups']))
        operations.undo(result['id'], home=self.home)
        row = next(r for r in operations.table(self.home)['skills'] if r['name'] == 'alpha')
        self.assertEqual((row['title'], row['tags']), ('整理笔记', ['笔记']))

    def test_label_queue_skips_a_skill_deleted_between_batches(self):
        (self.home / '.skill-pilot/labels.json').unlink()
        sent = []
        def complete(config, system, body):
            sent.extend(row['name'] for row in body['skills'])
            if len(sent) == 5:
                cleanup.delete(['zeta'], True, self.home)
            return {'skills': [{**row, 'title': '示例名称', 'summary': '说明', 'description': '说明', 'group': '整理'}
                               for row in body['skills']]}
        progress = {'running': True}
        with patch.object(labels, 'completion', side_effect=complete):
            labels.run(self.home, 'missing', progress)
        self.assertEqual(progress['error'], '')
        self.assertEqual((progress['done'], progress['total']), (6, 6))
        self.assertNotIn('zeta', sent)


class DeleteWebTests(DeleteFixture):
    def setUp(self):
        super().setUp()
        self.server = Dashboard({'home': str(self.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, body=None):
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Origin': self.server.origin, 'X-Skill-Pilot-Token': self.server.token})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_delete_preview_apply_table_and_api_undo(self):
        before = self.fingerprint()
        status, view = self.request('/api/cleanup/delete/preview', {'skills': ['alpha', 'gamma']})
        self.assertEqual(status, 200, view)
        self.assertEqual(self.fingerprint(), before)
        status, result = self.request('/api/cleanup/delete/apply', {'skills': view['skills'], 'revision': view['revision'], 'confirmed': True})
        self.assertEqual(status, 200, result)
        _, table = self.request('/api/table')
        self.assertNotIn('alpha', [r['name'] for r in table['skills']])
        status, undone = self.request('/api/undo', {'id': result['id'], 'revision': table['revision'], 'confirmed': True})
        self.assertEqual(status, 200, undone)
        self.assertEqual(self.fingerprint(), before)

    def test_delete_api_guards_preview_errors_and_busy_state(self):
        before = self.fingerprint()
        self.assertEqual(self.request('/api/cleanup/delete/preview', {'skills': []})[0], 400)
        self.assertEqual(self.request('/api/cleanup/delete/preview', {'skills': ['skill-pilot']})[0], 400)
        _, view = self.request('/api/cleanup/delete/preview', {'skills': ['alpha']})
        body = {'skills': ['alpha'], 'revision': view['revision'], 'confirmed': True}
        self.assertEqual(self.request('/api/cleanup/delete/apply', {**body, 'confirmed': False})[0], 400)
        for revision in (None, '', 'stale'):
            self.assertEqual(self.request('/api/cleanup/delete/apply', {**body, 'revision': revision})[0], 409)
        self.assertEqual(self.request('/api/cleanup/delete/apply', {**body, 'skills': ['missing']})[0], 400)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertEqual(self.request('/api/cleanup/delete/preview', {'skills': ['alpha']})[0], 200)
            self.assertEqual(self.request('/api/cleanup/delete/apply', body)[0], 403)
        self.server.reload_pending = True
        self.assertEqual(self.request('/api/cleanup/delete/apply', body)[0], 503)
        self.server.reload_pending = False
        self.server.program_progress.update(running=True, phase='apply')
        self.assertEqual(self.request('/api/cleanup/delete/apply', body)[0], 503)
        self.server.program_progress.update(running=False, phase='idle')
        self.assertEqual(self.fingerprint(), before)

    def test_bulk_restore_api_preserves_validation_and_readonly(self):
        cleanup.apply(['alpha', 'beta'], True, self.home)
        _, table = self.request('/api/table')
        body = {'skills': ['alpha', 'beta'], 'revision': table['revision'], 'confirmed': True}
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertEqual(self.request('/api/withdrawn/restore', body)[0], 403)
        status, bad = self.request('/api/withdrawn/restore', {**body, 'skills': ['alpha', 'delta']})
        self.assertEqual((status, bad['error']), (409, '请选择已撤下的技能。'))
        status, restored = self.request('/api/withdrawn/restore', body)
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored['restored'], ['alpha', 'beta'])


if __name__ == '__main__':
    unittest.main()
