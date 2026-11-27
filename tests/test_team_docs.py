"""Shared team documents: hub storage, CLI, local dashboard API, and safe Markdown rendering."""
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, store, team, team_hub
from skill_pilot.web import server as web_server
from tests import test_team_hub


def sha(data):
    return hashlib.sha256(data).hexdigest()


PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'0' * 40
PDF_BYTES = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n'
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class DocHubTests(unittest.TestCase):
    Hub = test_team_hub.TeamHubTests
    setUp, close, request = Hub.setUp, Hub.close, Hub.request

    def raw(self, path, method='GET', body=None, token='first', headers=None):
        token = self.first['token'] if token == 'first' else token
        supplied = {'Authorization': 'Bearer ' + token} if token else {}
        supplied.update(headers or {})
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request(method, path, body=body, headers=supplied)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def upload_blob(self, data, token='first'):
        status, _, body = self.raw('/api/blobs/me/' + sha(data), 'PUT', data, token,
                                   {'Content-Type': 'application/octet-stream'})
        self.assertEqual(status, 200, body)

    def test_publish_read_update_delete_and_attachment_security(self):
        status, me = self.request('/api/me')
        self.assertEqual(status, 200)
        self.assertTrue(me.get('docs_supported'))

        payload = {
            'title': 'Workshop 第 4 期纪要',
            'category': 'Workshop 纪要',
            'summary': '总结了多 Agent 协作与技能沉淀。',
            'content': '# 纪要\n\n![架构图](img/arch.png)\n\n[下载原件](files/slides.pdf)\n',
            'files': {
                'img/arch.png': {'sha256': sha(PNG_BYTES), 'size': len(PNG_BYTES)},
                'files/slides.pdf': {'sha256': sha(PDF_BYTES), 'size': len(PDF_BYTES)},
                'files/icon.svg': {'sha256': sha(SVG_BYTES), 'size': len(SVG_BYTES)},
            },
        }
        status, missing = self.request('/api/docs/me/ws-04', 'PUT', payload)
        self.assertEqual(status, 409)
        self.assertEqual(sorted(missing['missing']), sorted({sha(PNG_BYTES), sha(PDF_BYTES), sha(SVG_BYTES)}))

        for blob in (PNG_BYTES, PDF_BYTES, SVG_BYTES):
            self.upload_blob(blob)
        status, saved = self.request('/api/docs/me/ws-04', 'PUT', payload)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved['key'], f'doc:{self.first["id"]}:ws-04')
        created_at = saved['created_at']

        # Catalog lists metadata without full content.
        status, cat = self.request('/api/catalog', token=self.second['token'])
        self.assertEqual(status, 200)
        self.assertEqual(len(cat['docs']), 1)
        row = cat['docs'][0]
        self.assertEqual((row['title'], row['category'], row['file_count']), ('Workshop 第 4 期纪要', 'Workshop 纪要', 3))
        self.assertNotIn('content', row)

        # Any authenticated member can read full content; unauthenticated is rejected.
        status, detail = self.request(f'/api/docs/{self.first["id"]}/ws-04', token=self.second['token'])
        self.assertEqual(status, 200)
        self.assertIn('# 纪要', detail['content'])
        self.assertEqual(self.raw(f'/api/docs/{self.first["id"]}/ws-04', token=None)[0], 401)

        # Raster images serve inline with sandbox CSP; PDF and SVG force attachment download.
        base = f'/api/docs/{self.first["id"]}/ws-04/'
        status, headers, raw = self.raw(base + 'img/arch.png', token=self.second['token'])
        self.assertEqual((status, raw), (200, PNG_BYTES))
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', headers['Content-Security-Policy'])
        self.assertNotIn('Content-Disposition', headers)

        for name, expected in (('files/slides.pdf', PDF_BYTES), ('files/icon.svg', SVG_BYTES)):
            status, headers, raw = self.raw(base + name, token=self.second['token'])
            self.assertEqual((status, raw), (200, expected))
            self.assertTrue(headers.get('Content-Disposition', '').startswith('attachment'))
            self.assertIn('sandbox', headers['Content-Security-Policy'])

        # Chinese attachment filename retains UTF-8 filename* and extension fallback.
        self.upload_blob(PDF_BYTES)
        status, _ = self.request('/api/docs/me/zh-doc', 'PUT', {
            'title': '中文附件测试', 'content': '正文',
            'files': {'培训讲义.pdf': {'sha256': sha(PDF_BYTES), 'size': len(PDF_BYTES)}},
        })
        self.assertEqual(status, 200)
        status, headers, _ = self.raw(f'/api/docs/{self.first["id"]}/zh-doc/' + urllib.parse.quote('培训讲义.pdf'))
        self.assertEqual(status, 200)
        self.assertIn('filename="attachment.pdf"', headers['Content-Disposition'])
        self.assertIn("filename*=UTF-8''" + urllib.parse.quote('培训讲义.pdf', safe=''), headers['Content-Disposition'])
        self.assertEqual(self.request('/api/docs/me/zh-doc', 'DELETE')[0], 200)

        # Case-insensitive doc ID collision is rejected rather than overwriting on macOS/Windows.
        self.assertEqual(self.request('/api/docs/me/WS-04', 'PUT', {'title': '冲突', 'content': '正文'})[0], 409)
        self.assertEqual(self.request(f'/api/docs/{self.first["id"]}/WS-04')[0], 404)
        self.assertEqual(self.request('/api/docs/me/WS-04', 'DELETE')[0], 404)

        # Path traversal blocked.
        for bad in ('../ws-04.json', 'img/../files/slides.pdf', 'img//arch.png'):
            self.assertEqual(self.raw(base + bad)[0], 404)

        # Updating without 'files' preserves existing attachments & created_at;
        # optimistic concurrency rejects stale expected_updated_at with 409.
        old_updated_at = saved['updated_at']
        self.assertEqual(self.request('/api/docs/me/ws-04', 'PUT', {
            'title': '旧版本提交', 'content': '正文',
            'expected_updated_at': '2000-01-01T00:00:00Z',
        })[0], 409)
        status, updated = self.request('/api/docs/me/ws-04', 'PUT', {
            'title': 'Workshop 第 4 期纪要（修订）',
            'content': '# 修订版正文\n',
            'expected_updated_at': old_updated_at,
        })
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated['created_at'], created_at)
        status, detail2 = self.request(f'/api/docs/{self.first["id"]}/ws-04')
        self.assertEqual(detail2['category'], 'Workshop 纪要')
        self.assertEqual(detail2['summary'], '总结了多 Agent 协作与技能沉淀。')
        self.assertEqual(detail2['file_count'], 3)

        # Self-healing index.json if index is deleted or out of sync.
        index_path = self.data / 'docs' / self.first['id'] / 'index.json'
        index_path.unlink()
        self.assertEqual([d['id'] for d in self.request('/api/catalog')[1]['docs']], ['ws-04'])
        self.assertTrue(index_path.exists())

        # Other member cannot delete first member's doc.
        self.assertEqual(self.request('/api/docs/me/ws-04', 'DELETE', token=self.second['token'])[0], 404)
        self.assertEqual(self.request('/api/docs/me/ws-04', 'DELETE')[0], 200)
        self.assertEqual(self.request('/api/catalog')[1]['docs'], [])
        # Blobs cleaned after delete.
        blobs = self.data / 'deliverables' / self.first['id'] / 'blobs'
        self.assertEqual(list(blobs.iterdir()), [])

    def test_limits_and_remove_member_cleanup(self):
        # Oversized body content rejected.
        big = 'a' * (team_hub.MAX_DOC_CONTENT + 1)
        status, _ = self.request('/api/docs/me/too-big', 'PUT', {'title': '大文档', 'content': big})
        self.assertEqual(status, 413)

        # Per-member count limit enforced.
        with patch.object(team_hub, 'MAX_DOCS_PER_MEMBER', 2):
            for idx in range(2):
                status, _ = self.request(f'/api/docs/me/doc-{idx}', 'PUT', {'title': f'文档 {idx}', 'content': '正文'})
                self.assertEqual(status, 200)
            # Updating existing still allowed at cap.
            status, _ = self.request('/api/docs/me/doc-0', 'PUT', {'title': '文档 0 更新', 'content': '新正文'})
            self.assertEqual(status, 200)
            # Third new doc blocked.
            status, body = self.request('/api/docs/me/doc-2', 'PUT', {'title': '文档 2', 'content': '正文'})
            self.assertEqual(status, 413, body)

        # Total hub cap enforced across members.
        with patch.object(team_hub, 'MAX_DOCS_TOTAL', 2):
            status, body = self.request('/api/docs/me/other-doc', 'PUT', {'title': '他人文档', 'content': '正文'},
                                        token=self.second['token'])
            self.assertEqual(status, 413, body)

        # Windows reserved device names are rejected across platforms.
        for bad_id in ('CON', 'NUL', 'COM1'):
            self.assertEqual(self.request(f'/api/docs/me/{bad_id}', 'PUT', {'title': '保留名', 'content': '正文'})[0], 400)

        # create_only rejects overwriting an existing doc; stale expected_updated_at blocks delete.
        self.assertEqual(self.request('/api/docs/me/doc-0', 'PUT', {
            'title': '重复创建', 'content': '正文', 'create_only': True,
        })[0], 409)
        self.assertEqual(self.request('/api/docs/me/doc-0?expected_updated_at=2000-01-01T00:00:00Z', 'DELETE')[0], 409)

        # Per-member orphan blob budget protects active-batch fresh uploads (507) and cleans expired orphans (> TTL).
        with patch.object(team_hub, 'MAX_ORPHAN_BLOBS_BYTES', len(PNG_BYTES) + 10):
            self.upload_blob(PNG_BYTES)
            blobs_dir = self.data / 'deliverables' / self.first['id'] / 'blobs'
            png_blob = blobs_dir / sha(PNG_BYTES)
            # Fresh orphan is NOT silently deleted mid-batch; once expired, it IS cleaned to admit new upload.
            old_ts = os.path.getmtime(png_blob) - team_hub.ORPHAN_BLOB_TTL_SECONDS - 60
            os.utime(png_blob, (old_ts, old_ts))
            self.upload_blob(PDF_BYTES)
            self.assertFalse(png_blob.exists())
            self.assertTrue((blobs_dir / sha(PDF_BYTES)).exists())

        # remove_member removes docs and blobs.
        self.upload_blob(PNG_BYTES)
        status, _ = self.request('/api/docs/me/doc-0', 'PUT', {
            'title': '带图文档', 'content': '正文',
            'files': {'a.png': {'sha256': sha(PNG_BYTES), 'size': len(PNG_BYTES)}},
        })
        self.assertEqual(status, 200)
        self.assertTrue((self.data / 'docs' / self.first['id']).is_dir())
        self.store.remove_member(self.first['id'])
        self.assertFalse((self.data / 'docs' / self.first['id']).exists())
        self.assertFalse((self.data / 'deliverables' / self.first['id']).exists())
        self.assertEqual(self.request('/api/catalog', token=self.second['token'])[1]['docs'], [])


class DocClientWebCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot docs ')
        root = Path(self.temp.name)
        self.hub_dir = root / 'hub data'
        self.hub = team_hub.HubServer(self.hub_dir, host='127.0.0.1', port=0, open=False)
        self.hub_thread = __import__('threading').Thread(target=self.hub.serve_forever, daemon=True)
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

    def test_folder_publish_secret_block_readonly_and_cli(self):
        doc_dir = Path(self.temp.name) / 'share pack'
        (doc_dir / 'images').mkdir(parents=True)
        (doc_dir / 'README.md').write_text('# 工具使用指南\n\n![截图](images/shot.png)\n\n示例 key: sk-1234567890abcdef1234\n', encoding='utf-8')
        (doc_dir / 'images' / 'shot.png').write_bytes(PNG_BYTES)
        (doc_dir / '.env').write_text('API_KEY=secret', encoding='utf-8')

        # Preview warns on secret content & blocks .env, writes nothing.
        preview = team.publish_doc(self.alice_home, source=doc_dir, identity='guide-1',
                                   category='工具指南', summary='实用指南', confirmed=False)
        self.assertTrue(preview['needs_confirmation'])
        self.assertEqual(preview['title'], '工具使用指南')
        self.assertEqual(preview['file_count'], 1)
        self.assertEqual([b['path'] for b in preview['blocked']], ['.env'])
        self.assertTrue(preview['warnings'])
        self.assertEqual(team.catalog(self.alice_home)['docs'], [])

        # Read-only mode blocks publish and delete.
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            with self.assertRaises(ValueError):
                team.publish_doc(self.alice_home, source=doc_dir, identity='guide-1', confirmed=True)
            with self.assertRaises(ValueError):
                team.remove_doc(self.alice_home, 'guide-1', confirmed=True)

        # CLI preview exits 2, then --yes publishes, doc-list sees it, doc-remove deletes it.
        env = {**os.environ, 'SKILL_PILOT_HOME': str(self.alice_home), 'PYTHONDONTWRITEBYTECODE': '1'}
        base_cmd = [sys.executable, '-m', 'skill_pilot', 'team']
        r1 = subprocess.run([*base_cmd, 'doc', str(doc_dir), '--id', 'guide-1', '--category', '工具指南', '--json'],
                            capture_output=True, text=True, env=env)
        self.assertEqual(r1.returncode, 2, r1.stderr)
        self.assertTrue(json.loads(r1.stdout)['needs_confirmation'])

        r2 = subprocess.run([*base_cmd, 'doc', str(doc_dir), '--id', 'guide-1', '--category', '工具指南', '--yes', '--json'],
                            capture_output=True, text=True, env=env)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(json.loads(r2.stdout)['id'], 'guide-1')

        r3 = subprocess.run([*base_cmd, 'doc-list', '--json'], capture_output=True, text=True, env=env)
        self.assertEqual(r3.returncode, 0, r3.stderr)
        listed = json.loads(r3.stdout)
        self.assertTrue(listed['docs_supported'])
        self.assertEqual([d['id'] for d in listed['docs']], ['guide-1'])

        # Updating only the Markdown file with the same --id preserves existing category/summary/attachments,
        # and passing an additional --asset merges rather than replacing existing attachments.
        updated_md = Path(self.temp.name) / 'updated.md'
        updated_md.write_text('# 工具使用指南 v2\n\n更新正文\n', encoding='utf-8')
        extra_png = Path(self.temp.name) / 'extra.png'
        extra_png.write_bytes(PNG_BYTES)
        r_upd = subprocess.run([*base_cmd, 'doc', str(updated_md), '--id', 'guide-1',
                                '--asset', str(extra_png), '--yes', '--json'],
                               capture_output=True, text=True, env=env)
        self.assertEqual(r_upd.returncode, 0, r_upd.stderr)
        upd_detail = team.doc_detail(self.alice_home, self.alice['id'], 'guide-1')
        self.assertEqual(upd_detail['category'], '工具指南')
        self.assertEqual(upd_detail['file_count'], 2)

        # Updating with --asset where all new assets are blocked must NOT clear existing attachments.
        blocked_secret = Path(self.temp.name) / 'blocked_extra.txt'
        blocked_secret.write_text('token=sk-1234567890abcdef123456\n', encoding='utf-8')
        r_blk = subprocess.run([*base_cmd, 'doc', str(updated_md), '--id', 'guide-1',
                                '--asset', str(blocked_secret), '--yes', '--json'],
                               capture_output=True, text=True, env=env)
        self.assertEqual(r_blk.returncode, 0, r_blk.stderr)
        self.assertEqual(team.doc_detail(self.alice_home, self.alice['id'], 'guide-1')['file_count'], 2)

        # Text attachment larger than 2 MiB is still scanned for secrets.
        big_secret = Path(self.temp.name) / 'big_log.txt'
        big_secret.write_bytes(b'token=sk-1234567890abcdef123456\n' + b'a' * (2 * 1024 * 1024 + 64))
        big_prev = team.publish_doc(self.alice_home, title='大日志测试', content='正文',
                                    assets=[big_secret], confirmed=False)
        self.assertEqual([b['path'] for b in big_prev['blocked']], ['big_log.txt'])
        self.assertEqual(big_prev['file_count'], 0)

        # Reserved Windows doc ID rejected in client preview too.
        with self.assertRaisesRegex(ValueError, '保留名称'):
            team.publish_doc(self.alice_home, identity='CON', title='测试', content='正文', confirmed=False)

        # Non-binary attachments of arbitrary extensions (e.g. .yaml) are scanned for secrets.
        yaml_dir = Path(self.temp.name) / 'yaml check'
        yaml_dir.mkdir()
        (yaml_dir / 'README.md').write_text('# 配置说明\n', encoding='utf-8')
        (yaml_dir / 'config.yaml').write_text('api_key: sk-1234567890abcdef123456\n', encoding='utf-8')
        yaml_prev = team.publish_doc(self.alice_home, source=yaml_dir, identity='yaml-1', confirmed=False)
        self.assertEqual([b['path'] for b in yaml_prev['blocked']], ['config.yaml'])
        self.assertEqual(yaml_prev['file_count'], 0)

        # Connection mismatch prevents writing to a switched hub/account.
        with self.assertRaisesRegex(ValueError, '连接已经切换'):
            team.publish_doc(self.alice_home, title='切换检测', content='正文',
                             expected_connection='other-connection', confirmed=True)

        # Unmigrated new member can still run team doc-list without local skill registry.
        fresh_home = Path(self.temp.name) / 'fresh unmigrated'
        fresh_home.mkdir()
        fresh_member = self.hub.store.add_member('Charlie')
        team.save_settings(fresh_home, {'url': self.hub_url, 'key': fresh_member['token']})
        self.assertEqual([d['id'] for d in team.list_docs(fresh_home)['docs']], ['guide-1'])

        # Secret check covers summary, .markdown attachment, and direct secret filename;
        # relative --asset path is preserved and duplicate paths raise.
        sub_doc_dir = Path(self.temp.name) / 'rel assets'
        (sub_doc_dir / 'img').mkdir(parents=True)
        (sub_doc_dir / 'post.md').write_text('# 正文\n\n![图](img/chart.png)\n', encoding='utf-8')
        (sub_doc_dir / 'img' / 'chart.png').write_bytes(PNG_BYTES)
        (sub_doc_dir / 'secret.markdown').write_text('token = sk-1234567890abcdef123456\n', encoding='utf-8')
        prev_rel = team.publish_doc(self.alice_home, source=sub_doc_dir / 'post.md', identity='rel-1',
                                    summary='含密钥 sk-1234567890abcdef123456',
                                    assets=[sub_doc_dir / 'img' / 'chart.png', sub_doc_dir / 'secret.markdown'],
                                    confirmed=False)
        self.assertTrue(prev_rel['warnings'])
        self.assertEqual([b['path'] for b in prev_rel['blocked']], ['secret.markdown'])
        self.assertEqual(prev_rel['file_count'], 1)
        team.publish_doc(self.alice_home, source=sub_doc_dir / 'post.md', identity='rel-1',
                         assets=[sub_doc_dir / 'img' / 'chart.png'], confirmed=True)
        rel_detail = team.doc_detail(self.alice_home, self.alice['id'], 'rel-1')
        self.assertEqual([f['path'] for f in rel_detail['files']], ['img/chart.png'])
        team.remove_doc(self.alice_home, 'rel-1', confirmed=True)
        dup_dir = Path(self.temp.name) / 'dup assets'
        (dup_dir / 'a').mkdir(parents=True)
        (dup_dir / 'b').mkdir(parents=True)
        (dup_dir / 'a' / 'same.png').write_bytes(PNG_BYTES)
        (dup_dir / 'b' / 'same.png').write_bytes(PNG_BYTES)
        with self.assertRaisesRegex(ValueError, '位置重复'):
            team.publish_doc(self.alice_home, title='重复测试', content='正文',
                             assets=[dup_dir / 'a' / 'same.png', dup_dir / 'b' / 'same.png'], confirmed=False)
        env_file = Path(self.temp.name) / '.env'
        env_file.write_text('SECRET=1', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已拦下'):
            team.publish_doc(self.alice_home, source=env_file, confirmed=False)

        r4 = subprocess.run([*base_cmd, 'doc-remove', 'guide-1', '--yes', '--json'],
                            capture_output=True, text=True, env=env)
        self.assertEqual(r4.returncode, 0, r4.stderr)
        self.assertEqual(team.catalog(self.bob_home)['docs'], [])

    def test_dashboard_routes_and_old_hub_compatibility(self):
        # Dashboard API flow
        with patch.object(web_server, 'target_home', return_value=self.alice_home):
            dash = web_server.Dashboard({'home': str(self.alice_home), 'deferred': True})
        t = __import__('threading').Thread(target=dash.serve_forever, daemon=True)
        t.start()
        try:
            def call(method, path, body=None, token=True):
                conn = http.client.HTTPConnection('127.0.0.1', dash.server_port, timeout=5)
                try:
                    headers = {'Host': f'127.0.0.1:{dash.server_port}'}
                    if token:
                        headers['X-Skill-Pilot-Token'] = dash.token
                    if method == 'POST':
                        headers['Origin'] = dash.origin
                        headers['Content-Type'] = 'application/json'
                    data = json.dumps(body).encode() if body is not None else None
                    conn.request(method, path, body=data, headers=headers)
                    resp = conn.getresponse()
                    return resp.status, dict(resp.getheaders()), resp.read()
                finally:
                    conn.close()

            # Upload with CLI asset first, then edit via dashboard preserving asset.
            asset_file = Path(self.temp.name) / 'pic.png'
            asset_file.write_bytes(PNG_BYTES)
            saved = team.publish_doc(self.alice_home, identity='d1', title='初始标题', category='工具指南',
                                     content='# 初始\n\n![图](pic.png)\n', assets=[asset_file], confirmed=True)
            key = saved['key']

            status, _, raw = call('GET', '/api/team/catalog')
            self.assertEqual(status, 200)
            cat = json.loads(raw)
            self.assertTrue(cat['docs_supported'])
            self.assertEqual(len(cat['docs']), 1)

            status, _, raw = call('GET', '/api/team/docs/detail?key=' + urllib.parse.quote(key))
            self.assertEqual(status, 200)
            detail = json.loads(raw)
            self.assertEqual(detail['title'], '初始标题')
            base_url = detail['file_base_url']

            # Fetch proxied image via session-scoped doc-file URL; forged key rejected.
            status, headers, raw = call('GET', base_url + 'pic.png', token=False)
            self.assertEqual((status, raw), (200, PNG_BYTES))
            self.assertIn('sandbox', headers['Content-Security-Policy'])
            self.assertEqual(call('GET', f'/doc-file/{"0"*32}/{self.alice["id"]}/d1/pic.png', token=False)[0], 404)

            # Edit via dashboard keeps attachment.
            status, _, raw = call('POST', '/api/team/docs/save', {
                'id': 'd1', 'title': '更新后标题', 'category': 'Workshop 纪要',
                'summary': '新摘要', 'content': '# 更新正文\n',
            })
            self.assertEqual(status, 200, raw)
            status, _, raw = call('GET', '/api/team/docs/detail?key=' + urllib.parse.quote(key))
            detail2 = json.loads(raw)
            self.assertEqual((detail2['title'], detail2['file_count']), ('更新后标题', 1))

            # Read-only dashboard blocks write routes.
            with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
                self.assertEqual(call('POST', '/api/team/docs/save', {
                    'title': '只读测试', 'content': '内容',
                })[0], 403)
                self.assertEqual(call('POST', '/api/team/docs/remove', {'id': 'd1'})[0], 403)

            # Delete via dashboard succeeds.
            status, _, _ = call('POST', '/api/team/docs/remove', {'id': 'd1'})
            self.assertEqual(status, 200)
            self.assertEqual(team.catalog(self.alice_home)['docs'], [])
        finally:
            dash.shutdown()
            dash.server_close()

        # Old hub compatibility: /api/me lacks docs_supported and /api/catalog lacks docs.
        orig_request = team._request
        def old_hub_request(config, path, method='GET', data=None):
            res = orig_request(config, path, method, data)
            if path == '/api/me':
                res = {k: v for k, v in res.items() if k != 'docs_supported'}
            elif path == '/api/catalog':
                res = {k: v for k, v in res.items() if k != 'docs'}
            return res

        with patch.object(team, '_request', side_effect=old_hub_request):
            cat = team.catalog(self.alice_home)
            self.assertFalse(cat['docs_supported'])
            self.assertEqual(cat['docs'], [])
            with self.assertRaisesRegex(ValueError, '分享站版本较旧'):
                team.publish_doc(self.alice_home, title='旧站测试', content='正文', confirmed=True)


class DocMarkdownSafetyTests(unittest.TestCase):
    def test_markdown_renderer_escapes_xss_and_allows_safe_content(self):
        app_js = (Path(__file__).resolve().parents[1] / 'skill_pilot/web/static/app.js').read_text(encoding='utf-8')
        # Extract escape and docMd* functions and test them in Node.
        start = app_js.index('function docSafeBaseUrl(')
        end = app_js.index('async function openDocDetail(')
        snippet = app_js[start:end]
        script = f"""
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
{snippet}
const files = [{{path: 'img/a.png', size: 100, is_image: true}}, {{path: 'img/chart..v2.png', size: 120, is_image: true}}, {{path: 'note.pdf', size: 200, is_image: false}}];
const base = '/doc-file/' + 'a'.repeat(32) + '/' + 'b'.repeat(32) + '/doc-1/';
const md = [
  '# 标题 <script>alert(1)</script>',
  '',
  '段落 **粗体** *斜体* `<b>code</b>`',
  '<img src=x onerror=alert(1)>',
  '[恶意链接](javascript:alert(1))',
  '[Data链接](data:text/html,<script>alert(1)</script>)',
  '[安全外链](https://example.com/docs)',
  '[附件](note.pdf)',
  '![合法图片](img/a.png)',
  '![双点文件名](img/chart..v2.png)',
  '![外部图片](https://evil.example/track.png)',
  '![穿越](../secret.png)',
  '> 引用行',
  '---',
  '| 列1 | 列2 |',
  '| --- | --- |',
  '| 值1 | 值2 |',
  '- [x] 已完成任务',
  '- [ ] 待办任务',
].join('\\n');
const html = docMdHtml(md, files, base);
console.log(JSON.stringify({{html}}));
"""
        proc = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        html = json.loads(proc.stdout)['html']
        self.assertIn('type="checkbox" disabled checked', html)
        self.assertIn('type="checkbox" disabled aria-label="未完成"', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', html)
        self.assertNotIn('javascript:', html)
        self.assertNotIn('data:text/html', html)
        self.assertNotIn('evil.example', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('<strong>粗体</strong>', html)
        self.assertIn('<em>斜体</em>', html)
        self.assertIn('href="https://example.com/docs"', html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertIn(f'src="/doc-file/{"a"*32}/{"b"*32}/doc-1/img/a.png"', html)
        self.assertIn(f'src="/doc-file/{"a"*32}/{"b"*32}/doc-1/img/chart..v2.png"', html)
        self.assertIn('[图片：外部图片]', html)
        self.assertIn('[图片：穿越]', html)
        self.assertIn('<blockquote>', html)
        self.assertIn('<hr>', html)
        self.assertIn('<table>', html)


if __name__ == '__main__':
    unittest.main()
