"""Tests for Issue #236: Team Store skill replacement metadata & install-time local cleanup."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import cleanup, operations, store, team, team_hub
from tests import set_up_home, test_cli_sharing, test_team, test_team_hub
from tests.test_nav_static import STATIC, extract_function
from tests.test_team_hub import listing


class TeamHubReplacesTests(unittest.TestCase):
    setUp = test_team_hub.TeamHubTests.setUp
    close = test_team_hub.TeamHubTests.close
    request = test_team_hub.TeamHubTests.request
    put = test_team_hub.TeamHubTests.put
    cards = test_team_hub.TeamHubTests.cards
    state = test_team_hub.TeamHubTests.state

    def test_clean_replaces_validation_and_normalization(self):
        self.assertEqual(team_hub.clean_replaces([' old-a ', 'old-b', 'old-a']), ['old-a', 'old-b'])
        self.assertEqual(team_hub.clean_replaces([]), [])
        self.assertEqual(len(team_hub.clean_replaces([f'old-{i}' for i in range(20)])), 20)
        for bad in ('old-a', 123, [123], [''], ['   '], ['../bad'], ['has space'], ['a' * 65],
                    [f'old-{i}' for i in range(21)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                team_hub.clean_replaces(bad)
        with self.assertRaises(ValueError):
            team_hub.clean_replaces(['self-skill'], own_name='self-skill')

    def test_put_edit_clear_replaces_and_unpublish_linkage(self):
        status, me = self.request('/api/me')
        self.assertEqual(status, 200)
        self.assertIs(me.get('replaces_supported'), True)

        # Publish old skill and new skill declaring replaces + replacement_note
        status, _ = self.put([
            listing('old-tool', 'files'),
            listing('new-tool', 'files', replaces=[' old-tool ', 'old-tool'], replacement_note='已升级为新版'),
        ])
        self.assertEqual(status, 200)
        by_name = {c['listings'][0]['name']: c['listings'][0] for c in self.cards()}
        self.assertEqual(by_name['new-tool']['replaces'], ['old-tool'])
        self.assertEqual(by_name['new-tool']['replacement_note'], '已升级为新版')
        self.assertNotIn('replaces', by_name['old-tool'])

        # Validation: replacement_note without replaces fails atomically
        before = self.state()
        status, body = self.put([listing('new-tool', 'files', replaces=[], replacement_note='孤立说明')])
        self.assertEqual(status, 400, body)
        self.assertEqual(self.state(), before)

        # Edit replaces (change target)
        status, _ = self.put([listing('new-tool', 'files', replaces=['old-tool', 'legacy-tool'], replacement_note='合并旧版')])
        self.assertEqual(status, 200)
        by_name = {c['listings'][0]['name']: c['listings'][0] for c in self.cards()}
        self.assertEqual(by_name['new-tool']['replaces'], ['old-tool', 'legacy-tool'])
        self.assertEqual(by_name['new-tool']['replacement_note'], '合并旧版')

        # Clear replaces (empty list omits replaces and replacement_note)
        status, _ = self.put([listing('new-tool', 'files', replaces=[])])
        self.assertEqual(status, 200)
        by_name = {c['listings'][0]['name']: c['listings'][0] for c in self.cards()}
        self.assertNotIn('replaces', by_name['new-tool'])
        self.assertNotIn('replacement_note', by_name['new-tool'])

        # Unpublish old-tool while attaching replaced_by and replacement_note
        new_updated_before = by_name['new-tool']['updated_at']
        status, body = self.request('/api/listings/me/old-tool', 'DELETE',
                                    {'replaced_by': 'new-tool', 'replacement_note': '请改用新版'})
        self.assertEqual(status, 200, body)
        by_name = {c['listings'][0]['name']: c['listings'][0] for c in self.cards()}
        self.assertNotIn('old-tool', by_name)
        self.assertEqual(by_name['new-tool']['replaces'], ['old-tool'])
        self.assertEqual(by_name['new-tool']['replacement_note'], '请改用新版')
        self.assertEqual(by_name['new-tool']['updated_at'], new_updated_before)

    def test_unpublish_preserves_existing_linkage_and_rejects_invalid_targets(self):
        self.put([listing('old-one', 'files'), listing('new-one', 'files', replaces=['old-one'])])
        self.put([listing('other-new', 'files', replaces=['old-one'])], token=self.second['token'])

        # Invalid replaced_by or standalone replacement_note fails without deleting
        before = self.state()
        for payload in (
            {'replaced_by': 'old-one'},
            {'replaced_by': 'missing-skill'},
            {'replaced_by': 'other-new'},
            {'replacement_note': '太长' * 120},
        ):
            status, _ = self.request('/api/listings/me/old-one', 'DELETE', payload)
            self.assertEqual(status, 400, payload)
        self.assertEqual(self.state(), before)

        # Unpublishing with replacement_note only updates existing linked listing
        status, _ = self.request('/api/listings/me/old-one', 'DELETE', {'replacement_note': '旧版已下线'})
        self.assertEqual(status, 200)
        listings_all = [r for c in self.cards() for r in c['listings']]
        mine_new = next(r for r in listings_all if r['name'] == 'new-one')
        other_new = next(r for r in listings_all if r['name'] == 'other-new')
        self.assertEqual(mine_new['replaces'], ['old-one'])
        self.assertEqual(mine_new['replacement_note'], '旧版已下线')
        self.assertEqual(other_new['replaces'], ['old-one'])


class TeamSupersedesIntegrationTests(unittest.TestCase):
    Team = test_team.TeamTests
    setUp, start, skill, make_home, registry = Team.setUp, Team.start, Team.skill, Team.make_home, Team.registry
    request, ok, configure, library, raw_put = Team.request, Team.ok, Team.configure, Team.library, Team.raw_put

    def wait_queue(self, server, key, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            items = self.ok(server, '/api/team/queue')['items']
            found = next((i for i in items if i.get('key') == key), None)
            if found and found['status'] in {'done', 'failed'}:
                return found
            time.sleep(0.05)
        self.fail(f'queue item {key} did not finish')

    def publish_with_replaces(self, name='acme-extra', replaces=('acme-other',), replacement_note='请换新版', note='新版推荐'):
        preview = self.ok(self.a, '/api/team/listings/preview', {
            'skills': [{'name': name, 'note': note, 'replaces': list(replaces), 'replacement_note': replacement_note}],
        })
        self.assertEqual(preview['errors'], [])
        self.ok(self.a, '/api/team/listings/apply', {'review': preview['review']})
        cards = self.ok(self.b, '/api/team/catalog')['cards']
        return next(c for c in cards if c['name'] == name)

    def test_publish_edit_clear_replaces_and_one_click_update_preserves_replaces(self):
        self.configure()
        card = self.publish_with_replaces('acme-local', ['acme-other'], '替代旧版')
        self.assertEqual(card['replaces'], ['acme-other'])
        self.assertEqual(card['replacement_note'], '替代旧版')
        self.assertEqual([item['name'] for item in card['replaces_local']], ['acme-other'])

        # One-click update preserves replaces and replacement_note when local files change
        skill_md = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        skill_md.write_text(skill_md.read_text() + '\n新增说明\n')
        conn = team._connection_version(team._connection(self.home))
        team.update_listing(self.home, 'acme-local', conn)
        card = next(c for c in self.ok(self.b, '/api/team/catalog')['cards'] if c['name'] == 'acme-local')
        self.assertEqual(card['replaces'], ['acme-other'])
        self.assertEqual(card['replacement_note'], '替代旧版')

        # Edit replaces to clear it via web preview/apply
        preview = self.ok(self.a, '/api/team/listings/preview', {
            'skills': [{'name': 'acme-local', 'note': '新版推荐', 'replaces': [], 'replacement_note': ''}],
        })
        self.ok(self.a, '/api/team/listings/apply', {'review': preview['review']})
        card = next(c for c in self.ok(self.b, '/api/team/catalog')['cards'] if c['name'] == 'acme-local')
        self.assertEqual(card['replaces'], [])
        self.assertNotIn('replacement_note', card)
        self.assertNotIn('replaces_local', card)

    def test_install_never_silently_deletes_and_supports_keep_withdraw_delete_with_undo(self):
        self.configure()
        card = self.publish_with_replaces('acme-local', ['acme-other'], '改用新版')
        # Queue install on B (which has acme-other enabled on claude)
        self.ok(self.b, '/api/team/queue', {'key': card['key']})
        done = self.wait_queue(self.b, card['key'])
        self.assertEqual(done['status'], 'done')
        self.assertEqual([i['name'] for i in done['replaced_local']], ['acme-other'])
        self.assertEqual(done['replacement_note'], '改用新版')
        self.assertTrue(done['replace_revision'])

        # Old skill MUST still be present and enabled on claude (never silently deleted/withdrawn!)
        reg = operations.load(self.other)[1]['skills']
        self.assertIn('acme-local', reg)
        self.assertIn('acme-other', reg)
        self.assertEqual(reg['acme-other']['assigned_agents'], ['claude'])
        self.assertTrue((self.other / '.claude/skills/acme-other').exists())

        # User chooses Keep -> zero writes, zero operations
        kept = self.ok(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'keep'}, 'revision': done['replace_revision'], 'confirmed': True,
        })
        self.assertEqual(kept['operations'], [])
        self.assertEqual(operations.load(self.other)[1]['skills']['acme-other']['assigned_agents'], ['claude'])

        # User chooses Withdraw -> withdraws from claude, recorded in log, undo restores completely
        withdrawn = self.ok(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'withdraw'}, 'revision': done['replace_revision'], 'confirmed': True,
        })
        self.assertEqual(withdrawn['withdrawn'], ['acme-other'])
        self.assertEqual(len(withdrawn['operations']), 1)
        reg = operations.load(self.other)[1]['skills']
        self.assertTrue(reg['acme-other'].get('withdrawn'))
        self.assertEqual(reg['acme-other']['assigned_agents'], [])
        self.assertFalse((self.other / '.claude/skills/acme-other').exists())
        operations.undo(withdrawn['operations'][0], home=self.other)
        reg = operations.load(self.other)[1]['skills']
        self.assertFalse(reg['acme-other'].get('withdrawn'))
        self.assertEqual(reg['acme-other']['assigned_agents'], ['claude'])
        self.assertTrue((self.other / '.claude/skills/acme-other').exists())

        # Fresh preview -> User chooses Delete -> removes from store & claude, undo restores completely
        preview = self.ok(self.b, '/api/team/replace/preview', {'names': ['acme-other']})
        deleted = self.ok(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'delete'}, 'revision': preview['revision'], 'confirmed': True,
        })
        self.assertEqual(deleted['deleted'], ['acme-other'])
        self.assertEqual(len(deleted['operations']), 1)
        self.assertNotIn('acme-other', operations.load(self.other)[1]['skills'])
        self.assertFalse((self.other / '.skill-pilot/store/acme-other').exists())
        operations.undo(deleted['operations'][0], home=self.other)
        reg = operations.load(self.other)[1]['skills']
        self.assertIn('acme-other', reg)
        self.assertEqual(reg['acme-other']['assigned_agents'], ['claude'])
        self.assertTrue((self.other / '.skill-pilot/store/acme-other/SKILL.md').is_file())

    def test_detects_store_and_agent_unmanaged_external_and_blocks_active_dependents_and_readonly(self):
        self.configure()
        # Add an unmanaged skill directly inside B's connected claude skills folder
        self.skill(self.other / '.claude/skills/legacy-unmanaged', 'legacy-unmanaged')
        # Add an external skill entry in B's registry
        def add_ext(skills):
            pass
        reg_path = self.other / '.skill-pilot/registry.json'
        reg_data = json.loads(reg_path.read_text())
        reg_data.setdefault('external_skills', {})['legacy-external'] = {'agents': ['claude']}
        reg_path.write_text(json.dumps(reg_data))

        # Inspect detects managed active + unmanaged + external
        items, rev = team.inspect_replaced_local(
            self.other, ['acme-other', 'legacy-unmanaged', 'legacy-external', 'not-installed'], include_revision=True)
        by_name = {i['name']: i for i in items}
        self.assertEqual(set(by_name), {'acme-other', 'legacy-unmanaged', 'legacy-external'})
        self.assertEqual(by_name['acme-other']['status'], 'active')
        self.assertTrue(by_name['acme-other']['can_withdraw'])
        self.assertTrue(by_name['acme-other']['can_delete'])
        self.assertEqual(by_name['legacy-unmanaged']['status'], 'unmanaged')
        self.assertFalse(by_name['legacy-unmanaged']['can_withdraw'])
        self.assertFalse(by_name['legacy-unmanaged']['can_delete'])
        self.assertIn('未纳入统一管理', by_name['legacy-unmanaged']['blocked_reason'])
        self.assertEqual(by_name['legacy-external']['status'], 'external')
        self.assertFalse(by_name['legacy-external']['can_delete'])
        self.assertIn('外部工具维护', by_name['legacy-external']['blocked_reason'])

        # Active dependent blocking: add acme-extra to B depending on acme-other
        shutil.copytree(self.repository / 'skills/acme-extra', self.other / '.skill-pilot/store/acme-extra')
        shutil.copytree(self.repository / 'skills/acme-extra', self.other / '.skill-pilot/bases/acme-extra')
        self.registry(self.other, lambda skills: skills.update({
            'acme-extra': {**skills['acme-other'], 'requires': ['acme-other'], 'assigned_agents': ['claude'], 'entries': []}
        }))
        dep_items = team.inspect_replaced_local(self.other, ['acme-other'])
        self.assertFalse(dep_items[0]['can_withdraw'])
        self.assertFalse(dep_items[0]['can_delete'])
        self.assertIn('依赖', dep_items[0]['blocked_reason'])
        for action in ('withdraw', 'delete'):
            status, body = self.request(self.b, '/api/team/replace/cleanup', {
                'actions': {'acme-other': action}, 'confirmed': True,
            })
            self.assertEqual(status, 409, body)
            self.assertIn('依赖', body['error'])
        self.assertIn('acme-other', operations.load(self.other)[1]['skills'])

        # Read-only mode blocking
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            preview = self.ok(self.b, '/api/team/replace/preview', {'names': ['acme-other']})
            self.assertFalse(preview['write_allowed'])
            self.assertFalse(preview['items'][0]['can_withdraw'])
            self.assertFalse(preview['items'][0]['can_delete'])
            self.assertIn('只读', preview['items'][0]['blocked_reason'])
            status, body = self.request(self.b, '/api/team/replace/cleanup', {
                'actions': {'acme-other': 'delete'}, 'confirmed': True,
            })
            self.assertEqual(status, 403, body)
            self.assertIn('只读', body['error'])
            with self.assertRaisesRegex(ValueError, '只读'):
                team.cleanup_replaced(self.other, {'acme-other': 'withdraw'}, confirmed=True)

        # Stale revision rejection
        self.registry(self.other, lambda skills: skills.pop('acme-extra', None))
        status, body = self.request(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'withdraw'}, 'revision': rev + '-stale', 'confirmed': True,
        })
        self.assertEqual(status, 409, body)
        self.assertIn('状态已经变了', body['error'])

    def test_backward_compatibility_between_old_and_new_hub_and_dashboard(self):
        self.configure()
        card = self.publish_with_replaces('acme-local', ['acme-other'], '新版说明')

        # 1. Old dashboard (strictly ignoring replaces/replacement_note) reading new hub catalog -> zero error
        raw = team._request(team.settings(self.other), '/api/catalog')
        old_clean = team_hub.clean_listing
        def legacy_clean_listing(row, packaged=False):
            stripped = {k: v for k, v in row.items() if k not in {'replaces', 'replacement_note'}}
            return old_clean(stripped, packaged=packaged)
        with patch.object(team_hub, 'clean_listing', side_effect=legacy_clean_listing):
            parsed = team._catalog(team.settings(self.other))
            self.assertEqual(len(parsed['cards']), 1)
            self.assertNotIn('replaces', parsed['cards'][0]['listings'][0])

        # 2. New dashboard talking to simulated old hub (/api/me lacks replaces_supported, /api/catalog lacks replaces)
        real_request = team._request
        def old_hub_request(config, path, method='GET', data=None):
            res = real_request(config, path, method, data)
            if path == '/api/me':
                res = {k: v for k, v in res.items() if k != 'replaces_supported'}
            elif path == '/api/catalog':
                res = copy.deepcopy(res)
                for c in res.get('cards', []):
                    for r in c.get('listings', []):
                        r.pop('replaces', None)
                        r.pop('replacement_note', None)
            return res
        with patch.object(team, '_request', side_effect=old_hub_request):
            cat = team.catalog(self.other)
            self.assertFalse(cat['replaces_supported'])
            self.assertEqual(cat['cards'][0]['replaces'], [])
            cand = team.listing_candidates(self.home)
            self.assertFalse(cand['replaces_supported'])
            # Normal install works against old hub
            installed = team.install(self.other, card['key'])
            self.assertEqual(installed['installed'], ['acme-local'])
            # Explicitly trying to save replaces against old hub fails clearly
            with self.assertRaisesRegex(ValueError, '分享站版本较旧'):
                team.listings_preview(self.home, [{'name': 'acme-extra', 'replaces': ['acme-other']}])
            # Plain unpublish works against old hub without body
            removed = team.listings_remove(self.home, ['acme-local'], confirmed=True)
            self.assertIn('下架', removed['message'])


class TeamSupersedesCliTests(unittest.TestCase):
    Team = test_team.TeamTests
    setUp, start, skill, make_home, registry = Team.setUp, Team.start, Team.skill, Team.make_home, Team.registry
    request, ok, configure, library, raw_put = Team.request, Team.ok, Team.configure, Team.library, Team.raw_put

    def run_cli(self, home, *args, extra_env=None):
        env = {**os.environ, 'SKILL_PILOT_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1', **(extra_env or {})}
        return subprocess.run([sys.executable, '-m', 'skill_pilot', *args], cwd=store.PROJECT,
                              env=env, capture_output=True, text=True, timeout=60)

    def test_cli_publish_replaces_unpublish_link_install_cleanup_and_undo(self):
        self.configure()
        # Publish acme-other and acme-local, then unpublish acme-other with --replaced-by acme-local
        pub = self.run_cli(self.home, 'team', 'publish', 'acme-other', 'acme-local', '--yes', '--json')
        self.assertEqual(pub.returncode, 0, pub.stdout + pub.stderr)

        unpub_preview = self.run_cli(self.home, 'team', 'unpublish', 'acme-other',
                                     '--replaced-by', 'acme-local', '--replacement-note', '请切换新版')
        self.assertEqual(unpub_preview.returncode, 2, unpub_preview.stdout + unpub_preview.stderr)
        self.assertIn('推荐替代技能', unpub_preview.stdout)

        unpub = self.run_cli(self.home, 'team', 'unpublish', 'acme-other',
                             '--replaced-by', 'acme-local', '--replacement-note', '请切换新版', '--yes', '--json')
        self.assertEqual(unpub.returncode, 0, unpub.stdout + unpub.stderr)
        cat = team.catalog(self.other)
        card = next(c for c in cat['cards'] if c['name'] == 'acme-local')
        self.assertEqual(card['replaces'], ['acme-other'])
        self.assertEqual(card['replacement_note'], '请切换新版')

        # Preview install on B shows local old skill acme-other
        prev = self.run_cli(self.other, 'team', 'install', 'acme-local')
        self.assertEqual(prev.returncode, 2, prev.stdout + prev.stderr)
        self.assertIn('acme-other', prev.stdout)
        self.assertIn('--replace-action', prev.stdout)

        # Install with --yes and NO --replace-action keeps acme-other untouched
        inst_keep = self.run_cli(self.other, 'team', 'install', 'acme-local', '--yes', '--json')
        self.assertEqual(inst_keep.returncode, 0, inst_keep.stdout + inst_keep.stderr)
        self.assertIn('acme-other', operations.load(self.other)[1]['skills'])
        self.assertEqual(operations.load(self.other)[1]['skills']['acme-other']['assigned_agents'], ['claude'])

        # Run team install with --replace-action withdraw + --yes, then undo
        inst_w = self.run_cli(self.other, 'team', 'install', 'acme-local', '--replace-action', 'withdraw', '--yes', '--json')
        self.assertEqual(inst_w.returncode, 0, inst_w.stdout + inst_w.stderr)
        res_w = json.loads(inst_w.stdout)
        self.assertEqual(res_w['replaced_cleanup']['withdrawn'], ['acme-other'])
        self.assertTrue(operations.load(self.other)[1]['skills']['acme-other'].get('withdrawn'))
        undo_w = self.run_cli(self.other, 'undo', res_w['replaced_cleanup']['operations'][0])
        self.assertEqual(undo_w.returncode, 0, undo_w.stdout + undo_w.stderr)
        self.assertFalse(operations.load(self.other)[1]['skills']['acme-other'].get('withdrawn'))

        # Read-only mode blocks CLI replace-action delete
        ro = self.run_cli(self.other, 'team', 'install', 'acme-local', '--replace-action', 'delete', '--yes',
                          extra_env={'SKILL_PILOT_READ_ONLY': '1'})
        self.assertNotEqual(ro.returncode, 0)
        self.assertIn('只读', ro.stdout + ro.stderr)

        # Active delete + undo via CLI
        inst_d = self.run_cli(self.other, 'team', 'install', 'acme-local', '--replace-action', 'acme-other=delete', '--yes', '--json')
        self.assertEqual(inst_d.returncode, 0, inst_d.stdout + inst_d.stderr)
        res_d = json.loads(inst_d.stdout)
        self.assertNotIn('acme-other', operations.load(self.other)[1]['skills'])
        undo_d = self.run_cli(self.other, 'undo', res_d['replaced_cleanup']['operations'][0])
        self.assertEqual(undo_d.returncode, 0, undo_d.stdout + undo_d.stderr)
        self.assertIn('acme-other', operations.load(self.other)[1]['skills'])

        # CLI --replaces and --clear-replaces
        pub_rep = self.run_cli(self.home, 'team', 'publish', 'acme-local',
                               '--replaces', 'acme-local=legacy-a,legacy-b', '--replacement-note', 'acme-local=新版替代', '--yes')
        self.assertEqual(pub_rep.returncode, 0, pub_rep.stdout + pub_rep.stderr)
        self.assertEqual(next(c for c in team.catalog(self.other)['cards'] if c['name'] == 'acme-local')['replaces'],
                         ['legacy-a', 'legacy-b'])
        pub_clr = self.run_cli(self.home, 'team', 'publish', 'acme-local', '--clear-replaces', 'acme-local', '--yes')
        self.assertEqual(pub_clr.returncode, 0, pub_clr.stdout + pub_clr.stderr)
        self.assertEqual(next(c for c in team.catalog(self.other)['cards'] if c['name'] == 'acme-local')['replaces'], [])


class TeamSupersedesStaticTests(unittest.TestCase):
    def test_dialogs_helpers_and_plain_chinese_copy(self):
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        source = (STATIC / 'app.js').read_text(encoding='utf-8')
        for marker in ('id="store-unlist-dialog"', 'id="store-unlist-replaced-by"', 'id="store-unlist-note"',
                       'id="store-replace-dialog"', 'id="store-replace-list"', 'id="store-replace-keep"',
                       'id="store-replace-apply"'):
            self.assertIn(marker, html)
        # Ensure new dialog HTML contains no banned developer jargon
        unlist_block = html[html.index('id="store-unlist-dialog"'):html.index('</dialog>', html.index('id="store-replace-dialog"'))]
        for jargon in ('symlink', 'diff', 'git', 'TOML', 'hash'):
            self.assertNotRegex(unlist_block, re.compile(r'\b' + re.escape(jargon) + r'\b', re.I))

        script = '\n'.join([
            'const assert = require("assert");',
            'const escape = v => String(v ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");',
            'const icon = n => `<svg data-icon="${n}"></svg>`;',
            'const teamCanWrite = () => true;',
            'const jobsBlocked = () => false;',
            extract_function(source, 'function parseReplacesInput('),
            extract_function(source, 'function storeReplacesFlagHtml('),
            extract_function(source, 'function storeReplacesDetailHtml('),
            extract_function(source, 'function replaceWhereText('),
            extract_function(source, 'function renderReplaceDialogList('),
        ]) + r'''
assert.deepEqual(parseReplacesInput(' old-a, old-b，old-a、old-c  '), ['old-a', 'old-b', 'old-c']);
assert.deepEqual(parseReplacesInput(''), []);
const flagLocal = storeReplacesFlagHtml({replaces: ['old-a'], replaces_local: [{name: 'old-a', title: '旧版 A'}]});
assert.ok(flagLocal.includes('可替代本机 1 个旧版'));
assert.ok(flagLocal.includes('is-local'));
const detailHtml = storeReplacesDetailHtml({
  key: 'team:1:new-a', replaces: ['old-a'], replacement_note: '新版更稳',
  replaces_local: [{name: 'old-a', title: '旧版 A'}], local: {status: 'installed'},
});
assert.ok(detailHtml.includes('旧版 A（old-a）'));
assert.ok(detailHtml.includes('新版更稳'));
assert.ok(detailHtml.includes('data-store-replace-open="team:1:new-a"'));
const listHtml = renderReplaceDialogList([
  {name: 'old-a', title: '旧版 A', status: 'active', agent_names: ['Codex'], can_withdraw: true, can_delete: true, irreplaceable: true},
  {name: 'old-b', title: '旧版 B', status: 'active', agent_names: ['Claude'], can_withdraw: false, can_delete: false,
   blocked_reason: '「某技能」正在使用且依赖「旧版 B」', withdraw_reason: '依赖拦截', delete_reason: '依赖拦截'},
], new Map([['old-a', 'withdraw'], ['old-b', 'keep']]));
assert.ok(listHtml.includes('已启用：Codex'));
assert.ok(listHtml.includes('自制或已本地修改'));
assert.ok(listHtml.includes('正在使用且依赖'));
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
