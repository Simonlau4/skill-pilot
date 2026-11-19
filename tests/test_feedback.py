import base64
import hashlib
import http.client
import json
import os
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests import set_up_home
from tests.test_nav_static import extract_function
from skill_pilot import cli, diagnostics, feedback, labels, team, team_hub
from skill_pilot.web.server import Dashboard

WEBHOOK = 'https://feedback.example.test/receive'
ENVIRONMENT = {'os': 'Example OS', 'python_version': '3.12.0', 'app_version': 'abcdef012345',
               'install_mode': '压缩包', 'tools': {'git': {'available': True, 'version': '2.45.0'}},
               'agent_types': ['Claude'], 'skill_count': 2}


def make_png(color=(47, 91, 211), pad=0):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    raw = b'\x00' + bytes(color)
    body = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw))
    if pad:
        body += chunk(b'tEXt', b'p\x00' + b'x' * pad)
    return body + chunk(b'IEND', b'')


def make_jpeg(pad=0):
    return b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00' + (b'\x00' * pad) + b'\xff\xd9'


def make_webp(pad=0):
    payload = b'VP8 ' + struct.pack('<I', 8 + pad) + b'\x00' * (8 + pad)
    return b'RIFF' + struct.pack('<I', 4 + len(payload)) + b'WEBP' + payload


def encode_shot(raw, content_type=''):
    item = {'data': base64.b64encode(raw).decode('ascii')}
    if content_type:
        item['content_type'] = content_type
    return item


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot feedback ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name) / 'simulated user')
        self.home.mkdir()
        self.environment = patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.data = Path(self.temp.name) / 'hub data'
        self.hub = team_hub.HubServer(self.data, '127.0.0.1', 0, feedback_webhook=WEBHOOK)
        self.member = self.hub.store.add_member('示例同事')
        self.serve(self.hub)
        self.config = {'url': f'http://127.0.0.1:{self.hub.server_port}', 'key': self.member['token']}
        labels.atomic_write(self.home, 'team.json', json.dumps(self.config), 0o600)
        self.dashboard = Dashboard({'home': str(self.home), 'agents': [], 'skills': []})
        self.serve(self.dashboard)
        self.row = diagnostics.record(self.home, 'backend', '读取文件', OSError('模拟故障'))
        self.env_patch = patch.object(diagnostics, 'environment', return_value=ENVIRONMENT)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(close)

    def request(self, path, data=None, hub=False, token=None, method=None):
        server = self.hub if hub else self.dashboard
        headers = {'Authorization': 'Bearer ' + (self.member['token'] if token is None else token)} if hub else {
            'Origin': server.origin, 'X-Skill-Pilot-Token': server.token}
        if not hub:
            path += '?token=' + server.token
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        try:
            connection.request(method or ('POST' if data is not None else 'GET'), path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def preview(self, **extra):
        status, result = self.request('/api/diagnostics/preview', {'error_id': self.row['error_id'], **extra})
        self.assertEqual(status, 200, result)
        return result

    def test_confirmed_snapshot_identity_and_local_receipt(self):
        prepared = self.preview(note='点击扫描后卡住')
        self.assertTrue(prepared['can_send'])
        self.assertEqual(prepared['member']['name'], '示例同事')
        self.assertNotIn('_connection', prepared)
        self.assertFalse((self.data / 'feedback.json').exists())
        with patch.object(feedback, 'forward') as forward:
            code, result = self.request('/api/diagnostics/submit', {'ticket': prepared['ticket'], 'detail': '篡改'})
            self.assertEqual((code, result), (200, {'submitted': True}))
            payload = forward.call_args.args[1]
            self.assertEqual(payload, prepared['payload'] | {'submitted_at': payload['submitted_at']})
            self.assertEqual(payload['user_note'], '点击扫描后卡住')
            self.assertEqual(set(payload), set(feedback.LIMITS) | {'reporter', 'status', 'submitted_at'})
            self.assertNotIn('篡改', json.dumps(payload))
            self.assertEqual(self.request('/api/diagnostics/submit', {'ticket': prepared['ticket']})[0], 400)
            self.assertEqual(forward.call_count, 1)
        self.assertTrue(self.preview()['submitted'])
        backup = json.loads((self.data / 'feedback.json').read_text())[0]
        self.assertEqual(backup['member_id'], self.member['id'])
        self.assertEqual(backup['state'], 'sent')

    def test_connection_failure_context_survives_preview_and_hub_forwarding(self):
        labels.save_settings(self.home, {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                                         'model': 'fixture', 'api_key': 'fixture-private-key'})
        certificate = ssl.SSLCertVerificationError('certificate rejected')
        certificate.verify_code = 19
        certificate.verify_message = 'self-signed certificate in certificate chain'
        with patch('skill_pilot.labels.urllib.request.OpenerDirector.open',
                   side_effect=urllib.error.URLError(certificate)):
            result = labels.test(self.home)
        prepared = self.preview(error_id=result['error_id'])
        self.assertIn('DeepSeek 官方接口', prepared['payload']['detail'])
        self.assertEqual(prepared['payload']['user_message'], result['message'])
        with patch.object(feedback, 'forward') as forward:
            status, submitted = self.request('/api/diagnostics/submit', {'ticket': prepared['ticket']})
        self.assertEqual(status, 200)
        self.assertTrue(submitted['submitted'])
        payload = forward.call_args.args[1]
        self.assertIn('证书校验编号：19', payload['detail'])
        self.assertIn('经过步骤', payload['detail'])
        self.assertIn('已用时间', payload['detail'])
        self.assertNotIn('fixture-private-key', json.dumps(payload))
        self.assertEqual(payload['error_id'], result['error_id'])
        self.assertEqual(payload['app_version'], ENVIRONMENT['app_version'])

    def test_hub_auth_whitelist_size_controls_and_forged_identity(self):
        data = self.preview()['payload'] | {'reporter': '伪造', 'member_id': 'fake', 'unknown': 'secret',
                                           'detail': 'a\x00\x1b\u202eb\nc', 'user_note': ['not text']}
        with patch.object(feedback, 'forward') as forward:
            self.assertEqual(self.request('/api/feedback', data, hub=True, token='invalid')[0], 401)
            self.assertEqual(self.request('/api/feedback', {'detail': 'x' * feedback.MAX_BODY}, hub=True)[0], 413)
            self.assertEqual(self.request('/api/feedback', data, hub=True)[0], 200)
            payload = forward.call_args.args[1]
            self.assertEqual(payload['reporter'], '示例同事')
            self.assertEqual(payload['detail'], 'ab\nc')
            self.assertEqual(payload['user_note'], '')
            self.assertNotIn('unknown', payload)
            self.assertNotIn('member_id', payload)
            self.assertEqual(forward.call_count, 1)

    def test_duplicate_ids_and_fingerprints_and_concurrent_pending(self):
        data = self.preview()['payload']
        started, finish = threading.Event(), threading.Event()
        def forward(*args):
            started.set()
            self.assertTrue(finish.wait(5))
        with patch.object(feedback, 'forward', side_effect=forward) as send, ThreadPoolExecutor() as pool:
            first = pool.submit(self.request, '/api/feedback', data, True)
            self.assertTrue(started.wait(3))
            try:
                self.assertEqual(self.request('/api/feedback', data, hub=True)[0], 429)
                self.assertEqual(self.request('/api/me', hub=True)[0], 200)
            finally:
                finish.set()
            self.assertEqual(first.result()[0], 200)
            self.assertTrue(self.request('/api/feedback', data, hub=True)[1]['duplicate'])
            self.assertEqual(self.request('/api/feedback', data | {'error_id': 'different'}, hub=True)[0], 429)
            self.assertEqual(send.call_count, 1)

    def test_atomic_bounded_backup_before_forward_and_failed_state(self):
        data = self.preview()['payload']
        def failed(*args):
            self.assertEqual(json.loads((self.data / 'feedback.json').read_text())[-1]['state'], 'pending')
            raise ValueError('模拟接收失败')
        with patch.object(feedback, 'forward', side_effect=failed):
            self.assertEqual(self.request('/api/feedback', data, hub=True)[0], 502)
        self.assertEqual(json.loads((self.data / 'feedback.json').read_text())[-1]['state'], 'failed')
        before = (self.data / 'feedback.json').read_bytes()
        with patch.object(team_hub.os, 'replace', side_effect=OSError('无法写入')), patch.object(feedback, 'forward') as send:
            self.assertEqual(self.request('/api/feedback', data | {'error_id': 'new', 'fingerprint': 'new'}, hub=True)[0], 500)
            self.assertEqual((self.data / 'feedback.json').read_bytes(), before)
            send.assert_not_called()
        with patch.object(feedback, 'MAX_RECORDS', 2), patch.object(feedback, 'MAX_BYTES', 3000), patch.object(feedback, 'forward'):
            for i in range(4):
                self.assertEqual(self.request('/api/feedback', data | {'error_id': str(i), 'fingerprint': str(i)}, hub=True)[0], 200)
        self.assertLessEqual(len(json.loads((self.data / 'feedback.json').read_text())), 2)
        self.assertLessEqual((self.data / 'feedback.json').stat().st_size, 3000)

    def test_read_only_never_contacts_hub_and_stale_or_changed_identity(self):
        prepared = self.preview()
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), patch.object(team, '_request') as request:
            self.assertEqual(self.request('/api/diagnostics/submit', {'ticket': prepared['ticket']})[0], 403)
            self.assertFalse(self.preview()['can_send'])
            request.assert_not_called()
        with patch.object(feedback, 'forward') as send:
            self.dashboard.feedback_previews[prepared['ticket']]['expires'] = 0
            self.assertEqual(self.request('/api/diagnostics/submit', {'ticket': prepared['ticket']})[0], 400)
            prepared = self.preview()
            other = self.hub.store.add_member('另一位同事')
            labels.atomic_write(self.home, 'team.json', json.dumps(self.config | {'key': other['token']}), 0o600)
            self.assertEqual(self.request('/api/diagnostics/submit', {'ticket': prepared['ticket']})[0], 400)
            send.assert_not_called()

    def test_backup_limit_counts_windows_text_line_endings(self):
        rows = [{'detail': 'x' * 75, 'state': 'sent'} for _ in range(2)]
        limit = len((json.dumps(rows, ensure_ascii=False, indent=2) + '\n').encode())
        original = os.fdopen
        def windows_text(fd, mode, **kwargs):
            return original(fd, mode, **kwargs, newline='\r\n')
        with patch.object(os, 'linesep', '\r\n'), patch.object(os, 'fdopen', windows_text), \
                patch.object(feedback, 'MAX_BYTES', limit):
            self.hub.store._save_feedback(rows)
        raw = (self.data / 'feedback.json').read_bytes()
        self.assertIn(b'\r\n', raw)
        self.assertLessEqual(len(raw), limit)
        self.assertEqual(len(json.loads(raw)), 1)

    def test_disabled_old_offline_hubs_and_manual_report(self):
        self.assertEqual(self.request('/api/me', hub=True)[1]['api'], 2)
        self.hub.feedback_webhook = ''
        self.assertFalse(self.preview()['can_send'])
        self.assertEqual(self.request('/api/feedback', self.preview()['payload'], hub=True)[0], 503)
        old = {'api': 2, 'service': 'skill-pilot-team', 'member': {k: self.member[k] for k in ('id', 'name')}}
        with patch.object(team, '_request', return_value=old):
            self.assertEqual(team.status(self.home)['member'], old['member'])
            self.assertIn('版本较旧', self.preview()['reason'])
        with patch.object(team, '_request', side_effect=ValueError('private-team-url')):
            result = self.preview()
            self.assertIn('连不上', result['reason'])
            self.assertNotIn('private-team-url', str(result))
        manual = self.request('/api/diagnostics/preview', {})[1]['payload']
        self.assertEqual(manual['detail'], '')
        self.assertEqual(manual['source'], 'manual')
        self.assertNotEqual(manual['error_id'], self.row['error_id'])

    def test_note_and_error_redaction_before_preview(self):
        (self.home / '.skill-pilot/settings.json').write_text(json.dumps({'api_key': 'example-secret-token'}))
        result = self.preview(note=f"example-secret-token {self.home / 'private.txt'} {self.member['token']}")
        for value in ('example-secret-token', str(self.home), self.member['token']):
            self.assertNotIn(value, json.dumps(result))

    def test_transport_real_http_flat_fields_redirect_failure_and_timeout(self):
        received = []
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(302 if self.path == '/redirect' else 500 if self.path == '/failure' else 200)
                self.send_header('Location', '/should-not-follow')
                self.end_headers()
                self.wfile.write(b'{"code":0}')
        receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        self.serve(receiver)
        original = urllib.request.OpenerDirector.open
        route = '/ok'
        def local_transport(opener, request, *args, **kwargs):
            if request.full_url == WEBHOOK:
                self.assertEqual(kwargs.get('timeout'), 10)
                request = urllib.request.Request(f'http://127.0.0.1:{receiver.server_port}{route}',
                    data=request.data, headers=dict(request.header_items()), method='POST')
            return original(opener, request, *args, **kwargs)
        data = self.preview()['payload']
        with patch.object(urllib.request.OpenerDirector, 'open', local_transport):
            self.assertEqual(self.request('/api/feedback', data, hub=True)[0], 200)
            self.assertEqual(received[0]['reporter'], '示例同事')
            for route in ('/redirect', '/failure'):
                with self.assertRaises(ValueError):
                    feedback.forward(WEBHOOK, data)
        self.assertEqual(len(received), 3)
        with patch.object(urllib.request.OpenerDirector, 'open', side_effect=TimeoutError(WEBHOOK)):
            with self.assertRaisesRegex(ValueError, '已保留备份') as error:
                feedback.forward(WEBHOOK, data)
            self.assertNotIn(WEBHOOK, str(error.exception))

    def test_configuration_rejects_unsafe_urls_without_echo(self):
        for url in ('http://example.test/a', 'https://user:password@example.test/a', 'https://localhost/a',
                    'https://127.0.0.1/a', 'https://[::1]/a', 'https://example.test/a#secret', 'https://example.test:bad/a'):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                feedback.webhook_url(url)
            self.assertNotIn(url, str(error.exception))
        self.assertEqual(feedback.webhook_url(WEBHOOK), WEBHOOK)

    def test_cli_environment_and_explicit_address(self):
        with patch.dict(os.environ, {'SKILL_PILOT_HUB_FEEDBACK_WEBHOOK': WEBHOOK}), \
                patch.object(team_hub, 'HubServer') as server, patch.object(team_hub.threading, 'Thread'):
            server.return_value.__enter__.return_value.serve_forever.side_effect = KeyboardInterrupt
            for args, expected in (([], WEBHOOK), (['--feedback-webhook', WEBHOOK + '/explicit'], WEBHOOK + '/explicit')):
                with patch.object(sys, 'argv', ['skill-pilot', 'hub', 'serve', '--data', str(self.data), *args]):
                    cli.main()
                self.assertEqual(server.call_args.kwargs['feedback_webhook'], expected)

    def test_normal_toast_has_no_feedback_but_error_and_jobs_do(self):
        # Execute the actual render helpers, rather than comparing generated source strings.
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const code = fs.readFileSync('skill_pilot/web/static/app.js', 'utf8');
const nodes = [];
const node = () => ({children: [], append(x) {this.children.push(x)}, addEventListener() {}, setAttribute() {},
  insertAdjacentHTML(where, html) {this.html = html}});
const context = {document: {createElement: node}, $: () => ({prepend: x => nodes.push(x)}),
  escape: x => x, autoDismiss() {}, titleOf: x => x};
vm.createContext(context);
for (const name of ['toast', 'appendFeedback', 'feedbackButton', 'jobFailedHtml']) {
  const begin = code.indexOf('function ' + name + '(');
  const end = code.indexOf('\n}', begin) + 2;
  vm.runInContext(code.slice(begin, end), context);
}
context.toast({text: '请选择技能', tone: 'error'});
context.toast({text: '操作失败', tone: 'error', error_id: 'abc'});
assert(!nodes[0].html); assert(nodes[1].html.includes('data-feedback="abc"'));
assert(context.jobFailedHtml({status:'failed', failed:[{name:'demo', error_id:'xyz'}]}).includes('data-feedback="xyz"'));
'''
        subprocess.run(['node', '-e', script], cwd=diagnostics.PROJECT, check=True, capture_output=True)

    def test_async_program_update_results_and_inline_errors_keep_ids(self):
        source = (diagnostics.PROJECT / 'skill_pilot/web/static/app.js').read_text()
        functions = '\n'.join(extract_function(source, signature) for signature in (
            'async function programAction(', 'function programText(', 'function showReview(', 'function renderUpdateBar(',
            'function showDetailTagError(', 'function feedbackButton('))
        script = r'''
const assert = require('node:assert/strict');
let programUpdate = {}, writeBusy = false, programFailedAction = '', programActionRunning = false;
const nodes = {}, tasks = {}, state = {}, updateState = {}, homeVisible = false;
const $ = id => nodes[id] ||= {setAttribute() {}, querySelector() {return this}};
const escape = x => String(x).replaceAll('<', '&lt;');
const icon = () => '', nameHtml = escape, updatesWritable = () => true, uncheckedReasons = () => ({count:0,known:true});
const updateJobFor = () => null, updateResultSentence = () => '检查完成', renderUncheckedSkills = () => {};
const renderProgram = () => {}, renderSkillTable = () => {}, renderDetail = () => {}, checkUpdates = () => {};
const setTask = (id, task) => tasks[id] = task, clearTask = id => delete tasks[id];
const setTimeout = fn => fn();
const api = async route => route.endsWith('/progress') ? {running:false,error:'升级出错',error_id:'program-id'} : {running:true};
const runWrite = async options => {try {return await options.work()} catch (error) {options.onError(error)}};
''' + functions + r'''
(async () => {
  for (const action of ['check', 'apply']) {
    programUpdate = {};
    await programAction(action);
    assert.equal(programUpdate.error_id, 'program-id');
    assert.equal(programUpdate.error, '升级出错');
  }
  showReview({id:'review',running:false,skills:[{name:'demo',error:'读不到来源',error_id:'row-id'}],error:'整轮失败',error_id:'review-id'});
  assert.equal(tasks.updates.error_id, 'review-id');
  assert(nodes['updates-status'].innerHTML.includes('data-feedback="review-id"'));
  assert(nodes['updates-failed'].innerHTML.includes('data-feedback="row-id"'));
  showDetailTagError('<failed>', 'inline-id');
  assert(nodes['detail-tag-error'].innerHTML.includes('&lt;failed>'));
  assert(nodes['detail-tag-error'].innerHTML.includes('data-feedback="inline-id"'));
  showDetailTagError('请先填写标签');
  assert(!nodes['detail-tag-error'].innerHTML.includes('data-feedback'));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', script], cwd=diagnostics.PROJECT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_and_finished_job_errors_keep_feedback_ids(self):
        source = (diagnostics.PROJECT / 'skill_pilot/web/static/app.js').read_text()
        functions = '\n'.join(extract_function(source, signature) for signature in (
            'async function refreshProgram(', 'function finishJobs('))
        script = r'''
const assert = require('node:assert/strict'), headers = {}, messages = [];
const state = {assignmentIntents:new Set()}, assignmentSubmitting = false;
const fetch = async () => ({status:409,ok:false,json:async()=>({error:'无法读取程序',error_id:'runtime-id'})});
const toast = value => messages.push(value), jobStatusLine = job => job.message;
const jobsHasActive = () => false, renderJobs = () => {};
const refreshTable = async () => {throw Object.assign(new Error('无法刷新'), {error_id:'refresh-id'})};
const refreshLog = async () => {}, loadCleanup = async () => {};
''' + functions + r'''
(async () => {
  await assert.rejects(refreshProgram(), error => error.error_id === 'runtime-id');
  finishJobs([{kind:'install',status:'failed',message:'失败',error_id:'job-id'},
              {kind:'assignment',status:'attention',message:'失败',error_id:'attention-id'}]);
  await new Promise(setImmediate);
  assert.deepEqual(messages.map(row=>row.error_id), ['job-id','attention-id','refresh-id']);
})().catch(error => {console.error(error);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', script], cwd=diagnostics.PROJECT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def raw_hub_get(self, path, token=None):
        headers = {} if token == '' else {'Authorization': 'Bearer ' + (self.member['token'] if token is None else token)}
        connection = http.client.HTTPConnection('127.0.0.1', self.hub.server_port, timeout=15)
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_screenshots_end_to_end_forward_storage_and_safe_download(self):
        png = make_png((10, 20, 30))
        jpeg = make_jpeg(32)
        webp = make_webp(24)
        shots = [
            encode_shot(png, 'image/png') | {'filename': '/private/path/secret.png'},
            encode_shot(jpeg, 'image/jpeg') | {'path': 'C:\\Users\\alice\\Desktop\\token.jpg'},
            encode_shot(webp, 'image/webp'),
        ]
        prepared = self.preview(note='页面弹窗错位', attachments=shots)
        self.assertTrue(prepared['can_send'])
        self.assertTrue(prepared['can_attach'])
        self.assertNotIn('_attachments', prepared)
        self.assertEqual(len(prepared['attachments']), 3)
        self.assertEqual(prepared['payload']['attachment_count'], '3')
        self.assertIn('已附 3 张截图', prepared['payload']['attachments_summary'])
        self.assertNotIn('secret.png', json.dumps(prepared, ensure_ascii=False))
        self.assertNotIn('token.jpg', json.dumps(prepared, ensure_ascii=False))
        self.assertNotIn('alice', json.dumps(prepared, ensure_ascii=False))
        with patch.object(feedback, 'forward') as forward:
            code, result = self.request('/api/diagnostics/submit', {'ticket': prepared['ticket']})
            self.assertEqual((code, result), (200, {'submitted': True}))
            payload = forward.call_args.args[1]
            self.assertEqual(payload['attachment_count'], '3')
            self.assertIn('feedback-attachments/', payload['attachments_summary'])
            self.assertIn('[附带截图]', payload['detail'])
            self.assertTrue(all(isinstance(v, str) for v in payload.values()))
            self.assertNotIn(base64.b64encode(png).decode('ascii'), json.dumps(payload))
            self.assertNotIn('secret.png', json.dumps(payload, ensure_ascii=False))
            self.assertNotIn('token.jpg', json.dumps(payload, ensure_ascii=False))
        backup = json.loads((self.data / 'feedback.json').read_text(encoding='utf-8'))[0]
        self.assertEqual(backup['state'], 'sent')
        self.assertEqual(len(backup['attachments']), 3)
        att_dir = self.data / 'feedback-attachments'
        for raw, att, expected_mime in ((png, backup['attachments'][0], 'image/png'),
                                        (jpeg, backup['attachments'][1], 'image/jpeg'),
                                        (webp, backup['attachments'][2], 'image/webp')):
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(att['sha256'], digest)
            self.assertEqual((att_dir / att['file']).read_bytes(), raw)
            status, headers, body = self.raw_hub_get(f"/api/feedback/{backup['id']}/attachments/{digest}")
            self.assertEqual(status, 200)
            self.assertEqual(body, raw)
            self.assertEqual(headers.get('Content-Type'), expected_mime)
            self.assertIn('attachment;', headers.get('Content-Disposition', ''))
            self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')
            self.assertEqual(headers.get('Content-Security-Policy'), "default-src 'none'; sandbox")
            self.assertEqual(headers.get('Cache-Control'), 'no-store')
        other = self.hub.store.add_member('其他同事')
        first_digest = backup['attachments'][0]['sha256']
        self.assertEqual(self.raw_hub_get(f"/api/feedback/{backup['id']}/attachments/{first_digest}", token=other['token'])[0], 404)
        self.assertEqual(self.raw_hub_get(f"/api/feedback/{backup['id']}/attachments/{first_digest}", token='')[0], 401)
        self.assertEqual(self.raw_hub_get(f"/api/feedback/{backup['id']}/attachments/../members.json")[0], 404)

    def test_attachment_validation_magic_count_size_and_antiforgery(self):
        before_errors = len(diagnostics.recent(self.home))
        valid_png = make_png()
        valid_jpeg = make_jpeg()
        bad_cases = [
            ('svg', encode_shot(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', 'image/svg+xml')),
            ('html', encode_shot(b'<html><body>hi</body></html>', 'image/png')),
            ('text_as_png', encode_shot(b'not a png file at all 1234567890', 'image/png')),
            ('gif', encode_shot(b'GIF89a' + b'\x00' * 30, 'image/gif')),
            ('truncated_png', encode_shot(b'\x89PNG\r\n\x1a\n', 'image/png')),
            ('empty', {'content_type': 'image/png', 'data': ''}),
            ('bad_base64', {'content_type': 'image/png', 'data': '!!!not-base64!!!'}),
            ('mime_mismatch', encode_shot(valid_jpeg, 'image/png')),
        ]
        for name, bad in bad_cases:
            with self.subTest(case=name):
                code, res = self.request('/api/diagnostics/preview', {'error_id': self.row['error_id'], 'attachments': [bad]})
                self.assertEqual(code, 400, res)
                hub_code, _ = self.request('/api/feedback', self.preview()['payload'] | {'attachments': [bad]}, hub=True)
                self.assertEqual(hub_code, 400)
        four_shots = [encode_shot(valid_png, 'image/png') for _ in range(4)]
        self.assertEqual(self.request('/api/diagnostics/preview', {'error_id': self.row['error_id'], 'attachments': four_shots})[0], 400)
        self.assertEqual(self.request('/api/feedback', self.preview()['payload'] | {'attachments': four_shots}, hub=True)[0], 400)
        with patch.object(feedback, 'MAX_ATTACHMENT_BYTES', 200), patch.object(feedback, 'MAX_ATTACHMENTS_TOTAL_BYTES', 350):
            too_big = encode_shot(make_png(pad=250), 'image/png')
            self.assertEqual(self.request('/api/diagnostics/preview', {'error_id': self.row['error_id'], 'attachments': [too_big]})[0], 413)
            self.assertEqual(self.request('/api/feedback', self.preview()['payload'] | {'attachments': [too_big]}, hub=True)[0], 413)
            medium = encode_shot(make_png(pad=80), 'image/png')
            self.assertEqual(self.request('/api/diagnostics/preview', {'error_id': self.row['error_id'], 'attachments': [medium, medium, medium]})[0], 413)
            self.assertEqual(self.request('/api/feedback', self.preview()['payload'] | {'attachments': [medium, medium, medium]}, hub=True)[0], 413)
        # Normal attachment validation rejections must not pollute local fault logs.
        self.assertEqual(len(diagnostics.recent(self.home)), before_errors)
        # Oversized text alongside attachments still triggers 413.
        self.assertEqual(self.request('/api/feedback', self.preview()['payload'] | {'detail': 'x' * feedback.MAX_BODY, 'attachments': [encode_shot(valid_png, 'image/png')]}, hub=True)[0], 413)
        # Server recomputes attachment_count & summary, ignoring client forgery.
        forged = self.preview()['payload'] | {'attachment_count': '9', 'attachments_summary': '伪造截图说明'}
        with patch.object(feedback, 'forward') as forward:
            self.assertEqual(self.request('/api/feedback', forged, hub=True)[0], 200)
            sent = forward.call_args.args[1]
            self.assertEqual(sent['attachment_count'], '0')
            self.assertEqual(sent['attachments_summary'], '')
            self.assertNotIn('[附带截图]', sent['detail'])

    def test_hub_attachment_quota_eviction_and_atomic_rollback(self):
        base = self.preview()['payload']
        imgs = [make_png((i * 40, 20, 80), pad=180) for i in range(3)]
        limit = len(imgs[0]) * 2 + 20
        with patch.object(feedback, 'MAX_ATTACHMENT_STORE_BYTES', limit), patch.object(feedback, 'forward'):
            for i, raw in enumerate(imgs):
                code, res = self.request('/api/feedback', base | {
                    'error_id': f'err-{i}', 'fingerprint': f'fp-{i}',
                    'attachments': [encode_shot(raw, 'image/png')]}, hub=True)
                self.assertEqual(code, 200, res)
        rows = json.loads((self.data / 'feedback.json').read_text(encoding='utf-8'))
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]['attachments'][0].get('evicted'))
        self.assertFalse(rows[1]['attachments'][0].get('evicted', False))
        self.assertFalse(rows[2]['attachments'][0].get('evicted', False))
        att_dir = self.data / 'feedback-attachments'
        files = list(att_dir.iterdir())
        self.assertEqual(len(files), 2)
        self.assertLessEqual(sum(p.stat().st_size for p in files), limit)
        self.assertEqual(self.raw_hub_get(f"/api/feedback/{rows[0]['id']}/attachments/{rows[0]['attachments'][0]['sha256']}")[0], 404)
        self.assertEqual(self.raw_hub_get(f"/api/feedback/{rows[2]['id']}/attachments/{rows[2]['attachments'][0]['sha256']}")[0], 200)
        # Cooldown rejection must not write orphan attachment files to disk.
        orphan_img = make_png((200, 100, 50), pad=40)
        orphan_digest = hashlib.sha256(orphan_img).hexdigest()
        with patch.object(feedback, 'forward') as send:
            self.assertEqual(self.request('/api/feedback', base | {
                'error_id': 'err-cooldown', 'fingerprint': 'fp-2',
                'attachments': [encode_shot(orphan_img, 'image/png')]}, hub=True)[0], 429)
            send.assert_not_called()
        self.assertFalse((att_dir / f'{orphan_digest}.png').exists())
        # Atomic rollback on feedback.json write failure removes newly created files and keeps old ones.
        before_json = (self.data / 'feedback.json').read_bytes()
        before_files = {p.name for p in att_dir.iterdir()}
        real_replace = team_hub.os.replace
        def fail_feedback_json(src, dst):
            if Path(dst).name == 'feedback.json':
                raise OSError('模拟磁盘已满')
            return real_replace(src, dst)
        with patch.object(team_hub.os, 'replace', side_effect=fail_feedback_json), patch.object(feedback, 'forward') as send:
            self.assertEqual(self.request('/api/feedback', base | {
                'error_id': 'err-fail', 'fingerprint': 'fp-fail',
                'attachments': [encode_shot(orphan_img, 'image/png')]}, hub=True)[0], 500)
            send.assert_not_called()
        self.assertEqual((self.data / 'feedback.json').read_bytes(), before_json)
        self.assertEqual({p.name for p in att_dir.iterdir()}, before_files)
        # Eviction by MAX_RECORDS also cleans up files belonging to dropped rows.
        with patch.object(feedback, 'MAX_RECORDS', 1), patch.object(feedback, 'forward'):
            fresh_img = make_png((10, 200, 10), pad=20)
            self.assertEqual(self.request('/api/feedback', base | {
                'error_id': 'err-keep', 'fingerprint': 'fp-keep',
                'attachments': [encode_shot(fresh_img, 'image/png')]}, hub=True)[0], 200)
        kept_files = {p.name for p in att_dir.iterdir()}
        self.assertEqual(kept_files, {f"{hashlib.sha256(fresh_img).hexdigest()}.png"})

    def test_degraded_modes_old_hub_offline_readonly_and_manual_with_shots(self):
        png = encode_shot(make_png((90, 90, 90)), 'image/png')
        # Old hub supporting text feedback only (no feedback_attachments_supported).
        old_text_hub = {'api': 2, 'service': 'skill-pilot-team',
                        'member': {k: self.member[k] for k in ('id', 'name')},
                        'feedback_supported': True, 'feedback_configured': True}
        with patch.object(team, '_request', return_value=old_text_hub):
            plain = self.preview()
            self.assertTrue(plain['can_send'])
            self.assertFalse(plain['can_attach'])
            self.assertIn('暂不支持随反馈上传截图', plain['attach_reason'])
            with_shot = self.preview(attachments=[png])
            self.assertFalse(with_shot['can_send'])
            self.assertFalse(with_shot['can_attach'])
            self.assertIn('移除截图', with_shot['reason'])
        # Downgrade race: preview saw new hub, then hub reports no attachment support at submit time.
        prepared = self.preview(attachments=[png])
        self.assertTrue(prepared['can_send'])
        with patch.object(team, '_request', return_value=old_text_hub):
            code, err = self.request('/api/diagnostics/submit', {'ticket': prepared['ticket']})
            self.assertEqual(code, 400)
            self.assertIn('暂不支持上传截图', err['error'])
        # Read-only mode, offline hub, and disconnected team sharing all keep plain text preview + attach notice.
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            ro = self.preview()
            self.assertFalse(ro['can_send'])
            self.assertFalse(ro['can_attach'])
            self.assertIn('只读模式', ro['attach_reason'])
            self.assertEqual(ro['payload']['error_id'], self.row['error_id'])
        with patch.object(team, '_request', side_effect=ValueError('unreachable')):
            offline = self.preview()
            self.assertFalse(offline['can_send'])
            self.assertFalse(offline['can_attach'])
            self.assertIn('无法连接分享站', offline['attach_reason'])
            self.assertEqual(offline['payload']['error_id'], self.row['error_id'])
        (self.home / '.skill-pilot/team.json').unlink()
        disconnected = self.preview()
        self.assertFalse(disconnected['can_send'])
        self.assertFalse(disconnected['can_attach'])
        self.assertIn('尚未连接团队分享站', disconnected['attach_reason'])
        labels.atomic_write(self.home, 'team.json', json.dumps(self.config), 0o600)
        # Manual feedback (no error_id) with screenshot works end-to-end.
        status, manual = self.request('/api/diagnostics/preview', {'note': '主动反馈带图', 'attachments': [png]})
        self.assertEqual(status, 200, manual)
        self.assertTrue(manual['can_send'])
        self.assertEqual(manual['payload']['source'], 'manual')
        with patch.object(feedback, 'forward') as forward:
            self.assertEqual(self.request('/api/diagnostics/submit', {'ticket': manual['ticket']})[0], 200)
            sent = forward.call_args.args[1]
            self.assertEqual(sent['attachment_count'], '1')
            self.assertIn('[附带截图]', sent['detail'])

    def test_frontend_screenshot_validators_magic_count_and_size(self):
        source = (diagnostics.PROJECT / 'skill_pilot/web/static/app.js').read_text(encoding='utf-8')
        functions = '\n'.join(extract_function(source, signature) for signature in (
            'function formatFeedbackBytes(', 'function detectFeedbackImage(',
            'function bytesToBase64(', 'function validateFeedbackFileBytes('))
        png_b64 = base64.b64encode(make_png()).decode('ascii')
        jpeg_b64 = base64.b64encode(make_jpeg()).decode('ascii')
        webp_b64 = base64.b64encode(make_webp()).decode('ascii')
        script = f'''
const assert = require('node:assert/strict');
const FEEDBACK_MAX_SHOTS = 3;
const FEEDBACK_MAX_SHOT_BYTES = 2 * 1024 * 1024;
const FEEDBACK_MAX_SHOTS_TOTAL = 5 * 1024 * 1024;
{functions}
const toBytes = b64 => new Uint8Array(Buffer.from(b64, 'base64'));
const png = toBytes('{png_b64}');
const jpeg = toBytes('{jpeg_b64}');
const webp = toBytes('{webp_b64}');
assert.deepEqual(detectFeedbackImage(png), {{content_type: 'image/png', label: 'PNG'}});
assert.deepEqual(detectFeedbackImage(jpeg), {{content_type: 'image/jpeg', label: 'JPEG'}});
assert.deepEqual(detectFeedbackImage(webp), {{content_type: 'image/webp', label: 'WebP'}});
assert.equal(detectFeedbackImage(new Uint8Array(Buffer.from('<svg></svg>'))), null);
assert.equal(detectFeedbackImage(new Uint8Array(Buffer.from('plain text file'))), null);
assert.equal(bytesToBase64(png), '{png_b64}');
assert.equal(validateFeedbackFileBytes(png, 0, 0, 'image/png').ok, true);
assert.equal(validateFeedbackFileBytes(jpeg, 1, png.length, 'image/jpg').ok, true);
assert.equal(validateFeedbackFileBytes(jpeg, 0, 0, 'image/png').ok, false);
assert.equal(validateFeedbackFileBytes(png, 3, 100, 'image/png').ok, false);
assert.equal(validateFeedbackFileBytes(new Uint8Array(0), 0, 0, 'image/png').ok, false);
const huge = new Uint8Array(FEEDBACK_MAX_SHOT_BYTES + 1);
huge.set(png);
assert.equal(validateFeedbackFileBytes(huge, 0, 0, 'image/png').ok, false);
assert.equal(validateFeedbackFileBytes(png, 2, FEEDBACK_MAX_SHOTS_TOTAL - png.length + 1, 'image/png').ok, false);
'''
        result = subprocess.run(['node', '-e', script], cwd=diagnostics.PROJECT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
