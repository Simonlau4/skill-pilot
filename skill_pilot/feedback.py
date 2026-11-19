"""Small feedback envelope and validated screenshot helpers shared by dashboard and team hub."""
import base64
import binascii
import hashlib
import ipaddress
import json
import struct
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.client import HTTPException

MAX_BODY = 64 * 1024
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BODY = 8 * 1024 * 1024
MAX_RECORDS = 200
MAX_BYTES = 1024 * 1024
MAX_ATTACHMENT_STORE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16_000_000
MAX_PNG_DECOMPRESSED_BYTES = 64 * 1024 * 1024
COOLDOWN = 300
IMAGE_TYPES = {
    'image/png': {'ext': 'png', 'label': 'PNG'},
    'image/jpeg': {'ext': 'jpg', 'label': 'JPEG'},
}
PNG_KEEP_CHUNKS = {b'IHDR', b'PLTE', b'tRNS', b'IDAT', b'IEND', b'sRGB'}
PNG_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
JPEG_SOF_MARKERS = {0xc0, 0xc1, 0xc2}
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


def _check_dimensions(width, height):
    if not (1 <= width <= MAX_IMAGE_DIMENSION and 1 <= height <= MAX_IMAGE_DIMENSION and
            width * height <= MAX_IMAGE_PIXELS):
        raise AttachmentError('图片分辨率超出支持范围，请裁剪或缩小后再试。')


def detect_image(raw):
    if not isinstance(raw, (bytes, bytearray)):
        return None
    if len(raw) >= 24 and raw[:8] == b'\x89PNG\r\n\x1a\n' and raw[12:16] == b'IHDR':
        return 'image/png'
    if len(raw) >= 16 and raw[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    return None


def _sanitize_png(raw):
    if len(raw) < 33 or raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
    pos, kept, idat_parts = 8, [], []
    ihdr = None
    plte_entries = 0
    has_trns = False
    has_srgb = False
    seen_idat = False
    ended_idat = False
    has_iend = False
    while pos < len(raw):
        if pos + 12 > len(raw):
            raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        length = struct.unpack('>I', raw[pos:pos + 4])[0]
        kind = raw[pos + 4:pos + 8]
        if not all(65 <= b <= 90 or 97 <= b <= 122 for b in kind):
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        end = pos + 12 + length
        if end > len(raw):
            raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        data = raw[pos + 8:pos + 8 + length]
        crc = struct.unpack('>I', raw[pos + 8 + length:end])[0]
        if (zlib.crc32(kind + data) & 0xffffffff) != crc:
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        if ihdr is None:
            if kind != b'IHDR' or length != 13:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            width, height, depth, color, comp, filt, interlace = struct.unpack('>IIBBBBB', data)
            _check_dimensions(width, height)
            if color not in PNG_DEPTHS or depth not in PNG_DEPTHS[color] or comp != 0 or filt != 0 or interlace not in (0, 1):
                raise AttachmentError('图片格式不受支持或已损坏，请重新截图后再试。')
            ihdr = (width, height, depth, color, interlace)
        elif kind == b'IHDR':
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        if kind != b'IDAT' and seen_idat:
            ended_idat = True
        if kind == b'PLTE':
            if seen_idat or plte_entries or ihdr[3] in {0, 4} or length == 0 or length % 3 != 0 or length > 768:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            plte_entries = length // 3
        elif kind == b'tRNS':
            if seen_idat or has_trns or ihdr[3] in {4, 6}:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            if (ihdr[3] == 0 and length != 2) or (ihdr[3] == 2 and length != 6) or (
                    ihdr[3] == 3 and (not plte_entries or not 1 <= length <= plte_entries)):
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            has_trns = True
        elif kind == b'sRGB':
            if seen_idat or plte_entries or has_srgb or length != 1 or data[0] > 3:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            has_srgb = True
        elif kind == b'IDAT':
            if ended_idat:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            seen_idat = True
            idat_parts.append(data)
        elif kind == b'IEND':
            if length != 0 or end != len(raw):
                raise AttachmentError('图片内容异常，请重新截图或选择有效图片。')
            kept.append(raw[pos:end])
            has_iend = True
            break
        elif kind != b'IHDR' and (kind[0] & 0x20) == 0:
            raise AttachmentError('图片格式不受支持或已损坏，请重新截图后再试。')
        if kind in PNG_KEEP_CHUNKS:
            kept.append(raw[pos:end])
        pos = end
    if ihdr is None or not has_iend or not idat_parts or (ihdr[3] == 3 and not plte_entries):
        raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
    width, height, depth, color, interlace = ihdr
    bpp = PNG_CHANNELS[color] * depth
    passes = []
    if interlace == 0:
        stride = 1 + (width * bpp + 7) // 8
        passes.append((height, stride))
    else:
        for x0, y0, dx, dy in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)):
            pw = (width - x0 + dx - 1) // dx if width > x0 else 0
            ph = (height - y0 + dy - 1) // dy if height > y0 else 0
            if pw and ph:
                passes.append((ph, 1 + (pw * bpp + 7) // 8))
    expected = sum(ph * stride for ph, stride in passes)
    if expected <= 0 or expected > MAX_PNG_DECOMPRESSED_BYTES:
        raise AttachmentError('图片分辨率或内容超出支持范围，请裁剪或缩小后再试。')
    try:
        decompressor = zlib.decompressobj()
        # Strict upper bound: never call unbounded flush() on untrusted IDAT streams.
        unpacked = decompressor.decompress(b''.join(idat_parts), expected + 1)
    except zlib.error:
        raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。') from None
    if len(unpacked) != expected or not decompressor.eof or decompressor.unconsumed_tail or decompressor.unused_data:
        raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
    offset = 0
    for ph, stride in passes:
        if any(b > 4 for b in unpacked[offset:offset + ph * stride:stride]):
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        offset += ph * stride
    return b'\x89PNG\r\n\x1a\n' + b''.join(kept)


def _check_jpeg_dqt(seg_data):
    pos = 0
    table_ids = set()
    if not seg_data:
        raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
    while pos < len(seg_data):
        info = seg_data[pos]
        pq, tq = info >> 4, info & 0x0f
        if pq not in (0, 1) or tq > 3:
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        size = 1 + (64 if pq == 0 else 128)
        if pos + size > len(seg_data):
            raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        table_ids.add(tq)
        pos += size
    return table_ids


def _check_jpeg_dht(seg_data):
    pos = 0
    dc_ids, ac_ids = set(), set()
    if not seg_data:
        raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
    while pos < len(seg_data):
        if pos + 17 > len(seg_data):
            raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        info = seg_data[pos]
        tc, th = info >> 4, info & 0x0f
        if tc not in (0, 1) or th > 3:
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        counts = seg_data[pos + 1:pos + 17]
        count = sum(counts)
        kraft = sum(c * (1 << (16 - i)) for i, c in enumerate(counts, 1))
        if count > 256 or kraft >= (1 << 16) or pos + 17 + count > len(seg_data):
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        (dc_ids if tc == 0 else ac_ids).add(th)
        pos += 17 + count
    return dc_ids, ac_ids


def _sanitize_jpeg(raw):
    if len(raw) < 16 or raw[:2] != b'\xff\xd8':
        raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
    out = [b'\xff\xd8']
    pos = 2
    sof = None
    sof_marker = None
    sof_components = {}
    dqt_ids = set()
    dc_dht_ids = set()
    ac_dht_ids = set()
    has_sos = False
    has_eoi = False
    while pos < len(raw):
        if raw[pos] != 0xff:
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        while pos < len(raw) and raw[pos] == 0xff:
            pos += 1
        if pos >= len(raw):
            raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        marker = raw[pos]
        pos += 1
        if marker == 0x00:
            raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
        if marker == 0xd9:
            if pos != len(raw):
                raise AttachmentError('图片内容异常，请重新截图或选择有效图片。')
            out.append(b'\xff\xd9')
            has_eoi = True
            break
        if marker == 0x01 or 0xd0 <= marker <= 0xd7:
            out.append(bytes([0xff, marker]))
            continue
        if pos + 2 > len(raw):
            raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        seg_len = struct.unpack('>H', raw[pos:pos + 2])[0]
        if seg_len < 2 or pos + seg_len > len(raw):
            raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        seg_data = raw[pos + 2:pos + seg_len]
        segment = bytes([0xff, marker]) + raw[pos:pos + seg_len]
        pos += seg_len
        if marker in JPEG_SOF_MARKERS:
            if sof is not None or len(seg_data) < 6:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            precision, height, width, components = struct.unpack('>BHHB', seg_data[:6])
            _check_dimensions(width, height)
            if precision != 8 or components not in {1, 3, 4} or len(seg_data) != 6 + 3 * components:
                raise AttachmentError('图片格式不受支持或已损坏，请重新截图后再试。')
            for idx in range(components):
                comp_id, sampling, tq = seg_data[6 + 3 * idx:6 + 3 * idx + 3]
                h_samp, v_samp = sampling >> 4, sampling & 0x0f
                if not (1 <= h_samp <= 4 and 1 <= v_samp <= 4) or tq > 3 or comp_id in sof_components:
                    raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
                sof_components[comp_id] = tq
            sof = (width, height, components)
            sof_marker = marker
            out.append(segment)
        elif marker == 0xdb:
            dqt_ids.update(_check_jpeg_dqt(seg_data))
            out.append(segment)
        elif marker == 0xc4:
            dc_ids, ac_ids = _check_jpeg_dht(seg_data)
            dc_dht_ids.update(dc_ids)
            ac_dht_ids.update(ac_ids)
            out.append(segment)
        elif marker == 0xdd:
            if len(seg_data) != 2:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            out.append(segment)
        elif marker == 0xe0:
            # Keep only a clean 14-byte JFIF header without embedded thumbnails.
            if seg_data.startswith(b'JFIF\x00') and len(seg_data) >= 14 and seg_data[12] == 0 and seg_data[13] == 0:
                out.append(b'\xff\xe0' + struct.pack('>H', 16) + seg_data[:14])
        elif marker == 0xee:
            if seg_data.startswith(b'Adobe') and len(seg_data) == 12:
                out.append(segment)
        elif 0xe1 <= marker <= 0xef or marker == 0xfe:
            # Strip EXIF, XMP, IPTC, ICC profiles and comments.
            continue
        elif marker == 0xda:
            if sof is None or not dqt_ids or not (dc_dht_ids or ac_dht_ids) or len(seg_data) < 4:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            ns = seg_data[0]
            if not 1 <= ns <= sof[2] or len(seg_data) != 1 + 2 * ns + 3:
                raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            ss, se, ah_al = seg_data[1 + 2 * ns:1 + 2 * ns + 3]
            ah, al = ah_al >> 4, ah_al & 0x0f
            if sof_marker in {0xc0, 0xc1}:
                if ss != 0 or se != 63 or ah != 0 or al != 0:
                    raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            else:
                if ss > se or se > 63 or ah > 13 or al > 13 or (ss == 0 and se != 0) or (ss > 0 and ns != 1):
                    raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
            seen_cs = set()
            for idx in range(ns):
                cs = seg_data[1 + 2 * idx]
                td_ta = seg_data[2 + 2 * idx]
                td, ta = td_ta >> 4, td_ta & 0x0f
                if cs not in sof_components or cs in seen_cs or sof_components[cs] not in dqt_ids or td > 3 or ta > 3:
                    raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
                if sof_marker in {0xc0, 0xc1}:
                    if td not in dc_dht_ids or ta not in ac_dht_ids:
                        raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
                else:
                    if ss == 0 and ah == 0 and td not in dc_dht_ids:
                        raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
                    if se > 0 and ta not in ac_dht_ids:
                        raise AttachmentError('图片内容已损坏，请重新截图或选择有效图片。')
                seen_cs.add(cs)
            has_sos = True
            out.append(segment)
            scan_start = pos
            found_next = False
            while pos < len(raw):
                ff = raw.find(b'\xff', pos)
                if ff == -1:
                    raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
                next_pos = ff + 1
                while next_pos < len(raw) and raw[next_pos] == 0xff:
                    next_pos += 1
                if next_pos >= len(raw):
                    raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
                nxt = raw[next_pos]
                if nxt == 0x00 or 0xd0 <= nxt <= 0xd7:
                    pos = next_pos + 1
                    continue
                if ff <= scan_start:
                    raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
                out.append(raw[scan_start:ff])
                pos = ff
                found_next = True
                break
            if not found_next:
                raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
        else:
            raise AttachmentError('图片格式不受支持或已损坏，请重新截图或转为 PNG 后再试。')
    if (sof is None or not dqt_ids or not (dc_dht_ids or ac_dht_ids) or not has_sos or not has_eoi or
            any(tq not in dqt_ids for tq in sof_components.values())):
        raise AttachmentError('图片内容不完整，请重新截图或选择有效图片。')
    return b''.join(out)


def sanitize_image(raw, content_type):
    if content_type == 'image/png':
        return _sanitize_png(raw)
    if content_type == 'image/jpeg':
        return _sanitize_jpeg(raw)
    raise AttachmentError('仅支持 PNG、JPEG 格式的截图图片。')


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
        if (len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP') or declared == 'image/webp':
            raise AttachmentError('暂不直接接收未转换的 WebP 图片，请通过看板选择或粘贴自动转换，或先转为 PNG、JPEG 后再试。')
        detected = detect_image(raw)
        if not detected:
            raise AttachmentError('仅支持 PNG、JPEG 格式的截图上传；如需附带 WebP 图片，请通过看板选择或粘贴。')
        if declared and declared != detected:
            raise AttachmentError('截图格式与文件内容不一致，仅支持 PNG、JPEG 图片。')
        clean_raw = sanitize_image(raw, detected)
        if len(clean_raw) > MAX_ATTACHMENT_BYTES:
            raise AttachmentTooLargeError(f'单张截图不能超过 {format_size(MAX_ATTACHMENT_BYTES)}，请压缩或裁剪后再试。')
        total += len(clean_raw)
        if total > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise AttachmentTooLargeError(f'截图总大小不能超过 {format_size(MAX_ATTACHMENTS_TOTAL_BYTES)}，请减少截图数量或缩小图片。')
        spec = IMAGE_TYPES[detected]
        parsed.append({'data': clean_raw, 'content_type': detected, 'ext': spec['ext'],
                       'label': spec['label'], 'size': len(clean_raw),
                       'sha256': hashlib.sha256(clean_raw).hexdigest()})
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
        lines = [f'已附 {len(attachments)} 张截图（已清除隐藏的拍摄和文件信息，不含本机路径）：']
        for index, item in enumerate(attachments, 1):
            filename = item.get('file') or f"{item['sha256']}.{item['ext']}"
            lines.append(
                f"{index}. {filename}（{item['label']}，{format_size(item['size'])}）"
            )
    return text('\n'.join(lines), LIMITS['attachments_summary'])


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
