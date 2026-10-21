"""External Codex preferences must not block unrelated local or team installs."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, store
from skill_pilot.platform_support import rmtree
from skill_pilot.sources import github
from skill_pilot.sync import visibility
from skill_pilot.web.server import Dashboard
from tests import test_operations, test_team


class ExternalDisableTests(unittest.TestCase):
    setUp = test_operations.OperationsTests.setUp
    skill = test_operations.OperationsTests.skill
    repository = test_operations.OperationsTests.repository
    fingerprint = test_operations.OperationsTests.fingerprint
    assign = test_operations.OperationsTests.assign

    def disable(self, row):
        path = self.home / '.codex/config.toml'
        original = path.read_text()
        text = ', ' + ', '.join(k + '=' + (str(v).lower() if isinstance(v, bool) else json.dumps(v)) for k, v in row.items())
        # Preserve an existing unrelated config section in the fixture.
        original = original.replace('}] # keep', '}, {' + text[2:] + '}] # keep')
        path.write_text(original)
        return path.read_bytes()

    def review(self):
        review = github.inspect(self.repository())
        self.addCleanup(lambda: rmtree(review['temporary'], ignore_errors=True))
        return review

    def test_name_and_path_preferences_allow_install_and_exact_undo(self):
        self.disable({'name': 'other', 'enabled': False})
        config = self.disable({'path': str(self.home / '.codex/skills/grilling/SKILL.md'), 'enabled': False})
        before = self.fingerprint()
        table = operations.table(self.home)
        rows = {row['name']: row for row in table['skills']}
        self.assertNotIn('codex', rows['other']['assigned_agents'])
        self.assertNotIn('codex', rows['grilling']['assigned_agents'])
        self.assertIn('由你停用', rows['other']['cell_notes']['codex'])
        self.assertEqual(next(a['count'] for a in table['agents'] if a['id'] == 'codex'), 1)
        self.assertEqual(self.fingerprint(), before, 'Reading the effective state must not write back')
        result = operations.install(self.review(), ['installed'], ['codex', 'claude'], home=self.home)
        visible = visibility(self.home)
        self.assertEqual(visible['codex']['available'], ['grill-me', 'installed'])
        self.assertIn('installed', visible['claude']['available'])
        self.assertEqual((self.home / '.codex/config.toml').read_bytes(), config)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_user_disabled_selection_and_dependency_are_not_silently_enabled(self):
        config = self.disable({'name': 'grilling', 'enabled': False})
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, 'grilling.*由你停用'):
            self.assign('grilling', 'codex')
        _, registry, agents = operations.load(self.home)
        registry['skills']['grill-me']['requires'] = ['grilling']
        registry['skills']['grill-me']['assigned_agents'].remove('codex')
        with self.assertRaisesRegex(ValueError, 'grilling.*由你停用'):
            operations.assignment_plan(registry, agents, [{'skill': 'grill-me', 'agent': 'codex', 'enabled': True}], self.home)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual((self.home / '.codex/config.toml').read_bytes(), config)

    def test_preexisting_path_disable_for_new_skill_is_respected(self):
        self.disable({'path': str(self.home / '.codex/skills/installed/SKILL.md'), 'enabled': False})
        review = self.review()
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, 'installed.*由你停用'):
            operations.install(review, ['installed'], ['codex'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_broken_new_install_still_rolls_back(self):
        self.disable({'name': 'other', 'enabled': False})
        review = self.review()
        before = self.fingerprint()
        reconcile = operations.reconcile_assignments
        def broken(registry, home, checked, changed):
            reconcile(registry, home, checked, changed)
            (home / '.codex/skills/installed').unlink()
        with patch.object(operations, 'reconcile_assignments', side_effect=broken):
            with self.assertRaisesRegex(ValueError, '实际技能名单'):
                operations.install(review, ['installed'], ['codex'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_manager_owned_disable_can_be_reenabled(self):
        self.assign('other', 'codex', False, confirmed=True)
        self.assign('other', 'dsh')
        self.assertNotIn('other', visibility(self.home)['codex']['available'])
        self.assign('other', 'codex')
        self.assertIn('other', visibility(self.home)['codex']['available'])

    def test_assigning_other_agent_does_not_reenable_path_disabled_skill(self):
        config = self.disable({'path': str(self.home / '.codex/skills/other/SKILL.md'), 'enabled': False})
        before = self.fingerprint()
        result = self.assign('other', 'dsh')
        visible = visibility(self.home)
        self.assertIn('other', visible['dsh']['available'])
        self.assertNotIn('other', visible['codex']['available'])
        self.assertEqual((self.home / '.codex/config.toml').read_bytes(), config)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_preview_counts_remain_effective_and_missing_unblocked_skill_is_not_ignored(self):
        self.disable({'name': 'other', 'enabled': False})
        preview = operations.preview_assign([{'skill': 'grill-me', 'agent': 'claude', 'enabled': True}], self.home)
        self.assertEqual(preview['after']['codex']['count'], 2)
        self.assertNotIn('codex', preview['assignments']['other'])
        (self.home / '.codex/skills/grilling').unlink()
        _, registry, agents = operations.load(self.home)
        with self.assertRaisesRegex(ValueError, '实际技能名单'):
            operations._validate(self.home, registry, agents)


class TeamExternalDisableTests(unittest.TestCase):
    setUp = test_team.TeamTests.setUp
    start = test_team.TeamTests.start
    skill = test_team.TeamTests.skill
    make_home = test_team.TeamTests.make_home
    registry = test_team.TeamTests.registry
    request = test_team.TeamTests.request
    ok = test_team.TeamTests.ok
    configure = test_team.TeamTests.configure
    publish = test_team.TeamTests.publish
    local_github = test_team.TeamTests.local_github
    wait_queue = test_team.TeamTests.wait_queue

    def test_team_files_and_github_install_with_unrelated_user_disable_and_undo(self):
        self.configure()
        self.publish()
        recipient = self.root / 'Recipient With Spaces'
        for agent in ('codex', 'claude'):
            self.skill(recipient / ('.' + agent) / 'skills/legacy', 'legacy')
        preview = store.preview(recipient, ['codex', 'claude'])
        store.apply(preview['plan_id'], recipient, ['codex', 'claude'])
        config_path = recipient / '.codex/config.toml'
        config_path.write_text('[skills]\nconfig=[{path=' + json.dumps(str(recipient / '.codex/skills/legacy/SKILL.md')) + ',enabled=false}]\n')
        config = config_path.read_bytes()
        member = self.hub.store.add_member('Recipient')
        server = self.start(Dashboard({'home': str(recipient)}))
        self.ok(server, '/api/team/settings', {'url': self.url, 'key': member['token']})
        for name in ('acme-local', 'acme-main'):
            with self.subTest(source=name):
                before = {str(p): store.physical_state(recipient / p) for p in
                          ('.codex/skills', '.claude/skills', '.codex/config.toml', '.skill-pilot/registry.json')}
                card = next(c for c in self.ok(server, '/api/team/catalog')['cards'] if c['name'] == name)
                with self.local_github():
                    queued = self.ok(server, '/api/team/queue', {'key': card['key']})['items'][-1]
                    result = next(item for item in self.wait_queue(server) if item['id'] == queued['id'])
                self.assertEqual(result['status'], 'done', result)
                registry = operations.load(recipient)[1]
                for installed in result['installed']:
                    self.assertEqual(registry['skills'][installed]['assigned_agents'], [])
                changes = [{'skill': name, 'agent': agent, 'enabled': True} for agent in ('codex', 'claude')]
                plan = operations.preview_assign(changes, recipient, recheck=True)
                enabled = operations.apply_assign(changes, plan['revision'], True, recipient, recheck=True)
                visible = visibility(recipient)
                self.assertIn(name, visible['codex']['available'])
                self.assertIn(name, visible['claude']['available'])
                self.assertNotIn('legacy', visible['codex']['available'])
                self.assertIn('legacy', visible['claude']['available'])
                self.assertEqual(config_path.read_bytes(), config)
                self.ok(server, '/api/undo', {'id': enabled['id'], 'confirmed': True,
                        'revision': operations.revision(recipient, ['codex', 'claude'])})
                self.ok(server, '/api/undo', {'id': result['operation'], 'confirmed': True,
                        'revision': operations.revision(recipient, ['codex', 'claude'])})
                self.assertEqual({p: store.physical_state(recipient / p) for p in before}, before)


if __name__ == '__main__':
    unittest.main()
