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
        for bad in (None, 'old-a', 123, [123], [''], ['   '], ['../bad'], ['has space'], ['a' * 65],
                    [f'old-{i}' for i in range(21)]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                team_hub.clean_replaces(bad)
        with self.assertRaises(ValueError):
            team.parse_replaces(None)
        with self.assertRaises(ValueError):
            team_hub.short_text('含非法代理\ud800字符', '替代说明')
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

        # Old dashboard omitting replaces/replacement_note preserves existing values on update (#236 R1 #2)
        status, _ = self.put([listing('new-tool', 'files', replaces=['old-tool'], replacement_note='保留说明')])
        self.assertEqual(status, 200)
        status, _ = self.put([listing('new-tool', 'files', note='旧版看板更新推荐语')])
        self.assertEqual(status, 200)
        by_name = {c['listings'][0]['name']: c['listings'][0] for c in self.cards()}
        self.assertEqual(by_name['new-tool']['replaces'], ['old-tool'])
        self.assertEqual(by_name['new-tool']['replacement_note'], '保留说明')
        self.assertEqual(by_name['new-tool']['note'], '旧版看板更新推荐语')
        # Clear again so unpublish linkage test below starts clean
        status, _ = self.put([listing('new-tool', 'files', replaces=[])])
        self.assertEqual(status, 200)
        by_name = {c['listings'][0]['name']: c['listings'][0] for c in self.cards()}

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
        self.assertTrue(done['replace_review'])

        # Old skill MUST still be present and enabled on claude (never silently deleted/withdrawn!)
        reg = operations.load(self.other)[1]['skills']
        self.assertIn('acme-local', reg)
        self.assertIn('acme-other', reg)
        self.assertEqual(reg['acme-other']['assigned_agents'], ['claude'])
        self.assertTrue((self.other / '.claude/skills/acme-other').exists())

        # Endpoint rejects missing revision, missing/invalid review, or unapproved arbitrary target (#236 R1 #3)
        status, body = self.request(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'withdraw'}, 'review': done['replace_review'], 'confirmed': True,
        })
        self.assertEqual(status, 409, body)
        status, body = self.request(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'withdraw'}, 'revision': done['replace_revision'], 'confirmed': True,
        })
        self.assertEqual(status, 409, body)
        status, body = self.request(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-local': 'delete'}, 'revision': done['replace_revision'],
            'review': done['replace_review'], 'confirmed': True,
        })
        self.assertEqual(status, 409, body)

        # User chooses Keep -> zero writes, zero operations
        kept = self.ok(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'keep'}, 'revision': done['replace_revision'],
            'review': done['replace_review'], 'confirmed': True,
        })
        self.assertEqual(kept['operations'], [])
        self.assertEqual(operations.load(self.other)[1]['skills']['acme-other']['assigned_agents'], ['claude'])

        # User chooses Withdraw -> withdraws from claude, recorded in log, undo restores completely
        withdrawn = self.ok(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'withdraw'}, 'revision': done['replace_revision'],
            'review': done['replace_review'], 'confirmed': True,
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

        # Fresh verified card preview -> User chooses Delete -> removes from store & claude, undo restores completely
        preview = self.ok(self.b, '/api/team/replace/preview', {'card_key': card['key']})
        deleted = self.ok(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'delete'}, 'revision': preview['revision'],
            'review': preview['review'], 'confirmed': True,
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

        # Install a replacement card first so /api/team/replace/preview is bound to an installed card
        card = self.publish_with_replaces('acme-local', ['acme-other'], '替代旧版')
        # Preview before installing must reject (#236 R1 #5)
        status, body = self.request(self.b, '/api/team/replace/preview', {'card_key': card['key']})
        self.assertEqual(status, 409, body)
        self.assertIn('先安装', body['error'])
        team.install(self.other, card['key'])

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
        dep_preview = self.ok(self.b, '/api/team/replace/preview', {'card_key': card['key']})
        for action in ('withdraw', 'delete'):
            status, body = self.request(self.b, '/api/team/replace/cleanup', {
                'actions': {'acme-other': action}, 'revision': dep_preview['revision'],
                'review': dep_preview['review'], 'confirmed': True,
            })
            self.assertEqual(status, 409, body)
            self.assertIn('依赖', body['error'])
        self.assertIn('acme-other', operations.load(self.other)[1]['skills'])

        # Read-only mode blocking
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            preview = self.ok(self.b, '/api/team/replace/preview', {'card_key': card['key']})
            self.assertFalse(preview['write_allowed'])
            self.assertFalse(preview['items'][0]['can_withdraw'])
            self.assertFalse(preview['items'][0]['can_delete'])
            self.assertIn('只读', preview['items'][0]['blocked_reason'])
            status, body = self.request(self.b, '/api/team/replace/cleanup', {
                'actions': {'acme-other': 'delete'}, 'revision': preview['revision'],
                'review': preview['review'], 'confirmed': True,
            })
            self.assertEqual(status, 403, body)
            self.assertIn('只读', body['error'])
            with self.assertRaisesRegex(ValueError, '只读'):
                team.cleanup_replaced(self.other, {'acme-other': 'withdraw'}, confirmed=True)

        # Stale revision rejection
        self.registry(self.other, lambda skills: skills.pop('acme-extra', None))
        fresh_preview = self.ok(self.b, '/api/team/replace/preview', {'card_key': card['key']})
        status, body = self.request(self.b, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'withdraw'}, 'revision': rev + '-stale',
            'review': fresh_preview['review'], 'confirmed': True,
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

    def test_same_origin_rename_does_not_delete_users_only_copy(self):
        # R1 #1: local acme-other shares GitHub (repository, path) with renamed store listing acme-other-v2
        self.configure()
        renamed = {
            'name': 'acme-other-v2', 'requires': [], 'replaces': ['acme-other'], 'replacement_note': '更名新版',
            'source': {'kind': 'github', 'repository': 'acme/skills', 'path': 'skills/acme-other'},
        }
        self.raw_put([renamed])
        cat = team.catalog(self.other)
        card = next(c for c in cat['cards'] if c['name'] == 'acme-other-v2')
        self.assertEqual(card['local']['status'], 'installed')
        self.assertNotIn('replaces_local', card)
        found = team.find(self.other, 'acme-other-v2')
        self.assertNotIn('replaced_local', found)
        with self.assertRaisesRegex(ValueError, '不在本次检测到的本机旧版技能中'):
            team.install_named(self.other, 'acme-other-v2', confirmed=True, replace_actions=['acme-other=delete'])
        self.assertIn('acme-other', operations.load(self.other)[1]['skills'])

    def test_different_authors_replaces_not_merged_on_same_card(self):
        # R1 #6: member A and member B both list same GitHub skill with different replaces
        self.configure()
        row_a = {
            'name': 'acme-extra', 'requires': [], 'replaces': ['old-from-a'], 'replacement_note': 'A 的说明',
            'source': {'kind': 'github', 'repository': 'acme/skills', 'path': 'skills/acme-extra'},
        }
        row_b = {
            'name': 'acme-extra', 'requires': [], 'replaces': ['acme-other'], 'replacement_note': 'B 的说明',
            'source': {'kind': 'github', 'repository': 'acme/skills', 'path': 'skills/acme-extra'},
        }
        # Put B first (older), then A (latest -> root listing)
        with patch.object(team_hub, 'now', return_value='2026-10-01T00:00:00+00:00'):
            self.raw_put([row_b], self.second)
        with patch.object(team_hub, 'now', return_value='2026-10-02T00:00:00+00:00'):
            self.raw_put([row_a], self.first)
        cat = team.catalog(self.other)
        card = next(c for c in cat['cards'] if c['name'] == 'acme-extra')
        self.assertEqual(card['replaces'], ['old-from-a'])
        self.assertEqual(card.get('replacement_note'), 'A 的说明')
        self.assertNotIn('replaces_local', card)
        # Explicitly selecting author A must NOT include B's acme-other
        found_a = team.find(self.other, 'acme-extra', author='示例成员 A')
        self.assertEqual(found_a.get('replaces'), ['old-from-a'])
        self.assertNotIn('replaced_local', found_a)

    def test_transitive_required_dependency_replaces_detected_on_install(self):
        # R1 #7: acme-main requires acme-base; acme-base declares replaces=['acme-other']
        self.configure()
        preview = self.ok(self.a, '/api/team/listings/preview', {
            'skills': [
                {'name': 'acme-base', 'note': '基础新版', 'replaces': ['acme-other'], 'replacement_note': '依赖替代旧版'},
                {'name': 'acme-main', 'note': '主技能'},
            ],
        })
        self.ok(self.a, '/api/team/listings/apply', {'review': preview['review']})
        # CLI find on acme-main pulls in acme-base and detects acme-other locally on B
        found = team.find(self.other, 'acme-main')
        self.assertEqual(found['installed'], ['acme-base', 'acme-main'])
        self.assertEqual([i['name'] for i in found['replaced_local']], ['acme-other'])
        # Web queue install on B also detects acme-other from transitive dependency acme-base
        card = next(c for c in self.ok(self.b, '/api/team/catalog')['cards'] if c['name'] == 'acme-main')
        self.assertEqual([i['name'] for i in card['replaces_local']], ['acme-other'])
        self.ok(self.b, '/api/team/queue', {'key': card['key']})
        done = self.wait_queue(self.b, card['key'])
        self.assertEqual(done['status'], 'done')
        self.assertEqual([i['name'] for i in done['replaced_local']], ['acme-other'])
        self.assertTrue(done['replace_review'])

    def test_batch_unpublish_prevalidates_replaces_cap_without_partial_delete(self):
        # R1 #10: target already has 19 replaces; batch unpublishing 2 skills must fail BEFORE deleting either
        self.configure()
        nineteen = [f'legacy-{i}' for i in range(19)]
        self.raw_put([
            listing('target-new', 'files', replaces=nineteen),
            listing('old-x', 'files'),
            listing('old-y', 'files'),
        ])
        with self.assertRaisesRegex(ValueError, '最多标记 20 个'):
            team.listings_remove(self.home, ['old-x', 'old-y'], confirmed=True, replaced_by='target-new')
        owned = team._owned(team._fetch(self.home)[2], self.first['id'])
        self.assertIn('old-x', owned)
        self.assertIn('old-y', owned)
        self.assertEqual(len(owned['target-new']['replaces']), 19)

    def test_cleanup_replaced_enforces_locked_active_dependent_check(self):
        # R1 #4: even if a dependent is enabled right before lock acquisition, locked_check blocks atomically
        self.configure()
        card = self.publish_with_replaces('acme-local', ['acme-other'], '替代旧版')
        team.install(self.other, card['key'])
        rev = operations.revision(self.other, operations.load(self.other)[2])
        # Simulate concurrent process adding a dependent skill between preview and locked execution
        real_run = operations._run
        def inject_concurrent_dependent(home, registry, agents, expected, message, mutation, **kwargs):
            shutil.copytree(self.repository / 'skills/acme-extra', self.other / '.skill-pilot/store/acme-extra')
            shutil.copytree(self.repository / 'skills/acme-extra', self.other / '.skill-pilot/bases/acme-extra')
            self.registry(self.other, lambda skills: skills.update({
                'acme-extra': {**skills['acme-other'], 'requires': ['acme-other'], 'assigned_agents': ['claude'], 'entries': []}
            }))
            return real_run(home, registry, agents, expected, message, mutation, **kwargs)
        with patch.object(operations, '_run', side_effect=inject_concurrent_dependent):
            with self.assertRaisesRegex(ValueError, '目录内容已经变了|依赖'):
                team.cleanup_replaced(self.other, {'acme-other': 'delete'}, confirmed=True,
                                      expected=rev, allowed=['acme-other'], required_present=['acme-local'])
        self.assertIn('acme-other', operations.load(self.other)[1]['skills'])

    def test_r2_surgical_fixes_revision_binding_update_gate_author_install_and_partial_undo(self):
        self.configure()
        # 1. Outdated local copy (status == 'update') blocks cleanup preview & find
        card = self.publish_with_replaces('acme-local', ['acme-other'], '第一版说明')
        team.install(self.other, card['key'])
        # Author publishes v2 with new files and updated replaces -> B now has status='update'
        skill_md = self.home / '.skill-pilot/store/acme-local/SKILL.md'
        skill_md.write_text(skill_md.read_text() + '\nv2 内容\n')
        self.publish_with_replaces('acme-local', ['acme-other'], '第二版说明')
        status, body = self.request(self.b, '/api/team/replace/preview', {'card_key': card['key']})
        self.assertEqual(status, 409, body)
        self.assertIn('先更新到新版', body['error'])
        found_outdated = team.find(self.other, 'acme-local')
        self.assertNotIn('replaced_local', found_outdated)

        # 2. Server verifies review entry['revision'] == client revision (R2 #1)
        # Re-publish v1-like or reinstall so B is current; easiest: test on A where status == 'mine'
        prev_a = self.ok(self.a, '/api/team/replace/preview', {'card_key': card['key']})
        self.assertTrue(prev_a['review'])
        status, body = self.request(self.a, '/api/team/replace/cleanup', {
            'actions': {'acme-other': 'withdraw'}, 'revision': 'mismatched-rev',
            'review': prev_a['review'], 'confirmed': True,
        })
        self.assertEqual(status, 409, body)
        self.assertIn('状态已经变了', body['error'])

        # 3. _check_replace_live verifies replacement store directory exists (R2 #3)
        missing_dir = self.other / '.skill-pilot/store/acme-local'
        backup_dir = self.other / '.skill-pilot/store/acme-local-tmp-bak'
        shutil.move(missing_dir, backup_dir)
        try:
            with self.assertRaisesRegex(ValueError, '尚未独立保存'):
                team.cleanup_replaced(self.other, {'acme-other': 'delete'}, confirmed=True,
                                      allowed=['acme-other'], required_present=['acme-local'])
        finally:
            shutil.move(backup_dir, missing_dir)

        # 4. One-click update_listing omits replaces from PUT so concurrent server edits stay intact (R2 #6)
        skill_md.write_text(skill_md.read_text() + '\nv3 内容\n')
        conn = team._connection_version(team._connection(self.home))
        real_apply = team.listings_apply
        captured_payloads = []
        def spy_apply(home_arg, review_arg):
            captured_payloads.append(copy.deepcopy(review_arg['listings']))
            return real_apply(home_arg, review_arg)
        with patch.object(team, 'listings_apply', side_effect=spy_apply):
            team.update_listing(self.home, 'acme-local', conn)
        self.assertEqual(len(captured_payloads), 1)
        self.assertNotIn('replaces', captured_payloads[0][0])
        self.assertNotIn('replacement_note', captured_payloads[0][0])
        # Hub still preserved replaces & replacement_note
        refreshed = next(c for c in self.ok(self.b, '/api/team/catalog')['cards'] if c['name'] == 'acme-local')
        self.assertEqual(refreshed['replaces'], ['acme-other'])
        self.assertEqual(refreshed['replacement_note'], '第二版说明')

        # 5. CLI install_named --author A installs A's listing when B's is newer on same card (R2 #2)
        row_a = {
            'name': 'acme-extra', 'requires': [], 'replaces': ['acme-other'], 'replacement_note': 'A 版',
            'source': {'kind': 'github', 'repository': 'acme/skills', 'path': 'skills/acme-extra',
                       'files': [test_team_hub.file('SKILL.md', '---\nname: acme-extra\ndescription: A版\n---\nFROM_AUTHOR_A\n')]},
        }
        row_b = {
            'name': 'acme-extra', 'requires': [], 'replaces': [],
            'source': {'kind': 'github', 'repository': 'acme/skills', 'path': 'skills/acme-extra',
                       'files': [test_team_hub.file('SKILL.md', '---\nname: acme-extra\ndescription: B版\n---\nFROM_AUTHOR_B\n')]},
        }
        with patch.object(team_hub, 'now', return_value='2026-10-01T00:00:00+00:00'):
            self.raw_put([row_a], self.first)
        with patch.object(team_hub, 'now', return_value='2026-10-02T00:00:00+00:00'):
            self.raw_put([row_b], self.second)
        called_member = []
        real_install = team.install
        def spy_install(home_arg, key_arg, member_id=None):
            called_member.append(member_id)
            return real_install(home_arg, key_arg, member_id=member_id)
        with patch.object(team, 'install', side_effect=spy_install):
            res_author = team.install_named(self.other, 'acme-extra', author='示例成员 A', confirmed=True)
        self.assertEqual(called_member, [self.first['id']])
        self.assertEqual(res_author['author_id'], self.first['id'])
        self.assertIn('FROM_AUTHOR_A', (self.other / '.skill-pilot/store/acme-extra/SKILL.md').read_text())

        # 6. Mixed withdraw + delete surfaces step-1 op ID if step 2 fails (R2 #7)
        # Set up two old skills on A (acme-extra & acme-other) and force step 2 delete to fail
        with patch.object(cleanup, 'delete', side_effect=ValueError('模拟第二步失败')):
            with self.assertRaisesRegex(ValueError, '已先撤下 1 个旧版技能.*编号.*模拟第二步失败') as ctx:
                team.cleanup_replaced(self.home, {'acme-other': 'withdraw', 'acme-extra': 'delete'}, confirmed=True)
        # Step 1 op ID from error message is undoable!
        op_match = re.search(r'编号 ([0-9a-f]+)', str(ctx.exception))
        self.assertIsNotNone(op_match)
        operations.undo(op_match.group(1), home=self.home)
        self.assertFalse(operations.load(self.home)[1]['skills']['acme-other'].get('withdrawn'))


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
// R1 #5 & R2 #3: installable/update/blocked/conflict/lark must NOT show 清理旧版 button
for (const status of ['installable', 'update', 'blocked', 'conflict', 'lark']) {
  const blockedHtml = storeReplacesDetailHtml({
    key: 'team:1:new-a', replaces: ['old-a'],
    replaces_local: [{name: 'old-a', title: '旧版 A'}], local: {status},
  });
  assert.ok(!blockedHtml.includes('data-store-replace-open'), status + ' must not show cleanup button');
}
// R1 #8 & R2 #1: checkReplacedJobs enqueues one prompt per job with its own revision, review & note
const sessionStore = new Map();
global.sessionStorage = {getItem: k => sessionStore.get(k) || null, setItem: (k, v) => sessionStore.set(k, v)};
const prompts = [];
const enqueueReplacePrompt = payload => prompts.push(payload);
''' + '\n'.join([
            'const REPLACE_SEEN_KEY = "sp-replace-seen-jobs";',
            'const seenReplaceJobs = new Set();',
            extract_function(source, 'function markReplaceJobSeen('),
            extract_function(source, 'function checkReplacedJobs('),
        ]) + r'''
checkReplacedJobs([
  {id: 'fast-1', kind: 'team_install', status: 'done', title: '新版 1', replacement_note: '说明 1',
   replaced_local: [{name: 'old-a', title: '旧版 A'}], replace_revision: 'rev1', replace_review: 'r1'},
  {id: 'fast-2', kind: 'team_install', status: 'done', title: '新版 2', replacement_note: '说明 2',
   replaced_local: [{name: 'old-b', title: '旧版 B'}], replace_revision: 'rev2', replace_review: 'r2'},
]);
assert.equal(prompts.length, 2);
assert.equal(prompts[0].review, 'r1');
assert.equal(prompts[0].note, '说明 1');
assert.equal(prompts[1].review, 'r2');
assert.equal(prompts[1].note, '说明 2');
// Second poll for same jobs must not re-open dialogs
checkReplacedJobs([{id: 'fast-1', kind: 'team_install', status: 'done', title: '新版 1',
  replaced_local: [{name: 'old-a', title: '旧版 A'}], replace_revision: 'rev1', replace_review: 'r1'}]);
assert.equal(prompts.length, 2);
const listHtml = renderReplaceDialogList([
  {name: 'old-a', title: '旧版 A', status: 'active', agent_names: ['Codex'], can_withdraw: true, can_delete: true, irreplaceable: true},
  {name: 'old-b', title: '旧版 B', status: 'active', agent_names: ['Claude'], can_withdraw: false, can_delete: false,
   blocked_reason: '「某技能」正在使用且依赖「旧版 B」', withdraw_reason: '依赖拦截', delete_reason: '依赖拦截'},
], new Map([['old-a', 'withdraw'], ['old-b', 'keep']]));
assert.ok(listHtml.includes('已启用：Codex'));
assert.ok(listHtml.includes('自制或已本地修改'));
assert.ok(listHtml.includes('正在使用且依赖'));
// R2 #4: switching replaced_by in unlist dialog must not carry over A's untouched prefilled note to B
const els = {
  'store-unlist-replaced-by': {value: ''},
  'store-unlist-note-wrap': {hidden: true},
  'store-unlist-note': {value: '', placeholder: ''},
};
const $ = id => els[id];
let unlistDialogState = {
  others: [
    {name: 'new-a', replacement_note: 'A 的说明'},
    {name: 'new-b', replacement_note: ''},
  ],
  existingLinked: [{name: 'new-a', replacement_note: 'A 的说明'}],
  noteDirty: false,
};
''' + extract_function(source, 'function syncUnlistDialogNote(') + r'''
syncUnlistDialogNote();
assert.equal(els['store-unlist-note-wrap'].hidden, false);
assert.equal(els['store-unlist-note'].value, 'A 的说明');
assert.equal(unlistDialogState.noteDirty, false);
// Switch dropdown from "" (linked A) to new-b -> resets untouched note to B's note ("")
els['store-unlist-replaced-by'].value = 'new-b';
syncUnlistDialogNote();
assert.equal(els['store-unlist-note'].value, '');
assert.equal(unlistDialogState.noteDirty, false);
// User edits note -> noteDirty becomes true and switching dropdown keeps user's text
els['store-unlist-note'].value = '自定义说明';
unlistDialogState.noteDirty = true;
els['store-unlist-replaced-by'].value = 'new-a';
syncUnlistDialogNote();
assert.equal(els['store-unlist-note'].value, '自定义说明');
// When no existing link and dropdown cleared -> hides note and resets noteDirty
unlistDialogState.existingLinked = [];
els['store-unlist-replaced-by'].value = '';
syncUnlistDialogNote();
assert.equal(els['store-unlist-note-wrap'].hidden, true);
assert.equal(els['store-unlist-note'].value, '');
assert.equal(unlistDialogState.noteDirty, false);
'''
        self.assertIn('const sendNote = Boolean(rawNote && unlistDialogState?.noteDirty);', source)
        self.assertIn('maxlength="1400"', source)
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
