"""Application upgrades exercise real repositories, ZIP installs and entry points."""
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from skill_pilot import self_update, store
from skill_pilot.sources.github import git
from skill_pilot.web.server import Dashboard
from tests import set_up_home


def make_fixture(case, checkout=False):
    temporary = tempfile.TemporaryDirectory(prefix='skill-pilot-app-update-')
    case.addCleanup(temporary.cleanup)
    case.root = Path(temporary.name).resolve()
    set_up_home(case, case.root / 'Users/Another Person')
    case.home.mkdir(parents=True)
    case.upstream = case.root / 'Publisher/Official SP'
    case.upstream.mkdir(parents=True)
    for name in ('skill_pilot', 'scripts', 'assets', 'docs', 'skill'):
        shutil.copytree(store.PROJECT / name, case.upstream / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('README.md', '.gitignore'):
        shutil.copy2(store.PROJECT / name, case.upstream / name)
    source = case.upstream / 'skill_pilot/self_update.py'
    source.write_text(source.read_text().replace(repr(self_update.SOURCE), repr(case.upstream.as_uri())))
    (case.upstream / 'skill_pilot/obsolete.py').write_text('old = True\n')
    git(case.upstream, 'init', '-q', '-b', 'main')
    commit(case.upstream, 'Initial application')
    case.code = case.root / "Downloads/Skill Pilot 'edition' $(false)"
    if checkout:
        git(case.root, 'clone', '-q', str(case.upstream), str(case.code))
    else:
        shutil.copytree(case.upstream, case.code, ignore=shutil.ignore_patterns('.git'))
    source_patch = patch.object(self_update, 'SOURCE', case.upstream.as_uri())
    source_patch.start()
    case.addCleanup(source_patch.stop)
    self_update.run_installer(case.code, case.home)
    private = case.home / '.skill-pilot'
    private.mkdir()
    (private / 'settings.json').write_text('{"private_example": "do not replace"}')
    (private / 'registry.json').write_text('{"private_example": "keep allocations"}')
    case.personal_before = store.physical_state(private)


def commit(repository, message):
    git(repository, 'add', '.')
    git(repository, '-c', 'user.name=Test Publisher', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', message)


def publish(case):
    (case.upstream / 'skill_pilot/obsolete.py').unlink()
    (case.upstream / 'skill_pilot/added.py').write_text('new = True\n')
    path = case.upstream / 'skill_pilot/web/server.py'
    source = path.read_text()
    needle = "'version': self.server.code_version, 'reloading': self.server.reload_pending,"
    case.assertIn(needle, source)
    path.write_text(source.replace(needle, "'self_update_marker': 'new edition', " + needle))
    commit(case.upstream, 'Updated application')
    return git(case.upstream, 'rev-parse', 'HEAD')


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        make_fixture(self)

    def review(self):
        review = self_update.check(self.home, self.code)
        self.addCleanup(shutil.rmtree, review['temporary'], True)
        return review

    def test_fresh_zip_is_current_and_check_never_changes_installation(self):
        before = store.physical_state(self.code)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            review = self.review()
        self.assertFalse(review['available'])
        self.assertEqual(before, store.physical_state(self.code))
        self.assertEqual(self.personal_before, store.physical_state(self.home / '.skill-pilot'))

    def test_zip_upgrade_keeps_unrelated_files_personal_data_and_works_again(self):
        extra = self.code / 'docs/My private note.md'
        extra.write_text('leave this file alone')
        target = publish(self)
        result = self_update.apply(self.review(), self.home, self.code)
        self.assertFalse(result['available'])
        self.assertFalse((self.code / 'skill_pilot/obsolete.py').exists())
        self.assertTrue((self.code / 'skill_pilot/added.py').exists())
        self.assertEqual(extra.read_text(), 'leave this file alone')
        record = self_update.read_record(self.code)
        self.assertEqual(record['revision'], target)
        self.assertNotIn('docs/My private note.md', record['files'])
        self.assertFalse(self.review()['available'])
        (self.upstream / 'README.md').write_text('Another public edition\n')
        commit(self.upstream, 'Next public edition')
        self_update.apply(self.review(), self.home, self.code)
        self.assertEqual(extra.read_text(), 'leave this file alone')
        self.assertEqual(self.personal_before, store.physical_state(self.home / '.skill-pilot'))
        cli = self.home / '.local/bin/skill-pilot'
        result = subprocess.run([str(cli), 'scan', '--json'], cwd=self.root,
            env={**os.environ, 'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'SKILL_PILOT_READ_ONLY': '1'},
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['home'], str(self.home))

    def test_changed_local_file_is_preserved_without_network_or_application(self):
        publish(self)
        review = self.review()
        path = self.code / 'README.md'
        path.write_text('my local edits')
        with patch.object(self_update, 'git') as network:
            with self.assertRaisesRegex(ValueError, '本地改动'):
                self_update.check(self.home, self.code)
            network.assert_not_called()
        with self.assertRaisesRegex(ValueError, '本地改动'):
            self_update.apply(review, self.home, self.code)
        self.assertEqual(path.read_text(), 'my local edits')

    def test_new_file_collision_and_external_directory_link_are_preserved(self):
        publish(self)
        review = self.review()
        path = self.code / 'skill_pilot/added.py'
        path.write_text('local content')
        with self.assertRaisesRegex(ValueError, '重名'):
            self_update.apply(review, self.home, self.code)
        self.assertEqual(path.read_text(), 'local content')
        path.unlink()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        path.symlink_to(elsewhere / 'file')
        with self.assertRaisesRegex(ValueError, '指向了其他位置'):
            self_update.apply(review, self.home, self.code)
        self.assertFalse((elsewhere / 'file').exists())

    def test_readonly_mode_and_tampered_download_do_not_change_program(self):
        publish(self)
        review = self.review()
        before = store.physical_state(self.code)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            with self.assertRaisesRegex(ValueError, '只读'):
                self_update.apply(review, self.home, self.code)
        (Path(review['candidate']) / 'skill_pilot/added.py').write_text('tampered = True\n')
        with self.assertRaisesRegex(ValueError, '下载的文件已经变了'):
            self_update.apply(review, self.home, self.code)
        self.assertEqual(before, store.physical_state(self.code))

    def test_incomplete_or_unloadable_new_version_never_replaces_original(self):
        before = store.physical_state(self.code)
        (self.upstream / 'skill_pilot/cli.py').write_text('this is not valid python!')
        commit(self.upstream, 'Broken public edition')
        with self.assertRaisesRegex(ValueError, '无法在这台电脑运行'):
            self_update.check(self.home, self.code)
        self.assertEqual(before, store.physical_state(self.code))
        (self.upstream / 'skill_pilot/cli.py').unlink()
        commit(self.upstream, 'Incomplete public edition')
        with self.assertRaisesRegex(ValueError, '不完整'):
            self_update.check(self.home, self.code)
        self.assertEqual(before, store.physical_state(self.code))

    def test_failed_installer_restores_program_marker_and_desktop_entries(self):
        publish(self)
        review = self.review()
        before = store.physical_state(self.code)
        entries_before = store.physical_state(self.home / 'Applications')
        cli = self.home / '.local/bin/skill-pilot'
        cli_before = cli.read_bytes()
        def fail(project, home):
            cli.write_text('partially replaced launcher')
            (home / 'Applications/Skill Pilot.app/Contents/Info.plist').write_text('incomplete')
            raise ValueError('simulated installation failure')
        with patch.object(self_update, 'run_installer', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '已恢复原版'):
                self_update.apply(review, self.home, self.code)
        self.assertEqual(before, store.physical_state(self.code))
        self.assertEqual(entries_before, store.physical_state(self.home / 'Applications'))
        self.assertEqual(cli_before, cli.read_bytes())
        self.assertEqual(self.personal_before, store.physical_state(self.home / '.skill-pilot'))
        # A failed attempt can be retried using the same complete download.
        self_update.apply(review, self.home, self.code)

    def test_download_failure_never_changes_original(self):
        before = store.physical_state(self.code)
        with patch.object(self_update, 'SOURCE', (self.root / 'unreachable').as_uri()):
            with self.assertRaisesRegex(ValueError, 'SP 更新没有完成，请检查网络'):
                self_update.check(self.home, self.code)
        self.assertEqual(before, store.physical_state(self.code))


class CheckoutSelfUpdateTests(unittest.TestCase):
    def setUp(self):
        make_fixture(self, checkout=True)

    def review(self):
        review = self_update.check(self.home, self.code)
        self.addCleanup(shutil.rmtree, review['temporary'], True)
        return review

    def test_checkout_fast_forwards_and_public_instructions_still_work(self):
        target = publish(self)
        self_update.apply(self.review(), self.home, self.code)
        self.assertEqual(git(self.code, 'rev-parse', 'HEAD'), target)
        self.assertEqual(git(self.code, 'status', '--porcelain'), '')
        self.assertFalse(self.review()['available'])
        self.assertEqual(self.personal_before, store.physical_state(self.home / '.skill-pilot'))

    def test_uncommitted_or_divergent_local_versions_are_not_overwritten(self):
        publish(self)
        (self.code / 'README.md').write_text('local version')
        with self.assertRaisesRegex(ValueError, '本地改动'):
            self.review()
        commit(self.code, 'Private local edition')
        before = git(self.code, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, '与公开版本不同'):
            self.review()
        self.assertEqual(git(self.code, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.code / 'README.md').read_text(), 'local version')

    def test_failed_upgrade_restores_checkout_position_and_can_retry(self):
        publish(self)
        before = git(self.code, 'rev-parse', 'HEAD')
        review = self.review()
        with patch.object(self_update, 'run_installer', side_effect=ValueError('failure')):
            with self.assertRaisesRegex(ValueError, '已恢复原版'):
                self_update.apply(review, self.home, self.code)
        self.assertEqual(git(self.code, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(self.code, 'status', '--porcelain'), '')
        self_update.apply(review, self.home, self.code)
        self.assertEqual(git(self.code, 'rev-parse', 'HEAD'), review['target'])

    def test_edit_made_while_preparing_upgrade_is_preserved(self):
        publish(self)
        review = self.review()
        before = git(self.code, 'rev-parse', 'HEAD')
        original = self_update.git
        def edit_after_fetch(project, *arguments, **kwargs):
            result = original(project, *arguments, **kwargs)
            if project == self.code and arguments[0] == 'fetch':
                (self.code / 'README.md').write_text('my edit while upgrade was preparing')
            return result
        with patch.object(self_update, 'git', side_effect=edit_after_fetch):
            with self.assertRaisesRegex(ValueError, '本地改动'):
                self_update.apply(review, self.home, self.code)
        self.assertEqual(git(self.code, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.code / 'README.md').read_text(), 'my edit while upgrade was preparing')


class SelfUpdateWebTests(unittest.TestCase):
    def setUp(self):
        make_fixture(self)
        publish(self)
        original_check, original_apply = self_update.check, self_update.apply
        for name, function in (('check', original_check), ('apply', original_apply)):
            wrapped = (lambda home, f=function: f(home, self.code)) if name == 'check' else \
                      (lambda review, home, f=function: f(review, home, self.code))
            patcher = patch.object(self_update, name, side_effect=wrapped)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = Dashboard({'home': str(self.home)})
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def close():
            self.server.shutdown()
            thread.join()
            self.server.server_close()
        self.addCleanup(close)

    def request(self, route, body=None, extra=None):
        headers = {'X-Skill-Pilot-Token': self.server.token, 'Origin': self.server.origin,
                   'Content-Type': 'application/json', **(extra or {})}
        req = urllib.request.Request(self.server.origin + route, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def finished(self):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            _, progress = self.request('/api/program/progress')
            if not progress['running']:
                return progress
            time.sleep(.02)
        self.fail('application update did not finish')

    def test_background_progress_survives_refresh_and_upgrade_keeps_private_data(self):
        self.assertEqual(self.request('/api/program/check', {})[0], 200)
        progress = self.finished()
        self.assertTrue(progress['available'], progress)
        self.assertNotIn('candidate', progress)
        self.assertNotIn('before', progress)
        self.assertEqual(self.request('/api/program/apply', {'confirmed': True, 'target': progress['target']})[0], 200)
        progress = self.finished()
        self.assertEqual(progress['phase'], 'ready', progress)
        self.assertEqual(self.personal_before, store.physical_state(self.home / '.skill-pilot'))
        # A refresh that only needs static/script changes acknowledges completion too.
        self.request('/api/runtime/refresh', {})
        self.assertEqual(self.request('/api/program/progress')[1]['phase'], 'latest')

    def test_origin_confirmation_stale_selection_and_readonly_are_enforced(self):
        for extra in ({'Origin': 'http://elsewhere.invalid'}, {'X-Skill-Pilot-Token': 'wrong'}):
            for action in ('check', 'apply'):
                self.assertEqual(self.request('/api/program/' + action, {}, extra)[0], 403)
        self.assertEqual(self.request('/api/program/apply', {})[0], 400)
        self.assertEqual(self.request('/api/program/apply', {'confirmed': True, 'target': 'stale'})[0], 409)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertEqual(self.request('/api/program/apply', {'confirmed': True})[0], 403)
            self.assertFalse(self.request('/api/program/progress')[1]['write_allowed'])

    def test_upgrade_waits_for_current_work_and_blocks_new_writes(self):
        self.request('/api/program/check', {})
        progress = self.finished()
        self.server.labels_progress['running'] = True
        try:
            self.request('/api/program/apply', {'confirmed': True, 'target': progress['target']})
            time.sleep(.05)
            self.assertTrue(self.request('/api/program/progress')[1]['running'])
            self.assertFalse((self.code / 'skill_pilot/added.py').exists())
            self.assertEqual(self.request('/api/assignment/apply', {'confirmed': True})[0], 503)
            self.assertEqual(self.request('/api/heartbeat', {})[0], 200)
        finally:
            self.server.labels_progress['running'] = False
        self.assertEqual(self.finished()['phase'], 'ready')


class SelfUpdateProcessTests(unittest.TestCase):
    def run_flow(self, checkout):
        make_fixture(self, checkout)
        # Use a genuinely initialized library for the process/browser acceptance path.
        (self.home / '.skill-pilot/registry.json').unlink()
        sample = self.home / '.codex/skills/example'
        sample.mkdir(parents=True)
        (sample / 'SKILL.md').write_text('---\nname: example\ndescription: test skill\n---\nkeep my content\n')
        self.env = {**os.environ, 'HOME': str(self.home), 'SKILL_PILOT_HOME': str(self.home),
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.env.pop('SKILL_PILOT_READ_ONLY', None)
        cli = str(self.home / '.local/bin/skill-pilot')
        def command(*arguments):
            result = subprocess.run([cli, *arguments], cwd=self.root, env=self.env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return json.loads(result.stdout)
        plan = command('migrate', '--plan', '--json')
        command('migrate', '--apply', '--plan-id', plan['plan_id'], '--json')
        bootstrap = 'from skill_pilot.web import server; server.webbrowser.open = lambda url: True; server.run(json_output=True)'
        program = subprocess.Popen([sys.executable, '-B', '-u', '-c', bootstrap],
            cwd=self.code, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        def stop():
            program.terminate()
            try:
                program.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                program.kill()
                program.communicate()
        self.addCleanup(stop)
        self.assertTrue(select.select([program.stdout], [], [], 10)[0])
        line = program.stdout.readline()
        self.assertTrue(line, program.stderr.read() if program.poll() is not None else 'no startup output')
        info = json.loads(line)
        origin = info['url'].split('/?')[0]
        token = info['url'].split('token=')[1]
        def request(route, body=None):
            req = urllib.request.Request(origin + route,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'X-Skill-Pilot-Token': token, 'Origin': origin, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.load(response)
        def until(route, predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    result = request(route)
                    if predicate(result):
                        return result
                except OSError:
                    pass
                time.sleep(.03)
            self.fail('update process did not reach the expected state')
        initial = request('/api/status')
        before = store.physical_state(self.home / '.skill-pilot')
        target = publish(self)
        request('/api/program/check', {})
        progress = until('/api/program/progress', lambda p: not p['running'])
        self.assertTrue(progress['available'], progress)
        self.assertEqual(progress['target'], target)
        request('/api/program/apply', {'confirmed': True, 'target': target})
        progress = until('/api/program/progress', lambda p: not p['running'])
        self.assertEqual(progress['phase'], 'ready', progress)
        self.assertNotIn('self_update_marker', request('/api/status'))
        self.assertTrue(request('/api/runtime/refresh', {})['changed'])
        current = until('/api/status', lambda p: p.get('self_update_marker') == 'new edition')
        self.assertEqual(current['pid'], initial['pid'])
        self.assertNotEqual(current['version'], initial['version'])
        self.assertEqual(request('/api/program/progress')['phase'], 'latest')
        self.assertEqual(request('/api/table')['home'], str(self.home))
        self.assertEqual(before, store.physical_state(self.home / '.skill-pilot'))
        self.assertFalse(request('/api/runtime/refresh', {})['changed'])
        request('/api/program/check', {})
        self.assertFalse(until('/api/program/progress', lambda p: not p['running'])['available'])

    def test_zip_download_upgrade_refresh_and_recheck(self):
        self.run_flow(False)

    def test_checkout_upgrade_refresh_and_recheck(self):
        self.run_flow(True)
