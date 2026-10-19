"""User-owned tags, independent of AI display labels."""
import json
import threading
import unicodedata
from pathlib import Path

from .labels import atomic_write
from .store import guard

_lock = threading.Lock()
UNREADABLE = '本机的标签文件读不懂，为保留原文件，这次没有改动，请先检查标签文件。'


def clean_tag(value):
    if not isinstance(value, str):
        raise ValueError('请填写 1–20 个字的标签。')
    value = value.strip()
    while value.startswith(('#', '＃')):
        value = value.lstrip('#＃').strip()
    if any(unicodedata.category(char) in {'Cc', 'Cf', 'Zl', 'Zp'} for char in value):
        raise ValueError('标签里不能有换行或看不见的字符，请修改后重试。')
    if not 1 <= len(value) <= 20:
        raise ValueError('每个标签需要 1–20 个字，请修改后重试。')
    return value


def _read(home):
    try:
        value = json.loads((Path(home) / '.skill-pilot/tags.json').read_text())
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
                or not isinstance(value.get('skills'), dict)):
            raise ValueError
        for items in value['skills'].values():
            if (not isinstance(items, list) or len(items) > 10 or any(clean_tag(t) != t for t in items)
                    or len({t.casefold() for t in items}) != len(items)):
                raise ValueError
        return value['skills']
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(UNREADABLE) from None


def read(home):
    try:
        return _read(home)
    except ValueError:
        return {}


def update(home, body):
    from .operations import load
    home = Path(home).resolve()
    with _lock:
        guard(home / '.skill-pilot/tags.json', home)
        if not isinstance(body, dict) or ('add' in body) == ('remove' in body):
            raise ValueError('每次只能添加或去掉一个标签，请选择其中一种操作。')
        names = body.get('skills')
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise ValueError('请至少选择一个技能。')
        if len(set(names)) != len(names):
            raise ValueError('选中的技能重复了，请重新选择。')
        registry = load(home)[1]['skills']
        for name in names:
            if name not in registry:
                raise ValueError(f'技能“{name}”不在登记表里，请刷新后重试。')
        adding = 'add' in body
        tag = clean_tag(body['add'] if adding else body['remove'])
        current = _read(home)
        key = tag.casefold()
        tag = next((t for items in current.values() for t in items if t.casefold() == key), tag)
        result = {}
        for name in names:
            items = list(current.get(name, []))
            if adding and not any(t.casefold() == key for t in items):
                if len(items) >= 10:
                    raise ValueError(f'技能“{name}”已有 10 个标签，请先去掉一个再添加。')
                items.append(tag)
            elif not adding:
                items = [t for t in items if t.casefold() != key]
            result[name] = items
        if any(current.get(n, []) != items for n, items in result.items()):
            current.update(result)
            atomic_write(home, 'tags.json', json.dumps({'version': 1, 'skills': current}, ensure_ascii=False) + '\n')
        return {'tags': result}
