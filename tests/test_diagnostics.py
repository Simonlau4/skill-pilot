from contextlib import contextmanager
import importlib.util
import http.client
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import diagnostics, labels, self_update
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
        with patch('skill_pilot.diagnostics.os.write', side_effect=OSError('磁盘已满')):
            self.assertEqual(self.record(), {})
        self.assertEqual([p for p in (self.private / 'logs').iterdir() if p.name != '.errors.lock'], [])

    def test_a_brief_file_lock_is_retried_and_the_record_is_kept(self):
        first = self.record()
        real = os.replace
        calls = {'n': 0}

        def occupied(source, destination):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError('文件正被占用')
            return real(source, destination)

        with patch.object(diagnostics, 'MAX_RECORDS', 1), patch('skill_pilot.diagnostics.os.replace', occupied):
            row = self.record()
        self.assertTrue(row.get('error_id'))
        self.assertNotEqual(row['error_id'], first['error_id'])
        self.assertEqual([item['error_id'] for item in diagnostics.recent(self.home)], [row['error_id']])
        self.assertGreater(calls['n'], 1)
        self.assertEqual([p.name for p in (self.private / 'logs').iterdir() if p.name.startswith('.errors-')], [])

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

    def test_escaped_credentials_are_hidden_through_ai_connection_failure(self):
        secret = 'demoKEYpartA\r\npartBdoNotLog'
        config = {'api_key': secret, 'base_url': 'https://ai.example.test', 'model': 'demo'}
        (self.private / 'settings.json').write_text(json.dumps({'llm': config}))
        def invalid_header(request, **kwargs):
            # Use the real HTTP header validator, without making a connection.
            connection = http.client.HTTPConnection('localhost')
            connection.putrequest('POST', '/')
            connection.putheader('Authorization', request.get_header('Authorization'))
        with patch('skill_pilot.labels.urllib.request.OpenerDirector.open', side_effect=invalid_header):
            result = labels.test(self.home)
        self.assertTrue(result['error_id'])
        stored = self.log.read_text()
        self.assertIn('ValueError', stored)
        self.assertNotIn('demoKEYpartA', stored)
        self.assertNotIn('partBdoNotLog', stored)
        for value in (repr(secret), repr(secret.encode()), json.dumps(secret)):
            clean = diagnostics.sanitize(value, self.home)
            self.assertNotIn('demoKEYpartA', clean)
            self.assertNotIn('partBdoNotLog', clean)

    def test_lock_release_uses_the_supported_cross_platform_contract(self):
        streams = []
        locks = SimpleNamespace(LOCK_EX=2, flock=lambda stream, operation: streams.append(stream))
        with patch.object(diagnostics, 'fcntl', locks):
            row = self.record()
        self.assertTrue(row['error_id'])
        self.assertEqual(diagnostics.recent(self.home)[0]['error_id'], row['error_id'])
        self.assertTrue(streams[0].closed)

    def test_parse_and_timeout_are_faults_but_missing_configuration_is_not(self):
        from skill_pilot.sources.update_github import Reader
        try:
            tomllib.loads('broken = [')
        except tomllib.TOMLDecodeError as error:
            self.assertTrue(self.record(error)['error_id'])
        try:
            Reader(self.home / 'temporary repository', deadline=0).remaining()
        except ValueError as error:
            self.assertEqual(self.record(error)['error_type'], 'TimeoutError')
        self.assertNotIn('error_id', labels.test(self.home))
        self.assertEqual(len(diagnostics.recent(self.home)), 2)

    def test_frontend_locations_strip_query_credentials_and_keep_distinct_sites(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const handlers = {}, requests = [];
const context = {
  URL, URLSearchParams, location: {search: '?token=local-session', href: 'http://localhost/'},
  window: {addEventListener: (name, fn) => handlers[name] = fn, dispatchEvent: () => {}},
  CustomEvent: class {},
  fetch: async (url, options) => { requests.push(JSON.parse(options.body)); return {ok: false}; }
};
vm.runInNewContext(fs.readFileSync('skill_pilot/web/static/diagnostics.js', 'utf8'), context);
(async () => {
  await handlers.error({error: new TypeError('first'), filename: 'http://localhost/app.js?token=private-token', lineno: 12, colno: 4});
  for (const stack of [
    'TypeError: second\n    at run (http://localhost/app.js?token=private-token:45:9)',
    'run@http://localhost/diagnostics.js?token=private-token:8:2',
    'run@http://localhost/vendor.js?token=private-token:8:2'
  ]) await handlers.unhandledrejection({reason: {name: 'TypeError', message: 'second', stack}});
  assert.deepEqual(requests.map(r => r.location), ['app.js:12:4', 'app.js:45:9', 'diagnostics.js:8:2', '']);
  assert(!JSON.stringify(requests).includes('private-token'));
  console.log(JSON.stringify(requests));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', script], cwd=diagnostics.PROJECT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = []
        for item in json.loads(result.stdout)[:2]:
            error = RuntimeError(item['message'])
            error.diagnostic_type = item['error_type']
            error.diagnostic_location = item['location']
            rows.append(diagnostics.record(self.home, 'frontend', '页面运行', error))
        self.assertNotEqual(rows[0]['fingerprint'], rows[1]['fingerprint'])

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
        for round_number in range(4):
            if self.log.exists():
                self.log.unlink()
            processes = [subprocess.Popen([sys.executable, '-B', '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
            for process in processes:
                _, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, error)
            rows = diagnostics.recent(self.home)
            self.assertEqual(len(rows), 24, f'第 {round_number + 1} 轮丢失了错误记录')
            self.assertEqual(len({row['error_id'] for row in rows}), 24)

    def test_overlapping_rewrites_drop_a_record_when_the_lock_does_not_exclude(self):
        barrier = threading.Barrier(2)

        def rewrite_stale_snapshot(path, line):
            current = path.read_bytes() if path.is_file() else b''
            barrier.wait(3)
            diagnostics._rewrite_log(path, self.home, current + line)

        @contextmanager
        def no_lock(path, home):
            path.parent.mkdir(parents=True, exist_ok=True)
            yield

        class NoThreadLock:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(diagnostics, '_append_line', rewrite_stale_snapshot), patch.object(
                diagnostics, 'log_lock', no_lock), patch.object(diagnostics, 'LOCK', NoThreadLock()):
            threads = [threading.Thread(target=self.record) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(diagnostics.recent(self.home)), 1)

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

    def test_external_process_failures_retain_type_without_private_output(self):
        from skill_pilot.sources import github, lark
        from skill_pilot.sources.update_github import Reader
        reader = Reader(self.home / 'upstream snapshot')
        failed = subprocess.CompletedProcess(['test-tool'], 1, b'private output', b'private stderr')
        actions = [lambda: self_update.run_installer(self.home, self.home),
                   lambda: github.api('repos/example/demo'),
                   lambda: reader.snapshot(self.home, 'abc')]
        for action in actions:
            with patch('subprocess.run', return_value=failed), patch.object(reader, 'git', return_value=''):
                with self.assertRaises(ValueError) as caught:
                    action()
            row = self.record(caught.exception)
            self.assertEqual(row['error_type'], 'CalledProcessError')
            self.assertNotIn('private', row['detail'])
        for returncode in (1, 0):
            result = subprocess.CompletedProcess(['test-tool'], returncode, '{"ok": false}', 'private stderr')
            with patch('subprocess.run', return_value=result), patch('shutil.which', return_value='test-tool'):
                with self.assertRaises(ValueError) as caught:
                    lark.check(home=self.home)
            self.assertTrue(self.record(caught.exception)['error_id'])

    def test_cached_repository_failure_preserves_its_cause(self):
        from skill_pilot.sources.update_github import Reader
        reader = Reader(self.home / 'repository cache')
        with patch.object(reader, '_repository', side_effect=ValueError('读取失败')) as load:
            load.side_effect.__cause__ = OSError('无法读取')
            for _ in range(2):
                with self.assertRaises(ValueError) as caught:
                    reader.repository({'repository': 'example/repo'})
                self.assertEqual(self.record(caught.exception)['error_type'], 'OSError')
            self.assertEqual(load.call_count, 1)

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

    def test_cleanup_preview_fault_is_recorded_even_with_400_status(self):
        with patch('skill_pilot.web.server.cleanup.preview', side_effect=OSError('private failure')):
            status, data = self.request('/api/cleanup/preview', {'skills': []})
        self.assertEqual(status, 400)
        self.assertTrue(data['error_id'])
        self.assertNotIn('private failure', data['error'])
        self.assertEqual(len(diagnostics.recent(self.home)), 1)

    def test_failed_version_validation_reaches_both_logs_and_progress(self):
        failed = subprocess.CompletedProcess(['validation'], 1, b'private body', b'private key')
        with patch('skill_pilot.self_update.check', side_effect=lambda home: self_update.validate(home, home)), \
                patch('subprocess.run', return_value=failed):
            self.server.program_work('check')
        row = diagnostics.recent(self.home)[0]
        self.assertEqual(self.server.program_progress['error_id'], row['error_id'])
        self.assertEqual(row['error_type'], 'CalledProcessError')
        self.assertNotIn('private', row['detail'])
        self.assertTrue((self.private / 'logs/program-update.jsonl').exists())

    def test_failed_external_query_reaches_http_response_and_log(self):
        from skill_pilot.sources import github
        failed = subprocess.CompletedProcess(['query'], 1, 'private body', 'private key')
        with patch('skill_pilot.sources.github.inspect', side_effect=lambda url: github.api('repos/example/repo')), \
                patch('subprocess.run', return_value=failed):
            status, data = self.request('/api/install/inspect', {'url': 'https://github.com/example/repo'})
        self.assertEqual(status, 409)
        row = diagnostics.recent(self.home)[0]
        self.assertEqual(data['error_id'], row['error_id'])
        self.assertEqual(row['error_type'], 'CalledProcessError')
        self.assertNotIn('private', row['detail'])

    def test_failed_refresh_reaches_status_and_log(self):
        from skill_pilot.web import runtime
        failed = subprocess.CompletedProcess(['validation'], 1, b'private body', b'private key')
        self.server.reload_callback = lambda: runtime.replace(self.server, None)
        self.server.reload_pending = True
        with patch('subprocess.run', return_value=failed):
            self.server.reload_when_idle()
        _, status = self.request('/api/status')
        self.assertEqual(status['error_id'], diagnostics.recent(self.home)[0]['error_id'])
        self.assertTrue(status['reload_error'])

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
