import base64
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

from skill_pilot import cli, team_hub
from tests import set_up_home


def listing(name='example', kind='github', **extra):
    source = ({'kind': 'github', 'repository': 'Acme/skills', 'path': 'skills/' + name} if kind == 'github' else
              {'kind': 'lark'} if kind == 'lark' else {'kind': 'files', 'files': [file('SKILL.md', 'Acme 示例正文')]})
    return {'name': name, 'requires': [], 'source': source, **extra}


def file(path, content='', executable=False):
    return {'path': path, 'content': base64.b64encode(content.encode() if isinstance(content, str) else content).decode(),
            'executable': executable}


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
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            self.assertEqual(response.headers.get_content_type(), 'application/json')
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            return response.status, json.loads(response.read()) if method != 'HEAD' else None
        finally:
            connection.close()

    def put(self, rows=None, token='first'):
        return self.request('/api/listings/me', 'PUT', {'listings': [listing()] if rows is None else rows}, token)

    def cards(self):
        status, body = self.request('/api/catalog')
        self.assertEqual(status, 200, body)
        return body['cards']

    def package(self, name='example', suffix=''):
        return self.request('/api/packages/' + self.first['id'] + '/' + name + suffix)

    def state(self):
        return {str(p.relative_to(self.data)): p.read_bytes() for p in self.data.rglob('*.json')}

    def test_members_private_and_api_two(self):
        self.assertNotEqual(self.first['token'], self.second['token'])
        saved = (self.data / 'members.json').read_text()
        self.assertEqual(json.loads(saved)['members'][0]['token_sha256'], hashlib.sha256(self.first['token'].encode()).hexdigest())
        self.assertNotIn(self.first['token'], saved)
        self.assertNotIn('token', json.dumps(self.store.members()))
        self.assertEqual(stat.S_IMODE((self.data / 'members.json').stat().st_mode), 0o600)
        self.assertEqual(self.request(), (200, {'service': 'skill-pilot-team', 'api': 2,
            'member': {k: self.first[k] for k in ('id', 'name')}}))
        for name in ('', '  ', 'Acme\nexample', 'Acme\x00example'):
            with self.assertRaises(ValueError):
                self.store.add_member(name)

    def test_every_path_and_method_authenticates(self):
        routes = [('/api/me', 'GET'), ('/api/catalog', 'GET'), ('/api/listings/me', 'PUT'),
                  ('/api/listings/me/example', 'DELETE'), ('/api/ratings', 'PUT'), ('/api/ratings', 'DELETE'),
                  ('/api/packages/' + self.first['id'] + '/example', 'GET'), ('/members.json', 'GET'), ('/api/me', 'BREW')]
        for path, method in routes:
            for token in (None, '', 'wrong'):
                with self.subTest(path=path, method=method):
                    status, body = self.request(path, method, token=token)
                    self.assertEqual(status, 401)
                    self.assertEqual(set(body), {'error'})
        self.assertEqual(self.request(headers={'Authorization': 'Basic wrong'})[0], 401)

    def test_open_join_returns_a_usable_token_but_stores_only_its_digest(self):
        self.server.open = True
        status, joined = self.request('/api/join', 'POST', {'name': '  示例成员  '}, token=None)
        self.assertEqual(status, 200)
        self.assertEqual(set(joined), {'service', 'api', 'member', 'token'})
        self.assertEqual(joined['service'], 'skill-pilot-team')
        self.assertEqual(joined['api'], 2)
        self.assertEqual(joined['member']['name'], '示例成员')
        self.assertEqual(set(joined['member']), {'id', 'name'})
        token = joined['token']
        self.assertRegex(token, r'^[A-Za-z0-9_-]+$')
        self.assertEqual(self.request(token=token)[1]['member'], joined['member'])
        self.assertEqual(self.put(token=token)[0], 200)
        self.assertEqual(self.cards()[0]['listings'][0]['member'], joined['member'])
        self.assertEqual(self.request('/api/listings/me/example', 'DELETE', token=token), (200, {}))
        self.assertEqual(self.cards(), [])
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


    def test_closing_open_join_after_restart_keeps_member_and_listing(self):
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
        self.assertEqual(self.request('/api/catalog', token=joined['token'])[1]['cards'][0]['listings'][0]['member'], joined['member'])
        self.assertEqual(self.request('/api/join', 'POST', {'name': '新示例成员'}, token=None)[0], 403)
        self.store.remove_member(joined['member']['id'])
        self.assertEqual(self.request(token=joined['token'])[0], 401)


    def test_join_auth_exemption_requires_the_exact_request_target(self):
        self.server.open = True
        for path in ('/api/join?', '//api/join', '///api/join'):
            with self.subTest(path=path):
                self.assertEqual(self.raw_upload([], path=path, method='POST', token=None)[0], 401)


    def test_catalog_coalesces_github_and_lark_but_separates_files_and_orders(self):
        self.assertEqual(self.cards(), [])
        self.put([listing(title='示例技能', description='示例\n说明'), listing('local', 'files'), listing('official', 'lark')])
        second = listing(note='示例推荐')
        second['source']['repository'] = 'acme/SKILLS'
        self.put([second, listing('local', 'files'), listing('official', 'lark')], self.second['token'])
        cards = self.cards()
        self.assertEqual(len(cards), 4)
        shared = next(c for c in cards if c['key'].startswith('github:'))
        self.assertEqual(shared['key'], 'github:acme/skills:skills/example')
        self.assertEqual([r['member']['id'] for r in shared['listings']], [self.second['id'], self.first['id']])
        self.assertEqual(len(next(c for c in cards if c['key'] == 'lark:official')['listings']), 2)
        self.assertNotIn('content', json.dumps(cards))
        self.assertEqual([c['listings'][0]['updated_at'] for c in cards], sorted(
            [c['listings'][0]['updated_at'] for c in cards], reverse=True))

    def test_whitelist_and_replacement_remove_old_package(self):
        row = listing(kind='files', title='示例', summary='示例用途', group='示例分组', description='示例说明', note='示例推荐')
        row['private'] = 'PRIVATE_FIELD'
        row['source'].update(private='PRIVATE_SOURCE', version='forged')
        row['source']['files'][0]['private'] = 'PRIVATE_FILE'
        self.assertEqual(self.put([row])[0], 200)
        package = self.package()[1]
        self.assertNotIn('PRIVATE_', json.dumps(self.state().__str__()))
        self.assertEqual(set(package['files'][0]), {'path', 'content', 'executable'})
        summary = self.cards()[0]['listings'][0]
        self.assertEqual(set(summary), {'name', 'requires', 'source', 'title', 'summary', 'group', 'description', 'note', 'member', 'updated_at'})
        self.assertEqual(summary['source']['version'], package['version'])
        self.assertEqual(self.put()[0], 200)
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(self.package()[0], 404)
        self.assertFalse((self.data / 'packages' / self.first['id'] / 'example.json').exists())
        self.assertNotIn('title', self.cards()[0]['listings'][0])

    def test_versions_include_content_path_and_executable_but_not_file_order(self):
        rows = [file('SKILL.md', '示例'), file('scripts/example.py', 'print(1)', True)]
        version = team_hub.package_version(rows)
        manual = [[r['path'], r['executable'], hashlib.sha256(base64.b64decode(r['content'])).hexdigest()] for r in rows]
        expected = hashlib.sha256(json.dumps(manual, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(version, expected)
        self.assertEqual(version, team_hub.package_version(list(reversed(rows))))
        for field, value in [('content', file('x', '改一个字')['content']), ('path', 'changed.py'), ('executable', False)]:
            changed = copy.deepcopy(rows)
            changed[1][field] = value
            self.assertNotEqual(version, team_hub.package_version(changed))

    def test_github_packages_keep_identity_and_legacy_catalog_fields(self):
        row = listing(title='示例技能')
        row['source'].update(commit='a' * 40, tree_sha='b' * 40,
                             files=[file('SKILL.md', '示例'), file('scripts/example.sh', 'exit 9', True)])
        key = team_hub.listing_key(self.first['id'], row)
        status, receipt = self.put([row])
        self.assertEqual(status, 200, receipt)
        version = receipt['listings'][0]['version']
        source = self.cards()[0]['listings'][0]['source']
        self.assertEqual(source, {'kind': 'github', 'repository': 'Acme/skills', 'path': 'skills/example',
            'commit': 'a' * 40, 'tree_sha': 'b' * 40, 'version': version, 'file_count': 2,
            'size': len('示例'.encode()) + len('exit 9')})
        self.assertEqual(self.cards()[0]['key'], key)
        self.assertEqual(self.package(suffix='?version=' + version)[1]['files'], row['source']['files'])
        self.assertEqual(team_hub.clean_listing(self.cards()[0]['listings'][0], packaged=True)['source'], source)
        # The old GitHub branch only kept these four fields, then normalized the folder.
        legacy = copy.deepcopy(self.cards()[0]['listings'][0])
        legacy['source'] = {k: source[k] for k in ('kind', 'repository', 'path', 'commit')}
        self.assertEqual(team_hub.clean_listing(legacy, packaged=True)['source'], legacy['source'])
        self.assertEqual(team_hub.listing_key(self.first['id'], legacy), key)
        self.assertEqual(self.request()[1]['api'], 2)
        self.assertEqual(self.put()[0], 200)
        self.assertEqual(self.package()[0], 404)
        self.assertNotIn('version', self.cards()[0]['listings'][0]['source'])

    def test_github_packages_reuse_file_limits_and_validate_optional_metadata(self):
        self.put([listing(kind='files')])
        before = self.state()
        for files in ([], [file('other.txt')], [file('SKILL.md'), file('../outside')],
                      [file('SKILL.md')] + [file(str(i)) for i in range(300)],
                      [file('SKILL.md', b'x' * (team_hub.MAX_SIZE + 1))]):
            row = listing()
            row['source']['files'] = files
            self.assertEqual(self.put([row])[0], 400)
            self.assertEqual(self.state(), before)
        for value in (None, [], '', 'A' * 40, 'a' * 39):
            row = listing()
            row['source']['tree_sha'] = value
            self.assertEqual(self.put([row])[0], 400)
            self.assertEqual(self.state(), before)
        for field, value in (('version', '../invalid'), ('file_count', True), ('file_count', 301),
                             ('size', -1), ('size', team_hub.MAX_SIZE + 1)):
            row = listing()
            row['source'].update(version='a' * 64, file_count=1, size=0)
            row['source'][field] = value
            with self.assertRaises(ValueError):
                team_hub.clean_listing(row, packaged=True)
        row = listing()
        row['source']['file_count'] = 1
        with self.assertRaises(ValueError):
            team_hub.clean_listing(row, packaged=True)

    def test_invalid_batches_preserve_every_previous_listing_and_package(self):
        self.put([listing(kind='files')])
        before = self.state()
        invalid = []
        for changes in ({'name': '../outside'}, {'name': ''}, {'requires': ['missing']}, {'requires': 'wrong'},
                        {'requires': ['base', 'base']}, {'requires': [[]]}, {'title': ''}, {'title': 'x' * 41},
                        {'summary': 'x' * 121}, {'group': 'x' * 21}, {'description': 'x' * 1501},
                        {'title': '示例\u200b'}, {'summary': 'bad\ntext'}, {'note': 'x' * 201}, {'note': 'bad\ntext'},
                        {'source': {'kind': 'manual'}}, {'source': {'kind': 'github', 'repository': 'https://a:b@example.invalid', 'path': '.'}},
                        {'source': {'kind': 'github', 'repository': 'Acme/skills', 'path': '../outside'}},
                        {'source': {'kind': 'github', 'repository': 'Acme/skills', 'path': '.', 'commit': 'wrong'}}):
            invalid.append(listing(**changes))
        for path in ('/absolute', '../outside', 'a/../b', 'a\\b', 'a//b', 'a/', './a', 'a\x00b', 'a\u200bb'):
            invalid.append(listing(kind='files', source={'kind': 'files', 'files': [file('SKILL.md'), file(path)]}))
        for files in ([file('only.txt')], [file('SKILL.md'), file('SKILL.md')], [file('SKILL.md'), file('a'), file('a/b')],
                      [{'path': 'SKILL.md', 'content': 'not base64', 'executable': False}],
                      [{'path': 'SKILL.md', 'content': 'AB==', 'executable': False}],
                      [{'path': 'SKILL.md', 'content': '', 'executable': 1}],
                      [file('SKILL.md')] + [file(str(i)) for i in range(300)],
                      [file('SKILL.md', b'x' * (team_hub.MAX_SIZE + 1))]):
            invalid.append(listing(kind='files', source={'kind': 'files', 'files': files}))
        for row in invalid:
            with self.subTest(row=str(row)[:120]):
                self.assertEqual(self.put([listing('otherwise-valid'), row])[0], 400)
                self.assertEqual(self.state(), before)
        self.assertEqual(self.put([listing(), listing()])[0], 400)
        for data in ([], {}, {'listings': []}, {'listings': [None]}):
            self.assertEqual(self.request('/api/listings/me', 'PUT', data)[0], 400)
        self.assertEqual(self.state(), before)

    def test_requires_may_be_in_same_batch_or_previously_owned(self):
        self.assertEqual(self.put([listing('main', requires=['base']), listing('base')])[0], 200)
        self.assertEqual(self.put([listing('second-main', requires=['base'])])[0], 200)
        self.assertEqual(self.put([listing('foreign-main', requires=['base'])], self.second['token'])[0], 400)
        self.assertEqual(self.request('/api/listings/me/base', 'DELETE')[0], 409)
        self.assertIn('main', self.request('/api/listings/me/base', 'DELETE')[1]['error'])

    def test_remove_scoped_missing_dependencies_and_files(self):
        self.put([listing('main', requires=['base']), listing('base', 'files')])
        self.assertEqual(self.request('/api/listings/me/base', 'DELETE', token=self.second['token'])[0], 404)
        self.assertEqual(self.request('/api/listings/me/base', 'DELETE')[0], 409)
        self.assertEqual(self.request('/api/listings/me/main', 'DELETE'), (200, {}))
        self.assertEqual(self.request('/api/listings/me/base', 'DELETE'), (200, {}))
        self.assertEqual(self.request('/api/listings/me/base', 'DELETE')[0], 404)
        self.assertFalse((self.data / 'packages' / self.first['id'] / 'base.json').exists())

    def test_packages_version_conflict_and_no_execution(self):
        self.assertEqual(self.package()[0], 404)
        row = listing(kind='files')
        row['source']['files'].append(file('scripts/example.sh', 'touch SHOULD_NEVER_EXIST', True))
        one = self.put([row])[1]['listings'][0]['version']
        self.assertEqual(self.package(suffix='?version=' + one)[0], 200)
        row['source']['files'][0] = file('SKILL.md', '新版示例')
        two = self.put([row])[1]['listings'][0]['version']
        self.assertNotEqual(one, two)
        self.assertEqual(self.package(suffix='?version=' + one)[0], 409)
        self.assertEqual(self.package(suffix='?version=' + two)[1]['files'], row['source']['files'])
        self.assertFalse(any(p.name == 'SHOULD_NEVER_EXIST' for p in self.home.rglob('*')))

    def test_rating_lifecycle_validation_visibility_and_ownership(self):
        self.put()
        key = self.cards()[0]['key']
        self.assertEqual(self.request('/api/ratings', 'PUT', {'key': key, 'stars': 5})[0], 403)
        for stars in (0, 6, 1.5, True, '5', None):
            self.assertEqual(self.request('/api/ratings', 'PUT', {'key': key, 'stars': stars}, self.second['token'])[0], 400)
        for text in ('x' * 201, 'bad\ntext', 'bad\u200btext'):
            self.assertEqual(self.request('/api/ratings', 'PUT', {'key': key, 'stars': 4, 'text': text}, self.second['token'])[0], 400)
        self.assertEqual(self.request('/api/ratings', 'PUT', {'key': 'missing', 'stars': 4}, self.second['token'])[0], 404)
        for stars in (4, 5):
            status, rating = self.request('/api/ratings', 'PUT', {'key': key, 'stars': stars, 'text': '<b>示例评价</b>', 'private': 'ignored'}, self.second['token'])
            self.assertEqual(status, 200)
            self.assertEqual(rating['stars'], stars)
        self.assertEqual(len(self.cards()[0]['ratings']), 1)
        self.request('/api/listings/me/example', 'DELETE')
        self.assertEqual(self.cards(), [])
        self.put()
        self.assertEqual(self.cards()[0]['ratings'][0]['stars'], 5)
        self.assertEqual(self.request('/api/ratings', 'DELETE', {'key': key}, self.second['token']), (200, {}))
        self.assertEqual(self.request('/api/ratings', 'DELETE', {'key': key}, self.second['token'])[0], 404)
        self.assertEqual(self.cards()[0]['ratings'], [])

    def test_remove_member_cleans_listings_packages_and_authored_ratings(self):
        self.put([listing(kind='files')])
        self.put([listing('second')], self.second['token'])
        key = 'github:acme/skills:skills/second'
        self.request('/api/ratings', 'PUT', {'key': key, 'stars': 5})
        self.store.remove_member(self.first['id'])
        self.assertEqual(self.request()[0], 401)
        self.assertFalse((self.data / 'listings' / (self.first['id'] + '.json')).exists())
        self.assertFalse((self.data / 'packages' / self.first['id']).exists())
        self.assertNotIn(self.first['id'], (self.data / 'ratings.json').read_text())
        cards = self.request('/api/catalog', token=self.second['token'])[1]['cards']
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['ratings'], [])

    def test_simultaneous_upserts_keep_both_and_complete_packages(self):
        barrier = threading.Barrier(2)
        def upload(name):
            barrier.wait(timeout=3)
            return self.put([listing(name, 'files')])
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(upload, ('one', 'two')))
        self.assertEqual([s for s, _ in results], [200, 200])
        self.assertEqual({c['listings'][0]['name'] for c in self.cards()}, {'one', 'two'})
        for name in ('one', 'two'):
            self.assertEqual(self.package(name)[0], 200)
        self.assertFalse(any(p.name.startswith('.') for p in (self.data / 'listings').iterdir()))

    def test_failed_batch_write_restores_packages_and_previous_listings(self):
        self.put([listing(kind='files')])
        before = self.state()
        replace = team_hub.os.replace
        def fail_listing(source, target):
            if Path(target).parent.name == 'listings':
                raise OSError('example write failure')
            return replace(source, target)
        with patch.object(team_hub.os, 'replace', side_effect=fail_listing):
            self.assertEqual(self.put([listing('new', 'files')])[0], 500)
        self.assertEqual(self.state(), before)

    def raw_upload(self, headers, body=b'', path='/api/listings/me', method='PUT', token='first'):
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

    def test_limits_reject_headers_without_waiting_for_body_and_accept_boundary(self):
        self.assertEqual(self.raw_upload([('Content-Length', str(team_hub.MAX_BODY + 1))])[0], 413)
        self.assertEqual(self.raw_upload([('Content-Length', str(team_hub.MAX_RATING_BODY + 1))], path='/api/ratings')[0], 413)
        raw = json.dumps({'listings': [listing()]}).encode()
        self.assertEqual(self.raw_upload([('Content-Length', str(team_hub.MAX_BODY))], raw + b' ' * (team_hub.MAX_BODY - len(raw)))[0], 200)

    def test_bad_lengths_json_and_encoding_leave_data_unchanged(self):
        before = self.state()
        for headers, status in [([], 411), ([('Content-Length', '-1')], 400), ([('Content-Length', 'wrong')], 400),
                                ([('Content-Length', '2'), ('Content-Length', '2')], 400),
                                ([('Transfer-Encoding', 'chunked')], 400), ([('Content-Length', '100')], 400)]:
            self.assertEqual(self.raw_upload(headers)[0], status)
        for raw in (b'{broken', b'\xff', b'{"listings": NaN}', b'[' * 1100):
            self.assertEqual(self.raw_upload([('Content-Length', str(len(raw)))], raw)[0], 400)
        self.assertEqual(self.state(), before)

    def test_only_documented_routes_and_methods_and_legacy_404(self):
        for path in ('/', '/members.json', '/api/me?token=wrong', '/api/shares', '/api/shares/me',
                     '/api/shares/' + self.first['id'], '/api/catalog/', '/api/packages/' + self.first['id'] + '/../members'):
            self.assertEqual(self.request(path)[0], 404)
        for method in ('POST', 'PATCH', 'OPTIONS', 'BREW', 'HEAD'):
            self.assertEqual(self.request('/api/me', method)[0], 405)
        self.assertEqual(self.request('/api/listings/me')[0], 405)
        self.assertEqual(self.request('/api/join', 'POST', {'name': '示例成员'})[0], 403)
        for method in ('GET', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'BREW'):
            self.assertEqual(self.request('/api/join', method)[0], 405)

    def test_logs_never_include_credentials_paths_or_payloads(self):
        with patch('sys.stderr', new_callable=io.StringIO) as log:
            self.put([listing('private-example', 'files')])
            self.request('/api/me?token=' + self.first['token'])
            self.request(token='wrong')
        self.assertEqual(log.getvalue(), '')

    def test_restart_keeps_members_listings_packages_and_ratings_ignores_old_shares(self):
        self.put([listing(kind='files')])
        self.request('/api/ratings', 'PUT', {'key': self.cards()[0]['key'], 'stars': 4}, self.second['token'])
        old = self.data / 'shares' / (self.first['id'] + '.json')
        old.parent.mkdir()
        old.write_text('old data is deliberately not JSON')
        before = self.cards()
        with team_hub.HubServer(self.data, '127.0.0.1', 0) as restarted:
            with restarted.store.locked():
                self.assertEqual(restarted.store._authenticate('Bearer ' + self.first['token'])['id'], self.first['id'])
                self.assertEqual(restarted.store._catalog()['cards'], before)
                self.assertEqual(restarted.store._package(self.first['id'], 'example')['files'], listing(kind='files')['source']['files'])


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
