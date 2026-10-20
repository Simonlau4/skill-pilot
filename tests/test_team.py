"""Team exchanges use two synthetic homes and loopback services only."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, sharing, store, team, team_hub
from skill_pilot.sources import github
from skill_pilot.web.server import Dashboard
from tests import set_up_home


class TeamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot team ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'Example A')
        self.other = self.root / 'Example B'
        self.repository = self.root / 'Acme skills'
        for name in ('acme-main', 'acme-base', 'acme-other', 'acme-extra', 'acme-local'):
            self.skill(self.repository / 'skills' / name, name)
        github.git(self.repository, 'init', '-q')
        github.git(self.repository, 'add', '.')
        github.git(self.repository, '-c', 'user.name=Example', '-c', 'user.email=example@local.invalid',
                   '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        self.commit = github.git(self.repository, 'rev-parse', 'HEAD')
        self.make_home(self.home, 'codex', ('acme-main', 'acme-base', 'acme-other', 'acme-extra', 'acme-local'))
        self.make_home(self.other, 'claude', ('acme-other',))
        self.registry(self.home, lambda skills: skills['acme-main'].update(requires=['acme-base']))
        self.registry(self.home, lambda skills: skills['acme-local'].update(source={'source': '本机自制'}))
        self.hub = self.start(team_hub.HubServer(self.root / 'Share data', '127.0.0.1', 0))
        self.url = 'http://127.0.0.1:' + str(self.hub.server_port)
        self.first = self.hub.store.add_member('示例成员 A')
        self.second = self.hub.store.add_member('示例成员 B')
        self.a = self.start(Dashboard({'home': str(self.home)}))
        self.b = self.start(Dashboard({'home': str(self.other)}))

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return server

    def skill(self, path, name):
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Acme 示例说明\n---\n示例正文\n')

    def make_home(self, home, agent, names):
        for name in names:
            shutil.copytree(self.repository / 'skills' / name, home / ('.' + agent) / 'skills' / name)
        plan = store.preview(home, [agent])
        store.apply(plan['plan_id'], home, [agent])
        def sources(skills):
            for name, row in skills.items():
                row['source'] = {'source': 'https://github.com/acme/skills', 'source_path': 'skills/' + name,
                                 'commit': self.commit}
        self.registry(home, sources)

    def registry(self, home, change):
        path = home / '.skill-pilot/registry.json'
        data = json.loads(path.read_text())
        change(data['skills'])
        path.write_text(json.dumps(data))

    def request(self, server, path, body=None, headers=None):
        defaults = {'Origin': server.origin, 'X-Skill-Pilot-Token': server.token, 'Content-Type': 'application/json'}
        request = urllib.request.Request(server.origin + path,
            data=json.dumps(body).encode() if body is not None else None, headers=defaults | (headers or {}))
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def ok(self, server, path, body=None):
        status, result = self.request(server, path, body)
        self.assertEqual(status, 200, result)
        for member in (self.first, self.second):
            self.assertNotIn(member['token'], json.dumps(result))
        return result

    def configure(self):
        for server, member in ((self.a, self.first), (self.b, self.second)):
            self.ok(server, '/api/team/settings', {'url': self.url, 'key': member['token']})

    def publish(self):
        preview = self.ok(self.a, '/api/team/upload/preview', {'exclude': ['acme-extra', 'acme-base']})
        self.ok(self.a, '/api/team/upload/apply', {'review': preview['review']})
        return preview

    def library(self, home):
        paths = ('.codex/skills', '.claude/skills', '.skill-pilot/registry.json', '.skill-pilot/bases')
        return {path: store.physical_state(home / path) for path in paths} | {
            'store': operations.content_digest(home / '.skill-pilot/store')}

    def test_two_users_upload_browse_import_saved_subset_and_undo_then_withdraw(self):
        self.configure()
        preview = self.publish()
        self.assertEqual(preview['retained'][0]['name'], 'acme-base')
        self.assertEqual(preview['retained'][0]['required_by'], ['acme-main'])
        self.assertNotIn('acme-extra', [row['name'] for row in preview['config']['skills']])
        status = self.ok(self.a, '/api/team')
        self.assertEqual(status['member']['id'], self.first['id'])
        self.assertEqual(status['share']['skill_count'], 4)
        listing = self.ok(self.b, '/api/team/shares')['shares']
        self.assertEqual([row['member']['id'] for row in listing], [self.first['id']])
        detail = self.ok(self.b, '/api/team/shares/' + self.first['id'])
        states = {row['name']: row['local']['status'] for row in detail['config']['skills']}
        self.assertEqual(states, {'acme-base': 'installable', 'acme-main': 'installable',
                                  'acme-other': 'installed', 'acme-local': 'needs_files'})
        # A's codex is absent on B, but availability above does not depend on it.
        unmapped = self.ok(self.b, '/api/team/import/preview', {'member': self.first['id'], 'skills': ['acme-main']})
        self.assertEqual(unmapped['unmapped_agents'], ['codex'])
        plan = self.ok(self.b, '/api/team/import/preview', {
            'member': self.first['id'], 'skills': ['acme-main', 'acme-other'], 'agents': ['claude']})
        self.assertEqual(plan['install'], ['acme-base', 'acme-main'])
        self.assertEqual(plan['reuse'], ['acme-other'])
        sharing.validate(plan['config'])
        subset = {row['name']: row for row in plan['config']['skills']}
        self.assertEqual(subset['acme-base']['assigned_agents'], [])
        self.assertEqual(subset['acme-main']['assigned_agents'], ['codex'])
        original = (self.other / '.skill-pilot/store/acme-other/SKILL.md').read_bytes()
        before = self.library(self.other)
        # Withdrawal and new remote data cannot change the reviewed installation.
        self.ok(self.a, '/api/team/withdraw', {})
        with patch.object(github, 'parse_url', return_value=(self.repository.as_uri(), 'acme/skills', self.commit)):
            result = self.ok(self.b, '/api/team/import/apply', {'review': plan['review'], 'revision': plan['revision']})
        self.assertTrue(result['complete'])
        self.assertEqual(operations.load(self.other)[1]['skills']['acme-base']['assigned_agents'], ['claude'])
        self.assertEqual((self.other / '.skill-pilot/store/acme-other/SKILL.md').read_bytes(), original)
        self.assertEqual(result['message'], '从示例成员 A的分享导入了技能：新装 2 个，沿用本机 1 个')
        history = self.ok(self.b, '/api/log')
        self.assertIn(result['message'], json.dumps(history, ensure_ascii=False))
        with tarfile.open(self.other / '.skill-pilot/backups' / result['backup_id'] / 'files.tar') as archive:
            self.assertNotIn('.skill-pilot/team.json', archive.getnames())
        self.ok(self.b, '/api/undo', {'id': result['id'], 'confirmed': True,
                                    'revision': operations.table(self.other)['revision']})
        self.assertEqual(self.library(self.other), before)
        self.assertEqual(self.ok(self.b, '/api/team/shares'), {'shares': []})

    def test_upload_applies_the_exact_preview_even_after_local_changes(self):
        self.configure()
        preview = self.ok(self.a, '/api/team/upload/preview', {})
        path = self.home / '.skill-pilot/store/acme-main/SKILL.md'
        path.write_text(path.read_text().replace('Acme 示例说明', '更改后的说明'))
        self.ok(self.a, '/api/team/upload/apply', {'review': preview['review'], 'exclude': ['acme-main']})
        self.assertEqual(team.share(self.other, self.first['id'])['config'], preview['config'])

    def test_settings_are_private_separate_and_tested_before_replacement(self):
        self.assertEqual(self.ok(self.a, '/api/team'), {'url': '', 'has_key': False, 'configured': False,
                                                     'member': None, 'share': None})
        self.configure()
        path = self.home / '.skill-pilot/team.json'
        original = path.read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.ok(self.a, '/api/settings/llm', {'provider': 'example', 'base_url': 'https://example.invalid', 'model': 'example'})
        self.assertEqual(path.read_bytes(), original)
        self.ok(self.a, '/api/team/settings', {'url': self.url})
        self.assertEqual(path.read_bytes(), original)
        before = store.physical_state(self.home)
        status, result = self.request(self.a, '/api/team/settings', {'url': self.url, 'key': 'wrong'})
        self.assertEqual(status, 409)
        self.assertIn('口令不对', result['error'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_open_station_connects_by_name_and_reuses_saved_identity(self):
        self.hub.open = True
        history = operations.history(self.home)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.ok(self.a, '/api/team/settings', {'url': self.url, 'name': '  示例新成员  '})
            self.assertEqual(result['member']['name'], '示例新成员')
            path = self.home / '.skill-pilot/team.json'
            original = path.read_bytes()
            saved = json.loads(original)
            self.assertEqual(set(saved), {'url', 'key'})
            self.assertRegex(saved['key'], r'^[A-Za-z0-9_-]+$')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(self.hub.store.members()), 3)
            for body in ({'url': self.url}, {'url': self.url, 'name': '其他示例名字', 'key': ''},
                         {'url': self.url, 'name': ['忽略已连接用户的名字']}):
                self.assertEqual(self.ok(self.a, '/api/team/settings', body)['member'], result['member'])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(len(self.hub.store.members()), 3)
            public = self.ok(self.a, '/api/team')
            self.assertEqual(set(public), {'url', 'has_key', 'configured', 'member', 'share'})
            self.assertEqual(public['member'], result['member'])
            self.assertNotIn(saved['key'], json.dumps([result, public]) + stderr.getvalue())
        self.assertEqual(operations.history(self.home), history)
        self.ok(self.b, '/api/team/settings', {'url': self.url, 'name': '另一示例成员', 'key': ''})
        self.assertEqual(len(self.hub.store.members()), 4)

    def test_damaged_settings_are_repaired_only_after_join_succeeds(self):
        path = self.home / '.skill-pilot/team.json'
        damaged = [b'{"url":', b'\xff', b'[]', json.dumps({'url': self.url}).encode(),
                   json.dumps({'url': self.url, 'key': ''}).encode()]
        for raw in damaged:
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                before = store.physical_state(self.home)
                self.hub.open = False
                status, result = self.request(self.a, '/api/team/settings', {
                    'url': self.url, 'name': '示例新成员'})
                self.assertEqual(status, 409)
                self.assertEqual(result['error'], '这个分享站需要管理员发的口令，请点「管理员给了口令？」填上。')
                self.assertEqual(store.physical_state(self.home), before)
                self.hub.open = True
                connected = self.ok(self.a, '/api/team/settings', {'url': self.url, 'name': '示例新成员'})
                self.assertEqual(connected['member']['name'], '示例新成员')
                saved = team.settings(self.home)
                self.assertEqual(saved['url'], self.url)
                self.assertRegex(saved['key'], r'^[A-Za-z0-9_-]+$')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.ok(self.a, '/api/team')['member'], connected['member'])
                self.assertNotIn(saved['key'], json.dumps(connected))

    def test_closed_station_requires_admin_key_and_leaves_settings_unchanged(self):
        before = [store.physical_state(home) for home in (self.home, self.hub.store.data)]
        status, result = self.request(self.a, '/api/team/settings', {'url': self.url, 'name': '示例新成员'})
        self.assertEqual(status, 409)
        self.assertEqual(result['error'], '这个分享站需要管理员发的口令，请点「管理员给了口令？」填上。')
        self.assertFalse((self.home / '.skill-pilot/team.json').exists())
        self.assertEqual([store.physical_state(home) for home in (self.home, self.hub.store.data)], before)
        connected = self.ok(self.a, '/api/team/settings', {'url': self.url, 'key': self.first['token'], 'name': []})
        self.assertEqual(connected['member']['id'], self.first['id'])

    def test_missing_or_invalid_join_name_never_contacts_station(self):
        before = store.physical_state(self.home)
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            url = 'http://127.0.0.1:' + str(unused.getsockname()[1])
            with patch.object(team, '_request') as network:
                for body in ({'url': url}, *({'url': url, 'name': name} for name in
                             ('', '   ', '字' * 41, '示例\n成员', '示例\x00成员', '示例\x7f成员', None, [], 1))):
                    with self.subTest(body=body):
                        status, result = self.request(self.a, '/api/team/settings', body)
                        self.assertEqual(status, 409)
                        name = body.get('name', '')
                        self.assertEqual(result['error'], '请填写你的名字，同事会看到它。' if
                                         isinstance(name, str) and not name.strip()
                                         else '请填写名字，不超过 40 个字，不能换行或含控制字符。')
                network.assert_not_called()
        self.assertEqual(store.physical_state(self.home), before)

    def test_removed_member_can_rejoin_by_name(self):
        self.hub.open = True
        connected = self.ok(self.a, '/api/team/settings', {'url': self.url, 'name': '示例新成员'})
        path = self.home / '.skill-pilot/team.json'
        old_key = json.loads(path.read_bytes())['key']
        original = path.read_bytes()
        self.hub.store.remove_member(connected['member']['id'])
        self.assertEqual(self.ok(self.a, '/api/team')['error'],
                         '分享站没认出你（口令不对、已失效或已被移出），请到设置里重新连接。')
        self.assertEqual(self.request(self.a, '/api/team/settings', {'url': self.url})[0], 409)
        self.assertEqual(path.read_bytes(), original)
        rejoined = self.ok(self.a, '/api/team/settings', {'url': self.url, 'name': '示例新成员'})
        self.assertNotEqual(rejoined['member']['id'], connected['member']['id'])
        self.assertNotEqual(json.loads(path.read_bytes())['key'], old_key)
        self.assertEqual(len(self.hub.store.members()), 3)

    def test_saved_key_is_tried_on_new_address_and_only_auth_rejection_allows_join(self):
        self.configure()
        self.hub.open = True
        other = self.start(team_hub.HubServer(self.hub.store.data, '127.0.0.1', 0, open=True))
        url = 'http://127.0.0.1:' + str(other.server_port)
        result = self.ok(self.a, '/api/team/settings', {'url': url, 'name': '不会另建成员'})
        self.assertEqual(result['member']['id'], self.first['id'])
        self.assertEqual(len(self.hub.store.members()), 2)
        before = store.physical_state(self.home)
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            closed_url = 'http://127.0.0.1:' + str(unused.getsockname()[1])
            with patch.object(team, '_request', wraps=team._request) as network:
                status, result = self.request(self.a, '/api/team/settings', {'url': closed_url, 'name': '示例成员'})
                self.assertEqual(status, 409)
                self.assertIn('连不上分享站', result['error'])
                self.assertEqual([call.args[1] for call in network.call_args_list], ['/api/me'])
        with patch.object(team, '_request', wraps=team._request) as network:
            self.assertEqual(self.request(self.a, '/api/team/settings', {
                'url': self.url, 'key': 'wrong', 'name': '示例成员'})[0], 409)
            self.assertEqual([call.args[1] for call in network.call_args_list], ['/api/me'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_invalid_join_responses_never_save_credentials_or_send_authorization(self):
        self.configure()
        before = store.physical_state(self.home)
        joined = {'service': 'skill-pilot-team', 'api': 1,
                  'member': {'id': 'a' * 32, 'name': '示例成员'}, 'token': 'example-joined-token'}
        invalid = [dict(joined, service='other'), dict(joined, api=2), dict(joined, api=True),
                   dict(joined, member=None), dict(joined, member={'id': '../outside', 'name': '示例成员'}),
                   dict(joined, member={'id': 'a' * 32, 'name': ''}),
                   *(dict(joined, token=value) for value in (None, '', 'bad token', 'bad\ntoken', '非口令')),
                   [], b'{broken', b'\xff']
        for body in invalid:
            requests = []
            def respond(path, headers):
                requests.append((path, headers.get('Authorization')))
                return (200, body, {}) if path == '/api/join' else (401, {}, {})
            service = self.faulty_service(respond)
            url = 'http://127.0.0.1:' + str(service.server_port)
            with self.subTest(body=body):
                status, result = self.request(self.a, '/api/team/settings', {'url': url, 'name': '示例成员'})
                self.assertEqual(status, 409)
                self.assertEqual(result['error'], team.FORMAT_ERROR)
                self.assertEqual(requests, [('/api/me', 'Bearer ' + self.first['token']), ('/api/join', None)])
                self.assertNotIn(joined['token'], json.dumps(result))
                self.assertEqual(store.physical_state(self.home), before)

    def test_saved_key_403_can_join_and_new_token_is_verified_and_redacted(self):
        self.configure()
        new_key = 'example-joined-token'
        requests = []
        def respond(path, headers):
            requests.append((path, headers.get('Authorization')))
            if path == '/api/join':
                return 200, {'service': 'skill-pilot-team', 'api': 1,
                             'member': {'id': 'a' * 32, 'name': '示例成员'}, 'token': new_key}, {}
            if headers.get('Authorization') != 'Bearer ' + new_key:
                return 403, {}, {}
            return 200, ({'service': 'skill-pilot-team', 'api': 1, 'member': {'id': 'a' * 32, 'name': new_key}}
                         if path == '/api/me' else {'shares': []}), {}
        service = self.faulty_service(respond)
        url = 'http://127.0.0.1:' + str(service.server_port)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.ok(self.a, '/api/team/settings', {'url': url, 'name': '示例成员'})
            self.assertEqual(result['member']['name'], '[已隐藏]')
            self.assertNotIn(new_key, json.dumps(result) + stderr.getvalue())
        self.assertEqual(requests, [('/api/me', 'Bearer ' + self.first['token']), ('/api/join', None),
                                    ('/api/me', 'Bearer ' + new_key), ('/api/shares', 'Bearer ' + new_key)])
        self.assertEqual(team.settings(self.home), {'url': url, 'key': new_key})

    def test_joined_token_must_pass_status_before_replacing_settings(self):
        self.configure()
        before = store.physical_state(self.home)
        new_key = 'example-joined-token'
        requests = []
        def respond(path, headers):
            requests.append(path)
            if path == '/api/join':
                return 200, {'service': 'skill-pilot-team', 'api': 1,
                             'member': {'id': 'a' * 32, 'name': '示例成员'}, 'token': new_key}, {}
            return ((200, {'service': 'skill-pilot-team', 'api': 2}, {})
                    if headers.get('Authorization') == 'Bearer ' + new_key else (401, {}, {}))
        service = self.faulty_service(respond)
        status, result = self.request(self.a, '/api/team/settings', {
            'url': 'http://127.0.0.1:' + str(service.server_port), 'name': '示例成员'})
        self.assertEqual(status, 409)
        self.assertIn('版本不匹配', result['error'])
        self.assertNotIn(new_key, json.dumps(result))
        self.assertEqual(requests, ['/api/me', '/api/join', '/api/me'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_get_status_keeps_settings_while_station_is_offline_and_recovers(self):
        self.configure()
        self.publish()
        path = self.home / '.skill-pilot/team.json'
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            port = unused.getsockname()[1]
            url = 'http://127.0.0.1:' + str(port)
            path.write_text(json.dumps({'url': url, 'key': self.first['token']}))
            before = store.physical_state(self.home)
            result = self.ok(self.a, '/api/team')
            self.assertEqual({key: value for key, value in result.items() if key != 'error'}, {
                'url': url, 'has_key': True, 'configured': True, 'member': None, 'share': None})
            self.assertIn('连不上分享站', result['error'])
            self.assertEqual(self.request(self.a, '/api/team/settings', {'url': url})[0], 409)
            self.assertEqual(store.physical_state(self.home), before)
        self.start(team_hub.HubServer(self.hub.store.data, '127.0.0.1', port))
        recovered = self.ok(self.a, '/api/team')
        self.assertNotIn('error', recovered)
        self.assertEqual(recovered['url'], url)
        self.assertEqual(recovered['member']['id'], self.first['id'])
        self.assertEqual(recovered['share']['skill_count'], 4)
        self.assertEqual(store.physical_state(self.home), before)

    def test_get_status_keeps_settings_with_wrong_key_and_recovers(self):
        self.configure()
        self.publish()
        path = self.home / '.skill-pilot/team.json'
        original = path.read_bytes()
        path.write_text(json.dumps({'url': self.url, 'key': 'wrong'}))
        before = store.physical_state(self.home)
        result = self.ok(self.a, '/api/team')
        self.assertEqual({key: value for key, value in result.items() if key != 'error'}, {
            'url': self.url, 'has_key': True, 'configured': True, 'member': None, 'share': None})
        self.assertIn('口令不对', result['error'])
        self.assertEqual(self.request(self.a, '/api/team/settings', {'url': self.url})[0], 409)
        self.assertEqual(self.request(self.a, '/api/team/shares')[0], 409)
        self.assertEqual(store.physical_state(self.home), before)
        path.write_bytes(original)
        recovered = self.ok(self.a, '/api/team')
        self.assertNotIn('error', recovered)
        self.assertEqual(recovered['member']['id'], self.first['id'])
        self.assertEqual(recovered['share']['skill_count'], 4)
        self.assertEqual(path.read_bytes(), original)

    def test_get_status_keeps_settings_on_version_and_list_errors_and_recovers(self):
        self.configure()
        self.publish()
        normal = self.ok(self.a, '/api/team')
        state = {'api': 2, 'list_status': 200}
        def respond(path, headers):
            return ((200, {'service': 'skill-pilot-team', 'api': state['api'], 'member': normal['member']}, {})
                    if path == '/api/me' else (state['list_status'], {'shares': [normal['share']]}, {}))
        service = self.faulty_service(respond)
        url = 'http://127.0.0.1:' + str(service.server_port)
        path = self.home / '.skill-pilot/team.json'
        path.write_text(json.dumps({'url': url, 'key': self.first['token']}))
        before = store.physical_state(self.home)
        for api, list_status, message in ((2, 200, '版本不匹配'), (1, 401, '口令不对')):
            state.update(api=api, list_status=list_status)
            with self.subTest(api=api, list_status=list_status):
                result = self.ok(self.a, '/api/team')
                self.assertEqual({key: value for key, value in result.items() if key != 'error'}, {
                    'url': url, 'has_key': True, 'configured': True, 'member': None, 'share': None})
                self.assertIn(message, result['error'])
                self.assertEqual(self.request(self.a, '/api/team/settings', {'url': url})[0], 409)
                self.assertEqual(store.physical_state(self.home), before)
        state.update(api=1, list_status=200)
        self.assertEqual(self.ok(self.a, '/api/team'), {**normal, 'url': url})
        self.assertEqual(store.physical_state(self.home), before)

    def test_get_status_returns_empty_settings_for_damaged_file_and_recovers(self):
        self.configure()
        self.publish()
        normal = self.ok(self.a, '/api/team')
        path = self.home / '.skill-pilot/team.json'
        path.write_text('{"url":')
        before = store.physical_state(self.home)
        result = self.ok(self.a, '/api/team')
        self.assertEqual({key: value for key, value in result.items() if key != 'error'}, {
            'url': '', 'has_key': False, 'configured': False, 'member': None, 'share': None})
        self.assertEqual(result['error'], '分享站设置没能读懂，请在设置里重新连接。')
        with patch.object(team, '_request') as network:
            status, result = self.request(self.a, '/api/team/settings', {'url': self.url})
            self.assertEqual(status, 409)
            self.assertEqual(result['error'], '请填写你的名字，同事会看到它。')
            network.assert_not_called()
        self.assertEqual(store.physical_state(self.home), before)
        self.ok(self.a, '/api/team/settings', {'url': self.url, 'key': self.first['token']})
        self.assertEqual(self.ok(self.a, '/api/team'), normal)

    def test_invalid_addresses_and_keys_never_connect_or_write(self):
        before = store.physical_state(self.home)
        addresses = [None, [], 1, 'ftp://example.invalid', 'file:///tmp/example', 'http://a:b@example.invalid',
                     'http://@example.invalid', 'http://example.invalid?key=secret',
                     'http://example.invalid?', 'http://example.invalid#fragment', 'http://example.invalid:bad',
                     'http://example.invalid:65536', 'http://example.invalid:0', 'http://exa\nmple.invalid']
        with patch.object(team, '_request') as network:
            for url in addresses:
                with self.subTest(url=url):
                    status, result = self.request(self.a, '/api/team/settings', {'url': url, 'key': self.first['token']})
                    self.assertEqual(status, 409)
                    if not isinstance(url, str):
                        self.assertEqual(result['error'], '请填写分享站地址。')
            for key in (None, [], 1, 'invalid\nkey', 'not a token', '非口令'):
                status, result = self.request(self.a, '/api/team/settings', {'url': self.url, 'key': key})
                self.assertEqual(status, 409)
                if not isinstance(key, str):
                    self.assertEqual(result['error'], '请填写分享站地址。')
            network.assert_not_called()
        self.assertEqual(store.physical_state(self.home), before)

    def test_readonly_allows_browsing_previews_and_rejects_every_write(self):
        self.configure()
        uploaded = self.publish()
        imported = self.ok(self.b, '/api/team/import/preview', {
            'member': self.first['id'], 'skills': ['acme-main'], 'agents': ['claude']})
        before = [store.physical_state(home) for home in (self.home, self.other, self.hub.store.data)]
        writes = [('/api/team/settings', {'url': self.url, 'key': self.second['token']}),
                  ('/api/team/settings', {'url': self.url, 'name': '示例成员'}),
                  ('/api/team/upload/apply', {'review': uploaded['review']}), ('/api/team/withdraw', {}),
                  ('/api/team/import/apply', {'review': imported['review'], 'revision': imported['revision']})]
        for readonly in (patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}),
                         patch.object(store, 'write_allowed', return_value=False)):
            with readonly, patch.object(github, 'inspect') as download:
                self.ok(self.b, '/api/team')
                self.ok(self.b, '/api/team/shares/' + self.first['id'])
                self.ok(self.b, '/api/team/upload/preview', {})
                self.ok(self.b, '/api/team/import/preview', {'member': self.first['id'], 'skills': ['acme-main']})
                for path, body in writes:
                    self.assertEqual(self.request(self.b, path, body)[0], 403)
                download.assert_not_called()
        self.assertEqual([store.physical_state(home) for home in (self.home, self.other, self.hub.store.data)], before)

    def test_all_team_routes_require_dashboard_token_and_origin(self):
        routes = [('/api/team', None), ('/api/team/shares', None), ('/api/team/shares/' + self.first['id'], None),
                  ('/api/team/settings', {}), ('/api/team/upload/preview', {}), ('/api/team/upload/apply', {}),
                  ('/api/team/withdraw', {}), ('/api/team/import/preview', {}), ('/api/team/import/apply', {})]
        before = store.physical_state(self.home)
        with patch.object(team, '_request') as network:
            for path, body in routes:
                for header in ({'X-Skill-Pilot-Token': 'wrong'}, {'Origin': 'https://example.invalid'},
                               {'Sec-Fetch-Site': 'cross-site'}):
                    self.assertEqual(self.request(self.a, path, body, header)[0], 403)
            network.assert_not_called()
        self.assertEqual(store.physical_state(self.home), before)

    def test_reviews_and_original_revision_cannot_be_replaced_by_request_data(self):
        self.configure()
        self.publish()
        plan = self.ok(self.b, '/api/team/import/preview', {
            'member': self.first['id'], 'skills': ['acme-main'], 'agents': ['claude']})
        before = store.physical_state(self.other)
        with patch.object(github, 'inspect') as download:
            for review in (None, '', 'missing', []):
                self.assertEqual(self.request(self.b, '/api/team/import/apply', {'review': review})[0], 409)
                self.assertEqual(self.request(self.b, '/api/team/upload/apply', {'review': review})[0], 409)
            for revision in (None, '', False, 'wrong'):
                self.assertEqual(self.request(self.b, '/api/team/import/apply', {
                    'review': plan['review'], 'revision': revision})[0], 409)
            self.assertEqual(store.physical_state(self.other), before)
            self.registry(self.other, lambda skills: skills['acme-other'].update(external='changed'))
            changed = store.physical_state(self.other)
            for revision in (plan['revision'], operations.table(self.other)['revision']):
                self.assertEqual(self.request(self.b, '/api/team/import/apply', {
                    'review': plan['review'], 'revision': revision})[0], 409)
            download.assert_not_called()
        self.assertEqual(store.physical_state(self.other), changed)

    def test_manual_source_urls_are_data_and_only_https_is_returned(self):
        self.configure()
        data = sharing.export_config(self.home)
        manual = next(row for row in data['skills'] if row['name'] == 'acme-local')
        reason = '<script>example()</script> 请通过飞书官方方式补齐'
        for url in ('javascript:example()', 'http://example.invalid', 'file:///tmp/example',
                    'https://example.invalid/skills'):
            manual['source'] = {'kind': 'manual', 'url': url, 'reason': reason}
            team.upload(self.home, data)
            result = self.ok(self.b, '/api/team/shares/' + self.first['id'])
            row = next(row for row in result['config']['skills'] if row['name'] == 'acme-local')
            self.assertEqual(row['source']['reason'], reason)
            self.assertEqual(row['source'].get('url'), url if url.startswith('https://') else None)

    def test_availability_covers_modified_manual_and_transitive_missing_dependencies(self):
        data = sharing.export_config(self.home)
        rows = {row['name']: row for row in data['skills']}
        rows['acme-main']['requires'] = ['acme-local']
        rows['acme-base']['local_modified'] = True
        for row in rows.values():
            row['assigned_agents'] = ['agent-absent-here']
        before = store.physical_state(self.other)
        with patch.object(github, 'inspect') as download:
            states = sharing.local_status(data, self.other)
            download.assert_not_called()
        self.assertEqual(states['acme-other']['status'], 'installed')
        self.assertIn('分享者', states['acme-base']['reason'])
        self.assertIn('acme-local', states['acme-main']['reason'])
        self.assertEqual(states['acme-local']['status'], 'needs_files')
        self.assertEqual(states['acme-extra']['status'], 'installable')
        self.assertEqual(store.physical_state(self.other), before)

    def test_bad_selections_are_rejected_without_download_or_writes(self):
        self.configure()
        self.publish()
        before = store.physical_state(self.other)
        with patch.object(github, 'inspect') as download:
            for skills in (None, [], 'acme-main', ['missing'], [[]]):
                self.assertEqual(self.request(self.b, '/api/team/import/preview', {
                    'member': self.first['id'], 'skills': skills})[0], 409)
            for agents in ('claude', [], ['missing'], [[]]):
                self.assertEqual(self.request(self.b, '/api/team/import/preview', {
                    'member': self.first['id'], 'skills': ['acme-main'], 'agents': agents})[0], 409)
            for exclude in ('acme-main', ['missing'], [[]]):
                self.assertEqual(self.request(self.b, '/api/team/upload/preview', {'exclude': exclude})[0], 409)
            for member in ('../example', '?key=example', None, []):
                self.assertEqual(self.request(self.b, '/api/team/import/preview', {
                    'member': member, 'skills': ['acme-main']})[0], 409)
            download.assert_not_called()
        self.assertEqual(store.physical_state(self.other), before)

    def test_fake_home_skips_team_settings_even_via_links(self):
        self.configure()
        (self.home / '.skill-pilot/key-link').symlink_to(self.home / '.skill-pilot/team.json')
        spec = importlib.util.spec_from_file_location('fake_home_team', store.PROJECT / 'scripts/make-fake-home.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        destination = self.root / 'Another Example Home'
        before = store.physical_state(self.home)
        module.make_fake_home(self.home, destination)
        self.assertFalse((destination / '.skill-pilot/team.json').exists())
        self.assertFalse((destination / '.skill-pilot/key-link').exists())
        self.assertEqual(store.physical_state(self.home), before)

    def faulty_service(self, response):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status, body, headers = response(self.path, self.headers)
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_POST = do_GET
        return self.start(ThreadingHTTPServer(('127.0.0.1', 0), Handler))

    def test_network_errors_version_format_and_size_leave_files_unchanged(self):
        self.configure()
        before = store.physical_state(self.home)
        me = {'service': 'skill-pilot-team', 'api': 1, 'member': {'id': self.first['id'], 'name': '示例成员'}}
        responses = [(401, {'error': self.first['token']}, {}, '口令不对'),
                     (200, dict(me, api=2), {}, '版本不匹配'),
                     (200, dict(me, api=True), {}, '版本不匹配'),
                     (200, b'invalid json', {}, '名单格式不对'),
                     (200, b'\xff', {}, '名单格式不对'),
                     (200, b'x' * (team.MAX_BODY + 1), {}, '超过 1 MB')]
        for status, body, headers, message in responses:
            service = self.faulty_service(lambda path, request_headers, s=status, b=body, h=headers: (s, b, h))
            with self.subTest(message=message), contextlib.redirect_stderr(io.StringIO()) as stderr:
                code, result = self.request(self.a, '/api/team/settings', {
                    'url': 'http://127.0.0.1:' + str(service.server_port), 'key': self.first['token']})
                self.assertEqual(code, 409, result)
                self.assertIn(message, result['error'])
                self.assertNotIn(self.first['token'], json.dumps(result) + stderr.getvalue())
            self.assertEqual(store.physical_state(self.home), before)
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            closed_url = 'http://127.0.0.1:' + str(unused.getsockname()[1])
        code, result = self.request(self.a, '/api/team/settings', {'url': closed_url, 'key': self.first['token']})
        self.assertEqual(code, 409)
        self.assertIn('连不上分享站', result['error'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_timeouts_and_incomplete_responses_are_plain_errors(self):
        self.configure()
        before = store.physical_state(self.home)
        for error in (TimeoutError('example timeout'), IncompleteRead(b'example', 100)):
            with patch.object(urllib.request.OpenerDirector, 'open', side_effect=error) as opened:
                with self.assertRaisesRegex(ValueError, '连不上分享站'):
                    team.status(self.home)
                self.assertEqual(opened.call_args.kwargs['timeout'], 10)
        self.assertEqual(store.physical_state(self.home), before)

    def test_oversized_upload_is_rejected_before_sending_the_list(self):
        self.configure()
        data = sharing.export_config(self.home)
        manual = next(row for row in data['skills'] if row['name'] == 'acme-local')
        manual['source']['reason'] = 'x' * team.MAX_BODY
        before = store.physical_state(self.home)
        with self.assertRaisesRegex(ValueError, '超过 1 MB'):
            team.upload(self.home, data)
        self.assertEqual(team.shares(self.home), {'shares': []})
        self.assertEqual(store.physical_state(self.home), before)

    def test_service_cannot_echo_credentials_in_recognized_fields(self):
        def respond(path, headers):
            data = ({'service': 'skill-pilot-team', 'api': 1,
                     'member': {'id': self.first['id'], 'name': self.first['token']}}
                    if path == '/api/me' else {'shares': []})
            # Escaped JSON characters must be redacted after decoding, too.
            raw = json.dumps(data).replace(self.first['token'],
                ''.join('\\u%04x' % ord(char) for char in self.first['token'])).encode()
            return 200, raw, {}
        service = self.faulty_service(respond)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.ok(self.a, '/api/team/settings', {
                'url': 'http://127.0.0.1:' + str(service.server_port), 'key': self.first['token']})
            self.assertEqual(result['member']['name'], '[已隐藏]')
            self.ok(self.a, '/api/team')
            self.assertNotIn(self.first['token'], stderr.getvalue())

    def test_saving_settings_cannot_write_through_an_outside_link(self):
        outside = self.root / 'Outside settings.json'
        outside.write_text('example private settings')
        path = self.home / '.skill-pilot/team.json'
        path.symlink_to(outside)
        before = store.physical_state(self.root)
        status, result = self.request(self.a, '/api/team/settings', {'url': self.url, 'key': self.first['token']})
        self.assertEqual(status, 409)
        self.assertIn('之外', result['error'])
        self.assertEqual(store.physical_state(self.root), before)

    def test_invalid_remote_configs_are_revalidated_locally(self):
        invalid = sharing.export_config(self.home)
        invalid['skills'][0]['requires'] = ['missing']
        def respond(path, headers):
            if path.startswith('/api/shares/'):
                return 200, {'member': {'id': self.first['id'], 'name': '示例成员'},
                             'updated_at': 'example', 'config': invalid}, {}
            return 200, ({'service': 'skill-pilot-team', 'api': 1,
                          'member': {'id': self.second['id'], 'name': '示例成员 B'}} if path == '/api/me' else
                         {'shares': []}), {}
        service = self.faulty_service(respond)
        self.ok(self.b, '/api/team/settings', {'url': 'http://127.0.0.1:' + str(service.server_port),
                                             'key': self.second['token']})
        before = store.physical_state(self.other)
        with patch.object(github, 'inspect') as download:
            for path, body in [('/api/team/shares/' + self.first['id'], None),
                               ('/api/team/import/preview', {'member': self.first['id'], 'skills': ['acme-main']})]:
                status, result = self.request(self.b, path, body)
                self.assertEqual(status, 409)
                self.assertIn('名单格式不对', result['error'])
            download.assert_not_called()
        self.assertEqual(store.physical_state(self.other), before)

    def test_redirects_never_forward_the_credential(self):
        hits = []
        target = self.faulty_service(lambda path, headers: (hits.append(dict(headers)) or 200, {}, {}))
        redirect = self.faulty_service(lambda path, headers: (
            302, {}, {'Location': 'http://127.0.0.1:' + str(target.server_port) + '/api/me'}))
        before = store.physical_state(self.home)
        status, result = self.request(self.a, '/api/team/settings', {
            'url': 'http://127.0.0.1:' + str(redirect.server_port), 'key': self.first['token']})
        self.assertEqual(status, 409)
        self.assertIn('跳转', result['error'])
        self.assertEqual(hits, [])
        self.assertEqual(store.physical_state(self.home), before)


if __name__ == '__main__':
    unittest.main()
