"""Explicit, authenticated exchanges with a team's skill-list service."""
import copy
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path

from . import labels, sharing
from .store import guard
from .team_hub import clean_config

MAX_BODY = 1024 * 1024
TIMEOUT = 10
FORMAT_ERROR = '分享站的名单格式不对，请联系分享者重新上传。'


def _settings(value):
    url, key = value.get('url'), value.get('key')
    if not isinstance(url, str) or not isinstance(key, str):
        raise ValueError('请填写分享站地址和自己的口令。')
    url, key = url.strip().rstrip('/'), key.strip()
    try:
        address = urllib.parse.urlsplit(url)
        port = address.port
        valid = (address.scheme in {'http', 'https'} and address.hostname and
                 address.username is None and address.password is None and
                 not any(char.isspace() or ord(char) < 32 for char in url) and
                 '?' not in url and '#' not in url and (port is None or port > 0))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('分享站地址只支持 http 或 https，不能带账号、密码或查询参数。')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('请粘贴管理员给你的完整口令。')
    return {'url': url, 'key': key}


def settings(home):
    path = Path(home) / '.skill-pilot/team.json'
    if not path.exists():
        return {}
    try:
        return _settings(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, AttributeError):
        raise ValueError('分享站设置没能读懂，请重新填写地址和口令。') from None


def _request(config, path, method='GET', data=None):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    if body is not None and len(body) > MAX_BODY:
        raise ValueError('名单超过 1 MB，请减少分享的技能后重试。')
    try:
        request = urllib.request.Request(config['url'] + path, data=body, method=method,
            headers={'Authorization': 'Bearer ' + config['key'], 'Content-Type': 'application/json'})
        with urllib.request.build_opener(labels.NoRedirect).open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError('分享站返回的内容超过 1 MB，请联系管理员检查。')
        def invalid_constant(value):
            raise ValueError
        def redact(value):
            if isinstance(value, str):
                return value.replace(config['key'], '[已隐藏]')
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value
        try:
            result = json.loads(raw.decode('utf-8'), parse_constant=invalid_constant)
            if not isinstance(result, dict):
                raise ValueError
            # Even a misconfigured service must not echo our credential to the page or history.
            result = redact(result)
        except (ValueError, RecursionError):
            raise ValueError(FORMAT_ERROR) from None
        return result
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        message = ('口令不对或已经失效，请向管理员核对。' if status in {401, 403} else
                   '分享站地址发生了跳转，请填写最终地址后重试。' if 300 <= status < 400 else
                   '这位成员还没有分享，或已经撤回。' if status == 404 else
                   '名单超过 1 MB，请减少分享的技能后重试。' if status == 413 else
                   '连不上分享站，请检查地址，或请管理员确认分享站已开启。')
        raise ValueError(message) from None
    except UnicodeError:
        raise ValueError(FORMAT_ERROR) from None
    except (OSError, HTTPException):
        raise ValueError('连不上分享站，请检查地址和网络，或请管理员确认分享站已开启。') from None


def _member(value):
    if (not isinstance(value, dict) or not isinstance(value.get('id'), str) or
            not re.fullmatch(r'[0-9a-f]{32}', value['id']) or
            not isinstance(value.get('name'), str) or not value['name'].strip()):
        raise ValueError(FORMAT_ERROR)
    return {key: value[key] for key in ('id', 'name')}


def _connection(home):
    config = settings(home)
    if not config:
        raise ValueError('请先在设置里填写分享站地址和自己的口令。')
    return config


def _me(config):
    result = _request(config, '/api/me')
    if type(result.get('api')) is not int or result['api'] != 1:
        raise ValueError('分享站和你的 Skill Pilot 版本不匹配。')
    if result.get('service') != 'skill-pilot-team':
        raise ValueError('这个地址不是 Skill Pilot 分享站，请核对地址。')
    return _member(result.get('member'))


def _summary(row):
    if (not isinstance(row, dict) or not isinstance(row.get('updated_at'), str) or
            type(row.get('skill_count')) is not int or row['skill_count'] < 0 or
            not isinstance(row.get('agents'), list) or
            any(not isinstance(agent, dict) or
                any(not isinstance(agent.get(key), str) for key in ('id', 'name')) for agent in row['agents'])):
        raise ValueError(FORMAT_ERROR)
    return {'member': _member(row.get('member')), 'updated_at': row['updated_at'],
            'skill_count': row['skill_count'],
            'agents': [{key: agent[key] for key in ('id', 'name')} for agent in row['agents']]}


def _shares(config):
    result = _request(config, '/api/shares')
    if not isinstance(result.get('shares'), list):
        raise ValueError(FORMAT_ERROR)
    return [_summary(row) for row in result['shares']]


def status(home, config=None, strict=True):
    result = {'url': '', 'has_key': False, 'configured': False, 'member': None, 'share': None}
    try:
        config = settings(home) if config is None else config
        public_url = config.get('url', '').replace(config['key'], '[已隐藏]') if config else ''
        result.update(url=public_url, has_key=bool(config.get('key')), configured=bool(config))
        if config:
            member = _me(config)
            share = next((row for row in _shares(config) if row['member']['id'] == member['id']), None)
            result.update(member=member, share=share)
    except (ValueError, OSError) as error:
        if strict:
            raise
        result['error'] = str(error) if isinstance(error, ValueError) else '没能读取团队分享，请稍后重试。'
    return result


def save_settings(home, body):
    guard(Path(home) / '.skill-pilot', home)
    value = {'url': body.get('url'), 'key': body.get('key', '')}
    if value['key'] == '':
        value['key'] = settings(home).get('key', '')
    config = _settings(value)
    result = status(home, config)
    # Failed connections leave the previous working settings intact.
    labels.atomic_write(home, 'team.json', json.dumps(config, ensure_ascii=False) + '\n', 0o600)
    return {**result, 'message': '分享站已连接，设置已保存。'}


def upload_preview(home, agent=None, exclude=None):
    data = sharing.export_config(home, agent)
    rows = {row['name']: row for row in data['skills']}
    exclude = [] if exclude is None else exclude
    if (not isinstance(exclude, list) or any(not isinstance(name, str) or name not in rows for name in exclude)):
        raise ValueError('请从本次分享的名单里选择要取消的技能。')
    selected = set(rows) - set(exclude)
    retained = {}
    pending = list(sorted(selected))
    while pending:
        name = pending.pop()
        for dependency in rows[name]['requires']:
            if dependency in exclude:
                retained.setdefault(dependency, set()).add(name)
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    data['skills'] = [row for row in data['skills'] if row['name'] in selected]
    return {'config': clean_config(data), 'retained': [
        {'name': name, 'required_by': sorted(parents),
         'reason': '这些技能需要它，已自动保留：' + '、'.join(sorted(parents))}
        for name, parents in sorted(retained.items())]}


def upload(home, data):
    guard(Path(home) / '.skill-pilot', home)
    config = _connection(home)
    _me(config)
    result = _request(config, '/api/shares/me', 'PUT', clean_config(data))
    if (not isinstance(result.get('updated_at'), str) or type(result.get('skill_count')) is not int or
            result['skill_count'] != len(data['skills'])):
        raise ValueError(FORMAT_ERROR)
    return {'updated_at': result['updated_at'], 'skill_count': result['skill_count'], 'message': '技能名单已分享。'}


def withdraw(home):
    guard(Path(home) / '.skill-pilot', home)
    config = _connection(home)
    _me(config)
    _request(config, '/api/shares/me', 'DELETE')
    return {'message': '已撤回自己的分享。'}


def shares(home):
    config = _connection(home)
    _me(config)
    return {'shares': _shares(config)}


def share(home, member):
    if not isinstance(member, str) or not re.fullmatch(r'[0-9a-f]{32}', member):
        raise ValueError('请选择分享列表里的成员。')
    config = _connection(home)
    _me(config)
    result = _request(config, '/api/shares/' + member)
    identity = _member(result.get('member'))
    if identity['id'] != member or not isinstance(result.get('updated_at'), str):
        raise ValueError(FORMAT_ERROR)
    try:
        data = clean_config(result.get('config'))
        for row in data['skills']:
            source = row['source']
            if any(not isinstance(source[field], str) for field in ('url', 'reason') if field in source):
                raise ValueError
            if not source.get('url', '').startswith('https://'):
                source.pop('url', None)
    except (ValueError, TypeError, KeyError):
        raise ValueError(FORMAT_ERROR) from None
    return {'member': identity, 'updated_at': result['updated_at'], 'config': data}


def detail(home, member):
    result = share(home, member)
    statuses = sharing.local_status(result['config'], home)
    for row in result['config']['skills']:
        row['local'] = statuses[row['name']]
    return result


def import_preview(home, member, skills, agents=None):
    result = share(home, member)
    data = result['config']
    rows = {row['name']: row for row in data['skills']}
    if (not isinstance(skills, list) or not skills or
            any(not isinstance(name, str) or name not in rows for name in skills)):
        raise ValueError('请先从这位同事的分享里选择技能。')
    selected = set(skills)
    pending = list(skills)
    while pending:
        for dependency in rows[pending.pop()]['requires']:
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    data = copy.deepcopy(data)
    data['skills'] = [row for row in data['skills'] if row['name'] in selected]
    for row in data['skills']:
        if row['name'] not in skills:
            row['assigned_agents'] = []
    plan = sharing.preview_import(data, agents, home)
    return {'config': data, 'agents': copy.deepcopy(agents), 'member': result['member'], 'revision': plan['revision']}, plan
