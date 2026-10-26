"""Queue integration checks use isolated homes, real writes, and loopback HTTP."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import cleanup, operations, self_update, store, team, updates
from skill_pilot.scan import content_digest, scan
from skill_pilot.sources import github
from skill_pilot.sync import visibility
from skill_pilot.web import runtime
from skill_pilot.web.server import Dashboard


class JobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot jobs ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'Acme home')
        curated = patch.object(operations, 'curated_dependencies', return_value=({}, {}))
        curated.start()
        self.addCleanup(curated.stop)
        for name in ('acme-b', 'acme-c', 'acme-dep', 'acme-extra'):
            self.skill(self.home / '.codex/skills' / name, name)
        store.apply(store.preview(self.home)['plan_id'], self.home)
        self.registry(lambda r: [r['skills'][n].update(source={
            'source': 'https://github.com/Acme/skills.git', 'repository': 'Acme/skills',
            'source_path': 'skills/' + n + '/SKILL.md'}) for n in r['skills']])
        operations._commit(self.home, operations.load(self.home)[1], [], [], 'Acme fixture')
        (self.home / '.skill-pilot/labels.json').write_text(json.dumps({'groups': [{
            'name': '示例', 'skills': {'acme-b': ['示例乙', '示例'], 'acme-c': ['示例丙', '示例']}}]}))
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
                from skill_pilot.platform_support import rmtree
                rmtree(review['temporary'], ignore_errors=True)

    def skill(self, path, name, body='原内容'):
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例技能\n---\n{body}\n')
        return path

    def registry(self, action):
        path = self.home / '.skill-pilot/registry.json'
        registry = json.loads(path.read_text())
        action(registry)
        path.write_text(json.dumps(registry))

    def request(self, path, body=None, extra=None, timeout=3):
        headers = {'X-Skill-Pilot-Token': self.server.token, 'Origin': self.server.origin,
                   'Content-Type': 'application/json'}
        headers.update(extra or {})
        request = urllib.request.Request(self.server.origin + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def ok(self, path, body=None, **kw):
        status, result = self.request(path, body, **kw)
        self.assertEqual(status, 200, result)
        return result

    def submit(self, kind, **parameters):
        return self.ok('/api/jobs', {'kind': kind, 'confirmed': True, **parameters})['job']

    def wait_jobs(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.ok('/api/jobs')
            if all(i['status'] not in {'waiting', 'running'} for i in result['items']) and not self.server.jobs_busy():
                return result['items']
            time.sleep(.01)
        self.fail('任务没有按时完成：' + str(result))

    def assignment(self, name='acme-b', agent='claude', enabled=True):
        changes = [{'skill': name, 'agent': agent, 'enabled': enabled}]
        plan = operations.preview_assign(changes, self.home, recheck=True)
        return {'changes': changes, 'seen': {k: plan[k] for k in ('changes', 'dependency_warnings', 'path_warnings')}}

    def cleanup_parameters(self, kind, *names):
        action = cleanup.delete_preview if kind == 'cleanup_delete' else cleanup.preview
        plan = action(list(names), self.home)
        fields = ['dependency_warnings', 'path_warnings'] + (['irreplaceable'] if kind == 'cleanup_delete' else [])
        return {'skills': list(names), 'seen': {k: plan[k] for k in fields}}

    @contextmanager
    def blocked_first(self):
        entered, release = threading.Event(), threading.Event()
        execute, calls = self.server.execute_job, []
        def delayed(item):
            calls.append(item['kind'])
            if len(calls) == 1:
                entered.set()
                if not release.wait(8):
                    raise AssertionError('测试未释放首个任务')
            return execute(item)
        with patch.object(self.server, 'execute_job', side_effect=delayed):
            self.submit('assignment', **self.assignment('acme-extra'))
            self.assertTrue(entered.wait(2))
            try:
                yield calls
            finally:
                release.set()
                self.wait_jobs()

    def install_review(self, name='acme-a'):
        temporary = self.root / ('install ' + name)
        path = self.skill(temporary / name, name)
        review = {'id': temporary.name, 'temporary': str(temporary), 'skills': [
            {'name': name, 'path': str(path), 'digest': content_digest(path),
             'source': {'source': 'https://github.com/Acme/skills.git', 'repository': 'Acme/skills',
                        'source_path': 'skills/' + name + '/SKILL.md'}}]}
        self.server.install_reviews[review['id']] = review
        return review

    def update_review(self, *names):
        review = updates.create(self.home)
        self.server.update_reviews[review['id']] = review
        registry = operations.load(self.home)[1]['skills']
        rows = []
        for name in names:
            path = self.skill(Path(review['temporary']) / name, name, '新版内容')
            tree = store.git_tree(path)
            rows.append({'name': name, 'source': {'kind': 'github', 'repository': 'Acme/skills', 'path': 'skills/' + name},
                'has_update': True, 'status': '有更新', 'local_modified': False, '_upstream_path': str(path),
                '_state': updates.skill_state(self.home, name, registry[name]), 'tree_sha': tree,
                'normalized_tree_sha': tree, 'commit': 'c' * 40})
        review.update(running=False, skills=rows, done=len(rows), update_count=len(rows), remaining=0,
                      _groups={'Acme/skills': [(n, registry[n], {}) for n in names]})
        return review

    def test_install_with_dependency_then_withdraw_then_update_and_undo(self):
        review = self.install_review()
        update = self.update_review('acme-c')
        withdraw = self.cleanup_parameters('cleanup_withdraw', 'acme-b')
        entered, release = threading.Event(), threading.Event()
        execute, calls = self.server.execute_job, []
        def delayed(item):
            calls.append(item['kind'])
            if len(calls) == 1:
                entered.set()
                release.wait(8)
            return execute(item)
        # The install uses the same confirmed dependency catalog as ordinary installs.
        with patch.object(operations, 'curated_dependencies', return_value=({'acme-a': {'acme-dep'}}, {})), \
                patch.object(self.server, 'execute_job', side_effect=delayed):
            first = self.submit('install', review_id=review['id'], skills=['acme-a'], agents=['claude'])
            try:
                self.assertTrue(entered.wait(2))
                second = self.submit('cleanup_withdraw', **withdraw)
                third = self.submit('update', review_id=update['id'], skills=['acme-c'])
                self.assertEqual([i['id'] for i in self.ok('/api/jobs')['items']], [first['id'], second['id'], third['id']])
            finally:
                release.set()
            items = self.wait_jobs()
        self.assertEqual(calls, ['install', 'cleanup_withdraw', 'update'])
        self.assertEqual([i['status'] for i in items], ['done'] * 3, items)
        self.assertEqual(set(visibility(self.home)['claude']['available']), {'acme-a', 'acme-dep'})
        self.assertEqual(operations.load(self.home)[1]['skills']['acme-b']['assigned_agents'], [])
        self.assertIn('新版内容', (self.home / '.skill-pilot/store/acme-c/SKILL.md').read_text())
        self.assertIn('acme-dep', items[0]['skills'])
        self.assertEqual(items[1]['summary'], '撤下「示例乙」')
        for item in reversed(items):
            self.assertEqual(len(item['operations']), 1)
            for identifier in item['operations']:
                self.ok('/api/undo', {'id': identifier, 'confirmed': True,
                                     'revision': operations.table(self.home)['revision']})
        self.assertNotIn('acme-a', operations.load(self.home)[1]['skills'])
        self.assertIn('acme-b', visibility(self.home)['codex']['available'])
        self.assertNotIn('新版内容', (self.home / '.skill-pilot/store/acme-c/SKILL.md').read_text())

    def test_slow_update_does_not_block_submissions_or_read_routes(self):
        review = self.update_review('acme-c')
        entered, release = threading.Event(), threading.Event()
        apply = updates.apply
        def delayed(*args, **kw):
            entered.set()
            release.wait(8)
            return apply(*args, **kw)
        with patch.object(updates, 'apply', side_effect=delayed):
            self.submit('update', review_id=review['id'], skills=['acme-c'])
            try:
                self.assertTrue(entered.wait(2))
                start = time.monotonic()
                next_job = self.submit('assignment', **self.assignment())
                self.assertEqual(next_job['status'], 'waiting')
                for path in ('/api/jobs', '/api/team/queue', '/api/table', '/api/cleanup', '/api/updates/progress', '/api/scan'):
                    self.ok(path, timeout=.75)
                self.assertLess(time.monotonic() - start, 2)
            finally:
                release.set()
            self.assertEqual([i['status'] for i in self.wait_jobs()], ['done', 'done'])

    def test_duplicates_ignore_seen_and_clear_keeps_active_items(self):
        parameters = self.assignment()
        with self.blocked_first():
            first = self.submit('assignment', **parameters)
            count = self.ok('/api/jobs')['version']
            for _ in range(3):
                result = self.ok('/api/jobs', {'kind': 'assignment', 'confirmed': True, **parameters,
                    'seen': {**parameters['seen'], 'dependency_warnings': ['已看到的额外提醒']}})
                self.assertTrue(result['duplicate'])
                self.assertEqual(result['job']['id'], first['id'])
                self.assertEqual(result['version'], count)
            self.assertEqual(len(self.ok('/api/jobs/clear', {})['items']), 2)
        items = self.wait_jobs()
        self.assertEqual(len(items), 2)
        self.assertEqual(self.ok('/api/jobs')['version'], 6)
        self.assertEqual(self.ok('/api/jobs/clear', {})['items'], [])

    def test_withdraw_then_delete_and_delete_then_restore_are_explicit(self):
        withdraw = self.cleanup_parameters('cleanup_withdraw', 'acme-b')
        delete = self.cleanup_parameters('cleanup_delete', 'acme-b')
        with self.blocked_first():
            self.submit('cleanup_withdraw', **withdraw)
            self.submit('cleanup_delete', **delete)
            self.submit('restore', skills=['acme-b'])
        items = self.wait_jobs()
        self.assertEqual([i['status'] for i in items], ['done', 'done', 'done', 'failed'])
        self.assertEqual(items[-1]['message'], '示例乙：已经删除，没有恢复。')
        self.assertNotIn('acme-b', operations.load(self.home)[1]['skills'])

    def test_assignment_before_update_cannot_overwrite_old_review(self):
        review = self.update_review('acme-c')
        with self.blocked_first():
            self.submit('assignment', **self.assignment('acme-c'))
            self.submit('update', review_id=review['id'], skills=['acme-c'])
        item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'failed')
        self.assertIn('内容或分配已经变了', item['message'])
        self.assertIn('重新检查更新', item['message'])
        self.assertNotIn('新版内容', (self.home / '.skill-pilot/store/acme-c/SKILL.md').read_text())

    def test_dependency_changes_need_confirmation_and_retry_uses_new_seen(self):
        parameters = self.assignment()
        with self.blocked_first():
            job = self.submit('assignment', **parameters)
            self.registry(lambda r: r['skills']['acme-b'].update(requires=['acme-dep']))
        item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'attention')
        self.assertEqual(item['attention'], 'confirmation')
        self.assertIn({'skill': 'acme-dep', 'agent': 'claude', 'enabled': True}, item['preview']['changes'])
        self.assertNotIn('acme-b', visibility(self.home)['claude']['available'])
        retried = self.ok('/api/jobs/retry', {'id': job['id'], 'seen': item['preview']})['job']
        self.assertNotEqual(retried['id'], job['id'])
        self.assertNotIn(job['id'], [i['id'] for i in self.ok('/api/jobs')['items']])
        self.assertEqual(self.wait_jobs()[-1]['status'], 'done')
        self.assertIn('acme-dep', visibility(self.home)['claude']['available'])

    def test_external_entry_replacement_is_a_conflict_and_stays_intact(self):
        parameters = self.assignment()
        with self.blocked_first():
            self.submit('assignment', **parameters)
            path = self.home / '.codex/skills/acme-b'
            path.unlink()
            self.skill(path, 'acme-b', '外部内容')
        item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'attention')
        self.assertEqual(item['attention'], 'conflict')
        self.assertEqual(item['conflicts'][0]['skill'], 'acme-b')
        self.assertEqual(item['conflicts'][0]['agent'], 'claude')
        self.assertIn('外部内容', (path / 'SKILL.md').read_text())

    def test_assignment_rechecks_bounded_concurrent_changes(self):
        apply = operations.apply_assign
        calls = []
        def changing(*args, **kw):
            calls.append(True)
            if len(calls) < 3:
                raise operations.AssignmentChanged('状态变化')
            return apply(*args, **kw)
        with patch.object(operations, 'apply_assign', side_effect=changing):
            self.submit('assignment', **self.assignment())
            self.assertEqual(self.wait_jobs()[-1]['status'], 'done')
        self.assertEqual(len(calls), 3)
        with patch.object(operations, 'apply_assign', side_effect=operations.AssignmentChanged('状态变化')) as apply:
            self.submit('assignment', **self.assignment('acme-c'))
            self.assertEqual(self.wait_jobs()[-1]['status'], 'failed')
            self.assertEqual(apply.call_count, 3)

    def test_cleanup_new_dependency_warning_requires_confirmation(self):
        parameters = self.cleanup_parameters('cleanup_withdraw', 'acme-b')
        with self.blocked_first():
            job = self.submit('cleanup_withdraw', **parameters)
            self.registry(lambda r: r['skills']['acme-c'].update(requires=['acme-b']))
        item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'attention')
        self.assertTrue(item['preview']['dependency_warnings'])
        self.ok('/api/jobs/retry', {'id': job['id'], 'seen': item['preview']})
        self.assertEqual(self.wait_jobs()[-1]['status'], 'done')

    def test_delete_new_irreplaceable_warning_and_path_warning(self):
        parameters = self.cleanup_parameters('cleanup_delete', 'acme-b')
        warning = {'file': '~/Acme workflow.md', 'entries': ['.codex/skills/acme-b']}
        with patch.object(operations, 'path_warnings', return_value=[warning]):
            with self.blocked_first():
                self.submit('cleanup_delete', **parameters)
                path = self.home / '.skill-pilot/store/acme-b/SKILL.md'
                path.write_text(path.read_text() + '\n手动修改\n')
        item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'attention')
        self.assertEqual({r['field'] for r in item['new_impacts']}, {'irreplaceable', 'path_warnings'})
        self.assertTrue(path.exists())

    def test_update_partial_failure_retry_only_repeats_the_failed_skill(self):
        review = self.update_review('acme-b', 'acme-c')
        apply, calls = updates._apply_group, []
        def partial(review, names, *args):
            calls.append(names)
            if names == ['acme-c'] and calls.count(names) == 1:
                raise ValueError('acme-c：示例失败，可以重试。')
            return apply(review, names, *args)
        with patch.object(updates, '_apply_group', side_effect=partial):
            job = self.submit('update', review_id=review['id'], skills=['acme-b', 'acme-c'])
            item = self.wait_jobs()[-1]
            self.assertEqual(item['status'], 'failed')
            self.assertEqual(item['updated'], ['acme-b'])
            self.assertEqual([r['name'] for r in item['failed']], ['acme-c'])
            self.assertEqual(item['failed'][0]['error'], '示例丙：示例失败，可以重试。')
            self.assertIn('已更新：示例乙。未更新：示例丙：', item['message'])
            self.assertNotIn('acme-', item['message'])
            self.assertEqual(len(item['operations']), 1)
            retry = self.ok('/api/jobs/retry', {'id': job['id']})['job']
            self.assertEqual(retry['skills'], ['acme-c'])
            completed = self.wait_jobs()[-1]
            self.assertEqual(completed['status'], 'done')
            self.assertEqual(len(completed['operations']), 2)
            self.assertEqual(completed['updated'], ['acme-b', 'acme-c'])
            self.assertEqual(completed['message'], '此前已更新：示例乙。已更新：示例丙。')
        self.assertEqual(calls, [['acme-b'], ['acme-c'], ['acme-c']])

    def test_failed_task_does_not_block_an_independent_task(self):
        with self.blocked_first():
            self.submit('install', review_id='expired', skills=['acme-a'], agents=['claude'])
            self.submit('assignment', **self.assignment())
        items = self.wait_jobs()
        self.assertEqual([i['status'] for i in items], ['done', 'failed', 'done'])
        self.assertIn('安装预览已失效', items[1]['message'])

    def test_install_conflicts_hide_download_paths_and_missing_agent_fails(self):
        review = self.install_review('acme-b')
        self.submit('install', review_id=review['id'], skills=['acme-b'], agents=['claude'])
        item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'attention')
        self.assertEqual(item['conflicts'][0]['name'], 'acme-b')
        self.assertNotIn(str(self.root), json.dumps(item))
        self.assertNotIn('incoming', item['conflicts'][0])
        self.submit('install', review_id=review['id'], skills=['acme-b'], agents=['missing'])
        self.assertIn('已不再接入', self.wait_jobs()[-1]['message'])

    def test_install_revalidates_downloads_before_writing(self):
        review = self.install_review()
        with self.blocked_first():
            self.submit('install', review_id=review['id'], skills=['acme-a'], agents=['claude'])
            path = Path(review['skills'][0]['path']) / 'SKILL.md'
            path.write_text(path.read_text() + '\nchanged\n')
        self.assertEqual(self.wait_jobs()[-1]['status'], 'failed')
        self.assertNotIn('acme-a', operations.load(self.home)[1]['skills'])

    def test_rollback_stops_waiting_jobs_and_backup_restore_unblocks(self):
        backup = operations.apply_assign([{'skill': 'acme-extra', 'agent': 'claude', 'enabled': True}],
                                         confirmed=True, home=self.home)['backup_id']
        entered, release = threading.Event(), threading.Event()
        def rollback(item):
            entered.set()
            release.wait(8)
            raise operations.RollbackError('自动恢复失败')
        with patch.object(self.server, 'execute_job', side_effect=rollback) as work:
            self.submit('assignment', **self.assignment())
            try:
                self.assertTrue(entered.wait(2))
                self.submit('restore', skills=['acme-c'])
            finally:
                release.set()
            self.wait_jobs()
            self.assertEqual(work.call_count, 1)
        result = self.ok('/api/jobs')
        self.assertTrue(result['blocked'])
        self.assertEqual([i['status'] for i in result['items'][-2:]], ['failed', 'failed'])
        status, rejected = self.request('/api/jobs', {'kind': 'restore', 'skills': ['acme-b'], 'confirmed': True})
        self.assertEqual(status, 409)
        self.assertEqual(rejected['error'], result['blocked'])
        self.assertTrue(rejected['blocked'])
        self.assertEqual(runtime.snapshot(self.server)['jobs_blocked'], result['blocked'])
        self.ok('/api/restore', {'backup_id': backup, 'confirmed': True})
        self.assertEqual(self.ok('/api/jobs')['blocked'], '')
        self.submit('assignment', **self.assignment())
        self.assertEqual(self.wait_jobs()[-1]['status'], 'done')

    def test_read_only_denies_all_queue_posts_without_changes(self):
        before = store.physical_state(self.home)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            for path, body in [('/api/jobs', {'kind': 'restore', 'skills': ['acme-b'], 'confirmed': True}),
                               ('/api/jobs/retry', {'id': 'missing'}), ('/api/jobs/clear', {})]:
                self.assertEqual(self.request(path, body)[0], 403)
        self.assertEqual(self.ok('/api/jobs')['items'], [])
        self.assertEqual(store.physical_state(self.home), before)

    def test_shape_confirmation_security_and_upgrade_rejections(self):
        for body in ({}, {'kind': []}, {'kind': 'restore', 'skills': 'acme-b'},
                     {'kind': 'install', 'skills': ['acme-a'], 'review_id': 3, 'agents': []},
                     {'kind': 'update', 'skills': ['acme-b'], 'review_id': 'x', 'choices': []},
                     {'kind': 'assignment', 'changes': [{'skill': 'acme-b', 'agent': 'codex', 'enabled': 1}], 'seen': {}},
                     {'kind': 'cleanup_withdraw', 'skills': ['acme-b'], 'seen': {'dependency_warnings': []}}):
            self.assertEqual(self.request('/api/jobs', {**body, 'confirmed': True})[0], 400, body)
        self.assertEqual(self.request('/api/jobs', {'kind': 'restore', 'skills': ['acme-b']})[0], 400)
        self.assertEqual(self.request('/api/jobs/retry', {'id': 'missing'})[0], 400)
        body = {'kind': 'restore', 'skills': ['acme-b'], 'confirmed': True}
        for extra in ({'Origin': 'https://elsewhere.example'}, {'Host': 'elsewhere.example'}, {'X-Skill-Pilot-Token': ''}):
            self.assertEqual(self.request('/api/jobs', body, extra)[0], 403)
        for path in ('/api/jobs', '/api/jobs/retry', '/api/jobs/clear'):
            self.server.reload_pending = True
            self.assertEqual(self.request(path, body)[0], 503)
            self.server.reload_pending = False
            self.server.program_progress.update(running=True, phase='apply')
            self.assertEqual(self.request(path, body)[0], 503)
            self.server.program_progress.update(running=False, phase='idle')
        self.assertEqual(self.ok('/api/jobs')['items'], [])

    def test_new_checks_keep_waiting_and_running_update_downloads_until_done(self):
        review = self.update_review('acme-c')
        temporary = Path(review['temporary'])
        with self.blocked_first():
            self.submit('update', review_id=review['id'], skills=['acme-c'])
            with patch.object(updates, 'check', side_effect=lambda review, lock: review.update(running=False)):
                fresh = self.ok('/api/updates/check', {})
                self.server.update_thread.join(2)
            self.assertNotEqual(fresh['id'], review['id'])
            self.assertTrue(temporary.is_dir())
            self.assertIn(review['id'], self.server.update_reviews)
        self.assertEqual(self.wait_jobs()[-1]['status'], 'done')
        self.assertFalse(temporary.exists())
        self.assertNotIn(review['id'], self.server.update_reviews)

    def test_sync_writes_wait_but_remote_team_requests_do_not_take_write_lock(self):
        with ThreadPoolExecutor() as pool:
            with self.blocked_first():
                revision = operations.table(self.home)['revision']
                write = pool.submit(self.request, '/api/updates/watch', {
                    'name': 'acme-c', 'mode': 'skill', 'revision': revision, 'confirmed': True})
                time.sleep(.05)
                self.assertFalse(write.done())
                with patch.object(team, 'rate', return_value={'message': '评价已保存。'}):
                    self.ok('/api/team/ratings', {'key': 'Acme', 'stars': 5}, timeout=.75)
                self.ok('/api/jobs', timeout=.75)
            self.assertIn(write.result()[0], (200, 409))

    def test_refresh_and_program_upgrade_wait_for_jobs(self):
        replaced, upgraded = threading.Event(), threading.Event()
        self.server.reload_callback = replaced.set
        with self.blocked_first(), patch.object(runtime, 'version', return_value='new'):
            result = self.ok('/api/runtime/refresh', {})
            self.assertTrue(result['busy'])
            self.assertFalse(replaced.wait(.05))
        self.assertTrue(replaced.wait(2))
        self.server.reload_pending = False
        with patch.object(self_update, 'apply', side_effect=lambda *a: upgraded.set() or {'available': False}):
            with self.blocked_first():
                self.server.program_review = {'target': 'new', 'available': True, 'temporary': ''}
                self.server.start_program('apply', 'new')
                self.assertFalse(upgraded.wait(.05))
            self.assertTrue(upgraded.wait(2))
            self.server.program_thread.join(3)

    def test_close_waits_for_running_job_and_never_starts_waiting_job(self):
        dashboard = Dashboard(scan(self.home))
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        execute = dashboard.execute_job
        def delayed(item):
            entered.set()
            release.wait(8)
            return execute(item)
        with patch.object(dashboard, 'execute_job', side_effect=delayed) as work:
            dashboard.queue_job({'kind': 'assignment', 'confirmed': True, **self.assignment()})
            try:
                self.assertTrue(entered.wait(2))
                dashboard.queue_job({'kind': 'assignment', 'confirmed': True, **self.assignment('acme-c')})
                closer = threading.Thread(target=lambda: (dashboard.server_close(), closed.set()))
                closer.start()
                self.assertFalse(closed.wait(.05))
                self.assertTrue(dashboard.closing)
            finally:
                release.set()
                closer.join(5)
            self.assertTrue(closed.is_set())
            self.assertEqual(work.call_count, 1)
        self.assertEqual([i['status'] for i in dashboard.jobs_status()['items']], ['done', 'waiting'])

    def test_team_installs_and_other_jobs_share_order_and_legacy_filter(self):
        review = self.install_review()
        with patch.object(team, '_fetch', return_value=(None, None, [{
                'key': 'Acme:key', 'listings': [{'name': 'acme-a', 'title': '示例甲'}]}])), \
                patch.object(team, 'install', side_effect=lambda home, key: {
                    **operations.install(review, ['acme-a'], [], home=home), 'installed': ['acme-a'], 'reuse': []}):
            with self.blocked_first() as calls:
                legacy = self.ok('/api/team/queue', {'key': 'Acme:key'})['items'][0]
                self.submit('cleanup_withdraw', **self.cleanup_parameters('cleanup_withdraw', 'acme-b'))
                self.assertEqual(len(self.ok('/api/team/queue')['items']), 1)
                self.assertEqual(len(self.ok('/api/jobs')['items']), 3)
                duplicate = self.ok('/api/jobs', {'kind': 'team_install', 'key': 'Acme:key', 'confirmed': True})
                self.assertTrue(duplicate['duplicate'])
                self.assertEqual(duplicate['job']['id'], legacy['id'])
        items = self.wait_jobs()
        self.assertEqual(calls, ['assignment', 'team_install', 'cleanup_withdraw'])
        self.assertEqual([i['status'] for i in items], ['done'] * 3)
        self.assertEqual(len(self.ok('/api/team/queue/clear', {})['items']), 0)
        self.assertEqual(len(self.ok('/api/jobs')['items']), 2)

    def test_submission_only_checks_shape_without_reading_or_writing_skill_state(self):
        parameters = self.assignment()
        with self.server.write_lock:
            with patch.object(operations, 'load', side_effect=AssertionError('提交时不能核对技能库')):
                job = self.submit('assignment', **parameters)
                self.assertEqual(job['status'], 'waiting')
        self.assertEqual(self.wait_jobs()[-1]['status'], 'done')
        for path in ('/api/jobs', '/api/jobs/retry', '/api/jobs/clear'):
            self.assertEqual(self.request(path, [1, 2])[0], 400)

    def test_recheck_inside_assignment_write_cannot_bypass_new_path_warning(self):
        parameters = self.assignment(enabled=False, agent='codex')
        warning = {'file': '~/Acme workflow.md', 'entries': ['.codex/skills/acme-b']}
        recheck, calls = operations._recheck_assignment, []
        def changing(*args):
            result, plan, before = recheck(*args)
            calls.append(True)
            if len(calls) >= 2:
                result['path_warnings'] = [warning]
                plan['path_warnings'] = [warning]
            return result, plan, before
        with patch.object(operations, '_recheck_assignment', side_effect=changing):
            self.submit('assignment', **parameters)
            item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'attention')
        self.assertEqual(item['preview']['path_warnings'], [warning])
        self.assertIn('acme-b', visibility(self.home)['codex']['available'])

    def test_restore_is_reversible_and_reports_an_already_restored_skill(self):
        cleanup.apply(['acme-b'], True, self.home)
        self.submit('restore', skills=['acme-b'])
        item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'done')
        self.assertEqual(len(item['operations']), 1)
        self.assertIn('acme-b', visibility(self.home)['codex']['available'])
        self.submit('restore', skills=['acme-b'])
        self.assertEqual(self.wait_jobs()[-1]['message'], '示例乙：已经恢复或不在已撤下列表，无需重复恢复。')
        self.ok('/api/undo', {'id': item['operations'][0], 'confirmed': True,
                             'revision': operations.table(self.home)['revision']})
        self.assertTrue(operations.load(self.home)[1]['skills']['acme-b']['withdrawn'])

    def test_new_check_preserves_a_running_update_review(self):
        review = self.update_review('acme-c')
        temporary = Path(review['temporary'])
        entered, release = threading.Event(), threading.Event()
        apply = updates.apply
        def delayed(*args, **kw):
            entered.set()
            release.wait(8)
            self.assertTrue(temporary.exists())
            return apply(*args, **kw)
        with patch.object(updates, 'apply', side_effect=delayed):
            self.submit('update', review_id=review['id'], skills=['acme-c'])
            try:
                self.assertTrue(entered.wait(2))
                with patch.object(updates, 'check', side_effect=lambda review, lock: review.update(running=False)):
                    self.ok('/api/updates/check', {}, timeout=.75)
                    self.server.update_thread.join(2)
                self.assertTrue(temporary.exists())
                self.ok('/api/updates/progress', timeout=.75)
            finally:
                release.set()
            self.assertEqual(self.wait_jobs()[-1]['status'], 'done')
        self.assertFalse(temporary.exists())

    def test_sync_rollback_stops_waiting_jobs_before_releasing_write_lock(self):
        entered, release = threading.Event(), threading.Event()
        def rollback(*args):
            entered.set()
            release.wait(8)
            raise operations.RollbackError('自动恢复失败')
        with ThreadPoolExecutor() as pool, patch.object(operations, 'absorb', side_effect=rollback):
            request = pool.submit(self.request, '/api/absorb', {'confirmed': True, 'revision': 'current'})
            try:
                self.assertTrue(entered.wait(2))
                self.submit('assignment', **self.assignment())
            finally:
                release.set()
            self.assertEqual(request.result()[0], 409)
            items = self.wait_jobs()
        self.assertEqual(items[-1]['status'], 'failed')
        self.assertIn('备份还原', items[-1]['message'])
        self.assertNotIn('acme-b', visibility(self.home)['claude']['available'])

    def test_rollback_after_a_partial_update_keeps_completed_operation_ids(self):
        review = self.update_review('acme-b', 'acme-c')
        apply = updates._apply_group
        def rollback(review, names, *args):
            if names == ['acme-c']:
                raise operations.RollbackError('自动恢复失败')
            return apply(review, names, *args)
        with patch.object(updates, '_apply_group', side_effect=rollback):
            self.submit('update', review_id=review['id'], skills=['acme-b', 'acme-c'])
            item = self.wait_jobs()[-1]
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['updated'], ['acme-b'])
        self.assertEqual(len(item['operations']), 1)
        self.assertTrue(self.ok('/api/jobs')['blocked'])

    def test_error_results_hide_temporary_paths_and_session_or_team_secrets(self):
        (self.home / '.skill-pilot/team.json').write_text(json.dumps({
            'url': 'http://127.0.0.1:1', 'key': 'AcmeTeamSecret'}))
        message = '文件 "' + str(self.root / 'private download/file') + '" 发生变化，' + self.server.token + ' AcmeTeamSecret'
        with patch.object(self.server, 'execute_job', side_effect=ValueError(message)):
            self.submit('assignment', **self.assignment())
            item = self.wait_jobs()[-1]
        result = json.dumps(item)
        for private in (str(self.root), self.server.token, 'AcmeTeamSecret'):
            self.assertNotIn(private, result)
        self.assertIn('[路径已隐藏]', item['message'])

    def test_team_connection_network_call_does_not_block_a_queue_write(self):
        entered, release = threading.Event(), threading.Event()
        def connect(*args, **kw):
            entered.set()
            release.wait(8)
            return {'connected': True}
        with ThreadPoolExecutor() as pool, patch.object(team, 'status', side_effect=connect):
            connection = pool.submit(self.request, '/api/team/settings', {
                'url': 'http://127.0.0.1:1', 'key': 'AcmeTeamSecret'})
            try:
                self.assertTrue(entered.wait(2))
                self.submit('assignment', **self.assignment())
                self.assertEqual(self.wait_jobs()[-1]['status'], 'done')
            finally:
                release.set()
            self.assertEqual(connection.result()[0], 200)

    def test_update_defaults_and_explicit_default_choices_are_the_same_intent(self):
        review = self.update_review('acme-c')
        with self.blocked_first():
            first = self.submit('update', review_id=review['id'], skills=['acme-c'])
            result = self.ok('/api/jobs', {'kind': 'update', 'confirmed': True, 'review_id': review['id'],
                'skills': ['acme-c'], 'choices': {'acme-c': 'upstream'}, 'merge_ids': {'acme-c': 'unused'}})
            self.assertTrue(result['duplicate'])
            self.assertEqual(result['job']['id'], first['id'])
        self.assertEqual([i['status'] for i in self.wait_jobs()], ['done', 'done'])

    def test_tags_and_ai_settings_save_while_a_queue_job_is_running(self):
        with self.blocked_first():
            self.assertEqual(self.ok('/api/tags', {'skills': ['acme-b'], 'add': '示例标签'}, timeout=.75),
                             {'tags': {'acme-b': ['示例标签']}})
            settings = self.ok('/api/settings/llm', {'provider': 'Acme', 'base_url': 'http://127.0.0.1:1/v1',
                'model': 'acme-example', 'api_key': 'AcmeSettingsKey'}, timeout=.75)
            self.assertEqual(settings['llm']['model'], 'acme-example')
            self.assertTrue(settings['llm']['has_key'])
            self.assertEqual(self.ok('/api/jobs')['items'][0]['status'], 'running')
            self.assertEqual(json.loads((self.home / '.skill-pilot/tags.json').read_text())['skills']['acme-b'], ['示例标签'])
            self.assertEqual(self.ok('/api/settings')['llm']['model'], 'acme-example')

    def test_team_unexpected_error_keeps_install_message_and_other_errors_stay_unchanged(self):
        for error, message in [(RuntimeError('示例意外错误'), '安装没能完成，请稍后重试。'),
                               (ValueError('示例安装原因'), '示例安装原因')]:
            with self.subTest(error=type(error).__name__), patch.object(team, 'install', side_effect=error):
                self.submit('team_install', key='Acme:key')
                item = self.wait_jobs()[-1]
                self.assertEqual(item['status'], 'failed')
                self.assertEqual(item['message'], message)
        with patch.object(self.server, 'execute_job', side_effect=RuntimeError('示例意外错误')):
            self.submit('assignment', **self.assignment())
            self.assertEqual(self.wait_jobs()[-1]['message'], '任务没能完成，请重新核对后重试。')

    def test_update_and_restore_messages_keep_unlabelled_names(self):
        review = self.update_review('acme-extra')
        self.submit('update', review_id=review['id'], skills=['acme-extra'])
        self.assertEqual(self.wait_jobs()[-1]['message'], '已更新：acme-extra。')
        self.submit('restore', skills=['acme-extra'])
        self.assertEqual(self.wait_jobs()[-1]['message'], 'acme-extra：已经恢复或不在已撤下列表，无需重复恢复。')
