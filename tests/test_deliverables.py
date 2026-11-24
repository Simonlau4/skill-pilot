"""Deliverables: a member uploads a static demo folder; any member can read it back."""
import hashlib
import http.client
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
from unittest.mock import patch

from skill_pilot import store, team, team_hub
from tests import test_team, test_team_hub


def sha(data):
    return hashlib.sha256(data).hexdigest()


PAGE = '<!doctype html><title>示例</title><img src="assets/a.png"><video src="assets/v.mp4"></video>'.encode()
IMAGE = b'\x89PNG\r\n\x1a\n' + b'0' * 40
VIDEO = bytes(range(256)) * 4


def manifest(files, **extra):
    return {'title': '示例成品', 'version': 'v1', 'summary': '示例说明', 'skills': ['acme-design', 'acme-writing'],
            'files': {path: {'sha256': sha(data), 'size': len(data)} for path, data in files.items()}, **extra}


class DeliverableHubTests(unittest.TestCase):
    # Borrowed through the module so discovery does not run those tests twice.
    Hub = test_team_hub.TeamHubTests
    setUp, close, request = Hub.setUp, Hub.close, Hub.request

    def raw(self, path, method='GET', body=None, token='first', headers=None):
        token = self.first['token'] if token == 'first' else token
        supplied = {'Authorization': 'Bearer ' + token} if token else {}
        supplied.update(headers or {})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, data, token='first'):
        status, _, body = self.raw('/api/blobs/me/' + sha(data), 'PUT', data, token,
                                   {'Content-Type': 'application/octet-stream'})
        self.assertEqual(status, 200, body)

    def publish(self, files, identity='demo-one', token='first', **extra):
        for data in files.values():
            self.upload(data, token)
        status, body = self.request('/api/deliverables/me/' + identity, 'PUT', manifest(files, **extra), token)
        self.assertEqual(status, 200, body)
        return body

    def deliverables(self, token='first'):
        status, body = self.request('/api/catalog', token=token)
        self.assertEqual(status, 200, body)
        return body['deliverables']

    def test_publish_lists_and_serves_files_with_ranges_to_any_member(self):
        files = {'index.html': PAGE, 'assets/a.png': IMAGE, 'assets/v.mp4': VIDEO}
        status, body = self.request('/api/deliverables/me/demo-one', 'PUT', manifest(files))
        self.assertEqual(status, 409)
        self.assertEqual(sorted(body['missing']), sorted(sha(d) for d in files.values()))
        self.assertEqual(self.raw('/api/blobs/me/' + sha(PAGE), 'PUT', PAGE + b'x')[0], 400)
        self.assertEqual(self.raw('/api/blobs/me/' + sha(PAGE), 'PUT', PAGE, token=None)[0], 401)
        self.publish(files)

        rows = self.deliverables(self.second['token'])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['key'], 'deliverable:' + self.first['id'] + ':demo-one')
        self.assertEqual(row['member'], {'id': self.first['id'], 'name': self.first['name']})
        self.assertEqual((row['title'], row['version'], row['skills']), ('示例成品', 'v1', ['acme-design', 'acme-writing']))
        self.assertEqual((row['file_count'], row['size']), (3, sum(len(d) for d in files.values())))
        self.assertNotIn('files', row)

        base = '/api/deliverables/' + self.first['id'] + '/demo-one/'
        status, headers, body = self.raw(base + 'index.html', token=self.second['token'])
        self.assertEqual((status, body), (200, PAGE))
        self.assertEqual(headers['Content-Type'], 'text/html; charset=utf-8')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Accept-Ranges'], 'bytes')
        self.assertEqual(self.raw(base + 'index.html', token=None)[0], 401)
        status, headers, body = self.raw(base + 'assets/v.mp4', headers={'Range': 'bytes=10-19'})
        self.assertEqual((status, body, headers['Content-Range']), (206, VIDEO[10:20], f'bytes 10-19/{len(VIDEO)}'))
        self.assertEqual(headers['Content-Type'], 'video/mp4')
        status, _, body = self.raw(base + 'assets/v.mp4', headers={'Range': 'bytes=-4'})
        self.assertEqual((status, body), (206, VIDEO[-4:]))
        self.assertEqual(self.raw(base + 'assets/v.mp4', headers={'Range': f'bytes={len(VIDEO)}-'})[0], 416)
        for missing in ('assets/none.png', '../demo-one/index.html', 'assets/../index.html', 'assets//a.png'):
            with self.subTest(path=missing):
                self.assertEqual(self.raw(base + missing)[0], 404)

    def test_republish_and_remove_clean_up_files_only_the_owner_can_change(self):
        self.publish({'index.html': PAGE, 'assets/a.png': IMAGE})
        blobs = self.data / 'deliverables' / self.first['id'] / 'blobs'
        self.assertTrue((blobs / sha(IMAGE)).is_file())
        self.publish({'index.html': PAGE, 'assets/v.mp4': VIDEO})
        self.assertFalse((blobs / sha(IMAGE)).exists())
        self.assertEqual(self.deliverables()[0]['file_count'], 2)
        self.assertEqual(self.request('/api/deliverables/me/demo-one', 'DELETE', token=self.second['token'])[0], 404)
        self.assertEqual(self.request('/api/deliverables/me/demo-one', 'DELETE')[0], 200)
        self.assertEqual(self.deliverables(), [])
        self.assertEqual(list(blobs.iterdir()), [])
        self.assertEqual(self.raw('/api/deliverables/' + self.first['id'] + '/demo-one/index.html')[0], 404)
        self.publish({'index.html': PAGE})
        self.store.remove_member(self.first['id'])
        self.assertFalse((self.data / 'deliverables' / self.first['id']).exists())

    def test_manifest_and_upload_limits(self):
        self.upload(PAGE)
        cases = [
            ({'files': {}}, '首页'),
            ({'files': {'other.html': {'sha256': sha(PAGE), 'size': len(PAGE)}}}, 'index.html'),
            ({'files': {'index.html': {'sha256': sha(PAGE), 'size': 1}}}, '大小'),
            ({'files': {'index.html': {'sha256': sha(PAGE), 'size': len(PAGE)},
                        '../x.png': {'sha256': sha(PAGE), 'size': len(PAGE)}}}, '位置'),
            ({'title': ''}, '名字'),
            ({'skills': ['../bad']}, '技能'),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                status, body = self.request('/api/deliverables/me/demo-one', 'PUT', {**manifest({'index.html': PAGE}), **change})
                self.assertEqual(status, 400, body)
                self.assertIn(message, body['error'])
        self.assertEqual(self.request('/api/deliverables/me/..', 'PUT', manifest({'index.html': PAGE}))[0], 404)
        with patch.object(team_hub, 'MAX_BLOB', 8):
            self.assertEqual(self.raw('/api/blobs/me/' + sha(PAGE), 'PUT', PAGE)[0], 413)
        with patch.object(team_hub, 'MAX_DELIVERABLE_SIZE', len(PAGE) - 1):
            self.assertEqual(self.request('/api/deliverables/me/demo-one', 'PUT', manifest({'index.html': PAGE}))[0], 413)
        with patch.object(team_hub, 'MAX_DELIVERABLE_FILES', 1):
            self.upload(IMAGE)
            files = {'index.html': PAGE, 'a.png': IMAGE}
            self.assertEqual(self.request('/api/deliverables/me/demo-one', 'PUT', manifest(files))[0], 413)
        self.assertEqual(self.deliverables(), [])


def find_browser():
    """A Chrome-family browser for the real-browser check; the test skips without one."""
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome', 'msedge'):
        if found := shutil.which(name):
            return found
    places = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
              '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            places += [os.path.join(os.environ[variable], 'Google', 'Chrome', 'Application', 'chrome.exe'),
                       os.path.join(os.environ[variable], 'Microsoft', 'Edge', 'Application', 'msedge.exe')]
    return next((place for place in places if os.path.isfile(place)), None)


def get(server, path, headers=None):
    """A plain browser-style request: no session token, the dashboard's own Host header."""
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        connection.request('GET', path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class DeliverableDashboardTests(unittest.TestCase):
    # Two synthetic homes, a loopback hub and both dashboards; borrowed through the module.
    Team = test_team.TeamTests
    setUp, start, skill, make_home, registry = Team.setUp, Team.start, Team.skill, Team.make_home, Team.registry
    request, ok, configure = Team.request, Team.ok, Team.configure

    def folder(self):
        root = self.root / 'Example deliverable'
        (root / 'assets').mkdir(parents=True)
        (root / 'index.html').write_bytes(PAGE)
        (root / 'assets/a.png').write_bytes(IMAGE)
        (root / 'assets/v.mp4').write_bytes(VIDEO)
        (root / '.env').write_text('example private value')
        (root / '.DS_Store').write_bytes(b'x')
        return root

    def publish(self, root, confirmed=True, **extra):
        return team.publish_deliverable(self.home, root, 'demo-one', '示例成品', summary='示例说明',
                                        skills=['acme-other', 'acme-missing'], confirmed=confirmed, **extra)

    def test_preview_upload_open_in_isolation_and_remove(self):
        self.configure()
        root = self.folder()
        preview = self.publish(root, confirmed=False)
        self.assertTrue(preview['needs_confirmation'])
        self.assertEqual((preview['file_count'], preview['size']), (3, len(PAGE) + len(IMAGE) + len(VIDEO)))
        self.assertEqual([row['path'] for row in preview['blocked']], ['.env'])
        self.assertEqual(self.ok(self.b, '/api/team/catalog')['deliverables'], [])
        self.assertEqual(self.publish(root)['uploaded'], 3)
        self.assertEqual(self.publish(root)['uploaded'], 0)

        catalog = self.ok(self.b, '/api/team/catalog')
        self.assertTrue(catalog['deliverables_supported'])
        row = catalog['deliverables'][0]
        self.assertEqual((row['title'], row['summary'], row['file_count'], row['mine']), ('示例成品', '示例说明', 3, False))
        self.assertEqual(row['skills'], [{'name': 'acme-other', 'installed': True, 'in_store': False},
                                         {'name': 'acme-missing', 'installed': False, 'in_store': False}])
        self.Team.publish(self, ('acme-other',))  # A skill on the team store is one a colleague can install from there.
        self.assertEqual([skill['in_store'] for skill in self.ok(self.b, '/api/team/catalog')['deliverables'][0]['skills']],
                         [True, False])
        opened = {'url': row['open_url']}
        self.assertTrue(opened['url'].endswith('/demo-one/'), opened)
        self.assertNotIn(self.b.token, opened['url'])
        base = opened['url'] + 'f/'
        # The tab holds a frame page of ours; the demo runs inside it and may only load its own files.
        status, headers, body = get(self.b, opened['url'])
        self.assertEqual(status, 200, body)
        self.assertIn(b'<iframe src="f/index.html"', body)
        self.assertIn(b'sandbox="allow-scripts"', body)
        frame = headers['Content-Security-Policy']
        self.assertIn("default-src 'none'", frame)
        self.assertIn('frame-src ' + self.b.origin + base + ';', frame)
        self.assertIn("frame-ancestors 'none'", frame)
        status, headers, body = get(self.b, base + 'index.html', {'Sec-Fetch-Dest': 'iframe'})
        self.assertEqual((status, body), (200, PAGE), body)
        policy = headers['Content-Security-Policy']
        self.assertIn('sandbox allow-scripts', policy)
        self.assertNotIn('allow-same-origin', policy)
        self.assertIn('connect-src ' + self.b.origin + base + ';', policy)
        self.assertNotIn(self.b.origin + ' ', policy)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['X-DNS-Prefetch-Control'], 'off')
        # Opened on its own as a page, a demo file goes back into the frame instead of running loose.
        status, headers, _ = get(self.b, base + 'index.html', {'Sec-Fetch-Dest': 'document'})
        self.assertEqual((status, headers['Location']), (303, opened['url']))
        self.assertEqual(get(self.b, opened['url'] + 'index.html')[0], 404)
        status, headers, body = get(self.b, base + 'assets/v.mp4', {'Range': 'bytes=0-9'})
        self.assertEqual((status, body, headers['Content-Range']), (206, VIDEO[:10], f'bytes 0-9/{len(VIDEO)}'))
        self.assertEqual(get(self.b, base + 'assets/none.png')[0], 404)
        forged = '/d/' + '0' * 32 + opened['url'][len('/d/') + 32:]
        self.assertEqual(get(self.b, forged)[0], 404)
        self.assertEqual(get(self.b, opened['url'], {'Host': 'localhost:' + str(self.b.server_port)})[0], 403)
        # The other dashboard session cannot reuse this address.
        self.assertEqual(get(self.a, opened['url'])[0], 404)

        self.assertTrue(team.remove_deliverable(self.home, 'demo-one')['needs_confirmation'])
        self.assertEqual(len(self.ok(self.b, '/api/team/catalog')['deliverables']), 1)
        team.remove_deliverable(self.home, 'demo-one', confirmed=True)
        self.assertEqual(self.ok(self.b, '/api/team/catalog')['deliverables'], [])
        self.assertEqual(get(self.b, base + 'index.html', {'Sec-Fetch-Dest': 'iframe'})[0], 404)

    def test_a_first_upload_of_the_most_files_allowed_goes_through(self):
        # The hub names every missing file on the first try; that list outgrows a 64 KB read past ~1000 files.
        self.configure()
        root = self.root / 'Many files'
        (root / 'parts').mkdir(parents=True)
        (root / 'index.html').write_bytes(PAGE)
        for index in range(1, team_hub.MAX_DELIVERABLE_FILES):
            (root / f'parts/{index:04}.txt').write_text(f'第 {index} 段\n')
        done = self.publish(root)
        self.assertEqual((done['uploaded'], done['file_count']), (team_hub.MAX_DELIVERABLE_FILES,) * 2)
        self.assertEqual(self.ok(self.b, '/api/team/catalog')['deliverables'][0]['file_count'], team_hub.MAX_DELIVERABLE_FILES)

    def test_detail_gives_recipe_and_teardown_address_and_tool_check_only_looks(self):
        self.configure()
        root = self.folder()
        (root / '_teardown').mkdir()
        (root / '_teardown/teardown.json').write_text(json.dumps({'version': 1, 'items': [], 'screens': []}))
        self.publish(root, recipe='# 示例配方\n\n- 第一步\n')
        row = self.ok(self.b, '/api/team/catalog')['deliverables'][0]
        self.assertTrue(row['has_teardown'] and row['has_recipe'])
        detail = self.ok(self.b, '/api/team/deliverables/detail?key=' + row['key'])
        self.assertEqual(detail['recipe'], '# 示例配方\n\n- 第一步\n')
        self.assertEqual(detail['base_url'], row['open_url'] + 'f/')
        status, _, body = get(self.b, detail['base_url'] + '_teardown/teardown.json')
        self.assertEqual((status, json.loads(body)['version']), (200, 1))
        self.assertEqual(self.request(self.b, '/api/team/deliverables/detail?key=bad')[0], 400)

        tools = self.root / 'Example tools'
        tools.mkdir()
        entry = tools / ('acme-tool.cmd' if os.name == 'nt' else 'acme-tool')
        entry.write_text('this tool must never be executed')
        entry.chmod(0o755)
        with patch.dict(os.environ, {'PATH': str(tools)}), \
                patch('subprocess.run', side_effect=AssertionError('工具检查不执行命令')):
            found = self.ok(self.b, '/api/tools/check?names=acme-tool,acme-missing')
        self.assertEqual(found, {'acme-tool': True, 'acme-missing': False})
        for bad in ('../acme-tool', 'acme tool', '', 'a,' * 40):
            with self.subTest(names=bad):
                self.assertEqual(self.request(self.b, '/api/tools/check?names=' + urllib.parse.quote(bad, safe=','))[0], 400)

    def test_playwright_cli_is_detected_from_the_users_desktop_tool_directories(self):
        suffix = '.cmd' if os.name == 'nt' else ''
        for folder in ('.local/bin', 'bin', '.npm-global/bin', '.volta/bin', '.nvm/versions/node/v24.1.0/bin', 'AppData/Roaming/npm'):
            with self.subTest(folder=folder):
                entry = self.b.home / folder / ('playwright-cli' + suffix)
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.write_text('this tool must never be executed')
                entry.chmod(0o755)
                try:
                    with patch.dict(os.environ, {'PATH': str(self.root / 'Empty path')}), \
                            patch('subprocess.run', side_effect=AssertionError('工具检查不执行命令')):
                        found = self.ok(self.b, '/api/tools/check?names=playwright,playwright-cli')
                    self.assertEqual(found, {'playwright': True, 'playwright-cli': True})
                finally:
                    entry.unlink()

    def test_standard_playwright_entry_does_not_claim_the_separate_cli_is_installed(self):
        tools = self.root / 'Custom tool directory'
        tools.mkdir()
        entry = tools / ('playwright.cmd' if os.name == 'nt' else 'playwright')
        entry.write_text('this tool must never be executed')
        entry.chmod(0o755)
        with patch.dict(os.environ, {'PATH': str(tools)}), \
                patch('skill_pilot.web.server.tool_environment', return_value={'PATH': str(tools)}):
            self.assertEqual(self.ok(self.b, '/api/tools/check?names=playwright,playwright-cli'),
                             {'playwright': True, 'playwright-cli': False})
            entry.unlink()
            self.assertEqual(self.ok(self.b, '/api/tools/check?names=playwright,playwright-cli'),
                             {'playwright': False, 'playwright-cli': False})

    def test_a_viewer_leaving_mid_file_is_quiet_and_serving_continues(self):
        self.configure()
        root = self.folder()
        (root / 'assets/big.mp4').write_bytes(os.urandom(8 * 1024 * 1024))
        self.publish(root)
        base = self.ok(self.b, '/api/team/catalog')['deliverables'][0]['open_url'] + 'f/'
        errors = []
        self.b.handle_error = self.hub.handle_error = lambda request, address: errors.append(address)
        for _ in range(3):
            connection = http.client.HTTPConnection('127.0.0.1', self.b.server_port, timeout=5)
            connection.request('GET', base + 'assets/big.mp4')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read(4096)
            connection.close()  # Like a video scrolled out of view.
        self.assertEqual(get(self.b, base + 'index.html')[:3:2], (200, PAGE))
        time.sleep(.5)  # Let the abandoned transfers finish failing.
        self.assertEqual(errors, [])

    def test_folder_checks_read_only_and_command_line(self):
        self.configure()
        root = self.folder()
        (root / 'index.html').unlink()
        with self.assertRaisesRegex(ValueError, 'index.html'):
            self.publish(root, confirmed=False)
        (root / 'index.html').write_bytes(PAGE + b'<link href="https://fonts.example.com/x.css">')
        self.assertEqual(self.publish(root, confirmed=False)['external'], ['fonts.example.com'])
        (root / 'link.png').symlink_to(root / 'assets/a.png')
        with self.assertRaisesRegex(ValueError, '链接'):
            self.publish(root, confirmed=False)
        (root / 'link.png').unlink()
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            with self.assertRaisesRegex(ValueError, '只读'):
                self.publish(root)

        env = {**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        def run(*args):
            return subprocess.run([sys.executable, '-m', 'skill_pilot', 'team', *args], cwd=store.PROJECT, env=env,
                                  capture_output=True, text=True, timeout=60)
        shown = run('deliverable', str(root), '--id', 'demo-two', '--title', '示例成品二', '--skill', 'acme-other')
        self.assertEqual(shown.returncode, 2, shown.stdout + shown.stderr)
        self.assertIn('3 个文件', shown.stdout)
        self.assertIn('不上传 .env', shown.stdout)
        self.assertIn('fonts.example.com', shown.stdout)
        self.assertEqual(self.ok(self.b, '/api/team/catalog')['deliverables'], [])
        done = run('deliverable', str(root), '--id', 'demo-two', '--title', '示例成品二', '--skill', 'acme-other', '--yes')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('已上架成品', done.stdout)
        self.assertEqual([r['id'] for r in self.ok(self.b, '/api/team/catalog')['deliverables']], ['demo-two'])
        removed = run('deliverable-remove', 'demo-two', '--yes')
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(self.ok(self.b, '/api/team/catalog')['deliverables'], [])

    def test_in_a_real_browser_a_demo_cannot_leave_for_another_site(self):
        browser = find_browser()
        if not browser:
            self.skipTest('这台电脑没有 Chrome 或 Edge，跳过真实浏览器检查。')
        hits = []
        class Receiver(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(204)
                self.end_headers()
            do_POST = do_GET
            def log_message(self, *args):
                pass
        receiver = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        threading.Thread(target=receiver.serve_forever, daemon=True).start()
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        elsewhere = f'http://127.0.0.1:{receiver.server_port}'
        # Each page fetches "trying.txt" (proof its script ran) right before its one way out; pages that are
        # refused without being unloaded then fetch "after.txt".
        attempts = {
            'stay': f"""fetch("{elsewhere}/fetch?text=1").catch(() => {{}}); new Image().src = "{elsewhere}/image?text=1";
                document.getElementById("top").click(); document.getElementById("blank").click();
                window.open("{elsewhere}/open?text=1"); document.getElementById("form").submit();
                try {{ top.location.href = "{elsewhere}/top?text=1"; }} catch (error) {{}}""",
            'script': f'location.href = "{elsewhere}/script?text=1";',
            'link': 'document.getElementById("plain").click();',
            'refresh': f"""const meta = document.createElement("meta"); meta.httpEquiv = "refresh";
                meta.content = "0;url={elsewhere}/refresh?text=1"; document.head.append(meta);""",
        }
        self.configure()
        for name, attempt in attempts.items():
            root = self.root / f'Leaving {name}'
            root.mkdir()
            (root / 'trying.txt').write_text('trying')
            (root / 'after.txt').write_text('after')
            (root / 'index.html').write_text(f"""<!doctype html><meta charset="utf-8"><title>{name}</title>
<a id="plain" href="{elsewhere}/link?text=1">plain</a><a id="top" href="{elsewhere}/link-top?text=1" target="_top">top</a>
<a id="blank" href="{elsewhere}/link-blank?text=1" target="_blank">blank</a>
<form id="form" action="{elsewhere}/form"><input name="text" value="1"></form>
<script>fetch("trying.txt").then(() => {{ {attempt}; setTimeout(() => fetch("after.txt"), 300); }});</script>""")
            team.publish_deliverable(self.home, root, 'leave-' + name, '外跳检查 ' + name, confirmed=True)
        rows = {row['id']: row for row in self.ok(self.b, '/api/team/catalog')['deliverables']}
        relayed = []
        original = team.open_deliverable_file
        def recording(home, member, identity, path, byte_range):
            relayed.append(identity + '/' + path)
            return original(home, member, identity, path, byte_range)
        expected = {f'leave-{name}/trying.txt' for name in attempts} | {'leave-stay/after.txt'}
        urls = [elsewhere + '/control'] + [self.b.origin + rows['leave-' + name]['open_url'] for name in attempts]
        with patch.object(team, 'open_deliverable_file', recording):
            # Real time, not virtual time: the demo frame runs in a process of its own. Headless
            # Chrome opens one address per run, so each page gets its own browser.
            browsing = [subprocess.Popen([browser, '--headless=new', '--disable-gpu', '--no-first-run',
                                          '--no-default-browser-check', '--no-proxy-server', '--disable-extensions',
                                          '--disable-background-networking',
                                          f'--user-data-dir={self.root / f"Browser profile {index}"}', url],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        for index, url in enumerate(urls)]
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and not ('/control' in hits and expected <= set(relayed)):
                    time.sleep(.1)
                time.sleep(1.5)  # Long enough for any way out to reach the receiver.
            finally:
                for process in browsing:
                    process.terminate()
                for process in browsing:
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        self.assertIn('/control', hits, '浏览器要能打开接收站，这项检查才有意义')
        self.assertLessEqual(expected, set(relayed), '演示页的脚本没有全部运行')
        self.assertEqual([hit for hit in hits if hit != '/favicon.ico'], ['/control'], '演示页不该连到别的网站')

class DeliverableStaticTests(unittest.TestCase):
    def test_team_page_shelf_opens_demos_in_a_separate_tab_and_comes_first(self):
        from tests.test_nav_static import STATIC, extract_function
        source = (STATIC / 'app.js').read_text()
        functions = '\n'.join(extract_function(source, name) for name in (
            'function storeDeliverablesShelfHtml(', 'function deliverableCardHtml('))
        helpers = '\n'.join(line for line in source.splitlines() if line.startswith((
            'const escape =', 'const icon =', 'const number =', 'const sizeText =')))
        script = helpers + '\nlet state = {};\n' + functions + r'''
const fail = message => { console.error(message); process.exit(1); };
state.store = {deliverables: []};
if (storeDeliverablesShelfHtml() !== '') fail('没有成品时不该显示这一排');
state.store.deliverables = [{key: 'deliverable:m:demo', id: 'demo', member: {id: 'm', name: '示例成员'},
  title: '<b>示例成品</b>', version: 'v1', summary: '示例说明', size: 2 * 1048576, updated_at: '2026-11-11T03:28:00Z',
  skills: [{name: 'acme-design', installed: true}, {name: 'acme-data', installed: false}],
  open_url: '/d/key/m/demo/'}];
const html = storeDeliverablesShelfHtml();
if (!html.includes('<h2 id="shelf-deliverables">成品</h2>')) fail('缺少“成品”标题：' + html);
if (html.includes('<b>示例成品</b>') || !html.includes('&lt;b&gt;')) fail('成品名字要转义：' + html);
if (!/href="\/d\/key\/m\/demo\/" target="_blank" rel="noopener noreferrer"/.test(html)) fail('演示要在新标签页、断开来源打开：' + html);
if (!html.includes('已安装 1 个，缺 1 个')) fail('要写本机装了几个、缺几个：' + html);
if (!html.includes('示例成员 上架 · v1') || !html.includes('2.0 MB')) fail('要写上架人、版本和大小：' + html);
if (!html.includes('只限团队内部')) fail('要标明只限团队内部');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shelves = extract_function(source, 'function renderStoreShelves(')
        for start in (
            'box.innerHTML = deliverableSection + emptyTeam + storeGithubOverviewHtml();',
            'box.innerHTML = deliverableSection + packSection + githubSection;',
            'box.innerHTML = deliverableSection + packSection + storeDuoBoardHtml(cards) + githubSection;',
        ):
            self.assertIn(start, shelves, '成品要排在技能包和推荐前面')


    def test_teardown_recipe_and_tools_render_only_known_fields_escaped(self):
        from tests.test_nav_static import STATIC, extract_function
        source = (STATIC / 'app.js').read_text()
        functions = '\n'.join(extract_function(source, name) for name in (
            'function cleanTeardown(', 'function mdHtml(', 'function toolStatus(', 'function ddItemStatus(', 'function ddScreensHtml(',
            'function ddItemsHtml(', 'function ddCommand(', 'function ddRecipeHtml('))
        helpers = '\n'.join(line for line in source.splitlines() if line.startswith((
            'const escape =', 'const icon =', 'const number =', 'const emptyInner =', 'const KIND_LABEL =', 'const KIND_TAG =',
            'const COMMAND_NAME =', 'const clip =', 'const mdInline =')))
        script = helpers + '\nlet state = {table: {skills: [{name: "acme-writing"}]}};\nlet dd = null;\n' + functions + r'''
const fail = message => { console.error(message); process.exit(1); };
const teardown = cleanTeardown({items: [
    {id: 'acme-writing', kind: 'skill', name: '<b>写法</b>', summary: '示例'},
    {id: 'acme-tool', kind: 'tool', name: '示例工具', check: 'acme-tool', install: {mac: 'brew install acme'}, without: '手动做'},
    {id: '../bad', kind: 'tool', name: '坏的'}, {id: 'acme-odd', kind: 'unknown', name: '不认识'}],
  screens: [{id: 's01', chapter: '00', chapter_name: '开场', title: '<img src=x onerror=alert(1)>', image: '_teardown/s01.jpg',
    marks: [{n: 9, x: 150, y: -20, item: 'acme-writing', name: '标题', how: '一句判断', evidence: '记录'},
            {x: 10, y: 10, item: 'missing', name: '丢弃'}]},
    {id: 's02', image: '../../secret.png', marks: [{x: 50, y: 50, item: 'acme-tool', name: '工具', how: '用法'}]}]});
if ([...teardown.items.keys()].join() !== 'acme-writing,acme-tool') fail('不认识的条目要丢掉：' + [...teardown.items.keys()]);
const mark = teardown.screens[0].marks[0];
if (teardown.screens[0].marks.length !== 1 || mark.n !== 1 || mark.x !== 100 || mark.y !== 0) fail('标注编号重排、坐标限制在 0–100：' + JSON.stringify(mark));
if (teardown.screens[1].image !== '' || teardown.screens[1].marks[0].evidence !== '推断') fail('图片位置只认 _teardown/，没写证据算推断');
dd = {row: {title: '示例成品', skills: [{name: 'acme-writing', installed: true}, {name: 'acme-missing', installed: false, in_store: true},
                         {name: 'acme-elsewhere', installed: false, in_store: false}]},
      base: '/d/key/m/demo/', recipe: '# 配方\n\n| 技能 | 用途 |\n|---|---|\n| `acme` | <script>x</script> |\n\n- **第一步**\n1. 第二步',
      teardown, tab: 'screens', screen: 0, picked: 1, item: '', tools: {'acme-tool': false}};
const screens = ddScreensHtml();
if (screens.includes('<img src=x') || !screens.includes('&lt;img src=x')) fail('标题要转义：' + screens);
if (!screens.includes('src="/d/key/m/demo/_teardown/s01.jpg"') || !screens.includes('left:100%;top:0%')) fail('截图和标注位置不对：' + screens);
if (!screens.includes('你的电脑：已安装') || !screens.includes('&lt;b&gt;写法')) fail('卡片要写本机状态并转义名字：' + screens);
const items = ddItemsHtml();
if (!items.includes('data-dd-goto="0"') || !items.includes('1 处')) fail('按技能和工具看要能跳回那一屏：' + items);
dd.item = 'acme-tool';
if (!ddItemsHtml().includes('未检测到')) fail('工具没找到时只能说未检测到');
const recipe = ddRecipeHtml();
if (recipe.includes('<script>') || !recipe.includes('&lt;script&gt;')) fail('配方要转义：' + recipe);
for (const part of ['<h3>配方</h3>', '<th>技能</th>', '<code>acme</code>', '<strong>第一步</strong>', '<ol><li>第二步</li></ol>',
                    '已安装 1 个，缺 2 个', '照着团队成品「示例成品」', '没有的话：手动做', 'data-dd-tools',
                    '未安装，可在团队页安装', '未安装，团队商店里没有，见配方'])
  if (!recipe.includes(part)) fail('配方页缺：' + part + '\n' + recipe);
// The handover stands on its own: a new Agent gets the recipe, the skills and the tools, nothing it would have to find.
const command = ddCommand();
for (const part of ['照着团队成品「示例成品」，给 [品牌名] 做一份', '缺哪个先告诉我', '- acme-writing：已安装',
                    '- acme-missing：还没装，先在 Skill Pilot 团队页安装，或运行 skill-pilot team install acme-missing',
                    '- acme-elsewhere：还没装，团队商店里也没有，按下面配方里写的方式安装',
                    '- 示例工具（未检测到）；Mac 装法：brew install acme；没有的话：手动做', '配方：\n# 配方', '- **第一步**'])
  if (!command.includes(part)) fail('开工指令缺：' + part + '\n' + command);
if (command.includes('deliverables-skill-recipes')) fail('开工指令不能依赖成品没写明的技能：' + command);
if (!recipe.includes(escape(command))) fail('页面上要显示完整的开工指令');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = extract_function(source, 'function showPage(')
        self.assertIn("if (page === 'tools') enterTools();", page)
        self.assertIn('只检查常用命令入口，不替你安装', (STATIC / 'index.html').read_text())

    def test_tool_status_and_install_instructions_do_not_claim_unchecked_tools_are_missing(self):
        from tests.test_nav_static import STATIC, extract_function
        source = (STATIC / 'app.js').read_text()
        functions = '\n'.join(extract_function(source, name) for name in (
            'function toolStatus(', 'function ddItemStatus(', 'function toolCommandHtml(', 'function renderTools('))
        helpers = '\n'.join(line for line in source.splitlines() if line.startswith((
            'const escape =', 'const icon =', 'const number =', 'const emptyInner =', 'const toolsView =', 'const toolPresent =')))
        script = helpers + r'''
const fail = message => { console.error(message); process.exit(1); };
const elements = new Map();
const $ = id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); };
function renderButtons(element, html) { element.innerHTML = html; }
let state = {page: 'tools'}, dd = {tools: {}};
const tool = {kind: 'tool', name: 'Playwright', check: 'playwright', users: ['示例'], mac: 'example-install', without: '手动做'};
toolsView.rows = [tool, {name: '在线服务', users: []}];
''' + functions + r'''
for (const [value, label] of [[undefined, '尚未检查'], [true, '已检测到'], [false, '未检测到']]) {
  toolsView.checked = dd.tools = value === undefined ? {} : {playwright: value};
  if (toolPresent(tool) !== (value ?? null)) fail('尚未检查不能算缺失');
  if (ddItemStatus(tool) !== label) fail('成品详情要使用同一套状态');
  renderTools();
  const html = $('tools-list').innerHTML;
  if (!html.includes(label) || !html.includes('需要账号或人工确认')) fail('工具状态不正确：' + html);
  if (html.includes('本机没有') || html.includes('本机有')) fail('不能把命令入口检查说成完整安装检查');
  if (html.includes('<details>') !== (value === true) || html.includes('<details open')) fail('已检测到时收起安装说明');
  toolsView.missingOnly = true;
  renderTools();
  if ($('tools-list').innerHTML.includes('<h2>Playwright</h2>') !== (value === false)) fail('只把明确未检测到的列入筛选');
  toolsView.missingOnly = false;
}
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tool_checks_go_in_batches_the_dashboard_accepts(self):
        from tests.test_nav_static import STATIC, extract_function
        source = (STATIC / 'app.js').read_text()
        functions = '\n'.join(extract_function(source, name) for name in (
            'function cleanTeardown(', 'async function checkTools(', 'async function openDeliverable(', 'async function enterTools('))
        for name in ('async function openDeliverable(', 'async function enterTools('):
            self.assertNotIn('/api/tools/check', extract_function(source, name), '工具检查要走 checkTools 分批')
        helpers = '\n'.join(line for line in source.splitlines() if line.startswith((
            'const KIND_LABEL =', 'const COMMAND_NAME =', 'const clip =', 'const TOOL_CHECK_BATCH =', 'const toolsView =',
            'const sizeText =')))
        script = helpers + r'''
const fail = message => { console.error(message); process.exit(1); };
const tools = (prefix, count) => ({items: Array.from({length: count}, (_, i) => ({id: `${prefix}-${i}`, kind: 'tool', name: `${prefix} ${i}`, check: `${prefix}-${i}`})), screens: []});
const teardowns = {'/one/': tools('acme', 31), '/two/': tools('beta', 20)};
let state = {page: 'tools', store: {deliverables: [
  {key: 'deliverable:m:one', title: '一号', has_teardown: true, member: {name: '示例'}, size: 1, skills: [], open_url: '/d/x/m/one/'},
  {key: 'deliverable:m:two', title: '二号', has_teardown: true, member: {name: '示例'}, size: 1, skills: [], open_url: '/d/x/m/two/'}]}};
let calls = 0, dd = null;
async function api(path) {
  if (path.startsWith('/api/team/deliverables/detail')) return {recipe: '', base_url: path.endsWith('one') ? '/one/' : '/two/'};
  const names = decodeURIComponent(path.split('names=')[1]).split(',');
  if (names.length > 30) throw new Error('工具名只能用字母、数字、点、加号、横线或下划线');
  calls += 1;
  return Object.fromEntries(names.map(name => [name, name.endsWith('-1')]));
}
async function fetch(url) { return {ok: true, json: async () => teardowns[url.replace('_teardown/teardown.json', '')]}; }
async function loadStore() {}
function renderTools() {}
function renderDeliverable() {}
const element = {textContent: '', href: '', open: true, showModal() {}};
const $ = () => element;
''' + functions + r'''
(async () => {
  await enterTools();
  if (toolsView.error) fail('工具页报错：' + toolsView.error);
  if (toolsView.rows.length !== 51 || Object.keys(toolsView.checked).length !== 51) fail('两份成品合计 51 个工具都要查到：' + Object.keys(toolsView.checked).length);
  if (toolsView.checked['acme-1'] !== true || toolsView.checked['acme-2'] !== false) fail('分批查到的结果要合并');
  calls = 0;
  await openDeliverable('deliverable:m:one', 'recipe');
  if (dd.error) fail('照着做一份报错：' + dd.error);
  if (Object.keys(dd.tools).length !== 31 || calls !== 2) fail('一份成品 31 个工具要分两批查完：' + calls);
  api = async () => { throw new Error('模拟断线'); };
  await enterTools();
  if (toolsView.error !== '模拟断线' || toolsView.checkedAt !== null || Object.keys(toolsView.checked).length) fail('重查失败不能留下旧结果和旧检查时间');
})().catch(error => fail(error.stack));
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
