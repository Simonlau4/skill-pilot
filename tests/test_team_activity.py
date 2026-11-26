"""Team hub member activity tracking, throttling, privacy, and compatibility tests."""
import http.client
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from skill_pilot import cli, diagnostics, store, team, team_hub
from skill_pilot.web.server import Dashboard
from tests import set_up_home


class TeamHubActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot activity hub ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, self.temp.name)
        self.data = self.home / 'team hub data'
        self.store = team_hub.HubStore(self.data)
        self.first = self.store.add_member('Acme 成员一')
        self.second = self.store.add_member('Acme 成员二')
        self.server = team_hub.HubServer(self.data, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def request(self, path='/api/activity', method='POST', data=None, token='first', headers=None):
        if token == 'first':
            token = self.first['token']
        elif token == 'second':
            token = self.second['token']
        supplied = {'Authorization': 'Bearer ' + token} if token is not None else {}
        if data is not None:
            supplied.setdefault('Content-Type', 'application/json')
        supplied.update(headers or {})
        body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            self.assertEqual(response.headers.get_content_type(), 'application/json')
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return response.status, json.loads(response.read()) if method != 'HEAD' else None
        finally:
            connection.close()

    def raw_request(self, headers, body=b'', path='/api/activity', method='POST', token='first'):
        if token == 'first':
            token = self.first['token']
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            rows = [('Host', 'localhost'), *([('Authorization', 'Bearer ' + token)] if token is not None else []), *headers]
            head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in rows)
            connection.sendall(head.encode() + b'\r\n' + body)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def test_auth_methods_routes_and_body_validation(self):
        for method in ('GET', 'POST'):
            for token in (None, '', 'wrong'):
                with self.subTest(method=method, token=token):
                    status, body = self.request('/api/activity', method, {} if method == 'POST' else None, token=token)
                    self.assertEqual(status, 401)
                    self.assertEqual(set(body), {'error'})
        self.assertEqual(self.request('/api/activity', 'GET', token=None, headers={'Authorization': 'Basic abc'})[0], 401)
        for method in ('PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'):
            self.assertEqual(self.request('/api/activity', method)[0], 405)
        for path, method, expected in [
            ('/api/activity?foo=1', 'GET', 404),
            ('/api/activity?days=7&days=8', 'GET', 404),
            ('/api/activity?days=7', 'POST', 404),
            ('/api/activity?days=0', 'GET', 400),
            ('/api/activity?days=91', 'GET', 400),
            ('/api/activity?days=abc', 'GET', 400),
        ]:
            with self.subTest(path=path, method=method):
                self.assertEqual(self.request(path, method, {} if method == 'POST' else None)[0], expected)
        for headers, expected in [
            ([], 411),
            ([('Content-Length', str(team_hub.MAX_ACTIVITY_BODY + 1))], 413),
            ([('Content-Length', '-1')], 400),
            ([('Transfer-Encoding', 'chunked')], 400),
        ]:
            self.assertEqual(self.raw_request(headers)[0], expected)
        for raw in (b'{broken', b'[]', b'null', b'123'):
            self.assertEqual(self.raw_request([('Content-Length', str(len(raw)))], raw)[0], 400)
        self.assertFalse((self.data / 'activity.json').exists())

    def test_daily_aggregation_last_active_privacy_and_window_filtering(self):
        with patch.object(team_hub, 'now', return_value='2026-11-20T09:00:00+00:00'):
            status, body = self.request('/api/activity', 'POST', {
                'skills': ['private-skill'], 'path': '/Users/example/secret', 'os': 'macOS'
            }, token='first')
            self.assertEqual(status, 200)
            self.assertEqual(body, {
                'recorded': True, 'date': '2026-11-20', 'opens_today': 1,
                'last_active_at': '2026-11-20T09:00:00+00:00',
            })
        with patch.object(team_hub, 'now', return_value='2026-11-26T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[1]['opens_today'], 1)
        with patch.object(team_hub, 'now', return_value='2026-11-26T10:30:00+00:00'):
            res = self.request('/api/activity', 'POST', {}, token='first')[1]
            self.assertEqual(res['opens_today'], 2)
            self.assertEqual(res['last_active_at'], '2026-11-26T10:30:00+00:00')
        with patch.object(team_hub, 'now', return_value='2026-11-26T12:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='second')[1]['opens_today'], 1)
            raw_file = (self.data / 'activity.json').read_text(encoding='utf-8')
            for forbidden in ('private-skill', '/Users/example/secret', 'macOS', self.first['token'], self.second['token'], 'token_sha256'):
                self.assertNotIn(forbidden, raw_file)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE((self.data / 'activity.json').stat().st_mode), 0o600)
            self.assertFalse(any(p.name.startswith('.activity.json.') for p in self.data.iterdir()))
            status, report = self.request('/api/activity', 'GET', token='first')
            self.assertEqual(status, 200)
            self.assertEqual(report['window_days'], 30)
            self.assertEqual(report['summary'], {
                'total_members': 2, 'active_members': 2, 'active_today': 2, 'total_opens': 4,
            })
            self.assertEqual([m['id'] for m in report['members']], [self.second['id'], self.first['id']])
            by_id = {m['id']: m for m in report['members']}
            self.assertEqual(by_id[self.first['id']]['active_days'], 2)
            self.assertEqual(by_id[self.first['id']]['total_opens'], 3)
            self.assertEqual(by_id[self.first['id']]['opens_today'], 2)
            self.assertEqual(by_id[self.first['id']]['daily'], {'2026-11-20': 1, '2026-11-26': 2})
            self.assertEqual(by_id[self.first['id']]['last_active_at'], '2026-11-26T10:30:00+00:00')
            # Window of 3 days excludes 2026-11-20 while preserving 2026-11-26
            status, short = self.request('/api/activity?days=3', 'GET', token='second')
            self.assertEqual(status, 200)
            self.assertEqual(short['window_days'], 3)
            short_first = next(m for m in short['members'] if m['id'] == self.first['id'])
            self.assertEqual(short_first['active_days'], 1)
            self.assertEqual(short_first['total_opens'], 2)
            self.assertEqual(short_first['daily'], {'2026-11-26': 2})

    def test_retention_window_and_size_cap_pruning(self):
        # Day 1 -> 2026-01-01; Day 90 (89 days later) -> 2026-03-31; Day 91 -> 2026-04-01
        with patch.object(team_hub, 'now', return_value='2026-01-01T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
        with patch.object(team_hub, 'now', return_value='2026-01-02T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
        with patch.object(team_hub, 'now', return_value='2026-03-31T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
            saved = json.loads((self.data / 'activity.json').read_text(encoding='utf-8'))['activity']
            self.assertEqual(sorted(saved[self.first['id']]['days']), ['2026-01-01', '2026-01-02', '2026-03-31'])
        with patch.object(team_hub, 'now', return_value='2026-04-01T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
            saved = json.loads((self.data / 'activity.json').read_text(encoding='utf-8'))['activity']
            self.assertEqual(sorted(saved[self.first['id']]['days']), ['2026-01-02', '2026-03-31', '2026-04-01'])

        # Size cap evicts oldest calendar days first, then least-recently-active member.
        with patch.object(team_hub, 'ACTIVITY_MAX_BYTES', 260), \
                patch.object(team_hub, 'now', return_value='2026-04-02T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='second')[0], 200)
            raw_bytes = (self.data / 'activity.json').read_bytes()
            self.assertLessEqual(len(raw_bytes), 260)
            saved = json.loads(raw_bytes.decode('utf-8'))['activity']
            self.assertIn(self.second['id'], saved)
            self.assertEqual(saved[self.second['id']]['days'], {'2026-04-02': 1})

    def test_concurrent_pings_preserve_all_counts(self):
        barrier = threading.Barrier(4)
        def ping(_):
            barrier.wait(timeout=3)
            return self.request('/api/activity', 'POST', {}, token='first')[0]
        with patch.object(team_hub, 'now', return_value='2026-11-26T09:00:00+00:00'):
            with ThreadPoolExecutor(max_workers=4) as executor:
                statuses = list(executor.map(ping, range(4)))
            self.assertEqual(statuses, [200, 200, 200, 200])
            report = self.request('/api/activity', 'GET', token='first')[1]
            row = next(m for m in report['members'] if m['id'] == self.first['id'])
            self.assertEqual(row['opens_today'], 4)
            self.assertEqual(row['total_opens'], 4)

    def test_remove_member_cleans_activity_atomically_and_preserves_other_members(self):
        # Removing a member before activity.json exists must not create activity.json.
        third = self.store.add_member('Acme 成员三')
        self.store.remove_member(third['id'])
        self.assertFalse((self.data / 'activity.json').exists())

        with patch.object(team_hub, 'now', return_value='2026-11-26T09:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='second')[0], 200)
        before = (self.data / 'activity.json').read_bytes()
        write = team_hub.atomic_write
        def fail_members(path, data):
            if path == self.data / 'members.json':
                raise OSError('simulated members write failure')
            return write(path, data)
        with patch.object(team_hub, 'atomic_write', side_effect=fail_members), self.assertRaises(OSError):
            self.store.remove_member(self.first['id'])
        self.assertEqual((self.data / 'activity.json').read_bytes(), before)

        # Rollback when writing activity.json itself fails (written after members.json).
        before_members = (self.data / 'members.json').read_bytes()
        def fail_activity(path, data):
            if path == self.data / 'activity.json':
                raise OSError('simulated activity write failure')
            return write(path, data)
        with patch.object(team_hub, 'atomic_write', side_effect=fail_activity), self.assertRaises(OSError):
            self.store.remove_member(self.first['id'])
        self.assertEqual((self.data / 'members.json').read_bytes(), before_members)
        self.assertEqual((self.data / 'activity.json').read_bytes(), before)

        self.store.remove_member(self.first['id'])
        saved = json.loads((self.data / 'activity.json').read_text(encoding='utf-8'))['activity']
        self.assertNotIn(self.first['id'], saved)
        self.assertIn(self.second['id'], saved)
        self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 401)
        report = self.request('/api/activity', 'GET', token='second')[1]
        self.assertEqual([m['id'] for m in report['members']], [self.second['id']])

    def test_corrupt_activity_file_does_not_block_remove_member_or_startup(self):
        corrupt = b'{not valid json'
        (self.data / 'activity.json').write_bytes(corrupt)
        # HubServer starts cleanly even if activity.json is corrupted.
        with team_hub.HubServer(self.data, '127.0.0.1', 0):
            self.assertEqual((self.data / 'activity.json').read_bytes(), corrupt)
        # remove_member still revokes token & deletes member, preserving corrupt file with a warning.
        with redirect_stderr(io.StringIO()) as err:
            self.store.remove_member(self.first['id'])
        self.assertIn('活跃记录文件损坏', err.getvalue())
        self.assertEqual((self.data / 'activity.json').read_bytes(), corrupt)
        self.assertEqual(self.request('/api/me', 'GET', token='first')[0], 401)
        self.assertEqual([m['id'] for m in self.store.members()], [self.second['id']])

    def test_startup_prunes_expired_activity_and_clock_rollback_bounds_future_dates(self):
        with patch.object(team_hub, 'now', return_value='2026-01-01T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
        with patch.object(team_hub, 'now', return_value='2026-03-31T08:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='second')[0], 200)
        # Restarting hub 91 days after 2026-01-01 prunes first's expired record on disk without new writes.
        with patch.object(team_hub, 'now', return_value='2026-04-02T08:00:00+00:00'):
            with team_hub.HubServer(self.data, '127.0.0.1', 0):
                pass
        saved = json.loads((self.data / 'activity.json').read_text(encoding='utf-8'))['activity']
        self.assertNotIn(self.first['id'], saved)
        self.assertIn(self.second['id'], saved)

        # Clock rollback: second has a future-dated entry (2026-03-31) when clock rolls back to 2026-03-30.
        with patch.object(team_hub, 'now', return_value='2026-03-30T10:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
            status, one_day = self.request('/api/activity?days=1', 'GET', token='first')
            self.assertEqual(status, 200)
            by_id = {m['id']: m for m in one_day['members']}
            self.assertEqual(by_id[self.first['id']]['active_days'], 1)
            self.assertEqual(by_id[self.first['id']]['total_opens'], 1)
            self.assertEqual(by_id[self.second['id']]['active_days'], 0)
            self.assertEqual(by_id[self.second['id']]['total_opens'], 0)
            self.assertEqual(by_id[self.second['id']]['daily'], {})
            self.assertIsNone(by_id[self.second['id']]['last_active_at'])
        # When first has both today (03-30) and future (03-31), --days 1 counts only 03-30.
        with patch.object(team_hub, 'now', return_value='2026-03-31T09:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
        with patch.object(team_hub, 'now', return_value='2026-03-30T11:00:00+00:00'):
            one_day = self.request('/api/activity?days=1', 'GET', token='first')[1]
            first_row = next(m for m in one_day['members'] if m['id'] == self.first['id'])
            self.assertEqual(first_row['active_days'], 1)
            self.assertEqual(first_row['total_opens'], 1)
            self.assertEqual(first_row['daily'], {'2026-03-30': 1})

    def test_restart_preserves_activity_and_corrupt_file_returns_500(self):
        with patch.object(team_hub, 'now', return_value='2026-11-26T09:00:00+00:00'):
            self.assertEqual(self.request('/api/activity', 'POST', {}, token='first')[0], 200)
        with team_hub.HubServer(self.data, '127.0.0.1', 0) as restarted:
            with patch.object(team_hub, 'now', return_value='2026-11-26T10:00:00+00:00'):
                summary = restarted.store.activity_summary()
                first_row = next(m for m in summary['members'] if m['id'] == self.first['id'])
                self.assertEqual(first_row['opens_today'], 1)
        (self.data / 'activity.json').write_text('{not valid json', encoding='utf-8')
        status, body = self.request('/api/activity', 'GET', token='first')
        self.assertEqual(status, 500)
        self.assertIn('活跃记录没能读懂', body['error'])


class TeamHubActivityCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot activity cli ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, self.temp.name)
        self.data = self.home / 'team hub data'

    def command(self, prefix, *args):
        return subprocess.run(
            [sys.executable, '-m', 'skill_pilot', prefix, *args],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'SKILL_PILOT_HOME': str(self.home / 'ignored home'),
                 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=10,
        )

    def test_cli_hub_and_team_hub_activity_human_json_and_remove(self):
        for prefix in ('hub', 'team-hub'):
            missing = self.command(prefix, 'activity')
            self.assertEqual(missing.returncode, 2)
            self.assertIn('--data', missing.stderr)

        empty = self.command('team-hub', 'activity', '--data', str(self.data))
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn('还没有成员', empty.stdout)

        store_obj = team_hub.HubStore(self.data)
        first = store_obj.add_member('Acme 成员甲')
        second = store_obj.add_member('Acme 成员乙')
        now_utc = team_hub.datetime.now(team_hub.timezone.utc).replace(microsecond=0)
        today_str = now_utc.date().isoformat()
        yesterday_dt = now_utc - team_hub.timedelta(days=1)
        yesterday_str = yesterday_dt.date().isoformat()
        with store_obj.locked():
            with patch.object(team_hub, 'now', return_value=yesterday_dt.isoformat()):
                store_obj._record_activity(first['id'])
            with patch.object(team_hub, 'now', return_value=now_utc.isoformat()):
                store_obj._record_activity(first['id'])
                store_obj._record_activity(first['id'])

        human = self.command('team-hub', 'activity', '--days', '90', '--data', str(self.data))
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('Acme 成员甲', human.stdout)
        self.assertIn('Acme 成员乙', human.stdout)
        self.assertIn(f'{today_str}(2次)', human.stdout)
        self.assertIn(f'{yesterday_str}(1次)', human.stdout)
        self.assertIn('暂无记录', human.stdout)
        for secret in (first['token'], second['token'], 'token_sha256', str(self.home)):
            self.assertNotIn(secret, human.stdout + human.stderr)

        as_json = self.command('hub', 'activity', '--days', '90', '--json', '--data', str(self.data))
        self.assertEqual(as_json.returncode, 0, as_json.stderr)
        parsed = json.loads(as_json.stdout)
        self.assertEqual(parsed['window_days'], 90)
        self.assertEqual(parsed['summary']['total_members'], 2)
        self.assertEqual(parsed['summary']['active_members'], 1)
        self.assertEqual(parsed['summary']['total_opens'], 3)
        self.assertNotIn(first['token'], as_json.stdout)
        self.assertNotIn('token_sha256', as_json.stdout)

        bad_days = self.command('team-hub', 'activity', '--days', '0', '--data', str(self.data))
        self.assertEqual(bad_days.returncode, 1)
        self.assertIn('统计天数', bad_days.stdout)

        removed = self.command('team-hub', 'remove-member', first['id'], '--data', str(self.data))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        after = json.loads(self.command('team-hub', 'activity', '--json', '--data', str(self.data)).stdout)
        self.assertEqual([m['id'] for m in after['members']], [second['id']])
        self.assertFalse((self.home / 'ignored home').exists())

    def test_activity_cli_never_resolves_local_home_or_tools(self):
        argv = ['skill-pilot', 'team-hub', 'activity', '--data', str(self.data)]
        with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()), \
                patch.object(cli, 'target_home', side_effect=AssertionError('must not resolve local home')), \
                patch.object(cli, 'tool_environment', side_effect=AssertionError('must not discover local tools')):
            cli.main()


class TeamDashboardActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot activity dash ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'User Home A')
        self.other = self.root / 'User Home B'
        self.other.mkdir(parents=True)
        for home in (self.home, self.other):
            skill_dir = home / '.codex/skills/private-local-skill'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text('---\nname: private-local-skill\ndescription: 本地测试技能\n---\n', encoding='utf-8')
            plan = store.preview(home, ['codex'])
            store.apply(plan['plan_id'], home, ['codex'])
        self.hub = self._start(team_hub.HubServer(self.root / 'Team Hub Data', '127.0.0.1', 0))
        self.url = f'http://127.0.0.1:{self.hub.server_port}'
        self.first = self.hub.store.add_member('示例成员一')
        self.second = self.hub.store.add_member('示例成员二')
        self.dash_a = self._start(Dashboard({'home': str(self.home)}))
        self.dash_b = self._start(Dashboard({'home': str(self.other)}))

    def _start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return server

    def _get(self, server, path):
        request = urllib.request.Request(
            server.origin + path,
            headers={'Origin': server.origin, 'X-Skill-Pilot-Token': server.token},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()

    def _post(self, server, path, body):
        request = urllib.request.Request(
            server.origin + path,
            data=json.dumps(body).encode('utf-8'),
            headers={'Origin': server.origin, 'X-Skill-Pilot-Token': server.token, 'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def _wait_ping(self, server):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with server.runtime_lock:
                if server.active_requests == 0:
                    break
            time.sleep(0.005)
        with server.activity_lock:
            thread = server.activity_thread
        if thread is not None:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def _configure(self, server, member, url=None):
        status, _ = self._post(server, '/api/team/settings', {'url': url or self.url, 'key': member['token']})
        self.assertEqual(status, 200)

    def test_zero_ping_when_unconnected_or_read_only(self):
        with patch.object(team, '_request', wraps=team._request) as network:
            self.assertEqual(self._get(self.dash_a, '/')[0], 200)
            self._wait_ping(self.dash_a)
            self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[0], 200)
            self._wait_ping(self.dash_a)
            self.assertEqual(team.report_activity(self.home), {'sent': False, 'reason': 'unconfigured'})
            network.assert_not_called()
        self.assertFalse((self.hub.store.data / 'activity.json').exists())

        self._configure(self.dash_a, self.first)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), \
                patch.object(team, '_request', wraps=team._request) as network:
            self.assertEqual(self._get(self.dash_a, '/')[0], 200)
            self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[1], {'queued': False})
            self.assertEqual(self._get(self.dash_a, '/api/team/catalog')[0], 200)
            self._wait_ping(self.dash_a)
            self.assertEqual(team.report_activity(self.home), {'sent': False, 'reason': 'read_only'})
            self.assertFalse(any(call.args[1] == '/api/activity' for call in network.call_args_list))
        self.assertFalse((self.hub.store.data / 'activity.json').exists())

    def test_throttled_ping_multi_open_midnight_rollover_and_empty_privacy_payload(self):
        self._configure(self.dash_a, self.first)
        self._configure(self.dash_b, self.second)
        captured_payloads = []
        original_request = team._request

        def spy_request(config, path, method='GET', data=None):
            if path == '/api/activity':
                captured_payloads.append((method, data))
            return original_request(config, path, method, data)

        with patch.object(team, '_request', side_effect=spy_request):
            # Repeated opens & team visits within one session throttle to a single ping.
            with patch.object(team_hub, 'now', return_value='2026-11-25T23:59:00+00:00'):
                self.assertEqual(self._get(self.dash_a, '/')[0], 200)
                self._wait_ping(self.dash_a)
                self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[0], 200)
                self._wait_ping(self.dash_a)
                self.assertEqual(self._get(self.dash_a, '/')[0], 200)
                self._wait_ping(self.dash_a)
                self.assertEqual(len(captured_payloads), 1)
                self.assertEqual(captured_payloads[0], ('POST', {}))

            # Crossing UTC midnight (23:59 -> 00:01) does NOT throttle the next day's first open.
            with patch.object(team_hub, 'now', return_value='2026-11-26T00:01:00+00:00'):
                self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[0], 200)
                self._wait_ping(self.dash_a)
                self.assertEqual(len(captured_payloads), 2)

                # After throttle window expires on the same UTC day, visiting team page records another open.
                self.dash_a.activity_last_ping -= (self.dash_a.activity_throttle_seconds + 1)
                self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[0], 200)
                self._wait_ping(self.dash_a)
                self.assertEqual(len(captured_payloads), 3)

                # A fresh dashboard session for member A records a 3rd open today; member B visits team page.
                fresh_a = self._start(Dashboard({'home': str(self.home)}))
                self.assertEqual(self._get(fresh_a, '/')[0], 200)
                self._wait_ping(fresh_a)
                self.assertEqual(self._post(self.dash_b, '/api/team/activity', {})[0], 200)
                self._wait_ping(self.dash_b)

                report = self.hub.store.activity_summary()
        by_id = {m['id']: m for m in report['members']}
        self.assertEqual(by_id[self.first['id']]['opens_today'], 3)
        self.assertEqual(by_id[self.first['id']]['daily'], {'2026-11-25': 1, '2026-11-26': 3})
        self.assertEqual(by_id[self.second['id']]['opens_today'], 1)
        raw_activity = (self.hub.store.data / 'activity.json').read_text(encoding='utf-8')
        self.assertNotIn('private-local-skill', raw_activity)
        self.assertNotIn(str(self.home), raw_activity)
        self.assertNotIn(self.first['token'], raw_activity)
        self.assertEqual(diagnostics.recent(self.home), [])

    def test_non_blocking_first_screen_and_offline_recovery_without_throttle_penalty(self):
        self._configure(self.dash_a, self.first)
        started = threading.Event()
        release = threading.Event()
        real_settings = team.settings

        def slow_settings(home):
            started.set()
            release.wait(timeout=3)
            return real_settings(home)

        # Even if reading team.json blocks, GET / returns HTML immediately without waiting on disk I/O.
        with patch.object(team, 'settings', side_effect=slow_settings):
            t0 = time.monotonic()
            self.assertEqual(self._get(self.dash_a, '/')[0], 200)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5)
            self.assertTrue(started.wait(timeout=2))
            release.set()
            self._wait_ping(self.dash_a)

        # Offline hub failure must not consume the 5-minute throttle window when hub comes back online.
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            offline_url = f'http://127.0.0.1:{unused.getsockname()[1]}'
        (self.home / '.skill-pilot/team.json').write_text(
            json.dumps({'url': offline_url, 'key': self.first['token']}), encoding='utf-8')
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._get(self.dash_a, '/')[0], 200)
            self._wait_ping(self.dash_a)
            self.assertEqual(team.report_activity(self.home), {'sent': False, 'reason': 'offline'})
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(diagnostics.recent(self.home), [])

        # Transient failure on the same URL+token followed immediately by hub recovery succeeds right away.
        fresh_b = self._start(Dashboard({'home': str(self.other)}))
        self._configure(fresh_b, self.second)
        with patch.object(team, '_request', side_effect=OSError('temporary network drop')):
            self.assertEqual(self._get(fresh_b, '/')[0], 200)
            self._wait_ping(fresh_b)
        self.assertIsNone(fresh_b.activity_last_ping)
        self.assertEqual(self._post(fresh_b, '/api/team/activity', {})[0], 200)
        self._wait_ping(fresh_b)
        second_row = next(m for m in self.hub.store.activity_summary()['members'] if m['id'] == self.second['id'])
        self.assertEqual(second_row['opens_today'], 1)

    def test_old_hub_404_cooldown_recovery_and_old_client_compatible(self):
        hits = {'activity': 0, 'me': 0, 'catalog': 0}
        upgraded = {'enabled': False}
        member_payload = {'id': self.first['id'], 'name': self.first['name']}

        class LegacyHubHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == '/api/me':
                    hits['me'] += 1
                    self._send(200, {'service': 'skill-pilot-team', 'api': 2, 'member': member_payload})
                elif self.path == '/api/catalog':
                    hits['catalog'] += 1
                    self._send(200, {'cards': []})
                else:
                    self._send(404, {'error': '没有这个分享站地址，请核对后重试。'})

            def do_POST(self):
                if self.path == '/api/activity':
                    hits['activity'] += 1
                    if upgraded['enabled']:
                        self._send(200, {'recorded': True, 'date': '2026-11-26', 'opens_today': 1,
                                         'last_active_at': '2026-11-26T09:00:00+00:00'})
                        return
                self._send(404, {'error': '没有这个分享站地址，请核对后重试。'})

        legacy = self._start(ThreadingHTTPServer(('127.0.0.1', 0), LegacyHubHandler))
        legacy_url = f'http://127.0.0.1:{legacy.server_port}'
        self._configure(self.dash_a, self.first, url=legacy_url)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._get(self.dash_a, '/')[0], 200)
            self._wait_ping(self.dash_a)
            # Within unsupported cooldown, repeated visits do not spam the old hub.
            self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[0], 200)
            self._wait_ping(self.dash_a)
            self.assertEqual(self._get(self.dash_a, '/api/team/catalog')[0], 200)
            self._wait_ping(self.dash_a)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(hits['activity'], 1)
        self.assertEqual(diagnostics.recent(self.home), [])

        # Once the bounded cooldown expires after an in-place hub upgrade, reporting recovers automatically.
        upgraded['enabled'] = True
        self.dash_a.activity_unsupported_until = time.monotonic() - 1
        self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[0], 200)
        self._wait_ping(self.dash_a)
        self.assertEqual(hits['activity'], 2)
        self.assertEqual(self.dash_a.activity_unsupported_connection, '')
        self.assertIsNone(self.dash_a.activity_unsupported_until)

        # Switching from an old 404 hub to a new hub clears cached unsupported state and pings immediately.
        self._configure(self.dash_a, self.first, url=self.url)
        self.assertEqual(self._post(self.dash_a, '/api/team/activity', {})[0], 200)
        self._wait_ping(self.dash_a)
        summary = self.hub.store.activity_summary()
        first_row = next(m for m in summary['members'] if m['id'] == self.first['id'])
        self.assertEqual(first_row['opens_today'], 1)

        # Unauthorized or cross-origin POST /api/team/activity is rejected with 403.
        for bad_headers in (
            {'Origin': self.dash_a.origin, 'X-Skill-Pilot-Token': 'wrong', 'Content-Type': 'application/json'},
            {'Origin': 'https://example.invalid', 'X-Skill-Pilot-Token': self.dash_a.token, 'Content-Type': 'application/json'},
        ):
            req = urllib.request.Request(self.dash_a.origin + '/api/team/activity', data=b'{}', headers=bad_headers)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()

        # Old dashboard (never calling /api/activity) works normally against the new hub.
        me = team._request({'url': self.url, 'key': self.second['token']}, '/api/me')
        self.assertEqual(me['api'], 2)
        cat = team._request({'url': self.url, 'key': self.second['token']}, '/api/catalog')
        self.assertEqual(cat['cards'], [])
        second_row = next(m for m in self.hub.store.activity_summary()['members'] if m['id'] == self.second['id'])
        self.assertEqual(second_row['opens_today'], 0)
