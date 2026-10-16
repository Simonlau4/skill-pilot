"""额外技能源是用户自己的设置，公开代码里不写私人目录。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from skill_pilot import store
from skill_pilot.extra_sources import add, ensure, paths, remove
from skill_pilot.scan import scan
from skill_pilot.web.server import Dashboard
from tests import set_up_home


class ExtraSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot extra ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name) / 'home')
        self.installed = self.home / '.codex/skills/sample'
        self.write_skill(self.installed, 'sample', '已安装')

    def write_skill(self, path, name, body='正文'):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: 示例技能\n---\n{body}\n', encoding='utf-8')

    def copies(self):
        return sorted(skill['name'] for skill in scan(self.home)['source_copies'])

    def test_unset_does_not_scan_an_extra_folder(self):
        self.write_skill(self.home / 'My Sources/sample-skill', 'sample-skill')
        self.assertEqual(paths(self.home), [])
        self.assertEqual(self.copies(), [])
        self.assertFalse((self.home / '.skill-pilot/extra-sources.json').exists())

    def test_a_configured_folder_with_spaces_is_included(self):
        folder = self.home / 'My Sources/sample skill'
        self.write_skill(folder, 'sample-skill', '额外这一份')
        self.assertIn('已记下', add(self.home, str(folder)))
        self.assertEqual(paths(self.home), ['My Sources/sample skill'])
        self.assertEqual(self.copies(), ['sample-skill'])

    def test_a_missing_folder_is_kept_but_not_scanned(self):
        missing = self.home / 'My Sources/not here yet'
        add(self.home, str(missing))
        self.assertEqual(paths(self.home), ['My Sources/not here yet'])
        self.assertEqual(self.copies(), [])
        remove(self.home, 'My Sources/not here yet')
        self.assertEqual(paths(self.home), [])

    def test_upgrade_keeps_sources_already_recorded_for_this_home(self):
        folder = self.home / 'My Sources/sample skill'
        self.write_skill(folder, 'sample-skill', '升级前就有')
        pilot = self.home / '.skill-pilot'
        pilot.mkdir()
        registry = {'schema_version': 1, 'skills': {'sample-skill': {
            'source': {'source': '本机源码', 'source_path': '~/My Sources/sample skill'},
            'assigned_agents': [], 'entries': []}}}
        (pilot / 'registry.json').write_text(json.dumps(registry), encoding='utf-8')
        self.assertEqual(self.copies(), ['sample-skill'])
        self.assertFalse((pilot / 'extra-sources.json').exists())
        self.assertEqual(ensure(self.home), ['My Sources/sample skill'])
        saved = json.loads((pilot / 'extra-sources.json').read_text())
        self.assertEqual(saved['paths'], ['My Sources/sample skill'])
        self.assertEqual(self.copies(), ['sample-skill'])

    def test_an_explicit_empty_list_is_not_filled_in_again(self):
        folder = self.home / 'My Sources/sample skill'
        self.write_skill(folder, 'sample-skill')
        pilot = self.home / '.skill-pilot'
        pilot.mkdir()
        (pilot / 'registry.json').write_text(json.dumps({'skills': {'sample-skill': {
            'source': {'source': '本机源码', 'source_path': '~/My Sources/sample skill'}}}}), encoding='utf-8')
        (pilot / 'extra-sources.json').write_text(json.dumps({'paths': []}), encoding='utf-8')
        self.assertEqual(ensure(self.home), [])
        self.assertEqual(self.copies(), [])

    def test_an_installed_copy_is_not_treated_as_an_extra_source(self):
        pilot = self.home / '.skill-pilot'
        pilot.mkdir()
        (pilot / 'registry.json').write_text(json.dumps({'skills': {'sample': {
            'source': {'source': '本机源码', 'source_path': '~/.codex/skills/sample'}}}}), encoding='utf-8')
        self.assertEqual(paths(self.home), [])
        self.assertEqual(self.copies(), [])
        self.assertFalse((pilot / 'extra-sources.json').exists())

    def test_command_lists_and_records_in_plain_language(self):
        env = {**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        listed = subprocess.run([sys.executable, '-m', 'skill_pilot', 'extra-sources'],
                                cwd=store.PROJECT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('还没有额外的技能源', listed.stdout)
        folder = self.home / 'My Sources/sample skill'
        self.write_skill(folder, 'sample-skill')
        added = subprocess.run([sys.executable, '-m', 'skill_pilot', 'extra-sources', str(folder), '--json'],
                               cwd=store.PROJECT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(added.returncode, 0, added.stderr)
        body = json.loads(added.stdout)
        self.assertIn('已记下', body['message'])
        self.assertEqual(body['paths'], ['My Sources/sample skill'])


class ExtraSourceWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot extra web ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name) / 'home')
        self.server = Dashboard({'home': str(self.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, body=None):
        headers = {'X-Skill-Pilot-Token': self.server.token, 'Origin': self.server.origin,
                   'Content-Type': 'application/json'}
        request = urllib.request.Request(self.server.origin + path, headers=headers,
                                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status, data = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, data = error.code, error.read()
        return status, json.loads(data)

    def test_settings_can_record_and_remove_a_folder(self):
        folder = self.home / 'My Sources/sample skill'
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('---\nname: sample-skill\ndescription: 示例技能\n---\n额外\n', encoding='utf-8')
        status, body = self.request('/api/settings')
        self.assertEqual(status, 200)
        self.assertEqual(body['extra_sources'], [])
        status, body = self.request('/api/settings/extra-sources', {'path': str(folder)})
        self.assertEqual(status, 200)
        self.assertIn('已记下', body['message'])
        self.assertEqual(body['extra_sources'], ['My Sources/sample skill'])
        self.assertEqual(sorted(skill['name'] for skill in self.server.inventory['source_copies']), ['sample-skill'])
        status, body = self.request('/api/settings/extra-sources', {'path': 'My Sources/sample skill', 'remove': True})
        self.assertEqual(status, 200)
        self.assertEqual(body['extra_sources'], [])
        self.assertIn('不再额外查看', body['message'])
