import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skill_pilot.platform_support import fcntl
from skill_pilot import codex_config, store


class PlatformTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows junction migration')
    def test_junction_migration_and_restore_preserve_target_and_entry(self):
        import _winapi
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            target = home / '.agents/skills/sample'
            target.mkdir(parents=True)
            (target / 'SKILL.md').write_text('---\nname: sample\ndescription: test\n---\nkeep me\n', encoding='utf-8')
            entry = home / '.claude/skills/sample'
            entry.parent.mkdir(parents=True)
            _winapi.CreateJunction(str(target), str(entry))
            roots = ['.agents/skills', '.claude/skills']
            before = {p: store.physical_state(home / p) for p in roots}
            plan = store.preview(home, ['codex', 'dsh', 'claude'])
            result = store.apply(plan['plan_id'], home, ['codex', 'dsh', 'claude'])
            self.assertTrue(entry.is_symlink())
            self.assertIn('keep me', (entry / 'SKILL.md').read_text())
            store.restore(result['backup_id'], home)
            self.assertTrue(entry.is_junction())
            self.assertEqual(entry.resolve(), target)
            self.assertEqual({p: store.physical_state(home / p) for p in roots}, before)

    def test_process_lock_preserves_readable_payload_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            path.write_text('dashboard-url', encoding='utf-8')
            code = '''
import sys
from skill_pilot.platform_support import fcntl
with open(sys.argv[1], 'r+') as stream:
    assert stream.read() == 'dashboard-url'
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(7)
'''
            with path.open('r+') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, str(path)], timeout=10)
                self.assertEqual(result.returncode, 7)
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, str(path)], timeout=10)
            self.assertEqual(result.returncode, 0)

    def test_crlf_config_roundtrip_preserves_original_bytes(self):
        original = b'# existing\r\n[skills]\r\nconfig = []\r\n'
        changed, ownership = codex_config.render(original, {}, ['sample'])
        restored, _ = codex_config.render(changed, ownership, [])
        self.assertEqual(restored, original)

    @unittest.skipUnless(os.name == 'nt', 'Windows directory link metadata')
    def test_broken_directory_link_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root = home / '.claude/skills'
            root.mkdir(parents=True)
            link = root / 'missing'
            link.symlink_to(home / 'absent', target_is_directory=True)
            checked = lambda p: store.guard(p, home)
            before = link.lstat().st_file_attributes
            backup = store.backup(home, {'roots': ['.claude/skills'], 'plan_id': 'test', 'before': {}}, checked)
            link.unlink()
            store.restore(backup['backup_id'], home)
            self.assertEqual(link.lstat().st_file_attributes, before)
            self.assertTrue(link.is_symlink())
