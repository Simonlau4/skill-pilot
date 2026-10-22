from ..platform_support import rmtree
"""Loopback dashboard; every migration write requires an explicit confirmation."""
import difflib
from ..platform_support import fcntl, user_home, user_id, check_directory_links
import hashlib
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..scan import scan
from ..store import SKIP_CONTENT, apply, backups, guard, preview, restore, target_home
from .. import operations, updates, cleanup, labels, tags, sharing, team, self_update, extra_sources
from .. import agents as agent_management, agent_discovery
from ..sources import github
from . import runtime

STATIC = Path(__file__).parent / "static"
HEARTBEAT_TIMEOUT = 30
# Browsers slow or pause timers in background tabs, so an open page keeps the
# dashboard alive until it says goodbye or has been silent this long.
OPEN_PAGE_TIMEOUT = 24 * 3600


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, inventory, resume=None):
        session = resume or {}
        self.token = session.get('token', secrets.token_urlsafe(32))
        self.inventory = inventory
        self.home = Path(inventory.get("home", target_home()))
        extra_sources.ensure(self.home)
        self.install_reviews = session.get('install_reviews', {})
        self.team_upload_reviews = session.get('team_upload_reviews', {})
        self.team_queue = []
        self.team_queue_lock = threading.RLock()
        self.team_queue_thread = None
        self.update_reviews = session.get('update_reviews', {})
        self.update_lock = threading.Lock()
        self.update_thread = None
        self.labels_lock = threading.Lock()
        self.labels_thread = None
        self.labels_progress = session.get('labels_progress', {'running': False, 'done': 0, 'total': 0, 'message': '', 'error': ''})
        self.program_review = session.get('program_review')
        self.program_progress = session.get('program_progress', {'running': False, 'phase': 'idle',
            'message': '从 GitHub 官方项目获取新版', 'error': '', 'available': False, 'current': '', 'latest': ''})
        self.program_progress.setdefault('automatic', False)
        if self.program_progress['phase'] == 'ready':
            self.program_progress.update(phase='latest', message='SP 已升级到最新版')
        # 换进程时没有还在跑的线程。自动检查被关掉或刷新打断时，按还没查到处理，避免一直显示正在检查。
        if (resume and self.program_progress.get('automatic') and self.program_progress.get('running')
                and self.program_progress.get('phase') == 'check'):
            self.program_progress.update(running=False, phase='idle', available=False, error='',
                                         message='还没查到新版信息')
        self.program_thread = None
        self.last_heartbeat = time.monotonic()
        self.pages = set(session.get('pages', []))
        self.code_version = runtime.version()
        self.runtime_lock = threading.Lock()
        self.active_requests = 0
        self.reload_callback = None
        self.reload_pending = False
        self.reload_error = ''
        self.reload_thread = None
        self.closing = False
        self.agents_generation = 0
        self.agents_cache = None
        self.agents_lock = threading.Lock()
        self.agent_findings = agent_discovery.read(self.home)
        self.agents_progress = {'running': False, 'checked_at': self.agent_findings['checked_at'],
                                'message': '', 'error': ''}
        self.agents_thread = None
        self.inventory_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), Handler, bind_and_activate=not bool(resume))
        if resume:
            self.socket.close()
            self.socket = socket.socket(fileno=resume['socket_fd'])
            self.server_address = self.socket.getsockname()
            self.server_name, self.server_port = self.server_address
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.url = f"{self.origin}/?token={self.token}"

    def server_close(self):
        with self.team_queue_lock:
            self.closing = True
        super().server_close()
        with self.team_queue_lock:
            queue_thread = self.team_queue_thread
        if queue_thread: queue_thread.join()
        if self.update_thread: self.update_thread.join()
        if self.labels_thread: self.labels_thread.join()
        if self.agents_thread: self.agents_thread.join()
        if self._program_thread_blocks_exit():
            self.program_thread.join()
        if self.reload_thread: self.reload_thread.join()
        if self.program_review:
            rmtree(self.program_review['temporary'], ignore_errors=True)

    def _program_thread_blocks_exit(self):
        # 打开看板时的自动检查不挡住关看板和刷新应用新版；手动检查和升级仍要等。
        thread = self.program_thread
        return bool(thread and thread.is_alive() and not self.program_progress.get('automatic'))

    def background_busy(self):
        return (any(thread and thread.is_alive() for thread in (self.update_thread, self.labels_thread, self.agents_thread)) or
                any(item['status'] in {'waiting', 'running'} for item in self.team_queue_status()['items']) or
                self._program_thread_blocks_exit() or
                self.labels_progress['running'] or any(r['running'] for r in list(self.update_reviews.values())))

    def team_queue_status(self):
        with self.team_queue_lock:
            return {'items': [{**item, 'installed': list(item['installed']), 'reuse': list(item['reuse'])}
                              for item in self.team_queue]}

    def queue_team(self, key):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('请选择要装上的技能。')
        with self.team_queue_lock:
            previous = next((item for item in self.team_queue if item['key'] == key), None)
            if previous and previous['status'] in {'waiting', 'running'}:
                return self.team_queue_status()
        if previous:
            name, title = previous['name'], previous.get('title')
        else:
            _, _, cards = team._fetch(self.home)
            card = next((c for c in cards if c['key'] == key), None)
            if card is None:
                raise ValueError('这个技能已经下架。')
            name = card['listings'][0]['name']
            title = next((r['title'] for r in card['listings'] if 'title' in r), None)
        with self.team_queue_lock:
            if self.closing:
                raise ValueError('看板正在关闭，请重新打开后再装上。')
            if any(item['key'] == key and item['status'] in {'waiting', 'running'} for item in self.team_queue):
                return self.team_queue_status()
            self.team_queue = [item for item in self.team_queue if item['key'] != key]
            self.team_queue.append({'id': secrets.token_urlsafe(16), 'key': key, 'name': name,
                **({'title': title} if title is not None else {}), 'status': 'waiting', 'message': '等待安装。',
                'installed': [], 'reuse': []})
            if self.team_queue_thread is None:
                self.team_queue_thread = threading.Thread(target=self.team_queue_work, daemon=True)
                self.team_queue_thread.start()
            return self.team_queue_status()

    def clear_team_queue(self):
        with self.team_queue_lock:
            self.team_queue = [item for item in self.team_queue if item['status'] in {'waiting', 'running'}]
            return self.team_queue_status()

    def team_queue_work(self):
        while True:
            with self.team_queue_lock:
                if self.closing:
                    self.team_queue_thread = None
                    return
                item = next((item for item in self.team_queue if item['status'] == 'waiting'), None)
                if item is None:
                    self.team_queue_thread = None
                    return
                item.update(status='running', message='正在装进技能库…')
            try:
                result = team.install(self.home, item['key'])
                self.invalidate_agents()
                with self.team_queue_lock:
                    item.update(status='done', message=result['message'], installed=result['installed'], reuse=result['reuse'])
                    if 'id' in result:
                        item['operation'] = result['id']
            except Exception as error:
                with self.team_queue_lock:
                    item.update(status='failed', message=str(error) if isinstance(error, ValueError) else
                                '安装没能完成，请稍后重试。')

    def start_program(self, action, target=None, automatic=False):
        with self.runtime_lock:
            if self.program_progress['running']:
                return self.program_progress.copy()
            if action == 'apply' and (not self.program_review or target != self.program_review['target'] or
                                      not self.program_review['available']):
                raise ValueError('请先检查 SP 更新，再升级。')
            self.program_progress.update(running=True, phase=action, error='', automatic=automatic,
                message='正在下载并检查新版…' if action == 'check' else '正在升级，等当前操作完成…')
            self.program_thread = threading.Thread(target=self.program_work, args=(action, automatic), daemon=True)
            self.program_thread.start()
            return self.program_progress.copy()

    def program_work(self, action, automatic=False):
        try:
            if action == 'check':
                if self.program_review:
                    rmtree(self.program_review['temporary'], ignore_errors=True)
                self.program_review = None
                self.program_review = self_update.check(self.home)
                review = self.program_review
                self.program_progress.update(phase='available' if review['available'] else 'latest',
                    target=review['target'], available=review['available'], current=review['current'], latest=review['latest'],
                    message='有新版，可以一键升级' if review['available'] else 'SP 已是最新版本')
            else:
                while not self.closing:
                    with self.runtime_lock:
                        idle = not self.active_requests and not (
                            any(t and t.is_alive() for t in (self.update_thread, self.labels_thread)) or
                            self.labels_progress['running'] or any(r['running'] for r in list(self.update_reviews.values())) or
                            any(item['status'] in {'waiting', 'running'} for item in self.team_queue_status()['items']))
                    if idle:
                        break
                    time.sleep(.05)
                if self.closing:
                    return
                self.program_progress['message'] = '正在安装 SP 新版…'
                result = self_update.apply(self.program_review, self.home)
                self.program_review['available'] = False
                self.program_progress.update(result, phase='ready', message='新版已装好，刷新后生效')
        except Exception as error:
            self_update.record_failure(self.home, action, error)
            if automatic:
                self.program_progress.update(phase='idle', available=False, error='', message='还没查到新版信息')
            else:
                self.program_progress.update(phase='error', available=False,
                    error=str(error) if isinstance(error, ValueError) else 'SP 更新没有完成，请检查网络和文件夹权限后重试。',
                    message='SP 更新没有完成')
        finally:
            self.program_progress['running'] = False

    def request_reload(self):
        changed = runtime.version() != self.code_version
        with self.runtime_lock:
            if not changed:
                self.reload_pending = False
                self.reload_error = ''
                if self.program_progress['phase'] == 'ready':
                    self.program_progress.update(phase='latest', message='SP 已升级到最新版')
            if changed and self.reload_callback and not self.reload_pending:
                self.reload_pending = True
                self.reload_error = ''
                self.reload_thread = threading.Thread(target=self.reload_when_idle, daemon=True)
                self.reload_thread.start()
            return {'supported': bool(self.reload_callback), 'changed': changed,
                    'version': self.code_version, 'busy': self.active_requests > 1 or self.background_busy()}

    def reload_when_idle(self):
        while not self.closing:
            with self.runtime_lock:
                if not self.reload_pending:
                    return
                if not self.active_requests and not self.background_busy():
                    try:
                        self.reload_callback()
                    except Exception as error:
                        self.reload_error = str(error) if isinstance(error, ValueError) else '新版没能应用，当前程序保持运行，请稍后刷新重试。'
                    finally:
                        self.reload_pending = False
                    return
            time.sleep(.05)

    def watch(self):
        while not self.closing and (time.monotonic() - self.last_heartbeat < (OPEN_PAGE_TIMEOUT if self.pages else HEARTBEAT_TIMEOUT) or self.background_busy() or self.reload_pending):
            time.sleep(0.1)
        if not self.closing:
            self.shutdown()

    def agents_view(self):
        with self.agents_lock:
            generation = self.agents_generation
            cached = self.agents_cache
            findings = self.agent_findings['candidates']
        if cached is not None and cached[0] == generation:
            result = cached[1]
        else:
            result = agent_management.overview(self.home, discovered=findings)
            with self.agents_lock:
                if self.agents_generation == generation:
                    self.agents_cache = (generation, result)
        with self.agents_lock:
            changed = self.agents_generation != generation
            progress = self.agents_progress.copy()
        return self.agents_view() if changed else {**result, 'discovery': progress}

    def invalidate_agents(self):
        with self.agents_lock:
            self.agents_generation += 1
            self.agents_cache = None

    def start_agents_scan(self, full=False):
        with self.agents_lock:
            if self.agents_progress['running']:
                return self.agents_progress.copy()
            self.agents_progress.update(running=True, error='',
                message='正在扫描 agent…' if full else '正在检查新安装的 agent…')
            self.agents_thread = threading.Thread(target=self.scan_agents, args=(full,), daemon=True)
            self.agents_thread.start()
            return self.agents_progress.copy()

    def scan_agents(self, full):
        try:
            with self.agents_lock:
                previous = self.agent_findings['candidates']
            findings = agent_discovery.find(self.home, full, previous)
            agent_discovery.save(self.home, findings)
            local = agent_management.profiles.state(self.home)
            unavailable = set(local['onboarded']) | set(local.get('ignored', []))
            current = {p['id'] for p in findings['candidates']}
            added = current - {p['id'] for p in previous} - unavailable
            pending = current - unavailable
            if added:
                message = f'发现 {len(added)} 个新 agent'
            elif pending:
                message = '没有更多新的 agent'
            else:
                message = '检查完成，没有发现新的 agent'
            with self.agents_lock:
                self.agent_findings = findings
                self.agents_generation += 1
                self.agents_cache = None
                self.agents_progress.update(checked_at=findings['checked_at'], message=message)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            with self.agents_lock:
                self.agents_progress.update(error=str(error) if isinstance(error, ValueError)
                    else 'agent 扫描没有完成，已保留上次名单，请重试。')
        finally:
            with self.agents_lock:
                self.agents_progress['running'] = False


class Handler(BaseHTTPRequestHandler):
    def parse_request(self):
        if not super().parse_request():
            return False
        # Unused browser connections must not delay applying a new program.
        with self.server.runtime_lock:
            self.server.active_requests += 1
            self.counted_request = True
        return True

    def handle_one_request(self):
        self.counted_request = False
        try:
            super().handle_one_request()
        finally:
            if self.counted_request:
                with self.server.runtime_lock:
                    self.server.active_requests -= 1

    def log_message(self, *args):
        # URLs contain a session secret; never put them in access logs.
        pass

    def authorized(self):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        token = self.headers.get("X-Skill-Pilot-Token") or query.get("token", [""])[0]
        return secrets.compare_digest(token, self.server.token)

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        # 浏览器会自己来要 /favicon.ico，不带本次口令。只放行这一个地址，避免控制台报错。
        if path == "/favicon.ico":
            self.reply(204, b"")
            return
        if not self.authorized():
            self.reply(403, {"error": "此页面只允许本机本次打开的浏览器访问。"})
            return
        if path == "/api/scan":
            with self.server.inventory_lock:
                if self.server.inventory.get('deferred'):
                    self.server.inventory = scan(self.server.home)
            self.reply(200, self.server.inventory)
        elif path == '/api/settings':
            self.reply(200, {**labels.public_settings(self.server.home),
                             'extra_sources': extra_sources.paths(self.server.home)})
        elif path == '/api/labels/progress':
            self.reply(200, self.server.labels_progress.copy())
        elif path == '/api/program/progress':
            self.reply(200, {**self.server.program_progress, 'write_allowed': preview_allowed(self.server.home)})
        elif path == '/api/config/export':
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.reply(200, sharing.export_config(self.server.home, query.get('agent', [None])[0]))
            except (ValueError, OSError, KeyError) as error:
                self.reply(409, {'error': str(error)})
        elif path == '/api/team/queue':
            self.reply(200, self.server.team_queue_status())
        elif path in {'/api/team', '/api/team/catalog', '/api/team/listings/candidates'}:
            try:
                result = (team.status(self.server.home, strict=False) if path == '/api/team' else
                          team.catalog(self.server.home) if path == '/api/team/catalog' else
                          team.listing_candidates(self.server.home))
                self.reply(200, result)
            except (ValueError, OSError) as error:
                self.reply(409, {'error': str(error) if isinstance(error, ValueError) else '没能读取团队分享，请稍后重试。'})
        elif path == '/api/updates/progress':
            with self.server.update_lock:
                review = next(reversed(self.server.update_reviews.values()), None)
                self.reply(200, updates.public(review) if review else None)
        elif path in ('/api/table', '/api/log', '/api/agents', '/api/cleanup'):
            try:
                result = (operations.table(self.server.home) if path == '/api/table' else
                          self.server.agents_view() if path == '/api/agents' else
                          cleanup.list_withdrawn(self.server.home) if path == '/api/cleanup' else operations.history(self.server.home))
                self.reply(200, result)
            except (ValueError, OSError, KeyError) as error:
                self.reply(409, {'error': str(error)})
        elif path == "/api/status":
            self.reply(200, {"pid": os.getpid(), "read_only": not preview_allowed(self.server.home),
                             'version': self.server.code_version, 'reloading': self.server.reload_pending,
                             'reload_error': self.server.reload_error})
        elif path in ("/api/migration/plan", "/api/backups", "/api/migration/compare"):
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                agents = query.get("agents", [None])[0]
                if path == "/api/backups":
                    self.reply(200, backups(self.server.home))
                    return
                plan = preview(self.server.home, agents.split(",") if agents is not None else None)
                if path == "/api/migration/plan":
                    self.reply(200, plan)
                    return
                if query.get("plan_id", [""])[0] != plan["plan_id"]:
                    raise ValueError("目录内容已经变了，请重新预览。")
                skill = next(s for s in plan["skills"] if s["name"] == query.get("name", [""])[0])
                baseline = next(o for o in skill["options"] if o["id"] == skill["default"])
                texts = []
                for option in skill["options"]:
                    if option == baseline:
                        continue
                    texts.append(compare_files(Path(baseline["path"]), Path(option["path"])))
                self.reply(200, {"comparison": "\n".join(texts)})
            except (ValueError, OSError, StopIteration) as error:
                self.reply(409, {"error": str(error) or "这个技能已不在预览里，请重新预览。"})
        elif path in ("/", "/app.js", "/app.css", "/favicon.svg",
                      "/vendor/gsap.min.js", "/vendor/ScrollTrigger.min.js"):
            file = STATIC / ("index.html" if path == "/" else path.removeprefix("/"))
            body = file.read_bytes().replace(b"__SESSION_TOKEN__", self.server.token.encode())
            kind = {"html": "text/html", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}[file.suffix.removeprefix(".")]
            self.reply(200, body, kind + "; charset=utf-8")
        else:
            self.reply(404, {"error": "没有这个页面。"})

    def do_POST(self):
        if not self.authorized() or self.headers.get("Origin") != self.server.origin:
            self.reply(403, {"error": "请求来源不属于本次看板。"})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in {'/api/heartbeat', '/api/runtime/refresh'}:
            # Consume small optional bodies before closing the HTTP/1.0
            # connection; unread request bytes can reset the reply on Windows.
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 1024 or len(self.rfile.read(length)) != length:
                    raise ValueError()
            except (ValueError, OSError):
                self.reply(400, {'error': '请求内容不正确，请刷新后重试。'})
                return
        if path == "/api/heartbeat":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            page = query.get("page", [""])[0]
            if page:
                (self.server.pages.discard if query.get("closing") else self.server.pages.add)(page)
            self.server.last_heartbeat = time.monotonic()
            self.reply(200, {"ok": True})
            return
        if path == '/api/runtime/refresh':
            try:
                self.reply(200, self.server.request_reload())
            except OSError:
                self.reply(409, {'error': '新版还没有准备完整，请稍后刷新重试。'})
            return
        if self.server.reload_pending:
            self.reply(503, {'error': '正在应用新版，请稍候再操作。'})
            return
        if self.server.program_progress['running'] and self.server.program_progress['phase'] == 'apply':
            self.reply(503, {'error': '正在升级 SP，请完成后刷新页面再操作。'})
            return
        read_routes = {'/api/assignment/preview', '/api/cleanup/preview', '/api/cleanup/delete/preview', '/api/install/inspect', '/api/incoming/compare',
                       '/api/updates/check', '/api/updates/explain', '/api/updates/merge', '/api/updates/calibrate',
                       '/api/agents/join/preview', '/api/agents/leave/preview', '/api/agents/ignore/preview', '/api/config/preview',
                       '/api/agents/scan', '/api/team/listings/preview'}
        write_routes = {'/api/assignment/apply', '/api/dependencies', '/api/install/apply', '/api/absorb', '/api/undo',
                        '/api/updates/apply', '/api/updates/watch', '/api/cleanup/apply', '/api/cleanup/delete/apply', '/api/withdrawn/restore',
                        '/api/agents/join/apply', '/api/agents/leave/apply', '/api/agents/ignore/apply', '/api/config/apply',
                        '/api/team/listings/apply', '/api/team/listings/remove', '/api/team/queue', '/api/team/queue/clear',
                        '/api/team/ratings', '/api/team/ratings/remove'}
        settings_routes = {'/api/settings/llm', '/api/settings/llm/test', '/api/labels/run',
                           '/api/settings/extra-sources', '/api/tags', '/api/team/settings'}
        program_routes = {'/api/program/check', '/api/program/apply'}
        if path.startswith(('/api/team/shares', '/api/team/upload/', '/api/team/import/')) or path == '/api/team/withdraw':
            self.reply(404, {'error': '没有这个团队商店操作。'})
            return
        if path not in {"/api/migration/apply", "/api/restore"} | read_routes | write_routes | settings_routes | program_routes:
            self.reply(405, {"error": "当前阶段没有这个操作。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise ValueError("请求内容不完整或太长，请重新打开核对页。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('请求内容格式不正确。')
            if path.startswith('/api/team/'):
                if path not in read_routes and not preview_allowed(self.server.home):
                    self.reply(403, {'error': '当前处于只读模式，分享和本机文件没有改动。'})
                    return
                self.reply(200, self.team_post(path, body))
                return
            if path == '/api/agents/scan':
                self.reply(200, self.server.start_agents_scan(full=True))
                return
            if path in program_routes:
                if path.endswith('apply'):
                    if body.get('confirmed') is not True:
                        self.reply(400, {'error': '请点击“一键升级”确认这次升级。'})
                        return
                    if not preview_allowed(self.server.home):
                        self.reply(403, {'error': '当前处于只读模式，程序没有改动。'})
                        return
                result = self.server.start_program(path.rsplit('/', 1)[1], body.get('target'))
                self.reply(200, result)
                return
            if path in settings_routes:
                if not preview_allowed(self.server.home):
                    self.reply(403, {'error': '当前处于只读模式，文件没有改动。'})
                    return
                if path == '/api/settings/llm':
                    saved = labels.save_settings(self.server.home, body)
                    self.server.invalidate_agents()
                    self.reply(200, saved)
                elif path == '/api/settings/llm/test':
                    self.reply(200, labels.test(self.server.home))
                elif path == '/api/tags':
                    self.reply(200, tags.update(self.server.home, body))
                elif path == '/api/settings/extra-sources':
                    folder = body.get('path', '')
                    if not isinstance(folder, str):
                        raise ValueError('请填写一个文件夹。')
                    message = (extra_sources.remove(self.server.home, folder) if body.get('remove') is True
                               else extra_sources.add(self.server.home, folder))
                    self.server.inventory = scan(self.server.home)
                    self.reply(200, {'message': message, 'extra_sources': extra_sources.paths(self.server.home)})
                else:
                    mode = body.get('mode')
                    if mode not in ('missing', 'all'):
                        raise ValueError('请选择补齐中文名或重新整理全部。')
                    with self.server.labels_lock:
                        if self.server.labels_progress['running']:
                            raise ValueError('正在给技能起中文名，请等这次完成。')
                        if not labels.settings(self.server.home).get('api_key'):
                            raise ValueError('请先到设置里填好 AI 服务的 key。')
                        self.server.labels_progress.update(running=True, done=0, total=0, message='正在准备技能名单…', error='')
                        def run_labels():
                            try:
                                labels.run(self.server.home, mode, self.server.labels_progress)
                            finally:
                                self.server.invalidate_agents()
                        self.server.labels_thread = threading.Thread(target=run_labels, daemon=True)
                        self.server.invalidate_agents()
                        self.server.labels_thread.start()
                        self.reply(200, self.server.labels_progress.copy())
                return
            if path == '/api/updates/check':
                with self.server.update_lock:
                    review = next(reversed(self.server.update_reviews.values()), None)
                    if review is None or not review['running']:
                        # A long-lived dashboard would otherwise keep every earlier check's downloads.
                        for old in self.server.update_reviews.values():
                            rmtree(old['temporary'], ignore_errors=True)
                        self.server.update_reviews.clear()
                        review = updates.create(self.server.home)
                        self.server.update_reviews[review['id']] = review
                        self.server.update_thread = threading.Thread(target=updates.check, kwargs={'review': review, 'lock': self.server.update_lock}, daemon=True)
                        self.server.update_thread.start()
                    self.reply(200, updates.public(review))
                return
            if path in {'/api/updates/explain', '/api/updates/merge', '/api/updates/calibrate'}:
                with self.server.update_lock:
                    review = self.server.update_reviews[body['review_id']]
                action = updates.explain if path.endswith('explain') else updates.calibrate if path.endswith('calibrate') else updates.prepare_merge
                result = action(review, body['name'])
                self.reply(200, result)
                return
            if path == '/api/assignment/preview':
                self.reply(200, operations.preview_assign(body['changes'], self.server.home, body.get('recheck') is True))
                return
            if path == '/api/config/preview':
                self.reply(200, sharing.preview_import(body.get('config'), body.get('agents'), self.server.home))
                return
            if path == '/api/cleanup/preview':
                self.reply(200, cleanup.preview(body.get('skills'), self.server.home))
                return
            if path == '/api/cleanup/delete/preview':
                self.reply(200, cleanup.delete_preview(body.get('skills'), self.server.home))
                return
            if path == '/api/agents/join/preview':
                self.reply(200, agent_management.preview_join(body['agents'], self.server.home))
                return
            if path in {'/api/agents/leave/preview', '/api/agents/ignore/preview'}:
                action = agent_management.preview_leave if path.endswith('leave/preview') else agent_management.preview_ignore
                self.reply(200, agent_management.public(action(body['agent'], self.server.home)))
                return
            if path == '/api/install/inspect':
                review = github.inspect(body['url'])
                self.server.install_reviews[review['id']] = review
                registry = operations.load(self.server.home)[1]
                self.reply(200, {**review, 'conflicts': operations.conflicts(review['skills'], registry, self.server.home)})
                return
            if path == '/api/incoming/compare':
                existing = self.server.home / '.skill-pilot/store' / body['name']
                if body.get('review_id'):
                    review = self.server.install_reviews[body['review_id']]
                    incoming = Path(next(s['path'] for s in review['skills'] if s['name'] == body['name']))
                else:
                    home, registry, agents = operations.load(self.server.home)
                    incoming = Path(next(s['path'] for s in operations.new_skills(home, agents)
                                         if s['relative'] == body['relative'] and s['name'] == body['name']))
                github.safe_skill(incoming)
                self.reply(200, {'comparison': compare_files(existing, incoming)})
                return
            if body.get("confirmed") is not True:
                if path == '/api/cleanup/apply':
                    try:
                        plan = cleanup.preview(body.get('skills'), self.server.home)
                    except ValueError as error:
                        self.reply(400, {'error': str(error)})
                        return
                    if plan['dependency_warnings'] or plan['path_warnings']:
                        self.reply(200, {'needs_confirmation': True, **plan})
                        return
                self.reply(400, {"error": "请先核对，并确认这次操作。"})
                return
            if not preview_allowed(self.server.home):
                self.reply(403, {"error": "当前处于只读模式，文件没有改动。"})
                return
            if path in {'/api/cleanup/apply', '/api/cleanup/delete/apply'}:
                try:
                    action = cleanup.delete_preview if path == '/api/cleanup/delete/apply' else cleanup.preview
                    action(body.get('skills'), self.server.home)
                except ValueError as error:
                    self.reply(400, {'error': str(error)})
                    return
            if path in {'/api/agents/join/apply', '/api/agents/leave/apply', '/api/agents/ignore/apply', '/api/cleanup/apply', '/api/cleanup/delete/apply', '/api/config/apply'} and (not isinstance(body.get('revision'), str) or not body['revision']):
                raise ValueError('请先读取当前状态，再确认这次操作。')
            if path == "/api/migration/apply":
                result = apply(body["plan_id"], self.server.home, body["agents"], body.get("choices"), body.get("sources"))
                operations.bootstrap(self.server.home)
            elif path == '/api/restore':
                result = restore(body["backup_id"], self.server.home)
            elif path == '/api/assignment/apply':
                result = operations.apply_assign(body['changes'], body['revision'], True, self.server.home,
                                                 body.get('recheck') is True)
            elif path == '/api/config/apply':
                result = sharing.import_config(body.get('config'), body.get('agents'), self.server.home, body['revision'])
            elif path == '/api/agents/join/apply':
                result = agent_management.join(body['agents'], body['plan_id'], body['revision'], body.get('choices'), self.server.home)
            elif path == '/api/agents/leave/apply':
                result = agent_management.leave(body['agent'], body['plan_id'], body['revision'], self.server.home)
            elif path == '/api/agents/ignore/apply':
                result = agent_management.ignore(body['agent'], body['plan_id'], body['revision'], self.server.home)
            elif path == '/api/dependencies':
                result = operations.decide_dependencies(body['decisions'], body['revision'], self.server.home)
            elif path == '/api/install/apply':
                result = operations.install(self.server.install_reviews[body['review_id']], body['skills'],
                                            body.get('agents'), body['revision'], self.server.home)
            elif path == '/api/absorb':
                result = operations.absorb(body.get('all', True), body.get('selected'), body['revision'], self.server.home)
            elif path == '/api/updates/watch':
                result = updates.set_watch(body.get('name'), body.get('mode'), body.get('revision'), self.server.home, True)
            elif path == '/api/updates/apply':
                with self.server.update_lock:
                    review = self.server.update_reviews[body['review_id']]
                    result = updates.apply(review, body['skills'], body.get('choices'), True,
                                           body.get('merge_ids'), body.get('confirm_lark_cli', False))
            elif path == '/api/cleanup/apply':
                result = cleanup.apply(body.get('skills'), True, self.server.home, body['revision'])
            elif path == '/api/cleanup/delete/apply':
                result = cleanup.delete(body.get('skills'), True, self.server.home, body['revision'])
            elif path == '/api/withdrawn/restore':
                result = cleanup.restore(body['skills'],self.server.home,body['revision'])
            else:
                result = operations.undo(body['id'], body['revision'], self.server.home)
                record = next((item for item in operations.history(self.server.home) if item['id'] == body['id']), None)
                if record:
                    for review in self.server.update_reviews.values():
                        updates.restore_arrows(review, record.get('skills'), body['id'])
            self.server.invalidate_agents()
            self.server.inventory = scan(self.server.home)
            self.reply(200, result)
        except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
            message = ('团队操作没能完成，请重新预览后重试。' if path.startswith('/api/team/') and not isinstance(error, ValueError) else
                       '设置没能保存，请核对后再试。' if path in settings_routes and not isinstance(error, ValueError) else str(error))
            self.reply(400 if path in {'/api/cleanup/preview', '/api/cleanup/delete/preview'} else 409, {
                "error": message or "操作没能完成，请重新预览。",
                **({'recheck': True} if isinstance(error, operations.AssignmentChanged) else {})})

    def team_post(self, path, body):
        home = self.server.home
        if path == '/api/team/settings':
            return team.save_settings(home, body)
        if path == '/api/team/listings/preview':
            saved, result = team.listings_preview(home, body.get('skills'))
            review = secrets.token_urlsafe(16)
            self.server.team_upload_reviews[review] = saved
            return {**result, 'review': review}
        if path == '/api/team/queue':
            return self.server.queue_team(body.get('key'))
        if path == '/api/team/queue/clear':
            return self.server.clear_team_queue()
        if path == '/api/team/listings/remove':
            return team.listings_remove(home, body.get('names'))
        if path in {'/api/team/ratings', '/api/team/ratings/remove'}:
            return team.rate(home, body, path.endswith('/remove'))
        reviews = self.server.team_upload_reviews
        identifier = body.get('review')
        if not isinstance(identifier, str) or identifier not in reviews:
            raise ValueError('这份预览已失效，请重新预览。')
        review = reviews[identifier]
        result = team.listings_apply(home, review)
        reviews.pop(identifier, None)
        self.server.invalidate_agents()
        self.server.inventory = scan(home)
        return result


def preview_allowed(home):
    try:
        guard(home / ".skill-pilot", home)
        return True
    except ValueError:
        return False


def compare_files(left, right):
    def files(root):
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()
                and not any(part in SKIP_CONTENT for part in p.relative_to(root).parts)}
    one, two = files(left), files(right)
    result = [f"默认选择：{left}\n另一份：{right}\n"]
    for name in sorted(one.keys() | two.keys()):
        a = one[name].read_bytes() if name in one else b""
        b = two[name].read_bytes() if name in two else b""
        if a == b:
            continue
        result.append(f"文件：{name}" + ("（脚本或可执行文件）" if Path(name).suffix in {".py", ".js", ".sh"} else ""))
        try:
            result.extend(difflib.unified_diff(a.decode().splitlines(), b.decode().splitlines(), fromfile="默认选择", tofile="另一份", lineterm=""))
        except UnicodeError:
            result.append("此文件不是文本，内容不同。")
    return "\n".join(result)


def lock_path():
    identity = hashlib.sha256(str(target_home()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"skill-pilot-{user_id()}-{identity}.lock"


def open_existing(lock):
    # A simultaneous launcher may hold the lock just before writing its URL.
    for _ in range(50):
        lock.seek(0)
        try:
            state = json.loads(lock.read())
            url = urllib.parse.urlsplit(state["url"])
            if url.scheme != "http" or url.hostname != "127.0.0.1":
                return False
            request = urllib.request.Request(f"http://127.0.0.1:{url.port}/api/status?{url.query}")
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if json.load(response)["pid"] != state["pid"]:
                    return False
            webbrowser.open(state["url"])
            return True
        except (ValueError, KeyError, OSError, urllib.error.URLError):
            time.sleep(0.1)
    return False


def run(json_output=False):
    if os.name == 'nt':
        from .windows_runtime import run as windows_run
        return windows_run(json_output)
    resume = runtime.resume_state()
    fd = resume['lock_fd'] if resume else os.open(lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as lock:
        if os.name != "nt":
            os.fchmod(lock.fileno(), 0o600)
        if not resume:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if open_existing(lock):
                    print(json.dumps({"message": "已打开正在运行的看板。"}, ensure_ascii=False) if json_output else "已打开正在运行的看板。")
                    return
                raise SystemExit("已有看板正在启动或退出，请稍后再打开。")
            operations.bootstrap(target_home())
        server = Dashboard(resume['inventory'] if resume else {'home': str(target_home()), 'deferred': True}, resume)
        server.reload_callback = lambda: runtime.replace(server, lock)
        server.start_agents_scan()
        if not resume:
            lock.seek(0)
            lock.truncate()
            json.dump({"pid": os.getpid(), "url": server.url}, lock)
            lock.flush()
            if preview_allowed(server.home):
                server.start_program('check', automatic=True)
        server.last_heartbeat = time.monotonic()
        threading.Thread(target=server.watch, daemon=True).start()
        if not resume:
            webbrowser.open(server.url)
            print(json.dumps({"message": "看板已打开；程序更新后刷新页面即可应用。", "pid": os.getpid(), "url": server.url}, ensure_ascii=False) if json_output else "看板已打开；程序更新后刷新页面即可应用。", flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            import shutil
            for review in server.install_reviews.values():
                rmtree(review['temporary'], ignore_errors=True)
            for review in server.update_reviews.values():
                rmtree(review['temporary'], ignore_errors=True)
            lock.seek(0)
            lock.truncate()
            # Do not unlink: another launcher could be holding the same inode.
