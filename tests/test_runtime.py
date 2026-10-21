"""Refresh upgrades use isolated copies, real process replacement, and loopback HTTP."""
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, store, team, team_hub
from skill_pilot.web import runtime, server
from tests import set_up_home


class RefreshRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill-pilot-refresh-http-')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, temporary.name)
        self.dashboard = server.Dashboard({'home': str(self.home)})
        self.changed = threading.Event()
        self.dashboard.reload_callback = self.changed.set
        self.thread = threading.Thread(target=self.dashboard.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.dashboard.shutdown()
        self.thread.join()
        self.dashboard.server_close()

    def request(self, path, body=None, extra=None):
        headers = {'X-Skill-Pilot-Token': self.dashboard.token, 'Origin': self.dashboard.origin,
                   'Content-Type': 'application/json'}
        headers.update(extra or {})
        request = urllib.request.Request(self.dashboard.origin + path, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_refresh_requires_the_same_session_and_origin(self):
        self.assertEqual(self.request('/api/runtime/refresh', {}, {'X-Skill-Pilot-Token': ''})[0], 403)
        self.assertEqual(self.request('/api/runtime/refresh', {}, {'Origin': 'http://elsewhere.invalid'})[0], 403)
        self.assertEqual(self.request('/api/runtime/refresh', {}, {'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertFalse(self.changed.is_set())

    def test_running_request_finishes_before_replacement_and_new_writes_wait(self):
        entered, release = threading.Event(), threading.Event()
        def preview(*args):
            entered.set()
            release.wait(3)
            return {'message': 'preview finished'}
        with patch.object(server.sharing, 'preview_import', side_effect=preview), \
                patch.object(runtime, 'version', return_value='new-program'), ThreadPoolExecutor() as pool:
            future = pool.submit(self.request, '/api/config/preview', {})
            try:
                self.assertTrue(entered.wait(2))
                change = self.request('/api/runtime/refresh', {})[1]
                self.assertTrue(change['changed'])
                self.assertTrue(change['busy'])
                self.request('/api/runtime/refresh', {})
                self.assertFalse(self.changed.wait(.1))
                self.assertEqual(self.request('/api/assignment/apply', {'confirmed': True})[0], 503)
                self.assertEqual(self.request('/api/heartbeat', {})[0], 200)
            finally:
                release.set()
            self.assertEqual(future.result()[0], 200)
            self.assertTrue(self.changed.wait(2))

    def test_static_changes_do_not_require_a_backend_replacement(self):
        with tempfile.TemporaryDirectory(prefix='skill-pilot-code-version-') as temporary:
            root = Path(temporary)
            (root / 'server.py').write_text('value = 1\n')
            (root / 'catalog.json').write_text('{}')
            with patch.object(runtime, 'PACKAGE', root):
                before = runtime.version()
                (root / 'app.css').write_text('body {}')
                self.assertEqual(before, runtime.version())
                (root / 'catalog.json').write_text('{"changed":true}')
                self.assertNotEqual(before, runtime.version())
        self.assertFalse(self.request('/api/runtime/refresh', {})[1]['changed'])
        self.assertFalse(self.changed.is_set())

    def test_unused_browser_connection_does_not_delay_replacement(self):
        with socket.create_connection(self.dashboard.server_address), \
                patch.object(runtime, 'version', return_value='new-program'):
            self.assertTrue(self.request('/api/runtime/refresh', {})[1]['changed'])
            self.assertTrue(self.changed.wait(2))


@unittest.skipIf(os.name == "nt", "POSIX process fixtures; Windows refresh covered by test_windows_update")
class ProcessRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill-pilot-runtime-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'Another User')
        skill = self.home / '.codex/skills/sample'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: sample\ndescription: example\n---\noriginal\n')
        self.code = self.root / 'Skill Pilot project'
        shutil.copytree(store.PROJECT / 'skill_pilot', self.code / 'skill_pilot',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(store.PROJECT / 'docs', self.code / 'docs')
        self.env = {**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '1',
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.before = store.physical_state(self.home)
        self.program = None
        self.addCleanup(self.stop)

    def start(self, busy=False):
        bootstrap = '''
import json, threading, time
from pathlib import Path
from skill_pilot.web import server
original = server.Dashboard.__init__
def initialize(self, inventory, resume=None):
    original(self, inventory, resume)
    self.install_reviews['saved-install'] = {'id':'saved-install', 'temporary':str(Path.cwd()/'download-install'),
        'skills':[{'name':'sample', 'path':str(self.home/'.codex/skills/sample')}]}
    self.update_reviews['saved-check'] = {'id':'saved-check', 'temporary':str(Path.cwd()/'download-update'),
        'skills':[{'name':'sample','has_update':True}], 'running':False, 'update_count':1}
    self.pages.add('another-tab')
    self.labels_progress['message'] = 'saved label result'
    if BUSY:
        self.labels_progress['running'] = True
        def finish():
            while not Path('../release').exists(): time.sleep(.02)
            Path('../completed').write_text('finished before replacement')
            self.labels_progress.update(running=False, message='background completed')
        self.labels_thread = threading.Thread(target=finish, daemon=True)
        self.labels_thread.start()
server.Dashboard.__init__ = initialize
server.webbrowser.open = lambda url: True
server.run(json_output=True)
'''.replace('BUSY', repr(busy))
        self.program = subprocess.Popen([sys.executable, '-B', '-u', '-c', bootstrap],
                                        cwd=self.code, env=self.env, text=True,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue(select.select([self.program.stdout], [], [], 8)[0], 'test dashboard did not start')
        line = self.program.stdout.readline()
        self.assertTrue(line, self.program.stderr.read() if self.program.poll() is not None else 'no startup result')
        self.info = json.loads(line)
        self.origin = self.info['url'].split('/?')[0]
        self.token = self.info['url'].split('token=')[1]

    def stop(self):
        if self.program:
            self.program.terminate()
            try:
                self.program.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.program.kill()
                self.program.communicate()

    def request(self, path, body=None):
        request = urllib.request.Request(self.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-Skill-Pilot-Token': self.token, 'Origin': self.origin, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def wait_status(self, predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                result = self.request('/api/status')[1]
                if predicate(result):
                    return result
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        self.fail('replacement did not reach the expected state')

    def change_backend(self):
        path = self.code / 'skill_pilot/web/server.py'
        original = path.read_text()
        needle = "'version': self.server.code_version, 'reloading': self.server.reload_pending,"
        self.assertIn(needle, original)
        path.write_text(original.replace(needle, "'test_marker': 'new-program', " + needle))

    def test_refresh_waits_for_background_work_and_preserves_address_session_and_previews(self):
        self.start(busy=True)
        initial = self.request('/api/status')[1]
        self.change_backend()
        status, change = self.request('/api/runtime/refresh', {})
        self.assertEqual(status, 200)
        self.assertTrue(change['changed'])
        self.assertTrue(change['busy'])
        time.sleep(.1)
        self.assertEqual(self.request('/api/status')[1]['version'], initial['version'])
        self.assertEqual(self.request('/api/assignment/apply', {'confirmed': True})[0], 503)
        self.assertFalse((self.root / 'completed').exists())
        (self.root / 'release').touch()
        current = self.wait_status(lambda s: s.get('test_marker') == 'new-program')
        self.assertEqual(current['pid'], self.program.pid)
        self.assertNotEqual(current['version'], initial['version'])
        self.assertFalse(current['reloading'])
        self.assertEqual((self.root / 'completed').read_text(), 'finished before replacement')
        self.assertEqual(self.request('/api/updates/progress')[1]['id'], 'saved-check')
        self.assertEqual(self.request('/api/labels/progress')[1]['message'], 'background completed')
        self.assertEqual(self.request('/api/incoming/compare', {'review_id': 'saved-install', 'name': 'sample'})[0], 200)
        # The original singleton lock must survive too: another launch reconnects.
        second = subprocess.run([sys.executable, '-B', '-c',
            'from skill_pilot.web import server; server.webbrowser.open=lambda url: True; server.run(json_output=True)'],
            cwd=self.code, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['message'], '已打开正在运行的看板。')
        self.assertEqual(self.before, store.physical_state(self.home))

    def test_refresh_preserves_team_uploads_and_discards_the_completed_queue(self):
        def discovery_after_scan():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if not self.request('/api/agents')[1]['discovery']['running']:
                    return json.loads((self.home / '.skill-pilot/agent-discovery.json').read_text())
                time.sleep(.01)
            self.fail('agent discovery did not finish')

        def state_without_discovery():
            entries = [p for p in self.home.iterdir() if p.name != '.skill-pilot']
            entries += [p for p in (self.home / '.skill-pilot').iterdir()
                        if p.name != 'agent-discovery.json']
            state = {str(p.relative_to(self.home)): store.physical_state(p) for p in entries}
            state['.'] = self.home.stat().st_mode
            state['.skill-pilot'] = (self.home / '.skill-pilot').stat().st_mode
            state['.skill-pilot/agent-discovery.json'] = (self.home / '.skill-pilot/agent-discovery.json').stat().st_mode
            return state

        plan = store.preview(self.home, ['codex'])
        store.apply(plan['plan_id'], self.home, ['codex'])
        hub = team_hub.HubServer(self.root / 'Team share data', '127.0.0.1', 0)
        thread = threading.Thread(target=hub.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def stop_hub():
            hub.shutdown()
            thread.join(timeout=5)
            hub.server_close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop_hub)
        member = hub.store.add_member('示例成员')
        team.save_settings(self.home, {'url': 'http://127.0.0.1:' + str(hub.server_port), 'key': member['token']})
        saved, _ = team.listings_preview(self.home, [{'name': 'sample'}])
        team.listings_apply(self.home, saved)
        colleague = hub.store.add_member('Acme 示例同事')
        from tests.test_team_hub import listing, file
        incoming = listing('acme-incoming', 'files')
        incoming['source']['files'] = [file('SKILL.md', '---\nname: acme-incoming\ndescription: example\n---\nexample\n')]
        team._request({'url': 'http://127.0.0.1:' + str(hub.server_port), 'key': colleague['token']},
                      '/api/listings/me', 'PUT', {'listings': [incoming]})
        shutil.copytree(store.PROJECT / 'skill', self.code / 'skill')
        self.env['SKILL_PILOT_READ_ONLY'] = '0'
        self.start()
        status, upload = self.request('/api/team/listings/preview', {'skills': [{'name': 'sample'}]})
        self.assertEqual(status, 200, upload)
        status, queued = self.request('/api/team/queue', {'key': 'team:' + colleague['id'] + ':acme-incoming'})
        self.assertEqual(status, 200, queued)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            queued = self.request('/api/team/queue')[1]
            if queued['items'][0]['status'] in {'done', 'failed'}:
                break
            time.sleep(.01)
        self.assertEqual(queued['items'][0]['status'], 'done', queued)
        self.assertEqual(operations.load(self.home)[1]['skills']['acme-incoming']['assigned_agents'], [])
        discovery_before = discovery_after_scan()
        before = state_without_discovery()
        self.change_backend()
        path = self.code / 'skill_pilot/web/server.py'
        source = path.read_text()
        needle = "'test_marker': 'new-program', "
        path.write_text(source.replace(needle, needle +
            "'test_team_uploads': self.server.team_upload_reviews, "))
        self.request('/api/runtime/refresh', {})
        current = self.wait_status(lambda s: s.get('test_marker') == 'new-program')
        self.assertEqual(current['test_team_uploads'][upload['review']]['listings'][0]['name'], 'sample')
        self.assertEqual(self.request('/api/team/queue'), (200, {'items': []}))
        # Restarting legitimately refreshes the discovery time, not the skill data.
        discovery_after = discovery_after_scan()
        self.assertEqual({**discovery_after, 'checked_at': ''}, {**discovery_before, 'checked_at': ''})
        self.assertGreaterEqual(datetime.fromisoformat(discovery_after['checked_at']),
                                datetime.fromisoformat(discovery_before['checked_at']))
        self.assertEqual(state_without_discovery(), before)

    def test_unloadable_update_keeps_old_program_available_then_allows_retry(self):
        self.start()
        initial = self.request('/api/status')[1]
        path = self.code / 'skill_pilot/web/server.py'
        original = path.read_text()
        path.write_text(original + "\nraise RuntimeError('unfinished update')\n")
        self.request('/api/runtime/refresh', {})
        failed = self.wait_status(lambda s: bool(s['reload_error']))
        self.assertEqual(failed['version'], initial['version'])
        self.assertEqual(failed['pid'], self.program.pid)
        self.assertFalse(failed['reloading'])
        self.assertEqual(self.request('/api/updates/progress')[0], 200)
        path.write_text(original)
        self.change_backend()
        self.request('/api/runtime/refresh', {})
        current = self.wait_status(lambda s: s.get('test_marker') == 'new-program')
        self.assertFalse(current['reload_error'])
        self.assertEqual(self.before, store.physical_state(self.home))

    def test_frontend_changes_are_served_immediately_without_replacing_the_backend(self):
        self.start()
        initial = self.request('/api/status')[1]
        path = self.code / 'skill_pilot/web/static/app.css'
        path.write_text(path.read_text() + '\n/* immediate frontend update */\n')
        with urllib.request.urlopen(self.origin + '/app.css?token=' + self.token) as response:
            self.assertIn(b'immediate frontend update', response.read())
        self.assertFalse(self.request('/api/runtime/refresh', {})[1]['changed'])
        self.assertEqual(self.request('/api/status')[1]['version'], initial['version'])
        self.assertEqual(self.before, store.physical_state(self.home))

    def test_invalid_command_entry_keeps_the_running_dashboard_available(self):
        self.start()
        initial = self.request('/api/status')[1]
        (self.code / 'skill_pilot/cli.py').write_text("raise RuntimeError('unfinished command entry')\n")
        self.request('/api/runtime/refresh', {})
        failed = self.wait_status(lambda s: bool(s['reload_error']))
        self.assertEqual(failed['pid'], initial['pid'])
        self.assertEqual(failed['version'], initial['version'])
        self.assertFalse(failed['reloading'])
        self.assertEqual(self.request('/api/updates/progress')[0], 200)
        self.assertEqual(self.before, store.physical_state(self.home))

    def test_reverting_the_pending_update_cancels_replacement(self):
        self.start(busy=True)
        path = self.code / 'skill_pilot/web/server.py'
        original = path.read_text()
        self.change_backend()
        self.assertTrue(self.request('/api/runtime/refresh', {})[1]['changed'])
        path.write_text(original)
        self.assertFalse(self.request('/api/runtime/refresh', {})[1]['changed'])
        self.assertFalse(self.request('/api/status')[1]['reloading'])
        (self.root / 'release').touch()
        self.assertEqual(self.before, store.physical_state(self.home))
