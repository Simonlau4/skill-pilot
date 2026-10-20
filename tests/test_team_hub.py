import copy
import hashlib
import http.client
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from skill_pilot import cli, sharing, team_hub
from tests import set_up_home


def config(name='example'):
    return {'format': sharing.FORMAT, 'version': 1, 'created_at': '2026-10-19T00:00:00+00:00',
            'agents': [{'id': 'acme', 'name': '示例 agent'}],
            'skills': [{'name': name, 'assigned_agents': ['acme'], 'requires': [],
                        'source': {'kind': 'github', 'repository': 'acme/skills', 'path': 'skills/' + name},
                        'local_modified': False}]}


class TeamHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot hub tests ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, self.temp.name)
        self.data = self.home / 'team data'
        self.store = team_hub.HubStore(self.data)
        self.first = self.store.add_member('Acme 示例成员一')
        self.second = self.store.add_member('Acme 示例成员二')
        self.server = team_hub.HubServer(self.data, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())

    def request(self, path='/api/me', method='GET', data=None, token='first', headers=None):
        if token == 'first':
            token = self.first['token']
        supplied = {'Authorization': 'Bearer ' + token} if token is not None else {}
        supplied.update(headers or {})
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            self.assertEqual(response.headers.get_content_type(), 'application/json')
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return response.status, json.loads(response.read()) if method != 'HEAD' else None
        finally:
            connection.close()

    def put(self, data=None, token='first'):
        return self.request('/api/shares/me', 'PUT', config() if data is None else data, token)

    def test_members_use_only_digests_and_list_never_returns_tokens(self):
        self.assertNotEqual(self.first['token'], self.second['token'])
        self.assertNotEqual(self.first['id'], self.second['id'])
        saved = (self.data / 'members.json').read_text()
        members = json.loads(saved)['members']
        self.assertEqual(members[0]['token_sha256'], hashlib.sha256(self.first['token'].encode()).hexdigest())
        self.assertNotIn(self.first['token'], saved)
        self.assertNotIn('token', json.dumps(self.store.members()))
        self.assertEqual(len(self.store.members()), 2)
        self.assertEqual(stat.S_IMODE((self.data / 'members.json').stat().st_mode), 0o600)
        self.store.add_member('Acme 示例成员一')
        self.assertEqual(len(self.store.members()), 3)
        for name in ('', '   ', '字' * 41, 'Acme\nexample', 'Acme\x00example', 'Acme\x7fexample', None):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '不超过 40 个字'):
                self.store.add_member(name)
        with self.assertRaises(ValueError):
            self.store.remove_member('../members')
        self.assertEqual(len(self.store.members()), 3)

    def test_open_join_returns_a_usable_token_but_stores_only_its_digest(self):
        self.server.open = True
        status, joined = self.request('/api/join', 'POST', {'name': '  示例成员  '}, token=None)
        self.assertEqual(status, 200)
        self.assertEqual(set(joined), {'service', 'api', 'member', 'token'})
        self.assertEqual(joined['service'], 'skill-pilot-team')
        self.assertEqual(joined['api'], 1)
        self.assertEqual(joined['member']['name'], '示例成员')
        self.assertEqual(set(joined['member']), {'id', 'name'})
        token = joined['token']
        self.assertRegex(token, r'^[A-Za-z0-9_-]+$')
        self.assertEqual(self.request(token=token)[1]['member'], joined['member'])
        self.assertEqual(self.put(token=token)[0], 200)
        self.assertEqual(self.request('/api/shares/' + joined['member']['id'])[1]['config'], config())
        self.assertEqual(self.request('/api/shares/me', 'DELETE', token=token), (200, {}))
        self.assertEqual(self.request('/api/shares/' + joined['member']['id'])[0], 404)
        saved = (self.data / 'members.json').read_text()
        self.assertNotIn(token, saved)
        self.assertEqual(json.loads(saved)['members'][-1]['token_sha256'], hashlib.sha256(token.encode()).hexdigest())
        duplicate = self.request('/api/join', 'POST', {'name': '示例成员'}, token=None)[1]
        self.assertNotEqual(duplicate['member']['id'], joined['member']['id'])
        self.assertEqual(len(self.store.members()), 4)

    def test_closed_join_does_not_read_or_write_member_data(self):
        path = self.data / 'members.json'
        path.unlink()
        before = {entry.name: entry.read_bytes() for entry in self.data.iterdir()}
        with patch.object(self.server.store, 'locked', side_effect=AssertionError('must not access members')):
            self.assertEqual(self.request('/api/join', 'POST', {'name': '示例成员'}, token=None),
                             (403, {'error': '这个分享站需要管理员发的口令。'}))
            self.assertEqual(self.raw_upload([], path='/api/join', method='POST', token=None)[0], 403)
        self.assertEqual({entry.name: entry.read_bytes() for entry in self.data.iterdir()}, before)
        self.assertFalse(path.exists())

    def test_join_validates_name_and_object_before_creating_members(self):
        self.server.open = True
        path = self.data / 'members.json'
        before = path.read_bytes()
        invalid = [{}, [], '示例成员', 1, *({'name': name} for name in
                   ('', '   ', '字' * 41, '示例\n成员', '示例\x00成员', '示例\x7f成员', None, [], 1))]
        for data in invalid:
            with self.subTest(data=data):
                self.assertEqual(self.request('/api/join', 'POST', data, token=None)[0], 400)
                self.assertEqual(path.read_bytes(), before)
        joined = self.request('/api/join', 'POST', {'name': '  ' + '字' * 40 + '  '}, token=None)
        self.assertEqual(joined[0], 200)
        self.assertEqual(joined[1]['member']['name'], '字' * 40)

    def test_join_rejects_invalid_lengths_encoding_and_json(self):
        self.server.open = True
        before = (self.data / 'members.json').read_bytes()
        for headers, expected in [([], 411), ([('Content-Length', '4097')], 413),
                                  ([('Content-Length', '-1')], 400), ([('Content-Length', 'wrong')], 400),
                                  ([('Content-Length', '2'), ('Content-Length', '2')], 400),
                                  ([('Transfer-Encoding', 'chunked')], 400),
                                  ([('Content-Length', '100')], 400)]:
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_upload(headers, path='/api/join', method='POST', token=None)[0], expected)
        for body in (b'{broken', b'\xff', b'null', b'{"name": NaN}', b'[' * 1100):
            with self.subTest(body=body[:30]):
                self.assertEqual(self.raw_upload([('Content-Length', str(len(body)))], body,
                                 '/api/join', 'POST', None)[0], 400)
        self.assertEqual((self.data / 'members.json').read_bytes(), before)
        body = json.dumps({'name': '示例成员'}, ensure_ascii=False).encode()
        self.assertEqual(self.raw_upload([('Content-Length', '4096')], body + b' ' * (4096 - len(body)),
                         '/api/join', 'POST', None)[0], 200)

    def test_closing_open_join_after_restart_keeps_member_and_share(self):
        self.close()
        self.server = team_hub.HubServer(self.data, '127.0.0.1', 0, open=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        joined = self.request('/api/join', 'POST', {'name': '示例成员'}, token=None)[1]
        self.assertEqual(self.put(token=joined['token'])[0], 200)
        self.close()
        self.server = team_hub.HubServer(self.data, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.assertEqual(self.request(token=joined['token'])[1]['member'], joined['member'])
        self.assertEqual(self.request('/api/shares/' + joined['member']['id'], token=joined['token'])[1]['config'], config())
        self.assertEqual(self.request('/api/join', 'POST', {'name': '新示例成员'}, token=None)[0], 403)
        self.store.remove_member(joined['member']['id'])
        self.assertEqual(self.request(token=joined['token'])[0], 401)

    def test_every_path_and_method_requires_a_valid_token(self):
        self.server.open = True
        for path, method in [('/api/me', 'GET'), ('/api/shares', 'GET'),
                             ('/api/shares/' + self.first['id'], 'GET'),
                             ('/api/shares/me', 'PUT'), ('/api/shares/me', 'DELETE'),
                             ('/members.json', 'GET'), ('/api/me', 'BREW'),
                             ('/api/join', 'GET'), ('/api/join', 'PUT'), ('/api/join', 'BREW'),
                             ('/api/join?name=example', 'POST'), ('/api/join/', 'POST'), ('/api/Join', 'POST')]:
            for token in (None, '', 'wrong'):
                with self.subTest(path=path, method=method, token=token):
                    status, body = self.request(path, method, token=token)
                    self.assertEqual(status, 401)
                    self.assertEqual(set(body), {'error'})
        self.assertEqual(self.request(headers={'Authorization': 'Basic wrong'})[0], 401)

    def test_join_auth_exemption_requires_the_exact_request_target(self):
        self.server.open = True
        for path in ('/api/join?', '//api/join', '///api/join'):
            with self.subTest(path=path):
                self.assertEqual(self.raw_upload([], path=path, method='POST', token=None)[0], 401)

    def test_identity_empty_list_and_missing_share(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body, {'service': 'skill-pilot-team', 'api': 1,
                               'member': {'id': self.first['id'], 'name': self.first['name']}})
        self.assertEqual(self.request('/api/shares'), (200, {'shares': []}))
        self.assertEqual(self.request('/api/shares/' + self.first['id'])[0], 404)

    def test_upload_list_read_and_latest_first(self):
        status, receipt = self.put()
        self.assertEqual(status, 200)
        self.assertEqual(set(receipt), {'updated_at', 'skill_count'})
        self.assertEqual(receipt['skill_count'], 1)
        self.assertEqual(self.put(config('second'), self.second['token'])[0], 200)
        status, body = self.request('/api/shares')
        self.assertEqual(status, 200)
        self.assertEqual([row['member']['id'] for row in body['shares']], [self.second['id'], self.first['id']])
        self.assertEqual(body['shares'][1]['agents'], config()['agents'])
        self.assertEqual(body['shares'][1]['skill_count'], 1)
        self.assertNotIn('config', body['shares'][0])
        status, shared = self.request('/api/shares/' + self.first['id'], token=self.second['token'])
        self.assertEqual(status, 200)
        self.assertEqual(shared['config'], config())
        self.assertEqual(shared['updated_at'], receipt['updated_at'])
        self.assertEqual(shared['member']['id'], self.first['id'])

    def test_whitelist_discards_extra_fields_at_every_level(self):
        data = config()
        data.update(instructions=['PRIVATE_INSTRUCTIONS'], settings={'key': 'PRIVATE_KEY'}, tags=['PRIVATE_TAG'])
        data['agents'][0]['folder'] = 'PRIVATE_FOLDER'
        data['skills'][0].update(title='示例技能', summary='示例用途', group='示例分组', description='示例说明',
                                 instructions='PRIVATE_SKILL_BODY', tags=['PRIVATE_TAG'])
        data['skills'][0]['source']['credentials'] = 'PRIVATE_CREDENTIALS'
        manual = copy.deepcopy(data['skills'][0])
        manual.update(name='manual', source={'kind': 'manual', 'reason': '另行提供',
                                           'url': 'https://example.invalid/skills', 'secret': 'PRIVATE_SOURCE'})
        data['skills'].append(manual)
        with patch.object(sharing, 'validate', wraps=sharing.validate) as validate:
            self.assertEqual(self.put(data)[0], 200)
            validate.assert_called_once()
        saved = self.request('/api/shares/' + self.first['id'])[1]['config']
        self.assertEqual(set(saved), {'format', 'version', 'created_at', 'agents', 'skills'})
        self.assertEqual(set(saved['agents'][0]), {'id', 'name'})
        self.assertEqual(set(saved['skills'][0]), set(team_hub.SKILL_FIELDS) | {'source'})
        for field in ('title', 'summary', 'group', 'description'):
            self.assertEqual(saved['skills'][0][field], data['skills'][0][field])
        self.assertEqual(saved['skills'][1]['source'], {'kind': 'manual', 'reason': '另行提供',
                                                      'url': 'https://example.invalid/skills'})
        self.assertNotIn('PRIVATE_', (self.data / 'shares' / (self.first['id'] + '.json')).read_text())

    def test_optional_display_fields_are_not_added_when_absent(self):
        self.assertEqual(self.put()[0], 200)
        row = self.request('/api/shares/' + self.first['id'])[1]['config']['skills'][0]
        for field in ('title', 'summary', 'group', 'description'):
            self.assertNotIn(field, row)

    def test_invalid_configs_leave_previous_share_unchanged(self):
        self.put()
        path = self.data / 'shares' / (self.first['id'] + '.json')
        before = path.read_bytes()
        invalid = [[], {}, {'format': sharing.FORMAT, 'version': True, 'skills': []}]
        for change in (lambda value: value['skills'][0].update(name='../invalid'),
                       lambda value: value['skills'][0].update(requires=['missing']),
                       lambda value: value['skills'][0]['source'].update(path='../outside'),
                       lambda value: value['skills'][0].update(local_modified='false'),
                       lambda value: value['skills'].append(copy.deepcopy(value['skills'][0])),
                       lambda value: value.update(agents='not-a-list'),
                       lambda value: value.update(agents=[{'id': 'acme'}])):
            value = config()
            change(value)
            invalid.append(value)
        for data in invalid:
            with self.subTest(data=data):
                status, error = self.put(data)
                self.assertEqual(status, 400)
                self.assertEqual(set(error), {'error'})
                self.assertEqual(path.read_bytes(), before)

    def test_invalid_json_utf8_and_non_finite_numbers_are_rejected(self):
        for raw in (b'{broken', b'\xff', b'{"skills": NaN}', b'[' * 1100):
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
            try:
                connection.request('PUT', '/api/shares/me', raw,
                                   {'Authorization': 'Bearer ' + self.first['token']})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertEqual(set(json.loads(response.read())), {'error'})
            finally:
                connection.close()
        self.assertEqual(self.request('/api/shares')[1], {'shares': []})

    def test_body_limit_is_in_bytes_and_rejects_before_reading(self):
        data = json.dumps(config(), ensure_ascii=False).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request('PUT', '/api/shares/me', data + b' ' * (team_hub.MAX_BODY - len(data)),
                               {'Authorization': 'Bearer ' + self.first['token']})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            connection.close()
        # Send only headers: a server that tries to read the oversized body would time out.
        self.assertEqual(self.raw_upload([('Content-Length', str(team_hub.MAX_BODY + 1))])[0], 413)
        self.assertEqual(self.request('/api/shares')[1]['shares'][0]['skill_count'], 1)

    def raw_upload(self, headers, body=b'', path='/api/shares/me', method='PUT', token='first'):
        if token == 'first':
            token = self.first['token']
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            rows = [('Host', 'localhost'), *([('Authorization', 'Bearer ' + token)] if token is not None else []), *headers]
            head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{key}: {value}\r\n' for key, value in rows)
            connection.sendall(head.encode() + b'\r\n' + body)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def test_missing_invalid_duplicate_and_chunked_lengths(self):
        for headers, status in [([], 411), ([('Content-Length', '-1')], 400),
                                ([('Content-Length', 'wrong')], 400),
                                ([('Content-Length', '2'), ('Content-Length', '2')], 400),
                                ([('Transfer-Encoding', 'chunked')], 400),
                                ([('Content-Length', '100')], 400)]:
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_upload(headers)[0], status)

    def test_overwrite_and_withdraw_are_scoped_to_current_member(self):
        self.put()
        self.put(config('second'), self.second['token'])
        self.put(config('replacement'))
        self.assertEqual(self.request('/api/shares/' + self.first['id'])[1]['config'], config('replacement'))
        self.assertEqual(len(list((self.data / 'shares').glob('*.json'))), 2)
        self.assertEqual(self.request('/api/shares/' + self.second['id'], 'DELETE')[0], 405)
        self.assertEqual(self.request('/api/shares/me', 'DELETE'), (200, {}))
        self.assertEqual(self.request('/api/shares/me', 'DELETE'), (200, {}))
        self.assertEqual(self.request('/api/shares/' + self.first['id'])[0], 404)
        self.assertEqual(self.request('/api/shares')[1]['shares'][0]['member']['id'], self.second['id'])

    def test_removing_member_deletes_share_and_revokes_token(self):
        self.put()
        self.put(config('second'), self.second['token'])
        self.store.remove_member(self.first['id'])
        self.assertFalse((self.data / 'shares' / (self.first['id'] + '.json')).exists())
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.put()[0], 401)
        self.assertEqual(self.request('/api/shares', token=self.second['token'])[1]['shares'][0]['skill_count'], 1)
        self.assertEqual(self.request('/api/shares/' + self.first['id'], token=self.second['token'])[0], 404)

    def test_removing_member_during_upload_cannot_recreate_share(self):
        authenticated = threading.Event()
        original = self.server.store._authenticate
        def authenticate(header):
            member = original(header)
            authenticated.set()
            return member
        raw = json.dumps(config()).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            with patch.object(self.server.store, '_authenticate', side_effect=authenticate):
                connection.putrequest('PUT', '/api/shares/me')
                connection.putheader('Authorization', 'Bearer ' + self.first['token'])
                connection.putheader('Content-Length', str(len(raw)))
                connection.endheaders()
                self.assertTrue(authenticated.wait(3))
                self.store.remove_member(self.first['id'])
                connection.send(raw)
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
        finally:
            connection.close()
        self.assertFalse((self.data / 'shares' / (self.first['id'] + '.json')).exists())

    def test_simultaneous_uploads_save_one_complete_config(self):
        barrier = threading.Barrier(2)
        values = [config('first-upload'), config('second-upload')]
        def upload(data):
            barrier.wait(timeout=3)
            return self.put(data)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(upload, values))
        self.assertEqual([status for status, _ in results], [200, 200])
        saved = self.request('/api/shares/' + self.first['id'])[1]
        self.assertIn(saved['config'], values)
        self.assertIn(saved['updated_at'], [result['updated_at'] for _, result in results])
        self.assertEqual(len(list((self.data / 'shares').iterdir())), 1)

    def test_only_documented_routes_and_methods_exist(self):
        for path in ('/', '/members.json', '/api/me?token=wrong', '/api/shares/../members.json',
                     '/api/shares/' + self.first['id'] + '/', '/api/join?name=example', '/api/join/'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/api/join', 'POST', {'name': '示例成员'})[0], 403)
        for method in ('GET', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'BREW'):
            self.assertEqual(self.request('/api/join', method)[0], 405)
        self.assertEqual(self.request('/api/shares/me')[0], 405)
        for method in ('POST', 'PATCH', 'OPTIONS', 'BREW'):
            self.assertEqual(self.request('/api/me', method)[0], 405)
        self.assertEqual(self.request('/api/me', 'HEAD')[0], 405)

    def test_http_logs_never_contain_tokens_paths_or_lists(self):
        self.server.open = True
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.assertEqual(self.request('/api/join', 'POST', {'name': '日志里不能出现的示例成员'}, token=None)[0], 200)
            self.put(config('private-list-name'))
            self.request('/api/me?token=' + self.first['token'])
            self.request(token='wrong')
        self.assertEqual(log.getvalue(), '')

    def test_failed_atomic_write_preserves_existing_share_and_cleans_temporary_file(self):
        self.put()
        path = self.data / 'shares' / (self.first['id'] + '.json')
        before = path.read_bytes()
        with patch.object(team_hub.os, 'replace', side_effect=OSError('test write failure')):
            self.assertEqual(self.put(config('replacement'))[0], 500)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_new_store_and_server_keep_existing_member_and_share(self):
        self.put()
        other = team_hub.HubStore(self.data)
        self.assertEqual(other.members(), self.store.members())
        with team_hub.HubServer(self.data, '127.0.0.1', 0) as restarted:
            with restarted.store.locked():
                member = restarted.store._authenticate('Bearer ' + self.first['token'])
                self.assertEqual(restarted.store._get_share(member['id'])['config'], config())


class TeamHubCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot hub cli ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, self.temp.name)
        self.data = self.home / 'team data'

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'skill_pilot', 'hub', *args],
                              cwd=Path(__file__).resolve().parents[1],
                              env={**os.environ, 'SKILL_PILOT_HOME': str(self.home / 'ignored home'),
                                   'PYTHONDONTWRITEBYTECODE': '1'},
                              capture_output=True, text=True, timeout=10)

    def test_all_subcommands_require_explicit_data_folder(self):
        for args in [('serve',), ('add-member', 'Acme'), ('members',), ('remove-member', 'missing')]:
            with self.subTest(args=args):
                result = self.command(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--data', result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_serve_open_flag_and_startup_access_message(self):
        for flags, opened, message in (([], False, '只有管理员发了口令的成员能用'),
                                       (['--open'], True, '开放加入，知道地址的同事填名字就能加入')):
            argv = ['skill-pilot', 'hub', 'serve', '--data', str(self.data), *flags]
            with self.subTest(open=opened), patch.object(sys, 'argv', argv), \
                    patch.object(team_hub, 'HubServer') as server_class, redirect_stdout(io.StringIO()) as output:
                server = server_class.return_value.__enter__.return_value
                server.server_port = 8765
                server.serve_forever.side_effect = KeyboardInterrupt
                cli.main()
                server_class.assert_called_once_with(str(self.data), '0.0.0.0', 8765, open=opened)
                self.assertIn(message, output.getvalue())
        self.assertIn('开放加入：知道地址的同事填名字就能加入，不用口令', self.command('serve', '--help').stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_add_list_remove_and_errors(self):
        added = self.command('add-member', 'Acme 示例成员', '--data', str(self.data))
        self.assertEqual(added.returncode, 0, added.stderr)
        member = team_hub.HubStore(self.data).members()[0]
        token = added.stdout.split('口令（只显示这一次）：')[1].splitlines()[0]
        listed = self.command('members', '--data', str(self.data))
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(member['id'], listed.stdout)
        self.assertIn(member['name'], listed.stdout)
        self.assertNotIn(token, listed.stdout)
        self.assertNotIn('token_sha256', listed.stdout)
        removed = self.command('remove-member', member['id'], '--data', str(self.data))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertIn('还没有成员', self.command('members', '--data', str(self.data)).stdout)
        self.assertEqual(self.command('remove-member', member['id'], '--data', str(self.data)).returncode, 1)
        self.assertEqual(self.command('add-member', '  ', '--data', str(self.data)).returncode, 1)
        self.assertEqual(self.command('serve', '--data', str(self.data), '--port', '65536').returncode, 1)
        self.assertFalse((self.home / 'ignored home').exists())

    def test_hub_never_initializes_local_home_or_tool_discovery(self):
        argv = ['skill-pilot', 'hub', 'members', '--data', str(self.data)]
        with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()), \
                patch.object(cli, 'target_home', side_effect=AssertionError('must not resolve local home')), \
                patch.object(cli, 'tool_environment', side_effect=AssertionError('must not discover local tools')):
            cli.main()
        self.assertEqual(set(self.home.iterdir()), {self.data})

    def test_concurrent_cli_member_additions_do_not_lose_members(self):
        def add(index):
            return self.command('add-member', f'Acme 示例成员 {index}', '--data', str(self.data))
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(add, range(4)))
        self.assertEqual([result.returncode for result in results], [0] * 4)
        members = team_hub.HubStore(self.data).members()
        self.assertEqual(len(members), 4)
        self.assertEqual(len({member['id'] for member in members}), 4)
