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
from pathlib import Path, PureWindowsPath
import re
import subprocess
import threading

from .store import PROJECT

APP_VERSION = '1.0.0'
RECORD = '.skill-pilot-install.json'
BUNDLE_DIRS = ('skill_pilot', 'scripts', 'assets', 'docs', 'skill', 'tests', '.github')
OFFICIAL_DIRS = ('skill_pilot', 'scripts', 'assets', 'skill', 'tests', '.github')
ROOT_FILES = (
    '.gitignore',
    '.gitattributes',
    '.claude/launch.json',
    'AGENTS.md',
    'CLAUDE.md',
    'DESIGN.md',
    'PLAN.md',
    'PRODUCT.md',
    'README.md',
    'WINDOWS.md',
    'skill-pilot',
)
OFFICIAL_DOC_FILES = (
    'docs/STATUS.md',
    'docs/brand/skill-pilot-brandkit.png',
    'docs/dependency-decisions.json',
    'docs/evidence.json',
    'docs/lark-paired-snapshot.json',
    'docs/path-references.md',
    'docs/sharing-config.md',
    'docs/source-attribution.md',
    'docs/spikes.md',
    'docs/team-hub.md',
)
OFFICIAL_FILES = ROOT_FILES + OFFICIAL_DOC_FILES
_OFFICIAL_FILE_SET = frozenset(OFFICIAL_FILES)
REQUIRED_FILES = (
    'skill_pilot/cli.py',
    'skill_pilot/web/server.py',
    'skill_pilot/web/static/app.js',
    'scripts/install.py',
    'assets/SkillPilot.icns',
    'README.md',
    'docs/STATUS.md',
    'skill-pilot',
)
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


def _normalized_bytes_digest(data):
    if b'\0' not in data:
        data = data.replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()


def _normalized_digest(path):
    return _normalized_bytes_digest(path.read_bytes())


def is_safe_manifest_path(name):
    if not isinstance(name, str) or not name or name.startswith(('/', '\\')):
        return False
    win_path = PureWindowsPath(name)
    if (win_path.drive or win_path.root or Path(name).is_absolute()
            or '..' in Path(name).parts or '..' in win_path.parts):
        return False
    return True


def is_official_path(name):
    if not is_safe_manifest_path(name):
        return False
    relative = Path(name)
    posix = relative.as_posix()
    if posix in _OFFICIAL_FILE_SET:
        return True
    parts = relative.parts
    if not parts or parts[0] not in OFFICIAL_DIRS:
        return False
    if '__pycache__' in parts or relative.name == '.DS_Store' or relative.suffix == '.pyc':
        return False
    return True


def _verify_manifest_files(project, manifest):
    if 'files' not in manifest:
        return 'absent'
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        return 'invalid'
    if not all(name in files for name in REQUIRED_FILES):
        return 'invalid'
    for directory in OFFICIAL_DIRS:
        root = project / directory
        if root.is_dir() and not root.is_symlink():
            try:
                for path in root.rglob('*'):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(project).as_posix()
                    if is_official_path(rel) and rel not in files:
                        return 'invalid'
            except OSError:
                return 'invalid'
    for name in OFFICIAL_FILES:
        if _safe_project_file(project, name) is not None and name not in files:
            return 'invalid'
    for name, expected in files.items():
        if not is_safe_manifest_path(name):
            return 'invalid'
        if not is_official_path(name):
            # Ignore private notes accidentally recorded by older installers.
            continue
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
        if not name or not is_official_path(name):
            continue
        path = _safe_project_file(project, name)
        if path is None:
            continue
        items[path.relative_to(project).as_posix()] = path
    return sorted(items.items())


def _scan_bundle_paths(project):
    items = {}
    try:
        candidates = []
        for directory in OFFICIAL_DIRS:
            root = project / directory
            if root.is_dir() and not root.is_symlink():
                for path in root.rglob('*'):
                    if '__pycache__' not in path.parts:
                        candidates.append(path)
        for name in OFFICIAL_FILES:
            candidates.append(project / name)
        for path in candidates:
            try:
                rel = path.relative_to(project).as_posix()
            except ValueError:
                continue
            if not is_official_path(rel):
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
            if not is_official_path(name):
                continue
            path = _safe_project_file(project, name)
            if path is None:
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


def read_git_tree_version(project, revision):
    """Compute the official release version directly from a git commit tree object."""
    project = Path(project)
    if not isinstance(revision, str) or not HEX_RE.fullmatch(revision.strip()):
        return ''
    try:
        listed = subprocess.run(
            ['git', '-C', str(project), 'ls-tree', '-r', '-z', revision.strip()],
            env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    if listed.returncode != 0:
        return ''
    entries = []
    for raw in listed.stdout.split(b'\0'):
        if not raw or b'\t' not in raw:
            continue
        meta, raw_path = raw.split(b'\t', 1)
        parts = meta.decode('ascii', 'replace').split()
        if len(parts) != 3 or parts[1] != 'blob' or parts[0] not in ('100644', '100755'):
            continue
        rel = raw_path.decode('utf-8', 'surrogateescape')
        if not is_official_path(rel):
            continue
        entries.append((rel, parts[2]))
    if not entries:
        return ''
    entries.sort(key=lambda item: item[0])
    batch_input = ('\n'.join(oid for _, oid in entries) + '\n').encode('ascii')
    try:
        batched = subprocess.run(
            ['git', '-C', str(project), 'cat-file', '--batch'],
            env=_git_env(), input=batch_input, capture_output=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    if batched.returncode != 0:
        return ''
    output = batched.stdout
    pos = 0
    digest = hashlib.sha256()
    base_version = APP_VERSION
    for rel, _ in entries:
        nl = output.find(b'\n', pos)
        if nl == -1:
            return ''
        header = output[pos:nl].decode('ascii', 'replace').split()
        if len(header) != 3 or header[1] != 'blob':
            return ''
        try:
            size = int(header[2])
        except ValueError:
            return ''
        start = nl + 1
        end = start + size
        if end > len(output):
            return ''
        data = output[start:end]
        pos = end + 1
        if rel == 'skill_pilot/version.py':
            match = APP_VERSION_RE.search(data.decode('utf-8', 'replace'))
            if match:
                base_version = match.group(1)
        digest.update(rel.encode('utf-8') + b'\0' + _normalized_bytes_digest(data).encode('ascii') + b'\n')
    return format_version(fallback_digest=digest.hexdigest(), base_version=base_version)


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
