import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tests import test_operations
from skill_pilot import store, agent_discovery
from skill_pilot.web.server import Dashboard


class AgentWebTests(unittest.TestCase):
    def test_api_adds_agent_from_a_folder_with_spaces_and_undoes(self):
        folder = self.home / 'My Agents/New Tool/skills'
        self.skill('My Agents/New Tool/skills/hello', 'hello')
        before = self.fingerprint()
        status, plan = self.request('/api/agents/join/preview',
                                    {'agents': [{'name': '新工具', 'skill_root': str(folder)}]})
        self.assertEqual(status, 200, plan)
        self.assertEqual(self.fingerprint(), before)
        status, result = self.request('/api/agents/join/apply', {
            'agents': [{'name': '新工具', 'skill_root': str(folder)}],
            'plan_id': plan['plan_id'], 'revision': plan['revision'], 'confirmed': True})
        self.assertEqual(status, 200, result)
        _, table = self.request('/api/table')
        status, undone = self.request('/api/undo', {'id': result['id'], 'revision': table['revision'], 'confirmed': True})
        self.assertEqual(status, 200, undone)
        self.assertEqual(self.fingerprint(), before)

    skill = test_operations.OperationsTests.skill
    fingerprint = test_operations.OperationsTests.fingerprint

    def setUp(self):
        test_operations.OperationsTests.setUp(self)
        self.skill('.fifth/skills/extra', 'extra')
        self.server = Dashboard({'home': str(self.home)})
        self.server.agent_findings = agent_discovery.find(self.home)
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
                                           'reason': '你之前选了忽略或不再管理。'}])
        status, plan = self.request('/api/agents/join/preview', {'agents': ['fifth']})
        self.assertEqual(status, 200)
        status, rejoined = self.request('/api/agents/join/apply', {'agents': ['fifth'], 'plan_id': plan['plan_id'],
                                             'revision': plan['revision'], 'confirmed': True})
        self.assertEqual(status, 200)
        view = self.request('/api/agents')[1]
        self.assertEqual(view['ignored'], [])
        revision = self.request('/api/table')[1]['revision']
        self.assertEqual(self.request('/api/undo', {'id': rejoined['id'], 'revision': revision, 'confirmed': True})[0], 200)
        view = self.request('/api/agents')[1]
        self.assertEqual([a['id'] for a in view['ignored']], ['fifth'])
        revision = self.request('/api/table')[1]['revision']
        self.assertEqual(self.request('/api/undo', {'id': left['id'], 'revision': revision, 'confirmed': True})[0], 200)
        self.assertEqual(self.request('/api/agents')[1]['ignored'], [])
        revision = self.request('/api/table')[1]['revision']
        self.assertEqual(self.request('/api/undo', {'id': joined['id'], 'revision': revision, 'confirmed': True})[0], 200)
        self.assertEqual(self.fingerprint(), point)

    def test_agent_list_is_cached_until_a_write(self):
        from skill_pilot.web import server as web
        calls = []
        real = web.agent_management.overview

        def slow(home, **options):
            calls.append(home)
            time.sleep(0.25)
            return real(home, **options)

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
                                           'reason': '你之前选了忽略或不再管理。'}])
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

    def test_rescan_finds_a_new_agent_without_restarting_or_touching_allocations(self):
        self.request('/api/agents')
        original = self.fingerprint()
        (self.home / 'Applications/WorkBuddy.app').mkdir(parents=True)
        self.assertNotIn('workbuddy', [a['id'] for a in self.request('/api/agents')[1]['suggested']])
        status, progress = self.request('/api/agents/scan', {})
        self.assertEqual(status, 200)
        self.server.agents_thread.join(3)
        view = self.request('/api/agents')[1]
        self.assertFalse(view['discovery']['running'])
        self.assertTrue(view['discovery']['checked_at'])
        self.assertEqual(view['discovery']['message'], '发现 1 个新 agent')
        self.assertIn('workbuddy', [a['id'] for a in view['suggested']])
        self.request('/api/agents/scan', {})
        self.server.agents_thread.join(3)
        again = self.request('/api/agents')[1]
        self.assertEqual(again['discovery']['message'], '没有更多新的 agent')
        self.assertIn('workbuddy', [a['id'] for a in again['suggested']])
        self.assertEqual(self.fingerprint(), original)
        reopened = Dashboard({'home': str(self.home), 'deferred': True})
        self.addCleanup(reopened.server_close)
        with patch.object(agent_discovery, 'find', side_effect=AssertionError('must use saved findings')):
            self.assertIn('workbuddy', [a['id'] for a in reopened.agents_view()['suggested']])

    def test_duplicate_scans_share_one_worker_and_errors_can_retry(self):
        entered, release = threading.Event(), threading.Event()
        real = agent_discovery.find
        def slow(*args):
            entered.set()
            release.wait(3)
            return real(*args)
        with patch.object(agent_discovery, 'find', side_effect=slow) as find:
            self.request('/api/agents/scan', {})
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.request('/api/agents/scan', {})[1]['running'])
            self.assertTrue(self.request('/api/agents')[1]['discovery']['running'])
            self.assertEqual(find.call_count, 1)
            release.set()
            self.server.agents_thread.join(3)
        saved = (self.home / agent_discovery.CACHE).read_bytes()
        original = self.request('/api/agents')[1]['suggested']
        with patch.object(agent_discovery, 'find', side_effect=PermissionError('unreadable')):
            self.request('/api/agents/scan', {})
            self.server.agents_thread.join(3)
        failed = self.request('/api/agents')[1]
        self.assertFalse(failed['discovery']['running'])
        self.assertIn('请重试', failed['discovery']['error'])
        self.assertEqual(failed['suggested'], original)
        self.assertEqual((self.home / agent_discovery.CACHE).read_bytes(), saved)
        self.request('/api/agents/scan', {})
        self.server.agents_thread.join(3)
        self.assertEqual(self.request('/api/agents')[1]['discovery']['error'], '')

    def test_scan_with_nothing_waiting_says_none_found(self):
        status, plan = self.request('/api/agents/ignore/preview', {'agent': 'fifth'})
        self.assertEqual(status, 200)
        status, _written = self.request('/api/agents/ignore/apply', {
            'agent': 'fifth', 'plan_id': plan['plan_id'], 'revision': plan['revision'], 'confirmed': True})
        self.assertEqual(status, 200)
        self.request('/api/agents/scan', {})
        self.server.agents_thread.join(3)
        view = self.request('/api/agents')[1]
        self.assertEqual(view['suggested'], [])
        self.assertEqual(view['discovery']['message'], '检查完成，没有发现新的 agent')

    def test_background_check_and_agent_view_do_not_scan_skill_contents(self):
        with patch('skill_pilot.scan.scan', side_effect=AssertionError('full scan')), \
                patch('skill_pilot.agents.scan_root', side_effect=AssertionError('skill read')), \
                patch('skill_pilot.operations.revision', side_effect=AssertionError('allocation read')):
            self.server.start_agents_scan()
            self.server.agents_thread.join(3)
            self.assertEqual(self.request('/api/agents')[0], 200)

    def test_deferred_skill_inventory_is_loaded_only_when_requested(self):
        self.server.inventory = {'home': str(self.home), 'deferred': True}
        from skill_pilot.web import server as web
        with patch.object(web, 'scan', return_value={'generated_at': 'now', 'agents': []}) as scan:
            self.request('/api/agents')
            scan.assert_not_called()
            self.assertEqual(self.request('/api/scan')[1]['generated_at'], 'now')
            self.request('/api/scan')
            scan.assert_called_once_with(self.home)
