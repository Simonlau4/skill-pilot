"""Startup checks use real dashboard HTTP with an isolated home and no network."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from skill_pilot import self_update
from skill_pilot.web import runtime, server
from tests import set_up_home


class AutoUpdateCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot automatic check ')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, Path(temporary.name) / 'Another User')
        self.home.mkdir()
        self.inventory = {'home': str(self.home)}
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.home),
            'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'}))
        self.review = {'temporary': '', 'target': 'latest-version', 'current': 'old',
                       'latest': 'new', 'available': True}
        self.check = self.enterContext(patch.object(self_update, 'check', return_value=self.review))
        self.enterContext(patch.object(server.operations, 'bootstrap'))
        self.enterContext(patch.object(server, 'scan', return_value=self.inventory))
        self.enterContext(patch.object(server.webbrowser, 'open'))
        self.enterContext(patch.object(server, 'print', create=True))
        self.enterContext(patch.object(server.Dashboard, 'watch'))
        self.dashboard = None
        self.thread = None
        self.release = threading.Event()
        self.addCleanup(self.close)

    def start(self, resume=None):
        started = threading.Event()
        serve = server.Dashboard.serve_forever
        def serving(dashboard, **kwargs):
            self.dashboard = dashboard
            started.set()
            serve(dashboard, **kwargs)
        self.enterContext(patch.object(server.Dashboard, 'serve_forever', serving))
        self.enterContext(patch.object(runtime, 'resume_state', return_value=resume))
        self.thread = threading.Thread(target=server.run, kwargs={'json_output': True})
        self.thread.start()
        self.assertTrue(started.wait(3), '测试看板没有启动')

    def close(self):
        self.release.set()
        if self.dashboard:
            self.dashboard.shutdown()
        if self.thread:
            self.thread.join(3)
            self.assertFalse(self.thread.is_alive(), '测试看板没有退出')

    def request(self, path, body=None):
        request = urllib.request.Request(self.dashboard.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-Skill-Pilot-Token': self.dashboard.token,
                     'Origin': self.dashboard.origin, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response) if response.headers.get_content_type() == 'application/json' else response.read()

    def finished(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            progress = self.request('/api/program/progress')
            if not progress['running']:
                return progress
            time.sleep(.01)
        self.fail('自动检查没有结束')

    def test_startup_checks_once_and_pages_and_refresh_do_not_check_again(self):
        self.start()
        progress = self.finished()
        self.assertTrue(progress['available'], progress)
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'available')
        self.assertEqual(progress['target'], self.review['target'])
        self.assertEqual(self.dashboard.program_review, self.review)
        self.assertNotIn('temporary', progress)
        self.request('/')
        self.request('/')
        for page in ('first', 'second'):
            self.request('/api/heartbeat?page=' + page, {})
            self.request('/api/program/progress')
        server.run(json_output=True)
        self.assertFalse(self.request('/api/runtime/refresh', {})['changed'])
        self.check.assert_called_once_with(self.home)

    def test_startup_current_version_is_reported(self):
        self.check.return_value = {**self.review, 'available': False}
        self.start()
        progress = self.finished()
        self.assertFalse(progress['available'])
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'latest')
        self.assertEqual(progress['error'], '')

    def test_readonly_startup_does_not_check(self):
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.start()
            progress = self.finished()
        self.check.assert_not_called()
        self.assertFalse(progress['write_allowed'])
        self.assertFalse(progress['automatic'])
        self.assertEqual(progress['phase'], 'idle')

    def test_resumed_program_preserves_result_without_checking(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        self.addCleanup(listener.close)
        saved = {'running': False, 'phase': 'latest', 'automatic': True,
                 'message': 'SP 已是最新版本', 'available': False, 'error': ''}
        resume = {'inventory': self.inventory, 'socket_fd': os.dup(listener.fileno()),
                  'lock_fd': os.open(server.lock_path(), os.O_RDWR | os.O_CREAT, 0o600),
                  'program_progress': saved.copy()}
        self.start(resume)
        progress = self.finished()
        self.assertEqual(progress, {**saved, 'write_allowed': True})
        self.check.assert_not_called()

    def assert_neutral_failure(self, error):
        self.check.side_effect = error
        self.start()
        progress = self.finished()
        self.assertFalse(progress['running'])
        self.assertFalse(progress['available'])
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'idle')
        self.assertEqual(progress['error'], '')
        self.assertEqual(progress['message'], '还没查到新版信息')
        self.assertIsNone(self.dashboard.program_review)

    def test_automatic_network_failure_is_neutral(self):
        self.assert_neutral_failure(OSError('network unavailable'))

    def test_automatic_local_changes_are_neutral(self):
        self.assert_neutral_failure(ValueError('SP 程序文件有本地改动，已保留当前版本。'))

    def test_automatic_divergent_version_is_neutral(self):
        self.assert_neutral_failure(ValueError('当前 SP 含有与公开版本不同的改动，已保留当前版本。'))

    def test_manual_check_during_automatic_check_reuses_running_task(self):
        entered = threading.Event()
        def blocked(home):
            entered.set()
            self.release.wait(3)
            return self.review
        self.check.side_effect = blocked
        self.start()
        self.assertTrue(entered.wait(2))
        thread = self.dashboard.program_thread
        progress = self.request('/api/program/check', {})
        self.assertTrue(progress['running'])
        self.assertTrue(progress['automatic'])
        self.assertIs(thread, self.dashboard.program_thread)
        self.check.assert_called_once_with(self.home)
        self.release.set()
        self.assertTrue(self.finished()['available'])

    def test_manual_check_after_automatic_result_still_shows_failure_and_can_retry(self):
        self.check.return_value = {**self.review, 'available': False}
        self.start()
        self.assertTrue(self.finished()['automatic'])
        reason = '没能读取 SP 的最新版本，请检查网络后重试。'
        self.check.side_effect = ValueError(reason)
        self.request('/api/program/check', {})
        failed = self.finished()
        self.assertFalse(failed['automatic'])
        self.assertEqual(failed['phase'], 'error')
        self.assertEqual(failed['error'], reason)
        self.assertFalse(failed['available'])
        self.check.side_effect = None
        self.check.return_value = self.review
        self.request('/api/program/check', {})
        retried = self.finished()
        self.assertTrue(retried['available'])
        self.assertFalse(retried['automatic'])
        self.assertEqual(retried['error'], '')
        self.assertEqual(self.check.call_count, 3)

    def test_constructing_dashboard_does_not_start_network_work(self):
        dashboard = server.Dashboard(self.inventory)
        try:
            self.assertIsNone(dashboard.program_thread)
            self.check.assert_not_called()
        finally:
            dashboard.server_close()


class CloseDuringAutomaticCheckTests(unittest.TestCase):
    """关看板和刷新应用新版不应等启动时那次自动检查。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot close check ')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, Path(temporary.name) / 'Another User')
        self.home.mkdir()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.boards = []
        self.addCleanup(self.stop)
        self.review = {'temporary': '', 'target': 'latest-version', 'current': 'old',
                       'latest': 'new', 'available': False}

        def blocked(home):
            self.entered.set()
            self.release.wait()
            return self.review

        self.check = self.enterContext(patch.object(self_update, 'check', side_effect=blocked))

    def stop(self):
        self.release.set()
        for dashboard, serving in self.boards:
            if serving:
                dashboard.shutdown()
                serving.join(3)
            closer = getattr(dashboard, '_closer', None)
            if closer:
                closer.join(3)
            if not dashboard.closing:
                dashboard.server_close()

    def board(self, serve=False):
        dashboard = server.Dashboard({'home': str(self.home)})
        serving = None
        if serve:
            serving = threading.Thread(target=dashboard.serve_forever, kwargs={'poll_interval': 0.01})
            serving.start()
        self.boards.append((dashboard, serving))
        return dashboard

    def start_check(self, dashboard, automatic):
        dashboard.start_program('check', automatic=automatic)
        self.assertTrue(self.entered.wait(2), '检查没有开始')

    def test_close_does_not_wait_for_automatic_check(self):
        dashboard = self.board()
        self.start_check(dashboard, automatic=True)
        closer = threading.Thread(target=dashboard.server_close)
        dashboard._closer = closer
        closer.start()
        closer.join(1)
        self.assertFalse(closer.is_alive(), '关看板仍在等自动检查')
        self.assertTrue(dashboard.program_thread.is_alive(), '自动检查不应被关掉这件事打断')

    def test_watch_shuts_down_while_automatic_check_is_stuck(self):
        dashboard = self.board(serve=True)
        serving = self.boards[-1][1]
        self.start_check(dashboard, automatic=True)
        dashboard.last_heartbeat = time.monotonic() - server.HEARTBEAT_TIMEOUT - 1
        watcher = threading.Thread(target=dashboard.watch, daemon=True)
        watcher.start()
        serving.join(1)
        self.assertFalse(serving.is_alive(), '关掉页面后看板仍在等自动检查')
        watcher.join(1)

    def test_reload_does_not_wait_for_automatic_check(self):
        dashboard = self.board()
        applied = threading.Event()
        dashboard.reload_callback = applied.set
        self.start_check(dashboard, automatic=True)
        with patch.object(runtime, 'version', return_value='new-program'):
            dashboard.request_reload()
            self.assertTrue(applied.wait(1), '刷新应用新版仍在等自动检查')

    def test_close_still_waits_for_manual_check(self):
        dashboard = self.board()
        self.start_check(dashboard, automatic=False)
        closer = threading.Thread(target=dashboard.server_close)
        dashboard._closer = closer
        closer.start()
        self.assertTrue(closer.is_alive(), '手动检查进行中时关看板不应马上返回')
        time.sleep(0.3)
        self.assertTrue(closer.is_alive(), '手动检查进行中时关看板不应马上返回')
        self.release.set()
        closer.join(2)
        self.assertFalse(closer.is_alive(), '手动检查结束后关看板应返回')

    def test_reload_still_waits_for_manual_check(self):
        dashboard = self.board()
        applied = threading.Event()
        dashboard.reload_callback = applied.set
        self.start_check(dashboard, automatic=False)
        with patch.object(runtime, 'version', return_value='new-program'):
            dashboard.request_reload()
            self.assertFalse(applied.wait(0.3), '手动检查进行时不应立刻刷新应用新版')
            self.release.set()
            self.assertTrue(applied.wait(2), '手动检查结束后应能刷新应用新版')

    def test_resumed_interrupted_automatic_check_is_idle(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        self.addCleanup(listener.close)
        saved = {'running': True, 'phase': 'check', 'automatic': True,
                 'message': '正在下载并检查新版…', 'available': False, 'error': '',
                 'current': '', 'latest': ''}
        dashboard = server.Dashboard({'home': str(self.home)}, resume={
            'inventory': {'home': str(self.home)},
            'socket_fd': os.dup(listener.fileno()),
            'program_progress': saved.copy(),
        })
        self.addCleanup(dashboard.server_close)
        self.assertFalse(dashboard.program_progress['running'])
        self.assertEqual(dashboard.program_progress['phase'], 'idle')
        self.assertEqual(dashboard.program_progress['error'], '')
        self.assertEqual(dashboard.program_progress['message'], '还没查到新版信息')
        self.check.assert_not_called()
