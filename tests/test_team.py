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

from skill_pilot import operations, sharing, store, team, team_hub, updates, research
from skill_pilot.sources import github, normalize
from tests.test_team_hub import listing, file as package_file
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
        self.registry(self.home, lambda skills: skills['acme-local'].update(source={'source': '自制'}))
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

    def publish(self, names=('acme-main', 'acme-local'), notes=None):
        preview = self.ok(self.a, '/api/team/listings/preview', {'skills': [
            {'name': n, 'note': (notes or {}).get(n, '')} for n in names]})
        self.assertEqual(preview['errors'], [])
        self.ok(self.a, '/api/team/listings/apply', {'review': preview['review']})
        return preview

    def library(self, home):
        paths = ('.codex/skills', '.claude/skills', '.skill-pilot/registry.json', '.skill-pilot/bases')
        return {path: store.physical_state(home / path) for path in paths} | {
            'store': operations.content_digest(home / '.skill-pilot/store')}

    def cards(self, server=None):
        return {c['name']: c for c in self.ok(server or self.b, '/api/team/catalog')['cards']}

    def install(self, name='acme-local', apply=True):
        card = self.cards()[name]
        plan = self.ok(self.b, '/api/team/install/preview', {'key': card['key'], 'agents': ['claude']})
        if not apply:
            return plan
        return self.ok(self.b, '/api/team/install/apply', {'review': plan['review']})

    def raw_put(self, rows, member=None):
        return team._request({'url': self.url, 'key': (member or self.first)['token']}, '/api/listings/me', 'PUT', {'listings': rows})

    def local_github(self):
        return patch.object(github, 'parse_url', return_value=(self.repository.as_uri(), 'acme/skills', self.commit))

    def test_two_users_publish_install_files_and_github_with_dependencies_and_undo(self):
        self.configure()
        path = self.home / '.skill-pilot/store/acme-local'
        (path / '.env').write_text('example private value')
        (path / 'example.bin').write_bytes(b'\x00\xff\x01')
        script = path / 'example.sh'
        script.write_text('exit 9\n')
        script.chmod(0o755)
        labels = {'groups': [{'name': '示例分组', 'skills': {'acme-local': {
            'title': '示例技能', 'summary': '示例用途', 'description': ''}}}]}
        (self.home / '.skill-pilot/labels.json').write_text(json.dumps(labels))
        published = self.publish(notes={'acme-local': '<b>示例推荐</b>'})
        rows = {r['name']: r for r in published['listings']}
        self.assertEqual(rows['acme-base']['auto'], ['acme-main'])
        self.assertEqual(rows['acme-local']['files']['blocked'][0]['path'], '.env')
        self.assertEqual(rows['acme-local']['files']['count'], 3)
        self.assertNotIn('content', json.dumps(published))
        self.assertEqual(self.cards(self.a)['acme-local']['local']['status'], 'mine')
        card = self.cards()['acme-local']
        self.assertEqual(card['local']['status'], 'installable')
        self.assertFalse(card['can_rate'])
        self.assertEqual(card['listings'][0]['note'], '<b>示例推荐</b>')
        before = self.library(self.other)
        plan = self.install(apply=False)
        self.assertGreater(plan['tokens']['claude'], 0)
        installed = self.ok(self.b, '/api/team/install/apply', {'review': plan['review'], 'agents': ['codex'], 'key': 'ignored'})
        self.assertEqual(installed['message'], '从团队商店装上了「示例技能」')
        skill = operations.load(self.other)[1]['skills']['acme-local']
        self.assertEqual(skill['assigned_agents'], ['claude'])
        self.assertEqual(set(skill['source']), {'source', 'team'})
        self.assertEqual(normalize(skill['source'])['label'], '团队商店 · 示例成员 A')
        destination = self.other / '.skill-pilot/store/acme-local'
        self.assertFalse((destination / '.env').exists())
        self.assertEqual((destination / 'example.bin').read_bytes(), b'\x00\xff\x01')
        self.assertTrue((destination / 'example.sh').stat().st_mode & 0o111)
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'installed')
        self.assertTrue(self.cards()['acme-local']['can_rate'])
        with tarfile.open(self.other / '.skill-pilot/backups' / installed['backup_id'] / 'files.tar') as archive:
            self.assertNotIn('.skill-pilot/team.json', archive.getnames())
        self.ok(self.b, '/api/undo', {'id': installed['id'], 'confirmed': True, 'revision': operations.table(self.other)['revision']})
        self.assertEqual(self.library(self.other), before)
        with self.local_github():
            plan = self.install('acme-main', apply=False)
        self.assertEqual({r['name'] for r in plan['install']}, {'acme-main', 'acme-base'})
        installed = self.ok(self.b, '/api/team/install/apply', {'review': plan['review']})
        skills = operations.load(self.other)[1]['skills']
        self.assertEqual(skills['acme-base']['assigned_agents'], ['claude'])
        self.assertEqual(normalize(skills['acme-main']['source'])['kind'], 'github')
        self.ok(self.b, '/api/undo', {'id': installed['id'], 'confirmed': True, 'revision': operations.table(self.other)['revision']})
        self.assertEqual(self.library(self.other), before)

    def test_preview_keeps_downloaded_version_even_after_author_updates_or_removes_it(self):
        self.configure()
        self.publish(('acme-local',))
        old = (self.home / '.skill-pilot/store/acme-local/SKILL.md').read_bytes()
        plan = self.install(apply=False)
        path = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        path.write_bytes(old + b'\nnew version\n')
        self.publish(('acme-local',))
        self.ok(self.a, '/api/team/listings/remove', {'names': ['acme-local']})
        with patch.object(github, 'inspect', side_effect=AssertionError('apply must use the preview')):
            self.ok(self.b, '/api/team/install/apply', {'review': plan['review']})
        self.assertEqual((self.other / '.skill-pilot/store/acme-local/SKILL.md').read_bytes(), old)
        self.assertEqual(self.request(self.b, '/api/team/install/apply', {'review': plan['review']})[0], 409)

    def test_all_local_states_and_dependency_blockers(self):
        self.configure()
        self.raw_put([listing('acme-other'), listing('missing-official', 'lark'),
                      listing('needs-official', requires=['missing-official']), listing('acme-local', 'files')])
        cards = self.cards()
        self.assertEqual(cards['acme-other']['local']['status'], 'installed')
        self.assertEqual(cards['acme-other']['local']['agents'], ['claude'])
        self.assertEqual(cards['missing-official']['local']['status'], 'lark')
        self.assertEqual(cards['needs-official']['local']['status'], 'blocked')
        self.assertIn('missing-official', cards['needs-official']['local']['reason'])
        self.registry(self.other, lambda skills: skills['acme-other'].update(source={'source': '自制'}))
        self.assertEqual(self.cards()['acme-other']['local']['status'], 'conflict')
        self.raw_put([listing('needs-conflict', requires=['acme-other'])])
        # Dependencies with the same local name are reused, even with a different source.
        self.assertEqual(self.cards()['needs-conflict']['local']['status'], 'installable')
        self.raw_put([listing('acme-other', 'lark')])
        self.assertEqual(self.cards()['acme-other']['local']['status'], 'installed')
        self.raw_put([listing('acme-other', 'files')])
        self.assertEqual(self.cards()['acme-other']['local']['status'], 'conflict')
        # A faulty station may omit a dependency: report the missing author listing.
        data = team._catalog(team._connection(self.home))
        card = next(c for c in data if c['listings'][0]['name'] == 'needs-conflict')
        card['listings'][0]['requires'] = ['not-listed']
        with patch.object(team, '_catalog', return_value=data):
            blocked = self.cards()['needs-conflict']
            self.assertEqual(blocked['local']['status'], 'blocked')
            self.assertIn('作者没有上架', blocked['local']['reason'])
            plan = self.ok(self.b, '/api/team/install/preview', {'key': blocked['key'], 'agents': ['claude']})
            self.assertEqual(plan['install'], [])
            self.assertTrue(plan['skipped'])

    def test_existing_dependencies_and_their_dependencies_are_assigned_without_replacing(self):
        self.configure()
        self.registry(self.home, lambda skills: skills['acme-local'].update(requires=['acme-other']))
        self.publish(('acme-local',))
        self.registry(self.other, lambda skills: skills['acme-other'].update(source={'source': '自制'}))
        before = (self.other / '.skill-pilot/store/acme-other/SKILL.md').read_bytes()
        plan = self.install(apply=False)
        self.assertEqual(plan['reuse'], ['acme-other'])
        self.assertEqual([r['name'] for r in plan['install']], ['acme-local'])
        self.ok(self.b, '/api/team/install/apply', {'review': plan['review']})
        self.assertEqual((self.other / '.skill-pilot/store/acme-other/SKILL.md').read_bytes(), before)
        self.assertEqual(operations.load(self.other)[1]['skills']['acme-local']['requires'], ['acme-other'])

    def test_install_revision_permissions_and_download_tampering_require_new_preview(self):
        self.configure()
        self.publish(('acme-local',))
        plan = self.install(apply=False)
        saved = self.b.team_import_reviews[plan['review']]
        path = Path(saved['items'][0]['path']) / 'SKILL.md'
        path.chmod(0o755)
        before = self.library(self.other)
        self.assertEqual(self.request(self.b, '/api/team/install/apply', {'review': plan['review']})[0], 409)
        self.assertEqual(self.library(self.other), before)
        plan = self.install(apply=False)
        local = self.other / '.skill-pilot/store/acme-other/SKILL.md'
        local.chmod(0o755)
        changed = self.library(self.other)
        status, result = self.request(self.b, '/api/team/install/apply', {'review': plan['review'], 'revision': operations.table(self.other)['revision']})
        self.assertEqual(status, 409)
        self.assertIn('重新预览', result['error'])
        self.assertEqual(self.library(self.other), changed)

    def test_candidates_kind_changed_in_use_and_foreign_team_skill(self):
        self.configure()
        self.publish(('acme-local',))
        candidates = {r['name']: r for r in self.ok(self.a, '/api/team/listings/candidates')['skills']}
        self.assertEqual(candidates['acme-main']['kind'], 'github')
        self.assertEqual(candidates['acme-local']['kind'], 'files')
        self.assertTrue(candidates['acme-local']['listed'])
        self.assertFalse(candidates['acme-local']['changed'])
        path = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        path.write_text(path.read_text() + '\n新版\n')
        self.assertTrue(next(r for r in self.ok(self.a, '/api/team/listings/candidates')['skills'] if r['name'] == 'acme-local')['changed'])
        path = self.home / '.skill-pilot/store/acme-main/SKILL.md'
        path.write_text(path.read_text() + '\n本机改过\n')
        self.assertEqual(next(r for r in self.ok(self.a, '/api/team/listings/candidates')['skills'] if r['name'] == 'acme-main')['kind'], 'files')
        self.install()
        foreign = next(r for r in self.ok(self.b, '/api/team/listings/candidates')['skills'] if r['name'] == 'acme-local')
        self.assertIn('别人的', foreign['unavailable'])
        preview = self.ok(self.b, '/api/team/listings/preview', {'skills': [{'name': 'acme-local'}]})
        self.assertEqual(len(preview['errors']), 1)
        operations.apply_assign([{'skill': 'acme-extra', 'agent': 'codex', 'enabled': False}], home=self.home)
        self.assertNotIn('acme-extra', [r['name'] for r in self.ok(self.a, '/api/team/listings/candidates')['skills']])

    def test_secrets_are_excluded_internal_links_flattened_and_skipped_content_ignored(self):
        self.configure()
        root = self.home / '.skill-pilot/store/acme-local'
        names = ('.env', '.env.production', 'test.pem', 'test.key', 'test.p12', 'id_rsa.pub', 'id_ed25519', '.npmrc', '.pypirc', 'credentials.json')
        for name in names:
            (root / name).write_text('example private content')
        contents = ('-----BEGIN OPENSSH PRIVATE KEY-----', 'sk-' + 'a' * 32, 'ghp_' + 'b' * 36,
                    'github_pat_' + 'c' * 40, 'AKIA' + 'D' * 16, 'xoxb-' + 'e' * 32,
                    'api_key: "' + 'f' * 20 + '"', 'token = ' + 'g' * 20, 'secret: ' + 'h' * 20, 'password=' + 'i' * 20)
        for i, text in enumerate(contents):
            (root / f'secret-{i}.txt').write_text(text)
        (root / 'alias.md').symlink_to('SKILL.md')
        (root / 'refs').mkdir()
        (root / 'refs/example.txt').write_text('Acme example')
        (root / 'alias-dir').symlink_to('refs', target_is_directory=True)
        (root / 'node_modules').mkdir()
        (root / 'node_modules/private.txt').write_text('not uploaded')
        preview = self.publish(('acme-local',))
        row = preview['listings'][0]
        self.assertEqual(len(row['files']['blocked']), len(names) + len(contents))
        config = team._connection(self.home)
        package = team._request(config, '/api/packages/' + self.first['id'] + '/acme-local')
        self.assertEqual({f['path'] for f in package['files']}, {'SKILL.md', 'alias.md', 'refs/example.txt', 'alias-dir/example.txt'})
        self.assertEqual(package['version'], row['version'])
        self.assertNotIn('example private content', json.dumps(preview))

    def test_external_link_and_foreign_dependency_block_parents(self):
        self.configure()
        root = self.home / '.skill-pilot/store/acme-local'
        outside = self.root / 'outside example'
        outside.write_text('example')
        (root / 'outside.txt').symlink_to(outside)
        self.registry(self.home, lambda skills: skills['acme-main'].update(requires=['acme-local']))
        candidate = next(r for r in self.ok(self.a, '/api/team/listings/candidates')['skills'] if r['name'] == 'acme-local')
        self.assertIn('文件夹外', candidate['unavailable'])
        plan = self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-main'}]})
        self.assertEqual({r['name'] for r in plan['errors']}, {'acme-main', 'acme-local'})
        self.assertEqual(plan['listings'], [])

    def test_blocked_skill_manifest_and_size_count_limits_propagate_to_dependents(self):
        self.configure()
        root = self.home / '.skill-pilot/store/acme-local'
        self.registry(self.home, lambda skills: skills['acme-main'].update(requires=['acme-local']))
        manifest = root / 'SKILL.md'
        original = manifest.read_text()
        manifest.write_text(original + '\ntoken=' + 'x' * 32)
        for scenario in ('secret', 'size', 'count'):
            if scenario == 'size':
                manifest.write_text(original)
                (root / 'big.bin').write_bytes(b'\x00' * team_hub.MAX_SIZE)
            if scenario == 'count':
                (root / 'big.bin').unlink()
                for i in range(300):
                    (root / str(i)).touch()
            preview = self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-main'}]})
            self.assertEqual({r['name'] for r in preview['errors']}, {'acme-main', 'acme-local'}, scenario)
            self.assertEqual(preview['listings'], [])

    def test_publication_requires_original_preview_and_no_local_changes(self):
        self.configure()
        plan = self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-local'}]})
        path = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        path.write_text(path.read_text() + '\n本机变化\n')
        status, result = self.request(self.a, '/api/team/listings/apply', {'review': plan['review'], 'skills': []})
        self.assertEqual(status, 409)
        self.assertIn('重新预览', result['error'])
        self.assertEqual(self.cards(), {})
        self.publish(('acme-local',))
        plan = self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-local', 'note': '核对后的推荐'}]})
        self.ok(self.a, '/api/team/listings/apply', {'review': plan['review'], 'skills': [{'name': 'acme-extra'}]})
        self.assertEqual(self.cards()['acme-local']['listings'][0]['note'], '核对后的推荐')
        self.assertEqual(self.request(self.a, '/api/team/listings/apply', {'review': plan['review']})[0], 409)

    def test_remove_preflights_all_dependencies_without_partial_changes(self):
        self.configure()
        self.publish(('acme-main', 'acme-local'))
        before = store.physical_state(self.hub.store.data)
        status, result = self.request(self.a, '/api/team/listings/remove', {'names': ['acme-base', 'acme-local']})
        self.assertEqual(status, 409)
        self.assertIn('acme-main', result['error'])
        self.assertEqual(store.physical_state(self.hub.store.data), before)
        self.ok(self.a, '/api/team/listings/remove', {'names': ['acme-base', 'acme-main', 'acme-local']})
        self.assertEqual(self.cards(), {})

    def test_remove_includes_entire_dependency_cycle(self):
        self.configure()
        self.registry(self.home, lambda skills: skills['acme-local'].update(requires=['acme-main']))
        self.registry(self.home, lambda skills: skills['acme-main'].update(requires=['acme-local']))
        self.publish(('acme-main',))
        self.ok(self.a, '/api/team/listings/remove', {'names': ['acme-main', 'acme-local']})
        self.assertEqual(self.cards(), {})

    def test_catalog_uses_latest_metadata_and_accepts_same_source_name_aliases(self):
        self.configure()
        first = listing('acme-other', title='较早的中文名', description='较早的说明')
        self.raw_put([first])
        second = listing('acme-alias', summary='较新的用途')
        second['source'] = first['source']
        self.raw_put([second])
        card = self.cards()['acme-alias']
        self.assertEqual(card['title'], '较早的中文名')
        self.assertEqual(card['summary'], '较新的用途')
        self.assertEqual(card['description'], '较早的说明')
        self.assertEqual(card['local']['status'], 'installed')
        self.assertEqual(len(card['listings']), 2)
        self.assertEqual(card['tokens'], operations.listing_tokens('acme-alias', {'description': '较早的说明'}))

    def test_ratings_require_installation_prevent_own_rating_and_aggregate(self):
        self.configure()
        self.publish(('acme-local',))
        key = self.cards()['acme-local']['key']
        status, result = self.request(self.b, '/api/team/ratings', {'key': key, 'stars': 5})
        self.assertEqual(status, 409)
        self.assertEqual(result['error'], '装过才能评价。')
        self.assertEqual(self.request(self.a, '/api/team/ratings', {'key': key, 'stars': 5})[1]['error'], '自己上架的不能评价。')
        self.install()
        for stars in (5, 4):
            self.ok(self.b, '/api/team/ratings', {'key': key, 'stars': stars, 'text': '<b>有用的示例</b>'})
        third = self.hub.store.add_member('Acme 示例成员 C')
        team._request({'url': self.url, 'key': third['token']}, '/api/ratings', 'PUT', {'key': key, 'stars': 5})
        card = self.cards()['acme-local']
        self.assertEqual(card['rating'], {'average': 4.5, 'count': 2})
        self.assertEqual(card['my_rating'], {'stars': 4, 'text': '<b>有用的示例</b>'})
        self.assertEqual(card['reviews'][0]['member']['id'], third['id'])
        self.ok(self.b, '/api/team/ratings/remove', {'key': key})
        self.assertIsNone(self.cards()['acme-local']['my_rating'])
        self.assertEqual(self.cards()['acme-local']['rating'], {'average': 5.0, 'count': 1})

    def test_team_updates_read_catalog_once_merge_local_changes_undo_and_author_removal(self):
        self.configure()
        self.publish(('acme-local',))
        installed = self.install()
        author = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        author.write_text(author.read_text() + '\n作者新版\n')
        self.publish(('acme-local',))
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'update')
        local = self.other / '.skill-pilot/store/acme-local/SKILL.md'
        local.write_text(local.read_text() + '\n保留的本机修改\n')
        with patch.object(team, '_catalog', wraps=team._catalog) as read:
            review = updates.check(self.other)
        self.addCleanup(lambda: shutil.rmtree(review['temporary'], ignore_errors=True))
        read.assert_called_once()
        row = next(r for r in review['skills'] if r['name'] == 'acme-local')
        self.assertTrue(row['has_update'])
        self.assertTrue(row['local_modified'])
        self.assertIn('作者新版', row['comparison'])
        merged = local.read_text() + '\n作者新版\n'
        with patch.object(research, 'merge', return_value={'files': [{'path': 'SKILL.md', 'content': merged, 'executable': False}],
                                                        'explanation': '保留两边的示例修改'}):
            merge = updates.prepare_merge(review, 'acme-local')
        result = updates.apply(review, ['acme-local'], choices={'acme-local': 'merge'}, confirmed=True,
                               merge_ids={'acme-local': merge['merge_id']})
        self.assertTrue(result['complete'])
        self.assertEqual(local.read_text(), merged)
        version = self.cards()['acme-local']['listings'][0]['version']
        self.assertEqual(operations.load(self.other)[1]['skills']['acme-local']['source']['team']['version'], version)
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'installed')
        operations.undo(result['id'], home=self.other)
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'update')
        # Undo protects edits made outside the dashboard. Restore the original
        # installed content before removing that installation.
        local.write_bytes((self.other / '.skill-pilot/bases/acme-local/SKILL.md').read_bytes())
        operations.undo(installed['id'], home=self.other)
        self.assertNotIn('acme-local', operations.load(self.other)[1]['skills'])
        self.install()
        self.ok(self.a, '/api/team/listings/remove', {'names': ['acme-local']})
        review = updates.check(self.other)
        self.addCleanup(lambda: shutil.rmtree(review['temporary'], ignore_errors=True))
        row = next(r for r in review['skills'] if r['name'] == 'acme-local')
        self.assertEqual(row['status'], '作者已下架')
        self.assertFalse(row['has_update'])
        self.assertNotIn('error', row)

    def test_updates_keep_choice_records_version_and_restores_local_version_when_undone(self):
        self.configure()
        self.publish(('acme-local',))
        self.install()
        author = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        author.write_text(author.read_text() + '\n作者新版\n')
        self.publish(('acme-local',))
        before = (self.other / '.skill-pilot/store/acme-local/SKILL.md').read_bytes()
        review = updates.check(self.other)
        self.addCleanup(lambda: shutil.rmtree(review['temporary'], ignore_errors=True))
        result = updates.apply(review, ['acme-local'], choices={'acme-local': 'keep'}, confirmed=True)
        self.assertEqual((self.other / '.skill-pilot/store/acme-local/SKILL.md').read_bytes(), before)
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'installed')
        operations.undo(result['id'], home=self.other)
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'update')

    def test_readonly_browses_previews_and_rejects_every_write(self):
        self.configure()
        self.publish(('acme-local',))
        installed = self.install(apply=False)
        uploaded = self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-local'}]})
        before = [store.physical_state(h) for h in (self.home, self.other, self.hub.store.data)]
        writes = [('/api/team/settings', {'url': self.url}),
                  ('/api/team/listings/apply', {'review': uploaded['review']}),
                  ('/api/team/listings/remove', {'names': ['acme-local']}),
                  ('/api/team/install/apply', {'review': installed['review']}),
                  ('/api/team/ratings', {'key': 'example', 'stars': 5}), ('/api/team/ratings/remove', {'key': 'example'})]
        for readonly in (patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}),
                         patch.object(store, 'write_allowed', return_value=False)):
            with readonly:
                self.ok(self.b, '/api/team')
                self.ok(self.b, '/api/team/catalog')
                self.ok(self.b, '/api/team/listings/candidates')
                self.install(apply=False)
                self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-local'}]})
                for path, body in writes:
                    self.assertEqual(self.request(self.b, path, body)[0], 403)
        self.assertEqual([store.physical_state(h) for h in (self.home, self.other, self.hub.store.data)], before)

    def test_all_routes_require_dashboard_identity_and_origin_legacy_routes_disappear(self):
        gets = ['/api/team', '/api/team/catalog', '/api/team/listings/candidates']
        posts = ['/api/team/settings', '/api/team/listings/preview', '/api/team/listings/apply', '/api/team/listings/remove',
                 '/api/team/install/preview', '/api/team/install/apply', '/api/team/ratings', '/api/team/ratings/remove']
        before = store.physical_state(self.home)
        with patch.object(team, '_request') as network:
            for path, body in [(p, None) for p in gets] + [(p, {}) for p in posts]:
                for headers in ({'X-Skill-Pilot-Token': 'wrong'}, {'Origin': 'https://example.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
                    self.assertEqual(self.request(self.a, path, body, headers)[0], 403)
            network.assert_not_called()
        self.assertEqual(store.physical_state(self.home), before)
        for path in ('/api/team/shares', '/api/team/shares/' + self.first['id']):
            self.assertEqual(self.request(self.a, path)[0], 404)
        for path in ('/api/team/upload/preview', '/api/team/upload/apply', '/api/team/withdraw', '/api/team/import/preview', '/api/team/import/apply'):
            self.assertEqual(self.request(self.a, path, {})[0], 404)

    def test_invalid_selections_and_review_handles_never_write_or_download(self):
        self.configure()
        self.publish(('acme-local',))
        before = [store.physical_state(h) for h in (self.home, self.other, self.hub.store.data)]
        key = self.cards()['acme-local']['key']
        with patch.object(github, 'inspect') as download:
            for skills in (None, [], 'acme-local', ['acme-local'], [{'name': 'missing'}], [{'name': []}],
                           [{'name': 'acme-local', 'note': 'bad\ntext'}], [{'name': 'acme-local'}, {'name': 'acme-local'}]):
                self.assertEqual(self.request(self.a, '/api/team/listings/preview', {'skills': skills})[0], 409)
            for agents in (None, [], 'claude', ['missing'], [[]]):
                self.assertEqual(self.request(self.b, '/api/team/install/preview', {'key': key, 'agents': agents})[0], 409)
            for review in (None, '', 'missing', []):
                for route in ('/api/team/listings/apply', '/api/team/install/apply'):
                    self.assertEqual(self.request(self.b, route, {'review': review})[0], 409)
            download.assert_not_called()
        self.assertEqual([store.physical_state(h) for h in (self.home, self.other, self.hub.store.data)], before)

    def test_settings_are_private_separate_and_checked_before_replacement(self):
        self.assertEqual(self.ok(self.a, '/api/team'), {'url': '', 'has_key': False, 'configured': False, 'member': None})
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
            self.assertEqual(set(public), {'url', 'has_key', 'configured', 'member'})
            self.assertEqual(public['member'], result['member'])
            self.assertNotIn(saved['key'], json.dumps([result, public]) + stderr.getvalue())
        self.assertEqual(operations.history(self.home), history)
        self.ok(self.b, '/api/team/settings', {'url': self.url, 'name': '另一示例成员', 'key': ''})
        self.assertEqual(len(self.hub.store.members()), 4)

        self.publish(('acme-local',))
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'installable')
        self.install()
        self.assertTrue(self.cards()['acme-local']['can_rate'])

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
        joined = {'service': 'skill-pilot-team', 'api': 2,
                  'member': {'id': 'a' * 32, 'name': '示例成员'}, 'token': 'example-joined-token'}
        invalid = [dict(joined, service='other'), dict(joined, api=1), dict(joined, api=True),
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
                return 200, {'service': 'skill-pilot-team', 'api': 2,
                             'member': {'id': 'a' * 32, 'name': '示例成员'}, 'token': new_key}, {}
            if headers.get('Authorization') != 'Bearer ' + new_key:
                return 403, {}, {}
            return 200, ({'service': 'skill-pilot-team', 'api': 2, 'member': {'id': 'a' * 32, 'name': new_key}}
                         if path == '/api/me' else {'cards': []}), {}
        service = self.faulty_service(respond)
        url = 'http://127.0.0.1:' + str(service.server_port)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.ok(self.a, '/api/team/settings', {'url': url, 'name': '示例成员'})
            self.assertEqual(result['member']['name'], '[已隐藏]')
            self.assertNotIn(new_key, json.dumps(result) + stderr.getvalue())
        self.assertEqual(requests, [('/api/me', 'Bearer ' + self.first['token']), ('/api/join', None),
                                    ('/api/me', 'Bearer ' + new_key)])
        self.assertEqual(team.settings(self.home), {'url': url, 'key': new_key})


    def test_joined_token_must_pass_status_before_replacing_settings(self):
        self.configure()
        before = store.physical_state(self.home)
        new_key = 'example-joined-token'
        requests = []
        def respond(path, headers):
            requests.append(path)
            if path == '/api/join':
                return 200, {'service': 'skill-pilot-team', 'api': 2,
                             'member': {'id': 'a' * 32, 'name': '示例成员'}, 'token': new_key}, {}
            return ((200, {'service': 'skill-pilot-team', 'api': 1}, {})
                    if headers.get('Authorization') == 'Bearer ' + new_key else (401, {}, {}))
        service = self.faulty_service(respond)
        status, result = self.request(self.a, '/api/team/settings', {
            'url': 'http://127.0.0.1:' + str(service.server_port), 'name': '示例成员'})
        self.assertEqual(status, 409)
        self.assertIn('版本不匹配', result['error'])
        self.assertNotIn(new_key, json.dumps(result))
        self.assertEqual(requests, ['/api/me', '/api/join', '/api/me'])
        self.assertEqual(store.physical_state(self.home), before)


    def test_status_keeps_address_and_has_key_offline_wrong_key_and_version_error(self):
        self.configure()
        path = self.home / '.skill-pilot/team.json'
        original = path.read_bytes()
        for key in ('wrong', self.first['token']):
            with socket.socket() as unused:
                unused.bind(('127.0.0.1', 0))
                url = self.url if key == 'wrong' else 'http://127.0.0.1:' + str(unused.getsockname()[1])
                path.write_text(json.dumps({'url': url, 'key': key}))
                result = self.ok(self.a, '/api/team')
                self.assertEqual(result['url'], url)
                self.assertTrue(result['has_key'])
                self.assertTrue(result['configured'])
                self.assertIsNone(result['member'])
                self.assertIn('error', result)
        path.write_text('{broken')
        self.assertIn('设置没能读懂', self.ok(self.a, '/api/team')['error'])
        path.write_bytes(original)
        self.assertNotIn('error', self.ok(self.a, '/api/team'))

    def test_invalid_addresses_and_keys_do_not_connect(self):
        before = store.physical_state(self.home)
        addresses = ['ftp://example.invalid', 'file:///tmp/example', 'http://a:b@example.invalid', 'http://@example.invalid',
                     'http://example.invalid?key=secret', 'http://example.invalid?', 'http://example.invalid#fragment',
                     'http://example.invalid:bad', 'http://example.invalid:65536', 'http://example.invalid:0', 'http://exa\nmple.invalid']
        with patch.object(team, '_request') as network:
            for url in addresses:
                self.assertEqual(self.request(self.a, '/api/team/settings', {'url': url, 'key': self.first['token']})[0], 409)
            for key in (None, [], 'invalid\nkey', 'not a token', '非口令'):
                self.assertEqual(self.request(self.a, '/api/team/settings', {'url': self.url, 'key': key})[0], 409)
            network.assert_not_called()
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
        me = {'service': 'skill-pilot-team', 'api': 2, 'member': {'id': self.first['id'], 'name': '示例成员'}}
        responses = [(401, {'error': self.first['token']}, {}, '口令不对'),
                     (200, dict(me, api=1), {}, '版本不匹配'), (200, dict(me, api=True), {}, '版本不匹配'),
                     (200, b'invalid json', {}, '技能格式不对'), (200, b'\xff', {}, '技能格式不对'),
                     (200, b'x' * (team.MAX_BODY + 1), {}, '超过 20 MB')]
        for status, body, headers, message in responses:
            service = self.faulty_service(lambda path, request_headers, s=status, b=body, h=headers: (s, b, h))
            with self.subTest(message=message), contextlib.redirect_stderr(io.StringIO()) as stderr:
                code, result = self.request(self.a, '/api/team/settings', {
                    'url': 'http://127.0.0.1:' + str(service.server_port), 'key': self.first['token']})
                self.assertEqual(code, 409, result)
                self.assertIn(message, result['error'])
                self.assertNotIn(self.first['token'], json.dumps(result) + stderr.getvalue())
            self.assertEqual(store.physical_state(self.home), before)

    def test_timeouts_incomplete_responses_redirect_and_echo_cannot_leak_credentials(self):
        self.configure()
        before = store.physical_state(self.home)
        for error in (TimeoutError('example timeout'), IncompleteRead(b'example', 100)):
            with patch.object(urllib.request.OpenerDirector, 'open', side_effect=error) as opened:
                with self.assertRaisesRegex(ValueError, '连不上分享站'):
                    team.status(self.home)
                self.assertEqual(opened.call_args.kwargs['timeout'], 10)
        hits = []
        target = self.faulty_service(lambda path, headers: (hits.append(dict(headers)) or 200, {}, {}))
        redirect = self.faulty_service(lambda path, headers: (302, {}, {'Location': 'http://127.0.0.1:' + str(target.server_port) + '/api/me'}))
        self.assertIn('跳转', self.request(self.a, '/api/team/settings', {
            'url': 'http://127.0.0.1:' + str(redirect.server_port), 'key': self.first['token']})[1]['error'])
        self.assertEqual(hits, [])
        self.assertEqual(store.physical_state(self.home), before)
        echo = self.faulty_service(lambda path, headers: (200, {'service': 'skill-pilot-team', 'api': 2,
                     'member': {'id': self.first['id'], 'name': self.first['token']}}, {}))
        result = self.ok(self.a, '/api/team/settings', {'url': 'http://127.0.0.1:' + str(echo.server_port), 'key': self.first['token']})
        self.assertEqual(result['member']['name'], '[已隐藏]')
        # Neither the HTTP responses nor operation records expose a credential.
        self.assertNotIn(self.first['token'], json.dumps(self.ok(self.a, '/api/log')))

    def test_invalid_remote_catalog_and_package_are_validated_before_install(self):
        self.configure()
        self.publish(('acme-local',))
        real = team._request
        def request(config, path, *args):
            data = real(config, path, *args)
            if path == '/api/catalog':
                data['cards'][0]['listings'][0]['source']['version'] = '../invalid'
            return data
        before = self.library(self.other)
        with patch.object(team, '_request', side_effect=request):
            self.assertEqual(self.request(self.b, '/api/team/catalog')[0], 409)
        def request(config, path, *args):
            data = real(config, path, *args)
            if path.startswith('/api/packages/'):
                data['files'][0]['path'] = '../outside'
            return data
        key = self.cards()['acme-local']['key']
        with patch.object(team, '_request', side_effect=request):
            self.assertEqual(self.request(self.b, '/api/team/install/preview', {'key': key, 'agents': ['claude']})[0], 409)
        self.assertEqual(self.library(self.other), before)

    def test_fake_home_skips_team_settings_and_saving_cannot_follow_outside_link(self):
        self.configure()
        (self.home / '.skill-pilot/key-link').symlink_to(self.home / '.skill-pilot/team.json')
        spec = importlib.util.spec_from_file_location('fake_home_team', store.PROJECT / 'scripts/make-fake-home.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        destination = self.root / 'Another Example Home'
        module.make_fake_home(self.home, destination)
        self.assertFalse((destination / '.skill-pilot/team.json').exists())
        self.assertFalse((destination / '.skill-pilot/key-link').exists())
        outside = self.root / 'Outside settings.json'
        outside.write_text('example private settings')
        path = self.home / '.skill-pilot/team.json'
        path.unlink()
        path.symlink_to(outside)
        before = store.physical_state(self.root)
        status, result = self.request(self.a, '/api/team/settings', {'url': self.url, 'key': self.first['token']})
        self.assertEqual(status, 409)
        self.assertIn('之外', result['error'])
        self.assertEqual(store.physical_state(self.root), before)


if __name__ == '__main__':
    unittest.main()
