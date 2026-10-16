import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import agent_discovery, agents, profiles, store


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot discovery ')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, Path(temporary.name) / 'Another User Home')
        self.home.mkdir()

    def skill(self, relative):
        path = self.home / relative / 'example'
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text('---\nname: example\ndescription: Example\n---\n')
        return path

    def test_empty_user_and_installed_apps_need_no_skill_scan(self):
        self.assertEqual(agent_discovery.find(self.home)['candidates'], [])
        for p in profiles.ADDITIONAL_PROFILES:
            (self.home / 'Applications' / p['detect']['apps'][0]).mkdir(parents=True)
        with patch('skill_pilot.scan.content_digest', side_effect=AssertionError('skill body read')), \
                patch('skill_pilot.scan.scan', side_effect=AssertionError('full scan')), \
                patch('skill_pilot.agents.scan_root', side_effect=AssertionError('skill scan')), \
                patch('skill_pilot.operations.revision', side_effect=AssertionError('allocation scan')):
            findings = agent_discovery.find(self.home)
            view = agents.overview(self.home, discovered=findings['candidates'])
        self.assertEqual([p['id'] for p in view['suggested']], ['cursor', 'doubaowork', 'workbuddy'])
        self.assertEqual(view['managed'], [])
        self.assertFalse((self.home / '.skill-pilot').exists())

    def test_direct_and_deeper_folders_and_last_findings(self):
        self.skill('.new/skills')
        self.skill('.nested/config/agent/skills')
        self.assertEqual([p['id'] for p in agent_discovery.find(self.home)['candidates']], ['new'])
        findings = agent_discovery.find(self.home, full=True)
        self.assertEqual({p['id'] for p in findings['candidates']}, {'new', 'nested'})
        reopened = agent_discovery.find(self.home, previous=findings['candidates'])
        self.assertEqual({p['id'] for p in reopened['candidates']}, {'new', 'nested'})
        (self.home / '.nested/config/agent/skills/example/SKILL.md').unlink()
        self.assertEqual([p['id'] for p in agent_discovery.find(self.home, previous=findings['candidates'])['candidates']], ['new'])

    def test_known_roots_shared_skills_and_custom_profiles_stay_authoritative(self):
        self.skill('.agents/skills')
        custom = profiles.custom('cursor', 'My Cursor', '.cursor/skills')
        path = self.home / '.skill-pilot/agents.json'
        path.parent.mkdir()
        original = json.dumps({'onboarded': ['cursor'], 'ignored': ['dsh'], 'custom': [custom]})
        path.write_text(original)
        findings = agent_discovery.find(self.home, full=True)
        view = agents.overview(self.home, discovered=findings['candidates'])
        self.assertNotIn('agents', [p['id'] for p in findings['candidates']])
        self.assertNotIn('cursor', [p['id'] for p in view['suggested']])
        self.assertEqual(view['managed'][0]['skill_roots'], ['.cursor/skills'])
        self.assertEqual(path.read_text(), original)

    def test_no_projects_references_plugins_or_external_links(self):
        for root in ('.future/references/skills', '.future/projects/skills', '.future/cache/skills',
                     '.future/plugins/skills', '.future/skills-marketplace/skills',
                     '.cursor/plugins/skills', '.deep/one/two/three/skills', 'Projects/tool/skills'):
            self.skill(root)
        outside = Path(self.temp_directory())
        (outside / 'skills/example').mkdir(parents=True)
        (outside / 'skills/example/SKILL.md').write_text('do not read')
        (self.home / '.outside').symlink_to(outside, target_is_directory=True)
        candidates = agent_discovery.find(self.home, full=True)['candidates']
        self.assertEqual([p['id'] for p in candidates], ['cursor'])
        self.assertEqual(candidates[0]['skill_roots'], ['.cursor/skills', '.agents/skills'])
        previous = [{'id': 'future', 'name': 'Future', 'skill_roots': ['.future/projects/skills']}]
        self.assertNotIn('future', [p['id'] for p in agent_discovery.find(self.home, previous=previous)['candidates']])

    def temp_directory(self):
        directory = tempfile.TemporaryDirectory(prefix='skill pilot outside ')
        self.addCleanup(directory.cleanup)
        return directory.name

    def test_cache_is_relative_and_reopens_without_scanning(self):
        self.skill('.new/skills')
        findings = agent_discovery.find(self.home)
        before = store.physical_state(self.home / '.new')
        agent_discovery.save(self.home, findings)
        text = (self.home / agent_discovery.CACHE).read_text()
        self.assertNotIn(str(self.home), text)
        self.assertEqual(agent_discovery.read(self.home), findings)
        self.assertEqual(store.physical_state(self.home / '.new'), before)
        self.assertEqual(list((self.home / '.skill-pilot').glob('.agent-discovery-*')), [])

    def test_read_only_scan_does_not_write_cache_or_skill_state(self):
        self.skill('.new/skills')
        before = store.physical_state(self.home)
        with patch.dict('os.environ', {'SKILL_PILOT_READ_ONLY': '1'}):
            findings = agent_discovery.find(self.home, full=True)
            agent_discovery.save(self.home, findings)
        self.assertEqual([p['id'] for p in findings['candidates']], ['new'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_corrupt_or_unsafe_cache_is_discarded(self):
        path = self.home / agent_discovery.CACHE
        path.parent.mkdir()
        for body in ('invalid', '{}', '[]', json.dumps({'schema_version': 1, 'checked_at': '',
                'candidates': [{'id': 'bad', 'name': 'Bad', 'skill_roots': ['../outside/skills']}]})):
            path.write_text(body)
            self.assertEqual(agent_discovery.read(self.home)['candidates'], [])

    def test_cache_cannot_be_written_through_an_external_directory(self):
        outside = Path(self.temp_directory())
        (self.home / '.skill-pilot').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '主目录之外'):
            agent_discovery.save(self.home, agent_discovery.find(self.home))
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_cache_write_keeps_the_saved_result_and_removes_temporary_file(self):
        findings = agent_discovery.find(self.home)
        agent_discovery.save(self.home, findings)
        path = self.home / agent_discovery.CACHE
        saved = path.read_bytes()
        with patch.object(agent_discovery.json, 'dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                agent_discovery.save(self.home, findings)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(list(path.parent.glob('.agent-discovery-*')), [])
