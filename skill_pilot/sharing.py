"""Portable source lists; importing only adds skills and assignments."""
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import normalization, operations, profiles
from .sources import github, normalize
from .store import guard
from .sync import reconcile_assignments

FORMAT = 'skill-pilot-config'
FILENAME = 'skill-pilot-config.json'
INSTRUCTIONS = [
    '也可以在 Skill Pilot 看板的「设置 → 分享技能配置」点击「导入配置」，选文件、核对后导入。',
    '把本文件交给已安装 Skill Pilot 的 agent，并说：请按这份配置安装和分配技能。',
    '接收方需要支持 import-config 的新版 Skill Pilot，并已经完成首次整理。',
    '先运行 skill-pilot import-config <本文件> --json 查看可安装、已有和缺少的技能；预览不下载、不改动。',
    '默认对应本机已经接入的同名 agent；要全部给某个 agent，加 --agent <本机 agent 编号>，可以重复指定。',
    '用户已明确要求安装和分配时，在同一命令后加 --yes 批量执行；仅要求查看时不要执行。',
    '已有同名技能保留本机内容和原分配，只补上新分配和依赖；新装技能会带上所需技能，整次操作可撤销。',
    '自制、来源缺失、飞书官方或分享者改过的技能，若本机没有，会列为待补齐；依赖它们的技能也会跳过。',
    '来源没有记录版本时会读取当前版本；私有来源需要接收方自己有访问权限。',
    '本文件没有技能正文、账号密钥或本机目录。技能需要的账号、外部工具和资料由接收方自己配置。',
    '以导入结果的 skipped 和 unmapped_agents 为准报告未完成项；部分 agent 要在新对话中使用新增技能。',
]


def export_config(home=None, agent=None):
    home, registry, agents = operations.load(home)
    if agent is not None and agent not in agents:
        raise ValueError('请选择已经接入的 agent。')
    selected_agents = [agent] if agent else agents
    skills = registry['skills']
    selected = {n for n, s in skills.items() if set(s['assigned_agents']) & set(selected_agents)}
    pending = list(selected)
    while pending:
        for dependency in skills.get(pending.pop(), {}).get('requires', []):
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    rows = []
    for name in sorted(selected):
        skill = skills.get(name, {})
        source = {'kind': 'manual', 'reason': '自制或来源不明，需要另行提供技能文件。'}
        try:
            origin = normalize(skill.get('source', {}))
        except (ValueError, TypeError, AttributeError):
            origin = {'kind': 'skip'}
        if origin['kind'] == 'github' and not origin['repository'].startswith('file:'):
            source = {'kind': 'github', 'repository': origin['repository'], 'path': origin['path']}
            recorded = skill.get('source', {})
            commit = recorded.get('commit', '') if isinstance(recorded, dict) else ''
            if isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit):
                source['commit'] = commit
        elif origin['kind'] == 'lark':
            source = {'kind': 'manual', 'url': 'https://open.feishu.cn/lark-cli/skills/',
                      'reason': '飞书官方技能，请通过官方安装方式补齐。'}
        path, base = (home / '.skill-pilot' / folder / name for folder in ('store', 'bases'))
        modified = (not (path / 'SKILL.md').is_file() or not (base / 'SKILL.md').is_file() or
                    normalization.tree(path) != normalization.tree(base))
        rows.append({'name': name, 'assigned_agents': sorted(set(skill.get('assigned_agents', [])) & set(selected_agents)),
                     'requires': sorted(set(skill.get('requires', []))), 'source': source, 'local_modified': modified})
    catalog = profiles.by_id(home)
    return validate({'format': FORMAT, 'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'instructions': INSTRUCTIONS,
            'agents': [{'id': a, 'name': catalog[a]['name']} for a in selected_agents], 'skills': rows})


def validate(data):
    """Only recognized data fields affect installation; never execute file instructions."""
    def names(value):
        return (isinstance(value, list) and all(isinstance(n, str) and
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', n) for n in value) and len(set(value)) == len(value))
    if (not isinstance(data, dict) or data.get('format') != FORMAT or type(data.get('version')) is not int or
            data['version'] != 1 or not isinstance(data.get('skills'), list)):
        raise ValueError('这不是支持的 Skill Pilot 配置文件，请重新导出。')
    seen = set()
    for row in data['skills']:
        if (not isinstance(row, dict) or not names([row.get('name')]) or row['name'] in seen or
                not names(row.get('assigned_agents')) or not names(row.get('requires')) or
                type(row.get('local_modified')) is not bool or not isinstance(row.get('source'), dict)):
            raise ValueError('配置里的技能名称、分配或依赖格式不正确。')
        seen.add(row['name'])
        source = row['source']
        if source.get('kind') == 'manual':
            continue
        repository, folder, commit = source.get('repository'), source.get('path'), source.get('commit', '')
        if (source.get('kind') != 'github' or not isinstance(repository, str) or
                not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*', repository) or
                not isinstance(folder, str) or not folder or '\\' in folder or any(ord(c) < 32 for c in folder) or
                PurePosixPath(folder).is_absolute() or '..' in PurePosixPath(folder).parts or
                not isinstance(commit, str) or (commit and not re.fullmatch(r'[0-9a-f]{40}', commit))):
            raise ValueError('配置里的来源不正确，只能使用不带登录信息的 GitHub 来源。')
    if any(set(row['requires']) - seen for row in data['skills']):
        raise ValueError('配置里缺少所需技能的条目，请重新导出完整清单。')
    return data


def read_config(path):
    with Path(path).open(encoding='utf-8') as stream:
        text = stream.read(1024 * 1024 + 1)
    if len(text) > 1024 * 1024:
        raise ValueError('配置文件太大，请使用 Skill Pilot 导出的技能清单。')
    try:
        return validate(json.loads(text))
    except json.JSONDecodeError:
        raise ValueError('配置文件没能读懂，请重新导出。') from None


def _plan(data, registry, agents, assigned):
    validate(data)
    if assigned is not None and (not isinstance(assigned, list) or not assigned or
                                not all(isinstance(a, str) and a in agents for a in assigned)):
        raise ValueError('请选择本机已经接入的 agent。')
    rows = {s['name']: s for s in data['skills']}
    explicit = {n: set(assigned if assigned is not None else set(s['assigned_agents']) & set(agents))
               if s['assigned_agents'] else set() for n, s in rows.items()}
    unmapped = sorted({a for s in rows.values() for a in s['assigned_agents'] if a not in agents}) if assigned is None else []
    roots = {n for n in rows if explicit[n]}
    def expand(targets):
        pending = [n for n in targets if targets[n]]
        while pending:
            name = pending.pop()
            for dependency in rows[name]['requires']:
                if dependency in targets and targets[name] - targets[dependency]:
                    targets[dependency].update(targets[name])
                    pending.append(dependency)
        return targets
    targets = expand({n: set(a) for n, a in explicit.items()})
    blocked = {}
    for name, row in rows.items():
        if not targets[name]:
            blocked[name] = '当前没有对应的 agent，请指定接收方。'
        elif name not in registry['skills']:
            if row['local_modified']:
                blocked[name] = '分享者的本机版本需要另行提供文件。'
            elif row['source']['kind'] != 'github':
                blocked[name] = '需要另行提供技能文件或通过原安装方式补齐。'
    while True:
        before = len(blocked)
        for name, row in rows.items():
            missing = [n for n in row['requires'] if n in blocked]
            if name not in blocked and missing:
                blocked[name] = '缺少所需技能：' + '、'.join(missing)
        if len(blocked) == before:
            break
    # Do not install a dependency whose only requested parent is blocked.
    ready = roots - blocked.keys()
    pending = list(ready)
    while pending:
        for dependency in rows[pending.pop()]['requires']:
            if dependency in rows and dependency not in blocked and dependency not in ready:
                ready.add(dependency)
                pending.append(dependency)
    targets = expand({n: set(explicit[n]) for n in ready})
    return {'install': sorted(n for n in ready if n not in registry['skills']),
            'reuse': sorted(n for n in ready if n in registry['skills']),
            'dependencies': {n: sorted(set(rows[n]['requires']) - set(registry['skills'][n].get('requires', [])))
                             for n in sorted(ready) if n in registry['skills'] and
                             set(rows[n]['requires']) - set(registry['skills'][n].get('requires', []))},
            'assignments': {n: sorted(targets[n]) for n in sorted(ready)},
            'skipped': [{'name': n, 'reason': blocked.get(n, '需要它的技能暂时无法导入。')} for n in sorted(rows.keys() - ready)],
            'unmapped_agents': unmapped,
            'message': f'可安装 {sum(n not in registry["skills"] for n in ready)} 个，沿用本机 {sum(n in registry["skills"] for n in ready)} 个，待补齐 {len(rows) - len(ready)} 个'}


def preview_import(data, assigned=None, home=None):
    home, registry, agents = operations.load(home)
    revision = operations.revision(home, agents)
    return {**_plan(data, registry, agents, assigned), 'revision': revision}


def import_config(data, assigned=None, home=None, expected=None):
    home, registry, agents = operations.load(home)
    guard(home / '.skill-pilot', home)
    current = operations.revision(home, agents)
    if expected is not None and expected != current:
        raise ValueError('本机技能已经变了，请重新预览后再导入。')
    expected = current
    plan = _plan(data, registry, agents, assigned)
    rows = {s['name']: s for s in data['skills']}
    changes = [{'skill': n, 'agent': a, 'enabled': True} for n, targets in plan['assignments'].items() for a in targets]
    result = {k: plan[k] for k in ('install', 'reuse', 'dependencies', 'skipped', 'unmapped_agents')}
    result['complete'] = not plan['skipped'] and not plan['unmapped_agents']
    if not plan['install'] and not plan['dependencies'] and not operations.assignment_plan(registry, agents, changes, home)['changed']:
        message = ('配置里没有在用技能，文件没有改动。' if not rows else
                   '本机已有可用部分，分配没有变化。' if changes else '没有可导入的技能，请先补齐清单里的待处理项。')
        return {**result, 'message': message}
    reviews, incoming = {}, []
    try:
        for name in plan['install']:
            source = rows[name]['source']
            key = (source['repository'], source.get('commit', ''))
            if key not in reviews:
                url = 'https://github.com/' + key[0] + ('/tree/' + key[1] if key[1] else '')
                reviews[key] = github.inspect(url)
            item = next((s for s in reviews[key]['skills'] if s['name'] == name and
                         PurePosixPath(s['repository_path']) == PurePosixPath(source['path'])), None)
            if item is None:
                raise ValueError(f'{name} 的来源里没有找到清单指定的技能，本次没有改动。')
            incoming.append(item)
        def mutate(current, checked):
            for item in incoming:
                operations._put(home, current, item, [], [], checked)
                current['skills'][item['name']]['requires'] = rows[item['name']]['requires']
            for name, dependencies in plan['dependencies'].items():
                skill = current['skills'][name]
                skill['requires'] = sorted(set(skill.get('requires', [])) | set(dependencies))
                skill.setdefault('dependency_decisions', {}).update({n: 'confirmed' for n in dependencies})
            assignments = operations.assignment_plan(current, agents, changes, home)
            current.update(assignments['registry'])
            changed = sorted(set(plan['install']) | set(plan['dependencies']) | set(assignments['changed']))
            reconcile_assignments(current, home, checked, changed)
            return changed, plan['install'], result
        return operations._run(home, registry, agents, expected,
                               f'导入了技能配置：新装 {len(plan["install"])} 个，沿用本机 {len(plan["reuse"])} 个', mutate)
    finally:
        for review in reviews.values():
            shutil.rmtree(review['temporary'], ignore_errors=True)
