"""Small, text-only feedback envelope shared by the dashboard and team hub."""
import ipaddress
import json
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

MAX_BODY = 64 * 1024
MAX_RECORDS = 200
MAX_BYTES = 1024 * 1024
COOLDOWN = 300
LIMITS = {'error_id': 64, 'fingerprint': 64, 'occurred_at': 64,
          'app_version': 64, 'install_mode': 40, 'os': 160, 'python_version': 40,
          'tools_summary': 500, 'agents_summary': 500, 'source': 80, 'action': 160,
          'user_message': 400, 'error_type': 80, 'detail': 6000, 'user_note': 1500}


def text(value, limit):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value if c in '\n\t' or
                   not unicodedata.category(c).startswith('C'))[:limit].strip()


def clean(data):
    return {key: text(data.get(key), limit) for key, limit in LIMITS.items()}


def envelope(report):
    env = report['environment']
    row = next(iter(report['errors']), {})
    return clean({**row, 'occurred_at': row.get('time', ''),
                  **{k: env.get(k, '') for k in ('app_version', 'install_mode', 'os', 'python_version')},
                  'tools_summary': '; '.join(f"{name}: {tool['version'] if tool['available'] else '未安装'}"
                                            for name, tool in env['tools'].items()),
                  'agents_summary': ', '.join(env['agent_types']) + f"；技能数：{env['skill_count']}",
                  'source': row.get('source', 'manual'), 'action': row.get('action', '主动反馈'),
                  'user_note': report['note']})


def webhook_url(value):
    if not value:
        return ''
    try:
        url = urllib.parse.urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password or
                url.fragment or any(c.isspace() or ord(c) < 32 for c in value) or
                url.hostname.lower() == 'localhost' or url.hostname.lower().endswith('.localhost')):
            raise ValueError
        url.port  # Validate the port without printing the supplied address.
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError
    except ValueError:
        raise ValueError('反馈接收地址必须是有效的公网 HTTPS 地址，不能带账号或密码。') from None
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def forward(url, payload):
    request = urllib.request.Request(webhook_url(url),
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            raw = response.read(MAX_BODY + 1)
            if not 200 <= response.status < 300 or len(raw) > MAX_BODY:
                raise ValueError
        if raw:
            result = json.loads(raw)
            if not isinstance(result, dict) or any(result.get(key, 0) != 0 for key in ('code', 'StatusCode', 'errcode')):
                raise ValueError
    except (OSError, HTTPException, ValueError) as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        # The receiver may echo its secret URL or the submitted report. Never return it.
        raise ValueError('飞书暂时没能接收，分享站已保留备份。请复制或导出诊断信息发给维护者。') from None
