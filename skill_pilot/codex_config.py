"""Lossless edits of only the inline entries recorded as ours."""
from pathlib import Path
import json
import re
import tomllib

CONFIG_PATH = '.codex/config.toml'


def read(home):
    from .scan import read_disabled
    return read_disabled(home / CONFIG_PATH, home)


def read_external(home, registry):
    """Read user preferences without treating our shared-root exclusions as theirs."""
    from .scan import read_disabled
    path = home / CONFIG_PATH
    original = path.read_bytes() if path.exists() else b''
    text, _ = render(original, registry.get('codex_config'), [])
    text = text.decode('utf-8')
    rules = read_disabled(path, home, text=text)
    # Retain the written entry location as well as its current resolved target.
    # Moving that entry must not accidentally invalidate a user's path disable.
    rules['entry_paths'] = []
    for entry in tomllib.loads(text).get('skills', {}).get('config', []):
        if entry.get('enabled') is False and 'path' in entry:
            raw = entry['path']
            location = home / raw[2:] if raw.startswith('~/') else Path(raw)
            if not location.is_absolute():
                location = path.parent / location
            rules['entry_paths'].append(str(location.absolute()))
    return rules


def copy_skills_config(source, destination, checked):
    config = source / CONFIG_PATH
    if not config.exists():
        return
    original = tomllib.loads(config.read_text()).get('skills', {})
    if set(original) - {'config'}:
        raise ValueError('技能设置出现新格式，需核对后才能复制。')
    rows = []
    for entry in original.get('config', []):
        fields = []
        for key, value in entry.items():
            if key == 'path' and isinstance(value, str) and Path(value).is_absolute() and Path(value).is_relative_to(source):
                value = str(destination / Path(value).relative_to(source))
            fields.append(key + ' = ' + (str(value).lower() if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)))
        rows.append('{ ' + ', '.join(fields) + ' }')
    path = checked(destination / CONFIG_PATH)
    checked(path.parent).mkdir(parents=True, exist_ok=True)
    path.write_text('[skills]\nconfig = [' + ', '.join(rows) + ']\n')


def array_span(text):
    section = re.search(r'(?m)^\[skills\][ \t\r]*(?:#[^\n]*)?$', text)
    if section is None:
        return None
    end = re.search(r'(?m)^\[', text[section.end():])
    stop = section.end() + end.start() if end else len(text)
    config = re.search(r'(?m)^config\s*=\s*\[', text[section.end():stop])
    if config is None:
        return None
    start = section.end() + config.end() - 1
    depth, quote, escaped, comment = 0, None, False, False
    for index in range(start, stop):
        char = text[index]
        if comment:
            comment = char != '\n'
        elif quote:
            if escaped:
                escaped = False
            elif char == '\\' and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char == '#':
            comment = True
        elif char in '\"\'':
            quote = char
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if depth == 0:
                return start, index
    raise ValueError('Codex 的配置文件没能读懂，文件没有改动。')


def render(original, ownership, desired):
    """Return bytes and ownership; preserve every pre-existing byte."""
    text = original.decode('utf-8')
    tomllib.loads(text)
    ownership = dict(ownership or {})
    fragments = dict(ownership.get('fragments', {}))
    for name in reversed(list(fragments)):
        fragment = fragments.pop(name)
        span = array_span(text)
        if span is None or text[span[0]:span[1]].count(fragment) != 1:
            raise ValueError('工具添加的 Codex 设置被外部改过，请刷新并核对后再操作。')
        a, b = span
        text = text[:a] + text[a:b].replace(fragment, '', 1) + text[b:]
    if not fragments and ownership.get('scaffold'):
        scaffold = ownership['scaffold']
        if scaffold in text:
            text = text.replace(scaffold, '', 1)
            ownership.pop('scaffold')
    existing = tomllib.loads(text).get('skills', {}).get('config', [])
    for name in sorted(set(desired) - set(fragments)):
        if any(e.get('name') == name and e.get('enabled') is False for e in existing):
            continue
        span = array_span(text)
        if span is None:
            if re.search(r'(?m)^\[skills\]', text):
                section = re.search(r'(?m)^\[skills\][^\n]*\n?', text)
                scaffold = 'config = []\n'
                text = text[:section.end()] + scaffold + text[section.end():]
            else:
                scaffold = ('\n' if text and not text.endswith('\n') else '') + '[skills]\nconfig = []\n'
                text += scaffold
            ownership['scaffold'] = scaffold
            span = array_span(text)
        a, b = span
        inside = text[a + 1:b]
        # Entries are inline tables; an existing trailing comma needs no second comma.
        clean = re.sub(r'#[^\n]*', '', inside).rstrip()
        prefix = ', ' if clean and not clean.endswith(',') else ' '
        fragment = prefix + '{ name = ' + json.dumps(name, ensure_ascii=False) + ', enabled = false }'
        text = text[:b] + fragment + text[b:]
        fragments[name] = fragment
    result = text.encode('utf-8')
    tomllib.loads(text)
    ownership['fragments'] = fragments
    return result, ownership


def write(home, registry, desired, checked):
    path = checked(home / CONFIG_PATH)
    exists = path.exists()
    original = path.read_bytes() if exists else b''
    try:
        result, ownership = render(original, registry.get('codex_config'), desired)
    except tomllib.TOMLDecodeError as error:
        raise ValueError('Codex 的配置文件没能安全修改，原有文件保持原样。') from error
    if result != original:
        checked(path.parent).mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(result)
            tomllib.loads(path.read_text())
        except Exception as error:
            if exists:
                path.write_bytes(original)
            elif path.exists():
                path.unlink()
            raise ValueError('Codex 的配置文件改坏了，已经自动恢复原样。') from error
    registry['codex_config'] = ownership
