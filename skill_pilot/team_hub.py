"""Authenticated LAN skill store. Uploaded files are data, never executed."""
import base64
import binascii
import difflib
import fnmatch
from .platform_support import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

from . import feedback, sharing, tags
from .scan import metadata

MAX_BODY = 20 * 1024 * 1024
MAX_RATING_BODY = 64 * 1024
MAX_INSTALL_BODY = 64 * 1024
MAX_PACK_BODY = 64 * 1024
MAX_JOIN_BODY = 4096
MAX_ACTIVITY_BODY = 1024
ACTIVITY_RETENTION_DAYS = 90
ACTIVITY_MAX_BYTES = 1024 * 1024
INLINE_MAX_FILES = 300
INLINE_MAX_SIZE = 5 * 1024 * 1024
MAX_FILES = 1000
SKILL_FILE_HEADER = 'X-Skill-Pilot-Skill-Files'
MAX_SIZE = 100 * 1024 * 1024
TEXT_FIELDS = ('title', 'summary', 'group', 'description', 'note')
NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]*'
MAX_REPLACES = 20
MAX_BLOB = 50 * 1024 * 1024
MAX_DELIVERABLE_SIZE = 200 * 1024 * 1024
MAX_DELIVERABLE_FILES = 2000
MAX_DELIVERABLES = 50
MAX_MANIFEST_BODY = 1024 * 1024
MAX_SUGGESTIONS_PER_SKILL = 20
MAX_SUGGESTIONS_PER_MEMBER = 20
MAX_SUGGESTIONS_TOTAL = 200
MAX_SUGGESTION_TOTAL_BYTES = 500 * 1024 * 1024
MAX_SUGGESTION_BODY = 1024 * 1024
MAX_SUGGESTION_MESSAGE = 500
MAX_SUGGESTION_REVIEW_NOTE = 300
MAX_DIFF_FILE_BYTES = 256 * 1024
MAX_DIFF_TOTAL_CHARS = 120_000
MAX_ORPHAN_BLOBS_BYTES = 200 * 1024 * 1024
ORPHAN_BLOB_TTL_SECONDS = 3600
SECRET_NAMES = ('.env', '.env.*', '*.pem', '*.key', '*.p12', 'id_rsa*', 'id_ed25519*',
                '.npmrc', '.pypirc', 'credentials*')
SECRET_CONTENT = re.compile(
    r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|'
    r'(?:sk-|ghp_|github_pat_|AKIA|xoxb-)[A-Za-z0-9_-]{16,}|'
    r'(?:api[_-]?key|token|secret|password)[\"\']?\s*[:=]\s*[\"\']?[^\s\"\',;\x00-\x1f]{16,}', re.I)
OWN_SKILL = '这是你自己上架的技能，请直接使用「上架新版」更新。'
FILES_ONLY = '只能给成员上架的文件版技能提建议。公开仓库和飞书官方技能请向来源方反馈。'
NO_CHANGES = '本机技能与线上最新版本内容完全一致，没有可提交的改动。'
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
GITHUB_REFRESH_HOURS = 24 * 7
GITHUB_REFRESH_MIN_HOURS = 1
GITHUB_REFRESH_MAX_HOURS = 24 * 30


def now():
    return datetime.now(timezone.utc).isoformat()


def _activity_time(value):
    if (isinstance(value, str) and re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value)):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            pass
    raise ValueError('活跃记录的时间格式不正确。')


def _valid_activity_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


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


def clean_github_refresh_hours(value, *, allow_blank=True):
    message = f'GitHub 最近热门的刷新间隔须为 {GITHUB_REFRESH_MIN_HOURS} 到 {GITHUB_REFRESH_MAX_HOURS} 之间的整数小时。'
    if value is None:
        return GITHUB_REFRESH_HOURS
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            if allow_blank:
                return GITHUB_REFRESH_HOURS
            raise ValueError(message)
        if not re.fullmatch(r'\d+', value):
            raise ValueError(message)
        trimmed = value.lstrip('0')
        if not trimmed or len(trimmed) > 3:
            raise ValueError(message)
        hours = int(trimmed)
    elif isinstance(value, int):
        hours = value
    else:
        raise ValueError(message)
    if not GITHUB_REFRESH_MIN_HOURS <= hours <= GITHUB_REFRESH_MAX_HOURS:
        raise ValueError(message)
    return hours


def github_worker(store, stopped):
    while not stopped.is_set():
        store.refresh_github()
        stopped.wait(3600)


def short_text(value, label):
    if (not isinstance(value, str) or len(value) > 200 or
            any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in value)):
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


def skill_manifest(files):
    """Normalize an already validated inline package or a file manifest for comparison."""
    if isinstance(files, dict):
        return files
    result = {}
    for row in files:
        raw = base64.b64decode(row['content'])
        result[row['path']] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                               'executable': row['executable']}
    return result


def suggestion_text(value, *, review=False):
    limit = MAX_SUGGESTION_REVIEW_NOTE if review else MAX_SUGGESTION_MESSAGE
    if (not isinstance(value, str) or any(c != '\n' and unicodedata.category(c) in {'Cc', 'Cf', 'Cs'}
                                          for c in value) or len(value.strip()) > limit):
        raise HubError(400, f'说明最多 {limit} 字，请删除看不见的字符。')
    if not review and not value.strip():
        raise HubError(400, '请填写改进说明。')
    return value.strip()


def _clean_description(value):
    if not isinstance(value, str):
        return ''
    cleaned = value.replace('\t', ' ')
    cleaned = ''.join(char for char in cleaned
                      if unicodedata.category(char) not in {'Cc', 'Cf', 'Zl', 'Zp'} or char == '\n')
    return cleaned.strip()[:1500].rstrip()


def _sanitize_diff_text(text):
    out = []
    for char in text:
        if char in '\n\t':
            out.append(char)
        elif unicodedata.category(char) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}:
            out.append(f'\\x{ord(char):02x}' if ord(char) < 256 else f'\\u{ord(char):04x}')
        else:
            out.append(char)
    return ''.join(out)


def suggestion_diff(old, new, read_old, read_new):
    from .updates import SCRIPT_SUFFIXES
    summary = {'added': [], 'modified': [], 'removed': [], 'unchanged_count': 0, 'has_scripts': False}
    files = []
    remaining = MAX_DIFF_TOTAL_CHARS
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        if before == after:
            summary['unchanged_count'] += 1
            continue
        status = 'added' if before is None else 'removed' if after is None else 'modified'
        summary[status].append(path)
        script = ('scripts' in path.split('/') or Path(path).suffix.lower() in SCRIPT_SUFFIXES or
                  any(row['executable'] for row in (before, after) if row))
        truncated = binary = False
        if not remaining or any(row['size'] > MAX_DIFF_FILE_BYTES for row in (before, after) if row):
            preview, truncated = '内容较多，已省略文字预览。', True
        else:
            try:
                a = read_old(path) if before else b''
                b = read_new(path) if after else b''
                script |= a.startswith(b'#!') or b.startswith(b'#!')
                if b'\0' in a or b'\0' in b:
                    binary, preview = True, '这是非文本文件，无法显示文字改动。'
                else:
                    left, right = a.decode().splitlines(keepends=True), b.decode().splitlines(keepends=True)
                    # Bound the line matcher's work as well as the rendered output.
                    truncated = max(len(left), len(right)) > 2000
                    lines = difflib.unified_diff(left[:2000], right[:2000], path, path)
                    preview = _sanitize_diff_text(''.join(line if line.endswith('\n') else line + '\n' for line in lines))
            except UnicodeDecodeError:
                binary, preview = True, '这是非文本文件，无法显示文字改动。'
            except FileNotFoundError:
                preview, truncated = '文件内容已清理，仅保留改动记录。', True
        truncated |= len(preview) > remaining
        preview = preview[:remaining]
        remaining -= len(preview)
        summary['has_scripts'] |= script
        files.append({'path': path, 'status': status, 'is_script': script,
                      'mode_changed': bool(before and after and before['executable'] != after['executable']),
                      'old_size': before['size'] if before else None, 'new_size': after['size'] if after else None,
                      'binary': binary, 'truncated': truncated, 'preview': preview})
    return {'summary': summary, 'files': files}


def clean_replaces(value, own_name=None):
    if not isinstance(value, list):
        raise ValueError('替代的旧技能名称须放在列表中。')
    cleaned = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError('替代的旧技能名称须是文字。')
        name = item.strip()
        if not name or len(name) > 64 or not re.fullmatch(NAME, name):
            raise ValueError('替代的旧技能名称只能用字母、数字、点、横线或下划线，最多 64 个字符。')
        if own_name and name == own_name:
            raise ValueError('替代的旧技能名称不能包含当前技能自己。')
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
    if len(cleaned) > MAX_REPLACES:
        raise ValueError(f'每个技能最多标记 {MAX_REPLACES} 个替代的旧技能名称。')
    return cleaned


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
    if 'replaces' in row:
        replaces = clean_replaces(row['replaces'], own_name=result['name'])
        if replaces:
            result['replaces'] = replaces
    if 'replacement_note' in row:
        replacement_note = short_text(row['replacement_note'], '替代说明').strip()
        if replacement_note:
            if not result.get('replaces'):
                raise ValueError('填写替代说明前，请先填写替代的旧技能名称。')
            result['replacement_note'] = replacement_note
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
        elif kind == 'files' and source.get('preserve_files') is True and 'files' not in source:
            result['source']['preserve_files'] = True
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
    def __init__(self, data, github_refresh_hours=GITHUB_REFRESH_HOURS):
        self.data = Path(data).expanduser().resolve()
        self.github_refresh_hours = clean_github_refresh_hours(github_refresh_hours)
        self._in_flight_feedback = set()

    @contextmanager
    def locked(self):
        # CLI commands and HTTP threads use the same lock, including after restart.
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.data / '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _feedback_attachment_dir(self):
        return self.data / 'feedback-attachments'

    def _attachment_disk_sizes(self):
        folder = self._feedback_attachment_dir()
        sizes = {}
        try:
            entries = list(folder.iterdir())
        except FileNotFoundError:
            return sizes
        except OSError:
            raise HubError(507, '分享站暂时无法确认截图存储空间，本次未提交。请联系管理员检查数据目录，或复制、导出纯文本诊断信息。') from None
        for entry in entries:
            try:
                st = entry.stat()
            except FileNotFoundError:
                continue
            except OSError:
                raise HubError(507, '分享站暂时无法确认截图存储空间，本次未提交。请联系管理员检查数据目录，或复制、导出纯文本诊断信息。') from None
            if stat.S_ISREG(st.st_mode):
                sizes[entry.name] = st.st_size
        return sizes

    def _collect_feedback_attachments(self, rows):
        folder = self._feedback_attachment_dir()
        try:
            if not folder.is_dir():
                return
            entries = list(folder.iterdir())
        except OSError:
            return
        used = {
            att['file']
            for row in rows
            for att in row.get('attachments', ())
            if isinstance(att, dict) and not att.get('evicted') and isinstance(att.get('file'), str)
        }
        for entry in entries:
            if entry.name not in used:
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass  # Locked or temporarily unremovable; accounted for and retried later.

    def _cleanup_feedback_orphans(self):
        try:
            rows = self._feedback()
        except (HubError, OSError, ValueError):
            return
        # Mark any leftover 'pending' rows from a previously crashed hub process as interrupted ('failed')
        # so they never permanently lock up the in-flight protection quota after restart.
        changed = False
        for row in rows:
            if isinstance(row, dict) and row.get('state') == 'pending' and row.get('id') not in self._in_flight_feedback:
                row['state'] = 'failed'
                changed = True
        if changed:
            try:
                atomic_write(self.data / 'feedback.json', rows)
            except OSError:
                return
        self._collect_feedback_attachments(rows)

    def _attachment_file_matches(self, path, item):
        try:
            st = path.stat()
        except FileNotFoundError:
            return False
        except OSError:
            raise HubError(507, '分享站暂时无法确认截图存储空间，本次未提交。请联系管理员检查数据目录，或复制、导出纯文本诊断信息。') from None
        if not stat.S_ISREG(st.st_mode) or st.st_size != item['size']:
            return False
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
        except FileNotFoundError:
            return False
        except OSError:
            raise HubError(507, '分享站暂时无法确认截图存储空间，本次未提交。请联系管理员检查数据目录，或复制、导出纯文本诊断信息。') from None

    def _write_feedback_attachments(self, attachments, protected_files=()):
        if not attachments:
            return []
        folder = self._feedback_attachment_dir()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        protected = set(protected_files)
        created = []
        try:
            for item in attachments:
                target = folder / f"{item['sha256']}.{item['ext']}"
                if self._attachment_file_matches(target, item):
                    continue
                fd, temporary = tempfile.mkstemp(prefix='.att-', dir=folder)
                temp_path = Path(temporary)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(item['data'])
                    os.replace(temp_path, target)
                    created.append(target)
                finally:
                    temp_path.unlink(missing_ok=True)
            return created
        except OSError:
            for path in created:
                if path.name not in protected:
                    path.unlink(missing_ok=True)
            raise

    def _feedback(self):
        path = self.data / 'feedback.json'
        if not path.exists():
            return []
        rows = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(rows, list):
            raise HubError(500, '反馈备份没能读懂，请联系管理员检查。')
        return rows

    def _trim_feedback_rows(self, rows, protected=(), reject_if_full=False):
        rows = list(rows)
        protected_ids = set(self._in_flight_feedback) | set(protected)
        def over_limit():
            return (len(rows) > feedback.MAX_RECORDS or
                    len((json.dumps(rows, ensure_ascii=False, indent=2) + '\n').replace('\n', os.linesep).encode()) > feedback.MAX_BYTES)
        while rows and over_limit():
            idx = next((i for i, item in enumerate(rows) if item.get('id') not in protected_ids), None)
            if idx is None:
                if reject_if_full:
                    raise HubError(507, '当前有其他反馈正在转发且反馈留底空间已满，请稍后重试，或复制、导出纯文本诊断信息。')
                break
            rows.pop(idx)
        return rows

    def _save_feedback(self, rows):
        # Never delete attachment files before feedback.json is atomically committed.
        rows = self._trim_feedback_rows(rows)
        atomic_write(self.data / 'feedback.json', rows)
        self._collect_feedback_attachments(rows)
        return rows

    def _evict_attachments_for_budget(self, committed_rows, attachments):
        if not attachments:
            return committed_rows
        # First clean up only files already unreferenced by the committed feedback.json.
        self._collect_feedback_attachments(committed_rows)
        folder = self._feedback_attachment_dir()
        unique_items = {f"{item['sha256']}.{item['ext']}": item for item in attachments}
        in_flight_files = {
            att['file']
            for item in committed_rows
            if item.get('id') in self._in_flight_feedback
            for att in item.get('attachments', ())
            if isinstance(att, dict) and not att.get('evicted') and isinstance(att.get('file'), str)
        }
        while True:
            disk_sizes = self._attachment_disk_sizes()
            # Reuse an on-disk file ONLY if its size and SHA-256 content match; otherwise account for
            # the full peak bytes of the new temp file alongside existing on-disk bytes during replace.
            added_bytes = sum(
                item['size'] for name, item in unique_items.items()
                if not (name in disk_sizes and self._attachment_file_matches(folder / name, item))
            )
            if sum(disk_sizes.values()) + added_bytes <= feedback.MAX_ATTACHMENT_STORE_BYTES:
                return committed_rows
            target = next((
                item for item in committed_rows
                if item.get('id') not in self._in_flight_feedback and any(
                    isinstance(att, dict) and not att.get('evicted') and
                    isinstance(att.get('file'), str) and att['file'] not in in_flight_files
                    for att in item.get('attachments', ())
                )
            ), None)
            if target is None:
                if in_flight_files:
                    raise HubError(507, '当前有其他带图反馈正在转发，截图存储空间紧张。请稍后重试，或复制、导出纯文本诊断信息。')
                raise HubError(507, '分享站截图留底空间已满，无法保存新截图。请联系管理员清理磁盘后重试，或复制、导出纯文本诊断信息。')
            for att in target.get('attachments', ()):
                if isinstance(att, dict):
                    att['evicted'] = True
            # Commit eviction to feedback.json BEFORE deleting the unreferenced files,
            # then re-measure real disk usage so failed deletes cannot bypass the quota.
            atomic_write(self.data / 'feedback.json', committed_rows)
            self._collect_feedback_attachments(committed_rows)

    def _receive_feedback(self, member, data, attachments=None):
        payload = feedback.clean(data)
        if not payload['error_id'] or not payload['fingerprint']:
            raise HubError(400, '反馈缺少错误编号，请重新打开反馈窗口。')
        if attachments is None:
            try:
                attachments = feedback.parse_attachments(data.get('attachments'))
            except feedback.AttachmentTooLargeError as error:
                raise HubError(413, str(error)) from None
            except feedback.AttachmentError as error:
                raise HubError(400, str(error)) from None
        rows = self._feedback()
        current = datetime.now(timezone.utc)
        incoming_ids = [item['sha256'] for item in attachments]
        # Exact error_id in-flight / idempotency / conflict checks hold regardless of elapsed time.
        for row in reversed(rows):
            if row['member_id'] != member['id'] or row['payload']['error_id'] != payload['error_id']:
                continue
            if row.get('id') in self._in_flight_feedback:
                raise HubError(429, '这条反馈正在转发中，请稍候，不要重复提交。')
            if row['state'] == 'sent':
                # Compare against the original submitted attachment digests even if files were later evicted.
                existing_ids = [
                    att['sha256'] for att in row.get('attachments', ())
                    if isinstance(att, dict) and isinstance(att.get('sha256'), str)
                ]
                if existing_ids == incoming_ids:
                    return row, True
                raise HubError(409, '这条错误编号之前已提交，且截图内容与本次不同，本次未保存新截图。请改用主动反馈补充截图，或复制、导出诊断信息发给维护者。')
            if (current - datetime.fromisoformat(row['payload']['submitted_at'])).total_seconds() < feedback.COOLDOWN:
                raise HubError(429, '刚刚提交过同类反馈，本次未再次提交。请五分钟后再试，或复制、导出诊断信息发给维护者。')
        # Same-fingerprint rate limit (different error_id) applies within COOLDOWN.
        for row in reversed(rows):
            if (row['member_id'] == member['id'] and
                    row['payload']['fingerprint'] == payload['fingerprint'] and
                    (current - datetime.fromisoformat(row['payload']['submitted_at'])).total_seconds() < feedback.COOLDOWN):
                raise HubError(429, '刚刚提交过同类反馈，本次未再次提交。请五分钟后再试，或复制、导出诊断信息发给维护者。')
        row_id = secrets.token_hex(16)
        stored_meta = [
            {'id': item['sha256'], 'sha256': item['sha256'],
             'file': f"{item['sha256']}.{item['ext']}",
             'content_type': item['content_type'], 'ext': item['ext'],
             'label': item['label'], 'size': item['size']}
            for item in attachments
        ]
        payload['attachment_count'] = str(len(stored_meta))
        payload['attachments_summary'] = feedback.summarize_attachments(stored_meta, row_id=row_id)
        payload.update(status='待排查', submitted_at=current.isoformat(), reporter=feedback.text(member['name'], 40))
        row = {'id': row_id, 'member_id': member['id'], 'member_name': payload['reporter'],
               'payload': payload, 'state': 'pending'}
        if stored_meta:
            row['attachments'] = stored_meta
        # Check text backup capacity BEFORE evicting screenshots or writing files, protecting in-flight rows.
        self._trim_feedback_rows([*rows, row], protected=(row_id,), reject_if_full=True)
        rows = self._evict_attachments_for_budget(rows, attachments)
        next_rows = self._trim_feedback_rows([*rows, row], protected=(row_id,), reject_if_full=True)
        existing_used = {
            att['file']
            for item in rows
            for att in item.get('attachments', ())
            if isinstance(att, dict) and not att.get('evicted') and isinstance(att.get('file'), str)
        }
        created = self._write_feedback_attachments(attachments, protected_files=existing_used)
        try:
            atomic_write(self.data / 'feedback.json', next_rows)
        except OSError:
            for path in created:
                if path.name not in existing_used:
                    path.unlink(missing_ok=True)
            raise
        self._in_flight_feedback.add(row_id)
        # Commit succeeded; cleanup of obsolete files is best-effort and never rolls back new files.
        self._collect_feedback_attachments(next_rows)
        return row, False

    def _feedback_attachment_file(self, member_id, row_id, digest):
        if not re.fullmatch(r'[0-9a-f]{32}', row_id) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise HubError(404, '没有找到这张反馈截图，或已被清理。')
        row = next((item for item in self._feedback()
                    if item.get('id') == row_id and item.get('member_id') == member_id), None)
        if row is None:
            raise HubError(404, '没有找到这张反馈截图，或已被清理。')
        att = next((item for item in row.get('attachments', ())
                    if isinstance(item, dict) and item.get('sha256') == digest and not item.get('evicted')), None)
        if att is None:
            raise HubError(404, '没有找到这张反馈截图，或已被清理。')
        ext = att.get('ext', '')
        kind = att.get('content_type', '')
        if ext not in {'png', 'jpg'} or kind not in feedback.IMAGE_TYPES or feedback.IMAGE_TYPES[kind]['ext'] != ext:
            raise HubError(404, '没有找到这张反馈截图，或已被清理。')
        path = self._feedback_attachment_dir() / f'{digest}.{ext}'
        if not path.is_file():
            raise HubError(404, '没有找到这张反馈截图，或已被清理。')
        try:
            with path.open('rb') as stream:
                raw = stream.read(feedback.MAX_ATTACHMENT_BYTES + 1)
        except FileNotFoundError:
            raise HubError(404, '没有找到这张反馈截图，或已被清理。') from None
        if len(raw) > feedback.MAX_ATTACHMENT_BYTES:
            raise HubError(500, '截图留底文件异常，请联系管理员检查。')
        return raw, kind, f'feedback-{row_id[:8]}-{digest[:8]}.{ext}'

    def finish_feedback(self, row_id, state):
        with self.locked():
            self._in_flight_feedback.discard(row_id)
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

    def refresh_github(self, refresh_hours=None):
        hours = self.github_refresh_hours if refresh_hours is None else clean_github_refresh_hours(refresh_hours)
        try:
            with self.locked():
                previous = self._github()
            if previous and _github_time(now()) - _github_time(previous['fetched_at']) < timedelta(hours=hours):
                return
            data = clean_github(fetch_github_trending())
            with self.locked():
                if not data['repos']:
                    current = self._github()
                    message = ('这次没有查到热门仓库，继续保留上一份名单。' if current and current['repos']
                               else '这次没有查到热门仓库，稍后会重试。')
                    print('GitHub 最近热门更新失败：' + message, file=sys.stderr, flush=True)
                    return
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
            activity_path = self.data / 'activity.json'
            activity_skipped = False
            if activity_path.exists():
                try:
                    activity = self._activity()
                except (HubError, OSError):
                    activity_skipped = True
                else:
                    if member_id in activity:
                        activity.pop(member_id, None)
                        changes[activity_path] = {'activity': activity}
            affected = set()
            for suggestion in self._suggestions():
                if member_id in (suggestion['target_member_id'], suggestion['author_id']):
                    changes[self._suggestion_path(suggestion['id'])] = None
                    if suggestion['status'] == 'pending' and suggestion['author_id'] != member_id:
                        affected.add(suggestion['author_id'])
            self._save(changes)
            for author in affected:
                self._collect_blobs(author)
            shutil.rmtree(self.data / 'packages' / member_id, ignore_errors=True)
            shutil.rmtree(self.data / 'deliverables' / member_id, ignore_errors=True)
            if activity_skipped:
                print('提示：活跃记录文件损坏或无法读取，已保留原文件，请管理员检查 activity.json。',
                      file=sys.stderr, flush=True)
            return {'activity_cleaned': not activity_skipped}

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

    def _activity(self):
        path = self.data / 'activity.json'
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or not isinstance(raw.get('activity'), dict):
                raise ValueError
            result = {}
            for member_id, entry in raw['activity'].items():
                if (not isinstance(member_id, str) or not re.fullmatch(r'[0-9a-f]{32}', member_id) or
                        not isinstance(entry, dict) or not isinstance(entry.get('days'), dict)):
                    raise ValueError
                last_active_at = entry.get('last_active_at')
                if last_active_at is not None:
                    _activity_time(last_active_at)
                days = {}
                for day, count in entry['days'].items():
                    if not _valid_activity_date(day) or type(count) is not int or count < 1:
                        raise ValueError
                    days[day] = count
                if days:
                    result[member_id] = {'last_active_at': last_active_at, 'days': dict(sorted(days.items()))}
            return result
        except (ValueError, KeyError, TypeError, RecursionError):
            raise HubError(500, '活跃记录没能读懂，请联系管理员检查或还原备份。') from None

    def _prune_activity(self, activity, current_dt, active_member_ids=None, protect_member=None):
        cutoff = (current_dt.date() - timedelta(days=ACTIVITY_RETENTION_DAYS - 1)).isoformat()
        pruned = {}
        for member_id, entry in activity.items():
            if active_member_ids is not None and member_id not in active_member_ids:
                continue
            days = {day: count for day, count in entry['days'].items() if day >= cutoff}
            if not days:
                continue
            pruned[member_id] = {'last_active_at': entry.get('last_active_at'), 'days': dict(sorted(days.items()))}

        def encoded_size():
            text = json.dumps({'activity': pruned}, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
            return len(text.replace('\n', os.linesep).encode('utf-8'))

        while pruned and encoded_size() > ACTIVITY_MAX_BYTES:
            all_days = sorted({day for entry in pruned.values() for day in entry['days']})
            if len(all_days) > 1:
                oldest_day = all_days[0]
                for member_id in list(pruned):
                    pruned[member_id]['days'].pop(oldest_day, None)
                    if not pruned[member_id]['days']:
                        del pruned[member_id]
                continue
            candidates = [mid for mid in pruned if mid != protect_member] or list(pruned)
            victim = min(candidates, key=lambda mid: (
                _activity_time(pruned[mid]['last_active_at']).timestamp() if pruned[mid].get('last_active_at') else -1.0,
                mid,
            ))
            del pruned[victim]
        return pruned

    def _record_activity(self, member_id):
        timestamp = now()
        current_dt = _activity_time(timestamp)
        today = current_dt.date().isoformat()
        activity = self._activity()
        previous = activity.get(member_id)
        days = dict(previous['days']) if previous else {}
        days[today] = days.get(today, 0) + 1
        previous_last = previous.get('last_active_at') if previous else None
        if previous_last is not None and _activity_time(previous_last) > current_dt:
            latest = previous_last
        else:
            latest = timestamp
        activity[member_id] = {'last_active_at': latest, 'days': dict(sorted(days.items()))}
        active_ids = {m['id'] for m in self._members()}
        activity = self._prune_activity(activity, current_dt, active_member_ids=active_ids, protect_member=member_id)
        atomic_write(self.data / 'activity.json', {'activity': activity})
        saved = activity.get(member_id) or {'days': {}, 'last_active_at': latest}
        return {
            'recorded': True,
            'date': today,
            'opens_today': saved['days'].get(today, 0),
            'last_active_at': saved.get('last_active_at'),
        }

    def _cleanup_expired_activity(self):
        path = self.data / 'activity.json'
        if not path.exists():
            return
        try:
            activity = self._activity()
            current_dt = _activity_time(now())
            active_ids = {m['id'] for m in self._members()}
            pruned = self._prune_activity(activity, current_dt, active_member_ids=active_ids)
            if pruned != activity:
                atomic_write(path, {'activity': pruned})
        except (HubError, OSError, ValueError):
            return

    def _activity_summary(self, days=30):
        if type(days) is not int or not 1 <= days <= ACTIVITY_RETENTION_DAYS:
            raise HubError(400, f'统计天数须为 1 到 {ACTIVITY_RETENTION_DAYS} 之间的整数。')
        timestamp = now()
        current_dt = _activity_time(timestamp)
        today = current_dt.date()
        today_str = today.isoformat()
        retention_cutoff = (today - timedelta(days=ACTIVITY_RETENTION_DAYS - 1)).isoformat()
        window_cutoff = (today - timedelta(days=days - 1)).isoformat()
        members = self._members()
        activity = self._activity()
        rows = []
        for member in members:
            record = activity.get(member['id'])
            kept_days = ({d: c for d, c in record['days'].items() if retention_cutoff <= d <= today_str}
                         if record else {})
            daily = {d: c for d, c in sorted(kept_days.items()) if window_cutoff <= d <= today_str}
            last_active_at = record.get('last_active_at') if kept_days else None
            rows.append({
                'id': member['id'],
                'name': member['name'],
                'active_days': len(daily),
                'total_opens': sum(daily.values()),
                'opens_today': daily.get(today_str, 0),
                'last_active_at': last_active_at,
                'daily': daily,
            })
        rows.sort(key=lambda r: (
            0 if r['last_active_at'] is not None else 1,
            -_activity_time(r['last_active_at']).timestamp() if r['last_active_at'] is not None else 0.0,
            -r['total_opens'],
            -r['active_days'],
            r['name'],
            r['id'],
        ))
        return {
            'generated_at': timestamp,
            'window_days': days,
            'retention_days': ACTIVITY_RETENTION_DAYS,
            'summary': {
                'total_members': len(rows),
                'active_members': sum(1 for r in rows if r['active_days'] > 0),
                'active_today': sum(1 for r in rows if r['opens_today'] > 0),
                'total_opens': sum(r['total_opens'] for r in rows),
            },
            'members': rows,
        }

    def activity_summary(self, days=30):
        with self.locked():
            return self._activity_summary(days=days)

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
        except (OSError, UnicodeEncodeError):
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
                public_row = {k: v for k, v in row.items() if k != 'version_revisions'}
                cards.setdefault(key, {'key': key, 'listings': [], 'ratings': []})['listings'].append(
                    {'created_at': row['updated_at'], 'revision': 1, **public_row, 'member': member})
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
        if member_id:
            incoming, outgoing = {}, 0
            for suggestion in self._suggestions():
                if suggestion['status'] != 'pending':
                    continue
                if suggestion['target_member_id'] == member_id:
                    name = suggestion['skill_name']
                    incoming[name] = incoming.get(name, 0) + 1
                if suggestion['author_id'] == member_id:
                    outgoing += 1
            result['suggestions_summary'] = {'incoming_total': sum(incoming.values()),
                                              'incoming_by_skill': incoming, 'outgoing_total': outgoing}
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
        # Preserve existing replaces/replacement_note when an older client omits them.
        for raw_row, row in zip(data['listings'], rows):
            previous = current.get(row['name'], {})
            if 'replaces' not in raw_row and previous.get('replaces'):
                row['replaces'] = list(previous['replaces'])
                if 'replacement_note' not in raw_row and previous.get('replacement_note'):
                    row['replacement_note'] = previous['replacement_note']
                elif 'replacement_note' in raw_row:
                    try:
                        note_val = short_text(raw_row['replacement_note'], '替代说明').strip()
                    except ValueError as error:
                        raise HubError(400, str(error)) from None
                    if note_val:
                        row['replacement_note'] = note_val
                    else:
                        row.pop('replacement_note', None)
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
            previous = current.get(name, {})
            path = self.data / 'packages' / member_id / (name + '.json')
            preserve = bool(source.get('preserve_files'))
            if preserve:
                if previous.get('source', {}).get('kind') != 'files':
                    raise HubError(400, '请附带完整的技能文件后再上架。')
                row['source'] = dict(previous['source'])
                source = row['source']
                receipt = {'name': name, 'key': listing_key(member_id, row), 'updated_at': updated_at}
                if source.get('version'):
                    receipt['version'] = source['version']
            else:
                collect |= previous.get('source', {}).get('file_storage') == 'blobs'
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
            revision = previous.get('revision', 1)
            if not preserve and previous and previous['source'].get('version') != source.get('version'):
                revision += 1
            ver_revs = dict(previous.get('version_revisions') or {})
            if previous and previous.get('source', {}).get('version'):
                ver_revs.setdefault(previous['source']['version'], previous.get('revision', 1))
            if source.get('version'):
                ver_revs[source['version']] = revision
            if len(ver_revs) > 30:
                ver_revs = dict(list(ver_revs.items())[-30:])
            extra = {'published_via': previous['published_via']} if (preserve and previous.get('published_via')) else {}
            if ver_revs:
                extra['version_revisions'] = ver_revs
            current[name] = {**row, **extra, 'updated_at': updated_at, 'revision': revision,
                             'created_at': previous.get('created_at', previous.get('updated_at', updated_at))}
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

    def _remove_listing(self, member_id, name, data=None):
        current = self._listings(member_id)
        if name not in current:
            raise HubError(404, '这个技能还没有上架，或已经下架。')
        parents = [n for n, row in current.items() if n != name and name in row['requires']]
        if parents:
            raise HubError(409, '这些上架的技能仍需要它：' + '、'.join(sorted(parents)))
        if data is not None:
            replaced_by = data.get('replaced_by', '')
            replacement_note = data.get('replacement_note', '')
            if not isinstance(replaced_by, str):
                raise HubError(400, '推荐替代的新技能名称格式不正确。')
            replaced_by = replaced_by.strip()
            if replacement_note != '':
                try:
                    replacement_note = short_text(replacement_note, '替代说明').strip()
                except ValueError as error:
                    raise HubError(400, str(error)) from None
            if replaced_by:
                if replaced_by == name or replaced_by not in current:
                    raise HubError(400, '请选择你已上架的另一个技能作为推荐替代技能。')
                try:
                    merged = clean_replaces([*current[replaced_by].get('replaces', []), name], own_name=replaced_by)
                except ValueError as error:
                    raise HubError(400, str(error)) from None
                current[replaced_by]['replaces'] = merged
                if replacement_note:
                    current[replaced_by]['replacement_note'] = replacement_note
            elif replacement_note:
                linked = [row for other, row in current.items() if other != name and name in row.get('replaces', [])]
                if not linked:
                    raise HubError(400, '填写替代说明时，请同时指定推荐替代的新技能。')
                for row in linked:
                    row['replacement_note'] = replacement_note
        removed = current.pop(name)
        packs = self._packs(member_id)
        for identity, pack in list(packs.items()):
            if name in pack['skills']:
                pack['skills'].remove(name)
                if len(pack['skills']) < 2:
                    del packs[identity]
                else:
                    pack['updated_at'] = now()
        changes = {self.data / 'packages' / member_id / (name + '.json'): None,
                   self._pack_path(member_id): {'packs': packs},
                   self._listing_path(member_id): {'listings': current}}
        affected = set()
        for row in self._suggestions():
            if (row['target_member_id'], row['skill_name'], row['status']) == (member_id, name, 'pending'):
                self._finish_suggestion(row, 'rejected', '原作者已下架此技能。')
                changes[self._suggestion_path(row['id'])] = row
                affected.add(row['author_id'])
        self._save(changes)
        for author in affected:
            self._collect_blobs(author)
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

    def _inflight_bytes(self):
        return getattr(self, '_inflight_upload_bytes', 0)

    def _inflight_by_member(self, member_id):
        return getattr(self, '_inflight_member_bytes', {}).get(member_id, 0)

    def _reserve_upload(self, member_id, incoming_bytes):
        if incoming_bytes <= 0:
            return
        member_orphan = self._orphan_blob_bytes(member_id) + self._inflight_by_member(member_id)
        if member_orphan + incoming_bytes > MAX_ORPHAN_BLOBS_BYTES:
            self._collect_blobs(member_id, min_age=ORPHAN_BLOB_TTL_SECONDS)
            member_orphan = self._orphan_blob_bytes(member_id) + self._inflight_by_member(member_id)
        if member_orphan + incoming_bytes > MAX_ORPHAN_BLOBS_BYTES:
            raise HubError(507, '当前成员未完成的临时上传文件过多，请稍后重试。')
        total_orphan = self._total_orphan_blob_bytes() + self._inflight_bytes()
        if total_orphan + incoming_bytes > MAX_SUGGESTION_TOTAL_BYTES:
            for member in self._members():
                self._collect_blobs(member['id'], min_age=ORPHAN_BLOB_TTL_SECONDS)
            total_orphan = self._total_orphan_blob_bytes() + self._inflight_bytes()
        if total_orphan + incoming_bytes > MAX_SUGGESTION_TOTAL_BYTES:
            raise HubError(507, '分享站临时上传空间已满，请稍后重试。')
        self._inflight_upload_bytes = self._inflight_bytes() + incoming_bytes
        per = getattr(self, '_inflight_member_bytes', None)
        if per is None:
            per = {}
            self._inflight_member_bytes = per
        per[member_id] = per.get(member_id, 0) + incoming_bytes

    def release_upload(self, member_id, reserved_bytes):
        if reserved_bytes <= 0:
            return
        self._inflight_upload_bytes = max(0, self._inflight_bytes() - reserved_bytes)
        per = getattr(self, '_inflight_member_bytes', None)
        if per and member_id in per:
            remaining = per[member_id] - reserved_bytes
            if remaining > 0:
                per[member_id] = remaining
            else:
                per.pop(member_id, None)

    def upload_slot(self, member_id, incoming_bytes=0):
        """A private temporary file next to the blobs, so the final rename stays on one disk."""
        if incoming_bytes > 0:
            self._reserve_upload(member_id, incoming_bytes)
        folder = self._deliverable_dir(member_id) / 'uploads'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd, name = tempfile.mkstemp(prefix='.blob-', dir=folder)
            return os.fdopen(fd, 'wb'), Path(name)
        except OSError:
            if incoming_bytes > 0:
                self.release_upload(member_id, incoming_bytes)
            raise

    def _blob_dir(self, member_id):
        return self._deliverable_dir(member_id) / 'blobs'

    def _used_blobs(self, member_id):
        used = {f['sha256'] for row in self._deliverables(member_id).values() for f in row['files'].values()}
        for suggestion in self._suggestions():
            if suggestion['author_id'] == member_id and suggestion['status'] == 'pending':
                used.update(f['sha256'] for f in suggestion['files'].values())
        for name, row in self._listings(member_id).items():
            if row['source'].get('file_storage') == 'blobs':
                used.update(f['sha256'] for f in self._package(member_id, name)['files'].values())
        return used

    def _orphan_blob_bytes(self, member_id, used=None):
        used = self._used_blobs(member_id) if used is None else used
        total = 0
        for folder in (self._blob_dir(member_id), self._deliverable_dir(member_id) / 'uploads'):
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                if entry.is_file() and (folder.name == 'uploads' or entry.name not in used):
                    try:
                        total += entry.stat().st_size
                    except FileNotFoundError:
                        pass
        return total

    def _total_orphan_blob_bytes(self):
        return sum(self._orphan_blob_bytes(member['id']) for member in self._members())

    def _put_blob(self, member_id, digest, temporary, protect=()):
        blobs = self._blob_dir(member_id)
        blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = blobs / digest
        if not target.is_file():
            protected = set(protect)
            used = self._used_blobs(member_id) | protected
            orphan = self._orphan_blob_bytes(member_id, used)
            if orphan > MAX_ORPHAN_BLOBS_BYTES:
                self._collect_blobs(member_id, min_age=ORPHAN_BLOB_TTL_SECONDS, protect=protected)
                orphan = self._orphan_blob_bytes(member_id, used)
            if orphan > MAX_ORPHAN_BLOBS_BYTES:
                self._collect_blobs(member_id, protect=protected)
                orphan = self._orphan_blob_bytes(member_id, used)
            if orphan > MAX_ORPHAN_BLOBS_BYTES:
                raise HubError(507, '当前成员未完成的临时上传文件过多，请稍后重试。')
            if not protected:
                total_orphan = self._total_orphan_blob_bytes()
                if total_orphan > MAX_SUGGESTION_TOTAL_BYTES:
                    for member in self._members():
                        self._collect_blobs(member['id'], min_age=ORPHAN_BLOB_TTL_SECONDS)
                    total_orphan = self._total_orphan_blob_bytes()
                if total_orphan > MAX_SUGGESTION_TOTAL_BYTES:
                    raise HubError(507, '分享站临时上传空间已满，请稍后重试。')
        os.replace(temporary, target)
        return {'sha256': digest, 'size': target.stat().st_size}

    def _collect_blobs(self, member_id, min_age=0, protect=()):
        # A parallel upload of the same member may lose a blob here; its list then gets 409 with
        # `missing` and the uploader sends those again, so no reservation is kept.
        blobs = self._blob_dir(member_id)
        used = self._used_blobs(member_id) | set(protect)
        cutoff = (datetime.now(timezone.utc).timestamp() - min_age) if min_age > 0 else None
        for blob in (blobs.iterdir() if blobs.is_dir() else []):
            if blob.name not in used:
                try:
                    if cutoff is not None and blob.stat().st_mtime > cutoff:
                        continue
                    blob.unlink(missing_ok=True)
                except (PermissionError, FileNotFoundError):
                    pass  # Windows may still be serving the file. Revisit on the next write.
        uploads = self._deliverable_dir(member_id) / 'uploads'
        if uploads.is_dir() and min_age > 0:
            for temp_file in uploads.iterdir():
                try:
                    if temp_file.is_file() and temp_file.stat().st_mtime <= cutoff:
                        temp_file.unlink(missing_ok=True)
                except (PermissionError, FileNotFoundError):
                    pass

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

    def _suggestion_path(self, identity):
        return self.data / 'suggestions' / (identity + '.json')

    def _suggestions(self):
        for path in (self.data / 'suggestions').glob('*.json'):
            yield json.loads(path.read_text(encoding='utf-8'))

    def _suggestion(self, identity, member_id):
        path = self._suggestion_path(identity)
        if not path.is_file():
            raise HubError(404, '找不到这条改进建议。')
        row = json.loads(path.read_text(encoding='utf-8'))
        if member_id not in (row['author_id'], row['target_member_id']):
            raise HubError(403, '只有提交人和原作者能查看这条建议。')
        return row

    def _suggestion_listing(self, row):
        listing = self._listings(row['target_member_id']).get(row['skill_name'])
        return listing if listing and listing['source']['kind'] == 'files' else {}

    def _suggestion_info(self, row, member_id, listing):
        current = listing.get('source', {}).get('version', '')
        return {**{k: v for k, v in row.items() if k not in {'files', 'review_text', 'review_diff'}},
                'current_version': current, 'stale': current != row['base_version'],
                'role': 'incoming' if row['target_member_id'] == member_id else 'outgoing'}

    def _list_suggestions(self, member_id, role, status, skill):
        rows = []
        for row in self._suggestions():
            if (member_id not in (row['target_member_id'], row['author_id']) or
                    (role == 'incoming' and row['target_member_id'] != member_id) or
                    (role == 'outgoing' and row['author_id'] != member_id) or
                    (status == 'pending' and row['status'] != status) or (skill and row['skill_name'] != skill)):
                continue
            rows.append(self._suggestion_info(row, member_id, self._suggestion_listing(row)))
        return {'suggestions': sorted(rows, key=lambda r: r['created_at'], reverse=True)}

    def _suggestion_capacity(self, author, target, name, files):
        sizes = {(author, f['sha256']): f['size'] for f in files.values()}
        stored = per_skill = per_author = 0
        finished = []
        for row in self._suggestions():
            stored += 1
            if row['status'] != 'pending':
                finished.append((row['created_at'], row['id']))
                continue
            per_skill += (row['target_member_id'], row['skill_name']) == (target, name)
            per_author += row['author_id'] == author
            sizes.update(((row['author_id'], f['sha256']), f['size']) for f in row['files'].values())
        if per_skill >= MAX_SUGGESTIONS_PER_SKILL or per_author >= MAX_SUGGESTIONS_PER_MEMBER:
            raise HubError(413, '待审核建议已满，请先处理已有建议。')
        if sum(sizes.values()) > MAX_SUGGESTION_TOTAL_BYTES:
            raise HubError(413, '分享站待审核的文件已达容量上限，请先处理已有建议。')
        excess = max(0, stored + 1 - MAX_SUGGESTIONS_TOTAL)
        if excess > len(finished):
            raise HubError(413, '分享站待审核建议已满，请先处理已有建议。')
        return {self._suggestion_path(identity): None for _, identity in sorted(finished)[:excess]}

    def _check_suggestion_secrets(self, author, files):
        for relative, entry in files.items():
            name = Path(relative).name.lower()
            if any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_NAMES):
                raise HubError(400, f'文件「{relative}」文件名像账号或密钥文件，已拦下，不能提交。')
            raw = self._verified_bytes(author, entry)
            if SECRET_CONTENT.search(raw.decode('utf-8', errors='replace')):
                raise HubError(400, f'文件「{relative}」内容疑似含有密钥或密码，已拦下，不能提交。')

    def _verified_bytes(self, member, file):
        try:
            with (self._blob_dir(member) / file['sha256']).open('rb') as stream:
                raw = stream.read(file['size'] + 1)
        except FileNotFoundError:
            raise HubError(409, '建议里的文件缺失，请重新上传。') from None
        if len(raw) != file['size'] or hashlib.sha256(raw).hexdigest() != file['sha256']:
            raise HubError(409, '建议里的文件未通过完整性核对，请重新上传。')
        return raw

    def _suggestion_metadata(self, author, files, name):
        raw = self._verified_bytes(author, files['SKILL.md'])
        try:
            info = metadata(raw.decode('utf-8'))
        except (ValueError, AttributeError):
            raise HubError(400, 'SKILL.md 的说明格式不正确。') from None
        if info.get('name') != name:
            raise HubError(400, '技能文件里的名称和上架的名称不一致。')
        return info

    def _create_suggestion(self, member, data):
        target, name, base = (data.get(k) for k in ('target_member_id', 'skill_name', 'base_version'))
        if not isinstance(target, str) or not re.fullmatch('[0-9a-f]{32}', target):
            raise HubError(400, '原作者编号不正确。')
        if not isinstance(name, str) or not re.fullmatch(NAME, name):
            raise HubError(400, '技能名称不正确。')
        listing = self._listings(target).get(name)
        if not listing:
            raise HubError(404, '原技能已下架。')
        if listing['source']['kind'] != 'files':
            raise HubError(400, FILES_ONLY)
        author = member['id']
        if author == target:
            raise HubError(403, OWN_SKILL)
        if not isinstance(base, str) or not re.fullmatch('[0-9a-f]{64}', base):
            raise HubError(400, '请重新预览并核对原技能版本。')
        message = suggestion_text(data.get('message'))
        try:
            files = clean_skill_manifest(data.get('files'))
        except ValueError as error:
            raise HubError(400, str(error)) from None
        version = package_version(files)
        if version == listing['source']['version']:
            raise HubError(409, NO_CHANGES)
        changes = self._suggestion_capacity(author, target, name, files)
        self._check_blobs(author, files.values())
        self._check_suggestion_secrets(author, files)
        self._suggestion_metadata(author, files, name)
        base_revision = (listing.get('revision', 1) if base == listing['source']['version']
                         else (listing.get('version_revisions') or {}).get(base))
        stamp = now()
        row = {'id': secrets.token_hex(16), 'target_member_id': target,
               'target_member_name': next(m['name'] for m in self._members() if m['id'] == target),
               'author_id': author, 'author_name': member['name'], 'skill_name': name,
               'card_key': listing_key(target, listing), 'base_version': base, 'base_revision': base_revision,
               'version': version,
               'files': files, 'message': message, 'file_count': len(files), 'size': sum(f['size'] for f in files.values()),
               'status': 'pending', 'created_at': stamp, 'updated_at': stamp, 'reviewed_at': '',
               'review_note': '', 'published_version': ''}
        changes[self._suggestion_path(row['id'])] = row
        self._save(changes)
        return self._suggestion_info(row, author, listing)

    def _published_files(self, target, name):
        files = self._package(target, name)['files']
        if isinstance(files, list):
            inline = {r['path']: r['content'] for r in files}
            return skill_manifest(files), lambda path: base64.b64decode(inline[path])
        return files, lambda path: self._verified_bytes(target, files[path])

    def _suggestion_detail(self, row, member_id):
        listing = self._suggestion_listing(row)
        info = self._suggestion_info(row, member_id, listing)
        revision = listing.get('revision', 1) if listing else None
        if row['status'] != 'pending' and isinstance(row.get('review_diff'), dict):
            return {**info, 'current_revision': revision,
                    'diff': {**row['review_diff'], 'stale': info['stale'],
                             'current_version': info['current_version'], 'current_revision': revision}}
        old, read_old = self._published_files(row['target_member_id'], row['skill_name']) if listing else ({}, None)
        def read_new(path):
            file = row['files'][path]
            if row['status'] != 'pending':
                digest = file['sha256']
                review_text = row.get('review_text', {})
                if digest in review_text:
                    text = review_text[digest]
                    return text.encode('utf-8') if text is not None else b'\0'
                if (self._blob_dir(row['target_member_id']) / digest).is_file():
                    return self._verified_bytes(row['target_member_id'], file)
                if not (self._blob_dir(row['author_id']) / digest).is_file():
                    raise FileNotFoundError
            return self._verified_bytes(row['author_id'], file)
        comparison = suggestion_diff(old, row['files'], read_old, read_new)
        return {**info, 'current_revision': revision, 'diff': {**comparison, 'stale': info['stale'],
                'current_version': info['current_version'], 'current_revision': revision}}

    def _finish_suggestion(self, row, status, note):
        # Retain bounded text for future comparisons, even after both members publish other versions.
        # A null entry marks binary content. Original uploads can now be collected.
        review_text = {}
        remaining = MAX_DIFF_TOTAL_CHARS
        for file in row['files'].values():
            digest = file['sha256']
            if digest in review_text or not remaining or file['size'] > MAX_DIFF_FILE_BYTES:
                continue
            try:
                raw = self._verified_bytes(row['author_id'], file)
                text = None if b'\0' in raw else raw.decode('utf-8')
            except UnicodeDecodeError:
                text = None
            except HubError:
                continue  # A damaged upload can still be rejected or removed.
            if text is None or len(text) <= remaining:
                review_text[digest] = text
                remaining -= len(text) if text else 0
        stamp = now()
        row.update(status=status, review_note=note, reviewed_at=stamp, updated_at=stamp, review_text=review_text)

    def _review_suggestion(self, row, member_id, action, data):
        owner = row['author_id'] if action == 'withdraw' else row['target_member_id']
        if owner != member_id:
            raise HubError(403, '只有提交人能撤回建议。' if action == 'withdraw' else '只有原作者能审核建议。')
        if row['status'] != 'pending':
            raise HubError(409, '这条建议已经处理过了。')
        note = suggestion_text(data.get('review_note', ''), review=True)
        if action == 'accept':
            return self._accept_suggestion(row, note, data)
        if action == 'withdraw':
            self._save({self._suggestion_path(row['id']): None})
            self._collect_blobs(member_id)
            return {'withdrawn': True, 'id': row['id']}
        listing = self._suggestion_listing(row)
        if listing:
            old_files, read_old = self._published_files(row['target_member_id'], row['skill_name'])
            try:
                row['review_diff'] = suggestion_diff(
                    old_files, row['files'], read_old,
                    lambda path: self._verified_bytes(row['author_id'], row['files'][path]))
            except HubError:
                pass
        self._finish_suggestion(row, 'rejected', note)
        self._save({self._suggestion_path(row['id']): row})
        self._collect_blobs(row['author_id'])
        return {'suggestion': self._suggestion_info(row, member_id, listing),
                'message': '已拒绝这条改进建议。'}

    def _accept_suggestion(self, row, note, data):
        target, author, name = row['target_member_id'], row['author_id'], row['skill_name']
        listings = self._listings(target)
        listing = listings.get(name, {})
        if listing.get('source', {}).get('kind') != 'files':
            raise HubError(404, '原技能已下架或已改用其他来源。')
        current, version = listing['source']['version'], row['version']
        stale = row['base_version'] != current
        expected = data.get('expected_current_version')
        if ((stale and (data.get('allow_stale') is not True or not isinstance(expected, str) or expected != current)) or
                ('expected_current_version' in data and expected != current)):
            raise HubError(409, '原作者已更新技能，请核对与当前版本的改动后再接受。',
                           {'stale': True, 'current_version': current, 'base_version': row['base_version']})
        if current == version:
            raise HubError(409, '建议内容与线上当前版本完全一致。')
        try:
            files = clean_skill_manifest(row['files'])
        except ValueError as error:
            raise HubError(400, str(error)) from None
        self._check_suggestion_secrets(author, files)
        raw_desc = self._suggestion_metadata(author, files, name).get('description')
        description = _clean_description(raw_desc) if raw_desc else ''
        size = sum(file['size'] for file in files.values())
        if version != package_version(files) or row['size'] != size:
            raise HubError(409, '建议里的文件和提交时的记录不一致，请重新提交。')
        old_files, read_old = self._published_files(target, name)
        if description and listing.get('description'):
            previous = _clean_description(metadata(read_old('SKILL.md').decode('utf-8', errors='replace')).get('description', ''))
            if listing['description'] != previous:
                description = ''  # The author supplied a separate store description.
        if description:
            listing['description'] = description
        review_diff = suggestion_diff(old_files, files, read_old, lambda path: self._verified_bytes(author, files[path]))
        use_inline = len(files) <= INLINE_MAX_FILES and size <= INLINE_MAX_SIZE
        source = {'kind': 'files', 'version': version, 'file_count': len(files), 'size': size}
        if not use_inline:
            source['file_storage'] = 'blobs'
        new_revision = listing.get('revision', 1) + 1
        ver_revs = dict(listing.get('version_revisions') or {})
        ver_revs.setdefault(current, listing.get('revision', 1))
        ver_revs[version] = new_revision
        if len(ver_revs) > 30:
            ver_revs = dict(list(ver_revs.items())[-30:])
        listing.update(created_at=listing.get('created_at', listing['updated_at']), updated_at=now(),
                       revision=new_revision, published_via='suggestion', version_revisions=ver_revs, source=source)
        try:
            clean_listing(listing, packaged=True)
        except ValueError as error:
            raise HubError(400, str(error)) from None
        self._finish_suggestion(row, 'accepted', note)
        row['published_version'] = version
        row['review_diff'] = review_diff
        if use_inline:
            inline_files = [
                {'path': path, 'content': base64.b64encode(self._verified_bytes(author, file)).decode('ascii'),
                 'executable': file['executable']}
                for path, file in sorted(files.items())
            ]
            package_payload = {'name': name, 'version': version, 'files': inline_files}
        else:
            package_payload = {'name': name, 'version': version, 'files': files}
        changes = {self.data / 'packages' / target / (name + '.json'): package_payload,
                   self._listing_path(target): {'listings': listings}, self._suggestion_path(row['id']): row}
        try:
            if not use_inline:
                by_sha = {f['sha256']: f for f in files.values()}
                protected = set(by_sha)
                for digest, file in by_sha.items():
                    raw = self._verified_bytes(author, file)
                    if (self._blob_dir(target) / digest).is_file():
                        try:
                            self._verified_bytes(target, file)
                        except HubError:
                            pass  # Repair the author's damaged copy with verified bytes.
                        else:
                            continue
                    stream, temporary = self.upload_slot(target)
                    try:
                        with stream:
                            stream.write(raw)
                        self._put_blob(target, digest, temporary, protect=protected)
                    finally:
                        temporary.unlink(missing_ok=True)
                self._check_blobs(target, files.values())
            self._save(changes)
        finally:
            self._collect_blobs(target)
        self._collect_blobs(author)
        return {'suggestion': self._suggestion_info(row, target, listing), 'version': version,
                'revision': listing['revision'], 'message': '已接受改进建议并上架新版。'}

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
            stream, temporary = store.upload_slot(member['id'], incoming_bytes=length)
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
            with store.locked():
                store.release_upload(member['id'], length)

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

    def _serve_feedback_attachment(self, row_id, digest):
        store = self.server.store
        with store.locked():
            member = store._authenticate(self.headers.get('Authorization', ''))
            raw, kind, filename = store._feedback_attachment_file(member['id'], row_id, digest)
        # File handle is already closed before writing to the network so unlinked evicted files free disk immediately.
        try:
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return

    def _send_file(self, stream, size, kind, range_header=None, attachment=False, filename=None, csp=None):
        # Stream outside the data lock; the version was checked when opening the file.
        with stream:
            span = byte_range(range_header, size)
            start, end = span or (0, size - 1)
            self.send_response(206 if span else 200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(end - start + 1))
            if attachment:
                disposition = f'attachment; filename="{filename}"' if filename else 'attachment'
                self.send_header('Content-Disposition', disposition)
            else:
                self.send_header('Accept-Ranges', 'bytes')
            if span:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if csp:
                self.send_header('Content-Security-Policy', csp)
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

    def _read_feedback_body(self):
        length = self._length(feedback.MAX_UPLOAD_BODY, '反馈内容及截图超过上传上限，请减少截图数量或缩小图片后重试。')
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            raise HubError(408, '上传等待太久，请重新上传。') from None
        if len(body) != length:
            raise HubError(400, '反馈内容没有传完整，请重新提交。')
        def invalid_constant(value):
            raise ValueError
        try:
            data = json.loads(body.decode('utf-8'), parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise HubError(400, '反馈内容格式不正确，请重试。') from None
        if not isinstance(data, dict):
            raise HubError(400, '请求内容格式不正确。')
        if not data.get('attachments'):
            if length > feedback.MAX_BODY:
                raise HubError(413, '反馈内容超过 64 KB，请缩短后重试。')
        else:
            text_only = {k: v for k, v in data.items() if k != 'attachments'}
            if len(json.dumps(text_only, ensure_ascii=False).encode('utf-8')) > feedback.MAX_BODY:
                raise HubError(413, '反馈文字内容超过 64 KB，请缩短后重试。')
        return data

    def _handle(self):
        try:
            address = urllib.parse.urlsplit(self.path)
            if address.fragment:
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            path = address.path
            listing = re.fullmatch(r'/api/listings/me/(' + NAME + ')', path)
            suggestion = re.fullmatch(r'/api/suggestions/([0-9a-f]{32})(?:/(accept|reject))?', path)
            package = re.fullmatch(r'/api/packages/([0-9a-f]{32})/(' + NAME + r')(?:/([0-9a-f]{64}))?', path)
            blob = re.fullmatch(r'/api/blobs/me/([0-9a-f]{64})', path)
            feedback_att = re.fullmatch(r'/api/feedback/([0-9a-f]{32})/attachments/([0-9a-f]{64})', path)
            identity = '(' + DELIVERABLE_ID + ')'
            mine = re.fullmatch(r'/api/deliverables/me/' + identity, path)
            deliverable = re.fullmatch(r'/api/deliverables/([0-9a-f]{32})/' + identity, path)
            served = re.fullmatch(r'/api/deliverables/([0-9a-f]{32})/' + identity + '/(.+)', path)
            query = urllib.parse.parse_qs(address.query, keep_blank_values=True)
            activity_days = 30
            if path == '/api/suggestions' and self.command == 'GET':
                role = query.get('role', ['all'])[0]
                status = query.get('status', ['pending'])[0]
                skill = query.get('skill', [''])[0]
                if (query.keys() - {'role', 'status', 'skill'} or any(len(v) != 1 for v in query.values()) or
                        role not in {'all', 'incoming', 'outgoing'} or status not in {'all', 'pending'} or
                        ('skill' in query and not re.fullmatch(NAME, skill))):
                    raise HubError(400, '建议筛选条件不正确，请核对角色、状态和技能名称。')
            elif path == '/api/activity' and self.command == 'GET' and '?' in self.path:
                if set(query) != {'days'} or len(query['days']) != 1:
                    raise HubError(404, '没有这个分享站地址，请核对后重试。')
                raw_days = query['days'][0]
                if not re.fullmatch(r'\d{1,3}', raw_days):
                    raise HubError(400, f'统计天数须为 1 到 {ACTIVITY_RETENTION_DAYS} 之间的整数。')
                activity_days = int(raw_days)
                if not 1 <= activity_days <= ACTIVITY_RETENTION_DAYS:
                    raise HubError(400, f'统计天数须为 1 到 {ACTIVITY_RETENTION_DAYS} 之间的整数。')
            elif not served and ('?' in self.path or (package and package[3])) and (
                    not package or set(query) != {'version'} or len(query['version']) != 1 or
                    not re.fullmatch(r'[0-9a-f]{64}', query['version'][0])):
                raise HubError(404, '没有这个分享站地址，请核对后重试。')
            allowed = ({'POST'} if path in {'/api/join', '/api/feedback', '/api/feedback/with-attachments'} else
                       {'GET', 'POST'} if path in {'/api/activity', '/api/suggestions'} else
                       {'POST'} if suggestion and suggestion[2] else {'GET', 'DELETE'} if suggestion else
                       {'GET'} if path in {'/api/me', '/api/catalog'} or package or deliverable or served or feedback_att else
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
            if feedback_att:
                self._serve_feedback_attachment(feedback_att[1], feedback_att[2])
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
            is_feedback = path in {'/api/feedback', '/api/feedback/with-attachments'}
            has_listing_body = bool(listing and self.command == 'DELETE' and (
                self.headers.get('Transfer-Encoding') or
                any(value.strip() != '0' for value in self.headers.get_all('Content-Length', []))))
            data = (self._read_feedback_body()
                    if is_feedback else
                    self._read_body(MAX_SUGGESTION_BODY, '建议内容超过 1 MB，请减少文件数量。')
                    if self.command == 'POST' and (suggestion or path == '/api/suggestions') else
                    self._read_body(MAX_ACTIVITY_BODY, '活跃上报内容超过 1024 字节。')
                    if path == '/api/activity' and self.command == 'POST' else
                    self._read_body(MAX_INSTALL_BODY, '上报内容超过 64 KB，请缩短后重试。')
                    if path == '/api/installs/me' else
                    self._read_body(MAX_PACK_BODY, '技能包内容超过 64 KB，请缩短后重试。')
                    if path == '/api/packs/me' else
                    self._read_body(MAX_MANIFEST_BODY, '成品清单超过 1 MB，请减少文件数量。')
                    if mine and self.command == 'PUT' else None if mine else
                    self._read_body(MAX_RATING_BODY, '下架信息超过 64 KB，请缩短后重试。')
                    if has_listing_body else
                    self._read_body(MAX_BODY if path == '/api/listings/me' else MAX_RATING_BODY)
                    if self.command == 'PUT' or path == '/api/ratings' else None)
            parsed_attachments = None
            if is_feedback:
                if not self.server.feedback_webhook:
                    raise HubError(503, '管理员尚未启用飞书反馈接收，请复制或导出诊断信息。')
                if path == '/api/feedback/with-attachments' and not data.get('attachments'):
                    raise HubError(400, '请附带至少一张有效截图。')
                # Validate and sanitize screenshots before acquiring the store write lock.
                try:
                    parsed_attachments = feedback.parse_attachments(data.get('attachments'))
                except feedback.AttachmentTooLargeError as error:
                    raise HubError(413, str(error)) from None
                except feedback.AttachmentError as error:
                    raise HubError(400, str(error)) from None
            store = self.server.store
            skill_files = self.headers.get(SKILL_FILE_HEADER) == '1'
            with store.locked():
                # A member may have been removed while the upload body was arriving.
                member = store._authenticate(self.headers.get('Authorization', ''))
                if path == '/api/me':
                    result = {'service': 'skill-pilot-team', 'api': 2, 'member': member,
                              'feedback_supported': True, 'feedback_configured': bool(self.server.feedback_webhook),
                              'feedback_attachments_supported': True,
                              'skill_file_upload': True,
                              'suggestions_supported': True,
                              'replaces_supported': True}
                elif path == '/api/suggestions':
                    result = (store._create_suggestion(member, data) if self.command == 'POST' else
                              store._list_suggestions(member['id'], role, status, skill))
                elif suggestion:
                    row = store._suggestion(suggestion[1], member['id'])
                    result = (store._suggestion_detail(row, member['id']) if self.command == 'GET' else
                              store._review_suggestion(row, member['id'], suggestion[2] or 'withdraw', data or {}))
                elif is_feedback:
                    if not self.server.feedback_webhook:
                        raise HubError(503, '管理员尚未启用飞书反馈接收，请复制或导出诊断信息。')
                    row, duplicate = store._receive_feedback(member, data, attachments=parsed_attachments)
                    result = {'submitted': True, 'duplicate': duplicate, 'feedback_id': row['id']}
                    if row.get('attachments'):
                        result['attachments'] = [
                            {'id': att['id'], 'content_type': att['content_type'],
                             'size': att['size'], 'evicted': bool(att.get('evicted'))}
                            for att in row['attachments']
                        ]
                elif path == '/api/catalog':
                    result = store._catalog(member['id'], skill_files)
                elif path == '/api/activity':
                    result = (store._record_activity(member['id']) if self.command == 'POST' else
                              store._activity_summary(days=activity_days))
                elif path == '/api/listings/me':
                    result = store._put_listings(member['id'], data, skill_files)
                elif path == '/api/installs/me':
                    result = store._put_installs(member['id'], data, skill_files)
                elif path == '/api/packs/me':
                    result = store._put_packs(member, data, skill_files)
                elif listing:
                    result = store._remove_listing(member['id'], listing[1], data)
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
            if is_feedback and not duplicate:
                # Do not hold the store lock across the external request.
                try:
                    feedback.forward(self.server.feedback_webhook, row['payload'])
                except ValueError as error:
                    store.finish_feedback(row['id'], 'failed')
                    raise HubError(502, str(error)) from None
                except BaseException:
                    store.finish_feedback(row['id'], 'failed')
                    raise
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
    def __init__(self, data, host='0.0.0.0', port=8765, open=False, feedback_webhook='',
                 github_refresh_hours=GITHUB_REFRESH_HOURS):
        if not 0 <= port <= 65535:
            raise ValueError('端口应为 0 到 65535 之间的数字。')
        self.open = open
        self.feedback_webhook = feedback.webhook_url(feedback_webhook)
        self.store = HubStore(data, github_refresh_hours=github_refresh_hours)
        with self.store.locked():
            members = self.store._members()
            self.store._cleanup_feedback_orphans()
            self.store._cleanup_expired_activity()
            for member in members:
                self.store._collect_blobs(member['id'], min_age=ORPHAN_BLOB_TTL_SECONDS)
        super().__init__((host, port), Handler)


def run(args):
    if args.hub_command == 'serve':
        raw_refresh = getattr(args, 'github_refresh_hours', None)
        if raw_refresh is not None:
            refresh_hours = clean_github_refresh_hours(raw_refresh, allow_blank=False)
        else:
            refresh_hours = clean_github_refresh_hours(os.environ.get('SKILL_PILOT_HUB_GITHUB_REFRESH_HOURS', ''))
        with HubServer(args.data, args.host, args.port, open=args.open,
                       feedback_webhook=args.feedback_webhook or os.environ.get('SKILL_PILOT_HUB_FEEDBACK_WEBHOOK', ''),
                       github_refresh_hours=refresh_hours) as server:
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
    elif args.hub_command == 'activity':
        report = store.activity_summary(days=args.days)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        rows = report['members']
        if not rows:
            print('还没有成员，请先添加。')
            return
        summary = report['summary']
        print(f'最近 {report["window_days"]} 天成员活跃统计：共 {summary["total_members"]} 位成员，'
              f'{summary["active_members"]} 位活跃，今日 {summary["active_today"]} 位打开，'
              f'累计打开 {summary["total_opens"]} 次')
        print('成员编号\t显示名\t活跃天数\t总打开次数\t今日打开\t最近活跃时间\t每日打开')
        for row in rows:
            daily = '、'.join(f'{day}({count}次)' for day, count in sorted(row['daily'].items(), reverse=True)) or '—'
            last_active = row['last_active_at'] or '暂无记录'
            print(f'{row["id"]}\t{row["name"]}\t{row["active_days"]} 天\t{row["total_opens"]} 次\t'
                  f'{row["opens_today"]} 次\t{last_active}\t{daily}')
    else:
        result = store.remove_member(args.member_id)
        if isinstance(result, dict) and result.get('activity_cleaned') is False:
            print('已删除这位成员和他的上架、技能包、文件及评价；活跃记录文件损坏，已保留原文件，请管理员检查。')
        else:
            print('已删除这位成员和他的上架、技能包、文件、评价及活跃记录。')
