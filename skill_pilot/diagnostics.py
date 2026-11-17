"""Local, bounded diagnostics. No skill bodies, network calls or automatic uploads."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import traceback
import tomllib
import uuid
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from contextlib import contextmanager
from .platform_support import fcntl

from .redaction import _hide_paths_outside_home, hide_credentials
from .store import PROJECT, guard, target_home, write_allowed

MAX_RECORDS = 200
MAX_BYTES = 1024 * 1024
LOCK = threading.RLock()
SOURCES = {'startup', 'backend', 'job', 'self_update', 'frontend'}
SNAPSHOT_FIELDS = ('os', 'architecture', 'python_version', 'install_mode', 'app_version')


@contextmanager
def log_lock(path, home):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = guard(path.with_name('.errors.lock'), home)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield  # Closing the file releases the lock on both supported platforms.


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def sanitize(value, home, limit=6000):
    """Redact before truncating, including configured secrets without a prefix."""
    text = str(value)
    private = Path(home) / '.skill-pilot'
    hidden = set()
    def secrets_in(value, key=''):
        if isinstance(value, dict):
            for name, item in value.items():
                secrets_in(item, name)
        elif isinstance(value, str) and value and re.search(r'key|token|secret|password|url|name', key, re.I):
            hidden.add(value)
            if re.search(r'url', key, re.I):
                try:
                    address = urllib.parse.urlsplit(value)
                    if address.hostname:
                        hidden.add(address.hostname)
                except ValueError:
                    pass
    for name in ('settings.json', 'team.json'):
        secrets_in(read_json(private / name))
    registry = read_json(private / 'registry.json')
    for field in ('skills', 'external_skills'):
        skills = registry.get(field, {})
        if isinstance(skills, dict):
            hidden.update(skills)
    for name in ('store', 'bases'):
        try:
            hidden.update(p.name for p in (private / name).iterdir())
        except OSError:
            pass
    variants = set(hidden)
    for secret in hidden:
        variants.update({repr(secret)[1:-1], repr(secret.encode('utf-8'))[2:-1],
                         json.dumps(secret, ensure_ascii=False)[1:-1],
                         json.dumps(secret, ensure_ascii=True)[1:-1]})
    for name in sorted(variants, key=len, reverse=True):
        if name:
            text = text.replace(name, '[私人内容已隐藏]')
    text = hide_credentials(text)
    # Hide a whole path clause, including terminal filenames containing spaces.
    text = re.sub(r"(?<![\w.])(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/])[^\r\n'\"<>|，；：。]*", '[路径已隐藏]', text)
    text = _hide_paths_outside_home(text)
    for name in {Path(home).name, Path.home().name, os.environ.get('USERNAME', ''), os.environ.get('USER', '')}:
        if name:
            text = re.sub(r'(?<![\w])' + re.escape(name) + r'(?![\w])', '[用户]', text)
    text = ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127))
    return text[:limit]


def causes(error):
    found = set()
    while error is not None and id(error) not in found:
        found.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def is_fault(error):
    """Plain ValueError is the existing validation contract; wrapped failures count."""
    if type(error).__name__ == 'RequestError':
        return getattr(error, 'status', 500) >= 500
    return any(not isinstance(e, (ValueError, StopIteration)) or
               isinstance(e, (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeError)) or
               type(e).__name__ in {'ServiceError', 'DownloadError', 'CacheError', 'RollbackError'}
               for e in causes(error))


def code_frames(error):
    frames = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename)
        try:
            relative = path.resolve().relative_to(PROJECT.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        if relative.startswith(('skill_pilot/', 'scripts/')):
            frames.append(f'{relative}:{frame.lineno} ({frame.name})')
    return frames[-8:]


def recent(home, *, sanitize_records=True):
    path = Path(home) / '.skill-pilot/logs/errors.jsonl'
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - MAX_BYTES)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read(MAX_BYTES).decode('utf-8', 'replace').splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not re.fullmatch(r'[a-f0-9]{32}', row.get('error_id', '')):
                continue
            clean = {k: (sanitize(row.get(k, ''), home) if sanitize_records else row.get(k, '')) for k in
                     ('error_id', 'fingerprint', 'time', 'source', 'action', 'user_message', 'error_type', 'detail')}
            snapshot = row.get('environment')
            if isinstance(snapshot, dict):
                clean['environment'] = {k: (sanitize(snapshot[k], home, 160) if sanitize_records else snapshot[k][:160])
                                        for k in SNAPSHOT_FIELDS
                                        if isinstance(snapshot.get(k), str)}
            rows.append(clean)
        except (ValueError, TypeError):
            continue
    return rows[-MAX_RECORDS:][::-1]


def record(home, source, action, error, user_message='操作没有完成，请重试。', *, force=False):
    if not force and not is_fault(error):
        return {}
    try:
        home = Path(home)
        chain = list(causes(error))
        cause = chain[-1]
        frames = code_frames(cause) or code_frames(error)
        location = re.sub(r':\d+', '', frames[-1]) if frames else getattr(error, 'diagnostic_location', 'unknown')
        kind = getattr(error, 'diagnostic_type', type(cause).__name__)
        source = source if source in SOURCES else 'backend'
        action = {'check': '检查程序更新', 'apply': '安装程序更新', 'assignment': '分配技能',
                  'install': '安装技能', 'update': '更新技能', 'cleanup_withdraw': '撤下技能',
                  'cleanup_delete': '删除技能', 'restore': '恢复技能',
                  'team_install': '安装团队技能', 'github_install': '安装热门技能'}.get(action, action)
        action = sanitize(action, home, 160)
        detail = '\n'.join(type(e).__name__ + ': ' + str(e) for e in chain)
        if source == 'frontend':
            detail = kind + ': ' + str(error)
        # Subprocess output and traceback source lines may contain entire skill bodies.
        if isinstance(cause, subprocess.SubprocessError):
            detail = kind + ': ' + str(getattr(cause, 'returncode', '执行超时'))
        contextual = next((e for e in chain if isinstance(getattr(e, 'diagnostic_detail', None), str)), None)
        context = contextual.diagnostic_detail if contextual is not None else ''
        if context and user_message == '操作没有完成，请重试。':
            user_message = str(contextual)
        detail = ((sanitize(context, home, 2200) + '\n\n' + sanitize(detail, home, 2500))
                  if context else sanitize(detail, home, 4500))
        if source == 'frontend' and location != 'unknown':
            detail += '\n' + location
        row = {'error_id': uuid.uuid4().hex,
               'fingerprint': hashlib.sha256(f'{source}|{action}|{kind}|{location}'.encode()).hexdigest()[:20],
               'time': datetime.now(timezone.utc).isoformat(), 'source': source, 'action': action,
               'user_message': sanitize(user_message, home, 400), 'error_type': kind,
               'detail': detail + ('\n' + '\n'.join(frames) if frames else '')}
        # A late feedback submission must not replace the runtime at failure time.
        try:
            snapshot = environment(home, runtime_only=True)
            row['environment'] = {k: sanitize(snapshot[k], home, 160) for k in SNAPSHOT_FIELDS
                                  if isinstance(snapshot.get(k), str)}
        except Exception:
            pass  # Keep the original error even when its environment cannot be read.
        if write_allowed(home):
            path = guard(home / '.skill-pilot/logs/errors.jsonl', home)
            with LOCK, log_lock(path, home):
                rows = list(reversed(recent(home, sanitize_records=False))) + [row]
                encoded = [(json.dumps(r, ensure_ascii=False) + '\n').encode() for r in rows[-MAX_RECORDS:]]
                while sum(map(len, encoded)) > MAX_BYTES:
                    encoded.pop(0)
                fd, temporary = tempfile.mkstemp(prefix='.errors-', dir=path.parent)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(b''.join(encoded))
                    os.replace(temporary, guard(path, home))
                finally:
                    Path(temporary).unlink(missing_ok=True)
        return row
    except Exception:
        # Diagnostics must never replace the original failure.
        return {}


def failure(home, source, action, error, message='操作没有完成，请重试。'):
    row = record(home, source, action, error, message)
    return {'error_id': row['error_id']} if row else {}


def environment(home, project=PROJECT, *, runtime_only=False):
    from .environment import tool_environment
    from .profiles import PROFILES, ADDITIONAL_PROFILES
    home, project = Path(home), Path(project)
    env = {**tool_environment(home), 'GIT_OPTIONAL_LOCKS': '0'}
    tools = {}
    def output(command):
        try:
            result = subprocess.run(command, env=env, cwd=project, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=3, text=True, errors='replace')
            return result.stdout.strip() if result.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            return ''
    for name in (() if runtime_only else ('git', 'node', 'npm', 'lark-cli')):
        executable = shutil.which(name, path=env['PATH'])
        text = output([executable, '--version']) if executable else ''
        version = re.search(r'(?<!\d)\d+\.\d+(?:\.\d+)?(?:[-+][a-zA-Z0-9.]+)?', text)
        tools[name] = {'available': bool(executable), 'version': version[0] if version else '未能读取'}
    checkout = (project / '.git').exists()
    installation = read_json(project / '.skill-pilot-install.json')
    revision = output(['git', '-C', str(project), 'rev-parse', 'HEAD']) if checkout else installation.get('revision', '')
    modified = bool(output(['git', '-C', str(project), 'status', '--porcelain', '--untracked-files=no'])) if checkout and not runtime_only else None
    if not runtime_only and not checkout and installation.get('files'):
        from .self_update import bundle_files
        try:
            modified = bundle_files(project) != installation['files']
        except (OSError, ValueError):
            pass
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{7,64}', revision):
        from .web.runtime import version
        revision = version()
    os_version = ('macOS ' + platform.mac_ver()[0] if platform.system() == 'Darwin'
                  else platform.system() + ' ' + platform.release())
    runtime = {'os': os_version, 'architecture': platform.machine(),
               'python_version': platform.python_version(), 'install_mode': '源码' if checkout else '压缩包',
               'app_version': revision[:12] if isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{7,64}', revision) else '未能读取'}
    if runtime_only:
        return runtime
    agents = read_json(home / '.skill-pilot/agents.json').get('onboarded', [])
    agents = agents if isinstance(agents, list) else []
    agents = [a for a in agents if isinstance(a, str)]
    known = {p['id']: p['name'] for p in PROFILES + ADDITIONAL_PROFILES}
    registry = read_json(home / '.skill-pilot/registry.json').get('skills', {})
    registry = registry if isinstance(registry, dict) else {}
    team = read_json(home / '.skill-pilot/team.json')
    return {**runtime,
            'modified': modified, 'tools': tools, 'agent_count': len(agents),
            'agent_types': sorted({known.get(a, '自定义 Agent') for a in agents}),
            'skill_count': len(registry), 'read_only': not write_allowed(home),
            'team_connected': bool(team.get('url') and team.get('key'))}


def bundle(home=None, error_id=None, note=''):
    home = Path(home or target_home())
    rows = recent(home)
    if error_id:
        rows = [row for row in rows if row['error_id'] == error_id]
        if not rows:
            raise ValueError('这条错误记录已不在本机，请重新选择或导出当前诊断。')
    report = {'environment': environment(home), 'environment_captured_at': datetime.now(timezone.utc).isoformat(),
              'errors': rows[:20], 'note': sanitize(note, home, 1500)}
    return {'report': report, 'text': json.dumps(report, ensure_ascii=False, indent=2)}
