import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tests import test_operations
from skill_pilot import store
from skill_pilot.web.server import Dashboard


class AgentWebTests(unittest.TestCase):
    skill = test_operations.OperationsTests.skill
    fingerprint = test_operations.OperationsTests.fingerprint

    def setUp(self):
        test_operations.OperationsTests.setUp(self)
        self.skill('.fifth/skills/extra', 'extra')
        self.server = Dashboard({'home': str(self.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, body=None):
        request = urllib.request.Request(self.server.origin + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Origin': self.server.origin, 'X-Skill-Pilot-Token': self.server.token})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_read_preview_write_guards_and_roundtrip(self):
        status, view = self.request('/api/agents')
        self.assertEqual(status, 200)
        self.assertEqual([a['id'] for a in view['managed']], ['codex', 'claude', 'antigravity', 'dsh'])
        self.assertIn('fifth', [a['id'] for a in view['suggested']])
        self.assertEqual(view['ignored'], [])
        self.assertNotIn('usage_note', view['managed'][2])
        point = self.fingerprint()
        status, plan = self.request('/api/agents/join/preview', {'agents': ['fifth']})
        self.assertEqual(status, 200)
        body = {'agents': ['fifth'], 'plan_id': plan['plan_id'], 'revision': plan['revision']}
        self.assertEqual(self.request('/api/agents/join/apply', body)[0], 400)
        for bad in [None, '', 'stale']:
            self.assertEqual(self.request('/api/agents/join/apply', {**body, 'confirmed': True, 'revision': bad})[0], 409)
        with patch.object(store, 'write_allowed', return_value=False):
            self.assertEqual(self.request('/api/agents/join/apply', {**body, 'confirmed': True})[0], 403)
        self.assertEqual(self.fingerprint(), point)
        status, joined = self.request('/api/agents/join/apply', {**body, 'confirmed': True})
        self.assertEqual(status, 200)
        status, view = self.request('/api/table')
        self.assertEqual(view['agents'][-1]['id'], 'fifth')
        self.assertNotIn('usage_available', view['agents'][-1])
        self.assertNotIn('usage_note', view['agents'][-1])
        self.assertTrue(view['notes'])
        status, draft = self.request('/api/assignment/preview', {'changes': [{'skill': 'other', 'agent': 'fifth', 'enabled': True}]})
        self.assertEqual(status, 200)
        self.assertNotIn('revision', draft)
        self.assertIn('cell_notes', draft)
        status, plan = self.request('/api/agents/leave/preview', {'agent': 'fifth'})
        self.assertEqual(status, 200)
        self.assertNotIn('_registry', plan)
        self.assertEqual(plan['message'], '不再管理 Fifth；它文件夹里的技能原样留着，照常能用。')
        body = {'agent': 'fifth', 'plan_id': plan['plan_id'], 'revision': plan['revision'], 'confirmed': True}
        status, left = self.request('/api/agents/leave/apply', body)
        self.assertEqual(status, 200)
        self.assertEqual(left['message'], plan['message'])
        view = self.request('/api/agents')[1]
        self.assertNotIn('fifth', [a['id'] for a in view['suggested']])
        self.assertEqual(view['ignored'], [{'id': 'fifth', 'name': 'Fifth', 'skill_roots': ['.fifth/skills'],
                                           'skill_count': 1, 'reason': '你之前选了忽略或不再管理。'}])
        status, plan = self.request('/api/agents/join/preview', {'agents': ['fifth']})
        self.assertEqual(status, 200)
        status, rejoined = self.request('/api/agents/join/apply', {'agents': ['fifth'], 'plan_id': plan['plan_id'],
                                             'revision': plan['revision'], 'confirmed': True})
        self.assertEqual(status, 200)
        view = self.request('/api/agents')[1]
        self.assertEqual(view['ignored'], [])
        self.assertEqual(self.request('/api/undo', {'id': rejoined['id'], 'revision': view['revision'], 'confirmed': True})[0], 200)
        view = self.request('/api/agents')[1]
        self.assertEqual([a['id'] for a in view['ignored']], ['fifth'])
        revision = view['revision']
        self.assertEqual(self.request('/api/undo', {'id': left['id'], 'revision': revision, 'confirmed': True})[0], 200)
        self.assertEqual(self.request('/api/agents')[1]['ignored'], [])
        revision = self.request('/api/table')[1]['revision']
        self.assertEqual(self.request('/api/undo', {'id': joined['id'], 'revision': revision, 'confirmed': True})[0], 200)
        self.assertEqual(self.fingerprint(), point)

    def test_agent_list_is_cached_until_a_write(self):
        from skill_pilot.web import server as web
        calls = []
        real = web.agent_management.overview

        def slow(home):
            calls.append(home)
            time.sleep(0.25)
            return real(home)

        with patch.object(web.agent_management, 'overview', side_effect=slow):
            status, first = self.request('/api/agents')
            self.assertEqual(status, 200)
            started = time.perf_counter()
            status, second = self.request('/api/agents')
            elapsed = time.perf_counter() - started
            self.assertEqual(status, 200)
            self.assertEqual(second, first)
            self.assertLess(elapsed, 0.2)
            self.assertEqual(len(calls), 1)
            status, plan = self.request('/api/agents/ignore/preview', {'agent': 'fifth'})
            self.assertEqual(status, 200)
            self.assertEqual(len(calls), 1)
            status, _written = self.request('/api/agents/ignore/apply', {
                'agent': 'fifth', 'plan_id': plan['plan_id'], 'revision': plan['revision'], 'confirmed': True})
            self.assertEqual(status, 200)
            status, after = self.request('/api/agents')
            self.assertEqual(status, 200)
            self.assertEqual([item['id'] for item in after['ignored']], ['fifth'])
            self.assertEqual(len(calls), 2)

    def test_ignore_endpoints_and_generic_scan_fields(self):
        status, plan = self.request('/api/agents/ignore/preview', {'agent': 'fifth'})
        self.assertEqual(status, 200)
        self.assertNotIn('_profile', plan)
        status, result = self.request('/api/agents/ignore/apply', {'agent': 'fifth', 'plan_id': plan['plan_id'], 'revision': plan['revision'], 'confirmed': True})
        self.assertEqual(status, 200)
        view = self.request('/api/agents')[1]
        self.assertEqual(view['ignored'], [{'id': 'fifth', 'name': 'Fifth', 'skill_roots': ['.fifth/skills'],
                                           'skill_count': 1, 'reason': '你之前选了忽略或不再管理。'}])
        self.assertNotIn('fifth', [a['id'] for a in view['suggested']])
        status, plan = self.request('/api/agents/join/preview', {'agents': ['fifth']})
        self.assertEqual(status, 200)
        status, joined = self.request('/api/agents/join/apply', {'agents': ['fifth'], 'plan_id': plan['plan_id'],
                                             'revision': plan['revision'], 'confirmed': True})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('/api/agents')[1]['ignored'], [])
        view = self.request('/api/scan')[1]
        self.assertIn('disabled', view)
        self.assertNotIn('codex_disabled', view)
        for root in view['roots']:
            for skill in root['skills']:
                self.assertIn('manual_only_for', skill)
                self.assertNotIn('codex_implicit_invocation', skill)
