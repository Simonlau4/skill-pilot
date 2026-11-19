import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tests import set_up_home
from skill_pilot import diagnostics, feedback, labels, self_update


class NetworkDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot connection ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_up_home(self, self.root / 'fresh user')
        self.home.mkdir()
        env = patch.dict(os.environ, {'SKILL_PILOT_HOME': str(self.home), 'SKILL_PILOT_READ_ONLY': '0'})
        env.start()
        self.addCleanup(env.stop)
        self.config = {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                       'model': 'fixture', 'api_key': 'fixture-private-key'}
        labels.save_settings(self.home, self.config)

    def connection_failure(self, error):
        with patch('skill_pilot.labels.urllib.request.OpenerDirector.open', side_effect=error):
            result = labels.test(self.home)
        self.assertFalse(result['ok'])
        row = diagnostics.recent(self.home)[0]
        self.assertEqual(row['user_message'], result['message'])
        self.assertEqual(row['error_id'], result['error_id'])
        return row

    def certificate_error(self, code=19):
        error = ssl.SSLCertVerificationError('certificate rejected')
        error.verify_code = code
        error.verify_message = 'self-signed certificate in certificate chain'
        return urllib.error.URLError(error)

    def test_certificate_feedback_keeps_request_evidence_without_secrets(self):
        proxy = {'https': 'http://private-user:proxy-password@private-proxy.example:8123'}
        with patch('urllib.request.getproxies', return_value=proxy), \
                patch('urllib.request.proxy_bypass', return_value=False):
            row = self.connection_failure(self.certificate_error())
        detail = row['detail']
        for evidence in ('DeepSeek 官方接口', '本次请求使用程序识别到的代理',
                         '证书校验编号：19', '自签名证书不受信任', '已加载的可信证书数',
                         '加载可信证书 → 连接服务并等待响应', '已用时间', '未取得签发者'):
            self.assertIn(evidence, detail)
        self.assertEqual(row['error_type'], 'SSLCertVerificationError')
        self.assertNotIn('读取返回内容', detail)
        text = json.dumps(row, ensure_ascii=False)
        for secret in ('fixture-private-key', 'proxy-password', 'private-proxy.example', 'private-user', str(self.home)):
            self.assertNotIn(secret, text)

    def test_failure_snapshot_survives_settings_and_runtime_changes_and_old_hub(self):
        before = {'os': 'Example OS 1', 'python_version': '3.12.1', 'app_version': 'a' * 12,
                  'architecture': 'test-cpu', 'install_mode': '源码'}
        with patch.object(diagnostics, 'environment', return_value=before):
            row = self.connection_failure(self.certificate_error())
        labels.save_settings(self.home, self.config | {'base_url': 'https://private-ai.example'})
        after = before | {'python_version': '3.15.2', 'app_version': 'b' * 12,
                          'tools': {}, 'agent_types': [], 'skill_count': 0}
        with patch.object(diagnostics, 'environment', return_value=after):
            report = diagnostics.bundle(self.home, row['error_id'])['report']
        payload = feedback.clean(feedback.envelope(report))
        self.assertEqual(payload['app_version'], before['app_version'])
        self.assertEqual(payload['python_version'], before['python_version'])
        self.assertEqual(report['environment']['app_version'], after['app_version'])
        self.assertIn('采集于出错时', payload['detail'])
        self.assertIn(report['environment_captured_at'], payload['detail'])
        self.assertIn('DeepSeek 官方接口', payload['detail'])
        self.assertNotIn('private-ai.example', json.dumps(payload))
        self.assertEqual(set(payload), set(feedback.LIMITS))

    def test_old_records_distinguish_current_environment_and_ignore_unknown_fields(self):
        row = self.connection_failure(TimeoutError('wait expired'))
        row.pop('environment')
        log = self.home / '.skill-pilot/logs/errors.jsonl'
        log.write_text(json.dumps(row) + '\n')
        report = diagnostics.bundle(self.home, row['error_id'])['report']
        self.assertIn('未保存出错时的版本', feedback.envelope(report)['detail'])
        row['environment'] = {'python_version': '3.12.1', 'api_key': 'never-forward'}
        log.write_text(json.dumps(row) + '\n')
        self.assertNotIn('never-forward', json.dumps(diagnostics.recent(self.home)))

    def test_proxy_bypass_and_custom_service_do_not_expose_addresses(self):
        labels.save_settings(self.home, self.config | {'base_url': 'https://private-ai.example:8443/v1'})
        with patch('urllib.request.getproxies', return_value={'https': 'http://private-proxy.example:1234'}), \
                patch('urllib.request.proxy_bypass', return_value=True) as bypass:
            row = self.connection_failure(urllib.error.URLError(TimeoutError('expired')))
            bypass.assert_called_with('private-ai.example:8443')
        self.assertIn('自定义服务', row['detail'])
        self.assertIn('本次地址按规则绕过', row['detail'])
        self.assertEqual(row['user_message'], labels.SLOW)
        self.assertNotIn('private-ai.example', json.dumps(row))
        self.assertNotIn('private-proxy.example', json.dumps(row))

    def test_certificate_configuration_failure_is_distinct_from_handshake_failure(self):
        with patch('skill_pilot.labels.ssl.create_default_context', side_effect=FileNotFoundError('missing CA file')), \
                patch('skill_pilot.labels.urllib.request.OpenerDirector.open') as request:
            result = labels.test(self.home)
        request.assert_not_called()
        row = diagnostics.recent(self.home)[0]
        self.assertIn('读取本机可信证书失败', result['message'])
        self.assertIn('经过步骤：准备请求 → 加载可信证书', row['detail'])
        self.assertNotIn('连接服务并等待响应', row['detail'])

    def test_hostname_mismatch_does_not_disclose_custom_service_domain(self):
        labels.save_settings(self.home, self.config | {'base_url': 'https://private-ai.example/v1'})
        error = self.certificate_error(62)
        error.reason.verify_message = "Hostname mismatch, certificate is not valid for 'private-ai.example'."
        row = self.connection_failure(error)
        self.assertIn('证书与服务地址不匹配', row['detail'])
        self.assertNotIn('private-ai.example', json.dumps(row))

    def test_request_secrets_remain_hidden_after_settings_change_in_flight(self):
        for host in ('old-private-ai.example', '内部.公司'):
            with self.subTest(host=host):
                old = self.config | {'base_url': 'https://' + host + '/v1',
                                     'api_key': 'old-private-key\r\nnever-share'}
                labels.save_settings(self.home, old)
                wire_host = host.encode('idna').decode('ascii').upper()
                error = self.certificate_error(62)
                error.reason.verify_message = 'Hostname mismatch: ' + wire_host
                error.reason.args = (wire_host + ' ' + repr(old['api_key'].encode()),)
                def change_settings(*args, **kwargs):
                    labels.save_settings(self.home, self.config | {'base_url': 'https://new-private-ai.example'})
                    raise error
                row = self.connection_failure(change_settings)
                report = diagnostics.bundle(self.home, row['error_id'])['report']
                payload = feedback.envelope(report)
                stored = (self.home / '.skill-pilot/logs/errors.jsonl').read_text()
                for text in (stored, json.dumps(payload, ensure_ascii=False)):
                    for secret in (host, wire_host, 'old-private-key', 'never-share', 'diagnostic_secrets'):
                        self.assertNotIn(secret, text)
                self.assertIn('证书校验编号：62', payload['detail'])
                self.assertIn('证书与服务地址不匹配', payload['detail'])

    def test_loaded_version_survives_installation_change_until_process_restart(self):
        code = self.root / 'separate program'
        shutil.copytree(diagnostics.PROJECT / 'skill_pilot', code / 'skill_pilot',
                        ignore=shutil.ignore_patterns('__pycache__'))
        script = '''
import json
from pathlib import Path
from skill_pilot import diagnostics, feedback, version
home = Path(__import__('os').environ['SKILL_PILOT_HOME'])
before = version.RUNNING_BUILD['version']
app_js = Path('skill_pilot/web/static/app.js')
app_js.write_text(app_js.read_text(encoding='utf-8') + '\\n// disk upgrade\\n', encoding='utf-8')
row = diagnostics.record(home, 'backend', 'fixture', RuntimeError('fixture'))
report = diagnostics.bundle(home, row['error_id'])['report']
print(json.dumps({'before': before, 'row': row, 'current': report['environment'], 'payload': feedback.envelope(report)}))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=code,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        before_label = data['before']
        after_label = data['current']['app_version']
        self.assertRegex(before_label, r'^v\d+\.\d+\.\d+-[0-9a-f]{7}$')
        self.assertRegex(after_label, r'^v\d+\.\d+\.\d+-[0-9a-f]{7}$')
        self.assertNotEqual(before_label, after_label)
        self.assertEqual(data['row']['environment']['app_version'], before_label)
        self.assertEqual(data['payload']['app_version'], before_label)
        restarted = subprocess.run([sys.executable, '-B', '-c',
            'from skill_pilot.diagnostics import RUNNING_BUILD; print(RUNNING_BUILD["app_version"])'],
            cwd=code, capture_output=True, text=True, timeout=30)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(restarted.stdout.strip(), after_label)

    def test_certificate_reasons_and_missing_local_trust_locations_are_visible(self):
        from types import SimpleNamespace
        for code, expected in ((10, '证书已过期'), (20, '找不到可信签发者'), (62, '证书与服务地址不匹配')):
            with self.subTest(code=code), patch('ssl.get_default_verify_paths',
                                              return_value=SimpleNamespace(cafile=None, capath=None)):
                row = self.connection_failure(self.certificate_error(code))
            self.assertIn('故障分类：' + expected, row['detail'])
            self.assertIn('可发现的证书文件：未找到', row['detail'])
            self.assertIn('可发现的证书目录：未找到', row['detail'])

    def test_dns_refusal_timeout_and_authorization_are_distinguishable(self):
        cases = [(urllib.error.URLError(socket.gaierror(-2, 'not found')), '网络地址'),
                 (urllib.error.URLError(ConnectionRefusedError('refused')), '拒绝了连接'),
                 (urllib.error.URLError(TimeoutError('expired')), '太久没回应'),
                 (urllib.error.HTTPError('https://api.deepseek.com', 401, 'unauthorized', {}, None), 'API key 不对')]
        for error, message in cases:
            with self.subTest(message=message):
                row = self.connection_failure(error)
                self.assertIn(message, row['user_message'])
                self.assertIn('连接服务并等待响应', row['detail'])
                if isinstance(error, urllib.error.HTTPError):
                    self.assertIn('响应状态：401', row['detail'])

    def test_shared_ai_failure_retains_message_when_caller_has_no_custom_summary(self):
        from skill_pilot import github_digest
        with patch('skill_pilot.labels.urllib.request.OpenerDirector.open', side_effect=self.certificate_error()):
            try:
                labels.completion(self.config, 'fixture', {})
            except labels.ServiceError as error:
                row = diagnostics.record(self.home, 'backend', '整理热门技能介绍', error)
                self.assertIn(str(error), github_digest.SERVICE_ERRORS)
        self.assertEqual(row['user_message'], labels.CERTIFICATE_ERROR)
        self.assertIn('证书校验编号：19', row['detail'])

    def test_update_context_is_correlated_without_copying_other_operation_logs(self):
        cause = subprocess.CalledProcessError(128, ['tool'], stderr='private unrelated output')
        wrapped = ValueError('download failed')
        wrapped.__cause__ = cause
        with patch.object(self_update, 'repository_git', side_effect=wrapped):
            try:
                self_update.git(self.root, 'fetch', self_update.SOURCE)
            except ValueError as error:
                record = self_update.record_failure(self.home, 'check', error)
        row = diagnostics.recent(self.home)[0]
        self.assertEqual(record['error_id'], row['error_id'])
        self.assertIn('失败步骤：下载或准备新版', row['detail'])
        self.assertIn('尝试次数：1', row['detail'])
        self.assertNotIn('private unrelated output', row['detail'])
        log = json.loads((self.home / '.skill-pilot/logs/program-update.jsonl').read_text().splitlines()[-1])
        self.assertEqual(log['error_id'], row['error_id'])

    @unittest.skipUnless(shutil.which('openssl'), '真实证书握手检查需要 openssl 测试工具')
    def test_real_untrusted_tls_never_sends_key_and_trusted_tls_succeeds(self):
        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=self.root, check=True, capture_output=True)
        (self.root / 'root.cnf').write_text('[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n'
            '[dn]\nCN=Fixture Root\n[ca]\nbasicConstraints=critical,CA:TRUE\n'
            'keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
        (self.root / 'leaf.cnf').write_text('[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=localhost\n')
        (self.root / 'leaf.ext').write_text('basicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n'
            'subjectAltName=DNS:localhost\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n')
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'root.key', '-out', 'root.crt',
                '-days', '1', '-config', 'root.cnf')
        openssl('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr', '-config', 'leaf.cnf')
        openssl('x509', '-req', '-in', 'leaf.csr', '-CA', 'root.crt', '-CAkey', 'root.key', '-CAcreateserial',
                '-out', 'leaf.crt', '-days', '1', '-extfile', 'leaf.ext')
        (self.root / 'chain.crt').write_bytes((self.root / 'leaf.crt').read_bytes() + (self.root / 'root.crt').read_bytes())
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                calls.append(self.headers.get('Authorization'))
                self.rfile.read(int(self.headers['Content-Length']))
                body = json.dumps({'choices': [{'message': {'content': '{"ok":true}'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root / 'chain.crt', self.root / 'leaf.key')
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            labels.save_settings(self.home, self.config | {'base_url': f'https://localhost:{server.server_port}'})
            with patch('urllib.request.getproxies', return_value={}):
                rejected = labels.test(self.home)
                self.assertFalse(rejected['ok'])
                self.assertEqual(calls, [])
                self.assertIn('证书校验编号：19', diagnostics.recent(self.home)[0]['detail'])
                with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.root / 'root.crt')}):
                    accepted = labels.test(self.home)
                self.assertTrue(accepted['ok'])
                self.assertEqual(calls, ['Bearer fixture-private-key'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
