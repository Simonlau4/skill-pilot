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
import sys
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

from . import feedback, sharing, tags

MAX_BODY = 20 * 1024 * 1024
MAX_RATING_BODY = 64 * 1024
MAX_INSTALL_BODY = 64 * 1024
MAX_PACK_BODY = 64 * 1024
MAX_JOIN_BODY = 4096
INLINE_MAX_FILES = 300
INLINE_MAX_SIZE = 5 * 1024 * 1024
MAX_FILES = 1000
SKILL_FILE_HEADER = 'X-Skill-Pilot-Skill-Files'
MAX_SIZE = 100 * 1024 * 1024
TEXT_FIELDS = ('title', 'summary', 'group', 'description', 'note')
NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]*'
MAX_BLOB = 50 * 1024 * 1024
MAX_DELIVERABLE_SIZE = 200 * 1024 * 1024
MAX_DELIVERABLE_FILES = 2000
MAX_DELIVERABLES = 50
MAX_MANIFEST_BODY = 1024 * 1024
MAX_RECIPE = 64 * 1024
DELIVERABLE_TYPES = {
    '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8', '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
    '.csv': 'text/csv; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif', '.ico': 'image/x-icon',
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime', '.mp3': 'audio/mpeg',
    '.m4a': 'audio/mp4', '.wav': 'audio/wav', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
    '.otf': 'font/otf', '.pdf': 'application/pdf'}
GITHUB_SEARCH_LIMIT = 2 * 1024 * 1024
GITHUB_TREE_LIMIT = 8 * 1024 * 1024
GITHUB_READMES = ('readme.md', 'readme.markdown', 'readme.rst', 'readme.txt', 'readme')


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


def _github_time(value):
    if (isinstance(value, str) and re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value)):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    raise ValueError('GitHub 最近热门的时间格式不正确。')


def github_branch(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', value) or
            '..' in value or value.startswith('/') or value.endswith('/')):
        raise ValueError('GitHub 仓库的分支名不正确。')
    return value


def github_path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 300 or '\\' in value or
            any(part in {'', '.', '..'} for part in value.split('/')) or
            any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in value)):
        raise ValueError('GitHub 仓库的文件位置不正确。')
    return value


def _github_repo(row):
    if not isinstance(row, dict):
        raise ValueError('GitHub 最近热门的仓库格式不正确。')
    repository = row.get('repository')
    match = (re.fullmatch(r'([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)/([A-Za-z0-9._-]{1,100})', repository)
             if isinstance(repository, str) else None)
    description = row.get('description')
    stars, count = row.get('stars'), row.get('skill_count')
    if (not match or len(match[1]) > 39 or match[2] in {'.', '..'} or
            description is not None and not isinstance(description, str) or
            type(stars) is not int or stars < 0 or
            'skill_count' not in row or count is not None and (type(count) is not int or count <= 0)):
        raise ValueError('GitHub 最近热门的仓库格式不正确。')
    for field in ('created_at', 'pushed_at'):
        _github_time(row.get(field))
    result = {'repository': repository,
            'description': ''.join(c for c in description or '' if unicodedata.category(c) not in
                                   {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'})[:500],
            'stars': stars, 'created_at': row['created_at'], 'pushed_at': row['pushed_at'],
            'skill_count': count}
    fields = {'branch', 'readme_path', 'skill_paths'}
    if fields & row.keys():
        if not fields <= row.keys():
            raise ValueError('GitHub 仓库的文件位置不完整。')
        branch = github_branch(row['branch'])
        readme, skills = row['readme_path'], row['skill_paths']
        if readme is not None:
            github_path(readme)
            if readme.lower() not in GITHUB_READMES:
                raise ValueError('GitHub 仓库的说明文件位置不正确。')
        if (not isinstance(skills, list) or len(skills) > 30 or
                any(github_path(path).rsplit('/', 1)[-1] != 'SKILL.md' for path in skills) or
                len(set(skills)) != len(skills)):
            raise ValueError('GitHub 仓库的技能文件位置不正确。')
        result.update(branch=branch, readme_path=readme, skill_paths=sorted(skills))
    return result


def clean_github(data):
    if not isinstance(data, dict) or not isinstance(data.get('repos'), list) or len(data['repos']) > 20:
        raise ValueError('GitHub 最近热门的名单格式不正确。')
    _github_time(data.get('fetched_at'))
    return {'fetched_at': data['fetched_at'], 'repos': [_github_repo(row) for row in data['repos']]}


class _GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        address = urllib.parse.urlsplit(newurl)
        if address.scheme != 'https' or address.netloc != 'api.github.com':
            raise ValueError('GitHub 跳转到了其他地址，已停止访问。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _GitHubResponseTooLarge(ValueError):
    pass


def _github_json(path, limit):
    request = urllib.request.Request('https://api.github.com' + path, headers={
        'User-Agent': 'skill-pilot-team-hub', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.build_opener(_GitHubRedirect()).open(request, timeout=15) as response:
            content = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError(f'GitHub 公开接口返回 {error.code}，请稍后再试。') from None
    except (OSError, HTTPException):
        raise ValueError('暂时连不上 GitHub 公开接口。') from None
    if len(content) > limit:
        raise _GitHubResponseTooLarge('GitHub 返回的内容太大，暂时不能读取。')
    try:
        return json.loads(content)
    except (ValueError, RecursionError):
        raise ValueError('GitHub 返回的内容格式不正确。') from None


def fetch_github_trending():
    fetched_at = now()
    cutoff = (_github_time(fetched_at) - timedelta(days=30)).date().isoformat()
    query = urllib.parse.urlencode({'q': f'topic:agent-skills created:>{cutoff} fork:false archived:false',
                                    'sort': 'stars', 'order': 'desc', 'per_page': 50})
    data = _github_json('/search/repositories?' + query, GITHUB_SEARCH_LIMIT)
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValueError('GitHub 返回的仓库名单格式不正确。')
    candidates = []
    for item in data['items'][:50]:
        if not isinstance(item, dict):
            continue
        name, topics, branch = item.get('name'), item.get('topics', []), item.get('default_branch')
        if (not isinstance(name, str) or not isinstance(topics, list) or
                any(not isinstance(topic, str) for topic in topics) or
                any('awesome' in text.lower() for text in [name, *topics]) or
                item.get('fork') or item.get('archived') or not isinstance(branch, str) or not branch):
            continue
        try:
            github_branch(branch)
            row = _github_repo({'repository': item.get('full_name'), 'description': item.get('description'),
                                'stars': item.get('stargazers_count'), 'created_at': item.get('created_at'),
                                'pushed_at': item.get('pushed_at'), 'skill_count': None})
        except ValueError:
            continue
        if row['repository'].split('/')[1] == name:
            candidates.append((row, branch))
    repos, completed = [], 0
    for row, branch in sorted(candidates, key=lambda candidate: candidate[0]['stars'], reverse=True)[:40]:
        readme, skills = None, []
        try:
            tree = _github_json('/repos/' + row['repository'] + '/git/trees/' +
                                urllib.parse.quote(branch, safe='') + '?recursive=1', GITHUB_TREE_LIMIT)
            if (not isinstance(tree, dict) or type(tree.get('truncated')) is not bool or
                    not isinstance(tree.get('tree'), list) or
                    any(not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or
                        not isinstance(entry.get('type'), str) for entry in tree['tree'])):
                raise ValueError('GitHub 返回的文件清单格式不正确。')
            count = None if tree['truncated'] else sum(entry['type'] == 'blob' and
                    entry['path'].rsplit('/', 1)[-1] == 'SKILL.md' for entry in tree['tree'])
            if not tree['truncated']:
                paths = sorted(entry['path'] for entry in tree['tree'] if entry['type'] == 'blob')
                readme = next((path for name in GITHUB_READMES for path in paths if path.lower() == name), None)
                skills = [path for path in paths if path.rsplit('/', 1)[-1] == 'SKILL.md'][:30]
        except _GitHubResponseTooLarge:
            count = None
        except ValueError:
            continue
        completed += 1
        if count == 0:
            continue
        try:
            repos.append(_github_repo({**row, 'skill_count': count, 'branch': branch,
                                       'readme_path': readme, 'skill_paths': skills}))
        except ValueError:
            continue
        if len(repos) == 20:
            break
    if candidates and not completed:
        raise ValueError('暂时没能读取 GitHub 仓库的文件清单。')
    return {'fetched_at': fetched_at, 'repos': repos}


def github_worker(store, stopped):
    while not stopped.is_set():
        store.refresh_github()
        stopped.wait(3600)


def short_text(value, label):
    if (not isinstance(value, str) or len(value) > 200 or
            any(unicodedata.category(c) in {'Cc', 'Cf', 'Zl', 'Zp'} for c in value)):
        raise ValueError(f'{label}最多 200 字，不能含换行或看不见的字符。')
    return value


def clean_files(files):
    if not isinstance(files, list) or not files or len(files) > INLINE_MAX_FILES:
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
        if size > INLINE_MAX_SIZE:
            raise ValueError('单个技能的文件合计不能超过 5 MB。')
        seen.add(path)
        result.append({'path': path, 'content': content, 'executable': executable})
    if 'SKILL.md' not in seen:
        raise ValueError('技能根目录必须有 SKILL.md。')
    if any('/'.join(path.split('/')[:i]) in seen for path in seen for i in range(1, len(path.split('/')))):
        raise ValueError('技能文件和文件夹的位置相互冲突。')
    return result


def clean_skill_manifest(files):
    if not isinstance(files, dict) or 'SKILL.md' not in files or len(files) > MAX_FILES:
        raise ValueError(f'每个技能根目录须有 SKILL.md，且最多 {MAX_FILES} 个文件。')
    checked, paths, directories, sizes, size = {}, set(), {}, {}, 0
    for path, row in files.items():
        parts = path.split('/') if isinstance(path, str) else []
        if (not parts or len(path) > 300 or any(p in {'', '.', '..'} or p.endswith((' ', '.')) or
                PureWindowsPath(p).is_reserved() for p in parts) or re.search(r'[<>:"\\|?*]', path) or
                any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in path)):
            raise ValueError('技能文件的位置不安全，请使用文件夹内的相对位置。')
        folded = unicodedata.normalize('NFC', path).casefold()
        if folded in paths:
            raise ValueError('技能有位置重复的文件。')
        paths.add(folded)
        for i in range(1, len(parts)):
            directory = '/'.join(parts[:i])
            key = unicodedata.normalize('NFC', directory).casefold()
            if directories.setdefault(key, directory) != directory:
                raise ValueError('技能有位置重复的文件夹。')
        if (not isinstance(row, dict) or not isinstance(row.get('sha256'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) or type(row.get('size')) is not int or
                not 0 <= row['size'] <= MAX_BLOB or type(row.get('executable')) is not bool or
                sizes.setdefault(row['sha256'], row['size']) != row['size']):
            raise ValueError(f'技能文件的内容编号、执行权限或大小不正确，单个文件最多 {MAX_BLOB // 1024 // 1024} MB。')
        size += row['size']
        checked[path] = {k: row[k] for k in ('sha256', 'size', 'executable')}
    if paths.intersection(directories):
        raise ValueError('技能文件和文件夹的位置相互冲突。')
    if size > MAX_SIZE:
        raise ValueError(f'单个技能的文件合计不能超过 {MAX_SIZE // 1024 // 1024} MB。')
    return checked


def package_version(files):
    if isinstance(files, dict):
        rows = [[path, row['executable'], row['sha256']] for path, row in files.items()]
    else:
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
            files = source.get('files')
            result['source']['files'] = clean_skill_manifest(files) if isinstance(files, dict) else clean_files(files)
    return result


def listing_key(member, row):
    source = row['source']
    if source['kind'] == 'github':
        return 'github:' + source['repository'].lower() + ':' + source['path']
    return 'lark:' + row['name'] if source['kind'] == 'lark' else 'team:' + member + ':' + row['name']


def clean_pack(row, listings=None):
    if not isinstance(row, dict):
        raise ValueError('技能包格式不正确。')
    identity, skills = row.get('id'), row.get('skills')
    if not isinstance(identity, str) or len(identity) > 64 or not re.fullmatch(NAME, identity):
        raise ValueError('技能包编号须以字母或数字开头，最多 64 个字符，只能用字母、数字、点、横线或下划线。')
    if (not isinstance(skills, list) or not 2 <= len(skills) <= 50 or
            any(not isinstance(n, str) or not re.fullmatch(NAME, n) for n in skills) or
            len(set(skills)) != len(skills)):
        raise ValueError('每个技能包须有 2–50 个不重复的技能。')
    if listings is not None:
        if any(n not in listings for n in skills):
            raise ValueError('技能包只能包含本人已经上架的技能。')
        if any(listings[n]['source']['kind'] == 'lark' for n in skills):
            raise ValueError('飞书官方技能不能放进技能包。')
    result = {'id': identity, 'title': tags.clean_tag(row.get('title')), 'skills': list(skills)}
    if 'note' in row:
        result['note'] = short_text(row['note'], '推荐语')
    return result


def clean_packs(rows, listings=None):
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError('每位成员最多整理 50 个技能包，请把技能包放在列表里。')
    result, identities, skills = [], set(), set()
    for row in rows:
        pack = clean_pack(row, listings)
        if pack['id'] in identities:
            raise ValueError('技能包编号不能重复。')
        if skills.intersection(pack['skills']):
            raise ValueError('同一个技能最多放进一个技能包。')
        identities.add(pack['id'])
        skills.update(pack['skills'])
        result.append(pack)
    return result


def deliverable_path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 300 or '\\' in value or
            any(part in {'', '.', '..'} for part in value.split('/')) or
            any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in value)):
        raise ValueError('成品文件的位置不安全，请使用文件夹内的相对位置。')
    return value


DELIVERABLE_ID = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'


def deliverable_info(row):
    """The fields every member sees on a deliverable card."""
    if not isinstance(row, dict):
        raise ValueError('成品信息格式不正确。')
    title = row.get('title')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('请填写成品名字。')
    result = {'title': short_text(title.strip(), '成品名字'), 'version': short_text(row.get('version', ''), '版本'),
              'summary': short_text(row.get('summary', ''), '一句话说明')}
    skills = row.get('skills', [])
    if (not isinstance(skills, list) or len(skills) > 30 or len(set(skills)) != len(skills) or
            any(not isinstance(n, str) or len(n) > 64 or not re.fullmatch(NAME, n) for n in skills)):
        raise ValueError('用到的技能须是不重复的技能名，最多 30 个。')
    result['skills'] = list(skills)
    return result


def clean_deliverable(row):
    """A static demo folder: card fields, its recipe and a path → content fingerprint list."""
    result = deliverable_info(row)
    recipe = row.get('recipe', '')
    if (not isinstance(recipe, str) or len(recipe.encode()) > MAX_RECIPE or
            any(unicodedata.category(c) in {'Cc', 'Cf'} and c not in '\n\t' for c in recipe)):
        raise ValueError('复刻配方须是 64 KB 以内的文字。')
    result['recipe'] = recipe
    files = row.get('files')
    if not isinstance(files, dict) or 'index.html' not in files:
        raise ValueError('成品文件夹的首页必须是 index.html。')
    if len(files) > MAX_DELIVERABLE_FILES:
        raise HubError(413, f'成品最多 {MAX_DELIVERABLE_FILES} 个文件。')
    checked, size = {}, 0
    for path, entry in files.items():
        deliverable_path(path)
        if (not isinstance(entry, dict) or not isinstance(entry.get('sha256'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) or
                type(entry.get('size')) is not int or not 0 <= entry['size'] <= MAX_BLOB):
            raise ValueError('成品文件的大小或内容编号不正确。')
        checked[path] = {'sha256': entry['sha256'], 'size': entry['size']}
        size += entry['size']
    if any('/'.join(path.split('/')[:i]) in checked for path in checked for i in range(1, len(path.split('/')))):
        raise ValueError('成品文件和文件夹的位置相互冲突。')
    if size > MAX_DELIVERABLE_SIZE:
        raise HubError(413, f'成品文件合计不能超过 {MAX_DELIVERABLE_SIZE // 1024 // 1024} MB。')
    return {**result, 'files': checked, 'file_count': len(checked), 'size': size}


def byte_range(header, size):
    """One `bytes=` range as (start, end), None for a whole-file reply; raises for an unsatisfiable one."""
    if not header:
        return None
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header.strip())
    if not match or match[1] == match[2] == '':
        return None  # Unsupported forms (several ranges) get the whole file.
    if match[1] == '':
        start, end = max(size - int(match[2]), 0), size - 1
    else:
        start, end = int(match[1]), min(int(match[2]) if match[2] else size - 1, size - 1)
    if start >= size or start > end:
        raise HubError(416, '请求的文件范围超出文件大小。')
    return start, end


class HubError(ValueError):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


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

    def _feedback(self):
        path = self.data / 'feedback.json'
        if not path.exists():
            return []
        rows = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(rows, list):
            raise HubError(500, '反馈备份没能读懂，请联系管理员检查。')
        return rows

    def _save_feedback(self, rows):
        rows = rows[-feedback.MAX_RECORDS:]
        while len((json.dumps(rows, ensure_ascii=False, indent=2) + '\n').replace('\n', os.linesep).encode()) > feedback.MAX_BYTES:
            rows.pop(0)
        atomic_write(self.data / 'feedback.json', rows)

    def _receive_feedback(self, member, data):
        payload = feedback.clean(data)
        if not payload['error_id'] or not payload['fingerprint']:
            raise HubError(400, '反馈缺少错误编号，请重新打开反馈窗口。')
        rows = self._feedback()
        current = datetime.now(timezone.utc)
        for row in reversed(rows):
            if (row['member_id'] == member['id'] and
                    (row['payload']['error_id'] == payload['error_id'] or
                     row['payload']['fingerprint'] == payload['fingerprint']) and
                    (current - datetime.fromisoformat(row['payload']['submitted_at'])).total_seconds() < feedback.COOLDOWN):
                if row['state'] == 'sent' and row['payload']['error_id'] == payload['error_id']:
                    return row, True
                raise HubError(429, '刚刚提交过同类反馈，本次未再次提交。请五分钟后再试，或复制、导出诊断信息发给维护者。')
        payload.update(status='待排查', submitted_at=current.isoformat(), reporter=feedback.text(member['name'], 40))
        row = {'id': secrets.token_hex(16), 'member_id': member['id'], 'member_name': payload['reporter'],
               'payload': payload, 'state': 'pending'}
        self._save_feedback([*rows, row])
        return row, False

    def finish_feedback(self, row_id, state):
        with self.locked():
            rows = self._feedback()
            for row in rows:
                if row['id'] == row_id:
                    row['state'] = state
            self._save_feedback(rows)

    def _github(self):
        try:
            return clean_github(json.loads((self.data / 'github-trending.json').read_text(encoding='utf-8')))
        except (OSError, ValueError, RecursionError):
            return None

    def refresh_github(self):
        try:
            with self.locked():
                previous = self._github()
            if previous and _github_time(now()) - _github_time(previous['fetched_at']) < timedelta(hours=24):
                return
            data = clean_github(fetch_github_trending())
            with self.locked():
                atomic_write(self.data / 'github-trending.json', data)
        except ValueError as error:
            print('GitHub 最近热门更新失败：' + str(error), file=sys.stderr, flush=True)
        except OSError:
            print('GitHub 最近热门更新失败：没能读写热门名单，请检查分享站数据文件夹。', file=sys.stderr, flush=True)

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
            installs, with_mine = self._installs_state()
            installs.pop(member_id, None)
            with_mine.discard(member_id)
            changes = {p: None for p in (self.data / 'packages' / member_id).glob('*.json')}
            changes.update({self._listing_path(member_id): None,
                            self._pack_path(member_id): None,
                            self.data / 'ratings.json': {'ratings': ratings},
                            self.data / 'installs.json': self._installs_payload(installs, with_mine),
                            self.data / 'members.json': {'members': [m for m in members if m['id'] != member_id]}})
            self._save(changes)
            shutil.rmtree(self.data / 'packages' / member_id, ignore_errors=True)
            shutil.rmtree(self.data / 'deliverables' / member_id, ignore_errors=True)

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

    def _pack_path(self, member_id):
        return self.data / 'packs' / (member_id + '.json')

    def _packs(self, member_id):
        return self._read(self._pack_path(member_id), 'packs')

    def _pack_changes(self, member_id, rows, listings, skill_files=True):
        previous = self._packs(member_id)
        if not skill_files and isinstance(rows, list):
            # Older lookups omit packs they cannot show; an ordinary save must keep them.
            rows = [*rows, *(
                {'id': identity, **pack} for identity, pack in previous.items()
                if any(listings.get(n, {}).get('source', {}).get('file_storage') == 'blobs' for n in pack['skills']))]
        try:
            checked = clean_packs(rows, listings)
        except ValueError as error:
            raise HubError(400, str(error)) from None
        updated_at = now()
        return {p['id']: {**{k: v for k, v in p.items() if k != 'id'},
                         'updated_at': previous[p['id']]['updated_at']
                         if p['id'] in previous and clean_pack({'id': p['id'], **previous[p['id']]}) == p
                         else updated_at} for p in checked}

    def _public_packs(self, member):
        listings = self._listings(member['id'])
        return [{'key': 'pack:' + member['id'] + ':' + identity, 'id': identity, 'member': member,
                 **pack, 'cards': [listing_key(member['id'], listings[n]) for n in pack['skills'] if n in listings]}
                for identity, pack in self._packs(member['id']).items()]

    def _put_packs(self, member, data, skill_files=True):
        if not isinstance(data, dict) or 'packs' not in data:
            raise HubError(400, '请提供技能包列表。')
        packs = self._pack_changes(member['id'], data['packs'], self._listings(member['id']), skill_files)
        self._save({self._pack_path(member['id']): {'packs': packs}})
        return {'packs': sorted(self._public_packs(member), key=lambda p: p['updated_at'], reverse=True)}

    def _ratings(self):
        return self._read(self.data / 'ratings.json', 'ratings')

    def _installs_state(self):
        path = self.data / 'installs.json'
        if not path.exists():
            return {}, set()
        raw = json.loads(path.read_text(encoding='utf-8'))
        installs = raw['installs']
        with_mine = raw.get('with_mine', [])
        if not isinstance(installs, dict) or not isinstance(with_mine, list):
            raise ValueError
        return installs, set(with_mine)

    def _installs(self):
        return self._installs_state()[0]

    def _installs_payload(self, installs, with_mine):
        payload = {'installs': installs}
        if with_mine:
            payload['with_mine'] = sorted(with_mine)
        return payload

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

    def _catalog(self, member_id=None, skill_files=True):
        cards = {}
        members = {m['id']: {k: m[k] for k in ('id', 'name')} for m in self._members()}
        installs, with_mine = self._installs_state()
        for author_id, member in members.items():
            for row in self._listings(author_id).values():
                if not skill_files and row['source'].get('file_storage') == 'blobs':
                    continue
                key = listing_key(author_id, row)
                cards.setdefault(key, {'key': key, 'listings': [], 'ratings': []})['listings'].append(
                    {**row, 'member': member})
        for key, ratings in self._ratings().items():
            if key in cards:
                cards[key]['ratings'] = [{**r, 'member': members[m]} for m, r in ratings.items() if m in members]
        own_cards = set()
        for card in cards.values():
            card['listings'].sort(key=lambda r: r['updated_at'], reverse=True)
            card['ratings'].sort(key=lambda r: r['updated_at'], reverse=True)
            authors = {row['member']['id'] for row in card['listings']}
            if member_id in authors:
                own_cards.add(card['key'])
            card['installs'] = sum(
                (card['key'] in installs.get(mid, ())) or (mid in authors and mid not in with_mine)
                for mid in members)
        stored = set(installs.get(member_id, [])) & cards.keys()
        counted_for_me = stored if member_id in with_mine else (stored | own_cards)
        result = {'cards': sorted(cards.values(), key=lambda c: c['listings'][0]['updated_at'], reverse=True),
                'packs': sorted([p for m in members.values() for p in self._public_packs(m)],
                                key=lambda p: p['updated_at'], reverse=True),
                'my_installs': sorted(stored - own_cards),
                'my_all_installs': sorted(counted_for_me),
                'deliverables': self._public_deliverables()}
        if not skill_files:
            result['packs'] = [p for p in result['packs'] if all(
                key in cards and any(row['member']['id'] == p['member']['id'] for row in cards[key]['listings'])
                for key in p['cards'])]
        github = self._github()
        if github is not None:
            result['github'] = github
        return result

    def _put_installs(self, member_id, data, skill_files=True):
        if (not isinstance(data, dict) or not isinstance(data.get('keys'), list) or
                any(not isinstance(key, str) for key in data['keys']) or
                ('include_mine' in data and type(data['include_mine']) is not bool)):
            raise HubError(400, '上报内容须为技能卡片编号的文字列表。')
        include_mine = data.get('include_mine', False)
        cards = self._catalog()['cards']
        allowed = ({card['key'] for card in cards} if include_mine else
                   {card['key'] for card in cards if all(row['member']['id'] != member_id for row in card['listings'])})
        keys = set(data['keys']) & allowed
        installs, with_mine = self._installs_state()
        if not skill_files:
            visible = {card['key'] for card in cards if any(
                row['source'].get('file_storage') != 'blobs' for row in card['listings'])}
            keys |= (set(installs.get(member_id, [])) & allowed) - visible
        keys = sorted(keys)
        if keys:
            installs[member_id] = keys
        else:
            installs.pop(member_id, None)
        if include_mine:
            with_mine.add(member_id)
        else:
            with_mine.discard(member_id)
        atomic_write(self.data / 'installs.json', self._installs_payload(installs, with_mine))
        return {'keys': keys}

    def _put_listings(self, member_id, data, skill_files=True):
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
            if not skill_files and current.get(row['name'], {}).get('source', {}).get('file_storage') == 'blobs':
                raise HubError(409, '请更新 Skill Pilot 后再替换这个技能。')
            missing = set(row['requires']) - (set(current) | set(names))
            if missing:
                raise HubError(400, row['name'] + ' 缺少本人上架的所需技能：' + '、'.join(sorted(missing)))
        manifests = [r['source']['files'] for r in rows if isinstance(r['source'].get('files'), dict)]
        self._check_blobs(member_id, (f for manifest in manifests for f in manifest.values()))
        changes, receipts = {}, []
        collect = bool(manifests)
        updated_at = now()
        for row in rows:
            name, source = row['name'], row['source']
            path = self.data / 'packages' / member_id / (name + '.json')
            collect |= current.get(name, {}).get('source', {}).get('file_storage') == 'blobs'
            receipt = {'name': name, 'key': listing_key(member_id, row), 'updated_at': updated_at}
            if 'files' in source:
                files = source.pop('files')
                version = package_version(files)
                changes[path] = {'name': name, 'version': version, 'files': files}
                if isinstance(files, dict):
                    source.update(size=sum(f['size'] for f in files.values()), file_storage='blobs')
                else:
                    source['size'] = sum(len(base64.b64decode(f['content'])) for f in files)
                source.update(version=version, file_count=len(files))
                receipt['version'] = version
            else:
                changes[path] = None
            current[name] = {**row, 'updated_at': updated_at}
            receipts.append(receipt)
        changes[self._listing_path(member_id)] = {'listings': current}
        if 'packs' in data:
            changes[self._pack_path(member_id)] = {'packs': self._pack_changes(member_id, data['packs'], current, skill_files)}
        installs, with_mine = self._installs_state()
        if member_id in with_mine:
            merged = sorted(set(installs.get(member_id, [])) | {r['key'] for r in receipts})
            if merged != installs.get(member_id):
                installs[member_id] = merged
                changes[self.data / 'installs.json'] = self._installs_payload(installs, with_mine)
        self._save(changes)
        if collect:
            self._collect_blobs(member_id)
        return {'listings': receipts}

    def _remove_listing(self, member_id, name):
        current = self._listings(member_id)
        if name not in current:
            raise HubError(404, '这个技能还没有上架，或已经下架。')
        parents = [n for n, row in current.items() if n != name and name in row['requires']]
        if parents:
            raise HubError(409, '这些上架的技能仍需要它：' + '、'.join(sorted(parents)))
        removed = current.pop(name)
        packs = self._packs(member_id)
        for identity, pack in list(packs.items()):
            if name in pack['skills']:
                pack['skills'].remove(name)
                if len(pack['skills']) < 2:
                    del packs[identity]
                else:
                    pack['updated_at'] = now()
        self._save({self.data / 'packages' / member_id / (name + '.json'): None,
                    self._pack_path(member_id): {'packs': packs},
                    self._listing_path(member_id): {'listings': current}})
        if removed['source'].get('file_storage') == 'blobs':
            self._collect_blobs(member_id)
        return {}

    def _package(self, member_id, name, version=None):
        row = self._listings(member_id).get(name)
        if row is None or row['source']['kind'] not in {'files', 'github'} or 'version' not in row['source']:
            raise HubError(404, '这个技能没有可取的文件，或已经下架。')
        if version is not None and version != row['source']['version']:
            raise HubError(409, '作者已经上架了新版，请重新预览。')
        return json.loads((self.data / 'packages' / member_id / (name + '.json')).read_text(encoding='utf-8'))

    def _package_file(self, member_id, name, version, digest):
        files = self._package(member_id, name, version)['files']
        entry = next((row for row in files.values() if row['sha256'] == digest), None) if isinstance(files, dict) else None
        if entry is None:
            raise HubError(404, '技能里没有这个文件。')
        return (self._blob_dir(member_id) / digest).open('rb'), entry['size']

    # Deliverables: deliverables/<member>/items/<id>.json lists path → sha256; bytes live once in blobs/<sha256>.
    def _deliverable_dir(self, member_id):
        return self.data / 'deliverables' / member_id

    def _deliverables(self, member_id):
        items = self._deliverable_dir(member_id) / 'items'
        return {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in sorted(items.glob('*.json'))}

    def upload_slot(self, member_id):
        """A private temporary file next to the blobs, so the final rename stays on one disk."""
        folder = self._deliverable_dir(member_id) / 'uploads'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.blob-', dir=folder)
        return os.fdopen(fd, 'wb'), Path(name)

    def _blob_dir(self, member_id):
        return self._deliverable_dir(member_id) / 'blobs'

    def _put_blob(self, member_id, digest, temporary):
        blobs = self._blob_dir(member_id)
        blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(temporary, blobs / digest)
        return {'sha256': digest, 'size': (blobs / digest).stat().st_size}

    def _collect_blobs(self, member_id):
        # A parallel upload of the same member may lose a blob here; its list then gets 409 with
        # `missing` and the uploader sends those again, so no reservation is kept.
        blobs = self._blob_dir(member_id)
        used = {f['sha256'] for row in self._deliverables(member_id).values() for f in row['files'].values()}
        for name, row in self._listings(member_id).items():
            if row['source'].get('file_storage') == 'blobs':
                used.update(f['sha256'] for f in self._package(member_id, name)['files'].values())
        for blob in (blobs.iterdir() if blobs.is_dir() else []):
            if blob.name not in used:
                try:
                    blob.unlink(missing_ok=True)
                except PermissionError:
                    pass  # Windows may still be serving the file. Revisit on the next write.

    def _check_blobs(self, member_id, entries):
        blobs = self._blob_dir(member_id)
        sizes = {}
        for entry in entries:
            digest, size = entry['sha256'], entry['size']
            if sizes.setdefault(digest, size) != size:
                raise HubError(400, '文件的大小和上传的内容对不上，请重新上传。')
        missing = sorted(d for d in sizes if not (blobs / d).is_file())
        if missing:
            raise HubError(409, '还有文件没有上传，请先上传这些内容。', {'missing': missing})
        if any((blobs / digest).stat().st_size != size for digest, size in sizes.items()):
            raise HubError(400, '文件的大小和上传的内容对不上，请重新上传。')

    def _put_deliverable(self, member_id, identity, data):
        try:
            row = clean_deliverable(data)
        except HubError:
            raise
        except ValueError as error:
            raise HubError(400, str(error)) from None
        current = self._deliverables(member_id)
        if identity not in current and len(current) >= MAX_DELIVERABLES:
            raise HubError(413, f'每位成员最多上架 {MAX_DELIVERABLES} 个成品，请先下架不用的。')
        self._check_blobs(member_id, row['files'].values())
        row['updated_at'] = now()
        atomic_write(self._deliverable_dir(member_id) / 'items' / (identity + '.json'), row)
        self._collect_blobs(member_id)
        return {'key': 'deliverable:' + member_id + ':' + identity, 'updated_at': row['updated_at']}

    def _remove_deliverable(self, member_id, identity):
        path = self._deliverable_dir(member_id) / 'items' / (identity + '.json')
        if not path.is_file():
            raise HubError(404, '这个成品还没有上架，或已经下架。')
        path.unlink()
        self._collect_blobs(member_id)
        return {}

    def _public_deliverables(self):
        members = {m['id']: {k: m[k] for k in ('id', 'name')} for m in self._members()}
        rows = [{'key': 'deliverable:' + member_id + ':' + identity, 'id': identity, 'member': member,
                 **{k: row[k] for k in ('title', 'version', 'summary', 'skills', 'file_count', 'size', 'updated_at')},
                 'has_recipe': bool(row['recipe']), 'has_teardown': '_teardown/teardown.json' in row['files']}
                for member_id, member in members.items() for identity, row in self._deliverables(member_id).items()]
        return sorted(rows, key=lambda r: r['updated_at'], reverse=True)

    def _deliverable(self, member_id, identity):
        row = self._deliverables(member_id).get(identity)
        if row is None or not any(m['id'] == member_id for m in self._members()):
            raise HubError(404, '这个成品已经下架，请刷新商店。')
        return row

    def _deliverable_file(self, member_id, identity, path):
        try:
            deliverable_path(path)
        except ValueError:
            raise HubError(404, '成品里没有这个文件。') from None
        entry = self._deliverable(member_id, identity)['files'].get(path)
        if entry is None:
            raise HubError(404, '成品里没有这个文件。')
        blob = self._blob_dir(member_id) / entry['sha256']
        kind = DELIVERABLE_TYPES.get(Path(path).suffix.lower(), 'application/octet-stream')
        return blob.open('rb'), entry['size'], kind

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

    def _length(self, limit, too_large):
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
            raise HubError(413, too_large)
        return length

    def _receive_blob(self, digest):
        length = self._length(MAX_BLOB, f'单个文件不能超过 {MAX_BLOB // 1024 // 1024} MB。')
        store = self.server.store
        with store.locked():
            member = store._authenticate(self.headers.get('Authorization', ''))
            stream, temporary = store.upload_slot(member['id'])
        try:
            hasher = hashlib.sha256()
            with stream:
                remaining = length
                while remaining:
                    try:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                    except TimeoutError:
                        raise HubError(408, '上传等待太久，请重新上传。') from None
                    if not chunk:
                        raise HubError(400, '文件没有传完整，请重新上传。')
                    hasher.update(chunk)
                    stream.write(chunk)
                    remaining -= len(chunk)
            if hasher.hexdigest() != digest:
                raise HubError(400, '上传的内容和编号对不上，请重新上传。')
            with store.locked():
                # The member may have been removed while the file was arriving.
                store._authenticate(self.headers.get('Authorization', ''))
                return store._put_blob(member['id'], digest, temporary)
        finally:
            temporary.unlink(missing_ok=True)

    def _serve_deliverable(self, member_id, identity, path):
        store = self.server.store
        with store.locked():
            store._authenticate(self.headers.get('Authorization', ''))
            stream, size, kind = store._deliverable_file(member_id, identity, urllib.parse.unquote(path))
        self._send_file(stream, size, kind, self.headers.get('Range'))

    def _serve_package_file(self, member_id, name, version, digest):
        store = self.server.store
        with store.locked():
            store._authenticate(self.headers.get('Authorization', ''))
            stream, size = store._package_file(member_id, name, version, digest)
        self._send_file(stream, size, 'application/octet-stream', attachment=True)

    def _send_file(self, stream, size, kind, range_header=None, attachment=False):
        # Stream outside the data lock; the version was checked when opening the file.
        with stream:
            span = byte_range(range_header, size)
            start, end = span or (0, size - 1)
            self.send_response(206 if span else 200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(end - start + 1))
            if attachment:
                self.send_header('Content-Disposition', 'attachment')
            else:
                self.send_header('Accept-Ranges', 'bytes')
            if span:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            stream.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # The viewer cancelled, e.g. a video scrolled away.

    def _read_body(self, limit, too_large=None):
        length = self._length(limit, too_large or ('上架内容超过 20 MB，请分批上架。' if limit == MAX_BODY else
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
            package = re.fullmatch(r'/api/packages/([0-9a-f]{32})/(' + NAME + r')(?:/([0-9a-f]{64}))?', path)
            blob = re.fullmatch(r'/api/blobs/me/([0-9a-f]{64})', path)
            identity = '(' + DELIVERABLE_ID + ')'
            mine = re.fullmatch(r'/api/deliverables/me/' + identity, path)
            deliverable = re.fullmatch(r'/api/deliverables/([0-9a-f]{32})/' + identity, path)
            served = re.fullmatch(r'/api/deliverables/([0-9a-f]{32})/' + identity + '/(.+)', path)
            query = urllib.parse.parse_qs(address.query, keep_blank_values=True)
            if not served and ('?' in self.path or (package and package[3])) and (
                    not package or set(query) != {'version'} or len(query['version']) != 1 or
                    not re.fullmatch(r'[0-9a-f]{64}', query['version'][0])):
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            allowed = ({'POST'} if path in {'/api/join', '/api/feedback'} else
                       {'GET'} if path in {'/api/me', '/api/catalog'} or package or deliverable or served else
                       {'PUT'} if path in {'/api/listings/me', '/api/installs/me', '/api/packs/me'} or blob else
                       {'DELETE'} if listing else
                       {'PUT', 'DELETE'} if path == '/api/ratings' or mine else set())
            if not allowed:
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            if self.command not in allowed:
                raise HubError(405, '这个地址不支持这项操作。')
            if blob:
                self._json(200, self._receive_blob(blob[1]))
                return
            if package and package[3]:
                self._serve_package_file(package[1], package[2], query['version'][0], package[3])
                return
            if served:
                self._serve_deliverable(served[1], served[2], served[3])
                return
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
            data = (self._read_body(feedback.MAX_BODY, '反馈内容超过 64 KB，请缩短后重试。')
                    if path == '/api/feedback' else
                    self._read_body(MAX_INSTALL_BODY, '上报内容超过 64 KB，请缩短后重试。')
                    if path == '/api/installs/me' else
                    self._read_body(MAX_PACK_BODY, '技能包内容超过 64 KB，请缩短后重试。')
                    if path == '/api/packs/me' else
                    self._read_body(MAX_MANIFEST_BODY, '成品清单超过 1 MB，请减少文件数量。')
                    if mine and self.command == 'PUT' else None if mine else
                    self._read_body(MAX_BODY if path == '/api/listings/me' else MAX_RATING_BODY)
                    if self.command == 'PUT' or path == '/api/ratings' else None)
            store = self.server.store
            skill_files = self.headers.get(SKILL_FILE_HEADER) == '1'
            with store.locked():
                # A member may have been removed while the upload body was arriving.
                member = store._authenticate(self.headers.get('Authorization', ''))
                if path == '/api/me':
                    result = {'service': 'skill-pilot-team', 'api': 2, 'member': member,
                              'feedback_supported': True, 'feedback_configured': bool(self.server.feedback_webhook),
                              'skill_file_upload': True}
                elif path == '/api/feedback':
                    if not self.server.feedback_webhook:
                        raise HubError(503, '管理员尚未启用飞书反馈接收，请复制或导出诊断信息。')
                    row, duplicate = store._receive_feedback(member, data)
                    result = {'submitted': True, 'duplicate': duplicate}
                elif path == '/api/catalog':
                    result = store._catalog(member['id'], skill_files)
                elif path == '/api/listings/me':
                    result = store._put_listings(member['id'], data, skill_files)
                elif path == '/api/installs/me':
                    result = store._put_installs(member['id'], data, skill_files)
                elif path == '/api/packs/me':
                    result = store._put_packs(member, data, skill_files)
                elif listing:
                    result = store._remove_listing(member['id'], listing[1])
                elif package:
                    result = store._package(package[1], package[2], query.get('version', [None])[0])
                elif mine:
                    result = (store._put_deliverable(member['id'], mine[1], data) if self.command == 'PUT' else
                              store._remove_deliverable(member['id'], mine[1]))
                elif deliverable:
                    row = store._deliverable(deliverable[1], deliverable[2])
                    result = {k: row[k] for k in ('title', 'version', 'summary', 'skills', 'recipe',
                                                  'file_count', 'size', 'updated_at')}
                else:
                    result = store._rate(member, data, self.command == 'DELETE')
            if path == '/api/feedback' and not duplicate:
                # Do not hold the store lock across the external request.
                try:
                    feedback.forward(self.server.feedback_webhook, row['payload'])
                except ValueError as error:
                    store.finish_feedback(row['id'], 'failed')
                    raise HubError(502, str(error)) from None
                store.finish_feedback(row['id'], 'sent')
            self._json(200, result)
        except HubError as error:
            self._json(error.status, {'error': str(error), **error.extra})
        except (ValueError, KeyError, TypeError):
            self._json(500, {'error': '分享站数据格式不正确，请联系管理员检查或还原备份。'})
        except OSError:
            self._json(500, {'error': '分享站没能读写数据，请联系管理员检查数据文件夹。'})

    do_GET = do_PUT = do_DELETE = do_POST = do_PATCH = do_HEAD = do_OPTIONS = _handle


class HubServer(ThreadingHTTPServer):
    def __init__(self, data, host='0.0.0.0', port=8765, open=False, feedback_webhook=''):
        if not 0 <= port <= 65535:
            raise ValueError('端口应为 0 到 65535 之间的数字。')
        self.open = open
        self.feedback_webhook = feedback.webhook_url(feedback_webhook)
        self.store = HubStore(data)
        with self.store.locked():
            self.store._members()
        super().__init__((host, port), Handler)


def run(args):
    if args.hub_command == 'serve':
        with HubServer(args.data, args.host, args.port, open=args.open,
                       feedback_webhook=args.feedback_webhook or os.environ.get('SKILL_PILOT_HUB_FEEDBACK_WEBHOOK', '')) as server:
            access = ('开放加入，知道地址的同事填名字就能加入。' if args.open else
                      '只有管理员发了口令的成员能用。')
            print(f'分享站已启动，监听 {args.host}:{server.server_port}；{access}按 Ctrl+C 停止。', flush=True)
            stopped = threading.Event()
            threading.Thread(target=github_worker, args=(server.store, stopped), daemon=True).start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print('分享站已停止。')
            finally:
                stopped.set()
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
        print('已删除这位成员和他的上架、技能包、文件及评价。')
