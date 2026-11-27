"""Single-skill revisions through the authenticated hub, local library and CLI."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from skill_pilot import cli, operations, store, team, team_hub as hub, updates
from tests import set_up_home
from tests.test_team_hub import file, listing


def document(name='example', description='原说明'):
    return f'---\nname: {name}\ndescription: {description}\n---\n正文\n'


class CoCreationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='skill pilot collaboration ')
        self.addCleanup(tmp.cleanup)
        set_up_home(self, Path(tmp.name) / 'Author home')
        self.server = hub.HubServer(Path(tmp.name) / 'Shared data', '127.0.0.1', 0)
        self.db = self.server.store
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.b = self.home.parent / 'Contributor home'
        self.c = self.home.parent / 'Reader home'
        self.members = {}
        for home, label in ((self.home, '原作者'), (self.b, '提交人'), (self.c, '同事')):
            (home / '.claude/skills').mkdir(parents=True)
            plan = store.preview(home, ['claude'])
            store.apply(plan['plan_id'], home, ['claude'])
            member = self.db.add_member(label)
            self.members[home] = member['id']
            team.save_settings(home, {'url': f'http://127.0.0.1:{self.server.server_port}', 'key': member['token']})
        self.key = 'team:' + self.members[self.home] + ':example'
        self.original = listing('example', 'files', title='示例工具', note='保留推荐',
                                replaces=['older'], replacement_note='替换旧版', summary='简介',
                                group='示例', description='原说明', requires=['helper'])
        self.original['source']['files'] = [file('SKILL.md', document()), file('old.txt', '旧文件'),
                                            file('keep.txt', '保留')]
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [
            listing('helper', 'files', source={'kind': 'files', 'files': [file('SKILL.md', document('helper'))]}),
            self.original], 'packs': [{'id': 'tools', 'title': '组合', 'skills': ['example', 'helper']}]})
        team.install(self.b, self.key)
        self.base = self.card(self.b)['listings'][0]['version']

    def request(self, home, path, method='GET', data=None):
        return team._request(team.settings(home), path, method, data)

    def card(self, home):
        return next(c for c in team.catalog(home)['cards'] if c['key'] == self.key)

    def edit(self, home):
        root = home / '.skill-pilot/store/example'
        (root / 'SKILL.md').write_text(document(description='新版说明'), encoding='utf-8')
        (root / 'old.txt').unlink()
        (root / 'scripts').mkdir()
        (root / 'scripts/example.py').write_text('# example\n', encoding='utf-8')
        (root / '.env').write_text('sensitive example', encoding='utf-8')
        return root

    def propose(self, home=None):
        home = home or self.b
        self.edit(home)
        saved, _ = team.suggestion_preview(home, self.key)
        return team.suggestion_submit(home, saved, '请审核\n示例')['id']

    def error(self, status, call, *args, **kwargs):
        with self.assertRaises(team.RequestError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.status, status, str(caught.exception))
        return caught.exception.body

    def files_for(self, home):
        return {p.name for p in self.db._blob_dir(self.members[home]).glob('*')}

    def test_submit_review_publish_and_existing_update_flow(self):
        team.install(self.home, self.key)
        operations.apply_assign([{'skill': 'example', 'agent': 'claude', 'enabled': True}], home=self.home)
        team.install(self.c, self.key)
        operations.apply_assign([{'skill': 'example', 'agent': 'claude', 'enabled': True}], home=self.c)
        self.assertTrue(self.card(self.b)['can_suggest'])
        self.assertFalse(self.card(self.home)['can_suggest'])
        before = self.db._listings(self.members[self.home])['example']
        packs = self.db._packs(self.members[self.home])
        self.edit(self.b)
        saved, preview = team.suggestion_preview(self.b, '示例工具')
        self.assertEqual(preview['base_revision'], 1)
        self.assertEqual([f['path'] for f in preview['blocked']], ['.env'])
        self.assertEqual(preview['summary'], {'added': ['scripts/example.py'], 'modified': ['SKILL.md'],
                         'removed': ['old.txt'], 'unchanged_count': 1, 'has_scripts': True})
        self.assertNotIn(str(self.home.parent), json.dumps(preview))
        created = team.suggestion_submit(self.b, saved, ' 请审核\n第二行 ')
        identity = created['id']
        self.assertEqual(self.card(self.b)['listings'][0]['version'], self.base)
        self.assertEqual(self.card(self.home)['pending_suggestions_count'], 1)
        self.assertEqual(team.catalog(self.b)['suggestions_summary']['outgoing_total'], 1)
        incoming = team.suggestions_list(self.home, 'incoming', skill='example')['suggestions']
        self.assertEqual([(r['id'], r['role'], r['message']) for r in incoming], [(identity, 'incoming', '请审核\n第二行')])
        detail = team.suggestion_detail(self.home, identity)
        self.assertEqual(detail['diff']['files'], preview['files'])
        self.assertIn('+description: 新版说明', detail['diff']['files'][0]['preview'])
        self.assertEqual(detail['diff']['current_revision'], 1)
        self.assertFalse(detail['stale'])
        result = team.suggestion_accept(self.home, identity, '采用')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['version'], saved['version'])
        self.assertEqual(result['suggestion']['published_version'], saved['version'])
        after = self.db._listings(self.members[self.home])['example']
        for field in ('note', 'replaces', 'replacement_note', 'created_at', 'title', 'group', 'summary', 'requires'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after['description'], '新版说明')
        self.assertEqual(after['published_via'], 'suggestion')
        # Small skills stay inline so older dashboards can still read and install them.
        self.assertNotIn('file_storage', after['source'])
        pkg = self.request(self.c, f'/api/packages/{self.members[self.home]}/example')
        self.assertTrue(pkg.get('files'))
        # Author's own local copy is flagged behind_store (not changed) so one-click publish won't revert the accepted suggestion.
        author_candidates = [c for c in team.listing_candidates(self.home)['skills'] if c['name'] == 'example']
        self.assertEqual(len(author_candidates), 1)
        self.assertTrue(author_candidates[0]['behind_store'])
        self.assertFalse(author_candidates[0]['changed'])
        # Editing metadata on a behind_store listing preserves the accepted v2 files and description.
        edit_saved, edit_preview = team.listings_preview(self.home, [{'name': 'example', 'note': '更新后的推荐语'}])
        self.assertEqual(edit_preview['listings'][0]['version'], saved['version'])
        team.listings_apply(self.home, edit_saved)
        after_meta = self.db._listings(self.members[self.home])['example']
        self.assertEqual(after_meta['source']['version'], saved['version'])
        self.assertEqual(after_meta['description'], '新版说明')
        self.assertEqual(after_meta['note'], '更新后的推荐语')
        self.assertEqual(after_meta['published_via'], 'suggestion')
        self.assertEqual(self.db._packs(self.members[self.home]), packs)
        self.assertEqual(self.card(self.c)['local']['status'], 'update')
        review = updates.check(self.c)
        self.addCleanup(shutil.rmtree, review['temporary'], True)
        self.assertTrue(updates.apply(review, ['example'], confirmed=True)['complete'])
        self.assertEqual(self.card(self.c)['local']['status'], 'installed')
        self.assertTrue((self.c / '.skill-pilot/store/example/scripts/example.py').exists())
        self.assertFalse(self.files_for(self.b))
        self.error(409, team.suggestion_accept, self.home, identity)
        accepted_detail = team.suggestion_detail(self.b, identity)
        self.assertEqual(accepted_detail['status'], 'accepted')
        # Accepted diff stays accurate even if the author publishes another revision later.
        self.original['source']['files'].append(file('after_accept.txt', '后续新增'))
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        later_detail = team.suggestion_detail(self.b, identity)
        self.assertEqual(later_detail['diff']['summary'], accepted_detail['diff']['summary'])

    def test_reject_withdraw_gc_and_private_views(self):
        identity = self.propose()
        self.assertTrue(self.files_for(self.b))
        for home, action in ((self.c, team.suggestion_detail), (self.c, team.suggestion_withdraw),
                             (self.home, team.suggestion_withdraw), (self.b, team.suggestion_accept),
                             (self.b, team.suggestion_reject)):
            self.error(403, action, home, identity)
        for role in ('all', 'incoming', 'outgoing'):
            self.assertEqual(team.suggestions_list(self.c, role, 'all')['suggestions'], [])
        for query in ('role=other', 'status=rejected', 'skill=../x', 'extra=1', 'role=all&role=incoming', 'skill='):
            self.error(400, self.request, self.home, '/api/suggestions?' + query)
        self.error(404, team.suggestion_detail, self.home, '0' * 32)
        self.error(400, team.suggestion_reject, self.home, identity, 'a' * 301)
        result = team.suggestion_reject(self.home, identity, '已有更合适的做法')
        self.assertEqual(result['suggestion']['review_note'], '已有更合适的做法')
        self.assertFalse(self.files_for(self.b))
        self.assertEqual(team.suggestion_detail(self.b, identity)['status'], 'rejected')
        self.assertEqual(self.card(self.home)['listings'][0]['version'], self.base)
        self.error(409, team.suggestion_withdraw, self.b, identity)
        self.original['source']['files'].append(file('later.txt', '后来上架'))
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        latest = team.suggestion_detail(self.b, identity)['diff']
        # Rejected review_diff stays frozen at review time even after later author updates.
        self.assertNotIn('later.txt', latest['summary']['removed'])
        self.assertIn('+description: 新版说明', latest['files'][0]['preview'])
        saved, _ = team.suggestion_preview(self.b, self.key)
        identity = team.suggestion_submit(self.b, saved, '再提交')['id']
        self.assertEqual(team.suggestion_withdraw(self.b, identity), {'withdrawn': True, 'id': identity})
        self.assertFalse(self.files_for(self.b))

    def test_stale_version_and_manually_written_description(self):
        identity = self.propose()
        self.original['source']['files'].append(file('author.txt', '原作者新增'))
        self.original['description'] = '手写介绍'
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        detail = team.suggestion_detail(self.home, identity)
        self.assertTrue(detail['diff']['stale'])
        self.assertIn('author.txt', detail['diff']['summary']['removed'])
        self.assertEqual(detail['diff']['current_revision'], 2)
        for allow, expected in ((False, ''), (True, self.base)):
            body = self.error(409, team.suggestion_accept, self.home, identity, '', allow, expected)
            self.assertTrue(body['stale'])
            self.assertEqual((body['current_version'], body['base_version']), (detail['current_version'], self.base))
        self.assertEqual(team.suggestion_accept(self.home, identity, '', True, detail['current_version'])['revision'], 3)
        self.assertEqual(self.card(self.home)['description'], '手写介绍')

    def test_legacy_listing_counter(self):
        author = self.members[self.home]
        listings = self.db._listings(author)
        first = listings['example']['updated_at']
        for key in ('created_at', 'revision'):
            del listings['example'][key]
        hub.atomic_write(self.db._listing_path(author), {'listings': listings})
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        self.assertEqual((self.card(self.home)['created_at'], self.card(self.home)['revision']), (first, 1))
        self.original['source']['files'].append(file('new.txt', '新'))
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        self.assertEqual((self.card(self.home)['created_at'], self.card(self.home)['revision']), (first, 2))

    def test_concurrent_contributors_and_shared_pending_files(self):
        team.install(self.c, self.key)
        self.edit(self.b)
        self.edit(self.c)
        saves = [team.suggestion_preview(home, self.key)[0] for home in (self.b, self.c)]
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(team.suggestion_submit, home, saved, '一起改进')
                       for home, saved in zip((self.b, self.c), saves)]
            ids = [f.result()['id'] for f in futures]
        self.assertEqual(self.card(self.home)['pending_suggestions_count'], 2)
        identity = team.suggestion_submit(self.b, saves[0], '补充说明')['id']
        prior = self.files_for(self.b)
        team.suggestion_withdraw(self.b, identity)
        self.assertEqual(self.files_for(self.b), prior)
        team.suggestion_accept(self.home, ids[0])
        self.error(409, team.suggestion_accept, self.home, ids[1], '', True)
        self.assertTrue(self.files_for(self.c))
        team.suggestion_reject(self.home, ids[1])
        self.assertFalse(self.files_for(self.c))

    def test_source_name_identical_read_only_and_review_freshness(self):
        with self.assertRaisesRegex(ValueError, '完全一致'):
            team.suggestion_preview(self.b, self.key)
        with self.assertRaisesRegex(ValueError, '自己上架'):
            team.suggestion_preview(self.home, self.key)
        base_payload = {'target_member_id': self.members[self.home], 'skill_name': 'example', 'base_version': self.base,
                        'files': hub.skill_manifest(self.original['source']['files']), 'message': '说明'}
        self.error(409, self.request, self.b, '/api/suggestions', 'POST', base_payload)
        self.error(403, self.request, self.home, '/api/suggestions', 'POST', base_payload)
        for kind in ('lark', 'github'):
            self.request(self.home, '/api/listings/me', 'PUT', {'listings': [listing(kind, kind)]})
            with self.assertRaisesRegex(ValueError, '文件版技能'):
                team.suggestion_preview(self.b, kind)
            self.error(400, self.request, self.b, '/api/suggestions', 'POST', {**base_payload, 'skill_name': kind})
        root = self.edit(self.b)
        saved, _ = team.suggestion_preview(self.b, self.key)
        for msg in (' ', 'a' * 501, '说明\x00', '\t说明', 'a\u200db'):
            with self.assertRaises(ValueError):
                team.suggestion_submit(self.b, saved, msg)
        with self.assertRaisesRegex(ValueError, '设置已经变'):
            team.suggestion_submit(self.b, {**saved, 'connection': ''}, '说明')
        identity = team.suggestion_submit(self.b, saved, '待审核')['id']
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            for func, args in ((team.suggestion_submit, (self.b, saved, '说明')),
                               (team.suggestion_accept, (self.home, identity)),
                               (team.suggestion_reject, (self.home, identity)),
                               (team.suggestion_withdraw, (self.b, identity))):
                with self.assertRaisesRegex(ValueError, '只读'):
                    func(*args)
        (root / 'SKILL.md').write_text(document('wrong'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '重新预览'):
            team.suggestion_submit(self.b, saved, '说明')
        with self.assertRaisesRegex(ValueError, '名称'):
            team.suggestion_preview(self.b, self.key)
        manifest, _ = team._files(root)
        raw = (root / 'SKILL.md').read_bytes()
        team._upload_blob(team.settings(self.b), manifest['SKILL.md']['sha256'], raw)
        self.error(400, self.request, self.b, '/api/suggestions', 'POST', {**base_payload, 'files': manifest})
        registry_path = self.b / '.skill-pilot/registry.json'
        registry = json.loads(registry_path.read_text())
        registry['skills']['example']['withdrawn'] = True
        registry_path.write_text(json.dumps(registry))
        self.assertFalse(self.card(self.b)['can_suggest'])
        with self.assertRaisesRegex(ValueError, '先安装'):
            team.suggestion_preview(self.b, self.key)

    def test_limits_unlist_and_member_removal(self):
        self.edit(self.b)
        saved, _ = team.suggestion_preview(self.b, self.key)
        identity = team.suggestion_submit(self.b, saved, '第一次')['id']
        for key, limit in (('MAX_SUGGESTIONS_PER_SKILL', 1), ('MAX_SUGGESTIONS_PER_MEMBER', 1),
                           ('MAX_SUGGESTIONS_TOTAL', 1), ('MAX_SUGGESTION_TOTAL_BYTES', 1),
                           ('MAX_SUGGESTION_BODY', 1)):
            with self.subTest(key=key), patch.object(hub, key, limit):
                if key == 'MAX_SUGGESTION_BODY':
                    import http.client
                    conn = http.client.HTTPConnection(*self.server.server_address)
                    try:
                        conn.request('POST', '/api/suggestions', b'{}',
                                     {'Authorization': 'Bearer ' + team.settings(self.b)['key']})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 413)
                        response.read()
                    finally:
                        conn.close()
                else:
                    self.error(413, team.suggestion_submit, self.b, saved, '再次提交')
        team.suggestion_reject(self.home, identity)
        with patch.object(hub, 'MAX_SUGGESTIONS_TOTAL', 1):
            second = team.suggestion_submit(self.b, saved, '第二次')['id']
        self.error(404, team.suggestion_detail, self.home, identity)
        self.request(self.home, '/api/listings/me/example', 'DELETE')
        self.assertFalse(self.files_for(self.b))
        detail = team.suggestion_detail(self.b, second)
        self.assertEqual((detail['status'], detail['review_note']), ('rejected', '原作者已下架此技能。'))
        self.assertEqual(detail['diff']['current_version'], '')
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        third = team.suggestion_submit(self.b, saved, '第三次')['id']
        team.install(self.c, self.key)
        fourth = self.propose(self.c)
        self.db.remove_member(self.members[self.b])
        self.assertFalse(self.files_for(self.b))
        for identity in (second, third):
            self.error(404, team.suggestion_detail, self.home, identity)
        self.assertEqual(team.suggestion_detail(self.home, fourth)['status'], 'pending')
        self.db.remove_member(self.members[self.home])
        self.assertFalse(self.files_for(self.c))
        self.error(404, team.suggestion_detail, self.c, fourth)

    def test_limits_count_the_right_member_skill_and_distinct_contents(self):
        self.edit(self.b)
        saved, _ = team.suggestion_preview(self.b, self.key)
        payload = {k: saved[k] for k in ('target_member_id', 'skill_name', 'base_version', 'files')}
        body = self.error(409, self.request, self.b, '/api/suggestions', 'POST', dict(payload, message='说明'))
        self.assertEqual(set(body['missing']), {f['sha256'] for f in saved['files'].values()})
        team.suggestion_submit(self.b, saved, '第一份')
        team.install(self.c, self.key)
        self.edit(self.c)
        second, _ = team.suggestion_preview(self.c, self.key)
        with patch.object(hub, 'MAX_SUGGESTIONS_PER_SKILL', 1):
            self.error(413, team.suggestion_submit, self.c, second, '另一个人的改动')
        size = sum({f['sha256']: f['size'] for f in saved['files'].values()}.values())
        with patch.object(hub, 'MAX_SUGGESTION_TOTAL_BYTES', size * 2 - 1):
            self.error(413, team.suggestion_submit, self.c, second, '跨成员重复文件各自计费超上限')
        with patch.object(hub, 'MAX_SUGGESTION_TOTAL_BYTES', size * 2), patch.object(hub, 'MAX_SUGGESTIONS_PER_MEMBER', 1):
            team.suggestion_submit(self.c, second, '另一人提交相同文件')
            (self.b / '.skill-pilot/store/helper/new.txt').write_text('other')
            helper, _ = team.suggestion_preview(self.b, 'helper')
        with patch.object(hub, 'MAX_SUGGESTIONS_PER_MEMBER', 1):
            self.error(413, team.suggestion_submit, self.b, helper, '同一人提交另一个技能')
        with patch.object(hub, 'MAX_SUGGESTION_TOTAL_BYTES', size * 2):
            self.error(413, team.suggestion_submit, self.b, helper, '有额外文件')

    def test_server_rechecks_skill_name_and_listing_when_accepting(self):
        identity = self.propose()
        author = self.members[self.home]
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [listing('example', 'github')]})
        self.error(404, team.suggestion_accept, self.home, identity, '', True)
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        saved_path = self.db._suggestion_path(identity)
        saved = json.loads(saved_path.read_text())
        invalid = document('another').encode()
        entry = hub.skill_manifest([file('SKILL.md', invalid)])['SKILL.md']
        team._upload_blob(team.settings(self.b), entry['sha256'], invalid)
        saved['files']['SKILL.md'] = entry
        hub.atomic_write(saved_path, saved)
        self.error(400, team.suggestion_accept, self.home, identity)
        self.assertEqual(self.db._listings(author)['example']['source']['version'], self.base)

    def test_failed_accept_preserves_package_and_suggestion(self):
        identity = self.propose()
        row = self.db._suggestion(identity, self.members[self.b])
        path = self.db._blob_dir(self.members[self.b]) / row['files']['scripts/example.py']['sha256']
        original = path.read_bytes()
        path.write_bytes(b'x' * len(original))
        for missing in (False, True):
            if missing:
                path.unlink()
            self.error(409, team.suggestion_accept, self.home, identity)
            self.assertEqual(self.card(self.home)['revision'], 1)
            self.assertFalse(self.files_for(self.home))
        path.write_bytes(original)
        write = hub.atomic_write
        def fail_last(path, data):
            if path.name == identity + '.json':
                raise OSError
            write(path, data)
        with patch.object(hub, 'atomic_write', side_effect=fail_last):
            self.error(500, team.suggestion_accept, self.home, identity)
        self.assertEqual(self.card(self.home)['listings'][0]['version'], self.base)
        self.assertFalse(self.files_for(self.home))
        self.assertEqual(team.suggestion_detail(self.home, identity)['status'], 'pending')
        self.assertEqual(team.suggestion_accept(self.home, identity)['revision'], 2)

    def test_old_hub_and_diff_limits(self):
        real = team._request
        def old(config, path, *args):
            self.assertFalse(path.startswith('/api/suggestions'))
            data = real(config, path, *args)
            for k in ('suggestions_supported', 'suggestions_summary'):
                data.pop(k, None)
            for card in data.get('cards', []):
                for row in card['listings']:
                    row.pop('created_at', None)
                    row.pop('revision', None)
            return data
        with patch.object(team, '_request', side_effect=old):
            catalog = team.catalog(self.b)
            self.assertFalse(catalog['suggestions_supported'])
            self.assertFalse(any(c['can_suggest'] for c in catalog['cards']))
            self.assertEqual(catalog['suggestions_summary'], {'incoming_by_skill': {}, 'incoming_total': 0,
                                                               'outgoing_total': 0})
            self.assertEqual(team.suggestions_list(self.b), {'supported': False, 'suggestions': []})
            with self.assertRaisesRegex(ValueError, '分享站版本较旧'):
                team.suggestion_preview(self.b, self.key)
        raw = {'bin': b'\x00\xff', 'large': b'x' * (hub.MAX_DIFF_FILE_BYTES + 1), 'text': b'abc\n' * 3000}
        manifest = hub.skill_manifest([file(k, v) for k, v in raw.items()])
        def read(name):
            self.assertNotEqual(name, 'large')
            return raw[name]
        with patch.object(hub, 'MAX_DIFF_TOTAL_CHARS', 45):
            compared = hub.suggestion_diff({}, manifest, None, read)['files']
        self.assertTrue(compared[0]['binary'])
        self.assertTrue(compared[1]['truncated'])
        self.assertTrue(compared[2]['truncated'])
        self.assertLessEqual(sum(len(r['preview']) for r in compared), 45)
        mode = hub.suggestion_diff(manifest, {**manifest, 'text': {**manifest['text'], 'executable': True}},
                                   read, read)['files'][0]
        self.assertTrue(mode['mode_changed'])
        self.assertTrue(mode['is_script'])

    def command(self, home, *args, status=0, as_json=True):
        output = io.StringIO()
        argv = ['skill-pilot', 'team', *args, *(['--json'] if as_json else [])]
        with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'SKILL_PILOT_HOME': str(home)}), \
                contextlib.redirect_stdout(output):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, status, output.getvalue())
        return json.loads(output.getvalue()) if as_json else output.getvalue()

    def test_cli_preview_confirm_list_and_review(self):
        self.edit(self.b)
        args = ('suggest', '示例工具', '--author', '原作者')
        plain = self.command(self.b, *args, status=2, as_json=False)
        for text in ('新增', '修改', '删除', '不上传 .env', '--yes'):
            self.assertIn(text, plain)
        self.assertTrue(self.command(self.b, *args, status=2)['needs_confirmation'])
        self.assertIn('改进说明', self.command(self.b, *args, '--yes', status=1)['error'])
        identity = self.command(self.b, *args, '--message', '建议', '--yes')['id']
        result = self.command(self.home, 'suggestions', '--role', 'incoming', '--skill', 'example')
        self.assertEqual(result['suggestions'][0]['id'], identity)
        self.assertIn('+description: 新版说明', self.command(self.home, 'review', identity, as_json=False))
        self.assertEqual(self.command(self.home, 'review', identity, '--reject', status=2)['action'], 'reject')
        self.original['source']['files'].append(file('author.txt', '新增'))
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        stale_preview = self.command(self.home, 'review', identity, '--accept', status=2, as_json=False)
        self.assertIn('--allow-stale', stale_preview)
        self.assertIn('--expected-version', stale_preview)
        # Missing --expected-version is rejected when stale.
        self.assertIn('--expected-version', self.command(self.home, 'review', identity, '--accept', '--yes', '--allow-stale', status=1)['error'])
        current_version = team.suggestion_detail(self.home, identity)['current_version']
        result = self.command(self.home, 'review', identity, '--accept', '--yes', '--allow-stale', '--expected-version', current_version)
        self.assertEqual(result['revision'], 3)
        root = self.b / '.skill-pilot/store/example'
        (root / 'new.txt').write_text('more')
        second = self.command(self.b, *args, '--message', '补充', '--yes')['id']
        self.command(self.home, 'review', second, '--reject', '--note', '暂缓', '--yes')
        rows = self.command(self.b, 'suggestions', '--all', '--role', 'outgoing')['suggestions']
        self.assertEqual({r['status'] for r in rows}, {'rejected', 'accepted'})

    def test_r1_security_and_stale_base_guards(self):
        # Collaborator with unmodified v1 cannot submit a rollback suggestion against author's v2.
        self.original['source']['files'].append(file('author_v2.txt', '新版内容'))
        self.request(self.home, '/api/listings/me', 'PUT', {'listings': [self.original]})
        with self.assertRaisesRegex(ValueError, '没有改动|新版'):
            team.suggestion_preview(self.b, self.key)
        # Collaborator editing v1 after v2 is published still records exact base_revision == 1.
        self.edit(self.b)
        stale_saved, stale_preview = team.suggestion_preview(self.b, self.key)
        self.assertTrue(stale_preview['stale'])
        stale_created = team.suggestion_submit(self.b, stale_saved, '基于第1版的改进')
        self.assertEqual(stale_created['base_revision'], 1)
        team.suggestion_withdraw(self.b, stale_created['id'])
        # Server rejects secret filename/content even when manifest format & all blobs are valid.
        manifest = hub.skill_manifest(self.original['source']['files'])
        for entry in self.original['source']['files']:
            raw = __import__('base64').b64decode(entry['content'])
            team._upload_blob(team.settings(self.b), hashlib.sha256(raw).hexdigest(), raw)
        secret_bytes = b'API_KEY=sk-1234567890abcdef123456\n'
        secret_sha = hashlib.sha256(secret_bytes).hexdigest()
        team._upload_blob(team.settings(self.b), secret_sha, secret_bytes)
        manifest['notes.txt'] = {'sha256': secret_sha, 'size': len(secret_bytes), 'executable': False}
        payload = {
            'target_member_id': self.members[self.home], 'skill_name': 'example',
            'base_version': self.base, 'files': manifest, 'message': '尝试绕过上传密钥',
        }
        body = self.error(400, self.request, self.b, '/api/suggestions', 'POST', payload)
        self.assertIn('密钥', body['error'])
