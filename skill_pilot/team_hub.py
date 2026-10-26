"""Authenticated LAN skill store. Uploaded files are data, never executed."""
import base64
import binascii
from .platform_support import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import sharing

MAX_BODY = 20 * 1024 * 1024
MAX_RATING_BODY = 64 * 1024
MAX_INSTALL_BODY = 64 * 1024
MAX_JOIN_BODY = 4096
MAX_FILES = 300
MAX_SIZE = 5 * 1024 * 1024
TEXT_FIELDS = ('title', 'summary', 'group', 'description', 'note')
NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]*'


def now():
    return datetime.now(timezone.utc).isoformat()


def member_name(name):
    if isinstance(name, str) and not any(unicodedata.category(char) == 'Cc' for char in name):
        name = name.strip()
        if 1 <= len(name) <= 40:
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


def short_text(value, label):
    if (not isinstance(value, str) or len(value) > 200 or
            any(unicodedata.category(c) in {'Cc', 'Cf', 'Zl', 'Zp'} for c in value)):
        raise ValueError(f'{label}最多 200 字，不能含换行或看不见的字符。')
    return value


def clean_files(files):
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ValueError('每个技能须有文件，且最多 300 个文件。')
    result, seen, size = [], set(), 0
    for row in files:
        if not isinstance(row, dict):
            raise ValueError('技能文件格式不正确。')
        path, content, executable = (row.get(k) for k in ('path', 'content', 'executable'))
        if (not isinstance(path, str) or not path or '\\' in path or path.startswith('/') or
                any(p in {'', '.', '..'} for p in path.split('/')) or path in seen or
                any(unicodedata.category(c) in {'Cc', 'Cf', 'Zl', 'Zp'} for c in path)):
            raise ValueError('技能文件的位置不安全或重复，请使用文件夹内的相对位置。')
        if not isinstance(content, str) or type(executable) is not bool:
            raise ValueError('技能文件内容或执行权限格式不正确。')
        try:
            raw = base64.b64decode(content, validate=True)
            if base64.b64encode(raw).decode('ascii') != content:
                raise ValueError
        except (ValueError, binascii.Error):
            raise ValueError('技能文件内容编码不正确，请重新上架。') from None
        size += len(raw)
        if size > MAX_SIZE:
            raise ValueError('单个技能的文件合计不能超过 5 MB。')
        seen.add(path)
        result.append({'path': path, 'content': content, 'executable': executable})
    if 'SKILL.md' not in seen:
        raise ValueError('技能根目录必须有 SKILL.md。')
    if any('/'.join(path.split('/')[:i]) in seen for path in seen for i in range(1, len(path.split('/')))):
        raise ValueError('技能文件和文件夹的位置相互冲突。')
    return result


def package_version(files):
    rows = [[row['path'], row['executable'], hashlib.sha256(base64.b64decode(row['content'])).hexdigest()]
            for row in files]
    encoded = json.dumps(sorted(rows, key=lambda r: r[0]), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def clean_listing(row, packaged=False):
    if not isinstance(row, dict) or not isinstance(row.get('source'), dict):
        raise ValueError('上架的技能或来源格式不正确。')
    source = row['source']
    kind = source.get('kind')
    if kind not in {'github', 'lark', 'files'}:
        raise ValueError('上架来源只支持 GitHub、飞书官方或技能文件。')
    # Reuse the portable list's name, dependency, text and GitHub-source rules.
    checked = {k: row[k] for k in ('name', 'requires', *TEXT_FIELDS[:-1]) if k in row}
    checked.update(assigned_agents=[], local_modified=False,
                   source=source if kind == 'github' else {'kind': 'manual'})
    requires = row.get('requires', [])
    placeholders = ([{'name': n, 'requires': [], 'assigned_agents': [], 'local_modified': False,
                      'source': {'kind': 'manual'}} for n in requires if n != row.get('name')]
                    if isinstance(requires, list) else [])
    sharing.validate({'format': sharing.FORMAT, 'version': 1, 'skills': [checked, *placeholders]})
    result = {k: row[k] for k in ('name', 'requires', *TEXT_FIELDS[:-1]) if k in row}
    if 'note' in row:
        result['note'] = short_text(row['note'], '推荐语')
    if kind == 'github':
        result['source'] = {k: source[k] for k in ('kind', 'repository', 'path', 'commit') if k in source}
        result['source']['path'] = Path(source['path']).as_posix()
        if 'tree_sha' in source:
            if not isinstance(source['tree_sha'], str) or not re.fullmatch(r'[0-9a-f]{40}', source['tree_sha']):
                raise ValueError('技能来源的版本记录不正确。')
            result['source']['tree_sha'] = source['tree_sha']
    elif kind == 'lark':
        result['source'] = {'kind': 'lark'}
    else:
        result['source'] = {'kind': 'files'}
    if kind == 'files' or (kind == 'github' and
            (any(k in source for k in ('version', 'file_count', 'size')) if packaged else 'files' in source)):
        if packaged:
            if (not isinstance(source.get('version'), str) or not re.fullmatch(r'[0-9a-f]{64}', source['version']) or
                    type(source.get('file_count')) is not int or not 1 <= source['file_count'] <= MAX_FILES or
                    type(source.get('size')) is not int or not 0 <= source['size'] <= MAX_SIZE):
                raise ValueError('技能文件的版本或大小记录不正确。')
            result['source'].update({k: source[k] for k in ('version', 'file_count', 'size')})
        else:
            result['source']['files'] = clean_files(source.get('files'))
    return result


def listing_key(member, row):
    source = row['source']
    if source['kind'] == 'github':
        return 'github:' + source['repository'].lower() + ':' + source['path']
    return 'lark:' + row['name'] if source['kind'] == 'lark' else 'team:' + member + ':' + row['name']


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
            ratings = self._ratings()
            for key in list(ratings):
                ratings[key].pop(member_id, None)
                if key.startswith('team:' + member_id + ':') or not ratings[key]:
                    del ratings[key]
            installs = self._installs()
            installs.pop(member_id, None)
            changes = {p: None for p in (self.data / 'packages' / member_id).glob('*.json')}
            changes.update({self._listing_path(member_id): None,
                            self.data / 'ratings.json': {'ratings': ratings},
                            self.data / 'installs.json': {'installs': installs},
                            self.data / 'members.json': {'members': [m for m in members if m['id'] != member_id]}})
            self._save(changes)
            shutil.rmtree(self.data / 'packages' / member_id, ignore_errors=True)

    # The HTTP handler holds locked() while authenticating and accessing data.
    def _authenticate(self, authorization):
        scheme, _, token = authorization.partition(' ')
        if scheme.lower() == 'bearer' and token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            for member in self._members():
                if hmac.compare_digest(digest, member['token_sha256']):
                    return {'id': member['id'], 'name': member['name']}
        raise HubError(401, '口令缺失或不正确，请向管理员核对。')

    def _listing_path(self, member_id):
        return self.data / 'listings' / (member_id + '.json')

    def _read(self, path, field):
        return json.loads(path.read_text(encoding='utf-8'))[field] if path.exists() else {}

    def _listings(self, member_id):
        return self._read(self._listing_path(member_id), 'listings')

    def _ratings(self):
        return self._read(self.data / 'ratings.json', 'ratings')

    def _installs(self):
        return self._read(self.data / 'installs.json', 'installs')

    def _save(self, changes):
        # Readers hold the same lock. Roll back completed writes if any later write fails.
        previous = {p: json.loads(p.read_text(encoding='utf-8')) if p.exists() else None for p in changes}
        done = []
        try:
            for path, value in changes.items():
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, value)
                done.append(path)
        except OSError:
            for path in reversed(done):
                if previous[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, previous[path])
            raise

    def _catalog(self, member_id=None):
        cards = {}
        members = {m['id']: {k: m[k] for k in ('id', 'name')} for m in self._members()}
        installs = self._installs()
        for author_id, member in members.items():
            for row in self._listings(author_id).values():
                key = listing_key(author_id, row)
                cards.setdefault(key, {'key': key, 'listings': [], 'ratings': []})['listings'].append(
                    {**row, 'member': member})
        for key, ratings in self._ratings().items():
            if key in cards:
                cards[key]['ratings'] = [{**r, 'member': members[m]} for m, r in ratings.items() if m in members]
        for card in cards.values():
            card['listings'].sort(key=lambda r: r['updated_at'], reverse=True)
            card['ratings'].sort(key=lambda r: r['updated_at'], reverse=True)
            authors = {row['member']['id'] for row in card['listings']}
            card['installs'] = sum(card['key'] in keys for member, keys in installs.items()
                                   if member in members and member not in authors)
        return {'cards': sorted(cards.values(), key=lambda c: c['listings'][0]['updated_at'], reverse=True),
                'my_installs': sorted(set(installs.get(member_id, [])) & cards.keys())}

    def _put_installs(self, member_id, data):
        if (not isinstance(data, dict) or not isinstance(data.get('keys'), list) or
                any(not isinstance(key, str) for key in data['keys'])):
            raise HubError(400, '上报内容须为技能卡片编号的文字列表。')
        allowed = {card['key'] for card in self._catalog()['cards']
                   if all(row['member']['id'] != member_id for row in card['listings'])}
        keys = sorted(set(data['keys']) & allowed)
        installs = self._installs()
        if keys:
            installs[member_id] = keys
        else:
            installs.pop(member_id, None)
        atomic_write(self.data / 'installs.json', {'installs': installs})
        return {'keys': keys}

    def _put_listings(self, member_id, data):
        if not isinstance(data, dict) or not isinstance(data.get('listings'), list) or not data['listings']:
            raise HubError(400, '请至少选择一个要上架的技能。')
        try:
            rows = [clean_listing(row) for row in data['listings']]
        except ValueError as error:
            raise HubError(400, str(error)) from None
        current = self._listings(member_id)
        names = [r['name'] for r in rows]
        if len(set(names)) != len(names):
            raise HubError(400, '一次上架的名单里不能有同名技能。')
        for row in rows:
            missing = set(row['requires']) - (set(current) | set(names))
            if missing:
                raise HubError(400, row['name'] + ' 缺少本人上架的所需技能：' + '、'.join(sorted(missing)))
        changes, receipts = {}, []
        updated_at = now()
        for row in rows:
            name, source = row['name'], row['source']
            path = self.data / 'packages' / member_id / (name + '.json')
            receipt = {'name': name, 'key': listing_key(member_id, row), 'updated_at': updated_at}
            if 'files' in source:
                files = source.pop('files')
                version = package_version(files)
                changes[path] = {'name': name, 'version': version, 'files': files}
                source.update(version=version, file_count=len(files),
                              size=sum(len(base64.b64decode(f['content'])) for f in files))
                receipt['version'] = version
            else:
                changes[path] = None
            current[name] = {**row, 'updated_at': updated_at}
            receipts.append(receipt)
        changes[self._listing_path(member_id)] = {'listings': current}
        self._save(changes)
        return {'listings': receipts}

    def _remove_listing(self, member_id, name):
        current = self._listings(member_id)
        if name not in current:
            raise HubError(404, '这个技能还没有上架，或已经下架。')
        parents = [n for n, row in current.items() if n != name and name in row['requires']]
        if parents:
            raise HubError(409, '这些上架的技能仍需要它：' + '、'.join(sorted(parents)))
        del current[name]
        self._save({self.data / 'packages' / member_id / (name + '.json'): None,
                    self._listing_path(member_id): {'listings': current}})
        return {}

    def _package(self, member_id, name, version=None):
        row = self._listings(member_id).get(name)
        if row is None or row['source']['kind'] not in {'files', 'github'} or 'version' not in row['source']:
            raise HubError(404, '这个技能没有可取的文件，或已经下架。')
        if version is not None and version != row['source']['version']:
            raise HubError(409, '作者已经上架了新版，请重新预览。')
        return json.loads((self.data / 'packages' / member_id / (name + '.json')).read_text(encoding='utf-8'))

    def _rate(self, member, data, remove=False):
        if not isinstance(data, dict) or not isinstance(data.get('key'), str):
            raise HubError(400, '请选择要评价的技能。')
        key = data['key']
        ratings = self._ratings()
        if remove:
            if member['id'] not in ratings.get(key, {}):
                raise HubError(404, '你还没有评价这个技能。')
            del ratings[key][member['id']]
            result = {}
        else:
            card = next((c for c in self._catalog()['cards'] if c['key'] == key), None)
            if card is None:
                raise HubError(404, '这个技能已经下架，请刷新商店。')
            if any(r['member']['id'] == member['id'] for r in card['listings']):
                raise HubError(403, '自己上架的不能评价。')
            if type(data.get('stars')) is not int or not 1 <= data['stars'] <= 5:
                raise HubError(400, '评分请选择 1 到 5 星。')
            result = {'stars': data['stars'], 'updated_at': now()}
            if 'text' in data:
                try:
                    result['text'] = short_text(data['text'], '评价')
                except ValueError as error:
                    raise HubError(400, str(error)) from None
            ratings.setdefault(key, {})[member['id']] = result
            result = {**result, 'member': member}
        atomic_write(self.data / 'ratings.json', {'ratings': ratings})
        return result


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
        # The base parser collapses leading slashes; only the exact target is public.
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

    def _read_body(self, limit, too_large=None):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) > 1:
            raise HubError(400, '上传方式不支持，请直接发送完整内容。')
        if not lengths:
            raise HubError(411, '请带上配置文件的长度再上传。')
        try:
            length = int(lengths[0])
        except ValueError:
            raise HubError(400, '上传文件的长度不正确。') from None
        if length < 0:
            raise HubError(400, '上传文件的长度不正确。')
        if length > limit:
            raise HubError(413, too_large or ('上架内容超过 20 MB，请分批上架。' if limit == MAX_BODY else
                           '评价内容超过 64 KB，请缩短后重试。' if limit == MAX_RATING_BODY else
                           '加入信息超过 4096 字节，请缩短后重试。'))
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            raise HubError(408, '上传等待太久，请重新上传。') from None
        if len(body) != length:
            raise HubError(400, '配置文件没有传完整，请重新上传。')
        def invalid_constant(value):
            raise ValueError
        try:
            data = json.loads(body.decode('utf-8'), parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise HubError(400, '配置文件没能读懂，请重新导出。') from None
        if not isinstance(data, dict):
            raise HubError(400, '请求内容格式不正确。')
        return data

    def _handle(self):
        try:
            address = urllib.parse.urlsplit(self.path)
            if address.fragment:
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            path = address.path
            listing = re.fullmatch(r'/api/listings/me/(' + NAME + ')', path)
            package = re.fullmatch(r'/api/packages/([0-9a-f]{32})/(' + NAME + ')', path)
            query = urllib.parse.parse_qs(address.query, keep_blank_values=True)
            if '?' in self.path and (not package or set(query) != {'version'} or
                    len(query['version']) != 1 or not re.fullmatch(r'[0-9a-f]{64}', query['version'][0])):
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            allowed = ({'POST'} if path == '/api/join' else
                       {'GET'} if path in {'/api/me', '/api/catalog'} or package else
                       {'PUT'} if path in {'/api/listings/me', '/api/installs/me'} else {'DELETE'} if listing else
                       {'PUT', 'DELETE'} if path == '/api/ratings' else set())
            if not allowed:
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            if self.command not in allowed:
                raise HubError(405, '这个地址不支持这项操作。')
            if path == '/api/join':
                if not self.server.open:
                    raise HubError(403, '这个分享站需要管理员发的口令。')
                data = self._read_body(MAX_JOIN_BODY)
                try:
                    name = member_name(data.get('name'))
                except ValueError as error:
                    raise HubError(400, str(error)) from None
                # add_member takes the same data lock as administrator additions.
                member = self.server.store.add_member(name)
                self._json(200, {'service': 'skill-pilot-team', 'api': 2,
                                 'member': {key: member[key] for key in ('id', 'name')},
                                 'token': member['token']})
                return
            data = (self._read_body(MAX_INSTALL_BODY, '上报内容超过 64 KB，请缩短后重试。')
                    if path == '/api/installs/me' else
                    self._read_body(MAX_BODY if path == '/api/listings/me' else MAX_RATING_BODY)
                    if self.command == 'PUT' or path == '/api/ratings' else None)
            store = self.server.store
            with store.locked():
                # A member may have been removed while the upload body was arriving.
                member = store._authenticate(self.headers.get('Authorization', ''))
                if path == '/api/me':
                    result = {'service': 'skill-pilot-team', 'api': 2, 'member': member}
                elif path == '/api/catalog':
                    result = store._catalog(member['id'])
                elif path == '/api/listings/me':
                    result = store._put_listings(member['id'], data)
                elif path == '/api/installs/me':
                    result = store._put_installs(member['id'], data)
                elif listing:
                    result = store._remove_listing(member['id'], listing[1])
                elif package:
                    result = store._package(package[1], package[2], query.get('version', [None])[0])
                else:
                    result = store._rate(member, data, self.command == 'DELETE')
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
        print('已删除这位成员和他的上架、文件及评价。')
