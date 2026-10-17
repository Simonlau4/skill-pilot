import copy
import itertools
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_operations
from skill_pilot import agents, operations, profiles, store
from skill_pilot.sync import assignment_entries, disabled_lists, visibility
from skill_pilot.scan import scan


def original_assignment_entries(name, agents):
    """The shared root has exactly two readers: Codex and DSH."""
    agents = set(agents)
    entries = []
    if 'dsh' in agents:
        entries.append('.agents/skills/' + name)
    elif 'codex' in agents:
        entries.append('.codex/skills/' + name)
    for agent, root in [('claude', '.claude/skills'), ('antigravity', '.gemini/config/skills')]:
        if agent in agents:
            entries.append(root + '/' + name)
    return sorted(entries)


class PlacementTests(unittest.TestCase):
    def test_all_sixteen_original_combinations(self):
        ids = ['codex', 'claude', 'antigravity', 'dsh']
        for flags in itertools.product([False, True], repeat=4):
            selected = [a for a, enabled in zip(ids, flags) if enabled]
            with self.subTest(selected=selected):
                self.assertEqual(assignment_entries('sample', selected), original_assignment_entries('sample', selected))


class GenericAgentTests(unittest.TestCase):
    setUp = test_operations.OperationsTests.setUp
    skill = test_operations.OperationsTests.skill
    assign = test_operations.OperationsTests.assign

    def fingerprint(self):
        return test_operations.OperationsTests.fingerprint(self) | {
            r: store.physical_state(self.home / r) for r in ['.zcode/skills', '.joint/skills']}

    def incoming(self):
        for name in ['other', 'grilling']:
            shutil.copytree(self.home / '.skill-pilot/store' / name, self.home / '.zcode/skills' / name)
        (self.home / '.zcode/skills/grilling/SKILL.md').write_text('---\nname: grilling\ndescription: 示例技能\n---\n新 agent 版本\n')
        self.skill('.zcode/skills/novel', 'novel')
        invalid = self.home / '.zcode/skills/invalid'
        invalid.mkdir()
        (invalid / 'SKILL.md').write_text('invalid manifest')
        (self.home / '.zcode/skills/broken').symlink_to(self.home / 'missing')

    def join(self, choices=None):
        plan = agents.preview_join(['zcode'], self.home)
        return agents.join(['zcode'], plan['plan_id'], plan['revision'], choices, self.home)

    def indirect_incoming(self):
        self.skill('.agents/skills/x', 'x')
        with patch.object(operations.github, 'verify_source', return_value={'source': '来源不明'}):
            operations.absorb(home=self.home)
        operations.apply_assign([{'skill': 'x', 'agent': a, 'enabled': True}
                                 for a in ['claude', 'antigravity']], confirmed=True, home=self.home)
        path = self.home / '.zcode/skills/x'
        path.parent.mkdir(parents=True)
        path.symlink_to('../../.agents/skills/x', target_is_directory=True)
        return path

    def test_discover_join_assign_undo_leave_and_rejoin(self):
        self.incoming()
        self.assertIn('zcode', [p['id'] for p in agents.overview(self.home)['suggested']])
        before, original = self.fingerprint(), visibility(self.home)
        plan = agents.preview_join(['zcode'], self.home)
        self.assertEqual({s['action'] for s in plan['skills']}, {'link', 'choose', 'import'})
        self.assertEqual(len(plan['skipped']), 2)
        self.assertIn('所有 agent', plan['conflicts'][0]['reason'])
        self.assertIn('新 agent 版本', plan['conflicts'][0]['comparison'])
        result = self.join()
        current = visibility(self.home)
        self.assertEqual({a: current[a] for a in original}, original)
        self.assertEqual(current['zcode']['available'], ['grilling', 'novel', 'other'])
        self.assertTrue((self.home / '.zcode/skills/other').is_symlink())
        self.assertTrue((self.home / '.zcode/skills/broken').is_symlink())
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)
        self.join()
        self.assign('other', 'zcode', False)
        self.assertFalse((self.home / '.zcode/skills/other').exists())
        self.assign('other', 'zcode')
        self.assertTrue((self.home / '.zcode/skills/other').is_symlink())
        self.assertNotIn('last_used', operations.table(self.home)['skills'][0])
        point, visible = self.fingerprint(), visibility(self.home)
        ignored_before = profiles.state(self.home)['ignored']
        plan = agents.preview_leave('zcode', self.home)
        self.assertEqual(plan['message'], '不再管理 Zcode；它文件夹里的技能原样留着，照常能用。')
        left = agents.leave('zcode', plan['plan_id'], plan['revision'], self.home)
        self.assertEqual(left['message'], plan['message'])
        self.assertEqual(operations.history(self.home)[0]['message'], plan['message'])
        view = agents.overview(self.home)
        self.assertNotIn('zcode', [p['id'] for p in view['suggested']])
        self.assertEqual(view['ignored'], [{'id': 'zcode', 'name': 'Zcode', 'skill_roots': ['.zcode/skills'],
                                           'skill_count': 3, 'reason': '你之前选了忽略或不再管理。'}])
        self.assertEqual(profiles.state(self.home)['ignored'], ignored_before + ['zcode'])
        self.assertTrue((self.home / '.zcode/skills/other').is_symlink())
        self.assertNotIn('zcode', operations.load(self.home)[2])
        self.assertEqual(visibility(self.home), visible)
        self.assertTrue(all(not any(e.startswith('.zcode/') for e in s['entries']) for s in operations.load(self.home)[1]['skills'].values()))
        operations.undo(left['id'], home=self.home)
        self.assertEqual(self.fingerprint(), point)
        self.assertEqual(profiles.state(self.home)['ignored'], ignored_before)
        self.assertEqual(agents.overview(self.home)['ignored'], [])
        plan = agents.preview_leave('zcode', self.home)
        agents.leave('zcode', plan['plan_id'], plan['revision'], self.home)
        self.join()
        self.assertNotIn('zcode', profiles.state(self.home)['ignored'])
        self.assertEqual(agents.overview(self.home)['ignored'], [])
        self.assertEqual(visibility(self.home), visible)

    def test_choose_incoming_changes_the_shared_copy_and_undo_restores_it(self):
        self.incoming()
        before = self.fingerprint()
        plan = agents.preview_join(['zcode'], self.home)
        choice = next(o['id'] for s in plan['skills'] if s['name'] == 'grilling' for o in s['options'] if o['origin'] == 'incoming')
        result = self.join({'grilling': choice})
        self.assertIn('新 agent 版本', (self.home / '.codex/skills/grilling/SKILL.md').read_text())
        self.assertEqual(visibility(self.home)['codex']['versions']['grilling'], visibility(self.home)['zcode']['versions']['grilling'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_disabled_rules_and_shared_departure(self):
        registry = copy.deepcopy(operations.load(self.home)[1])
        registry['skills']['grilling'].update(assigned_agents=['dsh'], entries=['.agents/skills/grilling'])
        registry['skills']['other'].update(assigned_agents=[], entries=['.codex/skills/other'])
        registry['skills']['grill-me'].update(assigned_agents=[], entries=['.codex/skills/grill-me'])
        registry['skills']['third-unused'] = {'assigned_agents': [], 'entries': ['.codex/skills/third-unused']}
        self.assertEqual(disabled_lists(registry, self.home), {'codex': ['grilling']})
        operations.apply_assign([{'skill': 'other', 'agent': 'codex', 'enabled': False},
                                 {'skill': 'other', 'agent': 'dsh', 'enabled': True}], confirmed=True, home=self.home)
        point, before = self.fingerprint(), visibility(self.home)
        plan = agents.preview_leave('dsh', self.home)
        self.assertEqual(plan['lost_skills'], ['other'])
        result = agents.leave('dsh', plan['plan_id'], plan['revision'], self.home)
        after = visibility(self.home)
        self.assertEqual({a: after[a] for a in plan['remaining']}, {a: before[a] for a in plan['remaining']})
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), point)

    def test_two_custom_readers_must_join_together_and_share_assignments(self):
        self.skill('.joint/skills/shared', 'shared')
        local = profiles.state(self.home)
        local['custom'] = [profiles.custom(a, a.capitalize(), '.joint/skills') for a in ['one', 'two']]
        (self.home / '.skill-pilot/agents.json').write_text(json.dumps(local))
        point = self.fingerprint()
        self.assertEqual(agents.preview_join(['one'], self.home)['agents'], ['one', 'two'])
        self.assertEqual(self.fingerprint(), point)
        plan = agents.preview_join(['one', 'two'], self.home)
        agents.join(['one', 'two'], plan['plan_id'], home=self.home)
        point = self.fingerprint()
        with self.assertRaisesRegex(ValueError, 'One 和 Two 共用一个技能目录，没法只给 One'):
            self.assign('other', 'one')
        self.assertEqual(self.fingerprint(), point)

    def test_legacy_custom_profile_fields_are_ignored_without_rewriting(self):
        local = profiles.state(self.home)
        custom = profiles.custom('legacy', 'Legacy', '.legacy/skills')
        custom.update(usage={'reader': 'missing', 'roots': ['/retired/path']},
                      usage_note='old note', usage_reason='old reason')
        local['custom'] = [custom]
        path = self.home / '.skill-pilot/agents.json'
        path.write_text(json.dumps(local))
        before = path.read_bytes()
        profile = profiles.by_id(self.home)['legacy']
        self.assertEqual(set(profile), set(profiles.PROFILES[0]))
        for row in scan(self.home)['agents']:
            self.assertNotIn('usage', row)
            self.assertNotIn('usage_note', row)
            self.assertNotIn('usage_reason', row)
        self.assertEqual(path.read_bytes(), before)

    def test_builtin_departure_and_reenrollment_preserve_shared_readers(self):
        operations.apply_assign([{'skill': 'other', 'agent': 'codex', 'enabled': False},
                                 {'skill': 'other', 'agent': 'dsh', 'enabled': True}], confirmed=True, home=self.home)
        before = visibility(self.home)
        plan = agents.preview_leave('codex', self.home)
        agents.leave('codex', plan['plan_id'], home=self.home)
        after = visibility(self.home)
        self.assertEqual({a: after[a] for a in plan['remaining']}, {a: before[a] for a in plan['remaining']})
        plan = agents.preview_join(['codex'], self.home)
        agents.join(['codex'], plan['plan_id'], home=self.home)
        self.assertEqual(visibility(self.home), before)

    def test_preview_notes_come_from_actual_placement(self):
        self.assertEqual(operations.table(self.home)['notes'], ['Codex 和 DSH 共用一个技能目录：只给 DSH 的技能，会在 Codex 里自动停用。'])
        changes = [{'skill': 'other', 'agent': 'codex', 'enabled': False},
                   {'skill': 'other', 'agent': 'dsh', 'enabled': True}]
        self.assertEqual(operations.preview_assign(changes, self.home)['cell_notes']['other'], {'codex': 'Codex 里已自动停用'})

    def test_ignore_and_undo(self):
        self.incoming()
        point = self.fingerprint()
        plan = agents.preview_ignore('zcode', self.home)
        self.assertEqual(self.fingerprint(), point)
        result = agents.ignore('zcode', plan['plan_id'], plan['revision'], self.home)
        self.assertEqual(operations.history(self.home)[0]['agents'], ['zcode'])
        view = agents.overview(self.home)
        self.assertNotIn('zcode', [p['id'] for p in view['suggested']])
        self.assertEqual(view['ignored'], [{'id': 'zcode', 'name': 'Zcode', 'skill_roots': ['.zcode/skills'],
                                           'skill_count': 3, 'reason': '你之前选了忽略或不再管理。'}])
        ignored_point = self.fingerprint()
        joined = self.join()
        self.assertNotIn('zcode', profiles.state(self.home)['ignored'])
        self.assertEqual(agents.overview(self.home)['ignored'], [])
        operations.undo(joined['id'], home=self.home)
        self.assertEqual(self.fingerprint(), ignored_point)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), point)
        self.assertIn('zcode', [p['id'] for p in agents.overview(self.home)['suggested']])

    def test_ignored_overview_requires_profile_and_skill_directory(self):
        (self.home / '.empty/skills').mkdir(parents=True)
        (self.home / '.file').mkdir()
        (self.home / '.file/skills').write_text('not a directory')
        self.skill('.unknown/skills/sample', 'sample')
        local = profiles.state(self.home)
        local['custom'] = [profiles.custom('empty', 'Empty', '.empty/skills'),
                           profiles.custom('missing', 'Missing', '.missing/skills'),
                           profiles.custom('file', 'File', '.file/skills')]
        local['ignored'] += ['empty', 'missing', 'file', 'unknown']
        (self.home / '.skill-pilot/agents.json').write_text(json.dumps(local))
        point = self.fingerprint()
        view = agents.overview(self.home)
        self.assertEqual(view['ignored'], [{'id': 'empty', 'name': 'Empty', 'skill_roots': ['.empty/skills'],
                                           'skill_count': 0, 'reason': '你之前选了忽略或不再管理。'}])
        self.assertEqual(view['suggested'], [])
        self.assertEqual(self.fingerprint(), point)
        self.assertEqual(agents.preview_join(['empty'], self.home)['agents'], ['empty'])

    def test_empty_directory_join_leave_and_undo(self):
        root = self.home / '.empty/skills'
        root.mkdir(parents=True)
        request = [{'id': 'empty', 'name': 'Empty', 'skill_root': str(root)}]
        point = self.fingerprint()
        plan = agents.preview_join(request, self.home)
        result = agents.join(request, plan['plan_id'], home=self.home)
        self.assertEqual(operations.history(self.home)[0]['agents'], ['empty'])
        plan = agents.preview_leave('empty', self.home)
        left = agents.leave('empty', plan['plan_id'], home=self.home)
        self.assertEqual(operations.history(self.home)[0]['agents'], ['empty'])
        operations.undo(left['id'], home=self.home)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), point)

    def test_discovery_and_join_when_all_incoming_entries_are_links(self):
        root = self.home / '.zcode/skills'
        root.mkdir(parents=True)
        for name in ['other', 'grilling']:
            (root / name).symlink_to(self.home / '.skill-pilot/store' / name)
        point, before = self.fingerprint(), visibility(self.home)
        view = agents.overview(self.home)
        suggestion = next(p for p in view['suggested'] if p['id'] == 'zcode')
        self.assertEqual(suggestion['skill_count'], 2)
        plan = agents.preview_join(['zcode'], self.home)
        self.assertEqual({s['action'] for s in plan['skills']}, {'linked'})
        result = agents.join(['zcode'], plan['plan_id'], home=self.home)
        self.assertEqual({a: visibility(self.home)[a] for a in before}, before)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), point)

    def test_indirect_link_can_be_assigned_only_to_new_assistant(self):
        path = self.indirect_incoming()
        content = (path / 'SKILL.md').read_bytes()
        self.join()
        operations.apply_assign([{'skill': 'x', 'agent': a, 'enabled': False}
                                 for a in ['codex', 'claude', 'antigravity', 'dsh']],
                                confirmed=True, home=self.home)
        record = operations.load(self.home)[1]['skills']['x']
        self.assertEqual(record['assigned_agents'], ['zcode'])
        self.assertEqual(record['entries'], ['.zcode/skills/x'])
        self.assertEqual(str(path.readlink()), str(self.home / '.skill-pilot/store/x'))
        self.assertEqual((path / 'SKILL.md').read_bytes(), content)
        current = visibility(self.home)
        self.assertIn('x', current['zcode']['available'])
        for a in ['codex', 'claude', 'antigravity', 'dsh']:
            self.assertNotIn('x', current[a]['available'])
        self.assertFalse((self.home / '.agents/skills/x').is_symlink())

    def test_indirect_link_survives_departure_and_other_assignments_removed(self):
        path = self.indirect_incoming()
        content = (path / 'SKILL.md').read_bytes()
        self.join()
        plan = agents.preview_leave('zcode', self.home)
        agents.leave('zcode', plan['plan_id'], plan['revision'], self.home)
        operations.apply_assign([{'skill': 'x', 'agent': a, 'enabled': False}
                                 for a in ['codex', 'claude', 'antigravity', 'dsh']],
                                confirmed=True, home=self.home)
        self.assertEqual((path / 'SKILL.md').read_bytes(), content)
        self.assertEqual(str(path.readlink()), str(self.home / '.skill-pilot/store/x'))
        self.assertFalse((self.home / '.agents/skills/x').is_symlink())
        record = operations.load(self.home)[1]['skills']['x']
        self.assertEqual(record['assigned_agents'], [])
        self.assertEqual(record['entries'], [])
        self.assertIn('x', visibility(self.home)['zcode']['available'])

    def test_indirect_link_preview_and_exact_enrollment_undo(self):
        path = self.indirect_incoming()
        point, before = self.fingerprint(), visibility(self.home)
        plan = agents.preview_join(['zcode'], self.home)
        self.assertEqual(self.fingerprint(), point)
        self.assertEqual(plan['skills'][0]['action'], 'linked')
        self.assertEqual(plan['skills'][0]['default'], 'store')
        self.assertEqual(plan['conflicts'], [])
        self.assertIn('直接指向统一仓库', plan['skills'][0]['reason'])
        result = agents.join(['zcode'], plan['plan_id'], plan['revision'], home=self.home)
        self.assertEqual(str(path.readlink()), str(self.home / '.skill-pilot/store/x'))
        self.assertEqual({a: visibility(self.home)[a] for a in before}, before)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(str(path.readlink()), '../../.agents/skills/x')
        self.assertEqual(self.fingerprint(), point)

    def test_preview_is_cheap_and_matches_original_values(self):
        operations.decide_dependencies([{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': True}], home=self.home)
        home, registry, ids = operations.load(self.home)
        changes = [{'skill': 'grill-me', 'agent': 'claude', 'enabled': True}]
        table = operations.table(self.home)
        reference = operations.assignment_plan(registry, ids, changes)
        expected_after = {a: {'count': sum(a in s['assigned_agents'] for s in reference['registry']['skills'].values()),
                              'description_tokens': sum(s['description_tokens'] for s in table['skills'] if a in reference['registry']['skills'][s['name']]['assigned_agents'])} for a in ids}
        with patch.object(operations, 'table', side_effect=AssertionError('table')), patch.object(operations, 'revision', side_effect=AssertionError('revision')), patch.object(operations, 'new_skills', side_effect=AssertionError('new_skills')), patch.object(operations.normalization, 'tree', side_effect=AssertionError('tree')):
            result = operations.preview_assign(changes, self.home)
        self.assertNotIn('revision', result)
        self.assertEqual(result['after'], expected_after)
        self.assertEqual(result['reasons'], reference['reasons'])
        self.assertEqual(result['assignments'], {n: s['assigned_agents'] for n, s in reference['registry']['skills'].items()})
        self.assertEqual(result['auto_added'], [{'skill': 'grilling', 'agent': 'claude', 'because': 'grill-me'}])
        self.assertEqual(result['cell_notes']['grilling'], {})

    def test_stale_plan_invalid_choice_and_failure_preserve_files(self):
        self.incoming()
        plan = agents.preview_join(['zcode'], self.home)
        point = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '所选版本'):
            agents.join(['zcode'], plan['plan_id'], choices={'other': 'bad'}, home=self.home)
        self.assertEqual(self.fingerprint(), point)
        (self.home / '.zcode/skills/novel/new.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError, '重新预览'):
            agents.join(['zcode'], plan['plan_id'], home=self.home)
        point = self.fingerprint()
        with patch.object(operations, '_commit', side_effect=OSError('fail')):
            with self.assertRaisesRegex(ValueError, '恢复原样'):
                self.join()
        self.assertEqual(self.fingerprint(), point)

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'skill_pilot', *args], cwd=store.PROJECT,
                              env={**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'},
                              capture_output=True, text=True, timeout=20)

    def test_cli_and_api_add_agent_from_a_folder_with_spaces_and_undo(self):
        folder = self.home / 'My Agents/New Tool/skills'
        self.skill('My Agents/New Tool/skills/hello', 'hello')
        before = self.fingerprint()
        preview = self.command('add-agent', '--name', '新工具', str(folder))
        self.assertEqual(preview.returncode, 0, preview.stderr + preview.stdout)
        self.assertIn('请核对', preview.stdout)
        self.assertEqual(self.fingerprint(), before)
        added = self.command('add-agent', '--name', '新工具', str(folder), '--yes', '--json')
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        record = json.loads(added.stdout)
        self.assertIn('新工具', [item['name'] for item in agents.overview(self.home)['managed']])
        self.assertTrue((folder / 'hello').is_symlink())
        undone = self.command('undo', record['id'], '--json')
        self.assertEqual(undone.returncode, 0, undone.stderr + undone.stdout)
        self.assertEqual(self.fingerprint(), before)
        self.assertFalse((folder / 'hello').is_symlink())

    def test_cli_preview_names_every_required_new_reader(self):
        self.skill('.joint/skills/shared', 'shared')
        local = profiles.state(self.home)
        local['custom'] = [profiles.custom(a, a.title(), '.joint/skills') for a in ['one', 'two']]
        (self.home / '.skill-pilot/agents.json').write_text(json.dumps(local))
        before = self.fingerprint()
        preview = self.command('add-agent', '--name', 'Third', '.joint/skills', '--json')
        self.assertEqual(preview.returncode, 0, preview.stderr + preview.stdout)
        plan = json.loads(preview.stdout)
        self.assertEqual(plan['agents'], ['joint', 'one', 'two'])
        self.assertIn('Third、One、Two', plan['message'])
        self.assertEqual(self.fingerprint(), before)
        added = self.command('add-agent', '--name', 'Third', '.joint/skills', '--yes', '--json')
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        self.assertEqual(json.loads(added.stdout)['agents'], plan['agents'])
        undone = self.command('undo', json.loads(added.stdout)['id'], '--json')
        self.assertEqual(undone.returncode, 0, undone.stderr + undone.stdout)
        self.assertEqual(self.fingerprint(), before)

    def test_forbidden_directories_and_last_managed_assistant(self):
        point = self.fingerprint()
        for root in ['.skill-pilot/store', '.codex/skills/.system', '.claude/skills/synced', '../outside', str(self.home.parent / 'outside'), '.codex/skills/community']:
            with self.subTest(root=root), self.assertRaises(ValueError):
                agents.preview_join([{'id': 'new', 'name': 'New', 'skill_root': root}], self.home)
        self.assertEqual(self.fingerprint(), point)
        for identifier in ['dsh', 'antigravity', 'claude']:
            plan = agents.preview_leave(identifier, self.home)
            agents.leave(identifier, plan['plan_id'], home=self.home)
        with self.assertRaisesRegex(ValueError, '至少'):
            agents.preview_leave('codex', self.home)


if __name__ == '__main__':
    unittest.main()
