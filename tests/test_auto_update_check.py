"""Automatic checks use real dashboard HTTP with an isolated home and no network."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from skill_pilot import self_update
from skill_pilot.web import runtime, server, windows_runtime
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
        self.enterContext(patch.object(windows_runtime, 'print', create=True))
        self.enterContext(patch.object(server.Dashboard, 'watch'))
        self.dashboard = None
        self.thread = None
        self.release = threading.Event()
        self.addCleanup(self.close)

    def start(self, resume=None, windows=False):
        started = threading.Event()
        serve = server.Dashboard.serve_forever
        def serving(dashboard, **kwargs):
            self.dashboard = dashboard
            started.set()
            serve(dashboard, **kwargs)
        self.enterContext(patch.object(server.Dashboard, 'serve_forever', serving))
        self.enterContext(patch.object(runtime, 'resume_state', return_value=resume))
        self.thread = threading.Thread(target=windows_runtime.run if windows else server.run,
                                       kwargs={'json_output': True})
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

    def test_opening_pages_heartbeats_and_refresh_do_not_check_by_themselves(self):
        """只打开页面、心跳和刷新不会自己查。"""
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

    def test_auto_check_waits_for_interval_then_checks_again(self):
        self.check.return_value = {**self.review, 'available': False}
        self.start()
        latest = self.finished()
        checked_at = latest['checked_at']
        with patch.object(server.time, 'time', return_value=checked_at + server.PROGRAM_AUTO_CHECK_INTERVAL - 1):
            self.assertEqual(self.request('/api/program/auto-check', {}), latest)
        self.check.assert_called_once_with(self.home)
        self.check.return_value = self.review
        now = checked_at + server.PROGRAM_AUTO_CHECK_INTERVAL
        with patch.object(server.time, 'time', return_value=now):
            self.request('/api/program/auto-check', {})
            progress = self.finished()
        self.assertEqual(self.check.call_count, 2)
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'available')
        self.assertEqual(progress['checked_at'], now)

    def test_manual_success_and_failure_delay_automatic_check(self):
        self.check.return_value = {**self.review, 'available': False}
        self.start()
        checked_at = self.finished()['checked_at']
        for offset, error, phase in ((1, None, 'latest'), (2, ValueError('检查失败'), 'error')):
            with self.subTest(phase=phase), patch.object(server.time, 'time', return_value=checked_at + offset):
                self.check.side_effect = error
                self.request('/api/program/check', {})
                progress = self.finished()
                self.assertFalse(progress['automatic'])
                self.assertEqual(progress['phase'], phase)
                self.assertEqual(progress['checked_at'], checked_at + offset)
                self.assertEqual(self.request('/api/program/auto-check', {}), progress)
                self.assertEqual(self.check.call_count, offset + 1)

    def test_auto_check_is_rejected_during_reload_without_checking(self):
        self.check.return_value = {**self.review, 'available': False}
        self.start()
        self.finished()
        self.dashboard.program_progress['checked_at'] = time.time() - server.PROGRAM_AUTO_CHECK_INTERVAL - 1
        self.check.reset_mock()
        self.dashboard.reload_pending = True
        try:
            with patch.object(self.dashboard, 'auto_check_program', wraps=self.dashboard.auto_check_program) as auto_check:
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    self.request('/api/program/auto-check', {})
                with rejected.exception as response:
                    self.assertEqual(response.code, 503)
                auto_check.assert_not_called()
                self.check.assert_not_called()
        finally:
            self.dashboard.reload_pending = False

    def test_available_and_ready_results_are_preserved_even_after_interval(self):
        self.start()
        self.finished()
        for phase in ('available', 'ready'):
            with self.subTest(phase=phase):
                self.dashboard.program_progress.update(phase=phase,
                    checked_at=time.time() - server.PROGRAM_AUTO_CHECK_INTERVAL - 1)
                saved = self.dashboard.program_progress.copy()
                self.assertEqual(self.request('/api/program/auto-check', {}), {**saved, 'write_allowed': True})
                returned = self.dashboard.auto_check_program()
                self.assertEqual(returned, saved)
                returned['message'] = '只改返回值'
                self.assertEqual(self.dashboard.program_progress, saved)
        self.check.assert_called_once_with(self.home)

    def test_windows_cold_start_checks_once(self):
        self.check.return_value = {**self.review, 'available': False}
        self.start(windows=True)
        progress = self.finished()
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'latest')
        self.assertEqual(self.request('/api/program/auto-check', {}), progress)
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
            self.assertEqual(self.request('/api/program/auto-check', {}), progress)
        self.check.assert_not_called()
        self.assertFalse(progress['write_allowed'])
        self.assertFalse(progress['automatic'])
        self.assertEqual(progress['phase'], 'idle')

    def start_resumed(self, checked_at):
        saved = {'running': False, 'phase': 'latest', 'automatic': True,
                 'message': 'SP 已是最新版本', 'available': False, 'error': '', 'checked_at': checked_at}
        dashboard = server.Dashboard(self.inventory)
        try:
            dashboard.program_progress = saved.copy()
            resume = json.loads(json.dumps(runtime.snapshot(dashboard)))
            resume.update(socket_fd=dashboard.socket.dup().detach(),
                          lock_fd=os.open(server.lock_path(), os.O_RDWR | os.O_CREAT, 0o600))
        finally:
            dashboard.server_close()
        self.start(resume)
        progress = self.finished()
        self.assertEqual(progress, {**saved, 'write_allowed': True})
        self.check.assert_not_called()
        return progress

    @unittest.skipIf(os.name == "nt", "POSIX descriptor resume; Windows session retention has its own test")
    def test_resumed_program_preserves_result_and_recent_check_time(self):
        progress = self.start_resumed(time.time())
        self.assertEqual(self.request('/api/program/auto-check', {}), progress)
        self.check.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX descriptor resume; Windows session retention has its own test")
    def test_resumed_expired_check_time_allows_automatic_check(self):
        self.start_resumed(time.time() - server.PROGRAM_AUTO_CHECK_INTERVAL - 1)
        self.request('/api/program/auto-check', {})
        progress = self.finished()
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'available')
        self.check.assert_called_once_with(self.home)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor resume; Windows session retention has its own test")
    def test_resumed_future_check_time_is_treated_as_expired(self):
        self.start_resumed(time.time() + server.PROGRAM_AUTO_CHECK_INTERVAL)
        self.request('/api/program/auto-check', {})
        progress = self.finished()
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'available')
        self.check.assert_called_once_with(self.home)

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
        self.assertIsInstance(progress['checked_at'], float)
        return progress

    def test_automatic_network_failure_is_neutral(self):
        self.assert_neutral_failure(OSError('network unavailable'))

    def test_automatic_local_changes_are_neutral(self):
        self.assert_neutral_failure(ValueError('SP 程序文件有本地改动，已保留当前版本。'))

    def test_automatic_divergent_version_is_neutral(self):
        self.assert_neutral_failure(ValueError('当前 SP 含有与公开版本不同的改动，已保留当前版本。'))

    def test_automatic_failure_does_not_retry_or_log_again_during_interval(self):
        progress = self.assert_neutral_failure(OSError('network unavailable'))
        log = self.home / '.skill-pilot/logs/program-update.jsonl'
        recorded = log.read_bytes()
        self.assertEqual(len(recorded.splitlines()), 1)
        self.assertEqual(self.request('/api/program/auto-check', {}), progress)
        self.check.assert_called_once_with(self.home)
        self.assertEqual(log.read_bytes(), recorded)

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

    def test_auto_check_reuses_running_automatic_check(self):
        entered = threading.Event()
        def blocked(home):
            entered.set()
            self.release.wait(3)
            return self.review
        self.check.side_effect = blocked
        self.start()
        self.assertTrue(entered.wait(2))
        thread = self.dashboard.program_thread
        progress = self.request('/api/program/auto-check', {})
        self.assertTrue(progress['running'])
        self.assertTrue(progress['automatic'])
        self.assertEqual(progress['phase'], 'check')
        self.assertIs(thread, self.dashboard.program_thread)
        self.check.assert_called_once_with(self.home)
        self.release.set()
        self.finished()

    def test_auto_check_reuses_running_manual_check(self):
        self.check.return_value = {**self.review, 'available': False}
        self.start()
        self.finished()
        entered = threading.Event()
        def blocked(home):
            entered.set()
            self.release.wait(3)
            return self.review
        self.check.side_effect = blocked
        self.request('/api/program/check', {})
        self.assertTrue(entered.wait(2))
        thread = self.dashboard.program_thread
        progress = self.request('/api/program/auto-check', {})
        self.assertTrue(progress['running'])
        self.assertFalse(progress['automatic'])
        self.assertEqual(progress['phase'], 'check')
        self.assertIs(thread, self.dashboard.program_thread)
        self.assertEqual(self.check.call_count, 2)
        self.release.set()
        self.finished()

    def test_auto_check_during_upgrade_returns_progress_and_preserves_check_time(self):
        self.start()
        checked_at = self.finished()['checked_at']
        entered = threading.Event()
        def blocked(review, home):
            entered.set()
            self.release.wait(3)
            return {'available': False, 'current': 'new'}
        with patch.object(self_update, 'apply', side_effect=blocked) as apply:
            self.request('/api/program/apply', {'confirmed': True, 'target': self.review['target']})
            self.assertTrue(entered.wait(2))
            thread = self.dashboard.program_thread
            progress = self.request('/api/program/auto-check', {})
            self.assertTrue(progress['running'])
            self.assertFalse(progress['automatic'])
            self.assertEqual(progress['phase'], 'apply')
            self.assertTrue(progress['write_allowed'])
            self.assertIs(thread, self.dashboard.program_thread)
            self.check.assert_called_once_with(self.home)
            self.release.set()
            finished = self.finished()
            self.assertEqual(finished['phase'], 'ready')
            self.assertEqual(finished['checked_at'], checked_at)
            apply.assert_called_once_with(self.review, self.home)

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
            'socket_fd': listener.dup().detach(),
            'program_progress': saved.copy(),
        })
        self.addCleanup(dashboard.server_close)
        self.assertFalse(dashboard.program_progress['running'])
        self.assertEqual(dashboard.program_progress['phase'], 'idle')
        self.assertEqual(dashboard.program_progress['error'], '')
        self.assertEqual(dashboard.program_progress['message'], '还没查到新版信息')
        self.check.assert_not_called()
