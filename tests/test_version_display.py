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

        # Upgraded ZIP with revision/date and an extra private file in docs/ keeps the exact official version.
        (fresh_zip / 'docs/My private note.md').write_text('personal note', encoding='utf-8')
        self_update.record_installation(fresh_zip, rev, date, self_update.read_record(fresh_zip)['files'])
        with patch.object(app_version.subprocess, 'run', side_effect=AssertionError('ZIP不应调用git')):
            upgraded_build = app_version.read_build(fresh_zip)
        self.assertTrue(upgraded_build['manifest_valid'])
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
                    cwd=code, capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed = json.loads(result.stdout)
                self.assertEqual(parsed['v'], expected_local)
                self.assertEqual(parsed['d'], expected_local)

        # Deeply nested JSON manifest must also not crash `doctor --json`, while self_update still rejects it.
        manifest.write_text(deep_nested, encoding='utf-8')
        doctor = subprocess.run(
            [sys.executable, '-B', '-m', 'skill_pilot', 'doctor', '--json'],
            cwd=code, env={**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '1'},
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)['environment']['app_version'], expected_local)
        with self.assertRaises(ValueError) as caught:
            self_update.read_record(code)
        self.assertNotIn('build-app.sh', str(caught.exception))
        self.assertIn('重新运行一次安装程序', str(caught.exception))

        # Legacy revision/date-only manifest, incomplete manifest (README.md only), and manifests missing
        # `skill-pilot`, `README.md`, or `docs/STATUS.md` are all invalid and still compute full bundle version.
        self_update.record_installation(code, 'a' * 40, '2026-11-17T00:00:00Z')
        full_files = self_update.read_record(code)['files']
        without_launcher = {k: v for k, v in full_files.items() if k != 'skill-pilot'}
        without_readme = {k: v for k, v in full_files.items() if k != 'README.md'}
        without_status = {k: v for k, v in full_files.items() if k != 'docs/STATUS.md'}
        readme_stamp = self_update.stamp(code / 'README.md')
        for label, stale in (
            ('legacy', {'revision': 'a' * 40, 'date': '2026-11-17T00:00:00Z'}),
            ('readme-only', {'files': {'README.md': readme_stamp}, 'revision': 'a' * 40, 'date': '2026-11-17T00:00:00Z'}),
            ('no-launcher', {'files': without_launcher, 'revision': 'a' * 40, 'date': '2026-11-17T00:00:00Z'}),
            ('no-readme', {'files': without_readme, 'revision': 'a' * 40, 'date': '2026-11-17T00:00:00Z'}),
            ('no-status', {'files': without_status, 'revision': 'a' * 40, 'date': '2026-11-17T00:00:00Z'}),
        ):
            with self.subTest(label=label):
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

        # self_update.check() must NOT shortcut to "latest" when manifest is incomplete even if revision == HEAD.
        with patch.object(self_update, 'run_installer'):
            make_fixture(self, checkout=False)
        head = git(self.upstream, 'rev-parse', 'HEAD')
        self_update.record_installation(self.code, head, '2026-11-19T00:00:00Z')
        complete_files = self_update.read_record(self.code)['files']
        # Single-file manifest missing core files is rejected immediately.
        (self.code / '.skill-pilot-install.json').write_text(json.dumps({
            'files': {'README.md': self_update.stamp(self.code / 'README.md')},
            'revision': head, 'date': '2026-11-19T00:00:00Z'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '重新运行一次安装程序'):
            self_update.check(self.home, self.code)
        # Manifest missing root `skill-pilot`, `README.md`, or `docs/STATUS.md` — even when deleted from BOTH
        # disk and manifest — must fail validation and never falsely shortcut to "latest".
        for omitted in ('skill-pilot', 'README.md', 'docs/STATUS.md'):
            with self.subTest(omitted=omitted):
                incomplete = {k: v for k, v in complete_files.items() if k != omitted}
                (self.code / '.skill-pilot-install.json').write_text(json.dumps({
                    'files': incomplete, 'revision': head, 'date': '2026-11-19T00:00:00Z'}), encoding='utf-8')
                self.assertFalse(app_version.read_build(self.code)['manifest_valid'])
                with self.assertRaisesRegex(ValueError, '重新运行一次安装程序'):
                    self_update.check(self.home, self.code)
                omitted_path = self.code / omitted
                saved_bytes = omitted_path.read_bytes()
                try:
                    omitted_path.unlink()
                    self.assertFalse(app_version.read_build(self.code)['manifest_valid'])
                    with self.assertRaisesRegex(ValueError, '重新运行一次安装程序'):
                        self_update.check(self.home, self.code)
                finally:
                    omitted_path.write_bytes(saved_bytes)

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

            # Static-only hot refresh while 'ready' MUST verify disk build first:
            # unreadable or mismatched disk keeps old running version and invalidates stale conclusions.
            for label, patch_kwargs in (
                ('ready-refresh-read-error', {'side_effect': OSError('disk unreadable')}),
                ('ready-refresh-mismatch', {'return_value': {
                    'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                    'manifest_valid': True, 'version': 'v1.0.0-4444444'}}),
            ):
                with self.subTest(hot_refresh=label):
                    dashboard.program_progress.update(
                        phase='ready', message='新版已安装，刷新后生效',
                        latest='2026-11-19T00:00:00Z', checked_at=12345.0, target='t' * 40,
                        current_version=running, latest_version=pending, pending_version=pending)
                    with patch.object(runtime, 'version', return_value=dashboard.code_version), \
                            patch.object(app_version, 'read_build', **patch_kwargs):
                        dashboard.request_reload()
                    self.assertEqual(dashboard.running_version, running)
                    self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], running)
                    self.assertEqual(dashboard.program_progress['phase'], 'idle')
                    self.assertEqual(dashboard.program_progress['current_version'], running)
                    self.assertEqual(dashboard.program_progress['latest'], '')
                    self.assertEqual(dashboard.program_progress['latest_version'], '')
                    self.assertEqual(dashboard.program_progress['pending_version'], '')
                    self.assertNotIn('checked_at', dashboard.program_progress)
                    self.assertNotIn('target', dashboard.program_progress)

            # When disk_version != latest_version while available=False (e.g. untracked file in skill_pilot/),
            # MUST NOT enter 'ready' or 'latest' (avoids ready->idle->ready loop) and must report local difference.
            for auto, expect_phase in ((False, 'error'), (True, 'idle')):
                with self.subTest(disk_differs_auto=auto), patch.object(self_update, 'check', return_value={
                        'temporary': '', 'target': 't' * 40, 'current': '2026-11-19', 'latest': '2026-11-19',
                        'current_version': 'v1.0.0-ddddddd', 'latest_version': pending, 'available': False}):
                    dashboard.program_work('check', automatic=auto)
                self.assertEqual(dashboard.program_progress['phase'], expect_phase)
                self.assertEqual(dashboard.program_progress['pending_version'], '')
                self.assertIn('差异', dashboard.program_progress['message'])
                if not auto:
                    self.assertIn('差异', dashboard.program_progress['error'])
                    self.assertEqual(dashboard.program_progress['failed_action'], 'check')
                else:
                    self.assertEqual(dashboard.program_progress['error'], '')

            # When disk_version or latest_version is empty while available=False (e.g. unreadable file in program dir),
            # MUST NEVER report 'latest' ("已是最新版本").
            for case_name, c_ver, l_ver in (
                ('empty-disk-version', '', pending),
                ('empty-latest-version', running, ''),
                ('both-empty', '', ''),
            ):
                for auto, expect_phase in ((False, 'error'), (True, 'idle')):
                    with self.subTest(unverified=case_name, auto=auto), patch.object(self_update, 'check', return_value={
                            'temporary': '', 'target': 't' * 40, 'current': '', 'latest': '',
                            'current_version': c_ver, 'latest_version': l_ver, 'available': False}):
                        dashboard.program_work('check', automatic=auto)
                    self.assertEqual(dashboard.program_progress['phase'], expect_phase)
                    self.assertEqual(dashboard.program_progress['pending_version'], '')
                    self.assertNotIn('已是最新', dashboard.program_progress['message'])

            # Check when disk is already at latest (disk_version == latest_version) while running_version is still behind MUST enter 'ready', not 'latest'.
            with patch.object(self_update, 'check', return_value={
                    'temporary': '', 'target': 't' * 40, 'current': '2026-11-19', 'latest': '2026-11-19',
                    'current_version': pending, 'latest_version': pending, 'available': False}):
                dashboard.program_work('check', automatic=False)
            self.assertEqual(dashboard.program_progress['phase'], 'ready')
            self.assertEqual(dashboard.program_progress['message'], '新版已安装，刷新后生效')
            self.assertEqual(dashboard.program_progress['current_version'], running)
            self.assertEqual(dashboard.program_progress['latest_version'], pending)
            self.assertEqual(dashboard.program_progress['pending_version'], pending)

            # Interleaved apply+refresh: if backend code changes between initial runtime.version() check
            # and reading ready/disk build, request_reload() MUST trigger backend reload instead of static hot-refresh.
            reload_called = threading.Event()
            dashboard.reload_callback = reload_called.set
            try:
                with patch.object(runtime, 'version', side_effect=[dashboard.code_version, 'new-backend-code']), \
                        patch.object(app_version, 'read_build', return_value={
                            'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                            'manifest_valid': True, 'version': pending}):
                    interleaved = dashboard.request_reload()
                self.assertTrue(interleaved['changed'])
                self.assertTrue(reload_called.wait(2))
                if dashboard.reload_thread:
                    dashboard.reload_thread.join(2)
                self.assertEqual(dashboard.program_progress['phase'], 'ready')
                self.assertEqual(dashboard.running_version, running)
                self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], running)
            finally:
                dashboard.reload_callback = None
                dashboard.reload_pending = False

            # Static-only hot refresh when disk matches pending_version updates BOTH dashboard and diagnostics; old errors stay intact.
            dashboard.program_progress.update(
                phase='ready', message='新版已安装，刷新后生效',
                current_version=running, latest_version=pending, pending_version=pending)
            with patch.object(runtime, 'version', return_value=dashboard.code_version), \
                    patch.object(app_version, 'read_build', return_value={
                        'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                        'manifest_valid': True, 'version': pending}):
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

            # Mid-upgrade refresh (phase=='apply' / running==True) must NOT prematurely adopt partial disk version.
            mid_upgrade_version = 'v1.0.0-6666666'
            dashboard.program_progress.update(running=True, phase='apply', current_version=pending)
            with patch.object(runtime, 'version', return_value=dashboard.code_version), \
                    patch.object(app_version, 'read_build', return_value={
                        'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                        'manifest_valid': True, 'version': mid_upgrade_version}):
                dashboard.request_reload()
            self.assertEqual(dashboard.running_version, pending)
            self.assertEqual(dashboard.program_progress['current_version'], pending)
            self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], pending)

            # Ordinary (non-ready) static-only refresh (e.g. app.js changed on disk) syncs running version
            # AND invalidates the stale 'latest' check conclusion so outdated latest_version does not linger.
            static_next = 'v1.0.0-7777777'
            dashboard.program_progress.update(
                running=False, phase='latest', available=False,
                current_version=pending, latest_version=pending, checked_at=12345.0, target='old-target')
            with patch.object(runtime, 'version', return_value=dashboard.code_version), \
                    patch.object(app_version, 'read_build', return_value={
                        'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                        'manifest_valid': True, 'version': static_next}):
                dashboard.request_reload()
            self.assertEqual(dashboard.program_progress['current_version'], static_next)
            self.assertEqual(dashboard.program_progress['phase'], 'idle')
            self.assertEqual(dashboard.program_progress['latest_version'], '')
            self.assertNotIn('checked_at', dashboard.program_progress)
            self.assertNotIn('target', dashboard.program_progress)
            self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], static_next)
            after_static_row = diagnostics.record(self.home, 'backend', '普通静态刷新后', RuntimeError('static'))
            self.assertEqual(after_static_row['environment']['app_version'], static_next)
            self.assertEqual(feedback.envelope(diagnostics.bundle(self.home, after_row['error_id'])['report'])['app_version'], pending)
            self.assertEqual(feedback.envelope(diagnostics.bundle(self.home, after_static_row['error_id'])['report'])['app_version'], static_next)

            # Path 1: Static refresh arriving during an in-flight check increments static_refresh_seq
            # and defers version recheck until check completes, looping if a second refresh arrives mid-read.
            deferred_mid = 'v1.0.0-5555554'
            deferred_next = 'v1.0.0-5555555'
            seq_before = dashboard.static_refresh_seq
            dashboard.program_progress.update(running=True, phase='check', available=False,
                                              current_version=static_next, latest_version='')
            dashboard.program_action = 'check'
            with patch.object(runtime, 'version', return_value=dashboard.code_version):
                dashboard.request_reload()
            self.assertEqual(dashboard.static_refresh_seq, seq_before + 1)
            self.assertEqual(dashboard.running_version, static_next)
            read_calls = []

            def two_stage_read():
                read_calls.append(dashboard.static_refresh_seq)
                if len(read_calls) == 1:
                    # A second static refresh arrives while read_build() runs outside program_lock!
                    with patch.object(runtime, 'version', return_value=dashboard.code_version):
                        dashboard.request_reload()
                    return {'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                            'manifest_valid': True, 'version': deferred_mid}
                return {'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                        'manifest_valid': True, 'version': deferred_next}

            with patch.object(self_update, 'check', return_value={
                    'temporary': '', 'target': 't' * 40, 'current': '', 'latest': '',
                    'current_version': static_next, 'latest_version': static_next, 'available': False}), \
                    patch.object(app_version, 'read_build', side_effect=two_stage_read):
                dashboard.program_work('check', automatic=False, start_refresh_seq=seq_before)
            self.assertEqual(len(read_calls), 2)
            self.assertEqual(dashboard.static_refresh_seq, seq_before + 2)
            self.assertEqual(dashboard.running_version, deferred_next)
            self.assertEqual(dashboard.program_progress['current_version'], deferred_next)
            self.assertEqual(dashboard.program_progress['phase'], 'idle')
            self.assertEqual(dashboard.program_progress['latest_version'], '')
            self.assertNotIn('checked_at', dashboard.program_progress)
            self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], deferred_next)

            # Path 1b: Exception during deferred read_build clears stale 'latest'/checked_at AND running=False.
            seq_before_err = dashboard.static_refresh_seq
            dashboard.program_progress.update(
                running=True, phase='latest', available=False,
                current_version=deferred_next, latest_version=deferred_next,
                checked_at=12345.0, target='old-target')
            dashboard.program_action = 'check'
            with patch.object(runtime, 'version', return_value=dashboard.code_version):
                dashboard.request_reload()
            with patch.object(self_update, 'check', return_value={
                    'temporary': '', 'target': 't' * 40, 'current': '', 'latest': '',
                    'current_version': deferred_next, 'latest_version': deferred_next, 'available': False}), \
                    patch.object(app_version, 'read_build', side_effect=OSError('transient disk error')):
                dashboard.program_work('check', automatic=False, start_refresh_seq=seq_before_err)
            self.assertFalse(dashboard.program_progress['running'])
            self.assertEqual(dashboard.program_action, '')
            self.assertEqual(dashboard.program_progress['phase'], 'idle')
            self.assertEqual(dashboard.program_progress['latest_version'], '')
            self.assertNotIn('checked_at', dashboard.program_progress)
            self.assertNotIn('target', dashboard.program_progress)

            # Path 1c: Deferred recheck after static_refresh_seq increments MUST check runtime.version() first:
            # if backend code changed in the meantime, keep old running_version/RUNNING_BUILD and invalidate check.
            seq_before_backend = dashboard.static_refresh_seq
            dashboard.program_progress.update(
                running=True, phase='check', available=False,
                current_version=deferred_next, latest_version='', checked_at=12345.0, target='old-target')
            dashboard.program_action = 'check'
            with patch.object(runtime, 'version', return_value=dashboard.code_version):
                dashboard.request_reload()
            with patch.object(self_update, 'check', return_value={
                    'temporary': '', 'target': 't' * 40, 'current': '', 'latest': '',
                    'current_version': deferred_next, 'latest_version': deferred_next, 'available': False}), \
                    patch.object(runtime, 'version', return_value='backend-changed-during-check'), \
                    patch.object(app_version, 'read_build', return_value={
                        'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                        'manifest_valid': True, 'version': 'v1.0.0-eeeeeee'}):
                dashboard.program_work('check', automatic=False, start_refresh_seq=seq_before_backend)
            self.assertFalse(dashboard.program_progress['running'])
            self.assertEqual(dashboard.program_action, '')
            self.assertEqual(dashboard.running_version, deferred_next)
            self.assertEqual(dashboard.program_progress['current_version'], deferred_next)
            self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], deferred_next)
            self.assertEqual(dashboard.program_progress['phase'], 'idle')
            self.assertEqual(dashboard.program_progress['latest'], '')
            self.assertEqual(dashboard.program_progress['latest_version'], '')
            self.assertNotIn('checked_at', dashboard.program_progress)
            self.assertNotIn('target', dashboard.program_progress)

            # Path 1d: Deferred recheck when running A, check saw disk B (would enter 'ready'),
            # then disk reverted back to A and static refresh fired -> A != checked_disk_version (B),
            # so stale 'ready' conclusion MUST be withdrawn instead of publishing B as pending!
            seq_before_revert = dashboard.static_refresh_seq
            dashboard.program_progress.update(
                running=True, phase='check', available=False,
                current_version=deferred_next, latest_version='', checked_at=12345.0, target='old-target')
            dashboard.program_action = 'check'
            with patch.object(runtime, 'version', return_value=dashboard.code_version):
                dashboard.request_reload()
            with patch.object(self_update, 'check', return_value={
                    'temporary': '', 'target': 't' * 40, 'current': '2026-11-19', 'latest': '2026-11-19',
                    'current_version': 'v1.0.0-bbbbbbb', 'latest_version': 'v1.0.0-bbbbbbb', 'available': False}), \
                    patch.object(runtime, 'version', return_value=dashboard.code_version), \
                    patch.object(app_version, 'read_build', return_value={
                        'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                        'manifest_valid': True, 'version': deferred_next}):
                dashboard.program_work('check', automatic=False, start_refresh_seq=seq_before_revert)
            self.assertFalse(dashboard.program_progress['running'])
            self.assertEqual(dashboard.running_version, deferred_next)
            self.assertEqual(dashboard.program_progress['phase'], 'idle')
            self.assertEqual(dashboard.program_progress['pending_version'], '')
            self.assertEqual(dashboard.program_progress['latest_version'], '')
            self.assertNotIn('checked_at', dashboard.program_progress)
            self.assertNotIn('target', dashboard.program_progress)

            # Path 1e: apply() reporting version_verified=False distinguishes unmanaged files vs general failure
            # and sets can_retry_apply=False (while rollback failure with available review sets can_retry_apply=True).
            dashboard.program_review = {'temporary': '', 'target': 't' * 40, 'available': True,
                                        'latest': '2026-11-19', 'latest_version': 'v1.0.0-9999999'}
            with patch.object(self_update, 'apply', return_value={
                    'available': False, 'current': '2026-11-19', 'latest': '2026-11-19',
                    'latest_version': 'v1.0.0-9999999', 'pending_version': 'v1.0.0-9999999',
                    'installed_version': 'v1.0.0-ddddddd', 'version_verified': False, 'has_unmanaged_files': True}):
                dashboard.program_work('apply', automatic=False)
            self.assertEqual(dashboard.program_progress['phase'], 'error')
            self.assertEqual(dashboard.program_progress['pending_version'], '')
            self.assertEqual(dashboard.program_progress['failed_action'], 'apply')
            self.assertFalse(dashboard.program_progress['can_retry_apply'])
            self.assertIn('额外文件', dashboard.program_progress['error'])

            dashboard.program_review = {'temporary': '', 'target': 't' * 40, 'available': True,
                                        'latest': '2026-11-19', 'latest_version': 'v1.0.0-9999999'}
            with patch.object(self_update, 'apply', return_value={
                    'available': False, 'current': '2026-11-19', 'latest': '2026-11-19',
                    'latest_version': 'v1.0.0-9999999', 'pending_version': 'v1.0.0-9999999',
                    'installed_version': '', 'version_verified': False, 'has_unmanaged_files': False,
                    'error_id': 'e' * 32}):
                dashboard.program_work('apply', automatic=False)
            self.assertEqual(dashboard.program_progress['phase'], 'error')
            self.assertIsNone(dashboard.program_review)
            self.assertFalse(dashboard.program_progress['can_retry_apply'])
            self.assertEqual(dashboard.program_progress['error_id'], 'e' * 32)
            self.assertIn('未能核实安装后的版本', dashboard.program_progress['error'])
            self.assertNotIn('额外文件', dashboard.program_progress['error'])

            dashboard.program_review = {'temporary': '', 'target': 't' * 40, 'available': True,
                                        'latest': '2026-11-19', 'latest_version': 'v1.0.0-9999999'}
            with patch.object(self_update, 'apply', side_effect=ValueError('升级没有完成，已恢复原版')):
                dashboard.program_work('apply', automatic=False)
            self.assertEqual(dashboard.program_progress['phase'], 'error')
            self.assertTrue(dashboard.program_progress['can_retry_apply'])

            # Path 1f: When running_version is empty, program_work('check') NEVER fills current_version with disk_version,
            # and neither idle static refresh nor deferred recheck fills empty running_version from disk.
            saved_dash_running = dashboard.running_version
            dashboard.running_version = ''
            try:
                for auto, available, expect_phase in (
                    (False, False, 'error'),
                    (True, False, 'idle'),
                    (False, True, 'available'),
                ):
                    with self.subTest(empty_running=True, auto=auto, available=available), \
                            patch.object(self_update, 'check', return_value={
                                'temporary': '', 'target': 't' * 40, 'current': '2026-11-19', 'latest': '2026-11-19',
                                'current_version': pending, 'latest_version': pending, 'available': available}):
                        dashboard.program_work('check', automatic=auto)
                    self.assertEqual(dashboard.program_progress['current_version'], '')
                    self.assertEqual(dashboard.program_progress['phase'], expect_phase)

                # Idle static refresh with empty running_version withdraws stale check and keeps running_version empty.
                with patch.object(runtime, 'version', return_value=dashboard.code_version), \
                        patch.object(app_version, 'read_build', return_value={
                            'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                            'manifest_valid': True, 'version': 'v1.0.0-9999999'}):
                    dashboard.request_reload()
                self.assertEqual(dashboard.running_version, '')
                self.assertEqual(dashboard.program_progress['current_version'], '')
                self.assertEqual(dashboard.program_progress['phase'], 'idle')

                # Deferred recheck overlapping with check keeps running_version empty and withdraws stale check.
                seq_empty = dashboard.static_refresh_seq
                dashboard.program_progress.update(
                    running=True, phase='check', available=False, current_version='', checked_at=12345.0)
                dashboard.program_action = 'check'
                with patch.object(runtime, 'version', return_value=dashboard.code_version):
                    dashboard.request_reload()
                with patch.object(self_update, 'check', return_value={
                        'temporary': '', 'target': 't' * 40, 'current': '2026-11-19', 'latest': '2026-11-19',
                        'current_version': pending, 'latest_version': pending, 'available': False}), \
                        patch.object(runtime, 'version', return_value=dashboard.code_version), \
                        patch.object(app_version, 'read_build', return_value={
                            'install_mode': '源码', 'checkout': True, 'revision': '', 'date': '',
                            'manifest_valid': True, 'version': 'v1.0.0-9999999'}):
                    dashboard.program_work('check', automatic=False, start_refresh_seq=seq_empty)
                self.assertEqual(dashboard.running_version, '')
                self.assertEqual(dashboard.program_progress['current_version'], '')
                self.assertEqual(dashboard.program_progress['phase'], 'idle')
                self.assertNotIn('checked_at', dashboard.program_progress)
            finally:
                dashboard.running_version = saved_dash_running

            # Path 2: Resuming a session on a new running version (or legacy session without current_version,
            # or interrupted auto-check with version change) clears running=True and invalidates stale results.
            import socket
            for label, saved_prog, expected_msg in (
                ('changed-version', {
                    'running': False, 'phase': 'latest', 'available': False, 'error': '',
                    'current_version': 'v1.0.0-0000000', 'latest_version': 'v1.0.0-0000000',
                    'pending_version': '', 'checked_at': 12345.0, 'target': 'old-target',
                    'message': 'SP 已是最新版本',
                }, '从 GitHub 官方项目获取新版'),
                ('legacy-missing-version', {
                    'running': False, 'phase': 'latest', 'available': False, 'error': '',
                    'checked_at': 12345.0, 'target': 'old-target', 'message': 'SP 已是最新版本',
                }, '从 GitHub 官方项目获取新版'),
                ('interrupted-auto-check-version-changed', {
                    'running': True, 'phase': 'check', 'automatic': True, 'available': False, 'error': '',
                    'current_version': 'v1.0.0-0000000', 'message': '正在下载并检查新版…',
                }, '还没查到新版信息'),
            ):
                with self.subTest(resume_case=label):
                    listener = socket.socket()
                    listener.bind(('127.0.0.1', 0))
                    listener.listen()
                    self.addCleanup(listener.close)
                    resumed = server.Dashboard({'home': str(self.home)}, resume={
                        'inventory': {'home': str(self.home)},
                        'socket_fd': listener.dup().detach(),
                        'program_progress': dict(saved_prog),
                    })
                    self.addCleanup(resumed.server_close)
                    self.assertFalse(resumed.program_progress['running'])
                    self.assertEqual(resumed.program_progress['current_version'], deferred_next)
                    self.assertEqual(resumed.program_progress['phase'], 'idle')
                    self.assertEqual(resumed.program_progress['latest_version'], '')
                    self.assertEqual(resumed.program_progress['message'], expected_msg)
                    self.assertNotIn('checked_at', resumed.program_progress)
                    self.assertNotIn('target', resumed.program_progress)

    def test_attachment_preview_keeps_running_pending_version_readonly_and_old_hub(self):
        """A screenshot preview still reports the running and pending versions together.

        Read-only mode and an older share site keep their existing limits, and those
        limits do not drop the version line.
        """
        from tests.test_feedback import encode_shot, make_png

        saved_running = app_version.running_build()

        def restore_running():
            app_version.set_running_version(saved_running['version'], date=saved_running['date'])
            diagnostics.sync_running_build()

        self.addCleanup(restore_running)
        shot = encode_shot(make_png((12, 34, 56)), 'image/png')
        pending = 'v1.0.0-8888888'
        disk = {'install_mode': '源码', 'app_version': pending}
        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'}):
            dashboard = server.Dashboard({'home': str(self.home)})
            self.serve(dashboard)
            running = app_version.running_build()['version']
            dashboard.program_progress.update(
                phase='ready', message='新版已安装，刷新后生效',
                current_version=running, latest_version=pending, pending_version=pending)
            with patch.object(diagnostics, 'installed_build', return_value=disk):
                status, preview = self.post(dashboard, '/api/diagnostics/preview', {
                    'error_id': '', 'note': '带图核对', 'attachments': [shot]})
            self.assertEqual(status, 200, preview)
            self.assertEqual(preview['payload']['app_version'], running)
            self.assertEqual(preview['pending_app_version'], pending)
            self.assertIn(pending, preview['payload']['detail'])
            self.assertIn(running, preview['payload']['detail'])
            self.assertEqual(preview['payload']['attachment_count'], '1')
            self.assertEqual(len(preview['attachments']), 1)
            self.assertIn('截图', preview['payload']['attachments_summary'])
            self.assertFalse(preview['can_send'])

            team_path = self.home / '.skill-pilot/team.json'
            team_path.parent.mkdir(parents=True, exist_ok=True)
            team_path.write_text(json.dumps({'url': 'http://127.0.0.1:9', 'key': 'member-token'}), encoding='utf-8')
            old_hub = {'api': 2, 'service': 'skill-pilot-team',
                       'member': {'id': 'a' * 32, 'name': '示例同事'},
                       'feedback_supported': True, 'feedback_configured': True}
            with patch.object(team, '_request', return_value=old_hub), patch.object(
                    diagnostics, 'installed_build', return_value=disk):
                status, with_shot = self.post(dashboard, '/api/diagnostics/preview', {
                    'error_id': '', 'note': '', 'attachments': [shot]})
                self.assertEqual(status, 200, with_shot)
                self.assertFalse(with_shot['can_send'])
                self.assertFalse(with_shot['can_attach'])
                self.assertIn('移除截图', with_shot['reason'])
                self.assertEqual(with_shot['payload']['app_version'], running)
                self.assertEqual(with_shot['pending_app_version'], pending)
                self.assertEqual(with_shot['payload']['attachment_count'], '1')
                status, plain = self.post(dashboard, '/api/diagnostics/preview', {'error_id': '', 'note': ''})
                self.assertEqual(status, 200, plain)
                self.assertTrue(plain['can_send'])
                self.assertFalse(plain['can_attach'])
                self.assertIn('暂不支持随反馈上传截图', plain['attach_reason'])
                self.assertEqual(plain['payload']['app_version'], running)
                self.assertEqual(plain['pending_app_version'], pending)
                self.assertEqual(plain['payload']['attachment_count'], '0')

        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '1'}):
            dashboard.program_progress.update(
                phase='ready', current_version=running, latest_version=pending, pending_version=pending)
            with patch.object(diagnostics, 'installed_build', return_value=disk):
                status, readonly = self.post(dashboard, '/api/diagnostics/preview', {
                    'error_id': '', 'note': '', 'attachments': [shot]})
            self.assertEqual(status, 200, readonly)
            self.assertFalse(readonly['can_send'])
            self.assertFalse(readonly['can_attach'])
            self.assertIn('只读模式', readonly['reason'])
            self.assertIn('只读模式', readonly['attach_reason'])
            self.assertEqual(readonly['payload']['app_version'], running)
            self.assertEqual(readonly['pending_app_version'], pending)
            self.assertEqual(len(readonly['attachments']), 1)

    def test_windows_reload_callback_joining_auto_check_never_deadlocks(self):
        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'}):
            dashboard = server.Dashboard({'home': str(self.home)})
            self.addCleanup(dashboard.server_close)
            entered = threading.Event()
            release = threading.Event()
            finished = threading.Event()

            def slow_check(home):
                entered.set()
                release.wait(2)
                return {'temporary': '', 'target': 'a' * 40, 'current': '', 'latest': '',
                        'current_version': dashboard.running_version,
                        'latest_version': dashboard.running_version, 'available': False}

            def windows_like_restart():
                # Mirrors windows_runtime.py restart(): joins automatic check thread while reload_when_idle holds runtime_lock.
                release.set()
                if dashboard.program_thread and dashboard.program_thread.is_alive():
                    dashboard.program_thread.join(timeout=3)
                    self.assertFalse(dashboard.program_thread.is_alive(), '自动检查线程与刷新锁发生死锁')
                finished.set()

            dashboard.reload_callback = windows_like_restart
            with patch.object(self_update, 'check', side_effect=slow_check), \
                    patch.object(runtime, 'version', return_value='different-code-version'):
                dashboard.start_program('check', automatic=True)
                self.assertTrue(entered.wait(2))
                dashboard.request_reload()
                self.assertTrue(finished.wait(3), 'Windows 刷新等待自动检查退出时超时/死锁')
            self.assertFalse(dashboard.program_progress['running'])

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
        result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # Verify popup button switches to 「重新检查」 when can_retry_apply===false and empty current_version
        # displays 「暂未读到版本」 instead of fabricating a date version from progress.current.
        render_fns = '\n'.join(extract_function(source, sig) for sig in (
            'function programText(',
            'function programVersionLabel(',
            'function programChangesHtml(',
            'function programPopupPhase(',
            'function renderProgramPopup(',
            'function renderProgram(',
        ))
        ui_script = r'''
const assert = require('node:assert/strict');
const makeEl = () => ({
  hidden: false, disabled: false, inert: false, textContent: '', innerHTML: '',
  attrs: {}, classes: new Set(),
  classList: { remove(c) {}, add(c) {}, contains() { return false; } },
  setAttribute(k, v) { this.attrs[k] = v; },
  querySelector() { return this.span || (this.span = {textContent: ''}); },
});
const els = {};
const $ = id => els[id] || (els[id] = makeEl());
const escape = s => String(s || '');
const feedbackButton = () => '';
const appendFeedback = () => {};
const setTask = () => {};
const clearTask = () => {};
const positionProgramPopup = () => {};
const prefersReducedMotion = () => true;
const renderHomeTodos = () => {};
let homeVisible = false, writeBusy = false, programFailedAction = '', programActionRunning = false;
let programPopupHideTimer = null;
const programPopupDismissed = new Set();
let programUpdate = {};
''' + render_fns + r'''
// 1. Non-retryable apply failure (version_verified=false) shows 「重新检查」, not 「重试升级」,
//    and empty current_version with a current date shows 「暂未读到版本」 instead of v2026.11.19.
programUpdate = {
  running: false, phase: 'error', available: false, write_allowed: true,
  failed_action: 'apply', can_retry_apply: false, target: 'abc',
  current: '2026-11-19T00:00:00Z', current_version: '',
  latest: '2026-11-19T00:00:00Z', latest_version: 'v1.0.0-2222222',
  message: '新版已写入，但未能核实安装后的版本', error: '新版文件已写入，但未能核实安装后的版本，请刷新页面或重新检查。',
};
renderProgram();
assert.equal($('program-popup-action').textContent, '重新检查');
assert.ok($('program-version').textContent.includes('暂未读到版本'), $('program-version').textContent);
assert.ok(!$('program-version').textContent.includes('v2026.11.19'), $('program-version').textContent);
assert.ok($('program-popup-body').innerHTML.includes('暂未读到版本'), $('program-popup-body').innerHTML);
assert.ok(!$('program-popup-body').innerHTML.includes('v2026.11.19'), $('program-popup-body').innerHTML);

// 2. Retryable apply failure (can_retry_apply=true) still offers 「重试升级」.
programUpdate.can_retry_apply = true;
renderProgramPopup();
assert.equal($('program-popup-action').textContent, '重试升级');
'''
        ui_result = subprocess.run(['node', '-'], input=ui_script, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(ui_result.returncode, 0, ui_result.stdout + ui_result.stderr)

    def test_zip_official_file_verification_and_private_files(self):
        with patch.object(self_update, 'run_installer'):
            make_fixture(self, checkout=False)
        # Include WINDOWS.md in official upstream release as well so we can verify root official file checks.
        shutil.copy2(store.PROJECT / 'WINDOWS.md', self.upstream / 'WINDOWS.md')
        commit(self.upstream, 'Include WINDOWS.md in official release')
        shutil.copy2(store.PROJECT / 'WINDOWS.md', self.code / 'WINDOWS.md')
        head = git(self.upstream, 'rev-parse', 'HEAD')
        self_update.record_installation(self.code, head, '2026-11-19T00:00:00Z')
        complete_files = self_update.read_record(self.code)['files']
        official_version = app_version.read_build(self.code)['version']
        self.assertTrue(app_version.read_build(self.code)['manifest_valid'])

        # 1. User private files (in docs/ or root) must NOT invalidate manifest, pollute version, or trigger update.
        (self.code / 'docs/My private note.md').write_text('personal doc', encoding='utf-8')
        (self.code / 'My private note.md').write_text('personal root note', encoding='utf-8')
        with_private = app_version.read_build(self.code)
        self.assertTrue(with_private['manifest_valid'])
        self.assertEqual(with_private['version'], official_version)
        clean_check = self_update.check(self.home, self.code)
        self.assertFalse(clean_check['available'])
        self.assertEqual(clean_check['temporary'], '')
        self.assertEqual(clean_check['current_version'], official_version)
        self.assertEqual(clean_check['latest_version'], official_version)

        # 2. Official root file (WINDOWS.md) present on disk but omitted from manifest invalidates manifest
        #    and self_update.check() must NOT falsely report "已是最新".
        without_windows_md = {k: v for k, v in complete_files.items() if k != 'WINDOWS.md'}
        (self.code / '.skill-pilot-install.json').write_text(json.dumps({
            'files': without_windows_md, 'revision': head, 'date': '2026-11-19T00:00:00Z'}), encoding='utf-8')
        self.assertFalse(app_version.read_build(self.code)['manifest_valid'])
        missing_win_md = self_update.check(self.home, self.code)
        if missing_win_md['temporary']:
            self.addCleanup(shutil.rmtree, missing_win_md['temporary'], True)
        self.assertTrue(missing_win_md['available'])

        # 3. Deleting an official file (index.html or install-windows.ps1) from BOTH disk and manifest
        #    while revision == HEAD must NOT shortcut to "已是最新".
        for vanished in ('skill_pilot/web/static/index.html', 'scripts/install-windows.ps1'):
            with self.subTest(vanished=vanished):
                path = self.code / vanished
                saved = path.read_bytes()
                try:
                    path.unlink()
                    trimmed = {k: v for k, v in complete_files.items() if k != vanished}
                    (self.code / '.skill-pilot-install.json').write_text(json.dumps({
                        'files': trimmed, 'revision': head, 'date': '2026-11-19T00:00:00Z'}), encoding='utf-8')
                    checked = self_update.check(self.home, self.code)
                    if checked['temporary']:
                        self.addCleanup(shutil.rmtree, checked['temporary'], True)
                    self.assertTrue(checked['available'])
                    self.assertNotEqual(checked['current_version'], checked['latest_version'])
                finally:
                    path.write_bytes(saved)

        # 4. Re-running record_installation() with private files in root and docs/ must NEVER include them,
        #    change the version, or delete them on upgrade; and legacy manifests that recorded them remain safe.
        (self.code / '.skill-pilot-install.json').write_text(json.dumps({
            'files': complete_files, 'revision': head, 'date': '2026-11-19T00:00:00Z'}), encoding='utf-8')
        self_update.record_installation(self.code)
        rebuilt = self_update.read_record(self.code)
        self.assertNotIn('docs/My private note.md', rebuilt['files'])
        self.assertNotIn('My private note.md', rebuilt['files'])
        self.assertEqual(app_version.read_build(self.code)['version'], official_version)
        self.assertFalse(self_update.check(self.home, self.code)['available'])

        # New client upgrading from a legacy manifest that had accidentally recorded private notes
        # (including when those private paths were later replaced with a directory or symlink):
        # private entries are skipped during manifest reading, version calculation, check, and upgrade deletion.
        legacy_files = {
            **complete_files,
            'docs/My private note.md': self_update.stamp(self.code / 'docs/My private note.md'),
            'My private note.md': self_update.stamp(self.code / 'My private note.md'),
        }
        (self.code / '.skill-pilot-install.json').write_text(json.dumps({
            'files': legacy_files, 'revision': head, 'date': '2026-11-19T00:00:00Z'}), encoding='utf-8')
        legacy_build = app_version.read_build(self.code)
        self.assertTrue(legacy_build['manifest_valid'])
        self.assertEqual(legacy_build['version'], official_version)
        # Traversal, absolute, and Windows drive/root paths in a manifest must ALL be rejected even when non-official.
        bad_manifest = self.code / '.skill-pilot-install.json'
        for unsafe_path in ('../escape.md', '..\\escape.md', '/private.md', '\\private.md', 'D:private.md', 'D:\\private.md'):
            with self.subTest(unsafe_path=unsafe_path):
                bad_manifest.write_text(json.dumps({
                    'files': {**complete_files, unsafe_path: ['0' * 64, 0]}, 'revision': head}), encoding='utf-8')
                self.assertFalse(app_version.read_build(self.code)['manifest_valid'])
                with self.assertRaisesRegex(ValueError, '重新运行一次安装程序'):
                    self_update.read_record(self.code)
        bad_manifest.write_text(json.dumps({
            'files': legacy_files, 'revision': head, 'date': '2026-11-19T00:00:00Z'}), encoding='utf-8')

        # Replace recorded private notes with a directory and a symlink: check & upgrade by new client still succeed!
        (self.code / 'docs/My private note.md').unlink()
        (self.code / 'docs/My private note.md').mkdir()
        (self.code / 'docs/My private note.md/inner.txt').write_text('keep dir', encoding='utf-8')
        outside_target = self.root / 'outside-private-note.md'
        outside_target.write_text('outside target', encoding='utf-8')
        (self.code / 'My private note.md').unlink()
        (self.code / 'My private note.md').symlink_to(outside_target)
        self.assertEqual(app_version.read_build(self.code)['version'], official_version)
        self.assertTrue(app_version.read_build(self.code)['manifest_valid'])
        # Publish a new official release and upgrade: private directory & symlink survive untouched and drop from manifest.
        readme = self.upstream / 'README.md'
        readme.write_text(readme.read_text(encoding='utf-8') + '\nnext official release\n', encoding='utf-8')
        commit(self.upstream, 'Next official release')
        upgrade_review = self_update.check(self.home, self.code)
        if upgrade_review['temporary']:
            self.addCleanup(shutil.rmtree, upgrade_review['temporary'], True)
        self.assertTrue(upgrade_review['available'])
        with patch.object(self_update, 'run_installer'):
            self_update.apply(upgrade_review, self.home, self.code)
        self.assertTrue((self.code / 'docs/My private note.md').is_dir())
        self.assertEqual((self.code / 'docs/My private note.md/inner.txt').read_text(encoding='utf-8'), 'keep dir')
        self.assertTrue((self.code / 'My private note.md').is_symlink())
        self.assertEqual(outside_target.read_text(encoding='utf-8'), 'outside target')
        upgraded_record = self_update.read_record(self.code)
        self.assertNotIn('docs/My private note.md', upgraded_record['files'])
        self.assertNotIn('My private note.md', upgraded_record['files'])

        # 5. Unmanaged extra file in an official directory (e.g. skill_pilot/extra_local.py) causes apply()
        #    to report version_verified=False so server does not falsely enter a ready->mismatch loop.
        extra_code = self.code / 'skill_pilot/extra_local.py'
        extra_code.write_text('# unmanaged local file\n', encoding='utf-8')
        readme.write_text(readme.read_text(encoding='utf-8') + '\nanother official release\n', encoding='utf-8')
        commit(self.upstream, 'Another official release')
        unmanaged_review = self_update.check(self.home, self.code)
        if unmanaged_review['temporary']:
            self.addCleanup(shutil.rmtree, unmanaged_review['temporary'], True)
        self.assertTrue(unmanaged_review['available'])
        with patch.object(self_update, 'run_installer'):
            applied_unmanaged = self_update.apply(unmanaged_review, self.home, self.code)
        self.assertFalse(applied_unmanaged['version_verified'])
        self.assertTrue(applied_unmanaged['has_unmanaged_files'])
        self.assertNotEqual(applied_unmanaged['installed_version'], applied_unmanaged['pending_version'])
        extra_code.unlink()

        # 5b. Post-write read_build() exception (both RuntimeError and plain ValueError) inside apply()
        #     must be caught, record sanitized diagnostics with error_id, and return version_verified=False.
        for label, exc in (
            ('runtime-error', RuntimeError('transient read fault')),
            ('plain-value-error', ValueError('unexpected verification value')),
        ):
            with self.subTest(post_write_exc=label):
                readme.write_text(readme.read_text(encoding='utf-8') + f'\npost-write {label}\n', encoding='utf-8')
                commit(self.upstream, f'Post-write {label}')
                post_err_review = self_update.check(self.home, self.code)
                if post_err_review['temporary']:
                    self.addCleanup(shutil.rmtree, post_err_review['temporary'], True)
                self.assertTrue(post_err_review['available'])
                with patch.object(self_update, 'run_installer'), \
                        patch.object(self_update, 'read_build', side_effect=exc):
                    applied_post_err = self_update.apply(post_err_review, self.home, self.code)
                self.assertFalse(applied_post_err['version_verified'])
                self.assertFalse(applied_post_err['has_unmanaged_files'])
                self.assertEqual(applied_post_err['installed_version'], '')
                self.assertTrue(applied_post_err.get('error_id'))
                recent_rows = diagnostics.recent(self.home)
                matched_diag = next((row for row in recent_rows if row.get('error_id') == applied_post_err['error_id']), None)
                self.assertIsNotNone(matched_diag)
                self.assertEqual(matched_diag['environment']['app_version'], diagnostics.RUNNING_BUILD['app_version'])

        # 6. Checkout fast-return computes latest_version independently from git commit tree `target`
        #    (ignoring local `git replace` blob & commit objects and --skip-worktree edits), and detects mid-check
        #    modifications made during ls-remote OR during read_git_tree_version.
        with patch.object(self_update, 'run_installer'):
            make_fixture(self, checkout=True)
        head_rev = git(self.code, 'rev-parse', 'HEAD')
        official_checkout_version = app_version.read_build(self.code)['version']
        orig_blob = git(self.code, 'rev-parse', 'HEAD:README.md')
        fake_blob = subprocess.run(
            ['git', '-C', str(self.code), 'hash-object', '-w', '--stdin'],
            input='replaced official readme\n', capture_output=True, text=True, encoding='utf-8', check=True,
        ).stdout.strip()
        git(self.code, 'replace', orig_blob, fake_blob)
        try:
            self.assertEqual(app_version.read_git_tree_version(self.code, head_rev), official_checkout_version)
            replace_check = self_update.check(self.home, self.code)
            self.assertEqual(replace_check['latest_version'], official_checkout_version)
            self.assertEqual(replace_check['current_version'], official_checkout_version)
        finally:
            git(self.code, 'replace', '-d', orig_blob)

        # Also replace the commit object itself to lock ls-tree --no-replace-objects.
        empty_tree = subprocess.run(
            ['git', '-C', str(self.code), 'mktree'],
            input='', capture_output=True, text=True, encoding='utf-8', check=True,
        ).stdout.strip()
        fake_commit = subprocess.run(
            ['git', '-C', str(self.code), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
             'commit-tree', empty_tree, '-m', 'fake replaced commit'],
            capture_output=True, text=True, encoding='utf-8', check=True,
        ).stdout.strip()
        git(self.code, 'replace', head_rev, fake_commit)
        try:
            self.assertEqual(app_version.read_git_tree_version(self.code, head_rev), official_checkout_version)
        finally:
            git(self.code, 'replace', '-d', head_rev)

        git(self.code, 'update-index', '--skip-worktree', 'README.md')
        readme_path = self.code / 'README.md'
        # Keep the original bytes. Text-mode write_text on Windows rewrites LF as
        # CRLF, which is a real local change and must not leak into the next check.
        original_readme = readme_path.read_bytes()
        try:
            readme_path.write_bytes(original_readme + b'\nskip-worktree local edit\n')
            self.assertEqual(git(self.code, 'status', '--porcelain', '--untracked-files=normal'), '')
            skip_check = self_update.check(self.home, self.code)
            self.assertEqual(skip_check['latest_version'], official_checkout_version)
            self.assertNotEqual(skip_check['current_version'], skip_check['latest_version'])
        finally:
            readme_path.write_bytes(original_readme)
            git(self.code, 'update-index', '--no-skip-worktree', 'README.md')
        self.assertEqual(git(self.code, 'status', '--porcelain', '--untracked-files=normal'), '')

        original_read_tree = self_update.read_git_tree_version

        def mutate_during_read_tree(project, revision):
            version = original_read_tree(project, revision)
            readme_path.write_bytes(original_readme + b'\nlocal edit during tree read\n')
            return version

        try:
            with patch.object(self_update, 'read_git_tree_version', side_effect=mutate_during_read_tree):
                with self.assertRaisesRegex(ValueError, '检查期间发生了改动'):
                    self_update.check(self.home, self.code)
        finally:
            readme_path.write_bytes(original_readme)

        original_git = self_update.git

        def mutate_during_ls_remote(project, *args, **kwargs):
            output = original_git(project, *args, **kwargs)
            if args and args[0] == 'ls-remote':
                readme_path.write_bytes(readme_path.read_bytes() + b'\nlocal edit mid-check\n')
            return output

        try:
            with patch.object(self_update, 'git', side_effect=mutate_during_ls_remote):
                with self.assertRaisesRegex(ValueError, '检查期间发生了改动'):
                    self_update.check(self.home, self.code)
        finally:
            readme_path.write_bytes(original_readme)

    def test_crlf_residue_is_preexisting_and_tree_read_edit_is_mid_check(self):
        """A Windows text-mode round trip leaves CRLF in an LF checkout.

        That residue is already on disk when check() starts, so it is a local
        change. After the original bytes are put back, an edit made only while
        reading the official tree is a change during the check.
        """
        with patch.object(self_update, 'run_installer'):
            make_fixture(self, checkout=True)
        readme_path = self.code / 'README.md'
        original = readme_path.read_bytes()
        self.assertNotIn(b'\r\n', original)
        self.assertEqual(git(self.code, 'status', '--porcelain', '--untracked-files=normal'), '')

        text = readme_path.read_text(encoding='utf-8')
        readme_path.write_text(text, encoding='utf-8', newline='\r\n')
        self.assertNotEqual(readme_path.read_bytes(), original)
        self.assertIn(b'\r\n', readme_path.read_bytes())
        with self.assertRaisesRegex(ValueError, '有本地改动') as preexisting:
            self_update.check(self.home, self.code)
        self.assertNotIn('检查期间发生了改动', str(preexisting.exception))

        readme_path.write_bytes(original)
        self.assertEqual(git(self.code, 'status', '--porcelain', '--untracked-files=normal'), '')

        def mutate_during_read_tree(project, revision):
            version = app_version.read_git_tree_version(project, revision)
            readme_path.write_bytes(original + b'\nlocal edit during tree read\n')
            return version

        try:
            with patch.object(self_update, 'read_git_tree_version', side_effect=mutate_during_read_tree):
                with self.assertRaisesRegex(ValueError, '检查期间发生了改动') as during:
                    self_update.check(self.home, self.code)
            self.assertNotIn('有本地改动，已保留当前版本', str(during.exception))
        finally:
            readme_path.write_bytes(original)

    def test_program_work_run_id_atomic_dict_and_snapshot_isolation(self):
        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'}):
            dashboard = server.Dashboard({'home': str(self.home)})
            self.addCleanup(dashboard.server_close)
            old_started = threading.Event()
            release_old = threading.Event()

            def old_check(home):
                old_started.set()
                release_old.wait(2)
                return {'temporary': '', 'target': 'a' * 40, 'current': '', 'latest': '',
                        'current_version': dashboard.running_version,
                        'latest_version': dashboard.running_version, 'available': False}

            # 1. Older thread (run_id=1) finishing after newer task (run_id=2) never clobbers run_id=2.
            dashboard.program_run_id = 1
            old_thread = threading.Thread(
                target=lambda: self._run_with_check(dashboard, old_check, run_id=1), daemon=True)
            old_thread.start()
            self.assertTrue(old_started.wait(2))
            newer_review = {'temporary': '', 'target': 'b' * 40, 'available': True}
            with dashboard.program_lock:
                dashboard.program_run_id = 2
                dashboard.program_action = 'apply'
                dashboard.program_review = newer_review
                dashboard._set_program_progress({'running': True, 'phase': 'apply', 'message': '正在安装 SP 新版…'})
            release_old.set()
            old_thread.join(3)
            self.assertFalse(old_thread.is_alive())
            self.assertTrue(dashboard.program_progress['running'])
            self.assertEqual(dashboard.program_action, 'apply')
            self.assertEqual(dashboard.program_progress['phase'], 'apply')
            self.assertIs(dashboard.program_review, newer_review)

            # 2. Stale thread entering program_work when run_id has already moved on must NOT steal program_review.
            with patch.object(self_update, 'check', side_effect=AssertionError('stale run_id不应执行')):
                dashboard.program_work('check', automatic=False, run_id=1, start_refresh_seq=0)
            self.assertIs(dashboard.program_review, newer_review)
            self.assertTrue(dashboard.program_progress['running'])
            self.assertEqual(dashboard.program_action, 'apply')

            # 3. Even if an in-flight old thread exits via BaseException after run_id advances, finally preserves new task.
            class _Abort(BaseException):
                pass
            def abort_after_supersede(home):
                with dashboard.program_lock:
                    dashboard.program_run_id = 4
                    dashboard.program_action = 'apply'
                    dashboard._set_program_progress({'running': True, 'phase': 'apply'})
                raise _Abort()
            dashboard.program_run_id = 3
            with patch.object(self_update, 'check', side_effect=abort_after_supersede):
                with self.assertRaises(_Abort):
                    dashboard.program_work('check', automatic=False, run_id=3, start_refresh_seq=0)
            self.assertTrue(dashboard.program_progress['running'])
            self.assertEqual(dashboard.program_action, 'apply')

            # 4. Atomic dict replacement: completing a check publishes a new dict without mutating earlier snapshots.
            with dashboard.program_lock:
                dashboard.program_action = ''
                dashboard._set_program_progress({'running': False, 'phase': 'idle'})
            before_ref = dashboard.program_progress
            snap_before = runtime.snapshot(dashboard)
            with patch.object(self_update, 'check', return_value={
                    'temporary': '', 'target': 'c' * 40, 'current': '', 'latest': '',
                    'current_version': dashboard.running_version,
                    'latest_version': dashboard.running_version, 'available': False}):
                dashboard.program_work('check', automatic=False)
            self.assertIsNot(dashboard.program_progress, before_ref)
            self.assertEqual(before_ref['phase'], 'idle')
            self.assertEqual(dashboard.program_progress['phase'], 'latest')
            snap_after = runtime.snapshot(dashboard)
            snap_after['program_progress']['phase'] = 'mutated'
            self.assertEqual(dashboard.program_progress['phase'], 'latest')
            self.assertEqual(snap_before['program_progress']['phase'], 'idle')

            # 5. Immediate idle static refresh invalidates stale 'latest' if read_build fails or returns empty version.
            running = dashboard.running_version
            for label, patch_kwargs in (
                ('exception', {'side_effect': OSError('disk unreadable')}),
                ('empty-version', {'return_value': {'install_mode': '压缩包', 'checkout': False,
                                                    'revision': '', 'date': '', 'manifest_valid': False, 'version': ''}}),
            ):
                with self.subTest(idle_refresh=label):
                    with dashboard.program_lock:
                        dashboard._set_program_progress({
                            'running': False, 'phase': 'latest', 'available': False,
                            'latest': '2026-11-19T00:00:00Z',
                            'current_version': running, 'latest_version': running,
                            'checked_at': 12345.0, 'target': 'old-target', 'message': 'SP 已是最新版本',
                        })
                    with patch.object(runtime, 'version', return_value=dashboard.code_version), \
                            patch.object(app_version, 'read_build', **patch_kwargs):
                        dashboard.request_reload()
                    self.assertEqual(dashboard.running_version, running)
                    self.assertEqual(dashboard.program_progress['current_version'], running)
                    self.assertEqual(dashboard.program_progress['phase'], 'idle')
                    self.assertEqual(dashboard.program_progress['latest'], '')
                    self.assertEqual(dashboard.program_progress['latest_version'], '')
                    self.assertNotIn('checked_at', dashboard.program_progress)
                    self.assertNotIn('target', dashboard.program_progress)

    def _run_with_check(self, dashboard, check_fn, run_id):
        with patch.object(self_update, 'check', side_effect=check_fn):
            dashboard.program_work('check', automatic=False, run_id=run_id, start_refresh_seq=0)

    def test_resume_cleans_stale_state_before_bundled_skill_sync_and_invalidates_unknown_version(self):
        import socket
        from skill_pilot import operations

        # 1. Resume cleanup runs BEFORE sync_bundled_skill so a fresh startup sync error is never overwritten.
        for label, saved_prog in (
            ('version-changed', {
                'running': False, 'phase': 'latest', 'available': False, 'error': '',
                'current_version': 'v1.0.0-0000000', 'latest_version': 'v1.0.0-0000000',
                'pending_version': '', 'checked_at': 12345.0, 'target': 'old-target',
                'message': 'SP 已是最新版本',
            }),
            ('interrupted-running', {
                'running': True, 'phase': 'check', 'automatic': False, 'available': False, 'error': '',
                'current_version': 'v1.0.0-0000000', 'checked_at': 12345.0, 'target': 'old-target',
                'message': '正在下载并检查新版…',
            }),
        ):
            with self.subTest(sync_error_case=label):
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                self.addCleanup(listener.close)
                with patch.object(operations, 'sync_bundled_skill', side_effect=RuntimeError('sync boom')):
                    resumed = server.Dashboard({'home': str(self.home)}, resume={
                        'inventory': {'home': str(self.home)},
                        'socket_fd': listener.dup().detach(),
                        'program_progress': dict(saved_prog),
                    })
                self.addCleanup(resumed.server_close)
                self.assertFalse(resumed.program_progress['running'])
                self.assertEqual(resumed.program_progress['phase'], 'error')
                self.assertIn('自带技能说明没能同步', resumed.program_progress['error'])
                self.assertEqual(resumed.program_progress['latest_version'], '')
                self.assertNotIn('checked_at', resumed.program_progress)
                self.assertNotIn('target', resumed.program_progress)

        # 2. When new process running_version is empty/unreadable on resume, old 'latest' must be withdrawn.
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        self.addCleanup(listener.close)
        with patch.object(app_version, 'running_build', return_value={
                'install_mode': '压缩包', 'checkout': False, 'revision': '', 'date': '',
                'manifest_valid': False, 'version': ''}):
            resumed = server.Dashboard({'home': str(self.home)}, resume={
                'inventory': {'home': str(self.home)},
                'socket_fd': listener.dup().detach(),
                'program_progress': {
                    'running': False, 'phase': 'latest', 'available': False, 'error': '',
                    'current_version': 'v1.0.0-1111111', 'latest_version': 'v1.0.0-1111111',
                    'pending_version': '', 'checked_at': 12345.0, 'target': 'old-target',
                    'message': 'SP 已是最新版本',
                },
            })
        self.addCleanup(resumed.server_close)
        self.assertEqual(resumed.running_version, '')
        self.assertEqual(resumed.program_progress['phase'], 'idle')
        self.assertEqual(resumed.program_progress['current_version'], '')
        self.assertEqual(resumed.program_progress['latest_version'], '')
        self.assertNotIn('checked_at', resumed.program_progress)
        self.assertNotIn('target', resumed.program_progress)

        # 3. Resuming a 'ready' session only confirms 'latest' when BOTH running_version and pending_version
        #    are non-empty and equal; missing pending_version, empty running_version, or mismatch withdraws stale state.
        for label, actual_version, saved_pending, expect_phase, expect_current in (
            ('ready-unknown-version', '', 'v1.0.0-2222222', 'idle', ''),
            ('ready-missing-pending-version', 'v1.0.0-2222222', '', 'idle', 'v1.0.0-2222222'),
            ('ready-mismatched-version', 'v1.0.0-3333333', 'v1.0.0-2222222', 'idle', 'v1.0.0-3333333'),
            ('ready-matched-version', 'v1.0.0-2222222', 'v1.0.0-2222222', 'latest', 'v1.0.0-2222222'),
        ):
            with self.subTest(ready_resume=label):
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                self.addCleanup(listener.close)
                with patch.object(app_version, 'running_build', return_value={
                        'install_mode': '压缩包', 'checkout': False, 'revision': '', 'date': '',
                        'manifest_valid': bool(actual_version), 'version': actual_version}):
                    resumed_ready = server.Dashboard({'home': str(self.home)}, resume={
                        'inventory': {'home': str(self.home)},
                        'socket_fd': listener.dup().detach(),
                        'program_progress': {
                            'running': False, 'phase': 'ready', 'available': False, 'error': '',
                            'latest': '2026-11-19T00:00:00Z',
                            'current_version': 'v1.0.0-1111111',
                            'latest_version': saved_pending,
                            'pending_version': saved_pending,
                            'checked_at': 12345.0, 'target': 'target-sha',
                            'message': '新版已安装，刷新后生效',
                        },
                    })
                self.addCleanup(resumed_ready.server_close)
                self.assertEqual(resumed_ready.program_progress['phase'], expect_phase)
                self.assertEqual(resumed_ready.program_progress['current_version'], expect_current)
                self.assertEqual(resumed_ready.program_progress['pending_version'], '')
                if expect_phase == 'idle':
                    self.assertEqual(resumed_ready.program_progress['latest'], '')
                    self.assertEqual(resumed_ready.program_progress['latest_version'], '')
                    self.assertNotIn('checked_at', resumed_ready.program_progress)
                    self.assertNotIn('target', resumed_ready.program_progress)
                else:
                    self.assertEqual(resumed_ready.program_progress['latest_version'], 'v1.0.0-2222222')
