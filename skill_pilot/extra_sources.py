"""用户自己记下的额外技能文件夹。公开代码里不写私人目录。"""
import json
from pathlib import Path

from . import profiles

FILE_NAME = 'extra-sources.json'


def _home(home):
    if home is None:
        from .store import target_home
        return target_home()
    return Path(home).resolve()


def _locate(home, text):
    """把用户填的位置换成主目录里的相对路径。`~/` 按当前管理的主目录展开。"""
    home = Path(home).resolve()
    raw = text.strip() if isinstance(text, str) else ''
    if not raw or '\x00' in raw or '\n' in raw or '\r' in raw:
        return None
    if raw == '~':
        candidate = home
    elif raw.startswith('~/'):
        candidate = home / raw[2:]
    else:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = home / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved == home or not resolved.is_relative_to(home):
        return None
    relative = resolved.relative_to(home)
    if '..' in relative.parts:
        return None
    return relative.as_posix()


def _managed(home):
    home = Path(home).resolve()
    roots = [(home / '.skill-pilot').resolve()]
    for profile in profiles.all_profiles(home):
        for relative in profile.get('skill_roots', []):
            roots.append((home / relative).resolve())
        for item in profile.get('readonly_roots', []):
            relative = item[0] if isinstance(item, (list, tuple)) else item
            roots.append((home / relative).resolve())
    return roots


def _blocked(folder, home):
    resolved = Path(folder).resolve()
    return any(resolved == root or resolved.is_relative_to(root) for root in _managed(home))


def _read(home):
    path = Path(home).resolve() / '.skill-pilot' / FILE_NAME
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return []
    items = value.get('paths') if isinstance(value, dict) else None
    if not isinstance(items, list):
        return []
    found = []
    for item in items:
        relative = _locate(home, item)
        if relative and relative not in found:
            found.append(relative)
    return found


def derive(home):
    """从已有登记里找出「本机源码」并且文件夹还在的位置。不写入。"""
    home = _home(home)
    try:
        registry = json.loads((home / '.skill-pilot/registry.json').read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return []
    skills = registry.get('skills') if isinstance(registry, dict) else None
    if not isinstance(skills, dict):
        return []
    found = []
    for record in skills.values():
        source = record.get('source') if isinstance(record, dict) else None
        if not isinstance(source, dict) or source.get('source') != '本机源码':
            continue
        relative = _locate(home, source.get('source_path'))
        if not relative:
            continue
        folder = home / relative
        if folder.is_file() and folder.name == 'SKILL.md':
            folder = folder.parent
            relative = folder.relative_to(home).as_posix()
        if not (folder / 'SKILL.md').is_file() or _blocked(folder, home):
            continue
        if relative not in found:
            found.append(relative)
    return found


def paths(home=None):
    home = _home(home)
    saved = _read(home)
    return derive(home) if saved is None else saved


def _write(home, items):
    from .store import guard, write_json
    home = Path(home).resolve()
    write_json(home / '.skill-pilot' / FILE_NAME, {'paths': items}, lambda path: guard(path, home))


def ensure(home=None):
    """第一次发现已有来源时，记到用户自己的设置里。已有这份设置就不再重算。"""
    home = _home(home)
    saved = _read(home)
    if saved is not None:
        return saved
    derived = derive(home)
    if not derived or not (home / '.skill-pilot').is_dir():
        return derived
    try:
        from .store import write_allowed
        if write_allowed(home):
            _write(home, derived)
    except (OSError, ValueError):
        return derived
    return derived


def add(home, folder):
    home = _home(home)
    relative = _locate(home, folder)
    if relative is None:
        if isinstance(folder, str) and folder.strip():
            raise ValueError('这个文件夹不在主目录里，没有记下。')
        raise ValueError('请填写一个文件夹。')
    current = paths(home)
    if relative in current:
        return '这个文件夹已经记下了。'
    _write(home, [*current, relative])
    return '已记下这个文件夹。'


def remove(home, folder):
    home = _home(home)
    relative = _locate(home, folder)
    current = paths(home)
    if relative is None or relative not in current:
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError('请填写一个文件夹。')
        return '没有记下这个文件夹。'
    _write(home, [item for item in current if item != relative])
    return '已不再额外查看这个文件夹。'
