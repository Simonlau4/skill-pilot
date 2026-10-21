"""Independent updates, paired Lark updates, and the real npm config loader."""
import contextlib
import io
from skill_pilot.platform_support import rmtree
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import cli, operations, store, updates
from skill_pilot.sources import github, lark
from skill_pilot.web.server import Dashboard
from tests import set_up_home
from tests import test_updates


class UpdateBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_updates.UpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home

    def add_skill(self, name):
        folder = self.fixture.repo / name
        folder.mkdir()
        manifest = folder / 'SKILL.md'
        manifest.write_text(f'---\nname: {name}\ndescription: example\n---\nold\n')
        self.fixture.commit('add ' + name)
        review = github.inspect(self.fixture.repo.as_uri())
        try:
            operations.install(review, [name], ['codex'], home=self.home)
        finally:
            rmtree(review['temporary'])
        manifest.write_text(manifest.read_text().replace('old', 'new'))
        self.fixture.commit('update ' + name)

    def review(self, lark_names=()):
        review = self.fixture.check()
        if lark_names:
            review['lark'] = {'latest_version': '1.0.98'}
            review['_groups']['lark'] = [(name, {}, {}) for name in lark_names]
            for row in review['skills']:
                if row['name'] in lark_names:
                    row['source'] = {'kind': 'lark'}
                    row['package'] = {'digest': 'fixture'}
        return review

    def fingerprint(self):
        registry = operations.load(self.home)[1]
        paths = [*lark.CLI_PATHS, *('.skill-pilot/store/' + name for name in registry['skills'])]
        return self.fixture.fingerprint() | {p: store.physical_state(self.home / p) for p in paths}

    def undo(self, result):
        for operation in reversed(result['operations']):
            operations.undo(operation['id'], home=self.home)

    def request(self, server, path, body=None):
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            server.origin + path, data=json.dumps(body).encode() if body is not None else None,
            headers={'X-Skill-Pilot-Token': server.token, 'Origin': server.origin,
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_undoing_a_batch_brings_each_update_mark_back(self):
        self.add_skill('zlast')
        server = Dashboard({'home': str(self.home)})
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05})
        thread.start()

        def close():
            server.shutdown()
            thread.join()
            server.server_close()
            for review in server.update_reviews.values():
                rmtree(review['temporary'], ignore_errors=True)

        self.addCleanup(close)
        self.request(server, '/api/updates/check', {})
        deadline = time.monotonic() + 10
        review = None
        while time.monotonic() < deadline:
            _, review = self.request(server, '/api/updates/progress')
            if review and not review['running']:
                break
            time.sleep(.01)
        self.assertTrue(review and not review['running'])
        names = [row['name'] for row in review['skills'] if row['has_update']]
        self.assertIn('sample', names)
        self.assertIn('zlast', names)
        status, result = self.request(server, '/api/updates/apply', {
            'review_id': review['id'], 'skills': ['sample', 'zlast'], 'confirmed': True})
        self.assertEqual(status, 200, result)
        _, progress = self.request(server, '/api/updates/progress')
        marks = {row['name']: row['has_update'] for row in progress['skills']}
        self.assertFalse(marks['sample'])
        self.assertFalse(marks['zlast'])
        for operation in reversed(result['operations']):
            _, table = self.request(server, '/api/table')
            status, undone = self.request(server, '/api/undo', {
                'id': operation['id'], 'revision': table['revision'], 'confirmed': True})
            self.assertEqual(status, 200, undone)
        _, progress = self.request(server, '/api/updates/progress')
        marks = {row['name']: row['has_update'] for row in progress['skills']}
        self.assertTrue(marks['sample'])
        self.assertTrue(marks['zlast'])

    def test_description_watch_ignores_other_files_and_leaves_other_skills_alone(self):
        updates.apply(self.review(), ['sample'], confirmed=True)
        (self.fixture.folder / 'digest.json').write_text('{"day": 1}\n')
        self.fixture.commit('daily data')
        self.add_skill('other')
        noisy = {row['name']: row for row in self.review()['skills']}
        self.assertTrue(noisy['sample']['has_update'])
        self.assertTrue(noisy['other']['has_update'])
        saved = updates.set_watch('sample', 'description', home=self.home, confirmed=True)
        quiet = {row['name']: row for row in self.review()['skills']}
        self.assertFalse(quiet['sample']['has_update'])
        self.assertEqual(quiet['sample']['status'], '说明没变')
        self.assertTrue(quiet['other']['has_update'])
        self.fixture.manifest.write_text(self.fixture.manifest.read_text().replace('new upstream', 'newer note'))
        self.fixture.commit('skill text')
        noted = {row['name']: row for row in self.review()['skills']}
        self.assertTrue(noted['sample']['has_update'])
        self.assertTrue(noted['other']['has_update'])
        updates.set_watch('other', 'off', home=self.home, confirmed=True)
        muted = {row['name']: row for row in self.review()['skills']}
        self.assertFalse(muted['other']['has_update'])
        self.assertEqual(muted['other']['status'], '不提醒')
        self.assertTrue(muted['sample']['has_update'])
        operations.undo(saved['id'], home=self.home)
        self.assertIsNone(operations.load(self.home)[1]['skills']['sample'].get('update_watch'))

    @unittest.skipIf(os.name == "nt", "POSIX CLI replacement fixture; Windows rejects CLI installation before mutation")
    def test_lark_install_failure_preserves_other_updates_and_exact_undo(self):
        for name in ['lark-doc', 'lark-wiki', 'zlast']:
            self.add_skill(name)
        package = self.home / lark.CLI_PATHS[0]
        package.mkdir(parents=True)
        (package / 'old').write_text('old tool')
        entry = self.home / lark.CLI_PATHS[1]
        entry.parent.mkdir(parents=True)
        entry.symlink_to('../lib/node_modules/@larksuite/cli/old')
        before = self.fingerprint()
        old_lark = {name: operations.load(self.home)[1]['skills'][name] for name in ['lark-doc', 'lark-wiki']}
        review = self.review(old_lark)
        def failed_install(*args):
            (package / 'old').write_text('incomplete replacement')
            raise ValueError('飞书工具下载失败。这组技能保持原样。')
        with patch.object(lark, 'install_cli', side_effect=failed_install) as install:
            result = updates.apply(review, ['sample', 'lark-doc', 'lark-wiki', 'zlast'], confirmed=True, confirm_lark=True)
        install.assert_called_once()
        self.assertEqual(result['updated'], ['sample', 'zlast'])
        self.assertEqual([r['name'] for r in result['failed']], ['lark-doc', 'lark-wiki'])
        self.assertFalse(result['complete'])
        self.assertIn('已更新 2 个', result['message'])
        self.assertEqual(review['update_count'], 2)
        for name, old in old_lark.items():
            self.assertEqual(operations.load(self.home)[1]['skills'][name], old)
            row = next(r for r in review['skills'] if r['name'] == name)
            self.assertTrue(row['has_update'])
            self.assertIn('下载失败', row['update_error'])
        for path in lark.CLI_PATHS:
            self.assertEqual(before[path], store.physical_state(self.home / path))
        self.assertIn('new upstream', (self.fixture.local() / 'SKILL.md').read_text())
        self.assertIn('new', (self.home / '.skill-pilot/store/zlast/SKILL.md').read_text())
        self.undo(result)
        self.assertEqual(before, self.fingerprint())

    def test_middle_write_failure_rolls_back_only_that_skill_and_can_retry(self):
        for name in ['zbad', 'zzlast']:
            self.add_skill(name)
        before = self.fingerprint()
        review = self.review()
        original = updates.normalization.apply
        def fail_after_replacement(path):
            if Path(path) == self.home / '.skill-pilot/store/zbad':
                (Path(path) / 'SKILL.md').write_text('partial replacement')
                raise OSError('write failed')
            return original(path)
        with patch.object(updates.normalization, 'apply', side_effect=fail_after_replacement):
            result = updates.apply(review, ['sample', 'zbad', 'zzlast'], confirmed=True)
        self.assertEqual(result['updated'], ['sample', 'zzlast'])
        self.assertEqual([r['name'] for r in result['failed']], ['zbad'])
        self.assertEqual(before['.skill-pilot/store/zbad'], store.physical_state(self.home / '.skill-pilot/store/zbad'))
        retried = updates.apply(review, ['zbad'], confirmed=True)
        self.assertTrue(retried['complete'])
        row = next(r for r in review['skills'] if r['name'] == 'zbad')
        self.assertNotIn('update_error', row)
        self.assertFalse(row['has_update'])
        self.undo(retried)
        self.undo(result)
        self.assertEqual(before, self.fingerprint())

    def test_lark_confirmation_is_checked_before_any_other_update(self):
        for name in ['lark-doc', 'lark-wiki']:
            self.add_skill(name)
        review = self.review(['lark-doc', 'lark-wiki'])
        before = self.fingerprint()
        for names, confirmed in [(['sample', 'lark-doc', 'lark-wiki'], False), (['sample', 'lark-doc'], True)]:
            with self.assertRaisesRegex(ValueError, '单独确认'):
                updates.apply(review, names, confirmed=True, confirm_lark=confirmed)
        self.assertEqual(before, self.fingerprint())

    def test_all_failed_is_not_reported_as_success_and_does_not_create_operations(self):
        self.add_skill('zbad')
        review = self.review()
        for row in review['skills']:
            row['blocked_reason'] = '需要先核对内容。'
        before = self.fingerprint()
        result = updates.apply(review, ['sample', 'zbad'], confirmed=True)
        self.assertEqual(result['updated'], [])
        self.assertEqual(result['operations'], [])
        self.assertNotIn('id', result)
        self.assertEqual(len(result['failed']), 2)
        self.assertFalse(result['complete'])
        self.assertEqual(before, self.fingerprint())

    def test_missing_download_does_not_block_the_next_skill(self):
        self.add_skill('zlast')
        review = self.review()
        row = next(r for r in review['skills'] if r['name'] == 'sample')
        rmtree(row['_upstream_path'])
        result = updates.apply(review, ['sample', 'zlast'], confirmed=True)
        self.assertEqual(result['updated'], ['zlast'])
        self.assertEqual(result['failed'], [{'name': 'sample', 'error': '这项更新的文件没能读写，请重新检查更新后再试。'}])

    def test_unconfirmed_merge_does_not_block_other_skills(self):
        self.add_skill('zlast')
        review = self.review()
        result = updates.apply(review, ['sample', 'zlast'], {'sample': 'merge'}, confirmed=True)
        self.assertEqual(result['updated'], ['zlast'])
        self.assertEqual([r['name'] for r in result['failed']], ['sample'])
        self.assertIn('核对后再确认', result['failed'][0]['error'])
        self.assertEqual((self.fixture.local() / 'SKILL.md').read_text(), self.fixture.original)

    def test_partial_results_are_returned_by_http_and_keep_failed_rows_visible(self):
        self.add_skill('zbad')
        review = self.review()
        next(r for r in review['skills'] if r['name'] == 'zbad')['blocked_reason'] = '需要先核对内容。'
        self.server = Dashboard({'home': str(self.home)})
        self.server.update_reviews[review['id']] = review
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), thread.join(), self.server.server_close()))
        body = {'review_id': review['id'], 'skills': ['sample', 'zbad'], 'confirmed': True}
        status, result = test_updates.UpdateWebTests.request(self, '/api/updates/apply', body)
        self.assertEqual(status, 200)
        self.assertEqual(result['updated'], ['sample'])
        self.assertEqual(result['failed'], [{'name': 'zbad', 'error': 'zbad：需要先核对内容。'}])
        _, public = test_updates.UpdateWebTests.request(self, '/api/updates/progress')
        failed = next(r for r in public['skills'] if r['name'] == 'zbad')
        self.assertTrue(failed['has_update'])
        self.assertIn('需要先核对', failed['update_error'])

    def test_cli_partial_result_is_nonzero_and_prints_the_failed_skill(self):
        self.add_skill('zbad')
        review = self.review()
        next(r for r in review['skills'] if r['name'] == 'zbad')['blocked_reason'] = '需要先核对内容。'
        output = io.StringIO()
        with patch('sys.argv', ['skill-pilot', 'update', 'sample', 'zbad', '--yes']), \
                patch.object(updates, 'check', return_value=review), \
                patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home)}), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_result:
                cli.main()
        self.assertEqual(exit_result.exception.code, 1)
        self.assertIn('已更新 1 个', output.getvalue())
        self.assertIn('zbad：', output.getvalue())

    def test_incomplete_restore_stops_later_updates(self):
        self.add_skill('zlast')
        review = self.review()
        with patch.object(operations, '_validate', side_effect=ValueError('write verification failed')), \
                patch.object(operations, '_restore', side_effect=ValueError('restore failed')), \
                patch.object(updates, '_apply_group', wraps=updates._apply_group) as apply_group:
            with self.assertRaisesRegex(operations.RollbackError, '后续更新已停止'):
                updates.apply(review, ['sample', 'zlast'], confirmed=True)
        self.assertEqual(apply_group.call_count, 1)


@unittest.skipIf(os.name == "nt", "Windows does not automatically install the Lark CLI")
class LarkInstallerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('npm'), 'npm is needed to verify its real configuration loader')
    def test_staged_install_passes_real_npm_configuration_loading(self):
        with tempfile.TemporaryDirectory(prefix='skill-pilot-npm-config-') as temporary:
            set_up_home(self, Path(temporary) / 'home')
            real_run = subprocess.run
            def fake_install(command, **kwargs):
                root = Path(kwargs['env']['HOME'])
                self.assertNotEqual(root, self.home)
                self.assertNotEqual(kwargs['env']['npm_config_userconfig'], kwargs['env']['npm_config_globalconfig'])
                check = real_run(['npm', 'config', 'get', 'registry', '--logs-max=0', '--update-notifier=false'],
                                 cwd=root, env=kwargs['env'], capture_output=True, timeout=20)
                self.assertEqual(check.returncode, 0, check.stderr.decode())
                package = root / 'prefix/lib/node_modules/@larksuite/cli'
                (package / 'scripts').mkdir(parents=True)
                (package / 'package.json').write_text('{"version":"1.0.98"}')
                (package / 'scripts/run.js').write_text('fixture')
                return subprocess.CompletedProcess(command, 0, b'', b'')
            with patch.object(lark.subprocess, 'run', side_effect=fake_install):
                lark.install_cli(self.home, '1.0.98', lambda p: store.guard(p, self.home))
            self.assertEqual(json.loads((self.home / lark.CLI_PATHS[0] / 'package.json').read_text())['version'], '1.0.98')


if __name__ == '__main__':
    unittest.main()
