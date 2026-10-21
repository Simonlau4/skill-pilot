"""Install and refresh real entry points under unrelated users and paths."""
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from skill_pilot import store
from skill_pilot.environment import tool_environment
from skill_pilot.sources import lark
from tests import set_up_home


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + content)
    path.chmod(0o755)
    return path


@unittest.skipIf(os.name == "nt", "POSIX shell tool fixtures; Windows CLI upgrades are disabled")
class ToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill-pilot-tools-')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, Path(temporary.name) / 'Users/Robin Home')
        self.home.mkdir(parents=True)

    def test_lark_prefers_this_users_managed_entry_and_passes_their_home(self):
        result = {'ok': True, 'action': 'update_available', 'current_version': '1.0.97', 'latest_version': '1.0.98'}
        entry = executable(self.home / lark.CLI_PATHS[1],
            f'[ "$HOME" = {shlex.quote(str(self.home))} ] || exit 1\n'
            + 'printf \'%s\' ' + shlex.quote(json.dumps(result)))
        other = executable(self.home / 'other tools/lark-cli', 'exit 23\n')
        with patch.dict(os.environ, {'PATH': str(other.parent)}):
            self.assertTrue(lark.check(home=self.home)['available'])
            self.assertEqual(shutil.which('lark-cli', path=tool_environment(self.home)['PATH']), str(entry))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed to exercise a real node entry')
    def test_lark_in_another_prefix_and_node_in_a_different_version_manager_folder(self):
        node = Path(shutil.which('node'))
        node_bin = self.home / '.nvm/versions/node/v22.17.1/bin'
        node_bin.mkdir(parents=True)
        (node_bin / 'node').symlink_to(node)
        entry = self.home / 'Custom Tools/bin/lark-cli'
        entry.parent.mkdir(parents=True)
        entry.write_text('#!/usr/bin/env node\nconsole.log(JSON.stringify({ok:true, action:"already_up_to_date",'
                         'current_version:"1.0.98", latest_version:"1.0.98"}));\n')
        entry.chmod(0o755)
        # 桌面启动只能看见自定义目录和系统目录。系统目录用临时替身，只放 env，不放 node，
        # 这样系统自带的 node 不会排在用户自己的 node 前面。
        system = self.home.parent / 'system bin'
        system.mkdir()
        env_tool = shutil.which('env') or '/usr/bin/env'
        (system / 'env').symlink_to(env_tool)
        with patch.dict(os.environ, {'PATH': os.pathsep.join((str(entry.parent), str(system)))}):
            env = tool_environment(self.home)
            self.assertEqual(shutil.which('node', path=env['PATH']), str(node_bin / 'node'))
            self.assertFalse(lark.check(home=self.home)['available'])

    def test_missing_cli_and_npm_report_actionable_errors_without_writing(self):
        env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.home / 'empty')}
        before = store.physical_state(self.home)
        with patch.object(lark, 'tool_environment', return_value=env):
            with self.assertRaisesRegex(ValueError, '找不到飞书工具'):
                lark.check(home=self.home)
            with self.assertRaisesRegex(ValueError, 'Node.js 和 npm'):
                lark.install_cli(self.home, '1.0.98', lambda p: store.guard(p, self.home))
        self.assertEqual(before, store.physical_state(self.home))


@unittest.skipUnless(sys.platform == 'darwin', 'the application installer targets macOS')
class PortableInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill-pilot-install-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'Users/Taylor Home')
        self.home.mkdir(parents=True)
        self.code = self.root / "Downloads/Skill Pilot 'edition' $(false)"
        for name in ('scripts', 'assets', 'skill_pilot', 'docs', 'skill'):
            shutil.copytree(store.PROJECT / name, self.code / name, ignore=shutil.ignore_patterns('__pycache__'))
        self.tools = self.root / 'Provided Tools'
        self.tools.mkdir()
        (self.tools / 'python3').symlink_to(sys.executable)
        self.env = {'HOME': str(self.home), 'TMPDIR': tempfile.gettempdir(),
                    'PATH': str(self.tools) + ':/usr/bin:/bin',
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.cli = self.home / '.local/bin/skill-pilot'
        self.app = self.home / 'Applications/Skill Pilot.app'
        self.pid = None
        self.addCleanup(self.stop)

    def stop(self):
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.pid = None

    def install(self, extra=None):
        return subprocess.run(['/bin/sh', str(self.code / 'scripts/build-app.sh')],
                              cwd=self.root, env={**self.env, **(extra or {})},
                              capture_output=True, text=True, timeout=20)

    def command(self, *arguments):
        # Finder has no interactive shell profile or custom tool search path.
        result = subprocess.run([str(self.cli), *arguments], cwd=self.root,
                                env={**self.env, 'PATH': '/usr/bin:/bin'},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_install_launch_and_reinstall_after_project_move(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        info = plistlib.loads((self.app / 'Contents/Info.plist').read_bytes())
        self.assertEqual(info['CFBundleExecutable'], 'SkillPilot')
        self.assertEqual((self.app / 'Contents/Resources/SkillPilot.icns').read_bytes(),
                         (self.code / 'assets/SkillPilot.icns').read_bytes())
        self.assertTrue(os.access(self.app / 'Contents/MacOS/SkillPilot', os.X_OK))
        self.assertEqual(self.command('scan', '--json')['home'], str(self.home))
        destination = self.root / 'Moved Installation/Skill Pilot'
        destination.parent.mkdir()
        self.code.rename(destination)
        self.code = destination
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.command('scan', '--json')['home'], str(self.home))

    def test_invalid_explicit_python_does_not_partially_install(self):
        old = executable(self.root / 'old-python', 'exit 1\n')
        result = self.install({'SKILL_PILOT_PYTHON': str(old)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python 3.11.4', result.stderr)
        self.assertFalse(self.cli.exists())
        self.assertFalse(self.app.exists())

    def test_desktop_entry_migration_and_source_update_refresh_without_reopening(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        skill = self.home / '.codex/skills/sample'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: sample\ndescription: demo\n---\noriginal\n')
        plan = self.command('migrate', '--plan', '--json')
        self.command('migrate', '--apply', '--plan-id', plan['plan_id'], '--json')
        url_file = self.root / 'opened-url'
        browser = executable(self.root / 'browser', 'printf \'%s\' "$1" > ' + shlex.quote(str(url_file)))
        subprocess.run([str(self.app / 'Contents/MacOS/SkillPilot')], cwd=self.root,
                       env={**self.env, 'PATH': '/usr/bin:/bin', 'BROWSER': str(browser) + ' %s'},
                       check=True, capture_output=True, timeout=5)
        deadline = time.monotonic() + 10
        while not url_file.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(url_file.exists(), 'desktop entry did not open its own dashboard')
        url = urllib.parse.urlsplit(url_file.read_text())
        origin = f'http://127.0.0.1:{url.port}'
        token = urllib.parse.parse_qs(url.query)['token'][0]
        def request(path, body=None):
            req = urllib.request.Request(origin + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'X-Skill-Pilot-Token': token, 'Origin': origin, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.load(response)
        initial = request('/api/status')
        self.pid = initial['pid']
        self.assertEqual(request('/api/table')['home'], str(self.home))
        before = store.physical_state(self.home / '.skill-pilot')
        path = self.code / 'skill_pilot/web/server.py'
        source = path.read_text()
        needle = "'version': self.server.code_version, 'reloading': self.server.reload_pending,"
        self.assertIn(needle, source)
        path.write_text(source.replace(needle, "'portable_marker': 'updated', " + needle))
        self.assertTrue(request('/api/runtime/refresh', {})['changed'])
        deadline = time.monotonic() + 12
        current = {}
        while time.monotonic() < deadline:
            try:
                current = request('/api/status')
                if current.get('portable_marker') == 'updated':
                    break
            except OSError:
                pass
            time.sleep(.05)
        self.assertEqual(current.get('portable_marker'), 'updated')
        self.assertEqual(current['pid'], self.pid)
        self.assertNotEqual(current['version'], initial['version'])
        self.assertEqual(before, store.physical_state(self.home / '.skill-pilot'))
