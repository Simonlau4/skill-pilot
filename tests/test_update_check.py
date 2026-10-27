import sys
from skill_pilot.platform_support import rmtree
"""Background update checks against isolated homes and controlled repositories."""
import json
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, store, updates
from skill_pilot.sources import github, lark
from skill_pilot.sources.update_github import Reader
from skill_pilot.web.server import Dashboard
from tests import test_updates


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_updates.UpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home

    def add_skill(self, name='zsecond', assigned=True):
        folder = self.fixture.repo / name
        folder.mkdir()
        manifest = folder / 'SKILL.md'
        manifest.write_text(f'---\nname: {name}\ndescription: test\n---\nold\n')
        self.fixture.commit(name)
        review = github.inspect(self.fixture.repo.as_uri())
        try:
            operations.install(review, [name], ['codex'] if assigned else [], home=self.home)
        finally:
            rmtree(review['temporary'])
        manifest.write_text(manifest.read_text().replace('old', 'new'))
        self.fixture.commit('change ' + name)

    def server(self):
        self.dashboard = Dashboard({'home': str(self.home)})
        thread = threading.Thread(target=self.dashboard.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def close():
            self.dashboard.shutdown(); thread.join(); self.dashboard.server_close()
            for review in self.dashboard.update_reviews.values():
                rmtree(review['temporary'], ignore_errors=True)
        self.addCleanup(close)
        # Use the existing HTTP request helper with this isolated server.
        self.server = self.dashboard
        self.request = lambda path, body=None: test_updates.UpdateWebTests.request(self, path, body)

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, review = self.request('/api/updates/progress')
            if review and predicate(review): return review
            time.sleep(.01)
        self.fail('后台任务没有进入预期状态')

    def review(self):
        review = updates.check(self.home)
        self.addCleanup(shutil.rmtree, review['temporary'], True)
        return review

    def test_immediate_progress_refresh_and_repeated_start_reuse_task(self):
        self.server()
        gate = threading.Event()
        original = Reader.latest
        def held(reader, source):
            gate.wait(5)
            return original(reader, source)
        with patch.object(Reader, 'latest', held):
            try:
                started = time.monotonic()
                status, first = self.request('/api/updates/check', {})
                self.assertEqual(status, 200)
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(first['running'])
                self.assertEqual((first['total'], first['done'], first['remaining'], first['repositories']), (1, 0, 1, 1))
                refreshed = self.until(lambda r: r['running'])
                self.assertEqual(refreshed['id'], first['id'])
                self.assertEqual(self.request('/api/updates/check', {})[1]['id'], first['id'])
            finally: gate.set()
            done = self.until(lambda r: not r['running'])
        self.assertEqual((done['done'], done['remaining'], done['update_count']), (1, 0, 1))
        self.assertNotIn('_groups', done)
        self.assertNotIn('_state', done['skills'][0])

    def test_new_check_removes_earlier_check_downloads(self):
        self.server()
        first = self.request('/api/updates/check', {})[1]
        self.until(lambda r: not r['running'])
        temporary = Path(self.dashboard.update_reviews[first['id']]['temporary'])
        self.assertTrue(temporary.exists())
        second = self.request('/api/updates/check', {})[1]
        self.assertNotEqual(first['id'], second['id'])
        self.assertFalse(temporary.exists())
        self.assertEqual(list(self.dashboard.update_reviews), [second['id']])
        self.until(lambda r: not r['running'])

    def test_reopening_can_reconnect_after_heartbeat_expires_during_scan(self):
        self.server()
        gate = threading.Event()
        original = Reader.latest
        def held(reader, source):
            gate.wait(5)
            return original(reader, source)
        with patch.object(Reader, 'latest', held), patch('skill_pilot.web.server.HEARTBEAT_TIMEOUT', .02):
            try:
                self.request('/api/updates/check', {})
                self.server.last_heartbeat = time.monotonic() - 1
                watcher = threading.Thread(target=self.server.watch)
                watcher.start()
                time.sleep(.15)
                self.assertTrue(watcher.is_alive())
                self.assertTrue(self.request('/api/updates/progress')[1]['running'])
                self.assertEqual(self.request('/api/heartbeat', {})[0], 200)
            finally: gate.set()
            # The expired, page-less dashboard is meant to stop as soon as
            # its job finishes; do not poll HTTP after that shutdown point.
            self.server.update_thread.join(5)
            self.assertFalse(self.server.update_thread.is_alive())
            self.assertFalse(any(r['running'] for r in self.server.update_reviews.values()))
            watcher.join(2)
            self.assertFalse(watcher.is_alive())

    def test_single_apply_during_scan_and_later_results_preserve_applied_row(self):
        self.add_skill(); self.server()
        gate, entered = threading.Event(), threading.Event()
        original = Reader.current
        def held(reader, source, commit=None, tree=None):
            if source['path'] == 'zsecond':
                entered.set(); gate.wait(5)
            return original(reader, source, commit, tree)
        with patch.object(Reader, 'current', held):
            try:
                self.request('/api/updates/check', {})
                self.assertTrue(entered.wait(5))
                first = self.until(lambda r: r['done'] == 1 and r['running'])
                self.assertEqual(first['skills'][0]['name'], 'sample')
                body = {'confirmed': True, 'review_id': first['id'], 'skills': ['sample']}
                status, result = self.request('/api/updates/apply', body)
                self.assertEqual(status, 200, result)
                live = self.until(lambda r: r['skills'][0]['status'] == '已更新')
                self.assertTrue(live['running'])
                self.assertFalse(live['skills'][0]['has_update'])
                self.assertIn('new upstream', (self.fixture.local() / 'SKILL.md').read_text())
            finally: gate.set()
            done = self.until(lambda r: not r['running'])
        rows = {r['name']: r for r in done['skills']}
        self.assertEqual(rows['sample']['status'], '已更新')
        self.assertEqual(done['update_count'], 1)
        self.assertTrue(rows['zsecond']['has_update'])
        body['skills'] = ['zsecond']
        self.assertEqual(self.request('/api/updates/apply', body)[0], 200)
        self.assertIn('new', (self.home / '.skill-pilot/store/zsecond/SKILL.md').read_text())

    def test_unrelated_assignment_does_not_reject_single_apply_or_merge(self):
        self.add_skill()
        review = self.review()
        operations.apply_assign([{'skill': 'zsecond', 'agent': 'claude', 'enabled': True}], home=self.home)
        with patch('skill_pilot.research.merge', side_effect=self.fixture.mock_merge):
            self.assertIn('merge_id', updates.prepare_merge(review, 'sample'))
        updates.apply(review, ['sample'], confirmed=True)
        self.assertIn('claude', operations.load(self.home)[1]['skills']['zsecond']['assigned_agents'])

    def test_selected_skill_assignment_base_or_entry_change_is_rejected(self):
        for kind in ('assignment', 'base', 'entry'):
            with self.subTest(kind=kind):
                review = self.review()
                if kind == 'assignment':
                    result = operations.apply_assign([{'skill': 'sample', 'agent': 'claude', 'enabled': True}], home=self.home)
                    restore = lambda: operations.undo(result['id'], home=self.home)
                elif kind == 'base':
                    path = self.home / '.skill-pilot/bases/sample/SKILL.md'
                    old = path.read_bytes(); path.write_bytes(old + b'changed')
                    restore = lambda: path.write_bytes(old)
                else:
                    path = self.home / '.codex/skills/sample'
                    old = path.readlink(); path.unlink(); path.symlink_to(self.home / '.skill-pilot/bases/sample')
                    restore = lambda: (path.unlink(), path.symlink_to(old))
                try:
                    with self.assertRaisesRegex(ValueError, '已经变了'):
                        updates.apply(review, ['sample'], confirmed=True)
                finally: restore()

    def test_unassigned_and_self_made_skills_are_not_checked_or_counted(self):
        self.add_skill('zunused', assigned=False); self.add_skill('zmine'); self.add_skill('zbad')
        path = self.home / '.skill-pilot/registry.json'
        registry = json.loads(path.read_text())
        registry['skills']['zmine']['source'] = {'source': '自制'}
        registry['skills']['zbad']['source'] = {'source': 'example/repo', 'source_path': '../x'}
        path.write_text(json.dumps(registry))
        review = self.review()
        # 自制和未启用的只列入跳过清单；来源位置有误的仍报告检查失败。
        self.assertEqual({r['name']: r['status'] for r in review['skills']}, {'sample': '有更新', 'zbad': '没查清楚'})
        self.assertEqual((review['total'], review['repositories']), (2, 1))
        self.assertEqual([(r['code'], r['count'], r['names']) for r in review['unchecked']], [
            ('self_made', 1, ['zmine']),
            ('unused', 1, ['zunused']),
        ])
        self.assertNotIn('withdrawn', updates.public(review))
        self.server()  # 接口忽略旧的 withdrawn 字段
        first = self.request('/api/updates/check', {'withdrawn': True})[1]
        self.assertEqual(first['total'], 2)
        self.assertEqual(first['unchecked'], review['unchecked'])
        self.until(lambda r: not r['running'])

    def test_scope_explains_all_checked_partial_and_nothing(self):
        path = self.home / '.skill-pilot/registry.json'
        original = path.read_text()

        def opened(mutate):
            registry = json.loads(original)
            mutate(registry)
            path.write_text(json.dumps(registry))
            review = updates.create(self.home)
            self.addCleanup(shutil.rmtree, review['temporary'], True)
            return updates.public(review)

        def add(registry, name, source, assigned):
            registry['skills'][name] = {'source': source, 'assigned_agents': assigned, 'entries': [], 'requires': []}

        with self.subTest('all'):
            review = opened(lambda registry: None)
            self.assertEqual(review['total'], 1)
            self.assertEqual(review['unchecked'], [])
            self.assertTrue(review['checked_at'])
            self.assertNotIn('home', review)

        with self.subTest('partial'):
            def partial(registry):
                add(registry, 'zmade', {'source': '自制'}, ['codex'])
                add(registry, 'zidle', {'source': 'https://github.com/example/repo'}, [])
            review = opened(partial)
            self.assertEqual(review['total'], 1)
            self.assertEqual(review['repositories'], 1)
            self.assertEqual([(r['code'], r['count'], r['names']) for r in review['unchecked']], [
                ('self_made', 1, ['zmade']),
                ('unused', 1, ['zidle']),
            ])

        with self.subTest('none'):
            def nothing(registry):
                registry['skills']['sample']['source'] = {'source': '自制'}
                add(registry, 'zidle', {'source': 'https://github.com/example/repo'}, [])
            review = opened(nothing)
            self.assertEqual(review['total'], 0)
            self.assertEqual(review['repositories'], 0)
            self.assertEqual([(r['code'], r['count'], r['names']) for r in review['unchecked']], [
                ('self_made', 1, ['sample']),
                ('unused', 1, ['zidle']),
            ])
            self.assertEqual(review['skills'], [])

        with self.subTest('empty'):
            review = opened(lambda registry: registry.update(skills={}))
            self.assertEqual((review['total'], review['unchecked']), (0, []))

    def test_skipped_lists_distinguish_sources_without_changing_check_scope(self):
        path = self.home / '.skill-pilot/registry.json'
        registry = json.loads(path.read_text())
        for name, source, agents in [
            ('mine', {'source': '自制'}, ['codex']),
            ('local-code', {'source': '本机源码'}, ['codex']),
            ('local-doc', {'source': '本机文档生成'}, ['codex']),
            ('unknown', {'source': '来源不明'}, ['codex']),
            ('missing', {}, ['codex']),
            ('unsupported', {'source': 'https://example.test/skills'}, ['codex']),
            ('unused-mine', {'source': '自制'}, []),
            ('bad', {'source': 'https://github.com/example/repo', 'source_path': '../bad'}, ['codex']),
        ]:
            registry['skills'][name] = {'source': source, 'assigned_agents': agents, 'requires': [], 'entries': []}
        path.write_text(json.dumps(registry))
        before = store.physical_state(self.home)
        review = updates.create(self.home)
        self.addCleanup(shutil.rmtree, review['temporary'], True)
        result = updates.public(review)
        self.assertEqual(result['total'], 2)  # 正常技能和来源写错的技能仍在检查范围中。
        self.assertEqual([(r['code'], r['reason'], r['count'], r['names']) for r in result['unchecked']], [
            ('self_made', '自制技能', 1, ['mine']),
            ('local', '本机维护', 2, ['local-code', 'local-doc']),
            ('unknown', '来源未记录', 2, ['missing', 'unknown']),
            ('unsupported', '暂不支持检查', 1, ['unsupported']),
            ('unused', '未启用', 1, ['unused-mine']),
        ])
        names = [name for group in result['unchecked'] for name in group['names']]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(group['description'] for group in result['unchecked']))
        self.assertEqual(before, store.physical_state(self.home))

        # 旧的检查结果可从现存记录补齐名单；已经删除的技能不留在清单中。
        review['unchecked'] = [{'reason': '没有上游来源', 'count': 6}]
        del registry['skills']['local-code']
        path.write_text(json.dumps(registry))
        local = next(group for group in updates.public(review)['unchecked'] if group['code'] == 'local')
        self.assertEqual((local['count'], local['names']), (1, ['local-doc']))

    def pin_baseline(self):
        path = self.home / '.skill-pilot/registry.json'
        registry = operations.load(self.home)[1]
        baseline = store.git_tree(self.home / '.skill-pilot/bases/sample')
        registry['skills']['sample']['source'] = {'source': 'https://github.com/example/repo',
                                                'source_path': 'skills/sample', 'tree_sha': baseline}
        path.write_text(json.dumps(registry))
        return baseline

    def test_equal_tree_skips_history_and_download_even_with_local_edits(self):
        baseline = self.pin_baseline()
        (self.fixture.local() / 'SKILL.md').write_text(self.fixture.original + 'my edit\n')
        with patch.object(Reader, 'latest', return_value=('commit', baseline)), \
                patch.object(Reader, 'inspect') as inspect, patch.object(Reader, 'repository') as download:
            review = self.review()
        inspect.assert_not_called(); download.assert_not_called()
        self.assertEqual(review['skills'][0]['status'], '已是最新')
        self.assertTrue(review['skills'][0]['local_modified'])

    def test_unreliable_baseline_compares_current_without_history(self):
        (self.home / '.skill-pilot/bases/sample/SKILL.md').write_text('invalid base')
        original, commands = github.git, []
        def record(directory, *args, **kwargs):
            commands.append(args)
            return original(directory, *args, **kwargs)
        with patch.object(Reader, 'inspect', side_effect=ValueError('history forbidden')) as inspect, \
                patch.object(github, 'git', record):
            review = self.review()
        inspect.assert_not_called()
        self.assertFalse(any(args[0] == 'rev-list' or '--unshallow' in args for args in commands))
        self.assertFalse(review['errors'])
        self.assertEqual(review['skills'][0]['status'], '有更新')
        self.assertIsNone(review['skills'][0]['local_modified'])
        self.assertTrue(review['skills'][0]['baseline_uncertain'])

    def test_unreliable_base_equal_to_current_is_latest_without_history(self):
        shutil.copytree(self.fixture.folder, self.fixture.local(), dirs_exist_ok=True)
        (self.home / '.skill-pilot/bases/sample/SKILL.md').write_text('invalid base')
        before = self.fixture.fingerprint()
        with patch.object(Reader, 'inspect', side_effect=AssertionError('history forbidden')):
            review = self.review()
        self.assertEqual(review['skills'][0]['status'], '已是最新')
        self.assertEqual(before, self.fixture.fingerprint())

    def test_unknown_default_update_writes_reliable_base_and_next_check_is_fast(self):
        self.fixture.modified()
        before = self.fixture.fingerprint()
        with patch.object(Reader, 'inspect', side_effect=AssertionError('history forbidden')):
            review = self.review()
            row = review['skills'][0]
            result = updates.apply(review, ['sample'], confirmed=True)
            with patch.object(Reader, 'latest', return_value=(row['commit'], row['tree_sha'])), \
                    patch.object(Reader, 'current', side_effect=AssertionError('download forbidden')):
                latest = self.review()['skills'][0]
        skill = operations.load(self.home)[1]['skills']['sample']
        self.assertEqual(skill['base_tree_sha'], row['tree_sha'])
        self.assertEqual(skill['base_normalized_tree_sha'], store.git_tree(self.home / '.skill-pilot/bases/sample'))
        self.assertEqual(latest['status'], '已是最新')
        self.assertFalse(latest['baseline_uncertain'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fixture.fingerprint())

    def test_keep_selection_calibrates_once_readonly_with_longer_deadline(self):
        self.fixture.modified(); self.server()
        first = self.request('/api/updates/check', {})[1]
        self.until(lambda r: not r['running'])
        review = self.server.update_reviews[first['id']]
        before = self.fixture.fingerprint()
        original, deadlines = Reader.inspect, []
        def record(reader, source, local):
            deadlines.append(reader.remaining(300))
            return original(reader, source, local)
        body = {'review_id': first['id'], 'name': 'sample'}
        with patch.object(Reader, 'inspect', record):
            status, changes = self.request('/api/updates/calibrate', body)
            self.assertEqual(status, 200, changes)
            self.assertIn('my local edit', changes['comparison'])
            self.assertNotIn('my local edit', review['skills'][0]['comparison'])
            self.assertEqual(self.request('/api/updates/calibrate', body)[0], 200)
            self.assertEqual(before, self.fixture.fingerprint())
            updates.apply(review, ['sample'], {'sample': 'keep'}, confirmed=True)
        self.assertEqual(len(deadlines), 1)
        self.assertGreater(deadlines[0], updates.REPOSITORY_TIMEOUT)
        self.assertEqual(review['skills'][0]['base_commit'], self.fixture.old)
        self.assertEqual(self.review()['skills'][0]['status'], '这一版已看过')

    def test_unknown_merge_calibrates_before_three_way_material(self):
        self.fixture.modified(); review = self.review()
        from skill_pilot import research
        original = Reader.inspect
        with patch.object(Reader, 'inspect', autospec=True, side_effect=original) as inspect, \
                patch.object(research, 'merge', side_effect=self.fixture.mock_merge) as merge:
            updates.prepare_merge(review, 'sample')
        inspect.assert_called_once()
        self.assertEqual(merge.call_args.args[0]['base'][0]['content'], self.fixture.original)

    def test_calibration_failure_is_readonly_and_retry_has_fresh_temporary(self):
        self.fixture.modified(); review = self.review()
        before = self.fixture.fingerprint()
        original, roots = Reader.inspect, []
        def retry(reader, source, local):
            roots.append(reader.root)
            if len(roots) == 1:
                (reader.root / 'partial').mkdir()
                raise ValueError('historical timeout')
            return original(reader, source, local)
        with patch.object(Reader, 'inspect', retry):
            with self.assertRaisesRegex(ValueError, 'historical timeout'):
                updates.apply(review, ['sample'], {'sample': 'keep'}, confirmed=True)
            self.assertEqual(before, self.fixture.fingerprint())
            updates.calibrate(review, 'sample')
        self.assertNotEqual(roots[0], roots[1])
        self.assertEqual(before, self.fixture.fingerprint())

    def test_upstream_change_during_calibration_requires_new_review(self):
        self.fixture.modified(); review = self.review()
        before = self.fixture.fingerprint()
        self.fixture.manifest.write_text(self.fixture.manifest.read_text() + 'another update\n')
        self.fixture.commit('another update')
        with self.assertRaisesRegex(ValueError, '重新检查'):
            updates.calibrate(review, 'sample')
        self.assertTrue(review['skills'][0]['baseline_uncertain'])
        self.assertEqual(before, self.fixture.fingerprint())

    def test_repository_download_is_shared_by_multiple_skills(self):
        self.add_skill()
        original, commands = github.git, []
        def record(directory, *args, **kwargs):
            commands.append(args)
            return original(directory, *args, **kwargs)
        with patch.object(github, 'git', record): review = self.review()
        self.assertFalse(review['errors'])
        self.assertEqual(sum(args[0] == 'clone' for args in commands), 1)
        self.assertEqual(sum(args[:2] == ('fetch', '--unshallow') for args in commands), 0)

    def test_repository_timeout_does_not_block_other_results(self):
        self.add_skill()
        registry = operations.load(self.home)[1]
        registry['skills']['sample']['source'] = {'source': 'https://github.com/example/slow'}
        registry['skills']['zsecond']['source'] = {'source': 'https://github.com/example/fast'}
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        self.server()
        def api(endpoint, timeout=45):
            if '/slow/' in endpoint:
                try:
                    subprocess.run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=timeout, capture_output=True)
                except subprocess.TimeoutExpired:
                    raise ValueError('这个仓库检查超时，可以稍后重试。')
            return [{'sha': 'commit', 'commit': {'tree': {'sha': 'new-tree'}}}]
        with patch.object(updates, 'REPOSITORY_TIMEOUT', .3), patch.object(github, 'api', api), \
                patch.object(Reader, 'tree', return_value='new-tree'), \
                patch.object(Reader, 'current', side_effect=ValueError('fast result')):
            self.request('/api/updates/check', {})
            early = self.until(lambda r: r['done'] == 1)
            self.assertTrue(early['running'])
            self.assertEqual(early['skills'][0]['name'], 'zsecond')
            started = time.monotonic()
            done = self.until(lambda r: not r['running'])
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(done['remaining'], 0)
        self.assertIn('可以稍后重试', next(r for r in done['skills'] if r['name'] == 'sample')['error'])

    def test_repository_concurrency_is_bounded_at_four(self):
        registry = operations.load(self.home)[1]
        for index in range(6):
            name = 'worker' + str(index)
            shutil.copytree(self.fixture.local(), self.home / '.skill-pilot/store' / name)
            shutil.copytree(self.home / '.skill-pilot/bases/sample', self.home / '.skill-pilot/bases' / name)
            registry['skills'][name] = {**registry['skills']['sample'], 'entries': [],
                'source': {'source': 'https://github.com/example/repo' + str(index)}}
        registry['skills'].pop('sample')
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        lock = threading.Lock()
        active, maximum = 0, 0
        def latest(reader, source):
            nonlocal active, maximum
            with lock: active += 1; maximum = max(maximum, active)
            time.sleep(.1)
            with lock: active -= 1
            raise ValueError('controlled result')
        with patch.object(Reader, 'latest', latest): review = self.review()
        self.assertEqual(maximum, 4)
        self.assertEqual((review['done'], review['remaining']), (6, 0))

    def test_lark_check_participates_in_progress_without_github_download(self):
        registry = operations.load(self.home)[1]
        registry['skills']['sample']['source'] = {'record': {'sourceType': 'well-known',
            'sourceUrl': 'https://open.feishu.cn/lark-cli/skills/regular/.well-known/agent-skills/sample.tar.gz'}}
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        with patch.object(lark, 'check', return_value={'available': False, 'message': '已是最新'}), \
                patch.object(Reader, 'repository') as download:
            review = self.review()
        download.assert_not_called()
        self.assertEqual((review['done'], review['latest_count'], review['remaining']), (1, 1, 0))
        self.assertEqual(review['lark']['skill_count'], 1)
    def test_lark_package_reads_obey_group_deadline(self):
        sources = {'lark-doc': {'base_url': 'https://open.feishu.cn/lark-cli/skills/regular'}}
        with patch.object(lark, 'download') as download:
            with self.assertRaisesRegex(ValueError, '检查超时'):
                lark.prepare(sources, str(self.home), '1.0.98', time.monotonic() - 1)
            download.assert_not_called()
        archive = test_updates.LarkTests().archive()
        import hashlib
        index = {'skills': [{'name': 'lark-doc', 'type': 'archive', 'url': './lark-doc.tar.gz',
                            'digest': 'sha256:' + hashlib.sha256(archive).hexdigest()}]}
        with patch.object(lark, 'download', side_effect=[json.dumps(index).encode(), archive]) as download:
            lark.prepare(sources, str(self.home), '1.0.98', time.monotonic() + 1)
        self.assertTrue(all(0 < call.kwargs['timeout'] <= 1 for call in download.call_args_list))



if __name__ == '__main__':
    unittest.main()
