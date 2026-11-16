"""Release a skill to its existing external owner without changing agent entries."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import profiles
from .scan import scan_root
from .store import remove
from .sync import visibility


def entries(home, names):
    catalog = profiles.all_profiles(home)
    found = {name: [] for name in names}
    for relative in sorted({r for p in catalog for r in p['skill_roots']}):
        root = home / relative
        inventory = scan_root(root, profiles.root_nested(relative, catalog), excluded=profiles.exclusions(root, home))
        if inventory['errors']:
            raise ValueError('有技能目录没能完整读取，请解决后再退出托管。')
        for skill in inventory['skills']:
            if skill['name'] in found:
                found[skill['name']].append(skill)
    return found


def preview(name, home=None):
    from . import operations
    home, registry, agents = operations.load(home)
    if not isinstance(name, str) or name not in registry['skills'] or name == 'skill-pilot':
        raise ValueError('请选择仍由 Skill Pilot 托管的普通技能。')
    copies = entries(home, [name])[name]
    if not copies or any(not s['valid'] for s in copies):
        raise ValueError('还没有完整可用的外部副本，请先由原应用接管入口，再退出托管。')
    if any(Path(s['path']).resolve().is_relative_to(home / '.skill-pilot') for s in copies):
        raise ValueError('仍有 Agent 使用 Skill Pilot 库内副本，请先由原应用接管这些入口，再退出托管。')
    # Also cover old recorded aliases that no longer advertise this name.
    for relative in registry['skills'][name].get('entries', []):
        path = home / relative
        if (path.exists() or path.is_symlink()) and path.resolve().is_relative_to(home / '.skill-pilot'):
            raise ValueError('仍有旧入口依赖库内副本，请先核对入口，再退出托管。')
    removed = ['.skill-pilot/store/' + name, '.skill-pilot/bases/' + name]
    if operations.path_warnings(removed, home):
        raise ValueError('其他文件还直接引用库内副本，请先改为原应用的入口，再退出托管。')
    actual = visibility(home)
    available = [a for a in actual if name in actual[a]['available']]
    if not available:
        raise ValueError('没有 Agent 能正常读取外部副本，请先恢复使用入口，再退出托管。')
    owner = next((part[:-4] for s in copies for part in Path(s['path']).resolve().parts if part.endswith('.app')), '外部工具')
    view = {'name': name, 'owner': owner, 'agents': available,
            'entries': sorted({Path(s['path']).relative_to(home).as_posix() for s in copies}),
            'revision': operations.revision(home, agents),
            'message': '退出托管并保留现有入口，清理 Skill Pilot 库内副本，改为只读展示。'}
    evidence = [view, [(s['path'], s['resolved_path'], s['digest']) for s in copies]]
    view['plan_id'] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return view


def release(name, confirmed=False, home=None, expected=None, plan_id=None):
    from . import operations
    home, registry, agents = operations.load(home)
    view = preview(name, home)
    if (expected is not None and expected != view['revision']) or (plan_id is not None and plan_id != view['plan_id']):
        raise ValueError('技能或入口已经变化，请重新核对后再退出托管。')
    if confirmed is not True:
        return {'needs_confirmation': True, **view}
    operations.guard(home / '.skill-pilot', home)
    before = visibility(home)
    def mutate(current, checked):
        if preview(name, home)['plan_id'] != view['plan_id']:
            raise ValueError('技能或入口已经变化，请重新核对后再退出托管。')
        current.setdefault('external_skills', {})[name] = {
            'owner': view['owner'], 'entries': view['entries'],
            'released_at': datetime.now(timezone.utc).isoformat()}
        del current['skills'][name]
        for relative in ('store/' + name, 'bases/' + name, 'store/.skill-pilot-state/' + name + '.json'):
            remove(home / '.skill-pilot' / relative, checked)
        if visibility(home) != before:
            raise ValueError('退出后 Agent 的可用技能发生变化，已停止并恢复原样。')
        return [name], [name], {'released': [name], '_expected_visibility': {a: s['available'] for a, s in before.items()}}
    return operations._run(home, registry, agents, view['revision'], '已退出托管，保留使用入口：' + name, mutate,
                           preserve_paths=view['entries'])


def rows(home, registry, agents, actual):
    from .operations import listing_tokens
    records = registry.get('external_skills', {})
    if not records:
        return []
    copies = entries(home, records)
    result = []
    for name, record in sorted(records.items()):
        valid = [s for s in copies[name] if s['valid']]
        description = valid[0].get('description', '') if valid else ''
        result.append({'name': name, 'description': description, 'description_tokens': listing_tokens(name, {'description': description}),
                       'source': {}, 'source_kind': 'external', 'source_label': record['owner'] + ' 维护',
                       'read_only': True, 'owner': record['owner'], 'requires': [], 'withdrawn': False,
                       'assigned_agents': [a for a in agents if name in actual[a]['available']],
                       'previous_agents': [], 'update_watch': 'off', 'local_modified': False,
                       'cell_notes': {}, 'external_missing': not valid,
                       'version': valid[0].get('version') if valid else None})
    return result
