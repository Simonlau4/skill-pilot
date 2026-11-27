"""Lightweight team store discussions across skills, deliverables, and shared docs."""
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, store, team, team_hub
from skill_pilot.web import server as web_server
from tests import test_team_hub


class DiscussionHubTests(unittest.TestCase):
    Hub = test_team_hub.TeamHubTests
    setUp, close, request = Hub.setUp, Hub.close, Hub.request

    def _seed_targets(self):
        # 1) Team skill listed by first member
        skill_listing = test_team_hub.listing('demo-skill', 'files', note='推荐语')
        status, res = self.request('/api/listings/me', 'PUT', {'listings': [skill_listing]})
        self.assertEqual(status, 200, res)
        skill_key = res['listings'][0]['key']
        # Rate the skill first to confirm ratings stay completely untouched.
        status, _ = self.request('/api/ratings', 'PUT', {'key': skill_key, 'stars': 5, 'text': '很好用'},
                                 token=self.second['token'])
        self.assertEqual(status, 200)

        # 2) Deliverable published by first member
        html_bytes = b'<!doctype html><html><body>demo</body></html>'
        sha = __import__('hashlib').sha256(html_bytes).hexdigest()
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request('PUT', '/api/blobs/me/' + sha, body=html_bytes,
                         headers={'Authorization': 'Bearer ' + self.first['token'],
                                  'Content-Type': 'application/octet-stream'})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()
        status, deliv = self.request('/api/deliverables/me/case-1', 'PUT', {
            'title': '案例一', 'summary': '成品摘要', 'entry': 'index.html',
            'files': {'index.html': {'sha256': sha, 'size': len(html_bytes)}},
        })
        self.assertEqual(status, 200, deliv)
        deliv_key = deliv['key']

        # 3) Shared doc published by first member
        status, doc = self.request('/api/docs/me/doc-1', 'PUT', {
            'title': '分享纪要', 'category': 'Workshop 纪要', 'content': '# 纪要\n正文\n',
        })
        self.assertEqual(status, 200, doc)
        doc_key = doc['key']
        return skill_key, deliv_key, doc_key

    def test_post_reply_isolation_delete_auth_and_ratings_intact(self):
        skill_key, deliv_key, doc_key = self._seed_targets()
        third = self.store.add_member('Third')

        status, me = self.request('/api/me')
        self.assertEqual(status, 200)
        self.assertTrue(me.get('discussions_supported'))

        # Post on all three target types and verify strict isolation.
        s1, c_skill = self.request('/api/discussions', 'POST', {
            'target_type': 'skill', 'target_key': skill_key, 'body': '技能很好用，推荐搭配模板。',
        }, token=self.second['token'])
        self.assertEqual(s1, 200, c_skill)
        s2, c_deliv = self.request('/api/discussions', 'POST', {
            'target_type': 'deliverable', 'target_key': deliv_key, 'body': '成品结构很清晰！',
        }, token=self.second['token'])
        self.assertEqual(s2, 200, c_deliv)
        s3, c_doc = self.request('/api/discussions', 'POST', {
            'target_type': 'doc', 'target_key': doc_key, 'body': '请问第 2 部分的命令怎么跑？',
        }, token=self.second['token'])
        self.assertEqual(s3, 200, c_doc)

        # Single-level reply to doc comment by author (first)
        s4, c_reply = self.request('/api/discussions', 'POST', {
            'target_type': 'doc', 'target_key': doc_key,
            'body': '直接运行 skill-pilot team doc-list 即可。',
            'reply_to_id': c_doc['comment']['id'],
        })
        self.assertEqual(s4, 200, c_reply)
        self.assertEqual(c_reply['comment']['reply_to_id'], c_doc['comment']['id'])
        self.assertEqual(c_reply['comment']['reply_to_author'], self.second['name'])
        self.assertIn('第 2 部分', c_reply['comment']['reply_to_excerpt'])

        # Cross-target reply is rejected (cannot reply to skill comment from doc thread).
        self.assertEqual(self.request('/api/discussions', 'POST', {
            'target_type': 'doc', 'target_key': doc_key, 'body': '跨对象回复',
            'reply_to_id': c_skill['comment']['id'],
        })[0], 404)

        # Cross-type spoofing rejected.
        self.assertEqual(self.request('/api/discussions', 'POST', {
            'target_type': 'skill', 'target_key': doc_key, 'body': '伪造类型',
        })[0], 404)

        # Listing each target returns only its own comments; catalog reflects counts & keeps ratings untouched.
        _, d_list = self.request('/api/discussions?' + urllib.parse.urlencode({'target_type': 'doc', 'target_key': doc_key}))
        self.assertEqual(d_list['count'], 2)
        _, s_list = self.request('/api/discussions?' + urllib.parse.urlencode({'target_type': 'skill', 'target_key': skill_key}))
        self.assertEqual(s_list['count'], 1)
        _, cat = self.request('/api/catalog')
        self.assertEqual(cat['discussion_counts'], {skill_key: 1, deliv_key: 1, doc_key: 2})
        self.assertEqual(len(cat['cards'][0]['ratings']), 1)
        self.assertEqual(cat['cards'][0]['ratings'][0]['stars'], 5)

        # Delete permissions: third member forbidden (403); content owner (first) & comment author (second) allowed.
        self.assertEqual(self.request('/api/discussions/' + c_doc['comment']['id'], 'DELETE',
                                      token=third['token'])[0], 403)
        # First (doc owner) deletes second's comment; reply stays intact with reply_to_deleted=True.
        self.assertEqual(self.request('/api/discussions/' + c_doc['comment']['id'], 'DELETE')[0], 200)
        _, d_after = self.request('/api/discussions?' + urllib.parse.urlencode({'target_type': 'doc', 'target_key': doc_key}))
        self.assertEqual(d_after['count'], 1)
        self.assertTrue(d_after['comments'][0]['reply_to_deleted'])
        self.assertEqual(d_after['comments'][0]['reply_to_excerpt'], '')
        # Second deletes own comment on skill.
        self.assertEqual(self.request('/api/discussions/' + c_skill['comment']['id'], 'DELETE',
                                      token=self.second['token'])[0], 200)

    def test_limits_rate_limit_and_cascade_cleanup(self):
        skill_key, deliv_key, doc_key = self._seed_targets()

        # Length & control char validation
        for bad in ('', '   ', 'a' * (team_hub.MAX_DISCUSSION_TEXT + 1), 'bad\x00text'):
            self.assertEqual(self.request('/api/discussions', 'POST', {
                'target_type': 'doc', 'target_key': doc_key, 'body': bad,
            })[0], 400)

        # Short-term rate limit (5 posts within 60s -> 6th returns 429)
        with patch.object(team_hub, 'DISCUSSION_RATE_LIMIT', 2):
            for idx in range(2):
                self.assertEqual(self.request('/api/discussions', 'POST', {
                    'target_type': 'doc', 'target_key': doc_key, 'body': f'第 {idx + 1} 条',
                })[0], 200)
            self.assertEqual(self.request('/api/discussions', 'POST', {
                'target_type': 'doc', 'target_key': doc_key, 'body': '太快了',
            })[0], 429)

        # Per-target cap & hub total cap
        with patch.object(team_hub, 'DISCUSSION_RATE_LIMIT', 50):
            with patch.object(team_hub, 'MAX_DISCUSSIONS_PER_TARGET', 2):
                self.assertEqual(self.request('/api/discussions', 'POST', {
                    'target_type': 'doc', 'target_key': doc_key, 'body': '超单对象上限',
                }, token=self.second['token'])[0], 413)
            with patch.object(team_hub, 'MAX_DISCUSSIONS_TOTAL', 2):
                self.assertEqual(self.request('/api/discussions', 'POST', {
                    'target_type': 'skill', 'target_key': skill_key, 'body': '超全站上限',
                }, token=self.second['token'])[0], 413)

            # Second posts on skill and deliverable, first replies to second's skill comment.
            _, s_post = self.request('/api/discussions', 'POST', {
                'target_type': 'skill', 'target_key': skill_key, 'body': '二号成员留言',
            }, token=self.second['token'])
            _, s_reply = self.request('/api/discussions', 'POST', {
                'target_type': 'skill', 'target_key': skill_key, 'body': '一号回复二号',
                'reply_to_id': s_post['comment']['id'],
            })
            self.assertEqual(s_reply['count'], 2)
            self.assertEqual(self.request('/api/discussions', 'POST', {
                'target_type': 'deliverable', 'target_key': deliv_key, 'body': '成品留言',
            }, token=self.second['token'])[0], 200)

            # Removing doc cleans up its discussion thread.
            self.assertEqual(self.request('/api/docs/me/doc-1', 'DELETE')[0], 200)
            self.assertNotIn(doc_key, self.store._discussions())

            # Removing deliverable cleans up its discussion thread.
            self.assertEqual(self.request('/api/deliverables/me/case-1', 'DELETE')[0], 200)
            self.assertNotIn(deliv_key, self.store._discussions())

            # remove_member removes second's comments & scrubs reply quote pointing to them.
            self.store.remove_member(self.second['id'])
            _, after_rm = self.request('/api/discussions?' + urllib.parse.urlencode({'target_type': 'skill', 'target_key': skill_key}))
            self.assertEqual(after_rm['count'], 1)
            self.assertTrue(after_rm['comments'][0]['reply_to_deleted'])
            self.assertEqual(after_rm['comments'][0]['reply_to_excerpt'], '')

            # Unlisting skill cleans up its remaining thread.
            self.assertEqual(self.request('/api/listings/me/demo-skill', 'DELETE')[0], 200)
            self.assertEqual(self.store._discussions(), {})

    def test_r1_edge_cases_shared_skill_auth_corrupt_isolation_and_restart_ratelimit(self):
        # 1) Late recommender of a shared GitHub skill cannot delete earlier comments;
        #    unlisting one alias keeps discussions while second alias stays listed;
        #    long (>300 chars) valid GitHub path works.
        long_path = '/'.join(['deepfolder'] * 28)
        gh1 = test_team_hub.listing('gh-a', 'github', source={'kind': 'github', 'repository': 'owner/repo', 'path': long_path})
        gh2 = test_team_hub.listing('gh-b', 'github', source={'kind': 'github', 'repository': 'owner/repo', 'path': long_path})
        status, res = self.request('/api/listings/me', 'PUT', {'listings': [gh1, gh2]})
        self.assertEqual(status, 200, res)
        gh_key = res['listings'][0]['key']
        self.assertGreater(len(gh_key), 300)
        third = self.store.add_member('Third')
        s_post, c_early = self.request('/api/discussions', 'POST', {
            'target_type': 'skill', 'target_key': gh_key, 'body': '早期留言',
        }, token=third['token'])
        self.assertEqual(s_post, 200, c_early)
        # Second recommends the same GitHub skill AFTER third posted -> must NOT be able to delete third's comment!
        self.assertEqual(self.request('/api/listings/me', 'PUT', {'listings': [gh1]}, token=self.second['token'])[0], 200)
        self.assertEqual(self.request('/api/discussions/' + c_early['comment']['id'], 'DELETE',
                                      token=self.second['token'])[0], 403)
        # Unlisting gh-a while first still lists gh-b (and second lists gh-a) keeps discussions intact.
        self.assertEqual(self.request('/api/listings/me/gh-a', 'DELETE')[0], 200)
        _, listed = self.request('/api/discussions?' + urllib.parse.urlencode({'target_type': 'skill', 'target_key': gh_key}))
        self.assertEqual(listed['count'], 1)

        # 2) Rate limit survives BOTH deletion and hub restart (clears actual server store rate dict).
        with patch.object(team_hub, 'DISCUSSION_RATE_LIMIT', 1):
            self.assertEqual(self.request('/api/discussions/' + c_early['comment']['id'], 'DELETE',
                                          token=third['token'])[0], 200)
            self.server.store._discussion_rate = {}
            self.assertEqual(self.request('/api/discussions', 'POST', {
                'target_type': 'skill', 'target_key': gh_key, 'body': '删除后重启紧接着发言',
            }, token=third['token'])[0], 429)

        # 3) Corrupt discussions.json (both bad JSON and partially invalid target value)
        #    does NOT break /api/catalog, /api/ratings, or remove_member.
        (self.data / 'discussions.json').write_text('{"targets": {"bad": 1}}', encoding='utf-8')
        self.assertEqual(self.request('/api/catalog')[0], 200)
        self.assertEqual(self.request('/api/ratings', 'PUT', {'key': gh_key, 'stars': 4}, token=third['token'])[0], 200)
        (self.data / 'discussions.json').write_text('{corrupt json', encoding='utf-8')
        self.assertEqual(self.request('/api/catalog')[0], 200)
        self.store.remove_member(third['id'])
        # Corrupt file preserved for admin recovery.
        self.assertEqual((self.data / 'discussions.json').read_text(encoding='utf-8'), '{corrupt json')


class DiscussionWebAndSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot disc ')
        root = Path(self.temp.name)
        self.hub = team_hub.HubServer(root / 'hub data', host='127.0.0.1', port=0, open=False)
        self.hub_thread = threading.Thread(target=self.hub.serve_forever, daemon=True)
        self.hub_thread.start()
        self.hub_url = f'http://127.0.0.1:{self.hub.server_port}'
        self.alice = self.hub.store.add_member('Alice')
        self.bob = self.hub.store.add_member('Bob')
        self.alice_home = root / 'alice home'
        self.bob_home = root / 'bob home'
        for home, member in ((self.alice_home, self.alice), (self.bob_home, self.bob)):
            skill_dir = home / '.claude/skills/demo-skill'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text('---\nname: demo-skill\ndescription: 示例\n---\n正文\n', encoding='utf-8')
            plan = store.preview(home, ['claude'])
            store.apply(plan['plan_id'], home, ['claude'])
            operations.bootstrap(home)
            team.save_settings(home, {'url': self.hub_url, 'key': member['token']})

    def tearDown(self):
        self.hub.shutdown()
        self.hub.server_close()
        self.temp.cleanup()

    def test_dashboard_routes_readonly_and_old_hub_fallback(self):
        saved_doc = team.publish_doc(self.alice_home, identity='d1', title='分享文档', content='# 标题\n正文', confirmed=True)
        doc_key = saved_doc['key']

        with patch.object(web_server, 'target_home', return_value=self.bob_home):
            dash = web_server.Dashboard({'home': str(self.bob_home), 'deferred': True})
        t = threading.Thread(target=dash.serve_forever, daemon=True)
        t.start()
        try:
            def call(method, path, body=None):
                conn = http.client.HTTPConnection('127.0.0.1', dash.server_port, timeout=5)
                try:
                    headers = {'Host': f'127.0.0.1:{dash.server_port}', 'X-Skill-Pilot-Token': dash.token}
                    if method == 'POST':
                        headers['Origin'] = dash.origin
                        headers['Content-Type'] = 'application/json'
                    data = json.dumps(body).encode() if body is not None else None
                    conn.request(method, path, body=data, headers=headers)
                    resp = conn.getresponse()
                    return resp.status, json.loads(resp.read())
                finally:
                    conn.close()

            status, cat = call('GET', '/api/team/catalog')
            self.assertEqual(status, 200)
            self.assertTrue(cat['discussions_supported'])

            # Post and list via dashboard API
            status, posted = call('POST', '/api/team/discussions/post', {
                'target_type': 'doc', 'target_key': doc_key, 'body': '赞！<script>alert(1)</script>',
                'expected_connection': cat['connection_id'],
            })
            self.assertEqual(status, 200, posted)
            cid = posted['comment']['id']

            status, listed = call('GET', '/api/team/discussions?target_type=doc&target_key=' + urllib.parse.quote(doc_key))
            self.assertEqual(status, 200)
            self.assertEqual(listed['count'], 1)
            self.assertTrue(listed['comments'][0]['can_delete'])

            # Read-only mode blocks post & delete while keeping GET readable.
            with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
                self.assertEqual(call('GET', '/api/team/discussions?target_type=doc&target_key=' + urllib.parse.quote(doc_key))[0], 200)
                self.assertEqual(call('POST', '/api/team/discussions/post', {
                    'target_type': 'doc', 'target_key': doc_key, 'body': '只读测试',
                })[0], 403)
                self.assertEqual(call('POST', '/api/team/discussions/delete', {'id': cid})[0], 403)

            # Connection mismatch blocked
            self.assertEqual(call('POST', '/api/team/discussions/post', {
                'target_type': 'doc', 'target_key': doc_key, 'body': '错连接',
                'expected_connection': 'deadbeef00000000',
            })[0], 409)

            # Delete succeeds
            self.assertEqual(call('POST', '/api/team/discussions/delete', {'id': cid})[0], 200)
        finally:
            dash.shutdown()
            dash.server_close()

        # Old hub compatibility: /api/me lacks discussions_supported -> graceful fallback
        orig_request = team._request
        def old_hub(config, path, method='GET', data=None):
            res = orig_request(config, path, method, data)
            if path == '/api/me':
                res = {k: v for k, v in res.items() if k != 'discussions_supported'}
            elif path == '/api/catalog':
                res = {k: v for k, v in res.items() if k != 'discussion_counts'}
            return res

        with patch.object(team, '_request', side_effect=old_hub):
            cat = team.catalog(self.bob_home)
            self.assertFalse(cat['discussions_supported'])
            listed = team.discussions_list(self.bob_home, 'doc', doc_key)
            self.assertFalse(listed['discussions_supported'])
            self.assertEqual(listed['comments'], [])
            with self.assertRaisesRegex(ValueError, '分享站版本较旧'):
                team.discussion_post(self.bob_home, 'doc', doc_key, '旧站测试')

    def test_frontend_discussion_renderer_escapes_xss(self):
        app_js = (Path(__file__).resolve().parents[1] / 'skill_pilot/web/static/app.js').read_text(encoding='utf-8')
        start = app_js.index('const discussionCache = new Map();')
        end = app_js.index('async function ensureDiscussions(')
        snippet = app_js[start:end]
        script = f"""
const escape = v => String(v ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const number = n => String(n);
const friendlyDate = s => s || '';
const teamReadOnly = () => false;
const state = {{store: {{discussions_supported: true, discussion_counts: {{}}}}}};
const $ = () => null;
{snippet}
const entry = getDiscussionEntry('doc', 'doc:aaa:d1');
entry.loaded = true;
entry.comments = [{{
  id: '1'.repeat(32),
  author: {{id: '2'.repeat(32), name: '<b>Alice</b>'}},
  body: '<script>alert(1)</script><img src=x onerror=alert(2)>',
  reply_to_id: '3'.repeat(32),
  reply_to_author: '<svg onload=alert(3)>',
  reply_to_excerpt: '"><script>x</script>',
  reply_to_deleted: false,
  created_at: '2026-11-26',
  can_delete: true,
}}];
console.log(discussionMountHtml('doc', 'doc:aaa:d1'));
"""
        proc = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        html = proc.stdout
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img src=x', html)
        self.assertNotIn('<svg onload', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('&lt;b&gt;Alice&lt;/b&gt;', html)
        self.assertIn('&lt;svg onload=alert(3)&gt;', html)


if __name__ == '__main__':
    unittest.main()
