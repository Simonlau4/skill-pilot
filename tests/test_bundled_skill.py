"""The program's own skill follows upgrades without resetting user choices."""
import os
import unittest
from unittest.mock import patch

from skill_pilot import cleanup, operations, store
from skill_pilot.web import runtime, server
from tests import test_operations


class BundledSkillTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_operations.OperationsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        self.project = self.home / 'Program with spaces'
        self.incoming = self.project / 'skill/skill-pilot/SKILL.md'
        self.incoming.parent.mkdir(parents=True)
        self.incoming.write_text('---\nname: skill-pilot\ndescription: 管理技能\n---\n旧说明\n')
        (self.project / 'docs').mkdir()
        (self.project / 'docs/path-references.md').write_text('')
        self.enterContext(patch.object(operations, 'PROJECT', self.project))
        self.install = operations.bootstrap(self.home)
        self.current = self.home / '.skill-pilot/store/skill-pilot/SKILL.md'
        self.original = self.current.read_bytes()
        self.incoming.write_text(self.incoming.read_text() + '\n新版增加成品上传说明。\n')

    def test_sync_preserves_grants_and_undo_is_not_reapplied(self):
        self.fixture.assign('skill-pilot', 'claude', False)
        record = operations.load(self.home)[1]['skills']['skill-pilot']
        config = (self.home / '.codex/config.toml').read_bytes()
        other = store.physical_state(self.home / '.skill-pilot/store/other')
        result = operations.sync_bundled_skill(self.home)
        self.assertEqual(self.current.read_bytes(), self.incoming.read_bytes())
        updated = operations.load(self.home)[1]['skills']['skill-pilot']
        self.assertEqual(updated['assigned_agents'], record['assigned_agents'])
        self.assertEqual(updated['entries'], record['entries'])
        self.assertEqual(updated['source'], record['source'])
        self.assertEqual((self.home / '.codex/config.toml').read_bytes(), config)
        self.assertEqual(store.physical_state(self.home / '.skill-pilot/store/other'), other)
        count = len(operations.history(self.home))
        self.assertIsNone(operations.sync_bundled_skill(self.home))
        self.assertEqual(len(operations.history(self.home)), count)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.current.read_bytes(), self.original)
        self.assertIsNone(operations.sync_bundled_skill(self.home))
        self.incoming.write_text(self.incoming.read_text() + '再下一版。\n')
        self.assertIsNotNone(operations.sync_bundled_skill(self.home))

    def test_local_edits_and_readonly_are_preserved(self):
        self.current.write_bytes(self.original + b'\nmy changes\n')
        before = store.physical_state(self.home)
        self.assertIsNone(operations.sync_bundled_skill(self.home))
        self.assertEqual(store.physical_state(self.home), before)
        self.current.write_bytes(self.original)
        before = store.physical_state(self.home)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertIsNone(operations.sync_bundled_skill(self.home))
        self.assertEqual(store.physical_state(self.home), before)

    def test_withdrawn_skill_is_not_restored(self):
        cleanup.apply(['skill-pilot'], confirmed=True, home=self.home)
        before = store.physical_state(self.home)
        self.assertIsNone(operations.sync_bundled_skill(self.home))
        self.assertEqual(store.physical_state(self.home), before)

    def test_undone_first_install_is_not_restored(self):
        operations.undo(self.install['id'], home=self.home)
        before = store.physical_state(self.home)
        self.assertIsNone(operations.sync_bundled_skill(self.home))
        self.assertEqual(store.physical_state(self.home), before)

    def test_edits_during_the_update_check_are_preserved(self):
        original_revision = operations.revision
        def edit_then_read(home, agents):
            self.current.write_bytes(self.original + b'\nconcurrent change\n')
            return original_revision(home, agents)
        with patch.object(operations, 'revision', side_effect=edit_then_read):
            self.assertIsNone(operations.sync_bundled_skill(self.home))
        self.assertTrue(self.current.read_bytes().endswith(b'concurrent change\n'))

    def test_failed_copy_restores_the_previous_skill(self):
        before = self.fixture.fingerprint()
        original_copy = operations.copytree
        def fail_base(source, destination, **kwargs):
            if destination == self.home / '.skill-pilot/bases/skill-pilot':
                raise OSError('模拟保存失败')
            return original_copy(source, destination, **kwargs)
        with patch.object(operations, 'copytree', side_effect=fail_base):
            with self.assertRaisesRegex(ValueError, '恢复原样'):
                operations.sync_bundled_skill(self.home)
        self.assertEqual(self.fixture.fingerprint(), before)

    def test_dashboard_start_and_resumed_upgrade_sync_the_skill(self):
        dashboard = server.Dashboard({'home': str(self.home)})
        try:
            self.assertEqual(self.current.read_bytes(), self.incoming.read_bytes())
            session = runtime.snapshot(dashboard)
            session['socket_fd'] = dashboard.socket.dup().detach()
        finally:
            dashboard.server_close()
        self.incoming.write_text(self.incoming.read_text() + '又一次升级。\n')
        resumed = server.Dashboard({'home': str(self.home)}, session)
        try:
            self.assertEqual(self.current.read_bytes(), self.incoming.read_bytes())
            self.assertEqual(resumed.token, session['token'])
        finally:
            resumed.server_close()

    def test_sync_failure_keeps_dashboard_available_and_reports_error(self):
        with patch.object(operations, 'sync_bundled_skill', side_effect=ValueError('模拟写入失败')):
            dashboard = server.Dashboard({'home': str(self.home)})
        try:
            self.assertGreater(dashboard.server_port, 0)
            self.assertIn('自带技能说明没能同步', dashboard.program_progress['error'])
            self.assertEqual(self.current.read_bytes(), self.original)
        finally:
            dashboard.server_close()

    def test_skill_only_update_changes_runtime_version(self):
        package = self.project / 'skill_pilot'
        package.mkdir()
        (package / '__init__.py').write_text('')
        with patch.object(runtime, 'PACKAGE', package):
            before = runtime.version()
            self.incoming.write_text(self.incoming.read_text() + '仅修改技能说明。\n')
            self.assertNotEqual(runtime.version(), before)
