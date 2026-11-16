import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import agents, external, operations, sharing, store, team
from skill_pilot.scan import content_digest
from skill_pilot.sync import visibility


class ExternalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot external ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'New User')
        for name in ('app-skill', 'ordinary'):
            path = self.home / '.codex/skills' / name
            path.mkdir(parents=True)
            (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: original\n---\n')
        store.apply(store.preview(self.home)['plan_id'], self.home)
        self.app = self.root / 'Example App.app/Resources/skills/app-skill'
        shutil.copytree(self.home / '.skill-pilot/store/app-skill', self.app)
        self.entry = self.home / '.codex/skills/app-skill'
        self.entry.unlink()
        self.entry.symlink_to(self.app, target_is_directory=True)

    def test_release_preserves_entries_visibility_and_external_content_and_undo(self):
        before = visibility(self.home)
        registry = (self.home / '.skill-pilot/registry.json').read_bytes()
        link = os.readlink(self.entry)
        app = content_digest(self.app)
        view = external.release('app-skill', home=self.home)
        self.assertTrue(view['needs_confirmation'])
        self.assertEqual((self.home / '.skill-pilot/registry.json').read_bytes(), registry)
        result = external.release('app-skill', True, self.home, view['revision'], view['plan_id'])
        self.assertEqual(visibility(self.home), before)
        self.assertEqual(os.readlink(self.entry), link)
        self.assertEqual(content_digest(self.app), app)
        for rel in ('store/app-skill', 'bases/app-skill', 'store/.skill-pilot-state/app-skill.json'):
            self.assertFalse((self.home / '.skill-pilot' / rel).exists())
        table = operations.table(self.home)
        row = next(s for s in table['skills'] if s['name'] == 'app-skill')
        self.assertTrue(row['read_only'])
        self.assertEqual(row['owner'], 'Example App')
        self.assertEqual(table['new_skills'], [])
        self.assertNotIn('app-skill', operations.load(self.home)[1]['skills'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual((self.home / '.skill-pilot/registry.json').read_bytes(), registry)
        self.assertTrue((self.home / '.skill-pilot/store/app-skill/SKILL.md').is_file())
        self.assertEqual(visibility(self.home), before)
        self.assertEqual(os.readlink(self.entry), link)
        self.assertEqual(content_digest(self.app), app)

    def test_external_upgrade_missing_entry_and_unrelated_assignment(self):
        external.release('app-skill', True, self.home)
        operations.apply_assign([{'skill': 'ordinary', 'agent': 'claude', 'enabled': True}], confirmed=True, home=self.home)
        (self.app / 'SKILL.md').write_text('---\nname: app-skill\ndescription: upgraded\nmetadata:\n  version: "2.0"\n---\n')
        row = next(s for s in operations.table(self.home)['skills'] if s['name'] == 'app-skill')
        self.assertEqual(row['description'], 'upgraded')
        self.entry.unlink()
        row = next(s for s in operations.table(self.home)['skills'] if s['name'] == 'app-skill')
        self.assertTrue(row['external_missing'])
        self.assertEqual(row['assigned_agents'], [])
        self.assertIn('ordinary', visibility(self.home)['claude']['available'])

    def test_release_shared_skill_keeps_disable_through_writes_rollback_and_undo(self):
        self.entry.unlink()
        self.entry.symlink_to(self.home / '.skill-pilot/store/app-skill', target_is_directory=True)
        changes = [{'skill': name, 'agent': agent, 'enabled': enabled}
                   for name in ('app-skill', 'ordinary')
                   for agent, enabled in (('codex', False), ('dsh', True))]
        operations.apply_assign(changes, confirmed=True, home=self.home)
        entry = self.home / '.agents/skills/app-skill'
        entry.unlink()
        entry.symlink_to(self.app, target_is_directory=True)
        config = self.home / '.codex/config.toml'
        registry_path = self.home / '.skill-pilot/registry.json'
        original_config, original_registry = config.read_bytes(), registry_path.read_bytes()
        before = visibility(self.home)
        with patch.object(operations, '_commit', side_effect=OSError('cannot commit')):
            with self.assertRaisesRegex(ValueError, '恢复原样'):
                external.release('app-skill', True, self.home)
        self.assertEqual(config.read_bytes(), original_config)
        self.assertEqual(registry_path.read_bytes(), original_registry)
        released = external.release('app-skill', True, self.home)
        self.assertEqual(visibility(self.home), before)
        fragments = operations.load(self.home)[1]['codex_config']['fragments']
        self.assertNotIn('app-skill', fragments)
        self.assertIn('ordinary', fragments)
        assigned = operations.apply_assign([{'skill': 'ordinary', 'agent': 'claude', 'enabled': True}],
                                           confirmed=True, home=self.home)
        enabled = operations.apply_assign([{'skill': 'ordinary', 'agent': 'codex', 'enabled': True}],
                                          confirmed=True, home=self.home)
        self.assertNotIn('app-skill', visibility(self.home)['codex']['available'])
        self.assertIn('app-skill', visibility(self.home)['dsh']['available'])
        self.assertIn('ordinary', visibility(self.home)['claude']['available'])
        for result in (enabled, assigned, released):
            operations.undo(result['id'], home=self.home)
        self.assertEqual(config.read_bytes(), original_config)
        self.assertEqual(registry_path.read_bytes(), original_registry)
        self.assertEqual(visibility(self.home), before)

    def test_store_reference_missing_source_changed_preview_and_readonly_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '库内副本'):
            external.release('ordinary', True, self.home)
        view = external.preview('app-skill', self.home)
        (self.app / 'SKILL.md').write_text('---\nname: app-skill\ndescription: changed\n---\n')
        with self.assertRaisesRegex(ValueError, '已经变化'):
            external.release('app-skill', True, self.home, view['revision'], view['plan_id'])
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertTrue(external.release('app-skill', home=self.home)['needs_confirmation'])
            with self.assertRaisesRegex(ValueError, '只读'):
                external.release('app-skill', True, self.home)
        self.entry.unlink()
        with self.assertRaisesRegex(ValueError, '外部副本'):
            external.release('app-skill', True, self.home)
        self.assertTrue((self.home / '.skill-pilot/store/app-skill').is_dir())

    def test_failed_release_restores_copies_and_keeps_app_untouched(self):
        original = operations._commit
        def fail(home, registry, names, content, message):
            if '退出托管' in message:
                raise OSError('test failure')
            return original(home, registry, names, content, message)
        before = visibility(self.home)
        with patch.object(operations, '_commit', side_effect=fail):
            with self.assertRaisesRegex(ValueError, '恢复原样'):
                external.release('app-skill', True, self.home)
        self.assertEqual(visibility(self.home), before)
        self.assertNotIn('app-skill', operations.load(self.home)[1].get('external_skills', {}))
        self.assertTrue((self.home / '.skill-pilot/store/app-skill').is_dir())
        self.assertEqual(self.entry.resolve(), self.app)

    def test_in_home_external_copy_is_not_reimported_on_enrollment_or_install(self):
        target = self.home / 'Applications/Other App.app/skills/app-skill'
        shutil.copytree(self.app, target)
        self.entry.unlink()
        self.entry.symlink_to(target, target_is_directory=True)
        external.release('app-skill', True, self.home)
        with self.assertRaisesRegex(ValueError, '外部工具维护'):
            operations.install(operations.inspect_local(str(target)), ['app-skill'], home=self.home)
        copied = self.home / '.cursor/skills/app-skill'
        shutil.copytree(target, copied)
        view = agents.preview_join(['cursor'], self.home)
        self.assertFalse(any(s['name'] == 'app-skill' for s in view['skills']))
        self.assertTrue(any('外部工具维护' in s['reason'] for s in view['skipped']))
        self.assertEqual(operations.table(self.home)['new_skills'], [])
        registry = operations.load(self.home)[1]
        self.assertEqual(team._local({'name': 'app-skill', 'source': {'kind': 'github'}}, registry), ('external', 'app-skill'))

    def test_external_folder_is_preserved_even_if_application_updates_during_rollback(self):
        self.entry.unlink()
        shutil.copytree(self.app, self.entry)
        result = external.release('app-skill', True, self.home)
        def fail(*args, **kwargs):
            (self.entry / 'SKILL.md').write_text('---\nname: app-skill\ndescription: updated by app\n---\n')
            raise OSError('cannot commit')
        with patch.object(operations, '_commit', side_effect=fail):
            with self.assertRaises(ValueError):
                operations.apply_assign([{'skill': 'ordinary', 'agent': 'claude', 'enabled': True}], confirmed=True, home=self.home)
        self.assertIn('updated by app', (self.entry / 'SKILL.md').read_text())
        self.assertNotIn('ordinary', visibility(self.home)['claude']['available'])
        with self.assertRaisesRegex(ValueError, '外部改动'):
            operations.undo(result['id'], home=self.home)
        self.assertIn('updated by app', (self.entry / 'SKILL.md').read_text())

    def test_dependent_requires_external_skill_available_for_target_agent(self):
        registry = operations.load(self.home)[1]
        registry['skills']['ordinary']['requires'] = ['app-skill']
        (self.home / '.skill-pilot/registry.json').write_text(json.dumps(registry))
        operations._commit(self.home, registry, ['ordinary'], [], 'record dependency')
        external.release('app-skill', True, self.home)
        operations.apply_assign([{'skill': 'ordinary', 'agent': 'codex', 'enabled': True}], confirmed=True, home=self.home)
        with self.assertRaisesRegex(ValueError, '外部工具维护'):
            operations.apply_assign([{'skill': 'ordinary', 'agent': 'claude', 'enabled': True}], confirmed=True, home=self.home)
        exported = sharing.export_config(self.home)
        row = next(s for s in exported['skills'] if s['name'] == 'app-skill')
        self.assertIn('外部工具维护', row['source']['reason'])
        imported = sharing.preview_import(exported, home=self.home)
        self.assertNotIn('app-skill', imported['install'])

    def test_install_keeps_external_dependency_and_checks_target_agent(self):
        external.release('app-skill', True, self.home)
        path = self.root / 'Downloaded dependent'
        path.mkdir()
        (path / 'SKILL.md').write_text('---\nname: dependent\ndescription: needs app-skill\n---\n')
        review = operations.inspect_local(str(path), self.home)
        with patch.object(operations, 'curated_dependencies', return_value=({'dependent': {'app-skill'}}, {})):
            with self.assertRaisesRegex(ValueError, '外部工具维护'):
                operations.install(review, ['dependent'], ['claude'], home=self.home)
            self.assertNotIn('dependent', operations.load(self.home)[1]['skills'])
            self.assertFalse((self.home / '.skill-pilot/store/dependent').exists())
            installed = operations.install(review, ['dependent'], ['codex'], home=self.home)
            saved = json.loads((self.home / '.skill-pilot/registry.json').read_text())
            self.assertEqual(saved['skills']['dependent']['requires'], ['app-skill'])
            self.assertIn('dependent', visibility(self.home)['codex']['available'])
            operations.undo(installed['id'], home=self.home)
            operations.install(review, ['dependent'], home=self.home)
            with self.assertRaisesRegex(ValueError, '外部工具维护'):
                operations.apply_assign([{'skill': 'dependent', 'agent': 'claude', 'enabled': True}],
                                        confirmed=True, home=self.home)
        self.assertFalse((self.home / '.skill-pilot/store/app-skill').exists())
        self.assertEqual(self.entry.resolve(), self.app)

    def test_load_includes_external_dependencies_and_respects_rejection(self):
        external.release('app-skill', True, self.home)
        path = self.home / '.skill-pilot/registry.json'
        original = path.read_bytes()
        with patch.object(operations, 'curated_dependencies', return_value=({'ordinary': {'app-skill'}}, {})):
            self.assertEqual(operations.load(self.home)[1]['skills']['ordinary']['requires'], ['app-skill'])
            self.assertEqual(path.read_bytes(), original)
            registry = json.loads(original)
            registry['skills']['ordinary']['dependency_decisions'] = {'app-skill': 'rejected'}
            path.write_text(json.dumps(registry))
            self.assertEqual(operations.load(self.home)[1]['skills']['ordinary']['requires'], [])

    def test_cli_preview_release_and_later_operation_undo_order(self):
        env = {**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        command = [sys.executable, '-B', '-m', 'skill_pilot', 'release', 'app-skill', '--json']
        preview = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 2, preview.stderr)
        result = subprocess.run([*command, '--yes'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        operation = json.loads(result.stdout)['id']
        later = operations.apply_assign([{'skill': 'ordinary', 'agent': 'claude', 'enabled': True}], confirmed=True, home=self.home)
        with self.assertRaisesRegex(ValueError, '先撤销后来的操作'):
            operations.undo(operation, home=self.home)
        operations.undo(later['id'], home=self.home)
        operations.undo(operation, home=self.home)
        self.assertTrue((self.home / '.skill-pilot/store/app-skill').is_dir())
