"""Reviewed external copies can be adopted without losing sources or readers."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, profiles, store
from skill_pilot.sync import visibility
from skill_pilot.web.server import preview_absorb
from tests import set_up_home, test_cli_sharing, test_jobs, test_operations


class AbsorbConflictTests(unittest.TestCase):
    setUp = test_operations.OperationsTests.setUp
    skill = test_operations.OperationsTests.skill
    fingerprint = test_operations.OperationsTests.fingerprint
    assign = test_operations.OperationsTests.assign

    def external(self, name='other', relative=None):
        path = self.home / (relative or '.agents/skills/' + name)
        if path.is_symlink():
            path.unlink()
        return self.skill(str(path.relative_to(self.home)), name, '外部版本')

    def adopt(self, review, choices):
        return operations.absorb(all_skills=True, expected=review['revision'], choices=choices, home=self.home)

    def test_adopt_incoming_keeps_source_base_and_exact_undo(self):
        registry_path = self.home / '.skill-pilot/registry.json'
        registry = json.loads(registry_path.read_text())
        registry['skills']['other']['source'] = {'source': 'https://open.feishu.cn/lark-cli/skills/regular/.well-known/agent-skills/other.tar.gz'}
        registry_path.write_text(json.dumps(registry))
        original = copy.deepcopy(registry['skills']['other'])
        incoming = self.external()
        (incoming / 'guide.md').write_text('新增的参考文件')
        alias = self.home / '.claude/skills/other'
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(incoming, target_is_directory=True)
        before = self.fingerprint()
        review = preview_absorb(self.home)
        self.assertIn('guide.md', review['conflicts'][0]['options'][1]['comparison'])
        self.assertEqual(self.fingerprint(), before)
        result = self.adopt(review, {'other': '.agents/skills/other'})
        updated = operations.load(self.home)[1]['skills']['other']
        self.assertEqual(updated['source'], original['source'])
        self.assertEqual(updated['base_digest'], original['base_digest'])
        self.assertEqual(store.physical_state(self.home / '.skill-pilot/bases'), before['.skill-pilot/bases'])
        self.assertTrue(next(s for s in operations.table(self.home)['skills'] if s['name'] == 'other')['local_modified'])
        self.assertEqual((alias / 'guide.md').read_text(), '新增的参考文件')
        self.assertIn('other', visibility(self.home)['claude']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_keep_store_and_undo_restores_external_copy(self):
        incoming = self.external()
        before = self.fingerprint()
        old = (self.home / '.skill-pilot/store/other/SKILL.md').read_bytes()
        result = self.adopt(preview_absorb(self.home), {'other': 'store'})
        self.assertTrue(incoming.is_symlink())
        self.assertEqual((incoming / 'SKILL.md').read_bytes(), old)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_three_versions_choose_specific_copy(self):
        self.external()
        second = self.external(relative='.claude/skills/other')
        (second / 'SKILL.md').write_text((second / 'SKILL.md').read_text() + '\n第三份')
        review = preview_absorb(self.home)
        self.assertEqual(len(review['conflicts']), 1)
        self.assertEqual(len(review['conflicts'][0]['options']), 3)
        self.adopt(review, {'other': '.claude/skills/other'})
        self.assertIn('第三份', (self.home / '.skill-pilot/store/other/SKILL.md').read_text())
        self.assertEqual(operations.table(self.home)['changed_skills'], [])

    def test_new_conflicting_copies_can_be_resolved(self):
        self.external('fresh')
        self.skill('.claude/skills/fresh', 'fresh', '另一份')
        before = self.fingerprint()
        review = preview_absorb(self.home)
        self.assertNotIn('store', [o['value'] for o in review['conflicts'][0]['options']])
        with patch.object(operations.github, 'verify_source', return_value={'source': '自制'}):
            result = self.adopt(review, {'fresh': '.claude/skills/fresh'})
        self.assertIn('另一份', (self.home / '.skill-pilot/store/fresh/SKILL.md').read_text())
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_missing_revision_wrong_path_and_partial_choices_do_not_write(self):
        self.external()
        self.external('grilling')
        review = preview_absorb(self.home)
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '核对编号'):
            operations.absorb(True, home=self.home, choices={'other': 'store'})
        for choices in ({'other': '.agents/skills/grilling'}, {'unknown': 'store'}, {'other': '../outside'}):
            with self.assertRaises(ValueError):
                self.adopt(review, choices)
        result = self.adopt(review, {'other': 'store'})
        self.assertTrue(result['conflicts'])
        self.assertNotIn('id', result)
        self.assertEqual(self.fingerprint(), before)

    def test_changed_content_or_mode_invalidates_review(self):
        incoming = self.external()
        for change in (('content',) if os.name == 'nt' else ('content', 'mode')):
            review = preview_absorb(self.home)
            file = incoming / 'SKILL.md'
            if change == 'content':
                file.write_text(file.read_text() + '\n再次改变')
            else:
                file.chmod(0o755)
            before = self.fingerprint()
            with self.assertRaisesRegex(ValueError, '刷新'):
                self.adopt(review, {'other': '.agents/skills/other'})
            self.assertEqual(self.fingerprint(), before)

    def test_failed_adoption_restores_content_copies_and_links(self):
        self.external()
        review = preview_absorb(self.home)
        before = self.fingerprint()
        with patch.object(operations, '_validate', side_effect=ValueError('injected failure')):
            with self.assertRaises(ValueError):
                self.adopt(review, {'other': '.agents/skills/other'})
        self.assertEqual(self.fingerprint(), before)

    def test_preview_exposes_empty_file_and_executable_changes(self):
        incoming = self.external()
        original = self.home / '.skill-pilot/store/other/SKILL.md'
        (incoming / 'SKILL.md').write_bytes(original.read_bytes())
        (incoming / 'SKILL.md').chmod(0o755)
        (incoming / 'empty.md').touch()
        review = preview_absorb(self.home)
        comparison = review['conflicts'][0]['options'][1]['comparison']
        self.assertIn('empty.md（新增）', comparison)
        if os.name != 'nt':
            self.assertIn('执行权限不同', comparison)

    def test_readonly_preview_and_cli_review_apply_undo(self):
        self.external()
        before = self.fingerprint()
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertTrue(preview_absorb(self.home)['conflicts'])
            with self.assertRaises(ValueError):
                self.adopt(preview_absorb(self.home), {'other': 'store'})
        preview = test_cli_sharing.run(self.home, 'absorb', '--all', '--json')
        self.assertEqual(preview.returncode, 2, preview.stderr + preview.stdout)
        review = json.loads(preview.stdout)
        self.assertEqual(self.fingerprint(), before)
        result = test_cli_sharing.run(self.home, 'absorb', '--path', '.agents/skills/other',
                                      '--revision', review['revision'], '--choose', 'other=.agents/skills/other', '--yes', '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        operations.undo(json.loads(result.stdout)['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)


class AbsorbHttpTests(unittest.TestCase):
    setUp = test_jobs.JobTests.setUp
    close = test_jobs.JobTests.close
    skill = test_jobs.JobTests.skill
    registry = test_jobs.JobTests.registry
    request = test_jobs.JobTests.request
    ok = test_jobs.JobTests.ok

    def test_dashboard_preview_apply_stale_check_and_undo(self):
        incoming = self.home / '.agents/skills/acme-b'
        incoming.unlink(missing_ok=True)
        self.skill(incoming, 'acme-b', '外部版本')
        original = (incoming / 'SKILL.md').read_bytes()
        review = self.ok('/api/absorb/preview', {})
        result = self.ok('/api/absorb', {'all': True, 'revision': review['revision'],
                         'choices': {'acme-b': '.agents/skills/acme-b'}, 'confirmed': True})
        self.assertEqual((self.home / '.skill-pilot/store/acme-b/SKILL.md').read_bytes(), original)
        self.ok('/api/undo', {'id': result['id'], 'revision': operations.table(self.home)['revision'], 'confirmed': True})
        self.assertFalse(incoming.is_symlink())
        review = self.ok('/api/absorb/preview', {})
        (incoming / 'SKILL.md').write_bytes(original + b'\nchanged')
        status, response = self.request('/api/absorb', {'all': True, 'revision': review['revision'],
                                       'choices': {'acme-b': 'store'}, 'confirmed': True})
        self.assertEqual(status, 409, response)
        self.assertIn('刷新', response['error'])


class FullPruningTests(unittest.TestCase):
    skill = test_operations.OperationsTests.skill

    def test_four_conflicts_then_two_assistants_keep_nine_and_undo(self):
        with tempfile.TemporaryDirectory(prefix='skill pilot four copies ') as temporary:
            set_up_home(self, Path(temporary) / 'Example Home')
            agents = ['codex', 'claude', 'dsh', 'cursor', 'doubaowork', 'antigravity']
            names = [f'demo-{i:02d}' for i in range(28)]
            catalog = profiles.by_id()
            for agent in agents:
                (self.home / catalog[agent]['link_root']).mkdir(parents=True, exist_ok=True)
            for name in names:
                self.skill('.agents/skills/' + name, name)
            for name in (names[3], names[11]):
                self.skill(catalog['doubaowork']['link_root'] + '/' + name, name)
            (self.home / '.codex/config.toml').write_text('[skills]\nconfig = [{name="demo-00",enabled=false}]\n')
            plan = store.preview(self.home, agents)
            store.apply(plan['plan_id'], self.home, agents)
            registry_path = self.home / '.skill-pilot/registry.json'
            registry = json.loads(registry_path.read_text())
            for name in names:
                registry['skills'][name]['requires'] = [names[11]] if name != names[11] else []
            registry_path.write_text(json.dumps(registry))
            for index, name in enumerate(names):
                incoming = self.home / '.agents/skills' / name
                incoming.unlink()
                shutil.copytree(self.home / '.skill-pilot/store' / name, incoming)
                if 1 <= index <= 4:
                    (incoming / 'guide.md').write_text('外部新增参考资料')
                (self.home / '.claude/skills' / name).symlink_to('../../.agents/skills/' + name, target_is_directory=True)
            paths = {r for a in agents for r in catalog[a]['skill_roots']} | {'.codex/config.toml', '.skill-pilot/registry.json', '.skill-pilot/bases'}
            def fingerprint():
                return {p: store.physical_state(self.home / p) for p in paths} | {'store': operations.content_digest(self.home / '.skill-pilot/store')}
            before = fingerprint()
            actual_before = visibility(self.home)
            self.assertEqual(len(actual_before['codex']['available']), 27)
            self.assertEqual(len(actual_before['dsh']['available']), 28)
            review = preview_absorb(self.home)
            self.assertEqual({c['name'] for c in review['conflicts']}, set(names[1:5]))
            result = operations.absorb(True, expected=review['revision'], home=self.home,
                                       choices={n: '.agents/skills/' + n for n in names[1:5]})
            keep = set(names[1:3] + names[5:12])
            changes = [{'skill': n, 'agent': a, 'enabled': False} for a in ('codex', 'dsh') for n in names if n not in keep]
            applied = operations.apply_assign(changes, confirmed=True, home=self.home)
            actual = visibility(self.home)
            for agent in ('codex', 'dsh'):
                self.assertEqual(set(actual[agent]['available']), keep)
            for agent in ('claude', 'cursor', 'doubaowork', 'antigravity'):
                self.assertEqual(actual[agent]['available'], actual_before[agent]['available'])
            self.assertEqual(operations.table(self.home)['changed_skills'], [])
            operations.undo(applied['id'], home=self.home)
            operations.undo(result['id'], home=self.home)
            self.assertEqual(fingerprint(), before)
