"""Windows installation, successive official updates, conflicts and rollback."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from unittest.mock import patch

from skill_pilot import self_update, store, windows_install
from skill_pilot.platform_support import rmtree
from skill_pilot.sources.github import git


@unittest.skipUnless(os.name == 'nt', 'Windows installation and reload')
class WindowsUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='Skill Pilot Windows tests ')).resolve()
        cls.official = cls.root / 'Official Source'
        cls.official.mkdir()
        for name in self_update.BUNDLE_DIRS:
            if name != 'tests' and (store.PROJECT / name).is_dir():
                shutil.copytree(store.PROJECT / name, cls.official / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('README.md', 'WINDOWS.md', '.gitignore', '.gitattributes', 'skill-pilot'):
            shutil.copy2(store.PROJECT / name, cls.official / name)
        git(cls.official, 'init', '-q', '-b', 'main')
        cls.commit('baseline')
        cls.base = git(cls.official, 'rev-parse', 'HEAD')

    @classmethod
    def tearDownClass(cls):
        rmtree(cls.root)

    @classmethod
    def commit(cls, message):
        git(cls.official, 'add', '.')
        git(cls.official, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', message)

    def setUp(self):
        self.area = Path(tempfile.mkdtemp(prefix='case ', dir=self.root))
        self.home = self.area / 'User With Spaces 中文'
        self.home.mkdir(parents=True)
        self.project = self.area / 'Installed Program'
        git(self.official, 'reset', '--hard', self.base)
        shutil.copytree(self.official, self.project, ignore=shutil.ignore_patterns('.git'))
        self_update.record_installation(self.project, self.base, '2026-10-21T00:00:00Z')
        self.source_patch = patch.object(self_update, 'SOURCE', self.official.as_uri())
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.env_patch = patch.dict(os.environ, {'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def update(self, text):
        path = self.official / 'skill_pilot/web/server.py'
        path.write_text(path.read_text(encoding='utf-8') + '\n# ' + text + '\n', encoding='utf-8')
        self.commit(text)
        review = self_update.check(self.home, self.project)
        self.addCleanup(lambda: rmtree(review['temporary'], ignore_errors=True) if review.get('temporary') else None)
        return review

    def install_entries(self):
        try:
            windows_install.install(self.project, self.home)
        except ValueError as error:
            detail = getattr(error.__cause__, 'stderr', b'') or b''
            self.fail(str(error) + '\n' + detail.decode('utf-8', 'replace'))

    def test_two_upgrades_preserve_compatibility_entries_and_user_data(self):
        sentinel = self.home / '.skill-pilot/settings.json'
        sentinel.parent.mkdir()
        sentinel.write_text('{"test":"leave intact"}')
        self.install_entries()
        for label in ('release one', 'release two'):
            review = self.update(label)
            self.assertTrue(review['available'])
            result = self_update.apply(review, self.home, self.project)
            self.assertFalse(result['available'])
            self.assertIn('windows_runtime', (self.project / 'skill_pilot/web/server.py').read_text())
            self.assertIn(label, (self.project / 'skill_pilot/web/server.py').read_text())
            self.assertFalse(self_update.check(self.home, self.project)['available'])
            self.assertTrue(all(path.is_file() for path in windows_install.entries(self.home)))
            self.assertEqual(sentinel.read_text(), '{"test":"leave intact"}')

    def test_failed_install_restores_program_and_entry(self):
        self.install_entries()
        before = self_update.installed(self.project)
        cli = windows_install.entries(self.home)[0]
        entry = cli.read_bytes()
        review = self.update('rollback release')
        def fail(*args):
            cli.write_text('broken')
            raise ValueError('simulated installer failure')
        with patch.object(self_update, 'run_installer', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '已恢复原版'):
                self_update.apply(review, self.home, self.project)
        self.assertEqual(self_update.installed(self.project), before)
        self.assertEqual(cli.read_bytes(), entry)

    def test_source_checkout_updates_and_installed_cli_works(self):
        rmtree(self.project)
        git(self.area, 'clone', '--quiet', self.official.as_uri(), str(self.project))
        self_update.record_installation(self.project)
        review = self.update('source checkout release')
        self.assertTrue(review['before']['checkout'])
        self_update.apply(review, self.home, self.project)
        self.assertEqual(git(self.project, 'rev-parse', 'HEAD'), review['target'])
        cli = windows_install.entries(self.home)[0]
        result = subprocess.run([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', str(cli), '--help'],
                                env={**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '1'},
                                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        self.assertIn('Skill Pilot', result.stdout.decode('utf-8', 'replace'))

    def test_unrunnable_official_update_preserves_install(self):
        original = self_update.installed(self.project)
        path = self.official / 'skill_pilot/web/server.py'
        content = path.read_text(encoding='utf-8') + '\nimport incompatible_locking\n'
        path.write_text(content, encoding='utf-8')
        self.commit('conflict')
        with self.assertRaisesRegex(ValueError, '新版暂时无法'):
            self_update.check(self.home, self.project)
        self.assertEqual(self_update.installed(self.project), original)

    def test_local_edit_blocks_update(self):
        path = self.project / 'skill_pilot/cli.py'
        path.write_text(path.read_text() + '\n# user change\n')
        with self.assertRaisesRegex(ValueError, '本地改动'):
            self_update.check(self.home, self.project)

    def test_refresh_preserves_url_token_and_loads_new_python(self):
        log = self.area / 'process.log'
        errors = self.area / 'errors.log'
        code = '''import webbrowser
webbrowser.open=lambda *a,**k: True
from skill_pilot.web import server
original = server.Dashboard.__init__
def initialize(self, inventory, resume=None):
    original(self, inventory, resume)
    if resume is None:
        self.team_queue = [{'id':'retained-job', 'key':'files:example', 'name':'example',
                            'status':'done', 'message':'installed', 'installed':['example'], 'reuse':[]}]
server.Dashboard.__init__ = initialize
server.run(True)
'''
        env = {**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '1'}
        with log.open('w', encoding='utf-8') as output, errors.open('w', encoding='utf-8') as error:
            process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-u', '-c', code],
                                       cwd=self.project, env=env, stdout=output, stderr=error,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        for _ in range(200):
            if log.read_text(encoding='utf-8').strip():
                break
            self.assertIsNone(process.poll(), errors.read_text(encoding='utf-8'))
            time.sleep(.05)
        launch = json.loads(log.read_text(encoding='utf-8').splitlines()[0])
        url = urllib.parse.urlsplit(launch['url'])
        origin = f'{url.scheme}://{url.netloc}'
        token = urllib.parse.parse_qs(url.query)['token'][0]
        def request(path, body=None):
            headers = {'X-Skill-Pilot-Token': token, 'Origin': origin, 'Content-Type': 'application/json'}
            req = urllib.request.Request(origin + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.load(response)
        before = request('/api/status')
        queue = request('/api/team/queue')
        self.assertFalse(request('/api/runtime/refresh', {})['changed'])
        self.assertEqual(request('/api/team/queue'), queue)
        request('/api/heartbeat?page=windows-test', {})
        path = self.project / 'skill_pilot/web/server.py'
        content = path.read_text(encoding='utf-8')
        needle = "'version': self.server.code_version,"
        self.assertIn(needle, content)
        path.write_text(content.replace(needle, "'windows_test_reloaded': True, " + needle), encoding='utf-8')
        self.assertTrue(request('/api/runtime/refresh', {})['changed'])
        for _ in range(200):
            self.assertIsNone(process.poll(), errors.read_text(encoding='utf-8'))
            try:
                after = request('/api/status')
                if after.get('windows_test_reloaded'):
                    break
            except OSError:
                pass
            time.sleep(.05)
        else:
            self.fail('refresh did not load new Python: ' + errors.read_text(encoding='utf-8'))
        self.assertEqual(before['pid'], after['pid'])
        self.assertNotEqual(before['version'], after['version'])
        self.assertEqual(request('/api/team/queue'), {'items': []})
        self.assertTrue(request('/api/heartbeat?page=windows-test', {})['ok'])
        previous = after['version']
        path.write_text(path.read_text(encoding='utf-8') + '\n# second program refresh\n', encoding='utf-8')
        self.assertTrue(request('/api/runtime/refresh', {})['changed'])
        for _ in range(200):
            self.assertIsNone(process.poll(), errors.read_text(encoding='utf-8'))
            try:
                if request('/api/status')['version'] != previous:
                    break
            except OSError:
                pass
            time.sleep(.05)
        else:
            self.fail('second refresh did not finish: ' + errors.read_text(encoding='utf-8'))
        self.assertEqual(request('/api/team/queue'), {'items': []})
        process.terminate()
        process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
