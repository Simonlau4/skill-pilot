from .platform_support import copytree
"""Dashboard backend for the authenticated team skill store."""
import base64
from .platform_support import rmtree
import copy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from http.client import HTTPException
from pathlib import Path, PurePosixPath

from . import cleanup, diagnostics, feedback, labels, normalization, operations, profiles, relations, sharing, store, sync, tags, team_hub
from .scan import SKIP_CONTENT, metadata
from .sources import github, normalize
from .store import git_tree, guard

MAX_BODY = team_hub.MAX_BODY
TIMEOUT = 10
ACTIVITY_TIMEOUT = 3
_FEEDBACK_RECEIPT_LOCK = threading.Lock()
FORMAT_ERROR = '分享站的技能格式不对，请联系管理员检查。'
PACKS_UNSUPPORTED = '分享站版本较旧，暂不能整理技能包，请联系管理员更新分享站。'
REPLACES_UNSUPPORTED = '分享站版本较旧，暂不支持标记替代旧技能，请联系管理员更新分享站。'
SUGGESTIONS_UNSUPPORTED = '分享站版本较旧，暂不支持提交改进建议，请联系管理员更新分享站。'
LOCAL_LABELS = {'mine': '我上架的', 'installed': '已装', 'update': '有新版', 'installable': '可以装',
                'lark': '要用飞书官方方式装', 'conflict': '本机有同名技能', 'blocked': '暂时装不了',
                'external': '由外部工具维护'}
SECRET_NAMES = ('.env', '.env.*', '*.pem', '*.key', '*.p12', 'id_rsa*', 'id_ed25519*',
                '.npmrc', '.pypirc', 'credentials*')
SECRET_CONTENT = re.compile(
    r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|'
    r'(?:sk-|ghp_|github_pat_|AKIA|xoxb-)[A-Za-z0-9_-]{16,}|'
    r'(?:api[_-]?key|token|secret|password)[\"\']?\s*[:=]\s*[\"\']?[^\s\"\',;\x00-\x1f]{16,}', re.I)


class RequestError(ValueError):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


def _settings(value):
    url, key = value.get('url'), value.get('key')
    if not isinstance(url, str) or not isinstance(key, str):
        raise ValueError('请填写分享站地址。')
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
    if key and not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('请粘贴管理员给你的完整口令。')
    return {'url': url, 'key': key}


def settings(home):
    path = Path(home) / '.skill-pilot/team.json'
    if not path.exists():
        return {}
    try:
        config = _settings(json.loads(path.read_text(encoding='utf-8')))
        if not config['key']:
            raise ValueError
        return config
    except (OSError, ValueError, AttributeError):
        raise ValueError('分享站设置没能读懂，请在设置里重新连接。') from None


def _redact(value, key):
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, '[已隐藏]')
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    return value


def _request(config, path, method='GET', data=None):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    limit = (team_hub.MAX_JOIN_BODY if path == '/api/join' else
             team_hub.MAX_ACTIVITY_BODY if path == '/api/activity' else
             team_hub.MAX_RATING_BODY if path == '/api/ratings' else
             team_hub.MAX_INSTALL_BODY if path == '/api/installs/me' else MAX_BODY)
    if path == '/api/packs/me':
        limit = team_hub.MAX_PACK_BODY
    if path == '/api/suggestions':
        limit = team_hub.MAX_SUGGESTION_BODY
    if path in {'/api/feedback', '/api/feedback/with-attachments'}:
        has_attachments = isinstance(data, dict) and bool(data.get('attachments'))
        if has_attachments or path == '/api/feedback/with-attachments':
            text_only = {k: v for k, v in data.items() if k != 'attachments'}
            if len(json.dumps(text_only, ensure_ascii=False).encode('utf-8')) > feedback.MAX_BODY:
                raise ValueError('反馈文字内容超过 64 KB，请缩短后重试。')
            limit = feedback.MAX_UPLOAD_BODY
        else:
            limit = feedback.MAX_BODY
    if body is not None and len(body) > limit:
        raise ValueError('反馈内容及截图超过上传上限，请减少截图数量或缩小图片后重试。'
                         if path in {'/api/feedback', '/api/feedback/with-attachments'} and limit == feedback.MAX_UPLOAD_BODY else
                         '反馈内容超过 64 KB，请缩短后重试。' if path == '/api/feedback' else
                         '活跃上报内容超过 1024 字节。' if path == '/api/activity' else
                         '建议内容超过 1 MB，请减少文件数量。' if path == '/api/suggestions' else
                         '技能包内容超过 64 KB，请缩短后重试。' if path == '/api/packs/me' else
                         '上报内容超过 64 KB，请缩短后重试。' if path == '/api/installs/me' else
                         '上架内容超过 20 MB，请分批上架。' if limit == MAX_BODY else
                         '评价内容超过 64 KB。' if limit == team_hub.MAX_RATING_BODY else
                         '加入信息超过 4096 字节，请缩短后重试。')
    key = config.get('key', '')
    headers = {'Content-Type': 'application/json', team_hub.SKILL_FILE_HEADER: '1'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    try:
        request = urllib.request.Request(config['url'] + path, data=body, method=method, headers=headers)
        request_timeout = (max(TIMEOUT, 30) if path in {'/api/feedback', '/api/feedback/with-attachments'} else
                           ACTIVITY_TIMEOUT if path == '/api/activity' else TIMEOUT)
        with urllib.request.build_opener(labels.NoRedirect).open(request, timeout=request_timeout) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError('分享站返回的内容超过 20 MB，请联系管理员检查。')
        def invalid_constant(value):
            raise ValueError
        try:
            result = json.loads(raw.decode('utf-8'), parse_constant=invalid_constant)
            if not isinstance(result, dict):
                raise ValueError
            return _redact(result, key)
        except (ValueError, UnicodeError, RecursionError):
            raise RequestError(502, FORMAT_ERROR) from None
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            # A batch can list more missing files than a single deliverable.
            limit = MAX_BODY if path == '/api/listings/me' else team_hub.MAX_MANIFEST_BODY
            payload = json.loads(error.read(limit).decode('utf-8'))
            detail = payload.get('error')
        except (ValueError, UnicodeError, AttributeError, HTTPException):
            payload, detail = {}, None
        finally:
            error.close()
        message = ('这个分享站需要管理员发的口令，请点「管理员给了口令？」填上。'
                   if path == '/api/join' and status == 403 else
                   '分享站没认出你（口令不对、已失效或已被移出），请到设置里重新连接。'
                   if status == 401 or (path == '/api/me' and status == 403) else
                   '分享站地址发生了跳转，请填写最终地址后重试。' if 300 <= status < 400 else
                   _redact(detail, key) if isinstance(detail, str) and detail else
                   '这个技能已经下架，请刷新商店。' if status == 404 else
                   '上架内容超过 20 MB，请分批上架。' if status == 413 else
                   '分享站暂时没能完成操作，请稍后重试。')
        raise RequestError(status, message, _redact(payload, key) if isinstance(payload, dict) else {}) from None
    except (OSError, HTTPException):
        raise ValueError('连不上分享站，请检查地址和网络，或请管理员确认分享站已开启。') from None


def _member(value):
    if (not isinstance(value, dict) or not isinstance(value.get('id'), str) or
            not re.fullmatch(r'[0-9a-f]{32}', value['id']) or
            not isinstance(value.get('name'), str) or not value['name'].strip()):
        raise RequestError(502, FORMAT_ERROR)
    return {key: value[key] for key in ('id', 'name')}


def _connection(home):
    config = settings(home)
    if not config:
        raise ValueError('请先在设置里填写分享站地址和自己的口令。')
    return config


def _connection_version(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _me(config, capabilities=False):
    result = _request(config, '/api/me')
    if type(result.get('api')) is not int or result['api'] != 2:
        raise ValueError('分享站和你的 Skill Pilot 版本不匹配，请一起换成新版。')
    if result.get('service') != 'skill-pilot-team':
        raise ValueError('这个地址不是 Skill Pilot 分享站，请核对地址。')
    member = _member(result.get('member'))
    return ({'member': member, 'feedback_supported': result.get('feedback_supported') is True,
             'feedback_configured': result.get('feedback_configured') is True,
             'feedback_attachments_supported': result.get('feedback_attachments_supported') is True,
             'skill_file_upload': result.get('skill_file_upload') is True,
             'suggestions_supported': result.get('suggestions_supported') is True,
             'replaces_supported': result.get('replaces_supported') is True} if capabilities else member)


def status(home, config=None, strict=True):
    result = {'url': '', 'has_key': False, 'configured': False, 'member': None}
    try:
        config = settings(home) if config is None else config
        public_url = config.get('url', '').replace(config['key'], '[已隐藏]') if config else ''
        result.update(url=public_url, has_key=bool(config.get('key')), configured=bool(config))
        if config:
            info = _me(config, capabilities=True)
            result.update({k: info[k] for k in (
                'member', 'feedback_supported', 'feedback_configured', 'feedback_attachments_supported')})
    except (ValueError, OSError) as error:
        if strict:
            raise
        result['error'] = str(error) if isinstance(error, ValueError) else '没能读取团队商店，请稍后重试。'
        from .diagnostics import failure
        result.update(failure(home, 'backend', '连接分享站', error, result['error']))
    return result


def report_activity(home, config=None):
    """Send a lightweight authenticated dashboard-open ping when connected and writable."""
    if not store.write_allowed(home):
        return {'sent': False, 'reason': 'read_only'}
    if config is None:
        try:
            config = settings(home)
        except ValueError:
            return {'sent': False, 'reason': 'unconfigured'}
    if not config:
        return {'sent': False, 'reason': 'unconfigured'}
    try:
        result = _request(config, '/api/activity', 'POST', {})
    except RequestError as error:
        if error.status in {404, 405}:
            return {'sent': False, 'unsupported': True, 'reason': 'unsupported'}
        return {'sent': False, 'reason': 'error'}
    except (ValueError, OSError, HTTPException):
        return {'sent': False, 'reason': 'offline'}
    return {'sent': True, 'data': result}


def feedback_preview(home, error_id='', note='', attachments=None, *, prefer_running=False, pending_version=''):
    parsed = feedback.parse_attachments(attachments)
    report = diagnostics.bundle(home, error_id, note, prefer_running=prefer_running,
                                pending_version=pending_version)['report']
    if not error_id:
        report['errors'] = []  # A manual report must not silently include unrelated past failures.
    payload = feedback.envelope(report, attachments=parsed)
    if not error_id:
        import uuid
        payload.update(error_id=uuid.uuid4().hex, fingerprint='manual')
    public_attachments = [
        {'index': index, 'content_type': item['content_type'], 'label': item['label'],
         'size': item['size'], 'sha256': item['sha256'][:12]}
        for index, item in enumerate(parsed, 1)
    ]
    pending = report['environment'].get('pending_app_version', '')
    result = {
        'payload': payload, 'member': None, 'can_send': False, 'can_attach': False,
        'attach_reason': '', 'reason': '', 'submitted': False,
        'attachments': public_attachments,
        'attachment_limits': {
            'max_count': feedback.MAX_ATTACHMENTS,
            'max_bytes': feedback.MAX_ATTACHMENT_BYTES,
            'max_total_bytes': feedback.MAX_ATTACHMENTS_TOTAL_BYTES,
        },
        **({'pending_app_version': pending} if pending and pending != payload.get('app_version', '') else {}),
    }
    config = {}
    try:
        config = settings(home)
        if not store.write_allowed(home):
            result['reason'] = '只读模式不能发送反馈，请复制或导出诊断信息。'
            result['attach_reason'] = '只读模式暂不支持随反馈上传截图，请复制或导出纯文本诊断信息。'
        elif not config:
            result['reason'] = '尚未连接分享站，请复制或导出诊断信息。'
            result['attach_reason'] = '尚未连接团队分享站，暂不支持随反馈上传截图，可复制或导出纯文本诊断信息。'
        else:
            info = _me(config, capabilities=True)
            result['member'] = info['member']
            if not info['feedback_supported']:
                result['reason'] = '分享站版本较旧，暂不支持反馈，请联系管理员更新。'
                result['attach_reason'] = '当前分享站版本较旧，暂不支持随反馈上传截图，请联系管理员更新，或复制、导出纯文本诊断信息。'
            elif not info['feedback_configured']:
                result['reason'] = '管理员尚未启用飞书反馈接收，请复制或导出诊断信息。'
                result['attach_reason'] = '管理员尚未启用飞书反馈接收，暂不支持随反馈上传截图，可复制或导出纯文本诊断信息。'
            elif payload['action'] == '连接分享站':
                result['reason'] = '这次是分享站连接失败，请复制或导出诊断信息发给维护者。'
                result['attach_reason'] = '分享站连接失败时无法随反馈上传截图，可复制或导出纯文本诊断信息发给维护者。'
            elif not info['feedback_attachments_supported']:
                result['attach_reason'] = '当前分享站版本较旧，暂不支持随反馈上传截图；可直接发送纯文本反馈，或复制、导出诊断信息。'
                if parsed:
                    result['reason'] = '当前分享站版本较旧，暂不支持上传截图。请移除截图后发送纯文本反馈，或复制、导出诊断信息。'
                else:
                    result['can_send'] = True
            else:
                result['can_send'] = True
                result['can_attach'] = True
    except (ValueError, OSError):
        result['reason'] = '连不上分享站，请复制或导出诊断信息发给维护者。'
        result['attach_reason'] = '当前无法连接分享站，暂不支持随反馈上传截图，可复制或导出纯文本诊断信息发给维护者。'
    version = _connection_version(config)
    saved = diagnostics.read_json(Path(home) / '.skill-pilot/feedback.json').get('submitted', [])
    result['submitted'] = version + ':' + payload['error_id'] in saved
    if result['submitted']:
        result.update(can_send=False, can_attach=False, reason='这条反馈已提交。', attach_reason='')
    payload.update(status='待排查', reporter=(result['member'] or {}).get('name', '未连接'))
    result['_connection'] = version
    result['_attachments'] = parsed
    return result


def submit_feedback(home, prepared):
    if not store.write_allowed(home):
        raise ValueError('只读模式不能发送反馈，请复制或导出诊断信息。')
    if not prepared.get('can_send'):
        raise ValueError('当前不能发送反馈，请重新打开反馈窗口。')
    config = _connection(home)
    info = _me(config, capabilities=True)
    if (_connection_version(config) != prepared['_connection'] or info['member'] != prepared['member'] or
            not info['feedback_supported'] or not info['feedback_configured']):
        raise ValueError('分享站连接或署名已改变，请重新打开反馈窗口核对。')
    attachments = prepared.get('_attachments') or []
    if attachments and not info['feedback_attachments_supported']:
        raise ValueError('当前分享站暂不支持上传截图，请移除截图后重新打开反馈窗口核对。')
    body = prepared['payload']
    if attachments:
        body = {
            **prepared['payload'],
            'attachments': [
                {'content_type': item['content_type'],
                 'data': base64.b64encode(item['data']).decode('ascii')}
                for item in attachments
            ],
        }
    try:
        route = '/api/feedback/with-attachments' if attachments else '/api/feedback'
        result = _request(config, route, 'POST', body)
    except RequestError as error:
        if attachments and error.status == 404:
            raise ValueError('当前分享站版本较旧，暂不支持随反馈上传截图，本次未提交。请移除截图后重发，或复制、导出诊断信息。') from None
        raise ValueError(diagnostics.sanitize(str(error), home, 400)) from None
    except (ValueError, OSError) as error:
        raise ValueError(diagnostics.sanitize(str(error), home, 400)) from None
    if result.get('submitted') is not True:
        raise ValueError('分享站没有确认提交成功，请复制或导出诊断信息。')
    attachments_evicted = False
    if attachments:
        expected_ids = [item['sha256'] for item in attachments]
        returned = result.get('attachments')
        actual_ids = ([item.get('id') for item in returned if isinstance(item, dict)]
                      if isinstance(returned, list) else [])
        if len(actual_ids) != len(expected_ids) or actual_ids != expected_ids:
            raise ValueError('分享站未能确认收下本次截图，请核对分享站版本后重试，或复制、导出诊断信息发给维护者。')
        attachments_evicted = any(bool(item.get('evicted')) for item in returned if isinstance(item, dict))
    path = Path(home) / '.skill-pilot/feedback.json'
    key = prepared['_connection'] + ':' + prepared['payload']['error_id']
    with _FEEDBACK_RECEIPT_LOCK:
        saved = diagnostics.read_json(path).get('submitted', [])
        saved = saved if isinstance(saved, list) else []
        labels.atomic_write(home, 'feedback.json', json.dumps({'submitted': [*saved, key][-200:]}) + '\n', 0o600)
    outcome = {'submitted': True}
    if attachments_evicted:
        outcome['attachments_evicted'] = True
    return outcome


def save_settings(home, body, write_lock=None):
    guard(Path(home) / '.skill-pilot', home)
    config = _settings({'url': body.get('url'), 'key': body.get('key', '')})
    supplied_key = bool(config['key'])
    if not supplied_key:
        try:
            config['key'] = settings(home).get('key', '')
        except ValueError:
            config['key'] = ''
    result = None
    if config['key']:
        try:
            result = status(home, config)
        except RequestError as error:
            if supplied_key or error.status not in {401, 403} or not body.get('name'):
                raise
    if result is None:
        name = body.get('name', '')
        if isinstance(name, str) and not name.strip():
            raise ValueError('请填写你的名字，同事会看到它。')
        name = team_hub.member_name(name)
        joined = _request({'url': config['url']}, '/api/join', 'POST', {'name': name})
        if (joined.get('service') != 'skill-pilot-team' or type(joined.get('api')) is not int or
                joined['api'] != 2 or not isinstance(joined.get('token'), str) or
                not re.fullmatch(r'[A-Za-z0-9_-]+', joined['token'])):
            raise RequestError(502, FORMAT_ERROR)
        _member(joined.get('member'))
        config['key'] = joined['token']
        result = status(home, config)
    # Failed connections leave the previous working settings intact.
    from contextlib import nullcontext
    with write_lock if write_lock is not None else nullcontext():
        labels.atomic_write(home, 'team.json', json.dumps(config, ensure_ascii=False) + '\n', 0o600)
    return {**result, 'message': '分享站已连接，设置已保存。'}


def _catalog(config):
    data = _request(config, '/api/catalog')
    try:
        if not isinstance(data.get('cards'), list):
            raise ValueError
        has_installs = 'my_installs' in data
        if has_installs and (not isinstance(data['my_installs'], list) or
                            any(not isinstance(key, str) for key in data['my_installs'])):
            raise ValueError
        if 'my_all_installs' in data and (not has_installs or not isinstance(data['my_all_installs'], list) or
                                          any(not isinstance(key, str) for key in data['my_all_installs'])):
            raise ValueError
        result, seen = [], set()
        for card in data['cards']:
            if (not isinstance(card, dict) or not isinstance(card.get('key'), str) or card['key'] in seen or
                    not isinstance(card.get('listings'), list) or not card['listings'] or
                    not isinstance(card.get('ratings'), list)):
                raise ValueError
            if ('installs' in card) != has_installs or (has_installs and
                    (type(card['installs']) is not int or card['installs'] < 0)):
                raise ValueError
            rows, members, ratings, raters = [], set(), [], set()
            for row in card['listings']:
                member = _member(row.get('member'))
                identity = (member['id'], row.get('name'))
                if (not isinstance(row.get('updated_at'), str) or identity in members or
                        team_hub.listing_key(member['id'], team_hub.clean_listing(row, packaged=True)) != card['key']):
                    raise ValueError
                members.add(identity)
                rows.append({**team_hub.clean_listing(row, packaged=True), 'member': member, 'updated_at': row['updated_at']})
                if 'created_at' in row:
                    if not isinstance(row['created_at'], str):
                        raise ValueError
                    rows[-1]['created_at'] = row['created_at']
                if 'revision' in row:
                    if type(row['revision']) is not int or row['revision'] < 1:
                        raise ValueError
                    rows[-1]['revision'] = row['revision']
                if 'published_via' in row:
                    if not isinstance(row['published_via'], str):
                        raise ValueError
                    rows[-1]['published_via'] = row['published_via']
                if 'previous_versions' in row:
                    if (not isinstance(row['previous_versions'], list) or
                            any(not isinstance(v, str) for v in row['previous_versions'])):
                        raise ValueError
                    rows[-1]['previous_versions'] = list(row['previous_versions'])
            for row in card['ratings']:
                member = _member(row.get('member'))
                if (type(row.get('stars')) is not int or not 1 <= row['stars'] <= 5 or
                        not isinstance(row.get('updated_at'), str) or member['id'] in raters):
                    raise ValueError
                rating = {'member': member, 'stars': row['stars'], 'updated_at': row['updated_at']}
                if 'text' in row:
                    rating['text'] = team_hub.short_text(row['text'], '评价')
                ratings.append(rating)
                raters.add(member['id'])
            result.append({'key': card['key'], 'listings': sorted(rows, key=lambda r: r['updated_at'], reverse=True),
                           'ratings': sorted(ratings, key=lambda r: r['updated_at'], reverse=True)})
            if has_installs:
                result[-1]['installs'] = card['installs']
            seen.add(card['key'])
        catalog = {'cards': sorted(result, key=lambda c: c['listings'][0]['updated_at'], reverse=True)}
        if 'suggestions_summary' in data:
            summary = data['suggestions_summary']
            if (not isinstance(summary, dict) or not isinstance(summary.get('incoming_by_skill'), dict) or
                    any(type(n) is not int or n < 0 for n in (summary.get('incoming_total'), summary.get('outgoing_total'),
                                                              *summary['incoming_by_skill'].values()))):
                raise ValueError
            catalog['suggestions_summary'] = summary
        if has_installs:
            catalog['my_installs'] = data['my_installs']
        if 'my_all_installs' in data:
            catalog['my_all_installs'] = data['my_all_installs']
        if 'packs' in data:
            if not isinstance(data['packs'], list):
                raise ValueError
            packs, seen_packs = [], set()
            for row in data['packs']:
                pack = team_hub.clean_pack(row)
                member = _member(row.get('member'))
                key = 'pack:' + member['id'] + ':' + pack['id']
                if (row.get('key') != key or key in seen_packs or not isinstance(row.get('updated_at'), str) or
                        not isinstance(row.get('cards'), list) or
                        any(not isinstance(k, str) for k in row['cards'])):
                    raise ValueError
                owned = _owned(result, member['id'])
                owned_keys = {team_hub.listing_key(member['id'], owned[n]) for n in pack['skills'] if n in owned}
                for card_key in row['cards']:
                    if card_key in seen and card_key not in owned_keys:
                        raise ValueError
                packs.append({**pack, 'key': key, 'member': member, 'cards': row['cards'], 'updated_at': row['updated_at']})
                seen_packs.add(key)
            catalog['packs'] = sorted(packs, key=lambda p: p['updated_at'], reverse=True)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RequestError(502, FORMAT_ERROR) from None
    if 'github' in data:
        try:
            catalog['github'] = team_hub.clean_github(data['github'])
        except ValueError:
            pass
    if 'deliverables' in data:
        # A malformed deliverable list hides that row of the store; the skills still load.
        try:
            rows, keys = [], set()
            for row in data['deliverables']:
                member = _member(row.get('member'))
                identity = row.get('id')
                key = 'deliverable:' + member['id'] + ':' + str(identity)
                if (not isinstance(identity, str) or not re.fullmatch(team_hub.DELIVERABLE_ID, identity) or
                        row.get('key') != key or key in keys or not isinstance(row.get('updated_at'), str) or
                        type(row.get('file_count')) is not int or row['file_count'] < 1 or
                        type(row.get('size')) is not int or row['size'] < 0 or type(row.get('has_recipe')) is not bool or
                        type(row.get('has_teardown', False)) is not bool):
                    raise ValueError
                rows.append({'key': key, 'id': identity, 'member': member, **team_hub.deliverable_info(row),
                             **{k: row[k] for k in ('file_count', 'size', 'has_recipe', 'updated_at')},
                             'has_teardown': row.get('has_teardown', False)})
                keys.add(key)
            catalog['deliverables'] = sorted(rows, key=lambda r: r['updated_at'], reverse=True)
        except (ValueError, TypeError, KeyError, AttributeError):
            catalog['deliverables'] = []
    return catalog


def _fetch(home, with_packs=False):
    config = _connection(home)
    member, data = _me(config), _catalog(config)
    return config, member, data if with_packs else data['cards']


def _owned(cards, member):
    return {row['name']: row for card in cards for row in card['listings'] if row['member']['id'] == member}


def _origin(skill):
    try:
        return normalize(skill.get('source', {}))
    except (ValueError, TypeError, AttributeError):
        return {'kind': 'skip'}


def _local(row, registry):
    name, source = row['name'], row['source']
    if name in registry.get('external_skills', {}):
        return 'external', name
    skills = registry['skills']
    if source['kind'] == 'github':
        for local_name, skill in skills.items():
            origin = _origin(skill)
            if (origin['kind'] == 'github' and origin['repository'].lower() == source['repository'].lower() and
                    PurePosixPath(origin['path']) == PurePosixPath(source['path'])):
                return 'installed', local_name
    if name not in skills:
        return ('lark' if source['kind'] == 'lark' else 'installable'), name
    origin = _origin(skills[name])
    if source['kind'] == 'lark':
        return 'installed', name
    if (source['kind'] == 'files' and origin['kind'] == 'team' and
            origin['member'] == row['member']['id'] and origin['name'] == name):
        return ('installed' if origin['version'] == source['version'] else 'update'), name
    return 'conflict', name


def _plan(card, cards, registry):
    root = card['listings'][0]
    rows = _owned(cards, root['member']['id'])
    install, reuse, errors = {}, set(), {}
    visiting = set()
    def visit(name, first=False):
        if name in visiting:
            return
        visiting.add(name)
        if not first and name in registry['skills']:
            reuse.add(name)
            return
        row = root if first else rows.get(name)
        if row is None:
            errors[name] = '作者没有上架这个所需技能。'
            return
        state, local_name = _local(row, registry)
        if state in {'installed', 'update'}:
            reuse.add(local_name)
            return
        if state in {'lark', 'conflict', 'external'}:
            errors[name] = LOCAL_LABELS[state] + '。'
            return
        install[name] = row
        for dependency in row['requires']:
            visit(dependency)
    visit(root['name'], True)
    if errors:
        reason = '所需技能装不了：' + '；'.join(n + '（' + r + '）' for n, r in sorted(errors.items()))
        errors.setdefault(root['name'], reason)
        return {}, set(), [{'name': n, 'reason': r} for n, r in sorted(errors.items())]
    return install, reuse, []


def _counted_local(card, row, state, local_name, registry):
    if state in {'installed', 'update'}:
        return not registry['skills'].get(local_name, {}).get('withdrawn')
    if state == 'external':
        return True
    if state != 'conflict':
        return False
    skill = registry['skills'].get(local_name, {})
    if skill.get('withdrawn'):
        return False
    origin = _origin(skill)
    source = row['source']
    if origin['kind'] == 'team' and source['kind'] == 'files' and origin['key'] != card['key']:
        return False
    if origin['kind'] == 'github' and source['kind'] == 'github':
        return (origin['repository'].lower() == source['repository'].lower() and
                PurePosixPath(origin['path']) == PurePosixPath(source['path']))
    return True


def parse_replaces(value, own_name=None):
    if isinstance(value, str):
        parts = [part for part in re.split(r'[,，、\s]+', value.strip()) if part]
        return team_hub.clean_replaces(parts, own_name=own_name)
    return team_hub.clean_replaces(value, own_name=own_name)


def listing_replaces(row):
    replaces = list(dict.fromkeys(row.get('replaces', []))) if isinstance(row, dict) else []
    note = row.get('replacement_note', '') if isinstance(row, dict) else ''
    return replaces, note


def _plan_replaces(rows, exclude=()):
    excluded = set(exclude)
    replaces, notes = [], []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for old in row.get('replaces', []):
            if old not in excluded and old not in seen:
                seen.add(old)
                replaces.append(old)
        note = row.get('replacement_note', '')
        if note and note not in notes and any(old not in excluded for old in row.get('replaces', [])):
            notes.append(note)
    return replaces, '；'.join(notes)


def _local_visibility(home, agents):
    managed = profiles.managed(home, agents)
    agent_names = {p['id']: p['name'] for p in managed}
    try:
        actual = sync.visibility(home, managed)
    except (ValueError, OSError):
        actual = {}
    return agent_names, actual


def inspect_replaced_local(home, names, *, registry=None, agents=None, visibility_cache=None,
                           titles=None, irreplaceable_cache=None, include_revision=False):
    home = Path(home)
    if registry is None or agents is None:
        home, registry, agents = operations.load(home)
    names = list(dict.fromkeys(n for n in (names or []) if isinstance(n, str) and n))
    if not names:
        return ([], operations.revision(home, agents)) if include_revision else []
    agent_names, actual = visibility_cache if visibility_cache is not None else _local_visibility(home, agents)
    if titles is None:
        titles = {n: l['title'] for g in labels.read(home) for n, l in g['skills'].items()}
    write_ok = store.write_allowed(home)
    skills = registry['skills']
    external = registry.get('external_skills', {})
    items = []
    for old_name in names:
        visible_agents = sorted(a for a, info in actual.items() if old_name in info['available'])
        if old_name in skills:
            skill = skills[old_name]
            assigned = [a for a in skill.get('assigned_agents', []) if a in agents]
            withdrawn = bool(skill.get('withdrawn'))
            title = titles.get(old_name, old_name)
            dependents = [
                {'name': other, 'title': titles.get(other, other),
                 'agents': [a for a in row.get('assigned_agents', []) if a in agents]}
                for other, row in sorted(skills.items())
                if other != old_name and row.get('assigned_agents') and old_name in row.get('requires', [])
            ]
            dep_text = '、'.join(d['title'] for d in dependents)
            if not write_ok:
                can_withdraw, withdraw_reason = False, '当前处于只读模式，不能撤下技能。'
                can_delete, delete_reason = False, '当前处于只读模式，不能删除技能。'
            elif dependents:
                can_withdraw = False
                withdraw_reason = f'「{dep_text}」正在使用且依赖「{title}」，不能撤下。'
                can_delete = False
                delete_reason = f'「{dep_text}」正在使用且依赖「{title}」，不能删除。'
            else:
                if withdrawn or not assigned:
                    can_withdraw, withdraw_reason = False, '当前未启用给任何 Agent，无需撤下。'
                else:
                    can_withdraw, withdraw_reason = True, ''
                if old_name == 'skill-pilot':
                    can_delete, delete_reason = False, 'Skill Pilot 自带的技能只能撤下，不能删除。'
                else:
                    can_delete, delete_reason = True, ''
            if irreplaceable_cache is not None and old_name in irreplaceable_cache:
                irreplaceable = irreplaceable_cache[old_name]
            else:
                try:
                    irreplaceable = (normalize(skill['source'])['kind'] == 'skip' or
                                     normalization.tree(home / '.skill-pilot/store' / old_name) !=
                                     normalization.tree(home / '.skill-pilot/bases' / old_name))
                except (ValueError, OSError):
                    irreplaceable = True
                if irreplaceable_cache is not None:
                    irreplaceable_cache[old_name] = irreplaceable
            status = 'active' if (assigned and not withdrawn) else ('withdrawn' if withdrawn else 'idle')
            blocked_reason = ('当前处于只读模式，不能撤下或删除技能。' if not write_ok else
                              f'「{dep_text}」正在使用且依赖「{title}」，为避免影响正常使用，已拦下撤下和删除。'
                              if dependents else '')
            items.append({
                'name': old_name, 'title': title, 'status': status,
                'agents': assigned, 'agent_names': [agent_names.get(a, a) for a in assigned],
                'visible_agents': visible_agents, 'dependents': dependents,
                'can_withdraw': can_withdraw, 'withdraw_reason': withdraw_reason,
                'can_delete': can_delete, 'delete_reason': delete_reason,
                'irreplaceable': irreplaceable, 'blocked_reason': blocked_reason,
            })
        elif old_name in external:
            reason = '由外部工具维护，Skill Pilot 不会自动撤下或删除，请在原应用中处理。'
            items.append({
                'name': old_name, 'title': titles.get(old_name, old_name), 'status': 'external',
                'agents': visible_agents, 'agent_names': [agent_names.get(a, a) for a in visible_agents],
                'visible_agents': visible_agents, 'dependents': [],
                'can_withdraw': False, 'withdraw_reason': reason,
                'can_delete': False, 'delete_reason': reason,
                'irreplaceable': False, 'blocked_reason': reason,
            })
        elif visible_agents:
            where = '、'.join(agent_names.get(a, a) for a in visible_agents)
            reason = f'在 {where} 中发现未纳入统一管理的旧版，请先在技能页统一管理后再撤下或删除。'
            items.append({
                'name': old_name, 'title': titles.get(old_name, old_name), 'status': 'unmanaged',
                'agents': visible_agents, 'agent_names': [agent_names.get(a, a) for a in visible_agents],
                'visible_agents': visible_agents, 'dependents': [],
                'can_withdraw': False, 'withdraw_reason': reason,
                'can_delete': False, 'delete_reason': reason,
                'irreplaceable': False, 'blocked_reason': reason,
            })
    return (items, operations.revision(home, agents)) if include_revision else items


def preview_card_replace(home, card_key):
    if not isinstance(card_key, str) or not card_key.strip():
        raise ValueError('请选择要核对替代关系的商店技能。')
    home, registry, agents = operations.load(home)
    _, member, cards = _fetch(home)
    card = next((c for c in cards if c['key'] == card_key), None)
    if card is None:
        raise ValueError('这个技能已经下架，请刷新商店。')
    root = card['listings'][0]
    state, local_name = _local(root, registry)
    mine = any(r['member']['id'] == member['id'] for r in card['listings']) and root['name'] in registry['skills']
    if not mine and state == 'update':
        raise ValueError('本机仍是旧版，请先更新到新版后再清理被替代的旧技能。')
    if not (mine or state == 'installed'):
        raise ValueError('请先安装这个新版技能，再清理本机旧版。')
    installed_name = root['name'] if mine else local_name
    replaces, replacement_note = _plan_replaces([root], exclude={installed_name, root['name']})
    items, rev = inspect_replaced_local(home, replaces, registry=registry, agents=agents, include_revision=True)
    return {'items': items, 'revision': rev, 'installed': [installed_name],
            'allowed': [item['name'] for item in items],
            **({'replacement_note': replacement_note} if replacement_note else {})}


def _check_replace_live(home, registry, acting, *, allowed=None, required_present=None, titles=None):
    skills = registry['skills']
    if allowed is not None:
        allowed_set = set(allowed)
        disallowed = sorted(set(acting) - allowed_set)
        if disallowed:
            raise ValueError('以下技能不在本次已核对的替代清理名单中：' + '、'.join(disallowed) + '。')
    if required_present:
        store_dir = Path(home) / '.skill-pilot/store'
        missing_new = [n for n in required_present if n not in skills or n in acting or not (store_dir / n).is_dir()]
        if missing_new:
            raise ValueError('新版技能尚未独立保存在本机技能库中，已停止清理旧版以免误删唯一副本。')
    if titles is None:
        titles = {n: l['title'] for g in labels.read(home) for n, l in g['skills'].items()}
    for name in sorted(acting):
        if name not in skills:
            raise ValueError(f'「{name}」已不在本机技能库中，请刷新后再试。')
        active_deps = [
            titles.get(other, other)
            for other, row in sorted(skills.items())
            if other not in acting and row.get('assigned_agents') and name in row.get('requires', [])
        ]
        if active_deps:
            raise ValueError(
                f'「{"、".join(active_deps)}」正在使用且依赖「{titles.get(name, name)}」，为避免影响正常使用，已拦下本次清理。')


def validate_replace_cleanup(home, actions, *, allowed=None, required_present=None):
    if not isinstance(actions, dict):
        raise ValueError('请为每个旧版技能选择保留、撤下或删除。')
    kept, withdraw_names, delete_names = [], [], []
    for name, action in sorted(actions.items()):
        if not isinstance(name, str) or not name.strip():
            raise ValueError('旧版技能名称不正确。')
        if action not in {'keep', 'withdraw', 'delete'}:
            raise ValueError(f'「{name}」的处理方式只能选保留（keep）、撤下（withdraw）或删除（delete）。')
        if action == 'keep':
            kept.append(name)
        elif action == 'withdraw':
            withdraw_names.append(name)
        else:
            delete_names.append(name)
    acting = set(withdraw_names) | set(delete_names)
    if allowed is not None:
        allowed_set = set(allowed)
        unknown = sorted(set(actions) - allowed_set)
        if unknown:
            raise ValueError('以下技能不在本次已核对的替代清理名单中：' + '、'.join(unknown) + '。')
    if not acting:
        return kept, [], []
    home, registry, agents = operations.load(home)
    guard(home / '.skill-pilot', home)
    titles = {n: l['title'] for g in labels.read(home) for n, l in g['skills'].items()}
    inspected = {item['name']: item for item in inspect_replaced_local(
        home, sorted(acting), registry=registry, agents=agents, titles=titles)}
    skills = registry['skills']
    for name in sorted(acting):
        if name not in skills:
            info = inspected.get(name)
            if info and info['blocked_reason']:
                raise ValueError(f'「{info["title"]}」{info["blocked_reason"]}')
            raise ValueError(f'「{name}」已不在本机技能库中，请刷新后再试。')
    _check_replace_live(home, registry, acting, allowed=allowed, required_present=required_present, titles=titles)
    if withdraw_names:
        cleanup.preview(withdraw_names, home)
    if delete_names:
        cleanup.delete_preview(delete_names, home)
    return kept, withdraw_names, delete_names


def cleanup_replaced(home, actions, confirmed=False, expected=None, *, allowed=None, required_present=None):
    kept, withdraw_names, delete_names = validate_replace_cleanup(
        home, actions, allowed=allowed, required_present=required_present)
    if not withdraw_names and not delete_names:
        return {'kept': kept, 'withdrawn': [], 'deleted': [], 'operations': [],
                'new_session_agents': [], 'message': '已保留本机旧版技能。'}
    home, _, agents = operations.load(home)
    guard(home / '.skill-pilot', home)
    if expected is not None and expected != operations.revision(home, agents):
        raise ValueError('本机技能状态已经变了，请重新核对后再清理旧版。')
    if confirmed is not True:
        parts = []
        if withdraw_names:
            parts.append(f'撤下 {len(withdraw_names)} 个')
        if delete_names:
            parts.append(f'删除 {len(delete_names)} 个')
        return {'needs_confirmation': True, 'kept': kept, 'withdraw': withdraw_names, 'delete': delete_names,
                'message': '将' + '、'.join(parts) + '旧版技能。'}
    acting = set(withdraw_names) | set(delete_names)
    op_ids, new_session_agents = [], []
    next_expected = expected
    if withdraw_names:
        def check_withdraw_live(current, _agents):
            _check_replace_live(home, current, acting, allowed=allowed, required_present=required_present)
        res = cleanup.apply(withdraw_names, confirmed=True, home=home, expected=next_expected,
                            locked_check=check_withdraw_live)
        if 'id' in res:
            op_ids.append(res['id'])
            log_path = home / '.skill-pilot/logs' / (res['id'] + '.json')
            if log_path.is_file():
                next_expected = json.loads(log_path.read_text(encoding='utf-8')).get('after_revision', next_expected)
        new_session_agents.extend(res.get('new_session_agents', []))
    if delete_names:
        def check_delete_live(current, _agents):
            _check_replace_live(home, current, set(delete_names), allowed=allowed, required_present=required_present)
        try:
            res = cleanup.delete(delete_names, confirmed=True, home=home, expected=next_expected,
                                 locked_check=check_delete_live)
        except ValueError as error:
            if op_ids:
                raise ValueError(
                    f'已先撤下 {len(withdraw_names)} 个旧版技能（可在操作记录中撤销，编号 {op_ids[0]}），'
                    f'但删除其余旧版未完成：{error}') from None
            raise
        if 'id' in res:
            op_ids.append(res['id'])
        new_session_agents.extend(res.get('new_session_agents', []))
    parts = []
    if withdraw_names:
        parts.append(f'撤下 {len(withdraw_names)} 个')
    if delete_names:
        parts.append(f'删除 {len(delete_names)} 个')
    return {'kept': kept, 'withdrawn': withdraw_names, 'deleted': delete_names,
            'operations': op_ids, 'new_session_agents': list(dict.fromkeys(new_session_agents)),
            'message': '已' + '、'.join(parts) + '旧版技能。'}


def catalog(home):
    config = _connection(home)
    info = _me(config, capabilities=True)
    member = info['member']
    data = _catalog(config)
    cards = data['cards']
    _, registry, agents = operations.load(home)
    has_all = 'my_all_installs' in data
    visibility_cache = _local_visibility(home, agents) if any(r.get('replaces') for c in cards for r in c['listings']) else None
    titles = ({n: l['title'] for g in labels.read(home) for n, l in g['skills'].items()}
              if visibility_cache is not None else None)
    irreplaceable_cache = {} if visibility_cache is not None else None
    result, current = [], set()
    for card in cards:
        row = card['listings'][0]
        state, local_name = _local(row, registry)
        mine = any(r['member']['id'] == member['id'] for r in card['listings'])
        counted = _counted_local(card, row, state, local_name, registry)
        reason = ''
        install_rows, reuse_rows = {}, set()
        if mine:
            state = 'mine'
        elif state == 'installable':
            install_rows, reuse_rows, skipped = _plan(card, cards, registry)
            if skipped:
                state = 'blocked'
                reason = next(r['reason'] for r in skipped if r['name'] == row['name'])
        if state in {'lark', 'conflict', 'external'}:
            reason = LOCAL_LABELS[state] + '。'
        if counted and (has_all or not mine):
            current.add(card['key'])
        local = {'status': state, 'label': LOCAL_LABELS[state], 'reason': reason,
                 'agents': registry['skills'].get(local_name, {}).get('assigned_agents', [])}
        details = {}
        for field in team_hub.TEXT_FIELDS[:-1]:
            value = next((r[field] for r in card['listings'] if field in r), None)
            if value is not None:
                details[field] = value
        my_rating = next(({k: r[k] for k in ('stars', 'text') if k in r} for r in card['ratings']
                          if r['member']['id'] == member['id']), None)
        reviews = card['ratings']
        needs = []
        if state not in {'mine', 'installed', 'update'}:
            plan_rows, _, skipped = _plan(card, cards, registry)
            errors = {r['name']: r['reason'] for r in skipped}
            owned = _owned(cards, row['member']['id'])
            for name in row['requires']:
                dependency = owned.get(name)
                have = name in registry['skills'] or (dependency is not None and
                        _local(dependency, registry)[0] in {'installed', 'update'})
                status = 'have' if have else 'with' if name in plan_rows else 'missing'
                needs.append({'name': name, 'title': dependency.get('title', name) if dependency else name,
                              'status': status, **({'reason': errors.get(name, errors.get(row['name'], '这个所需技能暂时装不了。'))}
                                                  if status == 'missing' else {})})
        if state == 'installable' and install_rows:
            replaces, replacement_note = _plan_replaces(install_rows.values(), exclude=set(install_rows) | set(reuse_rows))
        else:
            exclude = {local_name, row['name']} if state in {'installed', 'update', 'mine'} else {row['name']}
            replaces, replacement_note = _plan_replaces([row], exclude=exclude)
        replaces_local = (inspect_replaced_local(home, replaces, registry=registry, agents=agents,
                                                 visibility_cache=visibility_cache, titles=titles,
                                                 irreplaceable_cache=irreplaceable_cache)
                          if replaces else [])
        result.append({'key': card['key'], 'kind': row['source']['kind'], 'name': row['name'], **details,
            'requires': row['requires'], 'updated_at': row['updated_at'],
            **{k: row[k] for k in ('revision', 'created_at') if k in row},
            'can_suggest': (info['suggestions_supported'] and row['source']['kind'] == 'files' and
                            state in {'installed', 'update'} and local_name in registry['skills'] and
                            not registry['skills'][local_name].get('withdrawn')),
            'pending_suggestions_count': data.get('suggestions_summary', {}).get('incoming_by_skill', {}).get(row['name'], 0) if mine else 0,
            'replaces': replaces,
            **({'replacement_note': replacement_note} if replacement_note else {}),
            **({'replaces_local': replaces_local} if replaces_local else {}),
            'needs': needs, 'required_by': sorted(n for n, r in _owned(cards, row['member']['id']).items()
                                                 if row['name'] in r['requires']), 'packs': [],
            'listings': [{**{k: r[k] for k in ('member', 'note', 'replaces', 'replacement_note', 'updated_at',
                                               'created_at', 'revision') if k in r},
                          **{k: r['source'][k] for k in ('version', 'file_count', 'size') if k in r['source']}}
                         for r in card['listings']],
            'rating': {'average': round(sum(r['stars'] for r in reviews) / len(reviews), 1) if reviews else None,
                       'count': len(reviews)}, 'reviews': reviews, 'my_rating': my_rating,
            'can_rate': not mine and state in {'installed', 'update'}, 'local': local,
            'tokens': operations.listing_tokens(row['name'], details)})
        if 'installs' in card:
            result[-1]['installs'] = card['installs']
    if 'my_installs' in data and store.write_allowed(home):
        previous = set(data['my_all_installs'] if has_all else data['my_installs'])
        if current != previous:
            payload = {'keys': sorted(current), **({'include_mine': True} if has_all else {})}
            try:
                _request(config, '/api/installs/me', 'PUT', payload)
            except (ValueError, OSError):
                pass
            else:
                for card in result:
                    key = card['key']
                    counted_before = key in previous and (has_all or card['local']['status'] != 'mine')
                    card['installs'] = max(0, card['installs'] - counted_before + (key in current))
    indexed = {c['key']: c for c in result}
    packs = []
    for pack in data.get('packs', []):
        keys = list(dict.fromkeys(k for k in pack['cards'] if k in indexed))
        if len(keys) < 2:
            continue
        for key in keys:
            indexed[key]['packs'].append(pack['key'])
        states = [indexed[k]['local']['status'] for k in keys]
        packs.append({**{k: v for k, v in pack.items() if k != 'skills'}, 'cards': keys,
                      'mine': pack['member']['id'] == member['id'],
                      'local': {'total': len(keys), 'installed': sum(s in {'installed', 'update'} for s in states),
                                'installable': states.count('installable'),
                                'blocked': sum(s in {'lark', 'conflict', 'blocked'} for s in states)},
                      'tokens': sum(indexed[k]['tokens'] for k in keys if indexed[k]['local']['status'] not in {'installed', 'update'})})
    catalog = {'member': member, 'cards': result, 'packs': packs, 'packs_supported': 'packs' in data,
               'suggestions_supported': info['suggestions_supported'],
               'suggestions_summary': data.get('suggestions_summary', {'incoming_total': 0, 'incoming_by_skill': {}, 'outgoing_total': 0}),
               'deliverables_supported': 'deliverables' in data, 'replaces_supported': info['replaces_supported']}
    local_names = registry['skills'].keys() | registry.get('external_skills', {}).keys()
    store_names = {listing['name'] for card in cards for listing in card['listings']}
    catalog['deliverables'] = [{**row, 'mine': row['member']['id'] == member['id'],
                                'skills': [{'name': n, 'installed': n in local_names, 'in_store': n in store_names}
                                           for n in row['skills']]}
                               for row in data.get('deliverables', [])]
    if 'github' in data:
        listed = {r['source']['repository'].lower() for c in cards for r in c['listings']
                  if r['source']['kind'] == 'github'}
        installed = {origin['repository'].lower() for skill in registry['skills'].values()
                     if (origin := _origin(skill))['kind'] == 'github'}
        repos = []
        for row in data['github']['repos']:
            repository = row['repository']
            if repository.lower() in listed:
                continue
            state = 'installed' if repository.lower() in installed else 'installable'
            repos.append({**row, 'url': 'https://github.com/' + repository,
                          'local': {'status': state, 'label': LOCAL_LABELS[state]}})
        if repos:
            catalog['github'] = {'fetched_at': data['github']['fetched_at'], 'repos': repos}
    return catalog


TEXT_SUFFIXES = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.md', '.txt', '.csv', '.svg'}
EXTERNAL = re.compile(r'''(?:(?:src|href)\s*=\s*["']?|url\(\s*["']?|@import\s+["'])(?:https?:)?//([^/"'\s)>]+)''', re.I)


def _deliverable_files(root):
    """path → (sha256, size, file) for a demo folder; reads only, never runs anything in it."""
    files, blocked, external = {}, [], set()
    def walk(folder):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError(f'成品文件夹里的「{relative}」是链接，请换成真实文件再上架。')
            if any(fnmatch.fnmatch(path.name.lower(), pattern) for pattern in (*SECRET_NAMES, 'team.json', 'settings.json')):
                blocked.append({'path': relative, 'reason': '文件名像账号或密钥文件，已拦下。'})
                continue
            if path.name.startswith('.') or path.name in SKIP_CONTENT | {'__MACOSX'}:
                continue
            if path.is_dir():
                walk(path)
                continue
            if not path.is_file():
                raise ValueError(f'成品文件夹里的「{relative}」读不了，请核对。')
            size = path.stat().st_size
            if size > team_hub.MAX_BLOB:
                raise ValueError(f'「{relative}」超过 {team_hub.MAX_BLOB // 1024 // 1024} MB，请先压缩。')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if path.suffix.lower() in TEXT_SUFFIXES:
                text = path.read_text(encoding='utf-8', errors='replace')
                if SECRET_CONTENT.search(text):
                    blocked.append({'path': relative, 'reason': '内容疑似含有密钥或密码，已拦下。'})
                    continue
                external.update(m.lower() for m in EXTERNAL.findall(text))
            files[relative] = (digest.hexdigest(), size, path)
    walk(root)
    return files, blocked, sorted(external)


def publish_deliverable(home, folder, identity, title, version='', summary='', skills=(), recipe='', confirmed=False):
    """Preview, then upload a demo folder file by file; only missing contents travel."""
    home, root = Path(home), Path(folder).expanduser().absolute()
    if not root.is_dir():
        raise ValueError('找不到成品文件夹，请核对位置。')
    if not isinstance(identity, str) or not re.fullmatch(team_hub.DELIVERABLE_ID, identity):
        raise ValueError('成品编号只能用字母、数字、点、横线和下划线，最多 64 个字符。')
    files, blocked, external = _deliverable_files(root)
    if 'index.html' not in files:
        raise ValueError('成品文件夹的首页必须叫 index.html，放在最外层。')
    manifest = {'title': title, 'version': version, 'summary': summary, 'skills': list(skills), 'recipe': recipe,
                'files': {p: {'sha256': d, 'size': s} for p, (d, s, _) in files.items()}}
    checked = team_hub.clean_deliverable(manifest)
    result = {'id': identity, 'title': checked['title'], 'file_count': checked['file_count'], 'size': checked['size'],
              'blocked': blocked, 'external': external}
    if not confirmed:
        return {**result, 'needs_confirmation': True,
                'message': f'将上架成品「{checked["title"]}」：{checked["file_count"]} 个文件，共 '
                           f'{checked["size"] / 1024 / 1024:.1f} MB。只限团队成员查看。'}
    guard(home / '.skill-pilot', home)
    config = _connection(home)
    _me(config)
    by_digest = {d: path for d, _, path in files.values()}
    saved, uploaded = _upload_missing(config, '/api/deliverables/me/' + identity, manifest, by_digest)
    return {**result, 'key': saved['key'], 'uploaded': uploaded,
            'message': f'已上架成品「{checked["title"]}」，同事在团队页「成品」里能打开。'}


def _upload_missing(config, route, payload, by_digest, read=None, method='PUT'):
    uploaded = 0
    for attempt in range(3):
        try:
            return _request(config, route, method, payload), uploaded
        except RequestError as error:
            missing = error.body.get('missing') if error.status == 409 else None
            if error.status == 404 and route.startswith('/api/deliverables/'):
                raise ValueError('分享站版本较旧，暂不能上架成品，请联系管理员更新分享站。') from None
            if not isinstance(missing, list) or not missing or any(
                    not isinstance(d, str) or d not in by_digest for d in missing):
                raise
            if attempt == 2:
                break
            for digest in dict.fromkeys(missing):
                value = by_digest[digest]
                _upload_blob(config, digest, read(digest, *value) if read else value)
                uploaded += 1
    raise ValueError('上传时有文件未能保存，请稍后重新上架。')


def _upload_blob(config, digest, data):
    from_file = isinstance(data, Path)
    size = data.stat().st_size if from_file else len(data)
    headers = {'Authorization': 'Bearer ' + config['key'], 'Content-Type': 'application/octet-stream',
               'Content-Length': str(size)}
    try:
        with (data.open('rb') if from_file else nullcontext(data)) as stream:
            request = urllib.request.Request(config['url'] + '/api/blobs/me/' + digest, data=stream,
                                             method='PUT', headers=headers)
            with urllib.request.build_opener(labels.NoRedirect).open(request, timeout=max(TIMEOUT, 120)) as response:
                response.read(team_hub.MAX_PACK_BODY)
    except urllib.error.HTTPError as error:
        with error:
            try:
                detail = json.loads(error.read(team_hub.MAX_PACK_BODY).decode('utf-8')).get('error')
            except (ValueError, UnicodeError, AttributeError, HTTPException):
                detail = None
        raise RequestError(error.code, _redact(detail, config['key']) if isinstance(detail, str) and detail else
                           '文件没能上传，请稍后重试。') from None
    except (OSError, HTTPException):
        raise ValueError('上传文件时连不上分享站，请检查网络后重试。') from None


def deliverable_detail(home, member_id, identity):
    """The recipe text of one deliverable; files stay with the hub until a viewer asks for them."""
    row = _request(_connection(home), '/api/deliverables/' + member_id + '/' + identity)
    recipe = row.get('recipe', '')
    if not isinstance(recipe, str) or len(recipe.encode()) > team_hub.MAX_RECIPE:
        raise RequestError(502, FORMAT_ERROR)
    return {'recipe': recipe}


def remove_deliverable(home, identity, confirmed=False):
    if not confirmed:
        return {'needs_confirmation': True, 'message': f'将从分享站下架成品「{identity}」，同事就看不到了。'}
    guard(Path(home) / '.skill-pilot', home)
    _request(_connection(home), '/api/deliverables/me/' + urllib.parse.quote(identity, safe=''), 'DELETE')
    return {'message': f'已下架成品「{identity}」。'}


def open_deliverable_file(home, member_id, identity, path, byte_range=None):
    """The hub's reply for one demo file, left open for streaming; the caller closes it."""
    config = _connection(home)
    headers = {'Authorization': 'Bearer ' + config['key']}
    if byte_range:
        headers['Range'] = byte_range
    address = (config['url'] + '/api/deliverables/' + member_id + '/' + identity + '/' +
               urllib.parse.quote(path, safe='/'))
    try:
        return urllib.request.build_opener(labels.NoRedirect).open(
            urllib.request.Request(address, headers=headers), timeout=max(TIMEOUT, 60))
    except urllib.error.HTTPError as error:
        error.close()
        raise RequestError(error.code, '成品里没有这个文件，或已经下架。' if error.code == 404 else
                           '请求的文件范围超出文件大小。' if error.code == 416 else
                           '分享站没认出你，请到设置里重新连接。' if error.code == 401 else
                           '分享站暂时没能完成操作，请稍后重试。') from None
    except (OSError, HTTPException):
        raise ValueError('连不上分享站，请检查地址和网络。') from None


def tag_pack(home, names, title):
    warnings = []
    try:
        skills = operations.load(home)[1]['skills']
        for name in names:
            if name not in skills:
                continue
            try:
                tags.update(home, {'skills': [name], 'add': title})
            except (ValueError, OSError) as error:
                message = str(error) if isinstance(error, ValueError) else '标签文件没能保存。'
                if message not in warnings:
                    warnings.append(message)
    except (ValueError, OSError):
        warnings.append('本机技能名单没能读取。')
    return '没能加上标签：' + '；'.join(warnings) if warnings else ''


def _download_skill_file(config, route, version, row, path):
    url = config['url'] + route + '/' + row['sha256'] + '?version=' + version
    headers = {'Authorization': 'Bearer ' + config['key']}
    request = urllib.request.Request(url, headers=headers)
    hasher = hashlib.sha256()
    remaining = row['size']
    try:
        with urllib.request.build_opener(labels.NoRedirect).open(request, timeout=max(TIMEOUT, 120)) as response:
            if response.headers.get_all('Content-Length') != [str(remaining)]:
                raise RequestError(502, FORMAT_ERROR)
            with path.open('wb') as output:
                while remaining:
                    part = response.read(min(remaining, 1024 * 1024))
                    if not part:
                        raise RequestError(502, FORMAT_ERROR)
                    hasher.update(part)
                    output.write(part)
                    remaining -= len(part)
        if hasher.hexdigest() != row['sha256']:
            raise RequestError(502, '技能有文件没有通过完整性核对，请重新安装。')
    except urllib.error.HTTPError as error:
        with error:
            if error.code in {404, 409}:
                raise ValueError('技能已下架或作者发布了新版，请刷新商店后再试。') from None
            if error.code == 401:
                raise ValueError('分享站没认出你，请到设置里重新连接。') from None
            raise ValueError('分享站没能提供这个技能文件，请稍后重试。') from None
    except (OSError, HTTPException):
        raise ValueError('技能文件没能完整下载，请检查网络后重试。') from None


def package(config, member, name, version, destination, *, manifest_out=None):
    route = '/api/packages/' + member + '/' + name
    data = _request(config, route + '?version=' + version)
    try:
        files = data.get('files')
        manifest = isinstance(files, dict)
        files = team_hub.clean_skill_manifest(files) if manifest else team_hub.clean_files(files)
        if data.get('name') != name or data.get('version') != version or team_hub.package_version(files) != version:
            raise ValueError
        if manifest_out is not None:
            compared = files if manifest else team_hub.clean_skill_manifest(team_hub.skill_manifest(files))
    except (ValueError, TypeError):
        raise RequestError(502, FORMAT_ERROR) from None
    destination = Path(destination)
    destination.mkdir(parents=True)
    paths = files.items() if manifest else ((row['path'], row) for row in files)
    downloaded = {}
    for relative, row in paths:
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if manifest:
            digest = row['sha256']
            if digest in downloaded:
                shutil.copyfile(downloaded[digest], path)
            else:
                _download_skill_file(config, route, version, row, path)
                downloaded[digest] = path
        else:
            path.write_bytes(base64.b64decode(row['content']))
        path.chmod(0o755 if row['executable'] else 0o644)
    github.safe_skill(destination)
    if metadata((destination / 'SKILL.md').read_text(encoding='utf-8')).get('name') != name:
        raise ValueError('技能文件里的名称和上架的名称不一致，请联系作者。')
    if manifest_out is not None:
        manifest_out.update(compared)
    return destination


def install(home, key, member_id=None):
    home, registry, agents = operations.load(home)
    guard(home / '.skill-pilot', home)
    config, _, cards = _fetch(home)
    card = next((c for c in cards if c['key'] == key), None)
    if card is None:
        raise ValueError('这个技能已经下架。')
    if member_id is not None:
        root = next((r for r in card['listings'] if r['member']['id'] == member_id), None)
        if root is None:
            raise ValueError('这位上架人的技能已经下架。')
        card = {**card, 'listings': [root, *(r for r in card['listings'] if r is not root)]}
    else:
        root = card['listings'][0]
    existing = {'installed': [], 'reuse': [], 'message': '本机已经有了。'}
    if _local(root, registry)[0] in {'installed', 'update'}:
        return existing
    rows, _, skipped = _plan(card, cards, registry)
    if skipped:
        raise ValueError(next(r['reason'] for r in skipped if r['name'] == root['name']))
    title = next((r['title'] for r in card['listings'] if 'title' in r), root['name'])
    with tempfile.TemporaryDirectory(prefix='skill-pilot-team-install-') as temporary:
        items = {}
        for name, row in sorted(rows.items()):
            source = row['source']
            path = Path(temporary) / name
            if source['kind'] == 'files' or 'version' in source:
                package(config, row['member']['id'], name, source['version'], path)
                if source['kind'] == 'files':
                    origin = {'source': '团队商店', 'team': {'member': row['member']['id'],
                        'member_name': row['member']['name'], 'name': name, 'version': source['version'],
                        'key': team_hub.listing_key(row['member']['id'], row)}}
                else:
                    origin = {'source': 'https://github.com/' + source['repository'] + '.git',
                        'repository': source['repository'], 'source_path': source['path'],
                        'commit': source.get('commit', ''), 'confidence': '高',
                        'evidence': '从团队商店读取 GitHub 来源的文件，未执行其中的程序'}
                    if 'tree_sha' in source:
                        origin['tree_sha'] = source['tree_sha']
                items[name] = {'name': name, 'path': str(path), 'source': origin}
            else:
                suffix = source.get('commit')
                if not suffix:
                    remote, _, _ = github.parse_url('https://github.com/' + source['repository'])
                    suffix = github.git(Path(temporary), 'ls-remote', '--exit-code', remote, 'HEAD').split()[0]
                if source['path'] != '.':
                    suffix += '/' + source['path']
                url = 'https://github.com/' + source['repository'] + '/tree/' + urllib.parse.quote(suffix, safe='/')
                download = github.inspect(url)
                try:
                    found = next((s for s in download['skills'] if s['name'] == name and
                                  PurePosixPath(s['repository_path']) == PurePosixPath(source['path'])), None)
                    if found is None:
                        raise ValueError(name + ' 的来源里没有找到指定的技能，请联系上架的同事。')
                    github.safe_skill(Path(found['path']))
                    copytree(found['path'], path, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_CONTENT))
                    items[name] = {**found, 'path': str(path)}
                finally:
                    rmtree(download['temporary'], ignore_errors=True)

        class AlreadyInstalled(ValueError):
            pass

        pending = {}
        def expected():
            current = operations.load(home)[1]
            if _local(root, current)[0] in {'installed', 'update'}:
                raise AlreadyInstalled
            current_rows, reuse, skipped = _plan(card, cards, current)
            if skipped:
                raise ValueError(next(r['reason'] for r in skipped if r['name'] == root['name']))
            if set(current_rows) - items.keys():
                raise ValueError('本机所需技能已经变了，请重试安装。')
            pending.update(rows=current_rows, reuse=sorted(reuse))
            return current

        def mutate(current, checked):
            names = sorted(pending['rows'])
            for name in names:
                item = items[name]
                operations._put(home, current, item, [], [], checked)
                skill = current['skills'][name]
                skill['requires'] = rows[name]['requires']
                if _origin(skill)['kind'] == 'github' and item['source'].get('tree_sha'):
                    skill['base_tree_sha'] = item['source']['tree_sha']
                    skill['base_normalized_tree_sha'] = git_tree(home / '.skill-pilot/bases' / name)
            return names, names, {'installed': names, 'reuse': pending['reuse']}
        try:
            outcome = operations._run(home, registry, agents, expected, '从团队商店装上了「' + title + '」', mutate)
        except AlreadyInstalled:
            return existing
        installed_rows = [rows[n] for n in sorted(pending['rows']) if n in rows]
        replaces, replacement_note = _plan_replaces(
            installed_rows, exclude=set(pending['rows']) | set(pending['reuse']))
        if replaces:
            replaced_local, replace_revision = inspect_replaced_local(home, replaces, include_revision=True)
            if replaced_local:
                outcome['replaced_local'] = replaced_local
                outcome['replace_revision'] = replace_revision
                if replacement_note:
                    outcome['replacement_note'] = replacement_note
        return outcome


def _files(root):
    root = Path(root)
    resolved_root = root.resolve()
    github.safe_skill(root)
    files, blocked, size = {}, [], 0
    def walk(folder, ancestors):
        nonlocal size
        resolved = folder.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise ValueError('这个技能引用了技能文件夹外的文件，不能上架。')
        if resolved in ancestors:
            raise ValueError('这个技能的文件夹相互引用，无法完整读取。')
        for path in sorted(folder.iterdir()):
            if path.name in SKIP_CONTENT:
                continue
            if not path.resolve().is_relative_to(resolved_root):
                raise ValueError('这个技能引用了技能文件夹外的文件，不能上架。')
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                walk(path, ancestors | {resolved})
                continue
            if any(fnmatch.fnmatch(path.name.lower(), pattern) for pattern in (*SECRET_NAMES, 'team.json', 'settings.json')):
                blocked.append({'path': relative, 'reason': '文件名像账号或密钥文件，已拦下。'})
                continue
            if not path.is_file():
                raise ValueError('技能里有不能读取的文件，不能上架。')
            with path.open('rb') as stream:
                raw = stream.read(team_hub.MAX_BLOB + 1)
            if len(raw) > team_hub.MAX_BLOB:
                raise ValueError(f'「{relative}」超过单文件 {team_hub.MAX_BLOB // 1024 // 1024} MB 的上限。')
            if team_hub.has_secret_content(raw):
                blocked.append({'path': relative, 'reason': '内容疑似含有密钥或密码，已拦下。'})
                continue
            size += len(raw)
            if size > team_hub.MAX_SIZE:
                raise ValueError(f'单个技能的文件合计不能超过 {team_hub.MAX_SIZE // 1024 // 1024} MB。')
            files[relative] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                               'executable': bool(path.stat().st_mode & 0o111)}
            if len(files) > team_hub.MAX_FILES:
                raise ValueError(f'单个技能不能超过 {team_hub.MAX_FILES} 个文件。')
    walk(root, set())
    if 'SKILL.md' not in files:
        raise ValueError('SKILL.md 缺失或疑似含有密钥，整个技能不能上架。')
    return team_hub.clean_skill_manifest(files), {'count': len(files), 'size': size, 'blocked': blocked}


def _candidates(home, registry, member, cards):
    shared = _owned(cards, member['id'])
    result = {}
    for row in sharing.export_config(home, translated_description=True)['skills']:
        name = row['name']
        skill = registry['skills'][name]
        origin = _origin(skill)
        kind = ('github' if origin['kind'] == 'github' and row['source']['kind'] == 'github' and not row['local_modified'] else
                'lark' if origin['kind'] == 'lark' else 'files')
        unavailable, version = '', None
        if origin['kind'] == 'team' and origin['member'] != member['id']:
            unavailable = '从团队商店装的别人的技能，不能作为自己的技能上架。'
        try:
            github.safe_skill(home / '.skill-pilot/store' / name)
        except ValueError:
            unavailable = '这个技能引用了技能文件夹外的文件，不能上架。'
        listed = shared.get(name)
        if kind in {'files', 'github'} and not unavailable and listed:
            try:
                files, info = _files(home / '.skill-pilot/store' / name)
                if kind == 'files' or not info['blocked']:
                    version = team_hub.package_version(files)
            except (ValueError, OSError):
                version = ''
        source = dict(row['source'])
        if kind == 'github':
            recorded = skill.get('source', {})
            recorded = recorded if isinstance(recorded, dict) else {}
            tree = (skill.get('base_tree_sha') or recorded.get('tree_sha') or
                    recorded.get('record', {}).get('skillFolderHash'))
            if isinstance(tree, str) and re.fullmatch(r'[0-9a-f]{40}', tree):
                source['tree_sha'] = tree
        previous_versions = set(listed.get('previous_versions') or ()) if listed else set()
        behind_store = bool(listed and listed.get('published_via') == 'suggestion' and
                            version and listed['source'].get('version') and version != listed['source']['version'] and
                            (not row['local_modified'] or version in previous_versions))
        result[name] = {**{k: row[k] for k in ('name', 'title', 'summary', 'group', 'description', 'requires') if k in row},
            'kind': kind, 'listed': bool(listed), 'note': listed.get('note', '') if listed else '',
            'replaces': list(listed.get('replaces', [])) if listed else [],
            'replacement_note': listed.get('replacement_note', '') if listed else '',
            'behind_store': behind_store,
            'changed': bool(listed and not behind_store and kind in {'files', 'github'} and (kind != 'github' or version) and
                            (not listed['source'].get('version') or version != listed['source']['version'])),
            'unavailable': unavailable, '_source': source}
    return result


def listing_candidates(home):
    config = _connection(home)
    info = _me(config, capabilities=True)
    member = info['member']
    data = _catalog(config)
    cards = data['cards']
    _, registry, _ = operations.load(home)
    rows = _candidates(home, registry, member, cards)
    shared = _owned(cards, member['id'])
    detected = relations.detect_requires(home, registry, shared)
    for name, row in rows.items():
        row['missing_requires'] = sorted(r['requires'] for r in detected.get(name, [])
                                         if r['requires'] not in shared.get(name, {}).get('requires', []))
    packs = [p for p in data.get('packs', []) if p['member']['id'] == member['id']]
    return {'skills': [{k: v for k, v in row.items() if not k.startswith('_')} for name, row in rows.items()
                       if registry['skills'][name]['assigned_agents']], 'packs': packs,
            'pack_suggestions': relations.suggest_packs(home, registry, shared, packs) if 'packs' in data else [],
            'packs_supported': 'packs' in data, 'replaces_supported': info['replaces_supported']}


def listings_preview(home, skills, rejected=None, packs=None):
    home, registry, agents = operations.load(home)
    revision = operations.revision(home, agents)
    config = _connection(home)
    info = _me(config, capabilities=True)
    member = info['member']
    data = _catalog(config)
    cards = data['cards']
    candidates = _candidates(home, registry, member, cards)
    if (not isinstance(skills, list) or not skills or
            any(not isinstance(s, dict) or not isinstance(s.get('name'), str) or s['name'] not in candidates or
                not registry['skills'][s['name']]['assigned_agents'] for s in skills) or
            len({s['name'] for s in skills}) != len(skills)):
        raise ValueError('请从本机在用的技能里选择要上架的技能。')
    notes = {s['name']: team_hub.short_text(s.get('note', ''), '推荐语') for s in skills}
    shared = _owned(cards, member['id'])
    replaces_map, rnotes_map, explicit_clear, replaces_explicit = {}, {}, set(), set()
    for s in skills:
        name = s['name']
        if 'replaces' in s or 'replacement_note' in s:
            replaces_explicit.add(name)
        if 'replaces' in s:
            replaces_map[name] = parse_replaces(s['replaces'], own_name=name)
            if not replaces_map[name]:
                explicit_clear.add(name)
        elif name in shared:
            replaces_map[name] = list(shared[name].get('replaces', []))
        else:
            replaces_map[name] = []
        if 'replacement_note' in s:
            rnote = team_hub.short_text(s['replacement_note'], '替代说明').strip()
            if rnote and not replaces_map[name]:
                raise ValueError('填写替代说明前，请先填写替代的旧技能名称。')
            rnotes_map[name] = rnote
        elif 'replaces' not in s and name in shared and replaces_map[name]:
            rnotes_map[name] = shared[name].get('replacement_note', '')
        else:
            rnotes_map[name] = ''
    if any(replaces_map.values()) and not info['replaces_supported']:
        raise ValueError(REPLACES_UNSUPPORTED)
    rejected = [] if rejected is None else rejected
    if (not isinstance(rejected, list) or any(not isinstance(pair, list) or len(pair) != 2 or
            any(not isinstance(n, str) for n in pair) for pair in rejected)):
        raise ValueError('取消的前置依赖须逐对填写技能名称。')
    rejected = {tuple(pair) for pair in rejected}
    detected, requires = {}, {}
    detection_registry = {'skills': dict(registry['skills'])}
    auto = {}
    selected = set(notes)
    pending = list(sorted(selected))
    while pending:
        detected.update(relations.detect_requires(home, detection_registry, pending))
        batch, pending = pending, []
        for parent in batch:
            skill = registry['skills'].get(parent, {})
            requires[parent] = sorted(set(skill.get('requires', [])) | {r['requires'] for r in detected.get(parent, [])
                                                                      if (parent, r['requires']) not in rejected})
            if parent in registry['skills']:
                detection_registry['skills'][parent] = {**skill, 'requires': requires[parent]}
            for dependency in requires[parent]:
                if dependency in shared and dependency not in selected:
                    continue
                auto.setdefault(dependency, set()).add(parent)
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
    payload, public, errors = {}, {}, {}
    for name in sorted(selected):
        row = candidates.get(name)
        if row is None:
            errors[name] = '本机缺少这个所需技能。'
            continue
        if row['unavailable']:
            errors[name] = row['unavailable']
            continue
        listing = {k: row[k] for k in ('name', 'title', 'summary', 'group', 'description', 'requires') if k in row}
        listing['requires'] = requires[name]
        listing['note'] = notes.get(name, '')
        if name in replaces_map:
            if replaces_map[name]:
                listing['replaces'] = replaces_map[name]
            if rnotes_map.get(name):
                listing['replacement_note'] = rnotes_map[name]
        elif name in shared and shared[name].get('replaces'):
            listing['replaces'] = list(shared[name]['replaces'])
            if shared[name].get('replacement_note'):
                listing['replacement_note'] = shared[name]['replacement_note']
        files_info = {'count': 0, 'size': 0, 'blocked': []}
        version = None
        download_note = ''
        try:
            if row['kind'] == 'files':
                local_files, local_info = _files(home / '.skill-pilot/store' / name)
                local_version = team_hub.package_version(local_files)
                same_as_online = bool(name in shared and shared[name]['source'].get('kind') == 'files' and
                                      shared[name]['source'].get('version') == local_version)
                if info['suggestions_supported'] and name in shared and (row.get('behind_store') or same_as_online):
                    listing['source'] = {'kind': 'files', 'preserve_files': True}
                    if row.get('behind_store') and 'description' in shared[name]:
                        listing['description'] = shared[name]['description']
                    version = shared[name]['source'].get('version')
                    files_info = {'count': shared[name]['source'].get('file_count', 0),
                                  'size': shared[name]['source'].get('size', 0), 'blocked': []}
                else:
                    files, files_info = local_files, local_info
                    listing['source'] = {'kind': 'files', 'files': files}
                    version = local_version
            else:
                listing['source'] = dict(row['_source']) if row['kind'] == 'github' else {'kind': 'lark'}
                if row['kind'] == 'github':
                    try:
                        files, files_info = _files(home / '.skill-pilot/store' / name)
                        if not files_info['blocked']:
                            listing['source']['files'] = files
                            version = team_hub.package_version(files)
                    except (ValueError, OSError):
                        pass
                    if version is None:
                        download_note = '同事装的时候从 GitHub 下载。'
            listing = team_hub.clean_listing(listing)
            if name in replaces_explicit:
                payload[name] = ({**listing, 'replaces': []}
                                 if (name in explicit_clear and info['replaces_supported']) else listing)
            else:
                # Omit replaces/replacement_note when not edited so the hub preserves latest server values.
                payload[name] = {k: v for k, v in listing.items() if k not in {'replaces', 'replacement_note'}}
            if (info['suggestions_supported'] and row['kind'] == 'files' and name in shared and
                    not payload[name]['source'].get('preserve_files') and shared[name]['source'].get('version')):
                payload[name]['expected_version'] = shared[name]['source']['version']
            public[name] = {**{k: v for k, v in listing.items() if k != 'source'},
                            'title': listing.get('title', name), 'kind': row['kind'],
                            'auto': sorted(auto.get(name, set())), 'files': files_info, 'version': version,
                            **({'download_note': download_note} if download_note else {})}
        except (ValueError, OSError) as error:
            errors[name] = str(error) if isinstance(error, ValueError) else '技能文件没能完整读取，请检查后重试。'
    while True:
        before = len(errors)
        for name in selected - errors.keys():
            missing = set(requires.get(name, [])) & errors.keys()
            if missing:
                errors[name] = '所需技能不能上架：' + '、'.join(sorted(missing))
        if len(errors) == before:
            break
    valid = sorted(selected - errors.keys())
    saved = {'listings': [payload[n] for n in valid], 'revision': revision, 'connection': _connection_version(config)}
    result = {'listings': [public[n] for n in valid], 'errors': [{'name': n, 'reason': r} for n, r in sorted(errors.items())],
              'detected': [{'skill': n, **r, 'adopted': (n, r['requires']) not in rejected}
                           for n in sorted(selected) for r in detected.get(n, [])],
              'packs': [], 'packs_supported': 'packs' in data, 'replaces_supported': info['replaces_supported'],
              'message': f'可上架 {len(valid)} 个，不能上架 {len(errors)} 个。'}
    if 'packs' in data:
        existing = [p for p in data['packs'] if p['member']['id'] == member['id']]
        if packs is None:
            proposed = [*existing, *relations.suggest_packs(home, registry, set(valid) | shared.keys(), existing)]
        else:
            proposed = team_hub.clean_packs(packs)
            involved = selected | {n for p in proposed for n in p['skills']}
            identities = {p['id'] for p in proposed}
            proposed = [{**p, 'skills': [n for n in p['skills'] if n not in errors]} for p in proposed]
            proposed = [p for p in proposed if len(p['skills']) >= 2]
            proposed += [p for p in existing if p['id'] not in identities and not involved.intersection(p['skills'])]
        checked = team_hub.clean_packs(proposed, {**shared, **{n: payload[n] for n in valid}})
        saved['packs'] = checked
        result['packs'] = [{**p, **({'why': original['why']} if 'why' in original else {}),
                            'existing': any(old['id'] == p['id'] for old in existing)}
                           for p, original in zip(checked, proposed)]
    return saved, result


def _read_skill_file(digest, root, relative, size):
    try:
        path = (root / relative).resolve()
        if path.is_relative_to(root.resolve()) and path.is_file():
            with path.open('rb') as stream:
                raw = stream.read(size + 1)
            if len(raw) == size and hashlib.sha256(raw).hexdigest() == digest:
                return raw
    except (OSError, RuntimeError):
        pass
    raise ValueError('本机技能文件已经变了或没能读取，请重新预览后再上架。')


def listings_apply(home, review):
    home, _, agents = operations.load(home)
    guard(home / '.skill-pilot', home)
    if operations.revision(home, agents) != review['revision']:
        raise ValueError('本机技能文件已经变了，请重新预览后再上架。')
    config = _connection(home)
    if _connection_version(config) != review['connection']:
        raise ValueError('分享站设置已经变了，请重新预览。')
    if not review['listings']:
        raise ValueError('没有可以上架的技能，请先处理预览里的原因。')
    capabilities = _me(config, capabilities=True)
    payload = copy.deepcopy({k: review[k] for k in ('listings', 'packs') if k in review})
    # The manifest is larger than the inline file metadata, so this budget is conservative.
    wire_size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    by_digest = {}
    for row in payload['listings']:
        files = row['source'].get('files')
        if files is None:
            continue
        root = home / '.skill-pilot/store' / row['name']
        size = sum(f['size'] for f in files.values())
        encoded_size = sum(4 * ((f['size'] + 2) // 3) for f in files.values())
        if (len(files) <= team_hub.INLINE_MAX_FILES and size <= team_hub.INLINE_MAX_SIZE and
                wire_size + encoded_size <= MAX_BODY):
            # A small skill stays visible on older dashboards even when published with a large one.
            row['source']['files'] = [
                {'path': path, 'content': base64.b64encode(
                    _read_skill_file(f['sha256'], root, path, f['size'])).decode('ascii'),
                 'executable': f['executable']} for path, f in files.items()]
            wire_size += encoded_size
        else:
            for relative, f in files.items():
                by_digest[f['sha256']] = (root, relative, f['size'])
    route = '/api/listings/me'
    if by_digest:
        if not capabilities['skill_file_upload']:
            raise ValueError('这次上架的技能文件较多或较大，请联系管理员更新分享站后重试。')
        result, _ = _upload_missing(config, route, payload, by_digest, read=_read_skill_file)
    else:
        result = _request(config, route, 'PUT', payload)
    return {**result, 'message': '技能已上架。'}


def update_listing(home, name, connection):
    """Refresh one owned listing without changing its note or pack membership."""
    guard(Path(home) / '.skill-pilot', home)
    if _connection_version(_connection(home)) != connection:
        raise ValueError('分享站连接已经变了，请回到我的上架重新更新。')
    _, member, cards = _fetch(home)
    owned = _owned(cards, member['id'])
    if name not in owned:
        raise ValueError('这个技能已不在我的上架中，请刷新列表。')
    saved, result = listings_preview(home, [{'name': name, 'note': owned[name].get('note', '')}], packs=[])
    if saved['connection'] != connection:
        raise ValueError('分享站连接已经变了，请回到我的上架重新更新。')
    if result['errors']:
        raise ValueError('；'.join(row['name'] + '：' + row['reason'] for row in result['errors']))
    if len(result['listings']) != 1 or result['listings'][0]['name'] != name:
        raise ValueError('有新增的前置依赖需要核对，请单独点击这个技能的“上架新版”。')
    row = result['listings'][0]
    if row['files']['blocked'] or not row['version']:
        raise ValueError('技能文件不能完整上传，请单独点击“上架新版”查看原因。')
    # Omitting packs preserves the hub's current packs, including concurrent edits.
    saved.pop('packs', None)
    if owned[name]['source'].get('version') == row['version']:
        return
    listings_apply(home, saved)
    _, current_member, cards = _fetch(home)
    current = _owned(cards, current_member['id']).get(name)
    if not current or current['source'].get('version') != row['version']:
        raise ValueError('分享站尚未读到这个技能的新版，请稍后重试。')


def packs_apply(home, packs):
    guard(Path(home) / '.skill-pilot', home)
    config, member, data = _fetch(home, with_packs=True)
    if 'packs' not in data:
        raise ValueError(PACKS_UNSUPPORTED)
    checked = team_hub.clean_packs(packs, _owned(data['cards'], member['id']))
    result = _request(config, '/api/packs/me', 'PUT', {'packs': checked})
    return {**result, 'message': '技能包已保存。'}


def listings_remove(home, names, confirmed=True, replaced_by=None, replacement_note=None):
    if confirmed:
        guard(Path(home) / '.skill-pilot', home)
    config = _connection(home)
    info = _me(config, capabilities=True)
    member = info['member']
    cards = _catalog(config)['cards']
    rows = _owned(cards, member['id'])
    if (not isinstance(names, list) or not names or
            any(not isinstance(n, str) or n not in rows for n in names)):
        raise ValueError('请从自己上架的技能里选择要下架的技能。')
    pending, order = set(names), []
    parents = [n for n, row in rows.items() if n not in pending and set(row['requires']) & pending]
    if parents:
        raise ValueError('这些上架的技能仍需要它们：' + '、'.join(sorted(parents)))
    if replaced_by is not None and not isinstance(replaced_by, str):
        raise ValueError('推荐替代的新技能名称格式不正确。')
    if replacement_note is not None and not isinstance(replacement_note, str):
        raise ValueError('替代说明须是文字。')
    replaced_by = (replaced_by or '').strip()
    replacement_note = team_hub.short_text(replacement_note, '替代说明').strip() if (replacement_note or '').strip() else ''
    existing_links = sorted(
        n for n, row in rows.items()
        if n not in pending and set(row.get('replaces', [])) & pending
    )
    if replaced_by or replacement_note:
        if not info['replaces_supported']:
            raise ValueError(REPLACES_UNSUPPORTED)
        if replaced_by:
            if replaced_by not in rows or replaced_by in pending:
                raise ValueError('请选择你已上架且本次不下架的另一个技能作为推荐替代技能。')
            # Pre-validate combined replaces cap before deleting any listing in a batch.
            team_hub.clean_replaces([*rows[replaced_by].get('replaces', []), *sorted(pending)], own_name=replaced_by)
        elif not existing_links:
            raise ValueError('填写替代说明时，请同时指定推荐替代的新技能。')
    if not confirmed:
        message = f'将从分享站下架 {len(names)} 个技能：' + '、'.join(sorted(names)) + '。同事已经装上的不受影响。'
        if replaced_by:
            target_title = rows[replaced_by].get('title', replaced_by)
            message += f'同时关联推荐替代技能「{target_title}」' + (f'（{replacement_note}）' if replacement_note else '') + '。'
        elif existing_links:
            linked_titles = '、'.join(rows[n].get('title', n) for n in existing_links)
            message += f'已关联的新技能（{linked_titles}）会继续保留替代提示' + (f'，并更新说明为「{replacement_note}」' if replacement_note else '') + '。'
        return {'names': sorted(names), 'needs_confirmation': True,
                **({'replaced_by': replaced_by} if replaced_by else {}),
                **({'replacement_note': replacement_note} if replacement_note else {}),
                **({'existing_replacements': existing_links} if existing_links else {}),
                'message': message}
    while pending:
        ready = sorted(n for n in pending if not any(n in rows[p]['requires'] for p in pending - {n}))
        if not ready:
            # The hub deletes one listing at a time. Break only the internal
            # edges of a cycle whose entire membership is being removed.
            replacements = []
            for name in sorted(pending):
                row = copy.deepcopy(rows[name])
                row['requires'] = [n for n in row['requires'] if n not in pending]
                if 'version' in row['source']:
                    data = _request(config, '/api/packages/' + member['id'] + '/' + name + '?version=' + row['source']['version'])
                    files = team_hub.clean_files(data.get('files'))
                    if team_hub.package_version(files) != row['source']['version']:
                        raise RequestError(502, FORMAT_ERROR)
                    row['source'] = {k: v for k, v in row['source'].items() if k not in {'version', 'file_count', 'size'}}
                    row['source']['files'] = files
                replacements.append(team_hub.clean_listing(row))
            _request(config, '/api/listings/me', 'PUT', {'listings': replacements})
            order.extend(sorted(pending))
            break
        order.extend(ready)
        pending.difference_update(ready)
    for name in order:
        body = None
        if replaced_by:
            body = {'replaced_by': replaced_by, **({'replacement_note': replacement_note} if replacement_note else {})}
        elif replacement_note and any(name in rows[other].get('replaces', []) for other in existing_links):
            body = {'replacement_note': replacement_note}
        _request(config, '/api/listings/me/' + name, 'DELETE', body)
    return {'message': '技能已下架。', **({'replaced_by': replaced_by} if replaced_by else {})}


def publish(home, names, notes=None, rejected=None, skip_packs=(), confirmed=False, pack=None,
            replaces=None, clear_replaces=(), replacement_notes=None):
    """Command-line publishing: the dashboard preview, then upload and read back."""
    notes = notes or {}
    replaces = replaces or {}
    clear_replaces = set(clear_replaces or ())
    replacement_notes = replacement_notes or {}
    if set(notes) - set(names):
        raise ValueError('推荐语只能写给这次要上架的技能。')
    if set(replaces) - set(names):
        raise ValueError('替代的旧技能只能标记给这次要上架的技能。')
    if clear_replaces - set(names):
        raise ValueError('清除替代标记只能用于这次要上架的技能。')
    if set(replacement_notes) - set(names):
        raise ValueError('替代说明只能写给这次要上架的技能。')
    if clear_replaces & set(replaces):
        raise ValueError('同一个技能不能同时设置和清除替代旧技能。')
    skills = []
    for n in names:
        item = {'name': n, **({'note': notes[n]} if notes.get(n) else {})}
        if n in clear_replaces:
            item['replaces'] = []
            item['replacement_note'] = ''
        elif n in replaces:
            item['replaces'] = parse_replaces(replaces[n], own_name=n)
        if n in replacement_notes:
            item['replacement_note'] = replacement_notes[n]
        skills.append(item)
    saved, result = listings_preview(home, skills, rejected)
    if skip_packs or pack is not None:
        unknown = set(skip_packs) - {p['id'] for p in result['packs']}
        if unknown:
            raise ValueError('预览里没有这些技能包：' + '、'.join(sorted(unknown)))
        packs = [{k: p[k] for k in ('id', 'title', 'note', 'skills') if k in p}
                 for p in result['packs'] if p['id'] not in skip_packs]
        if pack is not None:
            if not result['packs_supported']:
                raise ValueError('分享站版本较旧，暂不能整理技能包，请联系管理员更新分享站。')
            title = tags.clean_tag(pack)
            # The same title means the same pack: named skills join it, nothing leaves it.
            mine = next((p for p in packs if p['title'] == title), None) or {
                'id': 'pack-' + hashlib.sha256(title.encode()).hexdigest()[:10], 'title': title, 'skills': []}
            mine['skills'] = list(dict.fromkeys([*mine['skills'], *names]))
            if len(mine['skills']) < 2:
                raise ValueError('技能包至少要有 2 个技能，请多列一个再试。')
            # A skill sits in one pack only, so the named ones leave any other pack.
            others = [{**p, 'skills': [n for n in p['skills'] if n not in names]} for p in packs if p is not mine]
            packs = [p for p in others if len(p['skills']) >= 2] + [mine]
        saved, result = listings_preview(home, skills, rejected, packs)
        if pack is not None:
            failed = set(names) & {row['name'] for row in result['errors']}
            if failed and confirmed:
                raise ValueError('技能包里的这些技能不能上架，这次没有上传：' + '、'.join(sorted(failed)) + '。请先看预览里的原因。')
            for row in result['packs']:
                row['requested'] = row['id'] == mine['id']
    if not confirmed:
        return {**result, 'needs_confirmation': bool(result['listings'])}
    listings_apply(home, saved)
    _, member, cards = _fetch(home)
    shared = _owned(cards, member['id'])
    missing = [row['name'] for row in result['listings'] if row['name'] not in shared or
               (row['version'] and shared[row['name']]['source'].get('version') != row['version']) or
               ((row['name'] in replaces or row['name'] in clear_replaces) and
                shared[row['name']].get('replaces', []) != row.get('replaces', []))]
    if missing:
        raise ValueError('上架后分享站没有读到这些技能的新版本：' + '、'.join(missing) + '，请稍后重试。')
    return {**result, 'published': [row['name'] for row in result['listings']],
            'message': f'已上架 {len(result["listings"])} 个技能，分享站上已经能看到。'}


def unpublish(home, names, confirmed=False, replaced_by=None, replacement_note=None):
    result = listings_remove(home, names, confirmed, replaced_by=replaced_by, replacement_note=replacement_note)
    if not confirmed:
        return result
    _, member, cards = _fetch(home)
    owned = _owned(cards, member['id'])
    left = sorted(set(names) & owned.keys())
    if left:
        raise ValueError('分享站上还能看到这些技能：' + '、'.join(left) + '，请稍后重试。')
    message = f'已下架 {len(names)} 个技能，分享站上已经看不到了。'
    if replaced_by and replaced_by in owned:
        message += f'已将替代提示关联到「{owned[replaced_by].get("title", replaced_by)}」。'
    return {'names': sorted(names), **({'replaced_by': replaced_by} if replaced_by else {}), 'message': message}


def _resolve_card(cards, name, author=None):
    matches = [c for c in cards if c['key'] == name or any(name in {r['name'], r.get('title')} for r in c['listings'])]
    if author is not None:
        matches = [c for c in matches if any(r['member']['name'] == author for r in c['listings'])]
    if not matches:
        raise ValueError(f'团队商店里没有找到「{name}」，可以先运行 skill-pilot team list 查看。')
    if len(matches) > 1:
        authors = '、'.join(sorted({c['listings'][0]['member']['name'] for c in matches}))
        raise ValueError(f'团队商店里有多个「{name}」（上架人：{authors}），请用 --author 指定上架人。')
    return matches[0]


def _suggestion_connection(home: Path) -> tuple[dict, dict]:
    config = _connection(home)
    info = _me(config, capabilities=True)
    if not info['suggestions_supported']:
        raise ValueError(SUGGESTIONS_UNSUPPORTED)
    return config, info['member']


def _preserve_windows_modes(files, baseline):
    # Windows cannot represent the Unix execute bits of a downloaded script.
    if os.name == 'nt':
        for path in files.keys() & baseline.keys():
            files[path]['executable'] = baseline[path]['executable']


def _suggestion_root(home, name):
    folder = home / '.skill-pilot/store'
    root = folder / name
    if not root.resolve().is_relative_to(folder):
        raise ValueError('这个技能位于技能库之外，无法提交改动。')
    return root


def suggestion_preview(home: Path, card_key: str, message: str = '', *, author: str | None = None) -> tuple[dict, dict]:
    config, member = _suggestion_connection(home)
    card = _resolve_card(_catalog(config)['cards'], card_key, author)
    row = card['listings'][0]
    target, name = row['member']['id'], row['name']
    if row['source']['kind'] != 'files':
        raise ValueError(team_hub.FILES_ONLY)
    if member['id'] == target:
        raise ValueError(team_hub.OWN_SKILL)
    home, registry, agents = operations.load(home)
    state, local_name = _local(row, registry)
    root = _suggestion_root(home, local_name)
    if (state not in {'installed', 'update'} or registry['skills'][local_name].get('withdrawn') or
            not root.is_dir()):
        raise ValueError('请先安装并保留这位作者上架的技能，再提交本机的改动。')
    revision = operations.revision(home, agents)
    files, info = _files(root)
    manifest = files['SKILL.md']
    try:
        text = _read_skill_file(manifest['sha256'], root, 'SKILL.md', manifest['size']).decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('SKILL.md 的文字编码不正确，请保存为 UTF-8 后重试。') from None
    if metadata(text).get('name') != name:
        raise ValueError('技能文件里的名称和上架的名称不一致。')
    published_version = row['source']['version']
    installed_origin = _origin(registry['skills'][local_name])
    installed_version = (installed_origin.get('version')
                         if installed_origin.get('kind') == 'team' and installed_origin.get('member') == target
                         and installed_origin.get('name') == name else '')
    base = installed_version if isinstance(installed_version, str) and re.fullmatch(r'[0-9a-f]{64}', installed_version) else published_version
    old = {}
    with tempfile.TemporaryDirectory(prefix='skill pilot suggestion ') as tmp:
        published = package(config, target, name, published_version, Path(tmp) / 'published', manifest_out=old)
        _preserve_windows_modes(files, old)
        blocked_existing = sorted(item['path'] for item in info['blocked'] if item['path'] in old)
        if blocked_existing:
            raise ValueError('这些本机文件疑似包含密钥或密码已被拦下，为避免误删线上已有文件，请先清理敏感内容后再提交：' +
                             '、'.join(blocked_existing))
        version = team_hub.package_version(files)
        if version == base:
            if base != published_version:
                raise ValueError('本机还没有修改这个技能的内容；原作者已发布新版，请先更新后再修改并提交。')
            raise ValueError(team_hub.NO_CHANGES)
        if version == published_version:
            raise ValueError(team_hub.NO_CHANGES)
        comparison = team_hub.suggestion_diff(
            old, files, lambda path: (published / path).read_bytes(),
            lambda path: _read_skill_file(files[path]['sha256'], root, path, files[path]['size']))
    base_revision = row.get('revision') if base == published_version else None
    saved = {'target_member_id': target, 'skill_name': name, 'card_key': card['key'], 'base_version': base,
             'version': version, 'files': files, 'local_name': local_name, 'revision': revision,
             'connection': _connection_version(config), 'blocked': info['blocked']}
    result = {'skill_name': name, 'title': row.get('title', name), 'target_member_name': row['member']['name'],
              'base_version': base, 'base_revision': base_revision, 'current_version': published_version,
              'stale': base != published_version, 'file_count': len(files), 'size': info['size'],
              'blocked': info['blocked'], **comparison, 'needs_confirmation': True,
              'message': team_hub.suggestion_text(message) if message else '请核对文件改动，确认后提交给原作者。'}
    return saved, result


def suggestion_submit(home: Path, saved: dict, message: str) -> dict:
    guard(home / '.skill-pilot', home)
    message = team_hub.suggestion_text(message)
    home, registry, agents = operations.load(home)
    config = _connection(home)
    if _connection_version(config) != saved['connection']:
        raise ValueError('分享站设置已经变了，请重新预览。')
    if operations.revision(home, agents) != saved['revision']:
        raise ValueError('本机技能文件已经变了，请重新预览后再提交。')
    name = saved['local_name']
    if name not in registry['skills'] or name != saved['skill_name'] or not re.fullmatch(team_hub.NAME, name):
        raise ValueError('技能名称已经变了，请重新预览。')
    skill = registry['skills'][name]
    origin = _origin(skill)
    if (skill.get('withdrawn') or origin['kind'] != 'team' or origin['member'] != saved['target_member_id'] or
            origin['name'] != name):
        raise ValueError('本机技能与原作者的上架记录不一致，请重新预览。')
    root = _suggestion_root(home, name)
    files, _ = _files(root)
    _preserve_windows_modes(files, saved['files'])
    if files != saved['files']:
        raise ValueError('本机技能文件已经变了，请重新预览后再提交。')
    by_digest = {f['sha256']: (root, path, f['size']) for path, f in files.items()}
    payload = {k: saved[k] for k in ('target_member_id', 'skill_name', 'base_version')}
    created, _ = _upload_missing(config, '/api/suggestions', {**payload, 'message': message, 'files': files},
                                 by_digest, read=_read_skill_file, method='POST')
    return {**created, 'message': '改进建议已提交，等待原作者审核。'}


def suggestions_list(home: Path, role: str = 'all', status: str = 'pending', skill: str = '') -> dict:
    config = _connection(home)
    if not _me(config, capabilities=True)['suggestions_supported']:
        return {'supported': False, 'suggestions': []}
    query = urllib.parse.urlencode({'role': role, 'status': status, **({'skill': skill} if skill else {})})
    return {**_request(config, '/api/suggestions?' + query), 'supported': True}


def _suggestion_route(suggestion_id: str) -> str:
    if not isinstance(suggestion_id, str) or not re.fullmatch('[0-9a-f]{32}', suggestion_id):
        raise ValueError('建议编号不正确。')
    return '/api/suggestions/' + suggestion_id


def suggestion_detail(home: Path, suggestion_id: str) -> dict:
    config, _ = _suggestion_connection(home)
    return _request(config, _suggestion_route(suggestion_id))


def suggestion_accept(home: Path, suggestion_id: str, review_note: str = '', allow_stale: bool = False,
                      expected_current_version: str = '') -> dict:
    guard(home / '.skill-pilot', home)
    config, _ = _suggestion_connection(home)
    payload = {'review_note': review_note, 'allow_stale': allow_stale}
    if expected_current_version:
        payload['expected_current_version'] = expected_current_version
    return _request(config, _suggestion_route(suggestion_id) + '/accept', 'POST', payload)


def suggestion_reject(home: Path, suggestion_id: str, review_note: str = '') -> dict:
    guard(home / '.skill-pilot', home)
    config, _ = _suggestion_connection(home)
    return _request(config, _suggestion_route(suggestion_id) + '/reject', 'POST', {'review_note': review_note})


def suggestion_withdraw(home: Path, suggestion_id: str) -> dict:
    guard(home / '.skill-pilot', home)
    config, _ = _suggestion_connection(home)
    return _request(config, _suggestion_route(suggestion_id), 'DELETE')


def find(home, name, author=None):
    """Resolve a store card by skill name or Chinese title for command-line installs."""
    home, registry, agents = operations.load(home)
    _, member, cards = _fetch(home)
    card = _resolve_card(cards, name, author)
    root = (next((r for r in card['listings'] if r['member']['name'] == author), card['listings'][0])
            if author is not None else card['listings'][0])
    title = next((r['title'] for r in card['listings'] if 'title' in r), root['name'])
    state, local_name = _local(root, registry)
    mine = any(r['member']['id'] == member['id'] for r in card['listings']) and root['name'] in registry['skills']
    base_result = {'key': card['key'], 'name': local_name, 'title': title,
                   'author': root['member']['name'], 'author_id': root['member']['id']}
    if mine or state in {'installed', 'update'}:
        installed_name = root['name'] if mine else local_name
        if mine or state == 'installed':
            replaces, replacement_note = _plan_replaces([root], exclude={installed_name, root['name']})
            replaced_local = inspect_replaced_local(home, replaces, registry=registry, agents=agents) if replaces else []
        else:
            replaces, replacement_note, replaced_local = [], '', []
        return {**base_result, 'installed': [], 'reuse': [local_name],
                **({'replaces': replaces} if replaces else {}),
                **({'replacement_note': replacement_note} if replacement_note else {}),
                **({'replaced_local': replaced_local} if replaced_local else {}),
                'message': f'本机已经有「{title}」了。'}
    selected_card = {**card, 'listings': [root, *(r for r in card['listings'] if r is not root)]}
    rows, reuse, skipped = _plan(selected_card, cards, registry)
    if skipped:
        raise ValueError(next(r['reason'] for r in skipped if r['name'] == root['name']))
    extra = sorted(set(rows) - {root['name']})
    replaces, replacement_note = _plan_replaces(rows.values(), exclude=set(rows) | set(reuse))
    replaced_local = inspect_replaced_local(home, replaces, registry=registry, agents=agents) if replaces else []
    return {**base_result, 'installed': sorted(rows), 'reuse': sorted(reuse),
            **({'replaces': replaces} if replaces else {}),
            **({'replacement_note': replacement_note} if replacement_note else {}),
            **({'replaced_local': replaced_local} if replaced_local else {}),
            'message': f'将从团队商店装上「{title}」（{base_result["author"]} 上架）' +
                       ('，一起装上它需要的 ' + '、'.join(extra) if extra else '') + '。'}


def _resolve_replace_actions(replaced_local, raw_actions):
    known = [item['name'] for item in replaced_local]
    actions = {name: 'keep' for name in known}
    if not raw_actions:
        return actions
    if isinstance(raw_actions, dict):
        for name, act in raw_actions.items():
            if name not in actions:
                raise ValueError(f'「{name}」不在本次检测到的本机旧版技能中。')
            if act not in {'keep', 'withdraw', 'delete'}:
                raise ValueError(f'「{name}」的处理方式只能选 keep（保留）、withdraw（撤下）或 delete（删除）。')
            actions[name] = act
        return actions
    if isinstance(raw_actions, (list, tuple)):
        for entry in raw_actions:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError('--replace-action 请写成 keep / withdraw / delete，或 旧技能=动作。')
            if '=' in entry:
                target, _, act = entry.partition('=')
                target, act = target.strip(), act.strip()
                if target not in actions:
                    raise ValueError(f'「{target}」不在本次检测到的本机旧版技能中。')
                if act not in {'keep', 'withdraw', 'delete'}:
                    raise ValueError(f'「{target}」的处理方式只能选 keep（保留）、withdraw（撤下）或 delete（删除）。')
                actions[target] = act
            else:
                act = entry.strip()
                if act not in {'keep', 'withdraw', 'delete'}:
                    raise ValueError('--replace-action 请写成 keep（保留）、withdraw（撤下）或 delete（删除）。')
                for name in known:
                    actions[name] = act
        return actions
    raise ValueError('旧版技能处理方式格式不正确。')


def install_named(home, name, author=None, agents=(), confirmed=False, replace_actions=None):
    agents = list(dict.fromkeys(agents))
    if not set(agents) <= set(operations.load(home)[2]):
        raise ValueError('请选择已接入的 agent，可以先运行 skill-pilot status 查看编号。')
    plan = find(home, name, author)
    plan['agents'] = agents
    replaced_local = plan.get('replaced_local', [])
    resolved_actions = _resolve_replace_actions(replaced_local, replace_actions) if (replaced_local or replace_actions) else {}
    has_cleanup = any(act in {'withdraw', 'delete'} for act in resolved_actions.values())
    allowed_replaces = [item['name'] for item in replaced_local]
    if has_cleanup:
        validate_replace_cleanup(home, resolved_actions, allowed=allowed_replaces,
                                 required_present=None if plan['installed'] else [plan['name']])
    if resolved_actions:
        plan['replace_actions'] = resolved_actions
    if agents:
        plan['message'] += '然后分配给 ' + '、'.join(agents) + '，会一起带上它需要的技能。'
    elif plan['installed']:
        plan['message'] += '只装进技能库，暂不分配给 agent；要直接用，加 --agent <编号>。'
    if replaced_local:
        old_titles = '、'.join(item['title'] for item in replaced_local)
        plan['message'] += f'本机已装有被替代的旧版：{old_titles}。'
        if has_cleanup:
            labels_map = {'keep': '保留', 'withdraw': '撤下', 'delete': '删除'}
            summary = '、'.join(f'{n}（{labels_map[act]}）' for n, act in resolved_actions.items())
            plan['message'] += f'确认后将按以下方式处理旧版：{summary}。'
        else:
            plan['message'] += '默认保留旧版，不会自动删除；如需撤下或删除，可加 --replace-action withdraw / delete。'
    if not confirmed:
        return {**plan, 'needs_confirmation': bool(plan['installed'] or agents or has_cleanup)}
    guard(Path(home) / '.skill-pilot', home)
    result = {**plan, 'operations': [], 'message': f'本机已经有「{plan["title"]}」了'}
    if plan['installed']:
        installed = install(home, plan['key'], member_id=plan.get('author_id'))
        result.update(installed=installed['installed'], reuse=installed['reuse'], message=installed['message'])
        if 'id' in installed:
            result['operations'].append(installed['id'])
        if installed['installed']:
            try:
                catalog(home)
            except (ValueError, OSError):
                pass
    if agents:
        assigned = operations.apply_assign([{'skill': plan['name'], 'agent': a, 'enabled': True} for a in agents],
                                           confirmed=True, home=home)
        result['message'] += '；' + assigned['message']
        result['new_session_agents'] = assigned.get('new_session_agents', [])
        if 'id' in assigned:
            result['operations'].append(assigned['id'])
    else:
        result['new_session_agents'] = []
    if has_cleanup:
        required = sorted(set(result.get('installed') or []) | {plan['name']})
        post_rev = operations.revision(home, operations.load(home)[2])
        cleaned = cleanup_replaced(home, resolved_actions, confirmed=True, expected=post_rev,
                                   allowed=allowed_replaces, required_present=required)
        result['replaced_cleanup'] = cleaned
        result['operations'].extend(cleaned.get('operations', []))
        result['new_session_agents'] = list(dict.fromkeys([
            *result['new_session_agents'], *cleaned.get('new_session_agents', [])]))
        result['message'] += '；' + cleaned['message']
        try:
            catalog(home)
        except (ValueError, OSError):
            pass
    return result


def rate(home, body, remove=False):
    guard(Path(home) / '.skill-pilot', home)
    card = next((c for c in catalog(home)['cards'] if c['key'] == body.get('key')), None)
    if card is None:
        raise ValueError('这个技能已经下架，请刷新商店。')
    if card['local']['status'] == 'mine':
        raise ValueError('自己上架的不能评价。')
    if not card['can_rate']:
        raise ValueError('装过才能评价。')
    data = {'key': card['key']}
    if not remove:
        data.update({k: body[k] for k in ('stars', 'text') if k in body})
    result = _request(_connection(home), '/api/ratings', 'DELETE' if remove else 'PUT', data)
    return {**result, 'message': '评价已删除。' if remove else '评价已保存。'}
