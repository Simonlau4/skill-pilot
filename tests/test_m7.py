import json
import contextlib
import io
import os
import unittest
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from skill_pilot import cleanup, operations, store, updates
from skill_pilot.sync import visibility
from tests import test_operations, test_updates


class M7Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_operations.OperationsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home

    def test_first_launch_installs_four_agents_and_undo_stays_undone(self):
        before = self.fixture.fingerprint()
        with patch('skill_pilot.research.ask', side_effect=AssertionError('不应调用 AI')):
            result = operations.bootstrap(self.home)
        self.assertTrue((self.home / '.skill-pilot/store/skill-pilot/SKILL.md').is_file())
        for agent in ['codex', 'claude', 'antigravity', 'dsh']:
            self.assertIn('skill-pilot', visibility(self.home)[agent]['available'])
        for root in ['.agents/skills', '.claude/skills', '.gemini/config/skills']:
            self.assertTrue((self.home / root / 'skill-pilot').is_symlink())
        self.assertIsNone(operations.bootstrap(self.home))
        self.assertTrue((self.home / '.skill-pilot/backups' / result['backup_id'] / 'files.tar').is_file())
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fixture.fingerprint())
        self.assertFalse((self.home / '.skill-pilot/store/skill-pilot').exists())
        self.assertIsNone(operations.bootstrap(self.home))
        self.assertTrue(operations.history(self.home)[1]['undone'])

    def test_readonly_refuses_all_write_entries_and_bootstrap_is_skipped(self):
        operation = self.fixture.assign('other', 'claude')
        before = store.physical_state(self.home)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertIsNone(operations.bootstrap(self.home))
            calls = [lambda: operations.apply_assign([], home=self.home),
                     lambda: operations.decide_dependencies([], home=self.home),
                     lambda: operations.absorb(True, home=self.home),
                     lambda: operations.install({}, [], home=self.home),
                     lambda: operations.undo(operation['id'], home=self.home),
                     lambda: operations.set_source('other', 'test/repo', '.', True, self.home),
                     lambda: updates.apply({'home': str(self.home)}, ['other'], confirmed=True),
                     lambda: store.apply('unused', self.home),
                     lambda: store.restore(operation['backup_id'], self.home),
                     lambda: cleanup.restore(['other'], self.home),
                     lambda: cleanup.apply([], True, self.home)]
            for call in calls:
                with self.subTest(call=call), self.assertRaisesRegex(ValueError, '只读'):
                    call()
        self.assertEqual(before, store.physical_state(self.home))

    def test_manual_add_remove_persists_over_curated_and_exact_undo(self):
        self.fixture.assign('grill-me', 'claude')
        before = self.fixture.fingerprint()
        added = operations.decide_dependencies([{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': True}], home=self.home)
        self.assertIn('grilling', visibility(self.home)['claude']['available'])
        linked = self.fixture.fingerprint()
        removed = operations.decide_dependencies([{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': False}], home=self.home)
        with patch.object(operations, 'curated_dependencies', return_value=({'grill-me': {'grilling'}}, {})):
            self.assertEqual(operations.load(self.home)[1]['skills']['grill-me']['requires'], [])
        self.assertIn('grilling', visibility(self.home)['claude']['available'])
        operations.undo(removed['id'], home=self.home)
        self.assertEqual(linked, self.fixture.fingerprint())
        operations.undo(added['id'], home=self.home)
        self.assertEqual(before, self.fixture.fingerprint())

    def test_readonly_web_rejects_every_write_route_before_payload(self):
        from skill_pilot.web.server import Dashboard
        before = store.physical_state(self.home)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            server = Dashboard({'home': str(self.home)})
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
            thread.start()
            try:
                for route in ['migration/apply', 'restore', 'assignment/apply', 'dependencies',
                              'install/apply', 'absorb', 'undo', 'updates/apply', 'cleanup/apply', 'withdrawn/restore']:
                    headers = {'Origin': server.origin, 'X-Skill-Pilot-Token': server.token,
                               'Content-Type': 'application/json'}
                    request = urllib.request.Request(server.origin+'/api/'+route,
                                                     data=b'{"confirmed":true}', headers=headers)
                    with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request)
                    self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown(); thread.join(); server.server_close()
        self.assertEqual(before, store.physical_state(self.home))

    def test_ui_json_is_structured_and_stops_without_browser_heartbeat(self):
        from skill_pilot.web import server
        output = io.StringIO()
        with patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home)}), \
                patch.object(server, 'lock_path', return_value=self.home / 'dashboard-test.lock'), \
                patch.object(server, 'HEARTBEAT_TIMEOUT', .05), \
                patch.object(server.self_update, 'check', side_effect=ValueError('测试不读取远端')), \
                patch.object(server.webbrowser, 'open', return_value=True), \
                contextlib.redirect_stdout(output):
            server.run(json_output=True)
        result = json.loads(output.getvalue())
        self.assertEqual(result['pid'], os.getpid())
        self.assertTrue(result['url'].startswith('http://127.0.0.1:'))

    def test_plain_body_mentions_never_create_dependencies(self):
        self.assertEqual(operations.load(self.home)[1]['skills']['grill-me']['requires'], [])
        self.fixture.assign('grill-me', 'claude')
        self.assertNotIn('grilling', visibility(self.home)['claude']['available'])


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_updates.UpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_changed_scripts_and_executables_report_file_and_original_line(self):
        script = self.fixture.folder / 'scripts/task.py'
        script.write_text('import urllib.request\n# curl https://example.test; rm -rf temporary; npm install package\n')
        executable = self.fixture.folder / 'tool'
        executable.write_text('#!/bin/sh\necho hello\n')
        executable.chmod(0o755)
        ordinary = self.fixture.folder / 'notes.md'
        ordinary.write_text('curl and rm are discussed in this ordinary note')
        report = updates.comparison(self.fixture.home / '.skill-pilot/bases/sample', self.fixture.folder)
        self.assertTrue(any(hit['file'] == 'scripts/task.py' and hit['line'].startswith('# curl') for hit in report['risk_hits']))
        self.assertTrue(any(hit['file'] == 'tool' for hit in report['risk_hits']))
        self.assertFalse(any(hit['file'] == 'notes.md' for hit in report['risk_hits']))
