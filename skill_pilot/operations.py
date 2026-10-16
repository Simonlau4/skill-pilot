"""M3 assignment, installation, absorption and reversible local operations."""
import copy
import fcntl
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import profiles
from .scan import SKIP_CONTENT, content_digest, metadata, scan_root, tokens
from .store import (PROJECT, _restore, backup, check_links, git_tree, guard, physical_state,
                    target_home, transaction, write_allowed, write_json)
from .sync import assignment_entries, reconcile_assignments, visibility, cell_notes, notes
from .sources import github, normalize
from . import labels, normalization

DEPENDENCY_DECISIONS = PROJECT / 'docs/dependency-decisions.json'


class RollbackError(ValueError):
    """A failed operation could not be restored; later writes must stop."""


def curated_dependencies():
    """agent 已判定的依赖（用户不逐条确认）：只读取已确认依赖；正文猜测和否定建议不再参与。"""
    if not DEPENDENCY_DECISIONS.is_file():
        return {}, {}
    data = json.loads(DEPENDENCY_DECISIONS.read_text())
    confirmed = {}
    for name, dependency in data.get('confirmed', []):
        confirmed.setdefault(name, set()).add(dependency)
    return confirmed, {}


def load(home=None):
    home = target_home() if home is None else Path(home).resolve()
    path = home / '.skill-pilot/registry.json'
    if not path.is_file():
        raise ValueError('请先完成首次整理，再管理技能分配。')
    registry = json.loads(path.read_text())
    catalog = profiles.by_id(home)
    agents = [p['id'] for p in profiles.managed(home)]
    for name, skill in registry['skills'].items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('登记表中的技能名称不正确。')
        for entry in skill.get('entries', []):
            if not any(profiles.reads(catalog[agent], entry) for agent in agents):
                raise ValueError('登记表中有不属于已接入 agent 的技能入口。')
            if '..' in Path(entry).parts or any(Path(entry).is_relative_to(r) for r in profiles.readonly_roots(home)):
                raise ValueError('登记表涉及只读目录，已停止操作。')
    confirmed, _ = curated_dependencies()
    for name, skill in registry['skills'].items():
        rejected = {n for n, decision in skill.get('dependency_decisions', {}).items() if decision == 'rejected'}
        extra = confirmed.get(name, set()) & set(registry['skills']) - rejected
        if extra - set(skill.get('requires', [])):
            skill['requires'] = sorted(set(skill.get('requires', [])) | extra)
    return home, registry, agents


def revision(home, agents):
    catalog = profiles.managed(home, agents)
    roots = sorted({r for p in catalog for r in p['skill_roots']})
    paths = roots + profiles.config_paths(home, agents) + ['.agents/.skill-lock.json', '.skill-pilot/registry.json',
                     '.skill-pilot/agents.json', '.skill-pilot/bases']
    state = {p: physical_state(home / p) for p in paths}
    state['store'] = content_digest(home / '.skill-pilot/store')
    # Executable-only changes must invalidate an update or merge review too.
    state['skill_trees'] = {p.name: git_tree(p)
                            for p in (home / '.skill-pilot/store').iterdir()
                            if p.is_dir() and not p.name.startswith('.')}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def new_skills(home, agents):
    result = []
    catalog = profiles.managed(home, agents)
    roots = sorted({r for p in catalog for r in p['skill_roots']})
    visible = visibility(home)
    for root in roots:
        inventory = scan_root(home / root, profiles.root_nested(root, catalog), excluded=profiles.exclusions(home / root, home))
        if inventory['errors']:
            raise ValueError('有技能目录没有完整读出，请刷新后再收编。')
        for skill in inventory['skills']:
            path = Path(skill['path'])
            if not skill['valid'] or path.is_symlink() or not path.is_dir():
                continue
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', skill['name']):
                raise ValueError('新装技能的名称不能安全用作文件夹名称，请先核对。')
            if path.resolve().is_relative_to(home / '.skill-pilot/store'):
                continue
            check_links(path, home)
            relative = str(path.relative_to(home))
            readers = [p['id'] for p in catalog if profiles.reads(p, relative) and skill['name'] in visible[p['id']]['available']]
            result.append({**skill, 'relative': relative, 'assigned_agents': readers,
                           'manual_only_for': [p['id'] for p in catalog if profiles.reads(p, relative) and profiles.manual_only(skill, p)]})
    return result


def listing_tokens(name, info):
    """agent 每次新对话会读到的内容：技能名加说明。"""
    return tokens(info.get('name', name) + ' ' + info.get('description', ''))


def table(home=None):
    home, registry, agents = load(home)
    catalog = profiles.by_id(home)
    cells = cell_notes(registry, home)
    # 看板上的中文名和分组只用于显示；没写的技能显示原名。
    groups = labels.read(home)
    display = {name: label for group in groups for name, label in group['skills'].items()}
    blank = {'title': '', 'summary': '', 'description': '', 'tags': []}
    rows = []
    for name, skill in sorted(registry['skills'].items()):
        path = home / '.skill-pilot/store' / name
        info = metadata((path / 'SKILL.md').read_text())
        label = display.get(name, blank)
        rows.append({'name': name, 'title': label['title'], 'summary': label['summary'], 'description': info.get('description', ''),
                     'description_zh': label['description'], 'tags': label['tags'],
                     'description_tokens': listing_tokens(name, info), 'source': skill['source'],
                     'source_label': normalize(skill['source'])['label'],
                     'assigned_agents': skill['assigned_agents'], 'requires': skill.get('requires', []),
                     'update_watch': skill.get('update_watch') or 'all',
                     'local_modified': normalization.tree(path) != normalization.tree(home / '.skill-pilot/bases' / name),
                     'cell_notes': cells[name]})
    return {'home': str(home), 'revision': revision(home, agents), 'write_allowed': write_allowed(home), 'skills': rows,
            'agents': [{'id': a, 'name': catalog[a]['name'],
                        'activation': '目录改变后会动态刷新' if catalog[a]['activation'] == 'immediate' else '新会话生效',
                        'activation_text': profiles.activation_text(catalog[a]),
                        'count': sum(a in s['assigned_agents'] for s in rows),
                        'description_tokens': sum(s['description_tokens'] for s in rows if a in s['assigned_agents'])}
                       for a in agents], 'notes': notes(home, agents), 'new_skills': new_skills(home, agents),
            'groups': [{'name': group['name'], 'skills': list(group['skills'])} for group in groups],
            'labels_pending': sum(labels.needs_work(display.get(row['name'])) for row in rows),
            'llm_configured': bool(labels.settings(home).get('api_key'))}


def history(home=None):
    home = target_home() if home is None else Path(home).resolve()
    directory = home / '.skill-pilot/logs'
    records = [json.loads(p.read_text()) for p in directory.glob('*.json')]
    undone = {r['undo_of'] for r in records if r.get('undo_of')}
    return [{**r, 'undone': r['id'] in undone} for r in sorted(records, key=lambda r: r['time'], reverse=True)]


def _user_path(raw, home):
    text = str(raw).strip()
    if text.startswith('~/'):
        return (home / text[2:]).resolve()
    path = Path(text)
    if not path.is_absolute():
        path = home / path
    return path.resolve()


def _entry_key(raw, home):
    text = str(raw).strip()
    if text.startswith('~/'):
        return text[2:]
    path = Path(text)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(home).as_posix()
    except ValueError:
        return None


def _home_file(raw, home):
    located = _user_path(raw, home)
    try:
        located.relative_to(home)
    except ValueError:
        return None
    return located if located.is_file() else None


def path_warnings(removed, home=None):
    # 只认当前用户主目录里真实存在的引用文件，不套用别人的目录。
    home = target_home() if home is None else Path(home).resolve()
    warnings = []

    def add(located, impacted):
        if impacted:
            warnings.append({'file': '~/' + located.relative_to(home).as_posix(), 'entries': impacted})

    companion = PROJECT / 'work/path-references.json'
    if companion.is_file():
        records = json.loads(companion.read_text())['records']
        for row in records:
            located = _home_file(row.get('file', ''), home)
            if located is None:
                continue
            references = [key for key in (_entry_key(raw, home) for raw in row.get('references', [])) if key]
            add(located, [e for e in removed if any(r == e or r.startswith(e + '/') for r in references)])
    else:
        for line in (PROJECT / 'docs/path-references.md').read_text().splitlines():
            cells = line.split('|')
            if len(cells) < 5:
                continue
            raw_file = cells[1].strip().strip('`')
            if not raw_file.startswith(('~/', '/')):
                continue
            located = _home_file(raw_file, home)
            if located is None:
                continue
            names = re.findall(r'`([^`]+)`', cells[2])
            add(located, [e for e in removed if Path(e).name in names])
    return warnings


def assignment_plan(registry, agents, changes, home=None):
    catalog = profiles.by_id(home)
    managed = [catalog[a] for a in agents]
    result = copy.deepcopy(registry)
    explicit_removals, changed, reasons, auto_added = set(), set(), [], []
    for change in changes:
        name, agent, enabled = change['skill'], change['agent'], change['enabled']
        if name not in result['skills'] or agent not in agents or type(enabled) is not bool:
            raise ValueError('请选择登记表里的技能和已接入的 agent。')
        assigned = set(result['skills'][name]['assigned_agents'])
        if enabled:
            assigned.add(agent)
            result['skills'][name].pop('withdrawn', None)
            result['skills'][name].pop('withdrawn_at', None)
        else:
            assigned.discard(agent)
            explicit_removals.add((name, agent))
        result['skills'][name]['assigned_agents'] = sorted(assigned)
    def include(name, agent, visiting):
        if name in visiting:
            return
        for dependency in result['skills'][name].get('requires', []):
            if dependency not in result['skills']:
                raise ValueError(f'{name} 需要的 {dependency} 还没有收进仓库，请先安装。')
            if (dependency, agent) in explicit_removals:
                continue
            assigned = result['skills'][dependency]['assigned_agents']
            if agent not in assigned:
                assigned.append(agent); assigned.sort()
                reasons.append(f'因为 {name} 需要 {dependency}，已一起勾上给 {catalog[agent]["name"]}')
                auto_added.append({'skill': dependency, 'agent': agent, 'because': name})
            include(dependency, agent, visiting | {name})
    # Only new selections expand dependencies; withdrawing a dependency requires consent.
    for name, skill in result['skills'].items():
        for agent in set(skill['assigned_agents']) - set(registry['skills'][name]['assigned_agents']):
            include(name, agent, set())
    dependency_warnings = []
    for name, agent in sorted(explicit_removals):
        users = [n for n, s in result['skills'].items() if agent in s['assigned_agents'] and name in s.get('requires', [])]
        if users:
            dependency_warnings.append(f'{catalog[agent]["name"]} 的 {"、".join(users)} 仍需要 {name}，撤下后这些技能可能无法正常使用。')
    removed = []
    for name, skill in result['skills'].items():
        if skill['assigned_agents'] != registry['skills'][name]['assigned_agents']:
            changed.add(name)
            removed += sorted(set(skill.get('entries', [])) - set(assignment_entries(name, skill['assigned_agents'], home, managed)))
    summary = []
    for agent in agents:
        added = [n for n in changed if agent in result['skills'][n]['assigned_agents'] and agent not in registry['skills'][n]['assigned_agents']]
        withdrawn = [n for n in changed if agent not in result['skills'][n]['assigned_agents'] and agent in registry['skills'][n]['assigned_agents']]
        if added:
            summary.append(f'将给 {catalog[agent]["name"]} 加 {len(added)} 个')
        if withdrawn:
            summary.append(f'从 {catalog[agent]["name"]} 撤下 {len(withdrawn)} 个')
    return {'registry': result, 'changed': sorted(changed), 'reasons': reasons, 'auto_added': auto_added,
            'dependency_warnings': dependency_warnings, 'path_warnings': path_warnings(removed, home),
            'message': '、'.join(summary) or '分配没有变化'}


def preview_assign(changes, home=None):
    home, registry, agents = load(home)
    plan = assignment_plan(registry, agents, changes, home)
    cost = {n: listing_tokens(n, metadata((home / '.skill-pilot/store' / n / 'SKILL.md').read_text()))
            for n in registry['skills']}
    return {k: v for k, v in plan.items() if k != 'registry'} | {
        'assignments': {n: s['assigned_agents'] for n, s in plan['registry']['skills'].items()},
        'cell_notes': cell_notes(plan['registry'], home, agents),
        'after': {a: {'count': sum(a in s['assigned_agents'] for s in plan['registry']['skills'].values()),
                      'description_tokens': sum(cost[n] for n, s in plan['registry']['skills'].items() if a in s['assigned_agents'])}
                  for a in agents}}


def _snapshot(home, agents, checked, extra_paths=None):
    roots = sorted({r for p in profiles.managed(home, agents) for r in p['skill_roots']})
    return backup(home, {'roots': roots + profiles.config_paths(home, agents) + ['.skill-pilot/logs'] + (extra_paths or []),
                         'plan_id': revision(home, agents), 'before': visibility(home)}, checked)


def _validate(home, registry, agents):
    current = visibility(home)
    managed = set(registry['skills'])
    for agent in agents:
        expected = {n for n, s in registry['skills'].items() if agent in s['assigned_agents']}
        if set(current[agent]['available']) & managed != expected:
            raise ValueError(f'{profiles.by_id(home)[agent]["name"]} 的实际技能名单与勾选结果不一致，已经恢复原样。原有停用设置可能限制了这次分配。')


def _commit(home, registry, names, content_names, message):
    store = home / '.skill-pilot/store'
    for name in names:
        write_json(store / '.skill-pilot-state' / (name + '.json'), registry['skills'][name], lambda p: guard(p, home))
    paths = ['.skill-pilot-state/' + n + '.json' for n in names] + list(content_names)
    if not (store / '.git').exists():
        github.git(store, 'init', '-q')
    if paths:
        github.git(store, 'add', '--', *paths)
    github.git(store, '-c', 'user.name=Skill Pilot', '-c', 'user.email=local@skill-pilot.invalid',
               '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', message,
               *(['--only', '--', *paths] if paths else []))
    return github.git(store, 'rev-parse', 'HEAD')


def _run(home, registry, agents, expected, message, mutation, undo_of=None, extra_paths=None):
    checked = lambda p: guard(p, home)
    checked(home / '.skill-pilot')
    with transaction(home) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if callable(expected):
            registry = expected()
        elif revision(home, agents) != expected:
            raise ValueError('目录内容已经变了，请刷新后重新核对。')
        for root in sorted({r for p in profiles.managed(home, agents) for r in p['skill_roots']}):
            check_links(home / root, home)
        check_links(home / '.skill-pilot', home)
        snapshot = _snapshot(home, agents, checked, extra_paths)
        try:
            names, content, result = mutation(registry, checked)
            agents = [p['id'] for p in profiles.managed(home)]
            _validate(home, registry, agents)
            original_registry = result.pop('_registry_bytes', None)
            if original_registry is None:
                write_json(home / '.skill-pilot/registry.json', registry, checked)
            else:
                checked(home / '.skill-pilot/registry.json').write_bytes(original_registry)
            commit = result.pop('_commit', None) or _commit(home, registry, names, content, message)
            agents_changed = result.pop('_agents_changed', False)
            import tarfile
            with tarfile.open(home / '.skill-pilot/backups' / snapshot['backup_id'] / 'files.tar') as archive:
                previous = json.load(archive.extractfile('.skill-pilot/registry.json'))
            involved = sorted({a for n in names for a in registry['skills'].get(n, {}).get('assigned_agents', [])} |
                              {a for n in names for a in previous['skills'].get(n, {}).get('assigned_agents', [])})
            if agents_changed:
                involved = sorted(set(involved) | set(result.get('agents', [])) |
                                  ({result['agent']} if result.get('agent') else set()))
            record = {'id': commit, 'time': datetime.now(timezone.utc).isoformat(), 'message': message,
                      'agents': involved,
                      'backup_id': snapshot['backup_id'], 'skills': names, 'undo_of': undo_of,
                      'after_revision': revision(home, agents),
                      'extra_paths': extra_paths or [],
                      'extra_state': {p: physical_state(home / p) for p in extra_paths or []},
                      'agents_changed': agents_changed,
                      }
            write_json(home / '.skill-pilot/logs' / (commit + '.json'), record, checked)
            return {**result, 'id': commit, 'message': message, 'backup_id': snapshot['backup_id']}
        except Exception as error:
            try:
                _restore(home, snapshot['backup_id'], checked)
            except Exception as rollback:
                raise RollbackError('这项操作没能完全恢复，后续更新已停止，请先用备份还原。') from rollback
            if isinstance(error, ValueError):
                raise
            raise ValueError('操作没能完成，已经自动恢复原样。') from error


def apply_assign(changes, expected=None, confirmed=False, home=None):
    home, registry, agents = load(home)
    guard(home / '.skill-pilot', home)
    plan = assignment_plan(registry, agents, changes, home)
    if (plan['dependency_warnings'] or plan['path_warnings']) and not confirmed:
        return {'needs_confirmation': True, **{k: v for k, v in plan.items() if k != 'registry'}}
    if not plan['changed']:
        return {'message': plan['message'], 'reasons': plan['reasons']}
    def mutate(current, checked):
        current.update(plan['registry'])
        reconcile_assignments(current, home, checked, plan['changed'])
        return plan['changed'], [], {'reasons': plan['reasons']}
    return _run(home, registry, agents, expected or revision(home, agents), plan['message'], mutate)


def decide_dependencies(decisions, expected=None, home=None):
    home, registry, agents = load(home)
    guard(home / '.skill-pilot', home)
    names, changes = set(), []
    for d in decisions:
        name, dependency = d['skill'], d['requires']
        if name not in registry['skills'] or dependency not in registry['skills'] or name == dependency or type(d['confirmed']) is not bool:
            raise ValueError('请选择仓库里两个不同的技能。')
        skill = registry['skills'][name]
        skill.setdefault('dependency_decisions', {})[dependency] = 'confirmed' if d['confirmed'] else 'rejected'
        requires = set(skill.get('requires', []))
        (requires.add if d['confirmed'] else requires.discard)(dependency)
        skill['requires'] = sorted(requires)
        changes += [{'skill': dependency, 'agent': a, 'enabled': True} for a in skill['assigned_agents']] if d['confirmed'] else []
        names.add(name)
    plan = assignment_plan(registry, agents, changes, home)
    registry.update(plan['registry'])
    names.update(plan['changed'])
    def mutate(current, checked):
        reconcile_assignments(current, home, checked, sorted(names))
        return sorted(names), [], {}
    return _run(home, registry, agents, expected or revision(home, agents), f'修改了 {len(decisions)} 条需要的技能', mutate)


def _put(home, registry, item, assigned, entries, checked, normalize_content=True):
    name = item['name']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise ValueError('技能名称不能安全用作文件夹名称，请先核对。')
    source = Path(item['path'])
    github.safe_skill(source)
    destination = checked(home / '.skill-pilot/store' / name)
    if destination.exists():
        raise ValueError('仓库里已有这个名字的文件，请先核对两份内容。')
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_CONTENT))
    processing = normalization.apply(destination) if normalize_content else {}
    base = checked(home / '.skill-pilot/bases' / name)
    checked(base.parent).mkdir(parents=True, exist_ok=True)
    shutil.copytree(destination, base, symlinks=True)
    registry['skills'][name] = {'source': item['source'], 'base_digest': content_digest(destination),
                              'requires': [], 'assigned_agents': sorted(set(assigned)), 'entries': entries,
                              'installed_at': datetime.now(timezone.utc).isoformat(), 'installation_evidence': '本次安装或收编',
                              'normalization': processing}


def conflicts(items, registry, home):
    result = []
    for item in items:
        name = item['name']
        if name in registry['skills']:
            result.append({'name': name, 'same_content': normalization.tree(Path(item['path'])) == normalization.tree(home / '.skill-pilot/store' / name),
                           'existing': str(home / '.skill-pilot/store' / name), 'incoming': item['path'],
                           'message': '仓库里已有同名技能，请先对比；现有内容没有覆盖。'})
    return result


def install(review, names, assigned=None, expected=None, home=None):
    home, registry, agents = load(home)
    guard(home / '.skill-pilot', home)
    assigned = assigned or []
    if not set(assigned) <= set(agents) or not names:
        raise ValueError('请选择技能和已接入的 agent。')
    items = [s for s in review['skills'] if s['name'] in names]
    if set(names) != {s['name'] for s in items}:
        raise ValueError('所选技能不在本次安装预览里。')
    for item in items:
        if content_digest(Path(item['path'])) != item['digest']:
            raise ValueError('下载的文件已经变了，请重新读取仓库。')
    collisions = conflicts(items, registry, home)
    if collisions:
        return {'conflicts': collisions, 'message': '有同名技能需要先对比，文件没有改动。'}
    def mutate(current, checked):
        for item in items:
            _put(home, current, item, [], [], checked)
        changes = [{'skill': s['name'], 'agent': a, 'enabled': True} for s in items for a in assigned]
        plan = assignment_plan(current, agents, changes, home)
        current.update(plan['registry'])
        changed = sorted(set(names) | set(plan['changed']))
        reconcile_assignments(current, home, checked, changed)
        return changed, sorted(names), {'reasons': plan['reasons'],
                                      'normalization': {n: current['skills'][n]['normalization'] for n in names}}
    return _run(home, registry, agents, expected or revision(home, agents), f'安装了 {len(items)} 个技能', mutate)

def bootstrap(home):  # 首次安装；撤销或撤下后不再自动安装。
    if not write_allowed(home) or not (home / '.skill-pilot/registry.json').is_file():
        return
    _, registry, agents = load(home)
    if 'skill-pilot' in registry['skills'] or any('skill-pilot' in r['skills'] for r in history(home)):
        return
    path = PROJECT / 'skill/skill-pilot'
    return install({'skills': [{'name': 'skill-pilot', 'path': str(path), 'digest': content_digest(path),
                               'source': {'source': '自制'}}]}, ['skill-pilot'], agents, home=home)

def _local_folder(name, folder, home, registry):
    if not isinstance(name, str) or name not in registry['skills']:
        raise ValueError('这个技能不在登记表里，请刷新后再选。')
    if normalize(registry['skills'][name]['source'])['kind'] != 'skip':
        raise ValueError('这个技能有上游来源，请用检查更新。这里只更新自制技能。')
    raw = Path(folder)
    incoming = raw.resolve() if raw.is_absolute() else (home / raw).resolve()
    if not incoming.is_dir() or not (incoming / 'SKILL.md').is_file():
        raise ValueError('请选择带有技能说明的文件夹。')
    if metadata((incoming / 'SKILL.md').read_text()).get('name') != name:
        raise ValueError('文件夹里的技能名称和要更新的不一致。')
    current = (home / '.skill-pilot/store' / name).resolve()
    if incoming == current or current in incoming.parents:
        raise ValueError('请另选一份文件，不能用正在使用的这一份来更新自己。')
    github.safe_skill(incoming)
    return incoming


def update_local(name, folder, expected=None, home=None, confirmed=False):
    home, registry, agents = load(home)
    incoming = _local_folder(name, folder, home, registry)
    same = normalization.tree(incoming) == normalization.tree(home / '.skill-pilot/store' / name)
    if not confirmed:
        return {'name': name, 'changed': not same,
                'message': '内容和正在使用的一样，没有需要更新的。' if same else f'将用这份文件更新「{name}」。请核对后加 --yes 执行。'}
    if same:
        return {'name': name, 'changed': False, 'message': '内容和正在使用的一样，没有需要更新的。'}
    guard(home / '.skill-pilot', home)
    def mutate(current, checked):
        destination = checked(home / '.skill-pilot/store' / name)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(incoming, destination, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_CONTENT))
        base = checked(home / '.skill-pilot/bases' / name)
        if base.exists():
            shutil.rmtree(base)
        base.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(destination, base, symlinks=True)
        current['skills'][name]['base_digest'] = content_digest(destination)
        return [name], [name], {}
    return _run(home, registry, agents, expected or revision(home, agents), f'用本地文件更新了「{name}」', mutate)


def absorb(all_skills=False, selected=None, expected=None, home=None):
    home, registry, agents = load(home)
    guard(home / '.skill-pilot', home)
    start = expected or revision(home, agents)
    candidates = new_skills(home, agents)
    if not all_skills:
        if selected:
            candidates = [s for s in candidates if s['relative'] in selected]
        elif len(candidates) != 1:
            return {'new_skills': candidates, 'message': '请指定要收编的技能，或使用 --all 收编全部。'}
    if not candidates:
        return {'message': '没有需要收编的新技能。'}
    collisions = [c for c in conflicts(candidates, registry, home) if not c['same_content']]
    grouped = {}
    for item in candidates:
        name = item['name']
        if name in grouped and normalization.tree(Path(grouped[name]['path'])) != normalization.tree(Path(item['path'])):
            collisions.append({'name': name, 'existing': grouped[name]['path'], 'incoming': item['path'],
                               'message': '新装的同名技能内容不同，请先对比。'})
        grouped[name] = item
    if collisions:
        return {'conflicts': collisions, 'message': '有同名技能需要先对比，文件没有改动。'}
    lock_path = home / '.agents/.skill-lock.json'
    records = json.loads(lock_path.read_text()).get('skills', {}) if lock_path.exists() else {}
    for name, item in grouped.items():
        if name in registry['skills']:
            continue
        record = records.get(name)
        item['source'] = ({'source': record.get('sourceUrl', record.get('source', '安装来源记录')),
                           'source_path': record.get('skillPath', '.'), 'record': record,
                           'confidence': '高', 'evidence': '继承现有安装来源记录'} if record else
                          github.verify_source(Path(item['path']), name))
    def mutate(current, checked):
        names = sorted(grouped)
        content = [n for n in names if n not in current['skills']]
        for name in names:
            copies = [s for s in candidates if s['name'] == name]
            assigned = sorted({a for s in copies for a in s['assigned_agents']} |
                              set(current['skills'].get(name, {}).get('assigned_agents', [])))
            if name not in current['skills']:
                _put(home, current, grouped[name], assigned, [], checked)
            else:
                current['skills'][name]['assigned_agents'] = assigned
            for item in copies:
                source = checked(Path(item['path']))
                # Move the full original aside in this operation's backup; never discard user files.
                moved = checked(home / '.skill-pilot/backups' / ('absorbed-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')))
                shutil.move(str(source), str(moved))
                current['skills'][name]['entries'].append(item['relative'])
                source.symlink_to(str(home / '.skill-pilot/store' / name), target_is_directory=True)
            reconcile_assignments(current, home, checked, [name])
        return names, content, {'normalization': {n: current['skills'][n].get('normalization', {}) for n in names}}
    return _run(home, registry, agents, start, f'收编了 {len(grouped)} 个新装技能', mutate)


def set_source(name, repository, folder, confirmed=False, home=None):
    if not confirmed:
        raise ValueError('来源补登记需要确认，请核对后加 --yes 执行。')
    home, registry, agents = load(home)
    guard(home/'.skill-pilot', home)
    if name not in registry['skills'] or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('请选择已有技能，并填写 owner/repo 形式的来源。')
    source = {'source':'https://github.com/'+repository,'repository':repository,'source_path':folder}
    normalized = normalize(source)
    expected = revision(home,agents)
    import tempfile
    from .sources.update_github import Reader
    with tempfile.TemporaryDirectory(prefix='skill-pilot-source-') as temporary:
        row = Reader(temporary).inspect(normalized,home/'.skill-pilot/store'/name)
        if metadata((Path(row['_base_path'])/'SKILL.md').read_text()).get('name') != name:
            raise ValueError('来源位置中的技能名称不一致，请先核对目录。')
        def mutate(current, checked):
            base = checked(home/'.skill-pilot/bases'/name)
            if base.exists():
                shutil.rmtree(base)
            shutil.copytree(row['_base_path'],base,symlinks=True)
            skill = current['skills'][name]
            skill['source'] = {**source,'source_path':normalized['path'],'commit':row['base_commit'],
                               'tree_sha':row['base_tree_sha'],'confidence':'高','evidence':'指定来源后按完整历史目录重新校准',
                               'similarity':row['similarity']}
            skill['base_digest'] = content_digest(base)
            skill['base_tree_sha'] = row['base_tree_sha']
            skill.pop('reviewed_tree_sha',None)
            return [name], [], {'source':skill['source'],'local_modified':row['local_modified']}
        return _run(home,registry,agents,expected,f'补登记了 {name} 的来源，并重新核对原始版本',mutate)


def undo(operation_id, expected=None, home=None):
    home, registry, agents = load(home)
    guard(home / '.skill-pilot', home)
    record = next((r for r in history(home) if r['id'] == operation_id), None)
    if record is None or record['undone'] or record.get('undo_of'):
        raise ValueError('这条记录不能再次撤销。')
    before_file = home / '.skill-pilot/backups' / record['backup_id'] / 'files.tar'
    import tarfile

    def extract_prefix(relative):
        with tarfile.open(before_file) as source:
            for member in source.getmembers():
                if member.name == relative or member.name.startswith(relative + '/'):
                    source.extract(member, home, filter='data')

    with tarfile.open(before_file) as archive:
        before_bytes = archive.extractfile('.skill-pilot/registry.json').read()
        before = json.loads(before_bytes)
    def mutate(current, checked):
        store = home / '.skill-pilot/store'
        later = [r for r in history(home) if r['time'] > record['time'] and not r.get('undone') and not r.get('undo_of')]
        if later and (record.get('agents_changed') or any(r.get('agents_changed') for r in later)):
            raise ValueError('后来的操作涉及 agent 接入或退出，请先撤销后来的操作。文件没有改动。')
        if any(set(record['skills']) & set(r['skills']) for r in later):
            raise ValueError('后来的操作涉及同一个技能，请先撤销后来的操作。文件没有改动。')
        if any(physical_state(home / p) != state for p, state in record.get('extra_state', {}).items()):
            raise ValueError('本次操作涉及的目录又有变化，请先核对，再撤销。')
        if not later and record.get('after_revision') != revision(home, agents):
            raise ValueError('这次操作后出现了外部改动，请先核对这些变化，再撤销。文件没有改动。')
        try:
            if record.get('agents_changed') and not record['skills']:
                _commit(home, current, [], [], '撤销：' + record['message'])
            else:
                github.git(store, '-c', 'user.name=Skill Pilot', '-c', 'user.email=local@skill-pilot.invalid',
                           '-c', 'commit.gpgsign=false', 'revert', '--no-edit', operation_id)
        except ValueError as error:
            try:
                github.git(store, 'revert', '--abort')
            except ValueError:
                pass
            raise ValueError('后来的操作与这次改动重叠，请先撤销后来的操作。文件已恢复原样。') from error
        names = record['skills']
        # The user may have edited these files without recording an operation.
        # Reverting the commit alone would restore the last committed copy and
        # lose those edits; recover the exact pre-operation copy from backup.
        for name in names:
            if name in before['skills']:
                directory = checked(home / '.skill-pilot/store' / name)
                if directory.exists():
                    shutil.rmtree(directory)
                extract_prefix('.skill-pilot/store/' + name)
        if not later:
            # Exact root/config/base restoration, keeping the new reversal commit.
            snapshot = json.loads((home / '.skill-pilot/backups' / record['backup_id'] / 'manifest.json').read_text())
            original_paths = snapshot['paths']
            excluded = '.skill-pilot/store'
            filtered = {**snapshot, 'paths': [p for p in original_paths if p != excluded],
                        'existing': [p for p in snapshot['existing'] if p != excluded]}
            # Extract only the allowed members to a temporary backup, never rewrite the original.
            scratch = _snapshot(home, agents, checked)
            directory = home / '.skill-pilot/backups' / scratch['backup_id']
            filtered['backup_id'] = scratch['backup_id']
            filtered['paths'] = [p for p in filtered['paths'] if p != '.skill-pilot/logs']
            filtered['existing'] = [p for p in filtered['existing'] if p != '.skill-pilot/logs']
            write_json(directory / 'manifest.json', filtered, checked)
            with tarfile.open(before_file) as source, tarfile.open(directory / 'files.tar', 'w') as target:
                for member in source.getmembers():
                    if any(member.name == p or member.name.startswith(p + '/') for p in filtered['paths']):
                        target.addfile(member, source.extractfile(member) if member.isfile() else None)
            _restore(home, scratch['backup_id'], checked)
            current.clear(); current.update(before)
        else:
            if record.get('extra_paths'):
                raise ValueError('请先撤销后来的操作，再撤销这次飞书工具更新。')
            for name in names:
                if name in before['skills']:
                    # A later independent operation must not leave an updated
                    # baseline behind when this skill's content is reverted.
                    base = checked(home / '.skill-pilot/bases' / name)
                    if base.exists():
                        shutil.rmtree(base)
                    extract_prefix('.skill-pilot/bases/' + name)
                    old_entries = current['skills'][name].get('entries', [])
                    current['skills'][name] = copy.deepcopy(before['skills'][name])
                    current['skills'][name]['entries'] = sorted(set(old_entries) | set(current['skills'][name].get('entries', [])))
                else:
                    for entry in current['skills'][name].get('entries', []):
                        path = checked(home / entry)
                        if path.is_symlink():
                            path.unlink()
                    base = checked(home / '.skill-pilot/bases' / name)
                    if base.exists():
                        shutil.rmtree(base)
                    del current['skills'][name]
            reconcile_assignments(current, home, checked, [n for n in names if n in current['skills']],
                                  {n: before['skills'][n].get('entries', []) for n in names if n in before['skills']})
        return names, [], {'_commit': github.git(store, 'rev-parse', 'HEAD'),
                           **({'_registry_bytes': before_bytes} if not later else {})}
    return _run(home, registry, agents, expected or revision(home, agents), '撤销：' + record['message'], mutate,
                operation_id, extra_paths=record.get('extra_paths'))
