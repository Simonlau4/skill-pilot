"""Read-only source discovery. Never import code from downloaded skills."""
"""Normalize inherited installation records without changing the registry."""
import re
from pathlib import PurePosixPath


def normalize(source):
    source = source if isinstance(source, dict) else {'source': source}
    record = source.get('record', source)
    text = str(source.get('source', ''))
    if text == '团队商店':
        team = source.get('team', {})
        if (not isinstance(team, dict) or
                not re.fullmatch(r'[0-9a-f]{32}', str(team.get('member', ''))) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(team.get('name', ''))) or
                not re.fullmatch(r'[0-9a-f]{64}', str(team.get('version', ''))) or
                not isinstance(team.get('member_name'), str) or
                team.get('key') != 'team:' + team['member'] + ':' + team['name']):
            raise ValueError('团队商店的来源记录不完整，请重新装上这个技能。')
        return {'kind': 'team', 'label': '团队商店 · ' + team['member_name'],
                **{k: team[k] for k in ('member', 'member_name', 'name', 'version', 'key')}}
    if text == '自制':
        return {'kind': 'skip', 'label': '自制，不检查更新', 'skip_reason': 'self_made'}
    if text in {'本机源码', '本机文档生成'}:
        return {'kind': 'skip', 'label': '本机维护，不检查外部更新', 'skip_reason': 'local'}
    if text == '来源不明':
        return {'kind': 'skip', 'label': '来源未记录', 'skip_reason': 'unknown'}
    if record.get('sourceType') == 'well-known':
        url = record.get('sourceUrl', '')
        if url.startswith('https://open.feishu.cn/lark-cli/skills/'):
            return {'kind': 'lark', 'label': '飞书官方', 'url': url,
                    'base_url': record.get('sourceBaseUrl', url.split('/.well-known/')[0])}
        return {'kind': 'skip', 'label': '暂不支持检查此来源', 'skip_reason': 'unsupported'}
    repository = source.get('repository') or record.get('source', '')
    match = re.search(r'https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)',
                      text + ' ' + str(record.get('sourceUrl', '')))
    if match:
        repository = match[1].removesuffix('.git')
    local = str(repository).startswith('file:///')
    if not local and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(repository)):
        if not (text or repository or record.get('sourceUrl')):
            return {'kind': 'skip', 'label': '来源未记录', 'skip_reason': 'unknown'}
        return {'kind': 'skip', 'label': '暂不支持检查此来源', 'skip_reason': 'unsupported'}
    folder = str(source.get('source_path', record.get('skillPath', '.')) or '.')
    path = PurePosixPath(folder)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('来源记录中的技能位置不正确。')
    if path.name == 'SKILL.md':
        path = path.parent
    return {'kind': 'github', 'repository': repository, 'path': str(path),
            'label': str(repository), 'url': repository if local else 'https://github.com/' + repository}
