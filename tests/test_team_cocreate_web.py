"""Web dashboard API and frontend rendering tests for single-skill co-creation (#243)."""
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


def make_home(home, hub_url, token, skills=()):
    for name, body in skills:
        skill_dir = home / '.claude/skills' / name
        skill_dir.mkdir(parents=True)
        (skill_dir / 'SKILL.md').write_text(body, encoding='utf-8')
    if skills:
        plan = store.preview(home, ['claude'])
        store.apply(plan['plan_id'], home, ['claude'])
    else:
        dummy = home / '.claude/skills/seed-skill'
        dummy.mkdir(parents=True)
        (dummy / 'SKILL.md').write_text('---\nname: seed-skill\ndescription: 初始\n---\n正文\n', encoding='utf-8')
        plan = store.preview(home, ['claude'])
        store.apply(plan['plan_id'], home, ['claude'])
    operations.bootstrap(home)
    team.save_settings(home, {'url': hub_url, 'key': token})


class CoCreateWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot cocreate web ')
        root = Path(self.temp.name)
        self.hub = team_hub.HubServer(root / 'hub data', host='127.0.0.1', port=0, open=False)
        threading.Thread(target=self.hub.serve_forever, daemon=True).start()
        self.hub_url = f'http://127.0.0.1:{self.hub.server_port}'
        self.alice = self.hub.store.add_member('Alice')
        self.bob = self.hub.store.add_member('Bob')
        self.alice_home = root / 'alice home'
        self.bob_home = root / 'bob home'
        make_home(self.alice_home, self.hub_url, self.alice['token'],
                  [('demo-skill', '---\nname: demo-skill\ndescription: 示例技能\n---\n原版正文\n')])
        make_home(self.bob_home, self.hub_url, self.bob['token'])
        team.publish(self.alice_home, ['demo-skill'], notes={'demo-skill': '原推荐语'}, confirmed=True)
        team.install_named(self.bob_home, 'demo-skill', confirmed=True)

    def tearDown(self):
        self.hub.shutdown()
        self.hub.server_close()
        self.temp.cleanup()

    def _start_dash(self, home):
        with patch.object(web_server, 'target_home', return_value=home):
            dash = web_server.Dashboard({'home': str(home), 'deferred': True})
        threading.Thread(target=dash.serve_forever, daemon=True).start()
        self.addCleanup(dash.server_close)
        self.addCleanup(dash.shutdown)
        return dash

    def _call(self, dash, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', dash.server_port, timeout=5)
        try:
            headers = {'Host': f'127.0.0.1:{dash.server_port}', 'X-Skill-Pilot-Token': dash.token}
            if method == 'POST':
                headers['Origin'] = dash.origin
                headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode('utf-8'))
        finally:
            conn.close()

    def test_dashboard_preview_submit_review_accept_and_readonly(self):
        bob_dash = self._start_dash(self.bob_home)
        alice_dash = self._start_dash(self.alice_home)

        card_key = f'team:{self.alice["id"]}:demo-skill'
        # Edit Bob's local copy.
        (self.bob_home / '.skill-pilot/store/demo-skill/SKILL.md').write_text(
            '---\nname: demo-skill\ndescription: 示例技能\n---\n优化后的正文\n', encoding='utf-8')
        status, preview = self._call(bob_dash, 'POST', '/api/team/suggestions/preview', {'card_key': card_key})
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview['review'])
        self.assertEqual(preview['summary']['modified'], ['SKILL.md'])

        # Read-only blocks submit.
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            status, _ = self._call(bob_dash, 'POST', '/api/team/suggestions/submit',
                                   {'review': preview['review'], 'message': '改进说明'})
            self.assertEqual(status, 403)

        status, created = self._call(bob_dash, 'POST', '/api/team/suggestions/submit',
                                     {'review': preview['review'], 'message': '优化正文表达'})
        self.assertEqual(status, 200, created)
        suggestion_id = created['id']

        # Alice sees incoming suggestion in catalog & list & detail.
        status, cat = self._call(alice_dash, 'GET', '/api/team/catalog')
        self.assertEqual(status, 200)
        self.assertEqual(cat['suggestions_summary']['incoming_total'], 1)
        status, listed = self._call(alice_dash, 'GET', '/api/team/suggestions?role=incoming&status=pending')
        self.assertEqual(status, 200)
        self.assertEqual([item['id'] for item in listed['suggestions']], [suggestion_id])
        status, detail = self._call(alice_dash, 'GET', '/api/team/suggestions/detail?id=' + urllib.parse.quote(suggestion_id))
        self.assertEqual(status, 200)
        self.assertEqual(detail['diff']['summary']['modified'], ['SKILL.md'])

        # Read-only blocks accept/reject.
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertEqual(self._call(alice_dash, 'POST', '/api/team/suggestions/accept', {'id': suggestion_id})[0], 403)
            self.assertEqual(self._call(alice_dash, 'POST', '/api/team/suggestions/reject', {'id': suggestion_id})[0], 403)

        # Alice accepts and publishes new version.
        status, accepted = self._call(alice_dash, 'POST', '/api/team/suggestions/accept', {
            'id': suggestion_id, 'review_note': '已采纳',
            'expected_current_version': detail['diff']['current_version'],
        })
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted['revision'], 2)

        # Bob's catalog sees no pending outgoing and preserved note on Alice's listing.
        status, cat_after = self._call(alice_dash, 'GET', '/api/team/catalog')
        self.assertEqual(cat_after['suggestions_summary']['incoming_total'], 0)
        card = next(c for c in cat_after['cards'] if c['key'] == card_key)
        self.assertEqual(card['listings'][0]['note'], '原推荐语')
        self.assertEqual(card['listings'][0]['revision'], 2)

        # Small skill stays visible on older dashboards (inline package, no blobs storage).
        conn = http.client.HTTPConnection(*self.hub.server_address, timeout=5)
        try:
            conn.request('GET', '/api/catalog', headers={'Authorization': 'Bearer ' + self.bob['token']})
            old_cat = json.loads(conn.getresponse().read().decode('utf-8'))
        finally:
            conn.close()
        self.assertEqual(len(old_cat['cards']), 1)

        # Author's listing_candidates marks behind_store=True and changed=False so 一键更新 doesn't revert v2.
        cand = next(item for item in team.listing_candidates(self.alice_home)['skills'] if item['name'] == 'demo-skill')
        self.assertTrue(cand['behind_store'])
        self.assertFalse(cand['changed'])

    def test_unmodified_v1_stale_guard_long_desc_and_control_sanitization(self):
        card_key = f'team:{self.alice["id"]}:demo-skill'
        # Alice publishes v2 while Bob still has unmodified v1 -> Bob cannot submit unmodified v1.
        (self.alice_home / '.skill-pilot/store/demo-skill/SKILL.md').write_text(
            '---\nname: demo-skill\ndescription: 示例技能\n---\n作者第2版\n', encoding='utf-8')
        team.publish(self.alice_home, ['demo-skill'], notes={'demo-skill': '原推荐语'}, confirmed=True)
        with self.assertRaisesRegex(ValueError, '本机还没有修改'):
            team.suggestion_preview(self.bob_home, card_key)

        # Bob edits v1 with long (>1500 char) description + ANSI control char in body -> preview marks stale=True.
        long_desc = '说明' * 800
        (self.bob_home / '.skill-pilot/store/demo-skill/SKILL.md').write_text(
            f'---\nname: demo-skill\ndescription: {long_desc}\n---\n协作者改版\x1b[2J\n', encoding='utf-8')
        saved, prev = team.suggestion_preview(self.bob_home, card_key)
        self.assertTrue(prev['stale'])
        self.assertNotIn('\x1b', prev['files'][0]['preview'])
        created = team.suggestion_submit(self.bob_home, saved, '补充长说明')
        # Accepting stale suggestion without expected_current_version is rejected (409).
        with self.assertRaises(team.RequestError) as ctx:
            team.suggestion_accept(self.alice_home, created['id'], allow_stale=True)
        self.assertEqual(ctx.exception.status, 409)
        # Accepting with matching expected_current_version succeeds and truncates description safely.
        detail = team.suggestion_detail(self.alice_home, created['id'])
        accepted = team.suggestion_accept(self.alice_home, created['id'], allow_stale=True,
                                          expected_current_version=detail['current_version'])
        self.assertEqual(accepted['revision'], 3)
        # Historical accepted detail preserves the accepted diff snapshot.
        after_detail = team.suggestion_detail(self.alice_home, created['id'])
        self.assertEqual(after_detail['diff']['summary']['modified'], ['SKILL.md'])

        # Secret file on existing published file is rejected before preview turns it into deletion.
        team.install(self.bob_home, card_key)  # already installed; update local file with secret
        (self.bob_home / '.skill-pilot/store/demo-skill/SKILL.md').write_text(
            '---\nname: demo-skill\ndescription: 示例\n---\nkey = sk-1234567890abcdef123456\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '密钥'):
            team.suggestion_preview(self.bob_home, card_key)

    def test_diff_renderer_escapes_html_and_uses_plain_chinese(self):
        app_js = (Path(__file__).resolve().parents[1] / 'skill_pilot/web/static/app.js').read_text(encoding='utf-8')
        start = app_js.index('function renderSuggestDiffLines(')
        end = app_js.index('let suggestDialogState = null;')
        snippet = app_js[start:end]
        script = f"""
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const number = value => String(value);
const icon = name => `<svg data-icon="${{name}}"></svg>`;
{snippet}
const html = suggestFilesHtml(
  {{added: ['scripts/run.sh'], modified: ['SKILL.md'], removed: [], unchanged_count: 1, has_scripts: true}},
  [{{path: 'SKILL.md', status: 'modified', is_script: false, mode_changed: false, binary: false, truncated: false,
     preview: '--- SKILL.md\\n+++ SKILL.md\\n@@ -1 +1 @@\\n-<script>bad()</script>\\n+<img src=x onerror=alert(1)>'}}],
  [{{path: '.env', reason: '文件名像账号或密钥文件，已拦下。'}}]
);
console.log(JSON.stringify({{html}}));
"""
        proc = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        html = json.loads(proc.stdout)['html']
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;', html)
        self.assertIn('新增 1 个文件', html)
        self.assertIn('修改 1 个文件', html)
        self.assertIn('已自动拦下 1 个疑似敏感文件', html)


if __name__ == '__main__':
    unittest.main()
