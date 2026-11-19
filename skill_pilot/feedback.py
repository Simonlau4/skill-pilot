"""Small feedback envelope and validated screenshot helpers shared by dashboard and team hub."""
import base64
import binascii
import hashlib
import ipaddress
import json
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

MAX_BODY = 64 * 1024
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BODY = 8 * 1024 * 1024
MAX_RECORDS = 200
MAX_BYTES = 1024 * 1024
MAX_ATTACHMENT_STORE_BYTES = 50 * 1024 * 1024
COOLDOWN = 300
IMAGE_TYPES = {
    'image/png': {'ext': 'png', 'label': 'PNG'},
    'image/jpeg': {'ext': 'jpg', 'label': 'JPEG'},
    'image/webp': {'ext': 'webp', 'label': 'WebP'},
}
LIMITS = {'error_id': 64, 'fingerprint': 64, 'occurred_at': 64,
          'app_version': 64, 'install_mode': 40, 'os': 160, 'python_version': 40,
          'tools_summary': 500, 'agents_summary': 500, 'source': 80, 'action': 160,
          'user_message': 400, 'error_type': 80, 'detail': 6000, 'user_note': 1500,
          'attachment_count': 16, 'attachments_summary': 1000}


class AttachmentError(ValueError):
    """Validation failure for optional feedback screenshots."""


class AttachmentTooLargeError(AttachmentError):
    """Size limit violation for optional feedback screenshots."""


def format_size(size):
    size = max(0, int(size))
    if size < 1024:
        return f'{size} B'
    if size < 1024 * 1024:
        return f'{size // 1024} KB' if size % 1024 == 0 else f'{size / 1024:.1f} KB'
    return f'{size // (1024 * 1024)} MB' if size % (1024 * 1024) == 0 else f'{size / (1024 * 1024):.1f} MB'


def detect_image(raw):
    if not isinstance(raw, (bytes, bytearray)):
        return None
    if len(raw) >= 24 and raw[:8] == b'\x89PNG\r\n\x1a\n' and raw[12:16] == b'IHDR':
        return 'image/png'
    if len(raw) >= 16 and raw[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if (len(raw) >= 16 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP' and
            raw[12:16] in {b'VP8 ', b'VP8L', b'VP8X'}):
        return 'image/webp'
    return None


def parse_attachments(items):
    if items is None:
        return []
    if not isinstance(items, list):
        raise AttachmentError('截图附件格式不正确，请重新选择图片。')
    if len(items) > MAX_ATTACHMENTS:
        raise AttachmentError(f'最多只能附带 {MAX_ATTACHMENTS} 张截图，请移除多余图片后再试。')
    parsed = []
    total = 0
    max_b64 = ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4
    for item in items:
        if not isinstance(item, dict):
            raise AttachmentError('截图附件格式不正确，请重新选择图片。')
        encoded = item.get('data')
        declared = item.get('content_type', '')
        if not isinstance(encoded, str) or not encoded:
            raise AttachmentError('截图内容缺失，请重新选择图片。')
        if not isinstance(declared, str):
            raise AttachmentError('截图格式信息不正确，请重新选择图片。')
        if len(encoded) > max_b64:
            raise AttachmentTooLargeError(f'单张截图不能超过 {format_size(MAX_ATTACHMENT_BYTES)}，请压缩或裁剪后再试。')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise AttachmentError('截图内容已损坏或格式不正确，请重新选择图片。') from None
        if not raw:
            raise AttachmentError('截图文件为空，请重新选择有效图片。')
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise AttachmentTooLargeError(f'单张截图不能超过 {format_size(MAX_ATTACHMENT_BYTES)}，请压缩或裁剪后再试。')
        detected = detect_image(raw)
        if not detected:
            raise AttachmentError('仅支持 PNG、JPEG、WebP 格式的图片截图。')
        if declared and declared != detected:
            raise AttachmentError('截图格式与文件内容不一致，仅支持 PNG、JPEG、WebP 图片。')
        total += len(raw)
        if total > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise AttachmentTooLargeError(f'截图总大小不能超过 {format_size(MAX_ATTACHMENTS_TOTAL_BYTES)}，请减少截图数量或缩小图片。')
        spec = IMAGE_TYPES[detected]
        parsed.append({'data': raw, 'content_type': detected, 'ext': spec['ext'],
                       'label': spec['label'], 'size': len(raw),
                       'sha256': hashlib.sha256(raw).hexdigest()})
    return parsed


def summarize_attachments(attachments, row_id=''):
    if not attachments:
        return ''
    if row_id:
        lines = [f'已附 {len(attachments)} 张截图（留底编号 {row_id}，位于分享站 feedback-attachments/）：']
        for index, item in enumerate(attachments, 1):
            filename = item.get('file') or f"{item['sha256']}.{item['ext']}"
            lines.append(
                f"{index}. {filename}（{item['label']}，{format_size(item['size'])}，"
                f"接口 /api/feedback/{row_id}/attachments/{item['sha256']}）"
            )
    else:
        lines = [f'已附 {len(attachments)} 张截图（仅上传图片内容，不含本机路径）：']
        for index, item in enumerate(attachments, 1):
            lines.append(
                f"{index}. 截图 {index}（{item['label']}，{format_size(item['size'])}，编号 {item['sha256'][:12]}）"
            )
    return text('\n'.join(lines), LIMITS['attachments_summary'])


def append_attachment_fallback(detail, summary):
    if not summary:
        return detail
    fallback = '[附带截图]\n' + summary
    if not detail:
        return text(fallback, LIMITS['detail'])
    limit = LIMITS['detail']
    if len(detail) + 2 + len(fallback) <= limit:
        return detail + '\n\n' + fallback
    head_limit = max(0, limit - len(fallback) - 2)
    head = detail[:head_limit].rstrip()
    return (head + '\n\n' + fallback) if head else fallback[:limit]


def text(value, limit):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value if c in '\n\t' or
                   not unicodedata.category(c).startswith('C'))[:limit].strip()


def clean(data):
    return {key: text(data.get(key), limit) for key, limit in LIMITS.items()}


def envelope(report, attachments=()):
    env = report['environment']
    row = next(iter(report['errors']), {})
    snapshot = row.get('environment', {})
    runtime = {**env, **snapshot}
    timing = ('系统、Python 和程序版本采集于出错时。' if snapshot else
              '此记录未保存出错时的版本信息，版本取自本次反馈。') if row else '主动反馈，环境取自本次反馈。'
    timing += ' 工具与助手摘要采集于本次反馈：' + report.get('environment_captured_at', '未记录时间')
    return clean({**row, 'occurred_at': row.get('time', ''),
                  **{k: runtime.get(k, '') for k in ('app_version', 'install_mode', 'os', 'python_version')},
                  'detail': timing + '\n\n' + row.get('detail', '') if row else '',
                  'tools_summary': '; '.join(f"{name}: {tool['version'] if tool['available'] else '未安装'}"
                                            for name, tool in env['tools'].items()),
                  'agents_summary': ', '.join(env['agent_types']) + f"；技能数：{env['skill_count']}",
                  'source': row.get('source', 'manual'), 'action': row.get('action', '主动反馈'),
                  'user_note': report['note'],
                  'attachment_count': str(len(attachments)),
                  'attachments_summary': summarize_attachments(attachments)})


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
