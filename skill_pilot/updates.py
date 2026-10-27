from .platform_support import rmtree
"""M4 read-only reviews, confirmed batches, and preview-only Codex merging."""
import hashlib
import json
import re
import shutil
import tempfile
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import operations, research, normalization, profiles
from .scan import SKIP_CONTENT, metadata, is_disabled
from .sources import normalize, lark
from .sources.update_github import Reader, files
from .store import git_tree, physical_state
from .sync import reconcile_assignments

SCRIPT_SUFFIXES = {'.sh', '.bash', '.zsh', '.py', '.js', '.ts', '.mjs', '.cjs', '.rb', '.pl', '.ps1', '.fish'}
RISK_WORDS = re.compile(r'\b(curl|wget|fetch|requests|urllib|https?://|rm|rmdir|unlink|rmtree|remove|subprocess|exec|eval|os\.system|spawn|pip|npm|npx|brew|apt|uv\s+(pip|tool))\b', re.I)


def comparison(left, right):
    import difflib
    one, two = files(Path(left)), files(Path(right))
    changed, added, removed, scripts, details, risks = [], 0, 0, [], [], []
    for name in sorted(one.keys() | two.keys()):
        a, am = one.get(name, (b'', 0)); b, bm = two.get(name, (b'', 0))
        if a == b and am == bm:
            continue
        changed.append(name)
        if 'scripts' in Path(name).parts or Path(name).suffix.lower() in SCRIPT_SUFFIXES or am & 0o111 or bm & 0o111 or a.startswith(b'#!') or b.startswith(b'#!'):
            scripts.append(name)
            if bm & 0o111 or b.startswith(b'#!'): risks.append({'file': name, 'line': '新版中新增或改动了可执行文件'})
            risks.extend({'file': name, 'line': line} for line in b.decode(errors='replace').splitlines()
                         if RISK_WORDS.search(line))
        details.append('文件：' + name)
        if am != bm:
            details.append('执行权限或文件类型有变化')
        try:
            al, bl = a.decode().splitlines(), b.decode().splitlines()
            matcher = difflib.SequenceMatcher(None, al, bl)
            for tag, i, j, k, n in matcher.get_opcodes():
                if tag != 'equal':
                    removed += j - i; added += n - k
            details.extend(difflib.unified_diff(al, bl, fromfile='原来', tofile='现在', lineterm=''))
        except UnicodeError:
            details.append('这不是文本文件，内容有变化。')
    return {'file_count': len(changed), 'added_lines': added, 'removed_lines': removed,
            'changed_files': changed, 'script_files': scripts, 'risk_hits': risks, 'comparison': '\n'.join(details)}


REPOSITORY_TIMEOUT = 60


def skill_state(home, name, skill):
    local = home / '.skill-pilot/store' / name
    disabled = profiles.disabled(home)
    state = [skill, git_tree(local), physical_state(home / '.skill-pilot/bases' / name),
             {a: [data['error'], is_disabled({'name': name, 'path': str(local)}, data)] for a, data in disabled.items()},
             {p: physical_state(home / p) for p in skill.get('entries', [])}]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


UNCHECKED_REASONS = {
    'self_made': ('自制技能', '来源已标为“自制”，由你自行维护，不检查外部更新。如有外部原版，需先核对来源。'),
    'local': ('本机维护', '来源是本机源码或本机文档，需从本机原稿维护，不检查外部更新。'),
    'unknown': ('来源未记录', '没有记录可用于检查更新的来源；需先找到原作者的来源并补记。'),
    'unsupported': ('暂不支持检查', '已记录来源，但当前不支持从该来源检查更新；需到原来源核对版本。'),
    'unused': ('未启用', '目前没有任何 Agent 启用，本次跳过。需要使用时，可在详情中启用后重新检查。'),
}


def _unchecked(registry):
    groups = {}
    for name, skill in sorted(registry.items()):
        if not skill['assigned_agents']:
            reason = 'unused'
        else:
            try:
                source = normalize(skill['source'])
            except ValueError:
                continue  # 来源写错的仍要尝试检查，并单独报告失败。
            if source['kind'] != 'skip':
                continue
            reason = source['skip_reason']
        groups.setdefault(reason, []).append(name)
    return [{'code': code, 'reason': reason, 'description': description,
             'count': len(groups[code]), 'names': groups[code]}
            for code, (reason, description) in UNCHECKED_REASONS.items() if code in groups]


def create(home=None):
    home, registry, _ = operations.load(home)
    temporary = tempfile.mkdtemp(prefix='skill-pilot-updates-')
    result = {'id': Path(temporary).name, 'temporary': temporary, 'home': str(home),
              'checked_at': datetime.now(timezone.utc).isoformat(), 'write_allowed': operations.write_allowed(home),
              'skills': [], 'errors': [], 'lark': None, 'running': True, 'done': 0, 'current': [],
              'update_count': 0, 'latest_count': 0, 'api_calls': 0, '_groups': {}}
    # 记录跳过的原因和名单，不扩大检查范围。
    for name, skill in sorted(registry['skills'].items()):
        if not skill['assigned_agents']:
            continue  # 没有 agent 在用的不查
        try:
            source = normalize(skill['source'])
            if source['kind'] == 'skip':
                continue  # 没有可检查来源的只列入跳过清单。
        except ValueError as error:
            source = {'kind': 'skip', 'label': '没查清楚', 'error': str(error)}
        group = source.get('repository', source['kind'])
        result['_groups'].setdefault(group, []).append((name, skill, source))
    result['unchecked'] = _unchecked(registry['skills'])
    result['total'] = sum(map(len, result['_groups'].values()))
    result['remaining'] = result['total']
    result['repositories'] = sum(g not in {'skip', 'lark', 'team'} for g in result['_groups'])
    return result


def check(home=None, review=None, lock=None):
    result = review if review is not None else create(home)
    home, temporary = Path(result['home']), result['temporary']
    lock = lock or threading.Lock()
    def publish(row):
        with lock:
            result['skills'].append(row)
            result['done'] += 1; result['remaining'] -= 1
            result['update_count'] = sum(r['has_update'] for r in result['skills'])
            result['latest_count'] = sum(r['status'] == '已是最新' for r in result['skills'])
            if row.get('error'): result['errors'].append({'name': row['name'], 'error': row['error']})
    def scan_group(group, items):
        with lock: result['current'].append('飞书工具' if group == 'lark' else group)
        reader = Reader(Path(temporary) / hashlib.sha256(group.encode()).hexdigest()[:16], time.monotonic() + REPOSITORY_TIMEOUT)
        packages, failure = {}, None
        if group == 'lark':
            try:
                result['lark'] = {**lark.check(timeout=reader.remaining(), home=home), 'skill_count': len(items)}
                if result['lark']['available']:
                    packages = lark.prepare({n: s for n, _, s in items}, temporary, result['lark']['latest_version'], reader.deadline)
            except (ValueError, OSError, KeyError, TypeError) as error:
                failure = str(error); result['lark'] = {'available': False, 'message': '飞书更新没查清楚', 'error': failure}
        if group == 'team':
            from . import team
            try:
                team_config, _, cards = team._fetch(home)
                listings = {(r['member']['id'], r['name']): r for card in cards for r in card['listings']}
            except (ValueError, OSError) as error:
                failure = str(error)
        for name, skill, source in items:
            row = {'name': name, 'source': source, 'has_update': False, 'local_modified': False, 'status': '已是最新'}
            try:
                if source['kind'] == 'skip':
                    row['status'] = source['label']
                    if source.get('error'): raise ValueError(source['error'])
                else:
                    local, base = home / '.skill-pilot/store' / name, home / '.skill-pilot/bases' / name
                    row['_state'] = skill_state(home, name, skill)
                    row['local_tree_sha'] = git_tree(local)
                    if group == 'lark':
                        if failure: raise ValueError(failure)
                        if name in packages:
                            row.update(_base_path=str(base), _upstream_path=packages[name]['path'], package=packages[name],
                                       tree_sha=git_tree(Path(packages[name]['path'])), has_update=True, status=result['lark']['message'])
                    elif group == 'team':
                        if failure: raise ValueError(failure)
                        listing = listings.get((source['member'], source['name']))
                        if not listing or listing['source']['kind'] != 'files':
                            row['status'] = '作者已下架'
                        else:
                            version = listing['source']['version']
                            row.update(version=version, has_update=version != source['version'],
                                       status='有更新' if version != source['version'] else '已是最新')
                            if row['has_update']:
                                incoming = team.package(team_config, source['member'], source['name'], version,
                                                        Path(temporary) / 'team' / name)
                                processing = normalization.apply(incoming)
                                row.update(_base_path=str(base), _upstream_path=str(incoming),
                                           tree_sha=git_tree(incoming), normalized_tree_sha=git_tree(incoming),
                                           normalization=processing)
                    else:
                        reader.remaining()
                        record = skill['source'].get('record', skill['source'])
                        baseline = skill.get('base_tree_sha') or skill['source'].get('tree_sha') or record.get('skillFolderHash')
                        reliable = bool(baseline and git_tree(base) == skill.get('base_normalized_tree_sha', baseline))
                        commit, tree = reader.latest(source)
                        row.update(commit=commit, tree_sha=tree, base_tree_sha=baseline, baseline_uncertain=not reliable)
                        if not reliable or tree != baseline:
                            row.update(reader.current(source, commit, tree), _base_path=str(base if reliable else local))
                        row['has_update'] = tree != baseline and tree != skill.get('reviewed_tree_sha') if reliable else normalization.tree(local) != row['normalized_tree_sha']
                        row['status'] = '有更新' if row['has_update'] else '这一版已看过' if reliable and tree == skill.get('reviewed_tree_sha') and skill.get('last_update_choice') == 'keep' else '已是最新'
                    row['local_modified'] = None if row.get('baseline_uncertain') else normalization.tree(local) != normalization.tree(base)
                    if row.get('_upstream_path'):
                        row.update(comparison(row['_base_path'], row['_upstream_path']))
                        row['your_changes'] = comparison(row['_base_path'], local)
                        if row['has_update']:
                            from .sources.github import safe_skill
                            try: safe_skill(Path(row['_upstream_path']))
                            except ValueError:
                                row['blocked_reason'] = '新版引用了技能文件夹外的文件，已停止安装；请先核对来源位置。'
                                row['status'] = '有更新，需先核对来源'
                _apply_watch(row, skill, home)
            except (ValueError, OSError, KeyError, TypeError) as error:
                row.update(status='没查清楚', error=str(error), has_update=False)
            publish(row)
        with lock:
            result['api_calls'] += reader.api_calls
            result['current'].remove('飞书工具' if group == 'lark' else group)
    try:
        if 'skip' in result['_groups']:
            scan_group('skip', result['_groups']['skip'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda item: scan_group(*item), ((g, items) for g, items in result['_groups'].items() if g != 'skip')))
    finally:
        with lock: result['running'] = False
    return result


def _skill_bytes(path):
    file = Path(path) / 'SKILL.md'
    return file.read_bytes() if file.is_file() else None


def _apply_watch(row, skill, home):
    mode = skill.get('update_watch') or 'all'
    if mode == 'all' or row.get('error'):
        return
    if mode == 'off':
        row['has_update'] = False
        row['status'] = '不提醒'
        return
    if mode != 'description' or not row.get('has_update') or not row.get('_upstream_path'):
        return
    local = home / '.skill-pilot/store' / row['name']
    if _skill_bytes(local) == _skill_bytes(row['_upstream_path']):
        row['has_update'] = False
        row['status'] = '说明没变'


WATCH_MODES = {'all': '照常提醒', 'description': '只看说明有没有变', 'off': '先不提醒'}


def set_watch(name, mode, expected=None, home=None, confirmed=False):
    if not confirmed:
        raise ValueError('请先确认这个技能以后怎么提醒更新。')
    if mode not in WATCH_MODES:
        raise ValueError('请选择照常提醒、只看说明，或先不提醒。')
    home, registry, agents = operations.load(home)
    if not isinstance(name, str) or name not in registry['skills']:
        raise ValueError('这个技能不在登记表里，请刷新后再选。')
    def mutate(current, checked):
        skill = current['skills'][name]
        if mode == 'all':
            skill.pop('update_watch', None)
        else:
            skill['update_watch'] = mode
        return [name], [], {'update_watch': mode}
    return operations._run(home, registry, agents, expected or operations.revision(home, agents),
                           f'「{name}」的更新提醒改为{WATCH_MODES[mode]}', mutate)


def public(review):
    result = {k: ([{key: value for key, value in row.copy().items() if not key.startswith('_') and key != 'package'}
                 for row in list(v)] if k == 'skills' else list(v) if isinstance(v, list) else v)
            for k, v in review.copy().items() if not k.startswith('_') and k not in {'temporary', 'home'}}
    if 'home' not in review:
        return result
    registry = operations.load(review['home'])[1]['skills']
    result['skills'] = [row for row in result['skills'] if row['name'] in registry]
    result['errors'] = [row for row in result['errors'] if row['name'] in registry]
    groups = {key: [n for n, _, _ in items if n in registry] for key, items in review['_groups'].items()}
    result.update(total=sum(map(len, groups.values())), done=len(result['skills']),
                  update_count=sum(bool(row['has_update']) for row in result['skills']),
                  latest_count=sum(row['status'] == '已是最新' for row in result['skills']),
                  repositories=sum(bool(names) and key not in {'skip', 'lark', 'team'} for key, names in groups.items()))
    result['remaining'] = result['total'] - result['done']
    result['unchecked'] = _unchecked(registry)
    return result


def selected_row(review, name):
    if name not in operations.load(review['home'])[1]['skills']:
        raise ValueError('这个技能已经删除，请刷新后重新检查。')
    row = next((r for r in review['skills'] if r['name'] == name and r['has_update']), None)
    if row is None:
        raise ValueError('这个技能没有可更新的已核对版本，请重新检查。')
    return row


def explain(review, name):
    row = selected_row(review, name)
    return {'explanation': research.explain({'upstream': row['comparison'],
                                            'your_changes': row['your_changes']['comparison'],
                                            'scripts': row['script_files']})}


def calibrate(review, name):
    row, home = selected_row(review, name), Path(review['home'])
    if skill_state(home, name, operations.load(home)[1]['skills'][name]) != row['_state']:
        raise ValueError('这个技能的内容或分配已经变了，请重新检查更新。')
    if row.get('baseline_uncertain'):
        found = Reader(tempfile.mkdtemp(prefix='calibration-', dir=review['temporary']), time.monotonic() + 300).inspect(row['source'], home / '.skill-pilot/store' / name)
        if found['tree_sha'] != row['tree_sha']:
            raise ValueError('上游又有新变化，请重新检查后再选择。')
        row.update(found, baseline_uncertain=False, **comparison(found['_base_path'], found['_upstream_path']))
        row['your_changes'] = comparison(row['_base_path'], home / '.skill-pilot/store' / name)
    return row['your_changes']


def prepare_merge(review, name):
    calibrate(review, name)
    row, home = selected_row(review, name), Path(review['home'])
    material, preserved = {}, {}
    for label, path in [('base', row['_base_path']), ('local', home / '.skill-pilot/store' / name),
                        ('upstream', row['_upstream_path'])]:
        material[label] = []
        for filename, (data, mode) in files(Path(path)).items():
            try:
                if mode == 0o120000:
                    raise UnicodeError()
                content = data.decode()
                material[label].append({'path': filename, 'content': content, 'executable': bool(mode & 0o111)})
            except UnicodeError:
                versions = [files(Path(p)).get(filename) for p in (row['_base_path'], home / '.skill-pilot/store' / name, row['_upstream_path'])]
                if not versions[0] == versions[1] == versions[2]:
                    raise ValueError('这次包含非文本文件或特殊入口的变化，请选择保留我的或用上游的。')
                preserved[filename] = (data, mode)
    merged = research.merge(material)
    root = Path(review['temporary']) / ('merged-' + name)
    if root.exists():
        rmtree(root)
    root.mkdir()
    seen = set()
    for item in merged['files']:
        path = Path(item['path'])
        if path.is_absolute() or '..' in path.parts or not path.parts or any(p in SKIP_CONTENT for p in path.parts) or str(path) in seen:
            raise ValueError('合并结果包含不安全或重复的位置，文件没有改动。')
        seen.add(str(path)); destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item['content']); destination.chmod(0o755 if item['executable'] else 0o644)
    for filename, (data, mode) in preserved.items():
        path = root / filename
        if filename in seen:
            raise ValueError('合并结果改变了非文本文件，文件没有改动。')
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == 0o120000:
            path.symlink_to(data.decode())
        else:
            path.write_bytes(data); path.chmod(0o755 if mode & 0o111 else 0o644)
    normalization.apply(root)
    _validate_name(root, home / '.skill-pilot/store' / name)
    row['_merge'] = {'path': str(root), 'tree_sha': git_tree(root), 'explanation': merged['explanation']}
    return {'name': name, 'explanation': merged['explanation'], **comparison(home / '.skill-pilot/store' / name, root),
            'merge_id': hashlib.sha256((review['id'] + name + row['_merge']['tree_sha']).encode()).hexdigest()}


def _validate_name(incoming, local):
    from .sources.github import safe_skill
    safe_skill(incoming)
    if not (incoming / 'SKILL.md').is_file() or metadata((incoming / 'SKILL.md').read_text()).get('name') != metadata((local / 'SKILL.md').read_text()).get('name'):
        raise ValueError('新版技能的名称与本机不同，请先核对。')


def apply(review, names, choices=None, confirmed=False, merge_ids=None, confirm_lark=False):
    if not confirmed:
        raise ValueError('请先核对选中的更新，再确认执行。')
    home, registry, _ = operations.load(review['home'])
    # Check before any preparation or CLI subprocess can write.
    from .store import guard
    guard(home / '.skill-pilot', home)
    names = sorted(set(names) & registry['skills'].keys()); choices, merge_ids = choices or {}, merge_ids or {}
    if not names:
        raise ValueError('请先勾选要更新的技能。')
    by_name = {row['name']: row for row in review['skills']}
    lark_names = {name for name in names if by_name.get(name, {}).get('source', {}).get('kind') == 'lark'}
    if lark_names:
        all_lark = {n for n, _, _ in review['_groups']['lark'] if n in registry['skills']}
        if lark_names != all_lark or not confirm_lark:
            raise ValueError('飞书技能需要一起更新，并单独确认同时升级 lark-cli。')
    # Each independent update keeps its own backup and undo record. A failed
    # group is restored by _run before the next group starts.
    groups = [[name] for name in names if name not in lark_names]
    if lark_names:
        groups.append(sorted(lark_names))
    completed, failed, updated, normalization_results = [], [], [], {}
    batch = uuid.uuid4().hex
    for group in groups:
        try:
            result = _apply_group(review, group, choices, merge_ids, batch)
        except operations.RollbackError as error:
            error.operations = completed
            error.updated = updated
            raise
        except (ValueError, OSError) as error:
            if len(names) == 1:
                raise
            reason = str(error) if isinstance(error, ValueError) else '这项更新的文件没能读写，请重新检查更新后再试。'
            for name in group:
                failed.append({'name': name, 'error': reason})
                if name in by_name:
                    by_name[name].update(update_error=reason, status='没更新')
        else:
            completed.append({**result, 'skills': group})
            updated.extend(group)
            normalization_results.update(result['normalization'])
    message = f'已更新 {len(updated)} 个技能' + (f'，{len(failed)} 个没更新，请查看原因。' if failed else '。')
    return {**(completed[0] if len(completed) == 1 else {}),
            'updated': updated, 'failed': failed, 'operations': completed, 'complete': not failed,
            'normalization': normalization_results, 'message': message}


def _apply_group(review, names, choices, merge_ids, batch):
    home, registry, agents = operations.load(review['home'])
    rows = [selected_row(review, name) for name in names]
    lark_rows = [r for r in rows if r['source']['kind'] == 'lark']
    content = []
    for row in rows:
        if row.get('blocked_reason'):
            raise ValueError(row['name'] + '：' + row['blocked_reason'])
        name = row['name']; choice = choices.get(name, 'upstream')
        if choice not in {'keep', 'upstream', 'merge'}:
            raise ValueError(name + '：你改过，请选择保留我的、用上游的或交给 Codex 合并。')
        row['_choice'] = choice
        if choice == 'keep': calibrate(review, name)
        if git_tree(Path(row['_upstream_path'])) != row.get('normalized_tree_sha', row['tree_sha']):
            raise ValueError('下载内容已经变了，请重新检查。')
        incoming = Path(row['_upstream_path'])
        if choice == 'merge':
            merge = row.get('_merge')
            if not merge:
                raise ValueError('请先生成合并结果，核对后再确认更新。')
            actual_id = hashlib.sha256((review['id'] + name + merge['tree_sha']).encode()).hexdigest()
            if merge_ids.get(name) != actual_id or git_tree(Path(merge['path'])) != merge['tree_sha']:
                raise ValueError('合并结果还未确认或已经变了，请重新核对。')
            incoming = Path(merge['path'])
        if choice != 'keep':
            _validate_name(incoming, home / '.skill-pilot/store' / name)
            content.append(name)
    def mutate(current, checked):
        if lark_rows:
            lark.install_cli(home, review['lark']['latest_version'], checked)
        for row in rows:
            name = row['name']; skill = current['skills'][name]
            incoming = Path(row['_merge']['path']) if row['_choice'] == 'merge' else Path(row['_upstream_path'])
            if row['_choice'] != 'keep':
                destination = checked(home / '.skill-pilot/store' / name)
                rmtree(destination); shutil.copytree(incoming, destination, symlinks=True)
                skill['normalization'] = row.get('normalization', normalization.apply(destination))
            base = checked(home / '.skill-pilot/bases' / name)
            if base.exists():
                rmtree(base)
            base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(row['_upstream_path'], base, symlinks=True)
            normalization.apply(base)
            skill['base_digest'] = operations.content_digest(base)
            skill['base_tree_sha'] = row['tree_sha']
            skill['base_normalized_tree_sha'] = git_tree(base)
            skill['reviewed_tree_sha'] = row['tree_sha']
            skill['source'].update(tree_sha=row['tree_sha'])
            if row['source']['kind'] == 'github':
                skill['source'].update(repository=row['source']['repository'], commit=row['commit'], source_path=row['source']['path'])
            elif row['source']['kind'] == 'team':
                skill['source']['team']['version'] = row['version']
            else:
                skill['source'].update(cli_version=review['lark']['latest_version'], digest=row['package']['digest'])
            skill['last_update_choice'] = row['_choice']
        reconcile_assignments(current, home, checked, names,
                              {name: list(current['skills'][name]['entries']) for name in names})
        return names, content, {'normalization': {n: current['skills'][n].get('normalization', {}) for n in names}}
    def current_registry():
        current = operations.load(home)[1]
        if any(row['name'] not in current['skills'] or
               skill_state(home, row['name'], current['skills'][row['name']]) != row['_state'] for row in rows):
            raise ValueError('这个技能的内容或分配已经变了，请刷新后重新检查。')
        return current
    result = operations._run(home, registry, agents, current_registry, f'核对并更新了 {len(names)} 个技能', mutate,
                             extra_paths=lark.CLI_PATHS if lark_rows else None, batch=batch)
    for row in rows:
        row.pop('update_error', None)
        row['_arrow'] = {'has_update': row.get('has_update'), 'status': row.get('status'),
                         'operation_id': result['id']}
        row.update(has_update=False, status='已保留我的' if row['_choice'] == 'keep' else '已更新')
    review['update_count'] = sum(r['has_update'] for r in review['skills'])
    return result


def restore_arrows(review, skills, operation_id):
    """只有撤销记下标记的那次更新时，才把「有新版」放回同一次检查结果。"""
    if not review:
        return
    wanted = set(skills or [])
    for row in review.get('skills', []):
        saved = row.get('_arrow')
        if row.get('name') in wanted and saved and saved.get('operation_id') == operation_id:
            row['has_update'] = saved['has_update']
            row['status'] = saved['status']
            row.pop('_arrow', None)
    review['update_count'] = sum(bool(row.get('has_update')) for row in review.get('skills', []))
