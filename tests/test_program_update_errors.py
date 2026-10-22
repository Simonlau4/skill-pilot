import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
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
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob('*.json')), [])

    def test_automatic_failures_are_logged_without_a_red_error(self):
        server = Dashboard({'home': str(self.home)})
        self.addCleanup(server.server_close)
        with patch.object(self_update, 'check', side_effect=ValueError('测试下载失败')):
            server.program_work('check', automatic=True)
        self.assertEqual(server.program_progress['error'], '')
        self.assertTrue((self.home / '.skill-pilot/logs/program-update.jsonl').is_file())

    def test_failure_log_hides_paths_outside_home_only(self):
        inside = self.home / 'Library' / 'Logs' / 'update.log'
        outside = self.root / 'var' / 'opt' / 'skill-pilot' / 'self_update.py'
        spaced = self.project / 'config.toml'
        cases = {
            'inside': (
                f'Permission denied: {inside}',
                f'Permission denied: ~{os.sep}{inside.relative_to(self.home)}'),
            'outside': (
                f"fatal: cannot open '{spaced}': No such file or directory ({outside})",
                "fatal: cannot open '[路径已隐藏]': No such file or directory ([路径已隐藏])"),
            'plain': (
                'Connection reset by peer; error: RPC failed; HTTP/2 403',
                'Connection reset by peer; error: RPC failed; HTTP/2 403'),
        }
        for name, (original, expected) in cases.items():
            with self.subTest(name=name):
                error = ValueError(original)
                self_update.record_failure(self.home, 'check', error)
                detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                     .read_text().splitlines()[-1])['detail']
                self.assertEqual(detail, expected)
                self.assertEqual(str(error), original)
                self.assertNotIn(str(self.home), detail)
                self.assertNotIn(str(outside), detail)
                self.assertNotIn(str(spaced), detail)
                if name == 'outside':
                    self.assertNotIn('self_update.py', detail)
                    self.assertNotIn('config.toml', detail)

    def test_home_prefix_does_not_hide_a_longer_directory(self):
        home = str(self.home)
        foreign = f'{home}X/secret/notes.txt'
        cases = {
            'quoted': (f"Permission denied: '{foreign}'", "Permission denied: '[路径已隐藏]'"),
            'plain': (f'Permission denied: {foreign}', None),
        }
        for name, (original, expected) in cases.items():
            with self.subTest(name=name):
                error = ValueError(original)
                self_update.record_failure(self.home, 'check', error)
                detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                     .read_text().splitlines()[-1])['detail']
                if expected is not None:
                    self.assertEqual(detail, expected)
                self.assertNotIn('~X', detail)
                self.assertNotIn(home, detail)
                self.assertNotIn(foreign, detail)
                self.assertNotIn('notes.txt', detail)
                self.assertIn('Permission denied', detail)
                self.assertEqual(str(error), original)

    def test_profile_ancestor_does_not_reveal_paths_outside_the_managed_home(self):
        # Windows keeps temporary folders inside the user profile. Abbreviating
        # that shorter profile must not leave the rest of an outside path visible.
        inside = self.home / 'Library' / 'Logs' / 'update.log'
        sibling = self.home.parent / 'Other User' / 'secret' / 'notes.txt'
        foreign = f'{self.home}X/secret/notes.txt'
        cases = {
            'inside': (
                f'Permission denied: {inside}',
                f'Permission denied: ~{os.sep}{inside.relative_to(self.home)}'),
            'sibling': (f'cannot open {sibling}', 'cannot open [路径已隐藏]'),
            'quoted': (f"Permission denied: '{foreign}'", "Permission denied: '[路径已隐藏]'"),
            'plain': (f'Permission denied: {foreign}', 'Permission denied: [路径已隐藏]'),
            'same-prefix': (
                f'cannot read {self.home}X/secret/one.txt and {self.home}2/secret/two.txt',
                'cannot read [路径已隐藏] and [路径已隐藏]'),
            'backslash': (
                str(self.home.parent) + '\\secret\\notes.txt',
                '[路径已隐藏]'),
        }
        with patch.object(self_update.Path, 'home', return_value=self.home.parent):
            for name, (original, expected) in cases.items():
                with self.subTest(name=name):
                    error = ValueError(original)
                    self_update.record_failure(self.home, 'check', error)
                    detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                         .read_text().splitlines()[-1])['detail']
                    self.assertEqual(detail, expected)
                    self.assertNotIn('notes.txt', detail)
                    self.assertNotIn('one.txt', detail)
                    self.assertNotIn('two.txt', detail)
                    self.assertEqual(str(error), original)

    def test_windows_drive_outside_managed_home_is_hidden_when_profile_is_shorter(self):
        managed = PureWindowsPath(r'C:\Users\Test Person\AppData\Local\Temp\skill pilot batch abc\Another User')
        profile = PureWindowsPath(r'C:\Users\Test Person')
        foreign = str(managed) + 'X/secret/notes.txt'
        inside = str(managed / 'Library' / 'Logs' / 'update.log')
        sibling = str(profile / 'Documents' / 'secret.txt')
        cases = {
            'quoted': (f"Permission denied: '{foreign}'", "Permission denied: '[路径已隐藏]'"),
            'plain': (f'Permission denied: {foreign}', 'Permission denied: [路径已隐藏]'),
            'same-prefix': (
                f'cannot read {managed}X/secret/one.txt and {managed}2/secret/two.txt',
                'cannot read [路径已隐藏] and [路径已隐藏]'),
            'inside': (f'Permission denied: {inside}', r'Permission denied: ~\Library\Logs\update.log'),
            'sibling': (f'cannot open {sibling}', 'cannot open [路径已隐藏]'),
            'lower-drive': (
                'c:/users/test person/documents/secret.txt',
                '[路径已隐藏]'),
        }
        with patch.object(self_update.Path, 'home', return_value=profile):
            for name, (original, expected) in cases.items():
                with self.subTest(name=name):
                    detail = self_update._hide_paths_outside_home(
                        self_update._abbreviate_home(original, managed))
                    self.assertEqual(detail, expected)
                    self.assertNotIn('notes.txt', detail)
                    self.assertNotIn('secret.txt', detail)
                    self.assertNotIn(r'Test PersonX', detail)
                    self.assertNotIn('Another UserX', detail)

    def test_following_words_are_not_swallowed_into_the_path(self):
        cases = {
            'spaced': (
                'fatal: could not read /opt/My Apps/x/file.py: Permission denied',
                'fatal: could not read [路径已隐藏]: Permission denied'),
            'spaced-name': (
                '/opt/Skill Pilot copy 2/repo/.git/index.lock exists; remove it',
                '[路径已隐藏] exists; remove it'),
            'spaced-chinese': (
                '无法读取 /opt/技能 目录/文件.py：权限不足',
                '无法读取 [路径已隐藏]：权限不足'),
            'paren': ('(/opt/a b/c)', '([路径已隐藏])'),
            'sentence': (
                '/opt/x/y is not a git repo and src/main.py was skipped',
                '[路径已隐藏] is not a git repo and src/main.py was skipped'),
            'relative': (
                'error: ./scripts/build.sh and ../other/repo and skill_pilot/self_update.py failed',
                'error: ./scripts/build.sh and ../other/repo and skill_pilot/self_update.py failed'),
        }
        for name, (original, expected) in cases.items():
            with self.subTest(name=name):
                error = ValueError(original)
                self_update.record_failure(self.home, 'check', error)
                detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                     .read_text().splitlines()[-1])['detail']
                self.assertEqual(detail, expected)
                self.assertEqual(str(error), original)

    def test_separate_paths_preserve_the_explanation_between_them(self):
        cases = {
            'deep-relative': (
                'warning: /opt/x/y is not a git repo and src/a/main.py was skipped',
                'warning: [路径已隐藏] is not a git repo and src/a/main.py was skipped'),
            'short-relative-clause': (
                '/opt/x/y and src/a/main.py were skipped',
                '[路径已隐藏] and src/a/main.py were skipped'),
            'two-paths': (
                'error: cannot open /opt/a/b.txt and also /opt/c/d.txt failed',
                'error: cannot open [路径已隐藏] and also [路径已隐藏] failed'),
            'because': (
                'cannot open /opt/a/b.txt because the file at /opt/c/d.txt is busy',
                'cannot open [路径已隐藏] because the file at [路径已隐藏] is busy'),
            'rename': (
                'error: unable to rename /opt/a/b.lock to /opt/a/b: Permission denied',
                'error: unable to rename [路径已隐藏] to [路径已隐藏]: Permission denied'),
            'adjacent': ('/opt/a/b /opt/c/d failed', '[路径已隐藏] [路径已隐藏] failed'),
            'same-home-prefix': (
                f'cannot read {self.home}X/secret/one.txt and {self.home}2/secret/two.txt',
                'cannot read [路径已隐藏] and [路径已隐藏]'),
            'spaced-folder': (
                '/opt/Skill Pilot copy/file.txt is busy',
                '[路径已隐藏] is busy'),
        }
        for name, (original, expected) in cases.items():
            with self.subTest(name=name):
                error = ValueError(original)
                self_update.record_failure(self.home, 'check', error)
                detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                     .read_text().splitlines()[-1])['detail']
                self.assertEqual(detail, expected)
                self.assertEqual(str(error), original)

    def test_windows_paths_are_hidden_without_changing_relative_paths_or_reasons(self):
        cases = [
            (r'cannot rename C:\Private Data\repo\index.lock to D:\Other Data\repo\index: Permission denied',
             'cannot rename [路径已隐藏] to [路径已隐藏]: Permission denied'),
            (r'cannot open "C:\Private Folder\file.txt" because \\server\Share\other.txt is busy',
             'cannot open "[路径已隐藏]" because [路径已隐藏] is busy'),
            ('C:/Private Folder/repo/file.py and src/a/main.py failed',
             '[路径已隐藏] and src/a/main.py failed'),
            (r'run .\scripts\build.cmd from ..\other\repo; HTTP/2 403',
             r'run .\scripts\build.cmd from ..\other\repo; HTTP/2 403'),
        ]
        for original, expected in cases:
            with self.subTest(original=original):
                error = ValueError(original)
                self_update.record_failure(self.home, 'check', error)
                detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                     .read_text().splitlines()[-1])['detail']
                self.assertEqual(detail, expected)
                self.assertEqual(str(error), original)

    def test_windows_home_paths_keep_their_abbreviation_and_directory_boundary(self):
        cases = [
            (r'c:\users\test person\Logs\update.log', r'~\Logs\update.log'),
            ('C:/Users/Test Person/Logs/update.log', '~/Logs/update.log'),
            (r'C:\Users\Test Person: Permission denied', '~: Permission denied'),
            (r'C:\Users\Test PersonX\private\notes.txt', '[路径已隐藏]'),
        ]
        with patch.object(self_update.Path, 'home', return_value=PureWindowsPath('C:/Users/Test Person')):
            for original, expected in cases:
                with self.subTest(original=original):
                    self_update.record_failure(self.home, 'check', ValueError(original))
                    detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                         .read_text().splitlines()[-1])['detail']
                    self.assertEqual(detail, expected)

    def test_relative_paths_and_shell_links_stay_readable(self):
        original = ('run ./scripts/build.sh from ../other/repo '
                    'ssh://git@github.com/example/skill-pilot.git '
                    'git://github.com/example/skill-pilot.git')
        expected = ('run ./scripts/build.sh from ../other/repo '
                    '[链接已隐藏] '
                    '[链接已隐藏]')
        error = ValueError(original)
        self_update.record_failure(self.home, 'check', error)
        detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                             .read_text().splitlines()[-1])['detail']
        self.assertEqual(detail, expected)
        self.assertEqual(str(error), original)

    def test_home_followed_by_punctuation_stays_shortened(self):
        home = str(self.home)
        cases = {
            'colon': (f'fatal: {home}: Permission denied', 'fatal: ~: Permission denied'),
            'paren': (f'({home})', '(~)'),
            'comma': (f'{home}, next', '~, next'),
            'period': (f'{home}.', '~.'),
            'chinese': (f'{home}；权限不足', '~；权限不足'),
        }
        for name, (original, expected) in cases.items():
            with self.subTest(name=name):
                error = ValueError(original)
                self_update.record_failure(self.home, 'check', error)
                detail = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl')
                                     .read_text().splitlines()[-1])['detail']
                self.assertEqual(detail, expected)
                self.assertNotIn(home, detail)
                self.assertEqual(str(error), original)

    def test_page_error_stays_specific_when_log_hides_outside_paths(self):
        outside = self.project / 'skill_pilot' / 'self_update.py'
        stderr = f"fatal: unable to read '{outside}': Connection reset by peer"
        server = Dashboard({'home': str(self.home)})
        self.addCleanup(server.server_close)
        with patch.object(github.subprocess, 'run', return_value=self.failure(stderr)), \
                patch.object(self_update, 'check', side_effect=lambda home:
                    self_update.git(self.project, 'ls-remote', self_update.SOURCE)):
            server.program_work('check')
        self.assertEqual(server.program_progress['phase'], 'error')
        self.assertIn('连接中断', server.program_progress['error'])
        self.assertNotIn('当前操作失败', server.program_progress['error'])
        self.assertNotIn(str(outside), server.program_progress['error'])
        text = (self.home / '.skill-pilot/logs/program-update.jsonl').read_text()
        record = json.loads(text.splitlines()[-1])
        self.assertIn('Connection reset by peer', record['detail'])
        self.assertIn('[路径已隐藏]', record['detail'])
        self.assertNotIn('self_update.py', record['detail'])
        self.assertNotIn(str(outside), text)
        self.assertNotIn('Different Install Location', text)

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
