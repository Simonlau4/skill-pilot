"""A standalone, LAN-only service for portable skill lists."""
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import sharing

MAX_BODY = 1024 * 1024
SKILL_FIELDS = ('name', 'assigned_agents', 'requires', 'local_modified',
                'title', 'summary', 'group', 'description')


def now():
    return datetime.now(timezone.utc).isoformat()


def member_name(name):
    if isinstance(name, str):
        name = name.strip()
        if 1 <= len(name) <= 40 and not any(ord(char) < 32 or ord(char) == 127 for char in name):
            return name
    raise ValueError('请填写名字，不超过 40 个字，不能换行或含控制字符。')


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clean_config(data):
    data = sharing.validate(data)
    agents = data.get('agents', [])
    if (not isinstance(agents, list) or any(not isinstance(agent, dict) or
            any(not isinstance(agent.get(key), str) or not agent[key].strip()
                for key in ('id', 'name')) for agent in agents)):
        raise ValueError('配置里的 agent 名单格式不正确，请重新导出。')
    result = {key: data[key] for key in ('format', 'version', 'created_at') if key in data}
    result['agents'] = [{key: agent[key] for key in ('id', 'name')} for agent in agents]
    result['skills'] = []
    for skill in data['skills']:
        row = {key: skill[key] for key in SKILL_FIELDS if key in skill}
        source = skill['source']
        fields = (('kind', 'repository', 'path', 'commit') if source['kind'] == 'github' else
                  ('kind', 'url', 'reason'))
        row['source'] = {key: source[key] for key in fields if key in source}
        result['skills'].append(row)
    return result


class HubError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class HubStore:
    def __init__(self, data):
        self.data = Path(data).expanduser().resolve()

    @contextmanager
    def locked(self):
        # CLI commands and HTTP threads use the same lock, including after restart.
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.data / '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _members(self):
        path = self.data / 'members.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))['members'] if path.exists() else []
        except (ValueError, KeyError, TypeError):
            raise HubError(500, '成员数据没能读懂，请联系管理员检查或还原备份。') from None

    def add_member(self, name):
        name = member_name(name)
        token = secrets.token_urlsafe(32)
        member = {'id': secrets.token_hex(16), 'name': name, 'created_at': now(),
                  'token_sha256': hashlib.sha256(token.encode()).hexdigest()}
        with self.locked():
            atomic_write(self.data / 'members.json', {'members': [*self._members(), member]})
        return {key: member[key] for key in ('id', 'name', 'created_at')} | {'token': token}

    def members(self):
        with self.locked():
            return [{key: member[key] for key in ('id', 'name', 'created_at')}
                    for member in self._members()]

    def remove_member(self, member_id):
        with self.locked():
            members = self._members()
            if not any(member['id'] == member_id for member in members):
                raise ValueError('没有找到这位成员，请先查看成员编号。')
            atomic_write(self.data / 'members.json',
                         {'members': [member for member in members if member['id'] != member_id]})
            self._share_path(member_id).unlink(missing_ok=True)

    # The HTTP handler holds locked() while authenticating and accessing shares.
    def _authenticate(self, authorization):
        scheme, _, token = authorization.partition(' ')
        if scheme.lower() == 'bearer' and token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            for member in self._members():
                if hmac.compare_digest(digest, member['token_sha256']):
                    return {'id': member['id'], 'name': member['name']}
        raise HubError(401, '口令缺失或不正确，请向管理员核对。')

    def _share_path(self, member_id):
        return self.data / 'shares' / (member_id + '.json')

    def _get_share(self, member_id):
        member = next((member for member in self._members() if member['id'] == member_id), None)
        path = self._share_path(member_id)
        if member is None or not path.is_file():
            raise HubError(404, '这位成员还没有分享，或已经撤回。')
        try:
            return {'member': {key: member[key] for key in ('id', 'name')},
                    **json.loads(path.read_text(encoding='utf-8'))}
        except (ValueError, TypeError):
            raise HubError(500, '分享数据没能读懂，请联系管理员检查或还原备份。') from None

    def _shares(self):
        rows = []
        for member in self._members():
            if self._share_path(member['id']).is_file():
                share = self._get_share(member['id'])
                rows.append({'member': share['member'], 'updated_at': share['updated_at'],
                             'skill_count': len(share['config']['skills']), 'agents': share['config']['agents']})
        return {'shares': sorted(rows, key=lambda row: row['updated_at'], reverse=True)}

    def _put_share(self, member_id, config):
        updated_at = now()
        atomic_write(self._share_path(member_id), {'updated_at': updated_at, 'config': config})
        return {'updated_at': updated_at, 'skill_count': len(config['skills'])}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # Never log headers, request paths, tokens or uploaded lists.

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self._json(405 if code == 501 else code, {'error': '请求格式或操作不支持，请核对分享站地址和操作。'})

    def parse_request(self):
        if not super().parse_request():
            return False
        # The base parser collapses leading slashes in self.path; use the original target.
        if self.command == 'POST' and self.requestline.split()[1] == '/api/join':
            return True
        try:
            with self.server.store.locked():
                self.server.store._authenticate(self.headers.get('Authorization', ''))
        except HubError as error:
            self._json(error.status, {'error': str(error)})
            return False
        except (OSError, ValueError, KeyError, TypeError):
            self._json(500, {'error': '分享站没能读取数据，请联系管理员检查数据文件夹。'})
            return False
        return True

    def _read_json(self, limit=MAX_BODY):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) > 1:
            raise HubError(400, '上传方式不支持，请直接发送完整的请求内容。')
        if not lengths:
            raise HubError(411, '请带上请求内容的长度再发送。')
        try:
            length = int(lengths[0])
        except ValueError:
            raise HubError(400, '请求内容的长度不正确。') from None
        if length < 0:
            raise HubError(400, '请求内容的长度不正确。')
        if length > limit:
            raise HubError(413, '配置文件超过 1 MB，请缩小清单后再上传。' if limit == MAX_BODY else
                           '加入信息超过 4096 字节，请缩短后重试。')
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            raise HubError(408, '请求等待太久，请重新发送。') from None
        if len(body) != length:
            raise HubError(400, '请求内容没有传完整，请重新发送。')
        def invalid_constant(value):
            raise ValueError
        try:
            return json.loads(body.decode('utf-8'), parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise HubError(400, '请求内容没能读懂，请重新发送。') from None

    def _read_config(self):
        data = self._read_json()
        try:
            return clean_config(data)
        except ValueError as error:
            raise HubError(400, str(error)) from None

    def _handle(self):
        try:
            share = re.fullmatch(r'/api/shares/([0-9a-f]{32})', self.path)
            allowed = ({'POST'} if self.path == '/api/join' else
                       {'GET'} if self.path in {'/api/me', '/api/shares'} or share else
                       {'PUT', 'DELETE'} if self.path == '/api/shares/me' else set())
            if not allowed:
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            if self.command not in allowed:
                raise HubError(405, '这个地址不支持这项操作。')
            if self.path == '/api/join':
                if not self.server.open:
                    raise HubError(403, '这个分享站需要管理员发的口令。')
                data = self._read_json(4096)
                if not isinstance(data, dict):
                    raise HubError(400, '加入信息格式不正确，请填写名字后重试。')
                try:
                    name = member_name(data.get('name'))
                except ValueError as error:
                    raise HubError(400, str(error)) from None
                # add_member takes the data lock itself.
                member = self.server.store.add_member(name)
                self._json(200, {'service': 'skill-pilot-team', 'api': 1,
                                 'member': {key: member[key] for key in ('id', 'name')},
                                 'token': member['token']})
                return
            config = self._read_config() if self.command == 'PUT' else None
            store = self.server.store
            with store.locked():
                # A member may have been removed while the upload body was arriving.
                member = store._authenticate(self.headers.get('Authorization', ''))
                if self.path == '/api/me':
                    result = {'service': 'skill-pilot-team', 'api': 1, 'member': member}
                elif self.path == '/api/shares':
                    result = store._shares()
                elif share:
                    result = store._get_share(share[1])
                elif self.command == 'PUT':
                    result = store._put_share(member['id'], config)
                else:
                    store._share_path(member['id']).unlink(missing_ok=True)
                    result = {}
            self._json(200, result)
        except HubError as error:
            self._json(error.status, {'error': str(error)})
        except (ValueError, KeyError, TypeError):
            self._json(500, {'error': '分享站数据格式不正确，请联系管理员检查或还原备份。'})
        except OSError:
            self._json(500, {'error': '分享站没能读写数据，请联系管理员检查数据文件夹。'})

    do_GET = do_PUT = do_DELETE = do_POST = do_PATCH = do_HEAD = do_OPTIONS = _handle


class HubServer(ThreadingHTTPServer):
    def __init__(self, data, host='0.0.0.0', port=8765, open=False):
        if not 0 <= port <= 65535:
            raise ValueError('端口应为 0 到 65535 之间的数字。')
        self.open = open
        self.store = HubStore(data)
        with self.store.locked():
            self.store._members()
        super().__init__((host, port), Handler)


def run(args):
    if args.hub_command == 'serve':
        with HubServer(args.data, args.host, args.port, open=args.open) as server:
            access = ('开放加入，知道地址的同事填名字就能加入。' if args.open else
                      '只有管理员发了口令的成员能用。')
            print(f'分享站已启动，监听 {args.host}:{server.server_port}；{access}按 Ctrl+C 停止。', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print('分享站已停止。')
        return
    store = HubStore(args.data)
    if args.hub_command == 'add-member':
        member = store.add_member(args.name)
        print('成员编号：' + member['id'])
        print('显示名：' + member['name'])
        print('口令（只显示这一次）：' + member['token'])
        print('请单独交给这位同事，之后无法再次查看。')
    elif args.hub_command == 'members':
        members = store.members()
        print('成员编号\t显示名\t创建时间' if members else '还没有成员，请先添加。')
        for member in members:
            print('\t'.join(member[key] for key in ('id', 'name', 'created_at')))
    else:
        store.remove_member(args.member_id)
        print('已删除这位成员和他的分享。')
