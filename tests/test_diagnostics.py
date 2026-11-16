import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import diagnostics, self_update
from skill_pilot.web.server import Dashboard


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot diagnostics ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name) / '模拟 user')
        self.home.mkdir()
        self.private = self.home / '.skill-pilot'
        self.private.mkdir()
        self.log = self.private / 'logs/errors.jsonl'
        self.env = patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self, error=None):
        return diagnostics.record(self.home, 'backend', '读取文件', error or RuntimeError('读取中断'))

    def test_records_roll_and_have_private_permissions(self):
        with patch.object(diagnostics, 'MAX_RECORDS', 3):
            rows = [self.record() for _ in range(5)]
            self.assertEqual([r['error_id'] for r in diagnostics.recent(self.home)],
                             [r['error_id'] for r in rows[-3:]][::-1])
        with patch.object(diagnostics, 'MAX_BYTES', 1400):
            for _ in range(10):
                self.record(RuntimeError('x' * 800))
            self.assertLessEqual(self.log.stat().st_size, 1400)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_read_only_validation_and_failed_logging(self):
        self.assertEqual(self.record(ValueError('请先选择技能。')), {})
        self.assertFalse(self.log.exists())
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertTrue(self.record()['error_id'])
        self.assertFalse(self.log.exists())
        with patch('skill_pilot.diagnostics.os.replace', side_effect=OSError('磁盘已满')):
            self.assertEqual(self.record(), {})
        self.assertEqual([p for p in (self.private / 'logs').iterdir() if p.name != '.errors.lock'], [])

    def test_redacts_secrets_names_paths_and_control_characters(self):
        (self.private / 'settings.json').write_text(json.dumps({'llm': {'api_key': 'private-ai-key'}}))
        (self.private / 'team.json').write_text(json.dumps({'key': 'member-secret', 'url': 'https://private-team.example', 'name': 'Private Person'}))
        (self.private / 'registry.json').write_text(json.dumps({'skills': {'private-skill-name': {}}}))
        values = ['private-ai-key', 'member-secret', 'private-skill-name', 'Private Person',
                  'https://user:pass@example.test/a?token=secret', str(self.home / '私密资料 file.txt'),
                  '/Volumes/Private disk/中文 文件.md', r'C:\Private User\文档\private file.txt',
                  'D:/Private User/秘密 文件.txt', r'\\server\Private Share\private file.txt']
        text = '\n'.join(values) + '\nAuthorization: Bearer hidden-value\x00'
        result = self.record(RuntimeError(text))
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in values + ['hidden-value', 'Private', '秘密', '私密资料']:
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn('\\u0000', serialized)

    def test_fingerprint_ignores_variable_message_and_identity_is_unique(self):
        rows = [self.record(RuntimeError(message)) for message in ('one', 'two')]
        self.assertEqual(rows[0]['fingerprint'], rows[1]['fingerprint'])
        self.assertNotEqual(rows[0]['error_id'], rows[1]['error_id'])
        self.assertNotEqual(rows[0]['fingerprint'], self.record(OSError('one'))['fingerprint'])

    def test_wrapped_system_failure_is_not_validation(self):
        try:
            try:
                raise OSError('磁盘出错')
            except OSError as error:
                raise ValueError('操作没能完成') from error
        except ValueError as error:
            row = self.record(error)
        self.assertEqual(row['error_type'], 'OSError')
        self.assertIn('磁盘出错', row['detail'])

    def test_subprocess_output_does_not_include_skill_body(self):
        error = subprocess.CalledProcessError(1, ['tool', 'private argument'], stderr='private skill body')
        row = self.record(error)
        self.assertNotIn('private', row['detail'])
        self.assertIn('1', row['detail'])

    def test_corrupt_record_and_missing_error(self):
        row = self.record()
        with self.log.open('a') as stream:
            stream.write('broken record\n')
        self.assertEqual(len(diagnostics.recent(self.home)), 1)
        with patch.object(diagnostics, 'environment', return_value={'read_only': True}):
            report = diagnostics.bundle(self.home, row['error_id'], '/outside/private file.txt')['report']
            self.assertEqual(len(report['errors']), 1)
            self.assertNotIn('private', report['note'])
            with self.assertRaises(ValueError):
                diagnostics.bundle(self.home, 'missing')

    def test_environment_uses_counts_and_known_agent_types_only(self):
        (self.private / 'registry.json').write_text(json.dumps({'skills': {'private-skill': {}}}))
        (self.private / 'agents.json').write_text(json.dumps({'onboarded': ['codex', 'private-person']}))
        (self.private / 'team.json').write_text(json.dumps({'url': 'https://private.example', 'key': 'secret'}))
        with patch('skill_pilot.diagnostics.shutil.which', return_value=None), patch('skill_pilot.diagnostics.subprocess.run', side_effect=OSError):
            result = diagnostics.environment(self.home)
        self.assertEqual(result['skill_count'], 1)
        self.assertEqual(result['agent_count'], 2)
        self.assertEqual(result['agent_types'], ['Codex', '自定义 Agent'])
        self.assertTrue(result['team_connected'])
        self.assertNotIn('private', json.dumps(result))

    def test_tool_version_keeps_major_number_after_v_prefix(self):
        with patch('skill_pilot.diagnostics.shutil.which', return_value='tool'), patch(
                'skill_pilot.diagnostics.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'v26.15.0', '')):
            result = diagnostics.environment(self.home)
        self.assertEqual(result['tools']['node']['version'], '26.15.0')

    def test_parallel_processes_keep_every_record(self):
        code = "from skill_pilot.diagnostics import record; from skill_pilot.store import target_home; [record(target_home(), 'backend', '并发读取', OSError('failure')) for _ in range(8)]"
        processes = [subprocess.Popen([sys.executable, '-B', '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for process in processes:
            _, error = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, error)
        rows = diagnostics.recent(self.home)
        self.assertEqual(len(rows), 24)
        self.assertEqual(len({row['error_id'] for row in rows}), 24)

    def test_log_directory_outside_home_is_not_written(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.private / 'logs').symlink_to(outside, target_is_directory=True)
        self.record()
        self.assertEqual(list(outside.iterdir()), [])

    def test_doctor_json_is_read_only(self):
        self.record()
        before = self.log.read_bytes()
        result = subprocess.run([sys.executable, '-B', '-m', 'skill_pilot', 'doctor', '--json'],
                                env={**os.environ, 'SKILL_PILOT_READ_ONLY': '1'},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['environment']['read_only'])
        self.assertEqual(len(data['errors']), 1)
        self.assertNotIn(str(self.home), result.stdout)
        self.assertEqual(self.log.read_bytes(), before)

    def test_update_logs_remain_compatible(self):
        self_update.record_failure(self.home, 'check', OSError('cannot open /outside/private file'))
        self.assertEqual(len(diagnostics.recent(self.home)), 1)
        self.assertTrue((self.private / 'logs/program-update.jsonl').exists())

    def test_desktop_startup_failure_is_recorded_and_alerted(self):
        spec = importlib.util.spec_from_file_location('test_launcher', diagnostics.PROJECT / 'scripts/launch.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with patch('skill_pilot.cli.main', side_effect=RuntimeError('/outside/private file')), \
                patch.object(launcher.sys, 'platform', 'darwin'), patch.object(launcher.subprocess, 'run') as alert, \
                patch.object(sys, 'argv', ['test']):
            self.assertEqual(launcher.main(), 1)
        self.assertTrue(alert.called)
        self.assertEqual(diagnostics.recent(self.home)[0]['source'], 'startup')
        self.assertFalse((diagnostics.PROJECT / 'launcher.log').exists())

    def test_startup_records_a_broken_installation_without_importing_diagnostics(self):
        spec = importlib.util.spec_from_file_location('broken_launcher', diagnostics.PROJECT / 'scripts/launch.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with patch('skill_pilot.cli.main', side_effect=SyntaxError('private content')), \
                patch.dict(sys.modules, {'skill_pilot.diagnostics': None}), \
                patch.object(launcher.sys, 'platform', 'darwin'), patch.object(launcher.subprocess, 'run'), \
                patch.object(sys, 'argv', ['test']):
            self.assertEqual(launcher.main(), 1)
        row = diagnostics.recent(self.home)[0]
        self.assertEqual(row['error_type'], 'SyntaxError')
        self.assertNotIn('private content', self.log.read_text())


class DiagnosticsWebTests(unittest.TestCase):
    def setUp(self):
        DiagnosticsTests.setUp(self)
        self.server = Dashboard({'home': str(self.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, body=None, headers=None):
        auth = {'X-Skill-Pilot-Token': self.server.token, 'Origin': self.server.origin}
        auth.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.server.origin + path, data=data, headers=auth)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_unexpected_get_and_post_failures_return_error_id(self):
        with patch('skill_pilot.web.server.operations.table', side_effect=RuntimeError('private failure')):
            status, data = self.request('/api/table')
        self.assertEqual(status, 500)
        self.assertTrue(data['error_id'])
        self.assertNotIn('private failure', data['error'])
        with patch('skill_pilot.web.server.labels.save_settings', side_effect=OSError('disk failure')):
            status, data = self.request('/api/settings/llm', {})
        self.assertTrue(data['error_id'])
        self.assertEqual(len(diagnostics.recent(self.home)), 2)

    def test_normal_validation_does_not_create_fault(self):
        status, data = self.request('/api/jobs', {'kind': 'invalid'})
        self.assertEqual(status, 400)
        self.assertNotIn('error_id', data)
        self.assertFalse(self.log.exists())

    def test_frontend_auth_size_read_only_and_bundle(self):
        route = '/api/diagnostics/client-error'
        for headers in ({'X-Skill-Pilot-Token': 'wrong'}, {'Origin': 'https://outside.example'}):
            self.assertEqual(self.request(route, {'message': 'boom'}, headers)[0], 403)
        self.assertEqual(self.request(route, {'message': 'x' * 17000})[0], 413)
        self.assertEqual(self.request(route, {'message': []})[0], 400)
        self.assertFalse(self.log.exists())
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertEqual(self.request(route, {'message': 'boom'})[0], 403)
        self.assertFalse(self.log.exists())
        status, data = self.request(route, {'message': 'boom /private/outside file'})
        self.assertEqual(status, 200)
        self.assertTrue(data['error_id'])
        with patch.object(diagnostics, 'environment', return_value={'read_only': False}):
            status, report = self.request('/api/diagnostics/bundle', {'error_id': data['error_id']})
        self.assertEqual(status, 200)
        self.assertNotIn('outside file', report['text'])
        status, rows = self.request('/api/diagnostics/errors')
        self.assertEqual(status, 200)
        self.assertNotIn('detail', rows[0])


class DiagnosticsJobTests(unittest.TestCase):
    def test_queue_returns_error_id_only_for_unexpected_failures(self):
        from tests.test_jobs import JobTests
        fixture = JobTests('test_failed_task_does_not_block_an_independent_task')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for error, expected in ((OSError('disk unavailable'), True), (ValueError('请重新核对'), False)):
            with patch.object(fixture.server, 'execute_job', side_effect=error):
                fixture.submit('assignment', **fixture.assignment('acme-extra'))
                item = fixture.wait_jobs()[-1]
            self.assertEqual(bool(item.get('error_id')), expected)
        self.assertEqual(len(diagnostics.recent(fixture.home)), 1)
