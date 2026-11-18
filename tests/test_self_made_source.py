"""A skill with an upstream source can be turned into a self-made one, so update-local can take it over."""
from tests import set_up_home
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, store, updates
from skill_pilot.sources import normalize


class SelfMadeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='skill pilot self made ')
        self.addCleanup(self.tmp.cleanup)
        set_up_home(self, Path(self.tmp.name) / 'Example Home')
        for name, body in (('design', '上游内容'), ('other', '别的技能')):
            skill = self.home / '.codex/skills' / name
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例\n---\n{body}\n')
        store.apply(store.preview(self.home)['plan_id'], self.home)
        path = self.home / '.skill-pilot/registry.json'
        data = json.loads(path.read_text())
        data['skills']['design']['source'] = {'source': 'https://github.com/acme/skills', 'repository': 'acme/skills',
                                              'source_path': 'design'}
        path.write_text(json.dumps(data))
        self.newer = Path(self.tmp.name) / 'Merged copy' / 'design'
        self.newer.mkdir(parents=True)
        (self.newer / 'SKILL.md').write_text('---\nname: design\ndescription: 示例\n---\n合成后的新版\n')

    def state(self):
        return operations.revision(self.home, operations.load(self.home)[2])

    def test_mark_self_made_lets_update_local_take_over_and_both_undo(self):
        with self.assertRaisesRegex(ValueError, '上游来源'):
            operations.update_local('design', self.newer, home=self.home)
        before = self.state()
        assigned = operations.load(self.home)[1]['skills']['design']['assigned_agents']

        preview = operations.mark_self_made('design', home=self.home)
        self.assertTrue(preview['needs_confirmation'])
        self.assertIn('acme/skills', preview['message'])
        self.assertEqual(self.state(), before, '预览不该改动任何东西')

        done = operations.mark_self_made('design', True, self.home)
        skill = operations.load(self.home)[1]['skills']['design']
        self.assertEqual(normalize(skill['source'])['kind'], 'skip')
        self.assertIn('acme/skills', skill['source']['evidence'], '要留下原来的来源，方便以后核对')
        self.assertEqual(skill['assigned_agents'], assigned)
        self.assertEqual((self.home / '.skill-pilot/store/design/SKILL.md').read_text().split('---\n')[-1], '上游内容\n')
        again = operations.mark_self_made('design', home=self.home)
        self.assertFalse(again.get('needs_confirmation'))
        self.assertIn('已经是自制', again['message'])

        updated = operations.update_local('design', self.newer, home=self.home, confirmed=True)
        self.assertIn('合成后的新版', (self.home / '.skill-pilot/store/design/SKILL.md').read_text())
        operations.undo(updated['id'], home=self.home)
        operations.undo(done['id'], home=self.home)
        self.assertEqual(self.state(), before)

    def test_only_github_sources_can_become_self_made(self):
        # A team-store skill keeps its author record, or a member could list a colleague's skill as their own.
        path = self.home / '.skill-pilot/registry.json'
        data = json.loads(path.read_text())
        member, version = 'a' * 32, 'b' * 64
        sources = {
            '团队商店': {'source': '团队商店', 'team': {'member': member, 'member_name': '示例同事', 'name': 'design',
                                                  'version': version, 'key': f'team:{member}:design'}},
            '飞书官方': {'source': {'sourceType': 'well-known', 'sourceUrl': 'https://open.feishu.cn/lark-cli/skills/design'},
                     'record': {'sourceType': 'well-known', 'sourceUrl': 'https://open.feishu.cn/lark-cli/skills/design'}},
        }
        for label, source in sources.items():
            with self.subTest(source=label):
                data['skills']['design']['source'] = source
                path.write_text(json.dumps(data))
                before = self.state()
                for confirmed in (False, True):
                    with self.assertRaisesRegex(ValueError, 'GitHub'):
                        operations.mark_self_made('design', confirmed, self.home)
                self.assertEqual(self.state(), before)

    def test_a_setting_saved_meanwhile_is_kept(self):
        # Another operation saves while this one has read the registry but not yet taken the lock.
        load = operations.load
        saved = []
        def load_then_save_elsewhere(home=None):
            result = load(home)
            if not saved:
                saved.append(True)  # The other operation reads the registry too; trigger it only once.
                updates.set_watch('other', 'off', home=self.home, confirmed=True)
            return result
        with patch.object(operations, 'load', load_then_save_elsewhere):
            operations.mark_self_made('design', True, self.home)
        skills = operations.load(self.home)[1]['skills']
        self.assertEqual(normalize(skills['design']['source'])['kind'], 'skip')
        self.assertEqual(skills['other'].get('update_watch'), 'off', '同时保存的设置被覆盖了')

    def test_command_line_previews_first_and_keeps_the_two_ways_apart(self):
        env = {**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        def run(*args):
            return subprocess.run([sys.executable, '-m', 'skill_pilot', 'set-source', 'design', *args], cwd=store.PROJECT,
                                  env=env, capture_output=True, text=True, timeout=60)
        before = self.state()
        shown = run('--self-made')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('自制', shown.stdout)
        self.assertIn('--yes', shown.stdout)
        self.assertEqual(self.state(), before)
        mixed = run('--self-made', '--repo', 'acme/skills', '--path', 'design', '--yes')
        self.assertNotEqual(mixed.returncode, 0)
        self.assertEqual(self.state(), before)
        missing = run('--repo', 'acme/skills', '--yes')
        self.assertNotEqual(missing.returncode, 0)
        done = run('--self-made', '--yes')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(normalize(operations.load(self.home)[1]['skills']['design']['source'])['kind'], 'skip')


if __name__ == '__main__':
    unittest.main()
