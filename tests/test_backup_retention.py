"""Backup lifetime and exact undo, using synthetic homes with spaces."""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

from skill_pilot import operations, store
from tests import test_operations, test_update_batch


class BackupRetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_operations.OperationsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        self.assertIn(' ', str(self.home))
        self.root = self.home / '.skill-pilot/backups'
        self.organize = store.backups(self.home)[0]['backup_id']

    def manifests(self):
        return {p.parent.name: json.loads(p.read_text()) for p in self.root.glob('*/manifest.json')}

    def change(self):
        assigned = operations.load(self.home)[1]['skills']['other']['assigned_agents']
        return self.fixture.assign('other', 'claude', 'claude' not in assigned)

    def legacy(self, backup_id, paths):
        directory = self.root / backup_id
        directory.mkdir()
        (directory / 'manifest.json').write_text(json.dumps({'backup_id': backup_id, 'paths': paths}))
        with tarfile.open(directory / 'files.tar', 'w'):
            pass
        return directory

    def command(self, *args, read_only=False):
        return subprocess.run([sys.executable, '-m', 'skill_pilot', *args], cwd=store.PROJECT,
                              env={**os.environ, 'HOME': str(self.home), 'SKILL_PILOT_HOME': str(self.home),
                                   'SKILL_PILOT_READ_ONLY': '1' if read_only else '0',
                                   'PYTHONDONTWRITEBYTECODE': '1'},
                              capture_output=True, text=True, timeout=20)

    def test_fifteen_changes_keep_ten_and_each_undo_restores_exact_state(self):
        states, results = [], []
        incoming = self.home / 'local changes'
        incoming.mkdir()
        for index in range(15):
            states.append(self.fixture.fingerprint())
            if index % 3 == 0:
                self.fixture.skill('local changes', 'other', '新版 ' + str(index))
                result = operations.update_local('other', incoming, home=self.home, confirmed=True)
            else:
                result = self.change()
            results.append(result)
        self.assertEqual(set(self.manifests()), {self.organize, *(r['backup_id'] for r in results[-10:])})
        for result in results[:5]:
            before = store.physical_state(self.home)
            with self.assertRaisesRegex(ValueError, '^这次操作太早了，备份已经清掉，不能再撤销。$'):
                operations.undo(result['id'], home=self.home)
            self.assertEqual(store.physical_state(self.home), before)
        recent = operations.history(self.home)[:10]
        self.assertEqual([r['id'] for r in recent], [r['id'] for r in reversed(results[-10:])])
        for index in range(14, 4, -1):
            undone = operations.undo(results[index]['id'], home=self.home)
            self.assertEqual(self.fixture.fingerprint(), states[index])
            self.assertFalse((self.root / results[index]['backup_id']).exists())
            self.assertFalse((self.root / undone['backup_id']).exists())
        self.assertEqual(set(self.manifests()), {self.organize})

    def test_undo_only_takes_one_full_snapshot_and_discards_filtered_scratch(self):
        result = self.change()
        restore = operations._restore
        scratch_ids = []

        def inspect_scratch(home, backup_id, checked):
            manifest = self.manifests()[backup_id]
            self.assertEqual(manifest['kind'], 'undo')
            self.assertNotIn('.skill-pilot/store', manifest['paths'])
            self.assertNotIn('.skill-pilot/logs', manifest['paths'])
            with tarfile.open(self.root / backup_id / 'files.tar') as archive:
                self.assertFalse(any(m.name == p or m.name.startswith(p + '/') for m in archive.getmembers()
                                     for p in ('.skill-pilot/store', '.skill-pilot/logs')))
            scratch_ids.append(backup_id)
            return restore(home, backup_id, checked)

        with patch.object(operations, '_snapshot', wraps=operations._snapshot) as snapshot, \
                patch.object(operations, '_restore', side_effect=inspect_scratch):
            undone = operations.undo(result['id'], home=self.home)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(len(scratch_ids), 1)
        self.assertFalse((self.root / scratch_ids[0]).exists())
        self.assertFalse((self.root / undone['backup_id']).exists())
        self.assertEqual(set(self.manifests()), {self.organize})

    def test_failed_scratch_restore_is_discarded_and_operation_rolls_back(self):
        result = self.change()
        before = self.fixture.fingerprint()
        restore = operations._restore

        def fail_scratch(home, backup_id, checked):
            if self.manifests()[backup_id]['kind'] == 'undo':
                raise OSError('injected scratch restore failure')
            return restore(home, backup_id, checked)

        with patch.object(operations, '_restore', side_effect=fail_scratch):
            with self.assertRaisesRegex(ValueError, '已经自动恢复原样'):
                operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fixture.fingerprint(), before)
        self.assertEqual(set(self.manifests()), {self.organize, result['backup_id']})

    def test_first_change_prunes_legacy_operations_and_scratch_only(self):
        manifest_path = self.root / self.organize / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest.pop('kind')
        manifest_path.write_text(json.dumps(manifest))
        results = []
        with patch.object(operations, '_prune_backups'):
            for _ in range(12):
                result = self.change()
                path = self.root / result['backup_id'] / 'manifest.json'
                value = json.loads(path.read_text())
                value.pop('kind')
                path.write_text(json.dumps(value))
                results.append(result)
        scratch = self.legacy('20000101T000000000001', ['.skill-pilot/registry.json'])
        orphan = self.legacy('20000101T000000000002', ['.skill-pilot/store', '.skill-pilot/logs'])
        absorbed = self.root / 'absorbed-20000101T000000000000'
        absorbed.mkdir()
        (absorbed / 'original.txt').write_text('keep the original')
        incomplete = self.root / '20000101T000000000003'
        incomplete.mkdir()
        (incomplete / 'files.tar').write_text('no manifest')
        named = self.legacy('unmanaged-folder', ['.skill-pilot/store', '.skill-pilot/logs'])
        untouched = {p: store.physical_state(p) for p in (absorbed, incomplete, named)}
        current = self.change()
        self.assertEqual(set(self.manifests()), {self.organize, 'unmanaged-folder', current['backup_id'],
                                                *(r['backup_id'] for r in results[-9:])})
        self.assertFalse(scratch.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual({p: store.physical_state(p) for p in untouched}, untouched)
        self.assertEqual([m['backup_id'] for m in store.backups(self.home)], [self.organize])

    def test_successful_rollback_discards_failed_operation_backup(self):
        before, backups = self.fixture.fingerprint(), self.manifests()
        with patch.object(operations, '_validate', side_effect=OSError('injected verification failure')):
            with self.assertRaisesRegex(ValueError, '已经自动恢复原样'):
                self.change()
        self.assertEqual(self.fixture.fingerprint(), before)
        self.assertEqual(self.manifests(), backups)
        self.assertEqual(operations.history(self.home), [])

    def test_batch_expires_as_one_group_after_ten_later_operations(self):
        run = operations._run
        with patch.object(operations, '_run', side_effect=lambda *a, **kw: run(*a, **kw, batch='older batch')):
            batch = [self.change() for _ in range(12)]
        for _ in range(9):
            self.change()
        self.assertTrue(all((self.root / r['backup_id']).is_dir() for r in batch))
        self.change()
        self.assertTrue(all(not (self.root / r['backup_id']).exists() for r in batch))
        self.assertEqual(len(self.manifests()), 11)

    def test_failed_rollback_backup_stays_after_fifteen_more_changes(self):
        home, registry, agents = operations.load(self.home)

        def fail(current, checked):
            raise OSError('injected operation failure')

        with patch.object(operations, '_restore', side_effect=OSError('injected rollback failure')):
            with self.assertRaises(operations.RollbackError):
                operations._run(home, registry, agents, operations.revision(home, agents), '失败测试', fail)
        retained = next(m for m in self.manifests().values() if m.get('preserve'))
        fingerprint = store.physical_state(self.root / retained['backup_id'])
        for _ in range(15):
            self.change()
        self.assertEqual(store.physical_state(self.root / retained['backup_id']), fingerprint)
        self.assertEqual(len(self.manifests()), 12)

    def test_cleanup_error_logs_warning_without_undoing_success(self):
        orphan = self.legacy('20000101T000000000001', ['.skill-pilot/logs'])
        with patch.object(operations, 'remove', side_effect=PermissionError('injected cleanup failure')), \
                self.assertLogs('skill_pilot.operations', level='WARNING') as warnings:
            result = self.change()
        self.assertIn('旧备份没能清理', warnings.output[0])
        self.assertTrue(orphan.exists())
        self.assertEqual(operations.history(self.home)[0]['id'], result['id'])
        self.assertIn('claude', operations.load(self.home)[1]['skills']['other']['assigned_agents'])
        self.change()
        self.assertFalse(orphan.exists())

    def test_deletion_guard_rejects_backup_pointing_outside_home(self):
        outside = self.home.parent / (self.home.name + ' outside')
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside)
        (outside / 'keep').write_text('do not delete')
        link = self.root / '20000101T000000000001'
        link.symlink_to(outside, target_is_directory=True)
        before = store.physical_state(outside)
        with self.assertLogs('skill_pilot.operations', level='WARNING'):
            operations._discard_backup(link, lambda p: store.guard(p, self.home))
        self.assertEqual(store.physical_state(outside), before)
        self.assertTrue(link.is_symlink())

    def test_damaged_manifests_do_not_block_other_cleanup(self):
        iterdir = type(self.root).iterdir
        for index, content in enumerate(('{"paths":', '{}',
                '{"kind":"future-kind","paths":[".skill-pilot/logs"]}',
                '{"kind":"operation","paths":".skill-pilot/logs"}',
                '{"kind":null,"paths":[".skill-pilot/logs"]}',
                '{"kind":"","paths":[".skill-pilot/logs"]}',
                '{"kind":false,"paths":[".skill-pilot/logs"]}',
                '{"kind":0,"paths":[".skill-pilot/logs"]}',
                '{"kind":[],"paths":[".skill-pilot/logs"]}',
                '{"kind":{},"paths":[".skill-pilot/logs"]}')):
            with self.subTest(manifest=content):
                damaged = self.legacy(f'20000101T{index * 2 + 1:012}', ['.skill-pilot/logs'])
                (damaged / 'manifest.json').write_text(content)
                expired = self.legacy(f'20000101T{index * 2 + 2:012}', ['.skill-pilot/logs'])
                before = store.physical_state(damaged)

                def damaged_first(path):
                    entries = iterdir(path)
                    return iter(sorted(entries, key=lambda p: p != damaged)) if path == self.root else entries

                with patch.object(type(self.root), 'iterdir', damaged_first), \
                        self.assertLogs('skill_pilot.operations', level='WARNING') as warnings:
                    result = self.change()
                self.assertEqual(store.physical_state(damaged), before)
                self.assertFalse(expired.exists())
                self.assertTrue(any(damaged.name in warning for warning in warnings.output))
                self.assertEqual(operations.history(self.home)[0]['id'], result['id'])
                self.assertEqual('claude' in operations.load(self.home)[1]['skills']['other']['assigned_agents'],
                                 index % 2 == 0)
                self.assertTrue((self.root / result['backup_id'] / 'files.tar').is_file())

    def test_cli_lists_only_organize_and_readonly_commands_never_prune(self):
        self.change()
        self.legacy('20000101T000000000001', ['.skill-pilot/logs'])
        before = store.physical_state(self.home)
        for command in [('backups', '--json'), ('backups',), ('log', '--json'), ('status', '--json')]:
            result = self.command(*command, read_only=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            if command == ('backups', '--json'):
                self.assertEqual([m['backup_id'] for m in json.loads(result.stdout)], [self.organize])
            elif command == ('backups',):
                self.assertEqual(result.stdout.strip(), self.organize + '：整理前的备份')
        self.assertEqual(store.physical_state(self.home), before)
        denied = self.command('undo', operations.history(self.home)[0]['id'], read_only=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('只读', denied.stderr + denied.stdout)
        self.assertEqual(store.physical_state(self.home), before)


class UpdateBatchRetentionTests(unittest.TestCase):
    def test_twelve_updates_stay_together_and_undo_exactly(self):
        # 夹具目录名里已经有空格。不要把全进程的临时目录都换成同一个：
        # Windows 整理前会另建一个临时目录检查链接，用完就删，换掉之后会把夹具主目录一起删掉。
        fixture = test_update_batch.UpdateBatchTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertIn(' ', str(fixture.home))
        for index in range(11):
            fixture.add_skill('extra-' + str(index))
        before = fixture.fingerprint()
        review = fixture.review()
        names = [row['name'] for row in review['skills'] if row['has_update']]
        self.assertEqual(len(names), 12)
        result = test_update_batch.updates.apply(review, names, confirmed=True)
        self.assertTrue(result['complete'], result)
        self.assertEqual(len(result['operations']), 12)
        logs = {r['id']: r for r in operations.history(fixture.home)}
        self.assertEqual(len({logs[r['id']]['batch'] for r in result['operations']}), 1)
        for operation in result['operations']:
            self.assertTrue((fixture.home / '.skill-pilot/backups' / operation['backup_id']).is_dir())
        fixture.undo(result)
        self.assertEqual(fixture.fingerprint(), before)
        for operation in result['operations']:
            self.assertFalse((fixture.home / '.skill-pilot/backups' / operation['backup_id']).exists())
