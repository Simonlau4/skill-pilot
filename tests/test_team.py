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
import time
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
from skill_pilot.web import runtime
from skill_pilot.web.server import Dashboard
from skill_pilot.sources.update_github import Reader
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
        self.skill(self.repository / 'skills/acme-main/examples', 'nested-example')
        manifest = self.repository / 'skills/acme-main/SKILL.md'
        manifest.write_text(manifest.read_text() + '\nSee examples/SKILL.md\n')
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
                                 'commit': self.commit,
                                 'tree_sha': github.git(self.repository, 'rev-parse', self.commit + ':skills/' + name)}
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

    def enqueue(self, name='acme-local', server=None):
        server = server or self.b
        card = self.cards(server)[name]
        return self.ok(server, '/api/team/queue', {'key': card['key']})['items'][-1]

    def wait_queue(self, server=None):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            items = self.ok(server or self.b, '/api/team/queue')['items']
            if not any(item['status'] in {'waiting', 'running'} for item in items):
                return items
            time.sleep(.01)
        self.fail('安装队列没有完成')

    def install(self, name='acme-local'):
        queued = self.enqueue(name)
        item = next(item for item in self.wait_queue() if item['id'] == queued['id'])
        self.assertEqual(item['status'], 'done', item)
        return item

    def enable(self, name='acme-local'):
        return operations.apply_assign([{'skill': name, 'agent': 'claude', 'enabled': True}], home=self.other)

    def raw_put(self, rows, member=None):
        return team._request({'url': self.url, 'key': (member or self.first)['token']}, '/api/listings/me', 'PUT', {'listings': rows})

    @contextlib.contextmanager
    def local_github(self):
        parse = github.parse_url
        def local(url):
            _, repository, suffix = parse(url)
            return self.repository.as_uri(), repository, suffix
        with patch.object(github, 'parse_url', side_effect=local), patch.object(
                github, 'api', side_effect=AssertionError('fallback must not require the GitHub CLI')):
            yield

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
        self.registry(self.home, lambda skills: skills['acme-local'].update(requires=['acme-base']))
        published = self.publish(notes={'acme-local': '<b>示例推荐</b>'})
        rows = {r['name']: r for r in published['listings']}
        self.assertEqual(rows['acme-base']['auto'], ['acme-local', 'acme-main'])
        self.assertEqual(rows['acme-local']['files']['blocked'][0]['path'], '.env')
        self.assertEqual(rows['acme-local']['files']['count'], 3)
        self.assertNotIn('content', json.dumps(published))
        self.assertEqual(self.cards(self.a)['acme-local']['local']['status'], 'mine')
        card = self.cards()['acme-local']
        self.assertEqual(card['local']['status'], 'installable')
        self.assertFalse(card['can_rate'])
        self.assertEqual(card['listings'][0]['note'], '<b>示例推荐</b>')
        before = self.library(self.other)
        with patch.object(github, 'inspect', side_effect=AssertionError('installation must use the hub')):
            installed = self.install()
        self.assertEqual(installed['message'], '从团队商店装上了「示例技能」')
        self.assertEqual(installed['title'], '示例技能')
        self.assertEqual(installed['installed'], ['acme-base', 'acme-local'])
        skills = operations.load(self.other)[1]['skills']
        skill = skills['acme-local']
        for name in installed['installed']:
            self.assertEqual(skills[name]['assigned_agents'], [])
            self.assertEqual(skills[name]['entries'], [])
        self.assertEqual(set(skill['source']), {'source', 'team'})
        self.assertEqual(normalize(skill['source'])['label'], '团队商店 · 示例成员 A')
        destination = self.other / '.skill-pilot/store/acme-local'
        self.assertFalse((destination / '.env').exists())
        self.assertEqual((destination / 'example.bin').read_bytes(), b'\x00\xff\x01')
        self.assertTrue((destination / 'example.sh').stat().st_mode & 0o111)
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'installed')
        self.assertEqual(self.cards()['acme-local']['local']['agents'], [])
        self.assertTrue(self.cards()['acme-local']['can_rate'])
        record = next(r for r in operations.history(self.other) if r['id'] == installed['operation'])
        self.assertEqual(record['agents'], [])
        with tarfile.open(self.other / '.skill-pilot/backups' / record['backup_id'] / 'files.tar') as archive:
            self.assertNotIn('.skill-pilot/team.json', archive.getnames())
        self.ok(self.b, '/api/undo', {'id': installed['operation'], 'confirmed': True, 'revision': operations.table(self.other)['revision']})
        self.assertEqual(self.library(self.other), before)
        with patch.object(github, 'inspect', side_effect=AssertionError('installation must use the hub')):
            installed = self.install('acme-main')
        self.assertEqual(installed['installed'], ['acme-base', 'acme-main'])
        skills = operations.load(self.other)[1]['skills']
        self.assertEqual(skills['acme-main']['requires'], ['acme-base'])
        self.assertEqual(skills['acme-base']['assigned_agents'], [])
        self.assertEqual(normalize(skills['acme-main']['source'])['kind'], 'github')
        self.ok(self.b, '/api/undo', {'id': installed['operation'], 'confirmed': True, 'revision': operations.table(self.other)['revision']})
        self.assertEqual(self.library(self.other), before)

    def test_queue_reads_the_current_listing_when_work_starts(self):
        self.configure()
        self.publish(('acme-local',))
        entered, release = threading.Event(), threading.Event()
        install = team.install
        def delayed(home, key):
            entered.set()
            self.assertTrue(release.wait(5))
            return install(home, key)
        with patch.object(team, 'install', side_effect=delayed):
            self.enqueue()
            try:
                self.assertTrue(entered.wait(2))
                path = self.home / '.skill-pilot/store/acme-local/SKILL.md'
                path.write_text(path.read_text() + '\nnew version\n')
                self.publish(('acme-local',))
            finally:
                release.set()
            item = self.wait_queue()[0]
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual((self.other / '.skill-pilot/store/acme-local/SKILL.md').read_bytes(), path.read_bytes())

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
            self.enqueue('needs-conflict')
            item = self.wait_queue()[0]
            self.assertEqual(item['status'], 'failed')
            self.assertIn('作者没有上架', item['message'])
            self.assertNotIn('operation', item)

    def test_existing_dependencies_are_reused_without_assignment_or_replacement(self):
        self.configure()
        self.registry(self.home, lambda skills: skills['acme-local'].update(requires=['acme-other']))
        self.publish(('acme-local',))
        self.registry(self.other, lambda skills: skills['acme-other'].update(source={'source': '自制'}))
        before = (self.other / '.skill-pilot/store/acme-other/SKILL.md').read_bytes()
        item = self.install()
        self.assertEqual(item['reuse'], ['acme-other'])
        self.assertEqual(item['installed'], ['acme-local'])
        self.assertEqual((self.other / '.skill-pilot/store/acme-other/SKILL.md').read_bytes(), before)
        skills = operations.load(self.other)[1]['skills']
        self.assertEqual(skills['acme-local']['requires'], ['acme-other'])
        self.assertEqual(skills['acme-local']['assigned_agents'], [])
        self.assertEqual(skills['acme-other']['assigned_agents'], ['claude'])

    def test_install_reloads_the_registry_in_the_lock_and_cleans_downloads(self):
        self.configure()
        self.publish(('acme-local',))
        package = team.package
        temporary = []
        def download(*args):
            path = package(*args)
            temporary.append(path.parent)
            operations.apply_assign([{'skill': 'acme-other', 'agent': 'claude', 'enabled': False}], home=self.other)
            return path
        with patch.object(team, 'package', side_effect=download):
            self.install()
        skills = operations.load(self.other)[1]['skills']
        self.assertEqual(skills['acme-other']['assigned_agents'], [])
        self.assertEqual(skills['acme-local']['assigned_agents'], [])
        self.assertTrue(temporary)
        self.assertTrue(all(not path.exists() for path in temporary))

    def test_queue_order_duplicates_failure_retry_clear_and_busy(self):
        self.configure()
        self.publish(('acme-local', 'acme-main', 'acme-extra'))
        entered, release = threading.Event(), threading.Event()
        install, calls = team.install, []
        def delayed(home, key):
            calls.append(key)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(8))
            return install(home, key)
        with patch.object(team, 'install', side_effect=delayed):
            first = self.enqueue()
            try:
                self.assertTrue(entered.wait(2))
                second = self.enqueue('acme-main')
                third = self.enqueue('acme-extra')
                with patch.object(team, '_fetch', side_effect=AssertionError('duplicates must not fetch')):
                    for queued in (first, second):
                        items = self.ok(self.b, '/api/team/queue', {'key': queued['key']})['items']
                        self.assertEqual(next(i['id'] for i in items if i['key'] == queued['key']), queued['id'])
                self.assertEqual([i['status'] for i in items], ['running', 'waiting', 'waiting'])
                self.assertEqual(len(self.ok(self.b, '/api/team/queue/clear', {})['items']), 3)
                self.assertTrue(self.b.background_busy())
                self.ok(self.a, '/api/team/listings/remove', {'names': ['acme-main']})
            finally:
                release.set()
            items = self.wait_queue()
        self.assertEqual(calls, [i['key'] for i in (first, second, third)])
        self.assertEqual([i['status'] for i in items], ['done', 'failed', 'done'])
        self.assertEqual(items[1]['message'], '这个技能已经下架。')
        self.assertNotIn('operation', items[1])
        self.assertFalse(self.b.background_busy())
        retried = self.ok(self.b, '/api/team/queue', {'key': second['key']})['items'][-1]
        self.assertNotEqual(retried['id'], second['id'])
        self.assertEqual(self.wait_queue()[-1]['status'], 'failed')
        self.assertEqual(self.ok(self.b, '/api/team/queue/clear', {}), {'items': []})
        self.publish(('acme-main',))
        retried = self.install('acme-main')
        self.assertNotEqual(retried['id'], second['id'])
        self.assertEqual([i['name'] for i in self.wait_queue()], ['acme-main'])
        before = operations.history(self.other)
        again = self.install('acme-main')
        self.assertNotEqual(again['id'], retried['id'])
        self.assertEqual(again['message'], '本机已经有了。')
        self.assertEqual(again['installed'], [])
        self.assertNotIn('operation', again)
        self.assertEqual(operations.history(self.other), before)
        self.assertEqual(self.ok(self.b, '/api/team/queue/clear', {}), {'items': []})

    def test_queue_does_not_replace_an_installed_skill_with_an_author_update(self):
        self.configure()
        self.publish(('acme-local',))
        self.install()
        local = self.other / '.skill-pilot/store/acme-local/SKILL.md'
        before = local.read_bytes()
        author = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        author.write_text(author.read_text() + '\n新版\n')
        self.publish(('acme-local',))
        self.assertEqual(self.cards()['acme-local']['local']['status'], 'update')
        history = operations.history(self.other)
        item = self.install()
        self.assertEqual(item['message'], '本机已经有了。')
        self.assertNotIn('operation', item)
        self.assertEqual(local.read_bytes(), before)
        self.assertEqual(operations.history(self.other), history)

    def test_refresh_waits_for_the_queue(self):
        self.configure()
        self.publish(('acme-local',))
        entered, release, replaced = threading.Event(), threading.Event(), threading.Event()
        install = team.install
        def delayed(home, key):
            entered.set()
            self.assertTrue(release.wait(5))
            return install(home, key)
        self.b.reload_callback = replaced.set
        with patch.object(team, 'install', side_effect=delayed), patch.object(runtime, 'version', return_value='changed'):
            self.enqueue()
            try:
                self.assertTrue(entered.wait(2))
                result = self.ok(self.b, '/api/runtime/refresh', {})
                self.assertTrue(result['busy'])
                self.assertFalse(replaced.wait(.05))
            finally:
                release.set()
            self.assertEqual(self.wait_queue()[0]['status'], 'done')
            self.assertTrue(replaced.wait(2))

    def test_server_close_waits_for_its_queue_thread(self):
        self.configure()
        self.publish(('acme-local',))
        dashboard = Dashboard({'home': str(self.other)})
        self.addCleanup(dashboard.server_close)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        install = team.install
        def delayed(home, key):
            entered.set()
            self.assertTrue(release.wait(5))
            return install(home, key)
        def close():
            dashboard.server_close()
            closed.set()
        with patch.object(team, 'install', side_effect=delayed):
            dashboard.queue_team(self.cards()['acme-local']['key'])
            try:
                self.assertTrue(entered.wait(2))
                thread = threading.Thread(target=close)
                thread.start()
                self.assertFalse(closed.wait(.05))
            finally:
                release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(closed.is_set())
        self.assertEqual(dashboard.team_queue_status()['items'][0]['status'], 'done')

    def test_server_close_does_not_start_waiting_items(self):
        self.configure()
        self.publish(('acme-local', 'acme-main', 'acme-extra'))
        legacy = listing('acme-main')
        legacy['source']['commit'] = self.commit
        self.raw_put([legacy])  # A queued legacy listing would require GitHub.
        cards = self.cards()
        dashboard = Dashboard({'home': str(self.other)})
        self.addCleanup(dashboard.server_close)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        install = team.install
        def delayed(home, key):
            entered.set()
            self.assertTrue(release.wait(5))
            return install(home, key)
        def close():
            dashboard.server_close()
            closed.set()
        with patch.object(team, 'install', side_effect=delayed) as work, patch.object(
                github, 'inspect', side_effect=AssertionError('closing must not start a GitHub download')) as download:
            dashboard.queue_team(cards['acme-local']['key'])
            try:
                self.assertTrue(entered.wait(2))
                for name in ('acme-main', 'acme-extra'):
                    dashboard.queue_team(cards[name]['key'])
                thread = threading.Thread(target=close)
                thread.start()
                deadline = time.monotonic() + 2
                while not dashboard.closing and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(dashboard.closing)
                self.assertFalse(closed.wait(.05))
            finally:
                release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(closed.is_set())
            self.assertEqual(work.call_count, 1)
            download.assert_not_called()
        self.assertEqual([i['status'] for i in dashboard.team_queue_status()['items']], ['done', 'waiting', 'waiting'])
        skills = operations.load(self.other)[1]['skills']
        self.assertIn('acme-local', skills)
        self.assertNotIn('acme-main', skills)
        self.assertNotIn('acme-extra', skills)

    def test_packaged_github_updates_preserve_the_raw_and_normalized_baselines(self):
        self.configure()
        self.publish(('acme-main',))
        row = next(r for c in team._catalog(team._connection(self.home)) for r in c['listings'] if r['name'] == 'acme-main')
        self.assertIn('version', row['source'])
        self.assertEqual(row['source']['tree_sha'], github.git(self.repository, 'rev-parse', self.commit + ':skills/acme-main'))
        with patch.object(github, 'inspect', side_effect=AssertionError('must not contact GitHub')), \
                patch.object(github, 'api', side_effect=AssertionError('must not contact GitHub')):
            self.install('acme-main')
        path = self.other / '.skill-pilot/store/acme-main'
        self.assertFalse((path / 'examples/SKILL.md').exists())
        self.assertTrue((path / 'examples/SKILL.example.md').is_file())
        self.assertIn('examples/SKILL.example.md', (path / 'SKILL.md').read_text())
        skills = operations.load(self.other)[1]['skills']
        for name in ('acme-main', 'acme-base'):
            self.assertEqual(skills[name]['assigned_agents'], [])
            self.assertEqual(normalize(skills[name]['source'])['kind'], 'github')
            self.assertNotIn('team', skills[name]['source'])
        self.assertNotEqual(skills['acme-main']['base_tree_sha'], skills['acme-main']['base_normalized_tree_sha'])
        self.enable('acme-main')
        latest, current = Reader.latest, Reader.current
        def local_latest(reader, source):
            return latest(reader, {**source, 'repository': self.repository.as_uri()})
        def local_current(reader, source, *args):
            return current(reader, {**source, 'repository': self.repository.as_uri()}, *args)
        def check():
            review = updates.check(self.other)
            self.addCleanup(shutil.rmtree, review['temporary'], True)
            self.assertFalse(review['errors'], review['errors'])
            row = next(r for r in review['skills'] if r['name'] == 'acme-main')
            self.assertFalse(row['local_modified'])
            self.assertFalse(row['baseline_uncertain'])
            return row
        with patch.object(Reader, 'latest', local_latest), patch.object(Reader, 'current', local_current), \
                patch.object(github, 'api', side_effect=AssertionError('updates must use the local repository')):
            row = check()
            self.assertEqual(row['status'], '已是最新')
            self.assertFalse(row['has_update'])
            author = self.repository / 'skills/acme-main/SKILL.md'
            author.write_text(author.read_text() + '\nupstream version two\n')
            github.git(self.repository, 'add', '.')
            github.git(self.repository, '-c', 'user.name=Example', '-c', 'user.email=example@local.invalid',
                       '-c', 'commit.gpgsign=false', 'commit', '-qm', 'upstream version two')
            row = check()
            self.assertEqual(row['status'], '有更新')
            self.assertTrue(row['has_update'])
            self.assertEqual(row['your_changes']['file_count'], 0)

    def test_legacy_github_fallback_scopes_the_folder_even_with_duplicate_names(self):
        self.configure()
        duplicate = self.repository / 'another location'
        self.skill(duplicate, 'acme-main')
        (duplicate / 'SKILL.md').write_text((duplicate / 'SKILL.md').read_text() + '\nwrong copy\n')
        github.git(self.repository, 'add', '.')
        github.git(self.repository, '-c', 'user.name=Example', '-c', 'user.email=example@local.invalid',
                   '-c', 'commit.gpgsign=false', 'commit', '-qm', 'duplicate name')
        self.commit = github.git(self.repository, 'rev-parse', 'HEAD')
        self.raw_put([listing('acme-main')])  # Old listings may also omit the commit.
        inspected, temporary = github.inspect, []
        def inspect(url):
            result = inspected(url)
            temporary.append(Path(result['temporary']))
            self.assertEqual([s['name'] for s in result['skills']], ['acme-main'])
            return result
        with self.local_github(), patch.object(github, 'inspect', side_effect=inspect) as download:
            installed = self.install('acme-main')
        download.assert_called_once_with('https://github.com/Acme/skills/tree/' + self.commit + '/skills/acme-main')
        self.assertEqual(installed['installed'], ['acme-main'])
        self.assertNotIn('wrong copy', (self.other / '.skill-pilot/store/acme-main/SKILL.md').read_text())
        self.assertTrue(all(not path.exists() for path in temporary))

    def test_legacy_github_root_folder_omits_the_folder_suffix(self):
        self.configure()
        self.skill(self.repository / 'root staging', 'root-example')
        shutil.move(self.repository / 'root staging/SKILL.md', self.repository / 'SKILL.md')
        (self.repository / 'root staging').rmdir()
        github.git(self.repository, 'add', '.')
        github.git(self.repository, '-c', 'user.name=Example', '-c', 'user.email=example@local.invalid',
                   '-c', 'commit.gpgsign=false', 'commit', '-qm', 'root skill')
        self.commit = github.git(self.repository, 'rev-parse', 'HEAD')
        row = listing('root-example')
        row['source'].update(path='.', commit=self.commit)
        self.raw_put([row])
        with self.local_github(), patch.object(github, 'inspect', wraps=github.inspect) as download:
            self.install('root-example')
        download.assert_called_once_with('https://github.com/Acme/skills/tree/' + self.commit)

    def test_github_missing_package_is_changed_until_republished(self):
        self.configure()
        self.raw_put([listing('acme-main')])
        def changed():
            return next(r['changed'] for r in self.ok(self.a, '/api/team/listings/candidates')['skills'] if r['name'] == 'acme-main')
        self.assertTrue(changed())
        key = self.cards()['acme-main']['key']
        self.publish(('acme-main',))
        self.assertFalse(changed())
        self.assertEqual(self.cards()['acme-main']['key'], key)

    def test_github_incomplete_files_do_not_offer_republishing(self):
        self.configure()
        def candidate():
            return next(r for r in self.ok(self.a, '/api/team/listings/candidates')['skills'] if r['name'] == 'acme-main')
        for error in (OSError('incomplete read'), ValueError('SKILL.md 疑似含有密钥。')):
            with self.subTest(error=type(error).__name__), patch.object(team, '_files', side_effect=error):
                self.publish(('acme-main',))
                self.assertEqual(candidate()['kind'], 'github')
                self.assertFalse(candidate()['changed'])
        self.assertTrue(candidate()['changed'])
        self.publish(('acme-main',))
        self.assertFalse(candidate()['changed'])
        for folder in ('store', 'bases'):
            (self.home / '.skill-pilot' / folder / 'acme-main/README.md').write_text('new upstream file\n')
        self.assertEqual(candidate()['kind'], 'github')
        self.assertTrue(candidate()['changed'])

    def test_github_secret_and_size_count_limits_publish_without_a_package(self):
        self.configure()
        root = self.home / '.skill-pilot/store/acme-main'
        base = self.home / '.skill-pilot/bases/acme-main'
        for scenario in ('secret', 'size', 'count'):
            if scenario == 'secret':
                for folder in (root, base, self.repository / 'skills/acme-main'):
                    (folder / '.env').write_text('example private value')
                github.git(self.repository, 'add', '.')
                github.git(self.repository, '-c', 'user.name=Example', '-c', 'user.email=example@local.invalid',
                           '-c', 'commit.gpgsign=false', 'commit', '-qm', 'secret file')
                self.commit = github.git(self.repository, 'rev-parse', 'HEAD')
                self.registry(self.home, lambda skills: skills['acme-main']['source'].update(commit=self.commit))
            elif scenario == 'size':
                for folder in (root, base):
                    (folder / '.env').unlink()
                    (folder / 'big.bin').write_bytes(b'\x00' * team_hub.MAX_SIZE)
            else:
                for folder in (root, base):
                    (folder / 'big.bin').unlink()
                    for i in range(300):
                        (folder / str(i)).touch()
            with self.subTest(scenario=scenario):
                preview = self.publish(('acme-main',))
                row = next(r for r in preview['listings'] if r['name'] == 'acme-main')
                self.assertEqual(row['kind'], 'github')
                self.assertIsNone(row['version'])
                self.assertEqual(row['download_note'], '同事装的时候从 GitHub 下载。')
                shared = next(r for c in team._catalog(team._connection(self.home)) for r in c['listings'] if r['name'] == 'acme-main')
                self.assertNotIn('version', shared['source'])
                self.assertNotIn('files', shared['source'])
                candidate = next(r for r in self.ok(self.a, '/api/team/listings/candidates')['skills'] if r['name'] == 'acme-main')
                self.assertEqual(candidate['kind'], 'github')
                self.assertFalse(candidate['changed'])
                if scenario == 'secret':
                    self.assertEqual(row['files']['blocked'][0]['path'], '.env')
                    with self.local_github(), patch.object(github, 'inspect', wraps=github.inspect) as download:
                        self.install('acme-main')
                    download.assert_called_once()

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
        self.enable()
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
        enabled = self.enable()
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
        operations.undo(enabled['id'], home=self.other)
        operations.undo(installed['operation'], home=self.other)
        self.assertNotIn('acme-local', operations.load(self.other)[1]['skills'])
        self.install()
        self.enable()
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
        self.enable()
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
        key = self.cards()['acme-local']['key']
        uploaded = self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-local'}]})
        before = [store.physical_state(h) for h in (self.home, self.other, self.hub.store.data)]
        writes = [('/api/team/settings', {'url': self.url}),
                  ('/api/team/listings/apply', {'review': uploaded['review']}),
                  ('/api/team/listings/remove', {'names': ['acme-local']}),
                  ('/api/team/queue', {'key': key}), ('/api/team/queue/clear', {}),
                  ('/api/team/ratings', {'key': 'example', 'stars': 5}), ('/api/team/ratings/remove', {'key': 'example'})]
        for readonly in (patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}),
                         patch.object(store, 'write_allowed', return_value=False)):
            with readonly:
                self.ok(self.b, '/api/team')
                self.ok(self.b, '/api/team/catalog')
                self.ok(self.b, '/api/team/listings/candidates')
                self.ok(self.b, '/api/team/queue')
                self.ok(self.a, '/api/team/listings/preview', {'skills': [{'name': 'acme-local'}]})
                for path, body in writes:
                    self.assertEqual(self.request(self.b, path, body)[0], 403)
        self.assertEqual([store.physical_state(h) for h in (self.home, self.other, self.hub.store.data)], before)

    def test_all_routes_require_dashboard_identity_and_origin_legacy_routes_disappear(self):
        gets = ['/api/team', '/api/team/catalog', '/api/team/listings/candidates', '/api/team/queue']
        posts = ['/api/team/settings', '/api/team/listings/preview', '/api/team/listings/apply', '/api/team/listings/remove',
                 '/api/team/queue', '/api/team/queue/clear', '/api/team/ratings', '/api/team/ratings/remove']
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
        for path in ('/api/team/install/preview', '/api/team/install/apply'):
            self.assertEqual(self.request(self.a, path, {})[0], 405)

    def test_invalid_selections_and_review_handles_never_write_or_download(self):
        self.configure()
        self.publish(('acme-local',))
        before = [store.physical_state(h) for h in (self.home, self.other, self.hub.store.data)]
        key = self.cards()['acme-local']['key']
        with patch.object(github, 'inspect') as download:
            for skills in (None, [], 'acme-local', ['acme-local'], [{'name': 'missing'}], [{'name': []}],
                           [{'name': 'acme-local', 'note': 'bad\ntext'}], [{'name': 'acme-local'}, {'name': 'acme-local'}]):
                self.assertEqual(self.request(self.a, '/api/team/listings/preview', {'skills': skills})[0], 409)
            for invalid in (None, '', [], {}, 1):
                self.assertEqual(self.request(self.b, '/api/team/queue', {'key': invalid})[0], 409)
            status, result = self.request(self.b, '/api/team/queue', {'key': 'missing'})
            self.assertEqual(status, 409)
            self.assertEqual(result['error'], '这个技能已经下架。')
            for review in (None, '', 'missing', []):
                self.assertEqual(self.request(self.b, '/api/team/listings/apply', {'review': review})[0], 409)
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
            self.ok(self.b, '/api/team/queue', {'key': key})
            item = self.wait_queue()[0]
            self.assertEqual(item['status'], 'failed')
            self.assertNotIn('operation', item)
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
