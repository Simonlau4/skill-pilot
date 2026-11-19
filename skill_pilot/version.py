"""Cross-platform readable application version helpers.

Works for both git source checkouts and GitHub source ZIP installations,
without depending on maintainer paths or requiring git to be installed.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .store import PROJECT

RECORD = '.skill-pilot-install.json'
HEX_RE = re.compile(r'^[0-9a-fA-F]{7,64}$')
DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


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
        return text.replace('-', '.')
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return ''
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime('%Y.%m.%d')


def code_fingerprint(project=PROJECT):
    """Deterministic cross-platform digest of bundled application code."""
    project = Path(project)
    package = project / 'skill_pilot'
    if not package.is_dir():
        return ''
    try:
        digest = hashlib.sha256()
        files = sorted(
            (path.relative_to(package).as_posix(), path)
            for path in package.rglob('*')
            if path.is_file() and path.suffix in {'.py', '.json'} and '__pycache__' not in path.parts
        )
        for relative, path in files:
            digest.update(relative.encode('utf-8') + b'\0' + path.read_bytes())
        bundled = project / 'skill/skill-pilot'
        if bundled.is_dir():
            from .normalization import tree
            digest.update(tree(bundled).encode('utf-8'))
        return digest.hexdigest()
    except OSError:
        return ''


def format_version(date='', revision='', fallback_digest=''):
    day = format_date(date)
    rev = short_id(revision)
    if day and rev:
        return f'v{day}-{rev}'
    if day:
        return f'v{day}'
    if rev:
        return rev
    fallback = short_id(fallback_digest)
    return f'初始版-{fallback}' if fallback else ''


def _read_manifest(project):
    try:
        data = json.loads((Path(project) / RECORD).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_git_head(project):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    try:
        result = subprocess.run(
            ['git', '-C', str(project), 'show', '-s', '--format=%H%n%cI', 'HEAD'],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
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


def read_build(project=PROJECT):
    project = Path(project)
    checkout = (project / '.git').exists()
    manifest = _read_manifest(project)
    revision = manifest.get('revision', '') if isinstance(manifest.get('revision'), str) else ''
    date = manifest.get('date', '') if isinstance(manifest.get('date'), str) else ''
    if not HEX_RE.fullmatch(revision.strip()):
        revision = ''
    if checkout:
        git_rev, git_date = _read_git_head(project)
        if git_rev:
            revision, date = git_rev, git_date
    fallback = '' if (short_id(revision) or format_date(date)) else code_fingerprint(project)
    return {
        'install_mode': '源码' if checkout else '压缩包',
        'checkout': checkout,
        'revision': revision.strip().lower() if revision else '',
        'date': date.strip() if date else '',
        'version': format_version(date, revision, fallback),
    }


# Keep the loaded process identity even if an upgrade replaces its files on disk.
RUNNING_BUILD = read_build(PROJECT)


def running_build():
    return dict(RUNNING_BUILD)
