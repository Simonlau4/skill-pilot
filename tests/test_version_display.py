"""Cross-platform readable version display across checkout, ZIP, upgrades and diagnostics."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from skill_pilot import diagnostics, feedback, self_update, store, version as app_version
from skill_pilot.sources.github import git
from skill_pilot.web import runtime, server
from tests import set_up_home
from tests.test_self_update import commit, make_fixture


class VersionFormatAndReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot version ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / '模拟 user')
        self.home.mkdir(parents=True)

    def copy_minimal_program(self, destination):
        shutil.copytree(store.PROJECT / 'skill_pilot', destination / 'skill_pilot',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(store.PROJECT / 'skill', destination / 'skill',
                        ignore=shutil.ignore_patterns('__pycache__'))
        return destination

    def test_format_version_normalizes_timezones_and_handles_all_fallbacks(self):
        rev = 'AbCdEf1234567890' * 2 + '01234567'
        # Same instant in -08:00 and +08:00 produces the exact same UTC date version.
        self.assertEqual(app_version.format_version('2026-11-17T23:14:09-08:00', rev), 'v2026.11.18-abcdef1')
        self.assertEqual(app_version.format_version('2026-11-18T15:14:09+08:00', rev), 'v2026.11.18-abcdef1')
        self.assertEqual(app_version.format_version('2026-11-19', rev), 'v2026.11.19-abcdef1')
        self.assertEqual(app_version.format_version('2026-11-19T00:00:00Z', ''), 'v2026.11.19')
        self.assertEqual(app_version.format_version('', rev), 'abcdef1')
        self.assertEqual(app_version.format_version('', '', '0123456789abcdef' * 4), '初始版-0123456')
        self.assertEqual(app_version.format_version('', '', ''), '')

    def test_checkout_and_upgraded_zip_produce_identical_readable_version(self):
        repo = self.copy_minimal_program(self.root / 'Source Checkout With Spaces')
        git(repo, 'init', '-q', '-b', 'main')
        commit(repo, 'Initial release')
        rev = git(repo, 'rev-parse', 'HEAD')
        date = git(repo, 'show', '-s', '--format=%cI', 'HEAD')
        expected = app_version.format_version(date, rev)

        checkout_build = app_version.read_build(repo)
        self.assertEqual(checkout_build['install_mode'], '源码')
        self.assertEqual(checkout_build['version'], expected)
        self.assertRegex(expected, r'^v\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7}$')

        zip_copy = self.root / 'ZIP Edition With Spaces'
        shutil.copytree(repo, zip_copy, ignore=shutil.ignore_patterns('.git'))
        (zip_copy / '.skill-pilot-install.json').write_text(
            json.dumps({'files': {'skill_pilot/__init__.py': ['0' * 64, 0]}, 'revision': rev, 'date': date}))
        with patch.object(app_version.subprocess, 'run', side_effect=AssertionError('ZIP不应调用git')):
            zip_build = app_version.read_build(zip_copy)
        self.assertEqual(zip_build['install_mode'], '压缩包')
        self.assertEqual(zip_build['version'], expected)

    def test_fresh_zip_never_invokes_git_and_uses_deterministic_code_fingerprint(self):
        zip_a = self.copy_minimal_program(self.root / 'Fresh ZIP One')
        zip_b = self.copy_minimal_program(self.root / 'Fresh ZIP Two')
        (zip_a / '.skill-pilot-install.json').write_text(json.dumps({'files': {}, 'revision': '', 'date': ''}))
        with patch.object(app_version.subprocess, 'run', side_effect=AssertionError('ZIP不应调用git')):
            build_a = app_version.read_build(zip_a)
            build_b = app_version.read_build(zip_b)
        self.assertEqual(build_a['install_mode'], '压缩包')
        self.assertRegex(build_a['version'], r'^初始版-[0-9a-f]{7}$')
        self.assertEqual(build_a['version'], build_b['version'])

    def test_checkout_falls_back_gracefully_when_git_is_missing(self):
        repo = self.copy_minimal_program(self.root / 'Checkout Without Git')
        (repo / '.git').mkdir()
        (repo / '.skill-pilot-install.json').write_text(
            json.dumps({'files': {}, 'revision': 'c' * 40, 'date': '2026-11-19T00:00:00Z'}))
        with patch.object(app_version.subprocess, 'run', side_effect=FileNotFoundError('git missing')):
            build = app_version.read_build(repo)
        self.assertEqual(build['install_mode'], '源码')
        self.assertEqual(build['version'], 'v2026.11.19-ccccccc')

        (repo / '.skill-pilot-install.json').unlink()
        with patch.object(app_version.subprocess, 'run', side_effect=FileNotFoundError('git missing')):
            fallback = app_version.read_build(repo)
        self.assertEqual(fallback['install_mode'], '源码')
        self.assertRegex(fallback['version'], r'^初始版-[0-9a-f]{7}$')

    def test_self_update_check_returns_versions_for_checkout_and_zip(self):
        for checkout in (True, False):
            with self.subTest(checkout=checkout):
                with patch.object(self_update, 'run_installer'):
                    make_fixture(self, checkout=checkout)
                self_update.record_installation(self.code)
                up_to_date = self_update.check(self.home, self.code)
                if up_to_date['temporary']:
                    self.addCleanup(shutil.rmtree, up_to_date['temporary'], True)
                self.assertFalse(up_to_date['available'])
                self.assertRegex(up_to_date['latest_version'], r'^v\d{4}\.\d{2}\.\d{2}-[0-9a-f]{7}$')
                if checkout:
                    self.assertEqual(up_to_date['current_version'], up_to_date['latest_version'])
                else:
                    self.assertRegex(up_to_date['current_version'], r'^初始版-[0-9a-f]{7}$')

                (self.upstream / 'README.md').write_text((self.upstream / 'README.md').read_text() + '\nnext\n')
                commit(self.upstream, 'Publish next edition')
                target = git(self.upstream, 'rev-parse', 'HEAD')
                available = self_update.check(self.home, self.code)
                if available['temporary']:
                    self.addCleanup(shutil.rmtree, available['temporary'], True)
                self.assertTrue(available['available'])
                self.assertEqual(available['current_version'], up_to_date['current_version'])
                self.assertRegex(available['latest_version'], r'^v\d{4}\.\d{2}\.\d{2}-' + target[:7] + '$')
                self.assertNotEqual(available['current_version'], available['latest_version'])

    def test_dashboard_and_diagnostics_share_running_version_and_static_refresh_updates_it(self):
        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'}):
            dashboard = server.Dashboard({'home': str(self.home)})
            self.addCleanup(dashboard.server_close)
            running = app_version.running_build()['version']
            self.assertTrue(running)
            self.assertEqual(dashboard.program_progress['current_version'], running)
            self.assertEqual(diagnostics.RUNNING_BUILD['app_version'], running)

            row = diagnostics.record(self.home, 'backend', '检查状态', RuntimeError('boom'))
            report = diagnostics.bundle(self.home, row['error_id'])['report']
            payload = feedback.envelope(report)
            self.assertEqual(row['environment']['app_version'], running)
            self.assertEqual(payload['app_version'], running)

            # Simulate an upgrade that finished on disk while waiting for browser refresh.
            dashboard.program_progress.update(
                phase='ready',
                current_version=running,
                latest_version='v2026.11.20-9999999',
                pending_version='v2026.11.20-9999999',
            )
            with patch.object(runtime, 'version', return_value=dashboard.code_version):
                refreshed = dashboard.request_reload()
            self.assertFalse(refreshed['changed'])
            self.assertEqual(dashboard.program_progress['phase'], 'latest')
            self.assertEqual(dashboard.program_progress['current_version'], 'v2026.11.20-9999999')
            self.assertEqual(dashboard.program_progress['pending_version'], '')
