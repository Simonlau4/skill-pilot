"""Discover assistants from installation evidence, without reading skill bodies."""
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import profiles
from .scan import SKIP_DISCOVERY
from .store import guard, write_allowed

CACHE = '.skill-pilot/agent-discovery.json'
SKIP = SKIP_DISCOVERY | {'memories', 'plugins', 'builtin', 'skills-marketplace',
                         'connectors', 'connectors-marketplace'}


def read(home):
    try:
        data = json.loads((Path(home) / CACHE).read_text())
        if data['schema_version'] != 1 or not isinstance(data['candidates'], list):
            raise ValueError()
        for item in data['candidates']:
            if (not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', item['id']) or
                    not isinstance(item['name'], str) or not item['name'].strip() or
                    not isinstance(item['skill_roots'], list) or not item['skill_roots'] or
                    any(not isinstance(r, str) or not r or Path(r) == Path('.') or Path(r).is_absolute() or
                        '..' in Path(r).parts for r in item['skill_roots'])):
                raise ValueError()
        if not isinstance(data.get('checked_at'), str):
            raise ValueError()
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {'schema_version': 1, 'checked_at': '', 'candidates': []}


def find(home, full=False, previous=()):
    home = Path(home).resolve()
    catalog = profiles.all_profiles(home)
    known = {p['id'] for p in catalog}
    protected = {home / root for root in profiles.readonly_roots(home)}
    roots = {home / r for p in catalog for r in p['skill_roots']}
    found = {}
    for p in catalog:
        if ((profiles.evidence(p, home) or any((home / r).is_dir() for r in p['skill_roots'])) and
                all(not (home / r).exists() or (home / r).is_dir() for r in p['skill_roots'])):
            found[p['id']] = {'id': p['id'], 'name': p['name'], 'skill_roots': p['skill_roots'],
                              'profile_available': True,
                              'reason': '已识别到这个 agent，可以接入；还没有用户技能也能使用。'}

    def candidate(path):
        relative = path.relative_to(home)
        identifier = re.sub(r'[^a-z0-9_-]', '-', relative.parts[0].lstrip('.').lower())
        if (identifier in known or identifier in found or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', identifier) or
                path.name != 'skills' or not relative.parts[0].startswith('.') or
                any(part in SKIP for part in relative.parts) or
                len(relative.parts) > 4 or path in roots or
                any(path.is_relative_to(p) for p in protected) or
                not path.resolve().is_relative_to(home) or not path.is_dir()):
            return
        if any(child.resolve().is_relative_to(home) and (child / 'SKILL.md').is_file() for child in path.iterdir()):
            found[identifier] = {'id': identifier,
                'name': profiles.CATALOG['discovery_names'].get(relative.parts[0], relative.parts[0].lstrip('.').capitalize()),
                'skill_roots': [str(relative)], 'profile_available': False,
                'reason': '这个目录直属放着技能文件夹，可能属于一个新 agent；接入时会核对技能。'}

    # Remember deeper findings on the cheap check, but verify that they still exist.
    for item in previous:
        for relative in item['skill_roots']:
            candidate(home / relative)

    def visit(path, depth):
        if (path.is_symlink() or path in roots or depth > 4 or
                any(path.is_relative_to(p) for p in protected)):
            return
        if path.name == 'skills':
            candidate(path)
            return
        if full and depth < 4:
            for child in sorted(path.iterdir()):
                if child.name not in SKIP and child.is_dir():
                    visit(child, depth + 1)

    for path in sorted(home.iterdir()):
        identifier = re.sub(r'[^a-z0-9_-]', '-', path.name.lstrip('.').lower())
        if (not path.name.startswith('.') or path.name in SKIP or
                identifier in known or not path.is_dir() or path.is_symlink()):
            continue
        candidate(path / 'skills')
        if full:
            visit(path, 1)
    return {'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'candidates': list(found.values())}


def save(home, data):
    home = Path(home).resolve()
    if write_allowed(home):
        path = guard(home / CACHE, home)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.agent-discovery-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
