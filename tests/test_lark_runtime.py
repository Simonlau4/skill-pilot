"""Resolve HOME-dependent npm launchers before installing in an isolated home."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from skill_pilot import store
from skill_pilot.sources import lark
from tests import set_up_home


@unittest.skipIf(os.name == 'nt', 'automatic Lark installation is disabled on Windows')
@unittest.skipUnless(shutil.which('node'), 'Node is needed to exercise launcher resolution')
class LarkRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot runtime ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / "Example user's Home")
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        self.node = subprocess.check_output(['node', '-p', 'process.execPath'], text=True).strip()
        self.runtime = self.home / '.example-runtimes/version-a/bin'
        self.runtime.mkdir(parents=True)
        (self.runtime / 'node').symlink_to(self.node)
        self.entry = self.home / 'Npm Distribution/bin/npm-cli.js'
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text('''#!/usr/bin/env node
const fs = require('node:fs'), path = require('node:path'), cp = require('node:child_process');
if (process.argv[2] !== 'install') {
  fs.writeFileSync(path.join(process.env.HOME, 'unexpected-npm-start'), 'probe entered npm');
  process.exit(90);
}
const root = process.env.HOME;
for (const key of ['npm_config_prefix', 'npm_config_cache', 'npm_config_userconfig',
                  'npm_config_globalconfig', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME']) {
  if (!process.env[key].startsWith(root + path.sep)) process.exit(91);
}
if (process.cwd() !== fs.realpathSync(root) || process.env.NPM_CONFIG_REGISTRY || process.env.PNPM_HOME) process.exit(92);
const child = cp.execFileSync('node', ['-p', 'process.execPath'], {encoding:'utf8'}).trim();
if (child !== process.execPath) process.exit(93);
const target = path.join(process.env.npm_config_prefix, 'lib/node_modules/@larksuite/cli');
fs.mkdirSync(path.join(target, 'scripts'), {recursive:true});
fs.writeFileSync(path.join(target, 'package.json'), JSON.stringify({version:'1.0.98'}));
fs.writeFileSync(path.join(target, 'scripts/run.js'), 'test CLI');
fs.writeFileSync(path.join(target, 'observed.json'), JSON.stringify({home:root,node:process.execPath,entry:process.argv[1]}));
''')
        self.entry.chmod(0o755)
        self.launcher('node', '''for p in "$HOME"/.example-runtimes/*/bin/node; do
  [ -x "$p" ] && exec "$p" "$@"
done
echo 'node: 找不到可用解释器' >&2
exit 127
''')
        self.launcher('npm', '''for d in "$HOME"/.example-runtimes/*/bin; do
  [ -x "$d/node" ] && PATH="$d:$PATH" && export PATH && break
done
exec node ''' + shlex.quote(str(self.entry)) + ' "$@"\n')
        self.env = {**os.environ, 'HOME': str(self.home), 'PATH': str(self.bin) + os.pathsep + os.defpath,
                    'NPM_CONFIG_REGISTRY': 'https://example.invalid/private', 'PNPM_HOME': 'unused'}
        self.environment = patch.object(lark, 'tool_environment', return_value=self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.bin / 'lark-cli').write_text('original executable')

    def launcher(self, name, body):
        file = self.bin / name
        file.write_text('#!/bin/sh\n' + body)
        file.chmod(0o755)

    def install(self):
        lark.install_cli(self.home, '1.0.98', lambda p: store.guard(p, self.home))

    def test_home_dependent_launchers_install_without_exposing_original_home(self):
        before = {str(p): store.physical_state(p) for p in (self.runtime, self.entry, self.bin / 'node', self.bin / 'npm')}
        self.install()
        package = self.home / lark.CLI_PATHS[0]
        observed = json.loads((package / 'observed.json').read_text())
        self.assertNotEqual(observed['home'], str(self.home))
        self.assertFalse(Path(observed['home']).exists())
        self.assertEqual(observed['node'], self.node)
        self.assertEqual(observed['entry'], str(self.entry))
        self.assertFalse((self.home / 'unexpected-npm-start').exists())
        self.assertTrue((self.home / lark.CLI_PATHS[1]).is_symlink())
        self.assertEqual(before, {p: store.physical_state(Path(p)) for p in before})

    def test_launcher_can_locate_npm_itself_through_home(self):
        self.launcher('npm', 'exec node "$HOME/Npm Distribution/bin/npm-cli.js" "$@"\n')
        self.install()
        self.assertEqual((self.home / lark.CLI_PATHS[0] / 'scripts/run.js').read_text(), 'test CLI')

    def test_standard_symlink_launchers_and_spaced_staging_location(self):
        for name, target in (('node', self.node), ('npm', self.entry)):
            (self.bin / name).unlink()
            (self.bin / name).symlink_to(target)
        staging = self.root / "Installer's temporary space"
        staging.mkdir()
        make_temporary = tempfile.TemporaryDirectory
        with patch.object(lark.tempfile, 'TemporaryDirectory',
                          side_effect=lambda **options: make_temporary(dir=staging, **options)):
            self.install()
        self.assertEqual(list(staging.iterdir()), [])
        self.assertFalse((self.home / 'unexpected-npm-start').exists())

    def test_failed_launcher_and_installer_keep_original_files_and_error_cause(self):
        self.launcher('npm', "echo 'runtime unavailable' >&2\nexit 127\n")
        before = store.physical_state(self.home)
        with self.assertRaisesRegex(ValueError, '启动失败.*127') as failure:
            self.install()
        self.assertEqual(failure.exception.__cause__.returncode, 127)
        self.assertEqual(failure.exception.__cause__.stderr, b'runtime unavailable\n')
        self.assertEqual(store.physical_state(self.home), before)
        self.launcher('npm', 'exec node ' + shlex.quote(str(self.entry)) + ' "$@"\n')
        self.entry.write_text("process.stderr.write('example install error'); process.exit(23);\n")
        before = store.physical_state(self.home)
        with self.assertRaisesRegex(ValueError, '安装程序报错.*23') as failure:
            self.install()
        self.assertEqual(failure.exception.__cause__.stderr, b'example install error')
        self.assertEqual(store.physical_state(self.home), before)

    def test_timeout_and_malformed_runtime_response_do_not_write_home(self):
        before = store.physical_state(self.home)
        for result in (subprocess.TimeoutExpired(['npm', '--version'], 20),
                       subprocess.CompletedProcess(['npm'], 0, b'not runtime data', b'')):
            with patch.object(lark.subprocess, 'run', side_effect=result if isinstance(result, Exception) else None,
                              return_value=result):
                with self.assertRaisesRegex(ValueError, '工具启动入口|工具位置'):
                    self.install()
            self.assertEqual(store.physical_state(self.home), before)
        run = subprocess.run
        def timeout(command, **kwargs):
            if 'install' in command:
                raise subprocess.TimeoutExpired(command, 300)
            return run(command, **kwargs)
        with patch.object(lark.subprocess, 'run', side_effect=timeout):
            with self.assertRaisesRegex(ValueError, '等待超时'):
                self.install()
        self.assertEqual(store.physical_state(self.home), before)

    def test_readonly_never_launches_tools(self):
        before = store.physical_state(self.home)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), patch.object(lark.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, '只读'):
                self.install()
            run.assert_not_called()
        self.assertEqual(store.physical_state(self.home), before)


if __name__ == '__main__':
    unittest.main()
