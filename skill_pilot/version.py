"""Cross-platform readable application version helpers.

Computes a unified version string (`v<APP_VERSION>-<7-char bundle id>`) from the
installed application files so git checkouts, fresh GitHub ZIP downloads, and
self-updated ZIP installations report the exact same version for the same release,
without depending on maintainer paths or requiring git to be installed.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading

from .store import PROJECT

APP_VERSION = '1.0.0'
RECORD = '.skill-pilot-install.json'
BUNDLE_DIRS = ('skill_pilot', 'scripts', 'assets', 'docs', 'skill', 'tests', '.github')
ROOT_FILES = ('.gitignore', '.gitattributes', '.claude/launch.json', 'skill-pilot')
REQUIRED_FILES = ('skill_pilot/cli.py', 'skill_pilot/web/server.py', 'skill_pilot/web/static/app.js')
HEX_RE = re.compile(r'^[0-9a-fA-F]{7,64}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
APP_VERSION_RE = re.compile(r"""^APP_VERSION\s*=\s*['"]([0-9]+\.[0-9]+(?:\.[0-9]+)?)['"]""", re.M)
_RUNNING_LOCK = threading.Lock()


def short_id(value):
    if not isinstance(value, str):
        return ''
    text = value.strip()
    return text[:7].lower() if HEX_RE.fullmatch(text) else ''


def format_date(value):
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if not text:
        return ''
    if DAY_RE.fullmatch(text):
        try:
            parsed = datetime.strptime(text, '%Y-%m-%d')
        except ValueError:
            return ''
        return parsed.strftime('%Y.%m.%d')
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime('%Y.%m.%d')
    except (ValueError, OverflowError, OSError):
        return ''


def read_app_version(project=PROJECT):
    try:
        text = (Path(project) / 'skill_pilot/version.py').read_text(encoding='utf-8')
    except OSError:
        return APP_VERSION
    match = APP_VERSION_RE.search(text)
    return match.group(1) if match else APP_VERSION


def _safe_project_file(root, name):
    if not isinstance(name, str) or not name:
        return None
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        return None
    path = root / relative
    try:
        if not path.is_relative_to(root):
            return None
        if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)):
            return None
        if not path.is_file():
            return None
    except OSError:
        return None
    return path


def _file_stamp(path):
    return [hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o111]


def _normalized_digest(path):
    data = path.read_bytes()
    if b'\0' not in data:
        data = data.replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()


def _verify_manifest_files(project, manifest):
    if 'files' not in manifest:
        return 'absent'
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        return 'invalid'
    if not all(name in files for name in REQUIRED_FILES):
        return 'invalid'
    expected_on_disk = {rel for rel, _ in _scan_bundle_paths(project)}
    if not expected_on_disk or not expected_on_disk.issubset(files):
        return 'invalid'
    for name, expected in files.items():
        if (not isinstance(expected, list) or len(expected) != 2
                or not isinstance(expected[0], str) or not SHA256_RE.fullmatch(expected[0])
                or type(expected[1]) is not int):
            return 'invalid'
        path = _safe_project_file(project, name)
        if path is None:
            return 'invalid'
        try:
            if _file_stamp(path) != expected:
                return 'invalid'
        except OSError:
            return 'invalid'
    return 'valid'


def _git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    return env


def _git_tracked_paths(project):
    try:
        result = subprocess.run(
            ['git', '-C', str(project), 'ls-files', '-z'],
            env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
            text=True, errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    items = {}
    for name in result.stdout.split('\0'):
        if not name:
            continue
        path = _safe_project_file(project, name)
        if path is None or path.name == '.DS_Store' or path.suffix == '.pyc':
            continue
        items[path.relative_to(project).as_posix()] = path
    return sorted(items.items())


def _scan_bundle_paths(project):
    items = {}
    try:
        candidates = []
        for directory in BUNDLE_DIRS:
            root = project / directory
            if root.is_dir() and not root.is_symlink():
                for path in root.rglob('*'):
                    if '__pycache__' not in path.parts:
                        candidates.append(path)
        candidates.extend(project.glob('*.md'))
        for name in ROOT_FILES:
            candidates.append(project / name)
        for path in candidates:
            if path.name == '.DS_Store' or path.suffix == '.pyc':
                continue
            try:
                rel = path.relative_to(project).as_posix()
            except ValueError:
                continue
            checked = _safe_project_file(project, rel)
            if checked is not None:
                items[rel] = checked
    except OSError:
        return []
    return sorted(items.items())


def bundle_fingerprint(project=PROJECT, *, file_names=None):
    """Cross-platform normalized SHA-256 over the official release file set."""
    project = Path(project)
    if file_names is not None:
        pairs = []
        for name in sorted(set(file_names)):
            path = _safe_project_file(project, name)
            if path is None or path.name == '.DS_Store' or path.suffix == '.pyc':
                continue
            pairs.append((path.relative_to(project).as_posix(), path))
    elif (project / '.git').exists():
        pairs = _git_tracked_paths(project)
        if pairs is None:
            pairs = _scan_bundle_paths(project)
    else:
        pairs = _scan_bundle_paths(project)
    if not pairs:
        return ''
    try:
        digest = hashlib.sha256()
        for rel, path in pairs:
            digest.update(rel.encode('utf-8') + b'\0' + _normalized_digest(path).encode('ascii') + b'\n')
        return digest.hexdigest()
    except OSError:
        return ''


def format_version(date='', revision='', fallback_digest='', base_version=''):
    base = base_version.strip() if isinstance(base_version, str) else ''
    fallback = short_id(fallback_digest)
    if base and fallback:
        return f'v{base}-{fallback}'
    day = format_date(date)
    rev = short_id(revision)
    if day and rev:
        return f'v{day}-{rev}'
    if day:
        return f'v{day}'
    if rev:
        return rev
    return f'v{APP_VERSION}-{fallback}' if fallback else ''


def _read_manifest(project):
    try:
        data = json.loads((Path(project) / RECORD).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _read_git_head(project):
    try:
        result = subprocess.run(
            ['git', '-C', str(project), 'show', '-s', '--format=%H%n%cI', 'HEAD'],
            env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
            text=True, errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError):
        return '', ''
    if result.returncode != 0:
        return '', ''
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    revision = lines[0] if lines and HEX_RE.fullmatch(lines[0]) else ''
    date = lines[1] if len(lines) > 1 else ''
    return revision, date


def read_build(project=PROJECT, *, file_names=None):
    project = Path(project)
    checkout = (project / '.git').exists()
    manifest = _read_manifest(project)
    files_state = _verify_manifest_files(project, manifest)
    revision = ''
    date = ''
    if checkout:
        git_rev, git_date = _read_git_head(project)
        if git_rev:
            revision, date = git_rev, git_date
        elif files_state == 'valid':
            revision = manifest.get('revision', '') if isinstance(manifest.get('revision'), str) else ''
            date = manifest.get('date', '') if isinstance(manifest.get('date'), str) else ''
    elif files_state in {'valid', 'absent'}:
        revision = manifest.get('revision', '') if isinstance(manifest.get('revision'), str) else ''
        date = manifest.get('date', '') if isinstance(manifest.get('date'), str) else ''
    revision = revision.strip().lower() if short_id(revision) else ''
    date = date.strip() if format_date(date) else ''

    if file_names is not None:
        names = file_names
    elif checkout:
        tracked = _git_tracked_paths(project)
        names = [rel for rel, _ in tracked] if tracked is not None else (
            sorted(manifest['files']) if files_state == 'valid' else None)
    else:
        names = sorted(manifest['files']) if files_state == 'valid' else None
    digest = bundle_fingerprint(project, file_names=names)
    if digest:
        display = format_version(fallback_digest=digest, base_version=read_app_version(project))
    elif not checkout and file_names is None and files_state == 'absent' and (revision or date):
        # Legacy manifest fallback only when actual program files cannot be read.
        display = format_version(date, revision)
    else:
        display = ''

    return {
        'install_mode': '源码' if checkout else '压缩包',
        'checkout': checkout,
        'revision': revision,
        'date': date,
        'manifest_valid': files_state == 'valid',
        'version': display,
    }


# Keep the loaded process identity even if an upgrade replaces its files on disk.
RUNNING_BUILD = read_build(PROJECT)


def running_build():
    with _RUNNING_LOCK:
        return dict(RUNNING_BUILD)


def set_running_version(new_version, *, date=None):
    with _RUNNING_LOCK:
        if isinstance(new_version, str) and new_version.strip():
            RUNNING_BUILD['version'] = new_version.strip()
        if isinstance(date, str):
            RUNNING_BUILD['date'] = date.strip()
