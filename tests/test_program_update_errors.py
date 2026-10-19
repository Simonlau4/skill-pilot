import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import self_update
from skill_pilot.sources import github
from skill_pilot.web.server import Dashboard
from tests import set_up_home


class ProgramUpdateErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot update errors ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_up_home(self, self.root / 'Another User')
        self.home.mkdir()
        self.project = self.root / 'Different Install Location'
        self.project.mkdir()

    def failure(self, stderr):
        return subprocess.CompletedProcess([], 128, '', stderr)

    def test_download_recovers_from_a_connection_reset(self):
        failure = self.failure('fatal: RPC failed; curl 56 Recv failure: Connection reset by peer')
        with patch.object(github.subprocess, 'run', side_effect=[failure,
                subprocess.CompletedProcess([], 0, 'downloaded\n', '')]) as run:
            result = self_update.git(self.project, 'fetch', '--no-tags', self_update.SOURCE, 'a' * 40)
        self.assertEqual(result, 'downloaded')
        self.assertEqual(run.call_count, 2)

    def test_persistent_timeout_stops_after_one_retry(self):
        with patch.object(github.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 60)) as run:
            with self.assertRaisesRegex(ValueError, '超时.*重试'):
                self_update.git(self.project, 'ls-remote', self_update.SOURCE, 'refs/heads/main', timeout=60)
        self.assertEqual(run.call_count, 2)

    def test_permissions_are_not_reported_as_network_failures_or_retried(self):
        with patch.object(github.subprocess, 'run', return_value=self.failure('fatal: Failed to connect: Permission denied')) as run:
            with self.assertRaisesRegex(ValueError, '权限') as caught:
                self_update.git(self.project, 'fetch', self_update.SOURCE, 'a' * 40)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('检查网络', str(caught.exception))
        self.assertNotIn('文件夹', str(caught.exception))

    def test_http2_status_is_not_treated_as_a_dropped_connection(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                with patch.object(github.subprocess, 'run', return_value=self.failure(
                        f'error: RPC failed; HTTP/2 {status}')) as run:
                    with self.assertRaises(ValueError) as caught:
                        self_update.git(self.project, 'fetch', self_update.SOURCE, 'a' * 40)
                self.assertEqual(run.call_count, 1)
                self.assertNotIn('连接中断', str(caught.exception))
                self.assertNotIn('重试', str(caught.exception))

    def test_curl_operation_timeout_retries_once_and_says_timeout(self):
        failure = self.failure('fatal: RPC failed; curl 56 Recv failure: Operation timed out')
        with patch.object(github.subprocess, 'run', return_value=failure) as run:
            with self.assertRaisesRegex(ValueError, '超时') as caught:
                self_update.git(self.project, 'fetch', self_update.SOURCE, 'a' * 40)
        self.assertEqual(run.call_count, 2)
        self.assertIn('重试', str(caught.exception))
        self.assertNotIn('连接中断', str(caught.exception))

    def test_http2_stream_reset_still_retries_as_a_dropped_connection(self):
        for detail in ('curl: (92) HTTP/2 stream 1 was not closed cleanly: PROTOCOL_ERROR',
                       'curl: (92) HTTP/2 stream 1 was reset',
                       'curl: (16) Error in the HTTP2 framing layer',
                       'curl: (18) transfer closed with outstanding read data remaining',
                       'error: GnuTLS recv error (-110)'):
            with self.subTest(detail=detail):
                failure = self.failure(detail)
                with patch.object(github.subprocess, 'run', return_value=failure) as run:
                    with self.assertRaisesRegex(ValueError, '连接中断') as caught:
                        self_update.git(self.project, 'fetch', self_update.SOURCE, 'a' * 40)
                self.assertEqual(run.call_count, 2)
                self.assertIn('重试', str(caught.exception))
                self.assertNotIn('当前操作失败', str(caught.exception))

    def test_certificate_errors_are_explicit_and_not_retried(self):
        with patch.object(github.subprocess, 'run', return_value=self.failure('fatal: SSL_connect: certificate verify failed')) as run:
            with self.assertRaisesRegex(ValueError, '安全连接验证失败'):
                self_update.git(self.project, 'ls-remote', self_update.SOURCE)
        self.assertEqual(run.call_count, 1)

    def test_local_installation_commands_are_never_retried(self):
        with patch.object(github.subprocess, 'run', return_value=self.failure('fatal: Connection reset by peer')) as run:
            with self.assertRaises(ValueError):
                self_update.git(self.project, 'merge', '--ff-only', 'a' * 40)
        self.assertEqual(run.call_count, 1)

    def test_failed_command_retains_its_original_details(self):
        stderr = 'fatal: unable to access remote: SSL certificate problem'
        with patch.object(github.subprocess, 'run', return_value=self.failure(stderr)):
            with self.assertRaises(ValueError) as caught:
                github.git(self.project, 'ls-remote', self_update.SOURCE)
        self.assertIsInstance(caught.exception.__cause__, subprocess.CalledProcessError)
        self.assertEqual(caught.exception.__cause__.stderr, stderr)

    def test_dashboard_keeps_safe_details_in_a_separate_private_log(self):
        server = Dashboard({'home': str(self.home)})
        self.addCleanup(server.server_close)
        stderr = (f"fatal: unable to access 'https://user:example-password@github.com/repo?token=example': "
                  f"Connection reset by peer in {self.home}/private\nAuthorization: Bearer example-secret")
        with patch.object(github.subprocess, 'run', return_value=self.failure(stderr)), \
                patch.object(self_update, 'check', side_effect=lambda home:
                    self_update.git(self.project, 'ls-remote', self_update.SOURCE)):
            server.program_work('check')
        self.assertEqual(server.program_progress['phase'], 'error')
        self.assertIn('连接中断', server.program_progress['error'])
        path = self.home / '.skill-pilot/logs/program-update.jsonl'
        record, = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(record['action'], 'check')
        self.assertEqual(record['step'], 'ls-remote')
        self.assertEqual(record['returncode'], 128)
        self.assertIn('Connection reset by peer', record['detail'])
        self.assertNotIn('example-password', path.read_text())
        self.assertNotIn('example-secret', path.read_text())
        self.assertNotIn('token=example', path.read_text())
        self.assertNotIn(str(self.home), path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob('*.json')), [])

    def test_automatic_failures_are_logged_without_a_red_error(self):
        server = Dashboard({'home': str(self.home)})
        self.addCleanup(server.server_close)
        with patch.object(self_update, 'check', side_effect=ValueError('测试下载失败')):
            server.program_work('check', automatic=True)
        self.assertEqual(server.program_progress['error'], '')
        self.assertTrue((self.home / '.skill-pilot/logs/program-update.jsonl').is_file())

    def test_readonly_checks_do_not_write_logs(self):
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self_update.record_failure(self.home, 'check', ValueError('测试失败'))
        self.assertFalse((self.home / '.skill-pilot/logs').exists())

    def test_unwritable_logs_do_not_replace_the_update_failure(self):
        (self.home / '.skill-pilot').mkdir()
        (self.home / '.skill-pilot/logs').write_text('a file occupies the log folder')
        server = Dashboard({'home': str(self.home)})
        self.addCleanup(server.server_close)
        with patch.object(self_update, 'check', side_effect=ValueError('实际失败原因')):
            server.program_work('check')
        self.assertEqual(server.program_progress['error'], '实际失败原因')
