import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import agents, operations, profiles, store
from skill_pilot.scan import scan, suggestions
from skill_pilot.sync import visibility


class AgentDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot new user ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name) / 'New User Home')
        self.home.mkdir()

    def install_app(self, profile):
        app = self.home / 'Applications' / profile['detect']['apps'][0]
        app.mkdir(parents=True)
        return app

    def skill(self, relative, name):
        path = self.home / relative / name
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例\n---\n正文\n')
        return path

    def test_empty_home_ignores_the_developers_apps_and_commands(self):
        with patch.object(Path, 'home', return_value=self.home), patch.object(profiles.shutil, 'which') as which:
            inventory = scan(self.home)
            self.assertEqual(inventory['agents'], [])
            self.assertEqual(suggestions(self.home, inventory), [])
            which.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_apps_are_suggested_before_any_skill_directory_exists(self):
        for profile in profiles.ADDITIONAL_PROFILES:
            with self.subTest(agent=profile['id']):
                app = self.install_app(profile)
                before = store.physical_state(self.home)
                inventory = scan(self.home)
                row = next(a for a in inventory['agents'] if a['id'] == profile['id'])
                self.assertEqual(row['status'], 'profile_available')
                self.assertIn(str(app), row['evidence'])
                self.assertEqual(row['visible_skill_count'], 0)
                suggested = suggestions(self.home, inventory)
                self.assertEqual([p['id'] for p in suggested], [profile['id']])
                self.assertEqual(suggested[0]['skill_count'], 0)
                self.assertEqual(suggested[0]['skill_roots'], profile['skill_roots'])
                self.assertEqual(store.physical_state(self.home), before)
                app.rmdir()

    def test_configuration_directories_work_without_an_application_in_the_default_location(self):
        for profile in profiles.ADDITIONAL_PROFILES:
            for relative in profile['detect']['config_dirs']:
                with self.subTest(agent=profile['id'], config=relative):
                    home = self.home / profile['id'] / Path(relative).name
                    (home / relative).mkdir(parents=True)
                    result = agents.overview(home)
                    self.assertEqual([p['id'] for p in result['suggested']], [profile['id']])
                    self.assertEqual(result['suggested'][0]['skill_count'], 0)
                    self.assertFalse((home / '.skill-pilot').exists())

    def test_explicit_application_location_is_respected(self):
        applications = Path(self.temp.name) / 'Another Applications Folder'
        (applications / 'Cursor.app').mkdir(parents=True)
        inventory = scan(self.home, [applications])
        self.assertEqual([a['id'] for a in inventory['agents']], ['cursor'])
        self.assertEqual(scan(self.home, [])['agents'], [])

    def test_each_empty_agent_can_complete_first_setup_and_receive_a_skill(self):
        for profile in profiles.ADDITIONAL_PROFILES:
            with self.subTest(agent=profile['id']):
                home = self.home / profile['id']
                (home / 'Applications' / profile['detect']['apps'][0]).mkdir(parents=True)
                plan = store.preview(home, [profile['id']])
                self.assertEqual(plan['skills'], [])
                store.apply(plan['plan_id'], home, [profile['id']])
                self.assertEqual([p['id'] for p in profiles.managed(home)], [profile['id']])
                original = store.physical_state(home / profile['link_root'])
                installed = operations.bootstrap(home)
                entry = home / profile['link_root'] / 'skill-pilot'
                self.assertTrue(entry.is_symlink())
                self.assertEqual(entry.resolve(), home / '.skill-pilot/store/skill-pilot')
                self.assertEqual(visibility(home)[profile['id']]['available'], ['skill-pilot'])
                operations.undo(installed['id'], home=home)
                self.assertEqual(store.physical_state(home / profile['link_root']), original)

    def test_user_skills_are_separate_from_builtin_and_market_skills(self):
        for profile in profiles.ADDITIONAL_PROFILES:
            self.install_app(profile)
            self.skill(profile['link_root'], profile['id'] + '-user')
            self.skill(profile['readonly_roots'][0][0], profile['id'] + '-builtin')
        inventory = scan(self.home)
        for profile in profiles.ADDITIONAL_PROFILES:
            row = next(a for a in inventory['agents'] if a['id'] == profile['id'])
            self.assertEqual(row['visible_names'], [profile['id'] + '-user'])
            root = next(r for r in inventory['roots'] if r['path'] == str(self.home / profile['readonly_roots'][0][0]))
            self.assertNotEqual(root['category'], 'user')
            with self.assertRaisesRegex(ValueError, '自己管理'):
                agents._root(profile['readonly_roots'][0][0], self.home)
        self.assertEqual({p['id']: p['skill_count'] for p in suggestions(self.home, inventory)},
                         {p['id']: 1 for p in profiles.ADDITIONAL_PROFILES})

    def test_existing_custom_agent_profiles_keep_their_roots_and_behavior(self):
        custom = [profiles.custom('cursor', 'Cursor', '.cursor/skills'),
                  profiles.custom('workbuddy', 'WorkBuddy', '.workbuddy/skills')]
        path = self.home / '.skill-pilot/agents.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'onboarded': ['cursor', 'workbuddy'], 'ignored': [], 'custom': custom}))
        before = path.read_bytes()
        catalog = profiles.all_profiles(self.home)
        for saved in custom:
            self.assertEqual(next(p for p in catalog if p['id'] == saved['id']), saved)
            self.assertEqual(sum(p['id'] == saved['id'] for p in catalog), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_users_can_join_empty_agents_assign_and_undo(self):
        self.skill('.codex/skills', 'sample')
        plan = store.preview(self.home)
        store.apply(plan['plan_id'], self.home)
        for profile in profiles.ADDITIONAL_PROFILES:
            self.install_app(profile)
        ids = [p['id'] for p in profiles.ADDITIONAL_PROFILES]
        plan = agents.preview_join(ids, self.home)
        self.assertEqual(plan['skills'], [])
        joined = agents.join(ids, plan['plan_id'], home=self.home)
        self.assertEqual(profiles.state(self.home).get('custom', []), [])
        before = {p['id']: store.physical_state(self.home / p['link_root']) for p in profiles.ADDITIONAL_PROFILES}
        assigned = operations.apply_assign([{'skill': 'sample', 'agent': a, 'enabled': True} for a in ids], home=self.home)
        for profile in profiles.ADDITIONAL_PROFILES:
            self.assertEqual(visibility(self.home)[profile['id']]['available'], ['sample'])
            self.assertTrue((self.home / profile['link_root'] / 'sample').is_symlink())
        operations.undo(assigned['id'], home=self.home)
        for profile in profiles.ADDITIONAL_PROFILES:
            self.assertEqual(store.physical_state(self.home / profile['link_root']), before[profile['id']])
        operations.undo(joined['id'], home=self.home)
        self.assertEqual([p['id'] for p in agents.overview(self.home)['suggested']], ids)

    def test_cursor_shared_skills_require_joint_enrollment_but_allow_separate_assignment(self):
        self.install_app(profiles.ADDITIONAL_PROFILES[0])
        (self.home / '.dsh').mkdir()
        self.skill('.agents/skills', 'shared')
        with self.assertRaisesRegex(ValueError, '一起选择'):
            store.preview(self.home, ['cursor'])
        plan = store.preview(self.home)
        self.assertIn('cursor', plan['agents'])
        store.apply(plan['plan_id'], self.home)
        self.assertEqual(visibility(self.home)['cursor']['available'], ['shared'])
        before = store.physical_state(self.home / '.agents/skills')
        result = operations.apply_assign([{'skill': 'shared', 'agent': 'cursor', 'enabled': False}], home=self.home)
        visible = visibility(self.home)
        self.assertNotIn('shared', visible['cursor']['available'])
        self.assertIn('shared', visible['codex']['available'])
        self.assertIn('shared', visible['dsh']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(store.physical_state(self.home / '.agents/skills'), before)
        self.assertIn('shared', visibility(self.home)['cursor']['available'])

    def test_ignoring_an_empty_installed_agent_remains_visible_and_can_be_undone(self):
        self.skill('.codex/skills', 'sample')
        plan = store.preview(self.home)
        store.apply(plan['plan_id'], self.home)
        self.install_app(profiles.ADDITIONAL_PROFILES[2])
        plan = agents.preview_ignore('workbuddy', self.home)
        result = agents.ignore('workbuddy', plan['plan_id'], home=self.home)
        view = agents.overview(self.home)
        self.assertEqual([p['id'] for p in view['ignored']], ['workbuddy'])
        self.assertEqual(view['ignored'][0]['skill_count'], 0)
        self.assertEqual(view['suggested'], [])
        operations.undo(result['id'], home=self.home)
        self.assertEqual([p['id'] for p in agents.overview(self.home)['suggested']], ['workbuddy'])
