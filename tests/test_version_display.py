"""Cross-platform readable version display across checkout, ZIP, upgrades, readonly and diagnostics."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from skill_pilot import diagnostics, feedback, self_update, store, team, version as app_version
from skill_pilot.sources.github import git
from skill_pilot.web import runtime, server
from tests import set_up_home
from tests.test_nav_static import extract_function
from tests.test_self_update import commit, make_fixture


class VersionFormatAndReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot version ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / '模拟 user')
        self.home.mkdir(parents=True)

    def copy_release(self, destination):
        for name in ('skill_pilot', 'scripts', 'assets', 'docs', 'skill'):
            shutil.copytree(store.PROJECT / name, destination / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('README.md', '.gitignore', 'skill-pilot'):
            shutil.copy2(store.PROJECT / name, destination / name)
        return destination

    def serve(self, dashboard):
        thread = threading.Thread(target=dashboard.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def close():
            dashboard.shutdown()
            thread.join(3)
            dashboard.server_close()
        self.addCleanup(close)

    def post(self, dashboard, route, body=None):
        headers = {'X-Skill-Pilot-Token': dashboard.token, 'Origin': dashboard.origin,
                   'Content-Type': 'application/json'}
        req = urllib.request.Request(dashboard.origin + route, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_format_version_normalizes_timezones_and_rejects_bad_or_overflow_dates(self):
        rev = 'AbCdEf1234567890' * 2 + '01234567'
        self.assertEqual(app_version.format_version('2026-11-17T23:14:09-08:00', rev), 'v2026.11.18-abcdef1')
        self.assertEqual(app_version.format_version('2026-11-18T15:14:09+08:00', rev), 'v2026.11.18-abcdef1')
        self.assertEqual(app_version.format_version('2026-11-19', rev), 'v2026.11.19-abcdef1')
        self.assertEqual(app_version.format_version('2026-11-19T00:00:00Z', ''), 'v2026.11.19')
        self.assertEqual(app_version.format_version('', rev), 'abcdef1')
        self.assertEqual(app_version.format_version(fallback_digest='0123456789abcdef' * 4, base_version='1.0.0'),
                         'v1.0.0-0123456')
        # Invalid calendar dates and timezone overflow dates must never raise or be accepted.
        for bad in ('2026-99-99', '2026-02-30', '0001-01-01T00:00:00+01:00',
                    '9999-12-31T23:59:59-05:00', 'not-a-date'):
            with self.subTest(bad=bad):
                self.assertEqual(app_version.format_date(bad), '')
                self.assertEqual(app_version.format_version(bad, ''), '')
                self.assertEqual(app_version.format_version(bad, rev), 'abcdef1')

    def test_checkout_fresh_zip_upgraded_zip_and_crlf_produce_identical_version(self):
        repo = self.copy_release(self.root / 'Source Checkout With Spaces')
        git(repo, 'init', '-q', '-b', 'main')
        commit(repo, 'Initial release')
        rev = git(repo, 'rev-parse', 'HEAD')
        date = git(repo, 'show', '-s', '--format=%cI', 'HEAD')

        checkout_build = app_version.read_build(repo)
        self.assertEqual(checkout_build['install_mode'], '源码')
        self.assertRegex(checkout_build['version'], r'^v\d+\.\d+\.\d+-[0-9a-f]{7}$')

        # Fresh ZIP without revision/date never calls git and reports the exact same version.
        fresh_zip = self.root / 'Fresh ZIP With Spaces'
        shutil.copytree(repo, fresh_zip, ignore=shutil.ignore_patterns('.git'))
        self_update.record_installation(fresh_zip)
        with patch.object(app_version.subprocess, 'run', side_effect=AssertionError('ZIP不应调用git')):
            fresh_build = app_version.read_build(fresh_zip)
        self.assertEqual(fresh_build['install_mode'], '压缩包')
        self.assertEqual(fresh_build['version'], checkout_build['version'])

        # Upgraded ZIP with revision/date and an extra private file reports the exact same version.
        (fresh_zip / 'docs/My private note.md').write_text('personal note', encoding='utf-8')
        self_update.record_installation(fresh_zip, rev, date, self_update.read_record(fresh_zip)['files'])
        with patch.object(app_version.subprocess, 'run', side_effect=AssertionError('ZIP不应调用git')):
            upgraded_build = app_version.read_build(fresh_zip)
        self.assertEqual(upgraded_build['version'], checkout_build['version'])

        # CRLF line endings (e.g. Windows checkout) normalize to the exact same version.
        crlf_zip = self.root / 'Windows CRLF Copy'
        shutil.copytree(repo, crlf_zip, ignore=shutil.ignore_patterns('.git'))
        readme = crlf_zip / 'README.md'
        readme.write_bytes(readme.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
        self.assertEqual(app_version.read_build(crlf_zip)['version'], checkout_build['version'])

        # Missing git on a source checkout still yields the exact same version.
        with patch.object(app_version.subprocess, 'run', side_effect=FileNotFoundError('git missing')):
            no_git_build = app_version.read_build(repo)
        self.assertEqual(no_git_build['install_mode'], '源码')
        self.assertEqual(no_git_build['version'], checkout_build['version'])

    def test_frontend_only_change_produces_distinct_version_on_both_checkout_and_zip(self):
        repo = self.copy_release(self.root / 'Frontend Change Repo')
        git(repo, 'init', '-q', '-b', 'main')
        commit(repo, 'Baseline')
        zip_copy = self.root / 'Frontend Change ZIP'
        shutil.copytree(repo, zip_copy, ignore=shutil.ignore_patterns('.git'))
        self_update.record_installation(zip_copy)

        before_checkout = app_version.read_build(repo)['version']
        before_zip = app_version.read_build(zip_copy)['version']
        self.assertEqual(before_checkout, before_zip)

        for target in (repo, zip_copy):
            app_js = target / 'skill_pilot/web/static/app.js'
            app_js.write_text(app_js.read_text(encoding='utf-8') + '\n// page-only tweak\n', encoding='utf-8')
        commit(repo, 'Only update app.js')
        self_update.record_installation(zip_copy)

        after_checkout = app_version.read_build(repo)['version']
        after_zip = app_version.read_build(zip_copy)['version']
        self.assertEqual(after_checkout, after_zip)
        self.assertNotEqual(after_checkout, before_checkout)

    def test_corrupt_manifest_overflow_date_and_file_mismatch_never_crash_or_trust_stale_record(self):
        code = self.copy_release(self.root / 'Corrupt Manifest Program')
        expected_local = app_version.read_build(code)['version']
        manifest = code / '.skill-pilot-install.json'

        # Missing fields, bad date, overflow date, and deeply nested JSON (RecursionError)
        # must never block import or doctor, and readable files always use full bundle version.
        deep_nested = '[' * 2000 + ']' * 2000
        for raw in (
            json.dumps({}),
            json.dumps({'date': '2026-99-99'}),
            json.dumps({'date': '0001-01-01T00:00:00+01:00'}),
            json.dumps({'date': '0001-01-01T00:00:00+01:00', 'revision': 'e' * 40}),
            deep_nested,
        ):
            with self.subTest(raw=raw[:40]):
                manifest.write_text(raw, encoding='utf-8')
                result = subprocess.run(
                    [sys.executable, '-B', '-c',
                     'import json; from skill_pilot import diagnostics, version; '
                     'print(json.dumps({"v": version.RUNNING_BUILD["version"], "d": diagnostics.RUNNING_BUILD["app_version"]}))'],
                    cwd=code, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed = json.loads(result.stdout)
                self.assertEqual(parsed['v'], expected_local)
                self.assertEqual(parsed['d'], expected_local)

        # Deeply nested JSON manifest is still cleanly rejected before self-update with cross-platform guidance.
        manifest.write_text(deep_nested, encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            self_update.read_record(code)
        self.assertNotIn('build-app.sh', str(caught.exception))
        self.assertIn('重新运行一次安装程序', str(caught.exception))

        # Legacy revision/date-only manifest and incomplete manifest (e.g. README.md only)
        # still compute full release bundle version and detect app.js edits.
        readme_stamp = self_update.stamp(code / 'README.md')
        for stale in (
            {'revision': 'a' * 40, 'date': '2026-11-17T00:00:00Z'},
            {'files': {'README.md': readme_stamp}, 'revision': 'a' * 40, 'date': '2026-11-17T00:00:00Z'},
        ):
            with self.subTest(stale=sorted(stale)):
                manifest.write_text(json.dumps(stale), encoding='utf-8')
                before = app_version.read_build(code)
                self.assertFalse(before['manifest_valid'])
                self.assertEqual(before['version'], expected_local)
                app_js = code / 'skill_pilot/web/static/app.js'
                original_js = app_js.read_text(encoding='utf-8')
                try:
                    app_js.write_text(original_js + '\n// edit under legacy/partial manifest\n', encoding='utf-8')
                    after = app_version.read_build(code)
                    self.assertNotEqual(after['version'], before['version'])
                finally:
                    app_js.write_text(original_js, encoding='utf-8')

        # Manifest file mismatch: tampered file invalidates manifest and recomputes local version.
        self_update.record_installation(code, 'f' * 40, '2026-11-19T00:00:00Z')
        self.assertTrue(app_version.read_build(code)['manifest_valid'])
        (code / 'skill_pilot/web/static/app.js').write_text('// tampered\n', encoding='utf-8')
        tampered = app_version.read_build(code)
        self.assertFalse(tampered['manifest_valid'])
        self.assertEqual(tampered['revision'], '')
        self.assertNotEqual(tampered['version'], expected_local)

        # Unreadable / empty program folder falls back cleanly to legacy manifest or empty / 未能读取.
        empty = self.root / 'Empty Folder'
        empty.mkdir()
        (empty / '.skill-pilot-install.json').write_text(json.dumps({'revision': 'e' * 40, 'date': '2026-11-19'}), encoding='utf-8')
        self.assertEqual(app_version.read_build(empty)['version'], 'v2026.11.19-eeeeeee')
        (empty / '.skill-pilot-install.json').write_text('not json', encoding='utf-8')
        self.assertEqual(app_version.read_build(empty)['version'], '')
        self.assertEqual(diagnostics.installed_build(empty)['app_version'], '未能读取')

    def test_self_update_check_returns_unified_versions_for_checkout_and_zip(self):
        for checkout in (True, False):
            with self.subTest(checkout=checkout):
                with patch.object(self_update, 'run_installer'):
                    make_fixture(self, checkout=checkout)
                self_update.record_installation(self.code)
                up_to_date = self_update.check(self.home, self.code)
                if up_to_date['temporary']:
                    self.addCleanup(shutil.rmtree, up_to_date['temporary'], True)
                self.assertFalse(up_to_date['available'])
                self.assertRegex(up_to_date['current_version'], r'^v\d+\.\d+\.\d+-[0-9a-f]{7}$')
                self.assertEqual(up_to_date['current_version'], up_to_date['latest_version'])

                app_js = self.upstream / 'skill_pilot/web/static/app.js'
                app_js.write_text(app_js.read_text(encoding='utf-8') + '\n// ui update\n', encoding='utf-8')
                commit(self.upstream, 'Publish page-only update')
                available = self_update.check(self.home, self.code)
                if available['temporary']:
                    self.addCleanup(shutil.rmtree, available['temporary'], True)
                self.assertTrue(available['available'])
                self.assertEqual(available['current_version'], up_to_date['current_version'])
                self.assertRegex(available['latest_version'], r'^v\d+\.\d+\.\d+-[0-9a-f]{7}$')
                self.assertNotEqual(available['current_version'], available['latest_version'])

    def test_ready_blocks_recheck_and_readonly_blocks_check_without_downloading(self):
        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'}):
            dashboard = server.Dashboard({'home': str(self.home)})
            self.serve(dashboard)
            running = app_version.running_build()['version']
            dashboard.program_progress.update(
                phase='ready', message='新版已安装，刷新后生效',
                current_version=running, latest_version='v1.0.0-9999999', pending_version='v1.0.0-9999999')
            with patch.object(self_update, 'check') as check_mock:
                # Online or offline manual check from stale page must be rejected with ready payload intact.
                status, body = self.post(dashboard, '/api/program/check', {})
                self.assertEqual(status, 409)
                self.assertIn('先刷新页面', body['error'])
                self.assertEqual(body['program']['phase'], 'ready')
                self.assertEqual(body['program']['current_version'], running)
                self.assertEqual(body['program']['pending_version'], 'v1.0.0-9999999')
                check_mock.assert_not_called()
                progress = self.post(dashboard, '/api/program/progress')[1]
                self.assertEqual(progress['phase'], 'ready')
                self.assertEqual(progress['current_version'], running)
                self.assertEqual(progress['pending_version'], 'v1.0.0-9999999')

            # Unknown unexpected failure in program_work states unknown cause without guessing network/permissions.
            dashboard.program_progress.update(phase='idle', pending_version='')
            with patch.object(self_update, 'check', side_effect=RuntimeError('unexpected fault')):
                dashboard.program_work('check', automatic=False)
            self.assertEqual(dashboard.program_progress['phase'], 'error')
            self.assertIn('原因未知', dashboard.program_progress['error'])
            self.assertNotIn('网络和文件夹权限', dashboard.program_progress['error'])
            self.assertTrue(dashboard.program_progress['error_id'])

            # Read-only mode must block /api/program/check at the server before any temp dir or git fetch.
            dashboard.program_progress.update(phase='idle', pending_version='')
            with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), \
                    patch.object(self_update, 'check') as check_mock:
                status, body = self.post(dashboard, '/api/program/check', {})
                self.assertEqual(status, 403)
                self.assertIn('只读', body['error'])
                check_mock.assert_not_called()

    def test_pending_refresh_diagnostics_feedback_and_static_hot_refresh_stay_consistent(self):
        saved_running = app_version.running_build()
        def restore_running():
            app_version.set_running_version(saved_running['version'], date=saved_running['date'])
            diagnostics.sync_running_build()
        self.addCleanup(restore_running)

        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'}):
            dashboard = server.Dashboard({'home': str(self.home)})
            self.serve(dashboard)
            running = app_version.running_build()['version']
            pending = 'v1.0.0-8888888'

            # Error recorded before refresh captures old running version; also seed a legacy 12-char error.
            before_row = diagnostics.record(self.home, 'backend', '升级前操作', RuntimeError('before'))
            self.assertEqual(before_row['environment']['app_version'], running)
            legacy_row = {**before_row, 'error_id': '1' * 32,
                          'environment': {**before_row['environment'], 'app_version': 'abcdef012345'}}
            log = self.home / '.skill-pilot/logs/errors.jsonl'
            with log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(legacy_row, ensure_ascii=False) + '\n')

            # Ordinary disk change or mid-upgrade (not in ready phase) must NOT report pending_app_version.
            for phase in ('idle', 'apply'):
                with self.subTest(phase=phase), patch.object(
                        diagnostics, 'installed_build', return_value={'install_mode': '源码', 'app_version': pending}):
                    dashboard.program_progress.update(phase=phase, pending_version='')
                    status, bundled = self.post(dashboard, '/api/diagnostics/bundle', {})
                    self.assertEqual(status, 200)
                    self.assertEqual(bundled['report']['environment']['app_version'], running)
                    self.assertNotIn('pending_app_version', bundled['report']['environment'])
                    status, manual = self.post(dashboard, '/api/diagnostics/preview', {'error_id': '', 'note': ''})
                    self.assertEqual(status, 200)
                    self.assertEqual(manual['payload']['app_version'], running)
                    self.assertNotIn('pending_app_version', manual)
                    self.assertEqual(manual['payload']['detail'], '')

            dashboard.program_progress.update(
                phase='ready', message='新版已安装，刷新后生效',
                current_version=running, latest_version=pending, pending_version=pending)

            # During pending refresh: dashboard bundle and manual proactive feedback report running version + pending.
            with patch.object(diagnostics, 'installed_build', return_value={'install_mode': '源码', 'app_version': pending}):
                status, bundled = self.post(dashboard, '/api/diagnostics/bundle', {})
                self.assertEqual(status, 200)
                self.assertEqual(bundled['report']['environment']['app_version'], running)
                self.assertEqual(bundled['report']['environment']['pending_app_version'], pending)

                status, manual = self.post(dashboard, '/api/diagnostics/preview', {'error_id': '', 'note': '手动反馈'})
                self.assertEqual(status, 200)
                self.assertEqual(manual['payload']['app_version'], running)
                self.assertEqual(manual['pending_app_version'], pending)
                self.assertIn(pending, manual['payload']['detail'])

                # Error feedback preserves failure-time snapshot (including legacy 12-char version ID).
                status, legacy_preview = self.post(dashboard, '/api/diagnostics/preview', {'error_id': '1' * 32, 'note': ''})
                self.assertEqual(status, 200)
                self.assertEqual(legacy_preview['payload']['app_version'], 'abcdef012345')

            # Static-only hot refresh updates BOTH dashboard and diagnostics running version; old errors stay intact.
            with patch.object(runtime, 'version', return_value=dashboard.code_version):
                refreshed = dashboard.request_reload()
            self.assertFalse(refreshed['changed'])
            self.assertEqual(dashboard.program_progress['phase'], 'latest')
            self.assertEqual(dashboard.program_progress['current_version'], pending)
            self.assertEqual(dashboard.program_progress['pending_version'], '')
            self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], pending)

            after_row = diagnostics.record(self.home, 'backend', '刷新后操作', RuntimeError('after'))
            self.assertEqual(after_row['environment']['app_version'], pending)
            old_feedback = feedback.envelope(diagnostics.bundle(self.home, before_row['error_id'])['report'])
            new_feedback = feedback.envelope(diagnostics.bundle(self.home, after_row['error_id'])['report'])
            self.assertEqual(old_feedback['app_version'], running)
            self.assertEqual(new_feedback['app_version'], pending)

            # Ordinary (non-ready) static-only refresh (e.g. app.js changed on disk) also syncs running version.
            static_next = 'v1.0.0-7777777'
            with patch.object(runtime, 'version', return_value=dashboard.code_version), \
                    patch.object(app_version, 'read_build', return_value={
                        'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                        'manifest_valid': True, 'version': static_next}):
                dashboard.request_reload()
            self.assertEqual(dashboard.program_progress['current_version'], static_next)
            self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], static_next)
            after_static_row = diagnostics.record(self.home, 'backend', '普通静态刷新后', RuntimeError('static'))
            self.assertEqual(after_static_row['environment']['app_version'], static_next)
            self.assertEqual(feedback.envelope(diagnostics.bundle(self.home, after_row['error_id'])['report'])['app_version'], pending)
            self.assertEqual(feedback.envelope(diagnostics.bundle(self.home, after_static_row['error_id'])['report'])['app_version'], static_next)

    def test_stale_page_check_during_ready_recovers_directly_from_409_payload_even_if_progress_fails(self):
        source = (store.PROJECT / 'skill_pilot/web/static/app.js').read_text(encoding='utf-8')
        fn = extract_function(source, 'async function programAction(')
        script = r'''
const assert = require('node:assert/strict');
let programUpdate = {running: false, phase: 'latest', available: false, write_allowed: true, current_version: 'v1.0.0-1111111'};
let programFailedAction = '', programActionRunning = false, writeBusy = false;
const $ = () => ({});
const programText = s => s;
let renders = 0;
const renderProgram = () => { renders++; };
const api = async (path) => {
  if (path === '/api/program/check') {
    const error = new Error('新版已安装，请先刷新页面使新版生效，再检查更新。');
    error.program = {running: false, phase: 'ready', available: false, write_allowed: true,
      current_version: 'v1.0.0-1111111', pending_version: 'v1.0.0-2222222', latest_version: 'v1.0.0-2222222'};
    throw error;
  }
  throw new Error('follow-up progress should not be required and fails offline');
};
''' + fn + r'''
(async () => {
  await programAction('check');
  assert.equal(programUpdate.phase, 'ready');
  assert.equal(programUpdate.pending_version, 'v1.0.0-2222222');
  assert.equal(programUpdate.current_version, 'v1.0.0-1111111');
  assert.equal(programFailedAction, '');
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
