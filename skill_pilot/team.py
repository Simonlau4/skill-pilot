"""Dashboard backend for the authenticated team skill store."""
import base64
from .platform_support import rmtree
import copy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path, PurePosixPath

from . import labels, operations, sharing, store, team_hub
from .scan import SKIP_CONTENT, metadata
from .sources import github, normalize
from .store import git_tree, guard

MAX_BODY = team_hub.MAX_BODY
TIMEOUT = 10
FORMAT_ERROR = '分享站的技能格式不对，请联系管理员检查。'
LOCAL_LABELS = {'mine': '我上架的', 'installed': '已装', 'update': '有新版', 'installable': '可以装',
                'lark': '要用飞书官方方式装', 'conflict': '本机有同名技能', 'blocked': '暂时装不了'}
SECRET_NAMES = ('.env', '.env.*', '*.pem', '*.key', '*.p12', 'id_rsa*', 'id_ed25519*',
                '.npmrc', '.pypirc', 'credentials*')
SECRET_CONTENT = re.compile(
    r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|'
    r'(?:sk-|ghp_|github_pat_|AKIA|xoxb-)[A-Za-z0-9_-]{16,}|'
    r'(?:api[_-]?key|token|secret|password)[\"\']?\s*[:=]\s*[\"\']?[^\s\"\',;\x00-\x1f]{16,}', re.I)


class RequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _settings(value):
    url, key = value.get('url'), value.get('key')
    if not isinstance(url, str) or not isinstance(key, str):
        raise ValueError('请填写分享站地址。')
    url, key = url.strip().rstrip('/'), key.strip()
    try:
        address = urllib.parse.urlsplit(url)
        port = address.port
        valid = (address.scheme in {'http', 'https'} and address.hostname and
                 address.username is None and address.password is None and
                 not any(char.isspace() or ord(char) < 32 for char in url) and
                 '?' not in url and '#' not in url and (port is None or port > 0))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('分享站地址只支持 http 或 https，不能带账号、密码或查询参数。')
    if key and not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('请粘贴管理员给你的完整口令。')
    return {'url': url, 'key': key}


def settings(home):
    path = Path(home) / '.skill-pilot/team.json'
    if not path.exists():
        return {}
    try:
        config = _settings(json.loads(path.read_text(encoding='utf-8')))
        if not config['key']:
            raise ValueError
        return config
    except (OSError, ValueError, AttributeError):
        raise ValueError('分享站设置没能读懂，请在设置里重新连接。') from None


def _redact(value, key):
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, '[已隐藏]')
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    return value


def _request(config, path, method='GET', data=None):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    limit = (team_hub.MAX_JOIN_BODY if path == '/api/join' else
             team_hub.MAX_RATING_BODY if path == '/api/ratings' else
             team_hub.MAX_INSTALL_BODY if path == '/api/installs/me' else MAX_BODY)
    if body is not None and len(body) > limit:
        raise ValueError('上报内容超过 64 KB，请缩短后重试。' if path == '/api/installs/me' else
                         '上架内容超过 20 MB，请分批上架。' if limit == MAX_BODY else
                         '评价内容超过 64 KB。' if limit == team_hub.MAX_RATING_BODY else
                         '加入信息超过 4096 字节，请缩短后重试。')
    key = config.get('key', '')
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    try:
        request = urllib.request.Request(config['url'] + path, data=body, method=method, headers=headers)
        with urllib.request.build_opener(labels.NoRedirect).open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError('分享站返回的内容超过 20 MB，请联系管理员检查。')
        def invalid_constant(value):
            raise ValueError
        try:
            result = json.loads(raw.decode('utf-8'), parse_constant=invalid_constant)
            if not isinstance(result, dict):
                raise ValueError
            return _redact(result, key)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError(FORMAT_ERROR) from None
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            detail = json.loads(error.read(team_hub.MAX_RATING_BODY).decode('utf-8')).get('error')
        except (ValueError, UnicodeError, AttributeError, HTTPException):
            detail = None
        finally:
            error.close()
        message = ('这个分享站需要管理员发的口令，请点「管理员给了口令？」填上。'
                   if path == '/api/join' and status == 403 else
                   '分享站没认出你（口令不对、已失效或已被移出），请到设置里重新连接。'
                   if status == 401 or (path == '/api/me' and status == 403) else
                   '分享站地址发生了跳转，请填写最终地址后重试。' if 300 <= status < 400 else
                   _redact(detail, key) if isinstance(detail, str) and detail else
                   '这个技能已经下架，请刷新商店。' if status == 404 else
                   '上架内容超过 20 MB，请分批上架。' if status == 413 else
                   '分享站暂时没能完成操作，请稍后重试。')
        raise RequestError(status, message) from None
    except (OSError, HTTPException):
        raise ValueError('连不上分享站，请检查地址和网络，或请管理员确认分享站已开启。') from None


def _member(value):
    if (not isinstance(value, dict) or not isinstance(value.get('id'), str) or
            not re.fullmatch(r'[0-9a-f]{32}', value['id']) or
            not isinstance(value.get('name'), str) or not value['name'].strip()):
        raise ValueError(FORMAT_ERROR)
    return {key: value[key] for key in ('id', 'name')}


def _connection(home):
    config = settings(home)
    if not config:
        raise ValueError('请先在设置里填写分享站地址和自己的口令。')
    return config


def _connection_version(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _me(config):
    result = _request(config, '/api/me')
    if type(result.get('api')) is not int or result['api'] != 2:
        raise ValueError('分享站和你的 Skill Pilot 版本不匹配，请一起换成新版。')
    if result.get('service') != 'skill-pilot-team':
        raise ValueError('这个地址不是 Skill Pilot 分享站，请核对地址。')
    return _member(result.get('member'))


def status(home, config=None, strict=True):
    result = {'url': '', 'has_key': False, 'configured': False, 'member': None}
    try:
        config = settings(home) if config is None else config
        public_url = config.get('url', '').replace(config['key'], '[已隐藏]') if config else ''
        result.update(url=public_url, has_key=bool(config.get('key')), configured=bool(config))
        if config:
            result['member'] = _me(config)
    except (ValueError, OSError) as error:
        if strict:
            raise
        result['error'] = str(error) if isinstance(error, ValueError) else '没能读取团队商店，请稍后重试。'
    return result


def save_settings(home, body, write_lock=None):
    guard(Path(home) / '.skill-pilot', home)
    config = _settings({'url': body.get('url'), 'key': body.get('key', '')})
    supplied_key = bool(config['key'])
    if not supplied_key:
        try:
            config['key'] = settings(home).get('key', '')
        except ValueError:
            config['key'] = ''
    result = None
    if config['key']:
        try:
            result = status(home, config)
        except RequestError as error:
            if supplied_key or error.status not in {401, 403} or not body.get('name'):
                raise
    if result is None:
        name = body.get('name', '')
        if isinstance(name, str) and not name.strip():
            raise ValueError('请填写你的名字，同事会看到它。')
        name = team_hub.member_name(name)
        joined = _request({'url': config['url']}, '/api/join', 'POST', {'name': name})
        if (joined.get('service') != 'skill-pilot-team' or type(joined.get('api')) is not int or
                joined['api'] != 2 or not isinstance(joined.get('token'), str) or
                not re.fullmatch(r'[A-Za-z0-9_-]+', joined['token'])):
            raise ValueError(FORMAT_ERROR)
        _member(joined.get('member'))
        config['key'] = joined['token']
        result = status(home, config)
    # Failed connections leave the previous working settings intact.
    from contextlib import nullcontext
    with write_lock if write_lock is not None else nullcontext():
        labels.atomic_write(home, 'team.json', json.dumps(config, ensure_ascii=False) + '\n', 0o600)
    return {**result, 'message': '分享站已连接，设置已保存。'}


def _catalog(config):
    data = _request(config, '/api/catalog')
    try:
        if not isinstance(data.get('cards'), list):
            raise ValueError
        has_installs = 'my_installs' in data
        if has_installs and (not isinstance(data['my_installs'], list) or
                            any(not isinstance(key, str) for key in data['my_installs'])):
            raise ValueError
        result, seen = [], set()
        for card in data['cards']:
            if (not isinstance(card, dict) or not isinstance(card.get('key'), str) or card['key'] in seen or
                    not isinstance(card.get('listings'), list) or not card['listings'] or
                    not isinstance(card.get('ratings'), list)):
                raise ValueError
            if ('installs' in card) != has_installs or (has_installs and
                    (type(card['installs']) is not int or card['installs'] < 0)):
                raise ValueError
            rows, members, ratings, raters = [], set(), [], set()
            for row in card['listings']:
                member = _member(row.get('member'))
                identity = (member['id'], row.get('name'))
                if (not isinstance(row.get('updated_at'), str) or identity in members or
                        team_hub.listing_key(member['id'], team_hub.clean_listing(row, packaged=True)) != card['key']):
                    raise ValueError
                members.add(identity)
                rows.append({**team_hub.clean_listing(row, packaged=True), 'member': member, 'updated_at': row['updated_at']})
            for row in card['ratings']:
                member = _member(row.get('member'))
                if (type(row.get('stars')) is not int or not 1 <= row['stars'] <= 5 or
                        not isinstance(row.get('updated_at'), str) or member['id'] in raters):
                    raise ValueError
                rating = {'member': member, 'stars': row['stars'], 'updated_at': row['updated_at']}
                if 'text' in row:
                    rating['text'] = team_hub.short_text(row['text'], '评价')
                ratings.append(rating)
                raters.add(member['id'])
            result.append({'key': card['key'], 'listings': sorted(rows, key=lambda r: r['updated_at'], reverse=True),
                           'ratings': sorted(ratings, key=lambda r: r['updated_at'], reverse=True)})
            if has_installs:
                result[-1]['installs'] = card['installs']
            seen.add(card['key'])
        catalog = {'cards': sorted(result, key=lambda c: c['listings'][0]['updated_at'], reverse=True)}
        if has_installs:
            catalog['my_installs'] = data['my_installs']
        return catalog
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError(FORMAT_ERROR) from None


def _fetch(home):
    config = _connection(home)
    return config, _me(config), _catalog(config)['cards']


def _owned(cards, member):
    return {row['name']: row for card in cards for row in card['listings'] if row['member']['id'] == member}


def _origin(skill):
    try:
        return normalize(skill.get('source', {}))
    except (ValueError, TypeError, AttributeError):
        return {'kind': 'skip'}


def _local(row, registry):
    name, source = row['name'], row['source']
    skills = registry['skills']
    if source['kind'] == 'github':
        for local_name, skill in skills.items():
            origin = _origin(skill)
            if (origin['kind'] == 'github' and origin['repository'].lower() == source['repository'].lower() and
                    PurePosixPath(origin['path']) == PurePosixPath(source['path'])):
                return 'installed', local_name
    if name not in skills:
        return ('lark' if source['kind'] == 'lark' else 'installable'), name
    origin = _origin(skills[name])
    if source['kind'] == 'lark':
        return 'installed', name
    if (source['kind'] == 'files' and origin['kind'] == 'team' and
            origin['member'] == row['member']['id'] and origin['name'] == name):
        return ('installed' if origin['version'] == source['version'] else 'update'), name
    return 'conflict', name


def _plan(card, cards, registry):
    root = card['listings'][0]
    rows = _owned(cards, root['member']['id'])
    install, reuse, errors = {}, set(), {}
    visiting = set()
    def visit(name, first=False):
        if name in visiting:
            return
        visiting.add(name)
        if not first and name in registry['skills']:
            reuse.add(name)
            return
        row = root if first else rows.get(name)
        if row is None:
            errors[name] = '作者没有上架这个所需技能。'
            return
        state, local_name = _local(row, registry)
        if state in {'installed', 'update'}:
            reuse.add(local_name)
            return
        if state in {'lark', 'conflict'}:
            errors[name] = LOCAL_LABELS[state] + '。'
            return
        install[name] = row
        for dependency in row['requires']:
            visit(dependency)
    visit(root['name'], True)
    if errors:
        reason = '所需技能装不了：' + '；'.join(n + '（' + r + '）' for n, r in sorted(errors.items()))
        errors.setdefault(root['name'], reason)
        return {}, set(), [{'name': n, 'reason': r} for n, r in sorted(errors.items())]
    return install, reuse, []


def catalog(home):
    config = _connection(home)
    member = _me(config)
    data = _catalog(config)
    cards = data['cards']
    _, registry, _ = operations.load(home)
    result, current = [], set()
    for card in cards:
        row = card['listings'][0]
        state, local_name = _local(row, registry)
        mine = any(r['member']['id'] == member['id'] for r in card['listings'])
        reason = ''
        if mine:
            state = 'mine'
        elif state == 'installable':
            _, _, skipped = _plan(card, cards, registry)
            if skipped:
                state = 'blocked'
                reason = next(r['reason'] for r in skipped if r['name'] == row['name'])
        if state in {'lark', 'conflict'}:
            reason = LOCAL_LABELS[state] + '。'
        if state in {'installed', 'update'} and not registry['skills'].get(local_name, {}).get('withdrawn'):
            current.add(card['key'])
        local = {'status': state, 'label': LOCAL_LABELS[state], 'reason': reason,
                 'agents': registry['skills'].get(local_name, {}).get('assigned_agents', [])}
        details = {}
        for field in team_hub.TEXT_FIELDS[:-1]:
            value = next((r[field] for r in card['listings'] if field in r), None)
            if value is not None:
                details[field] = value
        my_rating = next(({k: r[k] for k in ('stars', 'text') if k in r} for r in card['ratings']
                          if r['member']['id'] == member['id']), None)
        reviews = card['ratings']
        result.append({'key': card['key'], 'kind': row['source']['kind'], 'name': row['name'], **details,
            'requires': row['requires'], 'updated_at': row['updated_at'],
            'listings': [{**{k: r[k] for k in ('member', 'note', 'updated_at') if k in r},
                          **{k: r['source'][k] for k in ('version', 'file_count', 'size') if k in r['source']}}
                         for r in card['listings']],
            'rating': {'average': round(sum(r['stars'] for r in reviews) / len(reviews), 1) if reviews else None,
                       'count': len(reviews)}, 'reviews': reviews, 'my_rating': my_rating,
            'can_rate': not mine and state in {'installed', 'update'}, 'local': local,
            'tokens': operations.listing_tokens(row['name'], details)})
        if 'installs' in card:
            result[-1]['installs'] = card['installs']
    if 'my_installs' in data and store.write_allowed(home):
        previous = set(data['my_installs'])
        if current != previous:
            try:
                _request(config, '/api/installs/me', 'PUT', {'keys': sorted(current)})
            except (ValueError, OSError):
                pass
            else:
                for card in result:
                    key = card['key']
                    counted_before = key in previous and card['local']['status'] != 'mine'
                    card['installs'] = max(0, card['installs'] - counted_before + (key in current))
    return {'member': member, 'cards': result}


def package(config, member, name, version, destination):
    data = _request(config, '/api/packages/' + member + '/' + name + '?version=' + version)
    try:
        files = team_hub.clean_files(data.get('files'))
        if data.get('name') != name or data.get('version') != version or team_hub.package_version(files) != version:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(FORMAT_ERROR) from None
    destination = Path(destination)
    destination.mkdir(parents=True)
    for row in files:
        path = destination / row['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(row['content']))
        path.chmod(0o755 if row['executable'] else 0o644)
    github.safe_skill(destination)
    if metadata((destination / 'SKILL.md').read_text(encoding='utf-8')).get('name') != name:
        raise ValueError('技能文件里的名称和上架的名称不一致，请联系作者。')
    return destination


def install(home, key):
    home, registry, agents = operations.load(home)
    guard(home / '.skill-pilot', home)
    config, _, cards = _fetch(home)
    card = next((c for c in cards if c['key'] == key), None)
    if card is None:
        raise ValueError('这个技能已经下架。')
    root = card['listings'][0]
    existing = {'installed': [], 'reuse': [], 'message': '本机已经有了。'}
    if _local(root, registry)[0] in {'installed', 'update'}:
        return existing
    rows, _, skipped = _plan(card, cards, registry)
    if skipped:
        raise ValueError(next(r['reason'] for r in skipped if r['name'] == root['name']))
    title = next((r['title'] for r in card['listings'] if 'title' in r), root['name'])
    with tempfile.TemporaryDirectory(prefix='skill-pilot-team-install-') as temporary:
        items = {}
        for name, row in sorted(rows.items()):
            source = row['source']
            path = Path(temporary) / name
            if source['kind'] == 'files' or 'version' in source:
                package(config, row['member']['id'], name, source['version'], path)
                if source['kind'] == 'files':
                    origin = {'source': '团队商店', 'team': {'member': row['member']['id'],
                        'member_name': row['member']['name'], 'name': name, 'version': source['version'],
                        'key': team_hub.listing_key(row['member']['id'], row)}}
                else:
                    origin = {'source': 'https://github.com/' + source['repository'] + '.git',
                        'repository': source['repository'], 'source_path': source['path'],
                        'commit': source.get('commit', ''), 'confidence': '高',
                        'evidence': '从团队商店读取 GitHub 来源的文件，未执行其中的程序'}
                    if 'tree_sha' in source:
                        origin['tree_sha'] = source['tree_sha']
                items[name] = {'name': name, 'path': str(path), 'source': origin}
            else:
                suffix = source.get('commit')
                if not suffix:
                    remote, _, _ = github.parse_url('https://github.com/' + source['repository'])
                    suffix = github.git(Path(temporary), 'ls-remote', '--exit-code', remote, 'HEAD').split()[0]
                if source['path'] != '.':
                    suffix += '/' + source['path']
                url = 'https://github.com/' + source['repository'] + '/tree/' + urllib.parse.quote(suffix, safe='/')
                download = github.inspect(url)
                try:
                    found = next((s for s in download['skills'] if s['name'] == name and
                                  PurePosixPath(s['repository_path']) == PurePosixPath(source['path'])), None)
                    if found is None:
                        raise ValueError(name + ' 的来源里没有找到指定的技能，请联系上架的同事。')
                    github.safe_skill(Path(found['path']))
                    shutil.copytree(found['path'], path, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_CONTENT))
                    items[name] = {**found, 'path': str(path)}
                finally:
                    rmtree(download['temporary'], ignore_errors=True)

        class AlreadyInstalled(ValueError):
            pass

        pending = {}
        def expected():
            current = operations.load(home)[1]
            if _local(root, current)[0] in {'installed', 'update'}:
                raise AlreadyInstalled
            current_rows, reuse, skipped = _plan(card, cards, current)
            if skipped:
                raise ValueError(next(r['reason'] for r in skipped if r['name'] == root['name']))
            if set(current_rows) - items.keys():
                raise ValueError('本机所需技能已经变了，请重试安装。')
            pending.update(rows=current_rows, reuse=sorted(reuse))
            return current

        def mutate(current, checked):
            names = sorted(pending['rows'])
            for name in names:
                item = items[name]
                operations._put(home, current, item, [], [], checked)
                skill = current['skills'][name]
                skill['requires'] = rows[name]['requires']
                if _origin(skill)['kind'] == 'github' and item['source'].get('tree_sha'):
                    skill['base_tree_sha'] = item['source']['tree_sha']
                    skill['base_normalized_tree_sha'] = git_tree(home / '.skill-pilot/bases' / name)
            return names, names, {'installed': names, 'reuse': pending['reuse']}
        try:
            return operations._run(home, registry, agents, expected, '从团队商店装上了「' + title + '」', mutate)
        except AlreadyInstalled:
            return existing


def _files(root):
    root = Path(root)
    github.safe_skill(root)
    files, blocked, size = [], [], 0
    def walk(folder, ancestors):
        nonlocal size
        resolved = folder.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError('这个技能引用了技能文件夹外的文件，不能上架。')
        if resolved in ancestors:
            raise ValueError('这个技能的文件夹相互引用，无法完整读取。')
        for path in sorted(folder.iterdir()):
            if path.name in SKIP_CONTENT:
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('这个技能引用了技能文件夹外的文件，不能上架。')
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                walk(path, ancestors | {resolved})
                continue
            if any(fnmatch.fnmatch(path.name.lower(), pattern) for pattern in SECRET_NAMES):
                blocked.append({'path': relative, 'reason': '文件名像账号或密钥文件，已拦下。'})
                continue
            if not path.is_file():
                raise ValueError('技能里有不能读取的文件，不能上架。')
            with path.open('rb') as stream:
                raw = stream.read(team_hub.MAX_SIZE + 1)
            if SECRET_CONTENT.search(raw.decode('utf-8', errors='replace')):
                blocked.append({'path': relative, 'reason': '内容疑似含有密钥或密码，已拦下。'})
                continue
            size += len(raw)
            if size > team_hub.MAX_SIZE:
                raise ValueError('单个技能的文件合计不能超过 5 MB。')
            files.append({'path': relative, 'content': base64.b64encode(raw).decode('ascii'),
                          'executable': bool(path.stat().st_mode & 0o111)})
            if len(files) > team_hub.MAX_FILES:
                raise ValueError('单个技能不能超过 300 个文件。')
    walk(root, set())
    if not any(f['path'] == 'SKILL.md' for f in files):
        raise ValueError('SKILL.md 缺失或疑似含有密钥，整个技能不能上架。')
    return team_hub.clean_files(files), {'count': len(files), 'size': size, 'blocked': blocked}


def _candidates(home, registry, member, cards):
    shared = _owned(cards, member['id'])
    result = {}
    for row in sharing.export_config(home, translated_description=True)['skills']:
        name = row['name']
        skill = registry['skills'][name]
        origin = _origin(skill)
        kind = ('github' if origin['kind'] == 'github' and row['source']['kind'] == 'github' and not row['local_modified'] else
                'lark' if origin['kind'] == 'lark' else 'files')
        unavailable, version = '', None
        if origin['kind'] == 'team' and origin['member'] != member['id']:
            unavailable = '从团队商店装的别人的技能，不能作为自己的技能上架。'
        try:
            github.safe_skill(home / '.skill-pilot/store' / name)
        except ValueError:
            unavailable = '这个技能引用了技能文件夹外的文件，不能上架。'
        listed = shared.get(name)
        if kind in {'files', 'github'} and not unavailable and listed:
            try:
                files, info = _files(home / '.skill-pilot/store' / name)
                if kind == 'files' or not info['blocked']:
                    version = team_hub.package_version(files)
            except (ValueError, OSError):
                version = ''
        source = dict(row['source'])
        if kind == 'github':
            recorded = skill.get('source', {})
            recorded = recorded if isinstance(recorded, dict) else {}
            tree = (skill.get('base_tree_sha') or recorded.get('tree_sha') or
                    recorded.get('record', {}).get('skillFolderHash'))
            if isinstance(tree, str) and re.fullmatch(r'[0-9a-f]{40}', tree):
                source['tree_sha'] = tree
        result[name] = {**{k: row[k] for k in ('name', 'title', 'summary', 'group', 'description', 'requires') if k in row},
            'kind': kind, 'listed': bool(listed), 'note': listed.get('note', '') if listed else '',
            'changed': bool(listed and kind in {'files', 'github'} and (kind != 'github' or version) and
                            (not listed['source'].get('version') or version != listed['source']['version'])),
            'unavailable': unavailable, '_source': source}
    return result


def listing_candidates(home):
    _, member, cards = _fetch(home)
    _, registry, _ = operations.load(home)
    rows = _candidates(home, registry, member, cards)
    return {'skills': [{k: v for k, v in row.items() if not k.startswith('_')} for name, row in rows.items()
                       if registry['skills'][name]['assigned_agents']]}


def listings_preview(home, skills):
    home, registry, agents = operations.load(home)
    revision = operations.revision(home, agents)
    config, member, cards = _fetch(home)
    candidates = _candidates(home, registry, member, cards)
    if (not isinstance(skills, list) or not skills or
            any(not isinstance(s, dict) or not isinstance(s.get('name'), str) or s['name'] not in candidates or
                not registry['skills'][s['name']]['assigned_agents'] for s in skills) or
            len({s['name'] for s in skills}) != len(skills)):
        raise ValueError('请从本机在用的技能里选择要上架的技能。')
    notes = {s['name']: team_hub.short_text(s.get('note', ''), '推荐语') for s in skills}
    shared, auto = _owned(cards, member['id']), {}
    selected = set(notes)
    pending = list(sorted(selected))
    while pending:
        parent = pending.pop()
        for dependency in registry['skills'].get(parent, {}).get('requires', []):
            if dependency in shared and dependency not in selected:
                continue
            auto.setdefault(dependency, set()).add(parent)
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    payload, public, errors = {}, {}, {}
    for name in sorted(selected):
        row = candidates.get(name)
        if row is None:
            errors[name] = '本机缺少这个所需技能。'
            continue
        if row['unavailable']:
            errors[name] = row['unavailable']
            continue
        listing = {k: row[k] for k in ('name', 'title', 'summary', 'group', 'description', 'requires') if k in row}
        listing['note'] = notes.get(name, '')
        files_info = {'count': 0, 'size': 0, 'blocked': []}
        version = None
        download_note = ''
        try:
            if row['kind'] == 'files':
                files, files_info = _files(home / '.skill-pilot/store' / name)
                listing['source'] = {'kind': 'files', 'files': files}
                version = team_hub.package_version(files)
            else:
                listing['source'] = dict(row['_source']) if row['kind'] == 'github' else {'kind': 'lark'}
                if row['kind'] == 'github':
                    try:
                        files, files_info = _files(home / '.skill-pilot/store' / name)
                        if not files_info['blocked']:
                            listing['source']['files'] = files
                            version = team_hub.package_version(files)
                    except (ValueError, OSError):
                        pass
                    if version is None:
                        download_note = '同事装的时候从 GitHub 下载。'
            listing = team_hub.clean_listing(listing)
            payload[name] = listing
            public[name] = {**{k: v for k, v in listing.items() if k != 'source'},
                            'title': listing.get('title', name), 'kind': row['kind'],
                            'auto': sorted(auto.get(name, set())), 'files': files_info, 'version': version,
                            **({'download_note': download_note} if download_note else {})}
        except (ValueError, OSError) as error:
            errors[name] = str(error) if isinstance(error, ValueError) else '技能文件没能完整读取，请检查后重试。'
    while True:
        before = len(errors)
        for name in selected - errors.keys():
            missing = set(registry['skills'].get(name, {}).get('requires', [])) & errors.keys()
            if missing:
                errors[name] = '所需技能不能上架：' + '、'.join(sorted(missing))
        if len(errors) == before:
            break
    valid = sorted(selected - errors.keys())
    saved = {'listings': [payload[n] for n in valid], 'revision': revision, 'connection': _connection_version(config)}
    result = {'listings': [public[n] for n in valid], 'errors': [{'name': n, 'reason': r} for n, r in sorted(errors.items())],
              'message': f'可上架 {len(valid)} 个，不能上架 {len(errors)} 个。'}
    return saved, result


def listings_apply(home, review):
    home, _, agents = operations.load(home)
    guard(home / '.skill-pilot', home)
    if operations.revision(home, agents) != review['revision']:
        raise ValueError('本机技能文件已经变了，请重新预览后再上架。')
    config = _connection(home)
    if _connection_version(config) != review['connection']:
        raise ValueError('分享站设置已经变了，请重新预览。')
    if not review['listings']:
        raise ValueError('没有可以上架的技能，请先处理预览里的原因。')
    _me(config)
    result = _request(config, '/api/listings/me', 'PUT', {'listings': review['listings']})
    return {**result, 'message': '技能已上架。'}


def listings_remove(home, names):
    guard(Path(home) / '.skill-pilot', home)
    config, member, cards = _fetch(home)
    rows = _owned(cards, member['id'])
    if (not isinstance(names, list) or not names or
            any(not isinstance(n, str) or n not in rows for n in names)):
        raise ValueError('请从自己上架的技能里选择要下架的技能。')
    pending, order = set(names), []
    parents = [n for n, row in rows.items() if n not in pending and set(row['requires']) & pending]
    if parents:
        raise ValueError('这些上架的技能仍需要它们：' + '、'.join(sorted(parents)))
    while pending:
        ready = sorted(n for n in pending if not any(n in rows[p]['requires'] for p in pending - {n}))
        if not ready:
            # The hub deletes one listing at a time. Break only the internal
            # edges of a cycle whose entire membership is being removed.
            replacements = []
            for name in sorted(pending):
                row = copy.deepcopy(rows[name])
                row['requires'] = [n for n in row['requires'] if n not in pending]
                if 'version' in row['source']:
                    data = _request(config, '/api/packages/' + member['id'] + '/' + name + '?version=' + row['source']['version'])
                    files = team_hub.clean_files(data.get('files'))
                    if team_hub.package_version(files) != row['source']['version']:
                        raise ValueError(FORMAT_ERROR)
                    row['source'] = {k: v for k, v in row['source'].items() if k not in {'version', 'file_count', 'size'}}
                    row['source']['files'] = files
                replacements.append(team_hub.clean_listing(row))
            _request(config, '/api/listings/me', 'PUT', {'listings': replacements})
            order.extend(sorted(pending))
            break
        order.extend(ready)
        pending.difference_update(ready)
    for name in order:
        _request(config, '/api/listings/me/' + name, 'DELETE')
    return {'message': '技能已下架。'}


def rate(home, body, remove=False):
    guard(Path(home) / '.skill-pilot', home)
    card = next((c for c in catalog(home)['cards'] if c['key'] == body.get('key')), None)
    if card is None:
        raise ValueError('这个技能已经下架，请刷新商店。')
    if card['local']['status'] == 'mine':
        raise ValueError('自己上架的不能评价。')
    if not card['can_rate']:
        raise ValueError('装过才能评价。')
    data = {'key': card['key']}
    if not remove:
        data.update({k: body[k] for k in ('stars', 'text') if k in body})
    result = _request(_connection(home), '/api/ratings', 'DELETE' if remove else 'PUT', data)
    return {**result, 'message': '评价已删除。' if remove else '评价已保存。'}
