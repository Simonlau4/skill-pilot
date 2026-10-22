"""Manual withdrawal, deletion and reversible restoration of registered skills."""
from datetime import datetime, timezone

from . import labels, normalization, operations
from .sources import normalize
from .store import remove
from .sync import reconcile_assignments


def list_withdrawn(home=None):
    home, registry, agents = operations.load(home)
    return {'revision': operations.revision(home, agents), 'write_allowed': operations.write_allowed(home),
            'withdrawn': [{'name': name, 'previous_agents': skill.get('withdrawn_agents', []),
                           'withdrawn_at': skill.get('withdrawn_at')}
                          for name, skill in sorted(registry['skills'].items()) if skill.get('withdrawn')]}


def preview(names, home=None):
    home, registry, agents = operations.load(home)
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
        raise ValueError('请选择要撤下的技能，名称需放在列表中。')
    selected = sorted(set(names))
    for name in selected:
        if name not in registry['skills']:
            raise ValueError(f'「{name}」不在登记表里，请刷新后再选择。')
        skill = registry['skills'][name]
        if skill.get('withdrawn'):
            raise ValueError(f'「{name}」已经撤下，请到已撤下列表恢复。')
        if not skill['assigned_agents']:
            raise ValueError(f'「{name}」没有分配给任何 agent，不能撤下。')
    titles = {name: label['title'] for group in labels.read(home)
              for name, label in group['skills'].items()}
    dependency_warnings = [f'「{titles.get(name, name)}」还要用「{titles.get(dependency, dependency)}」，撤下后它可能用不了。'
                           for name, skill in sorted(registry['skills'].items())
                           if name not in selected and skill['assigned_agents']
                           for dependency in sorted(set(skill.get('requires', [])) & set(selected))]
    removed = sorted({entry for name in selected for entry in registry['skills'][name]['entries']})
    return {'skills': selected, 'message': f'从所有 agent 撤下 {len(selected)} 个技能',
            'dependency_warnings': dependency_warnings, 'path_warnings': operations.path_warnings(removed, home),
            'revision': operations.revision(home, agents)}


def apply(names, confirmed=False, home=None, expected=None):
    home, registry, agents = operations.load(home)
    operations.guard(home/'.skill-pilot', home)
    view = preview(names, home)
    if expected is not None and expected != view['revision']:
        raise ValueError('目录内容已经变了，请刷新后重新核对。')
    if confirmed is not True:
        return {'needs_confirmation': True, **view}
    selected = view['skills']
    now = datetime.now(timezone.utc).isoformat()
    def mutate(current, checked):
        for name in selected:
            skill = current['skills'][name]
            skill.update(withdrawn=True, withdrawn_at=now, withdrawn_agents=list(skill['assigned_agents']),
                         withdrawn_entries=list(skill['entries']), assigned_agents=[])
        reconcile_assignments(current, home, checked, selected)
        return selected, [], {'withdrawn': selected, 'path_warnings': view['path_warnings'],
                              'dependency_warnings': view['dependency_warnings']}
    return operations._run(home, registry, agents, view['revision'], f'撤下了 {len(selected)} 个技能', mutate)


def delete_preview(names, home=None):
    home, registry, agents = operations.load(home)
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
        raise ValueError('请选择要删除的技能，名称需放在列表中。')
    selected = sorted(set(names))
    for name in selected:
        if name not in registry['skills']:
            raise ValueError(f'「{name}」不在登记表里，请刷新后再选择。')
        if name == 'skill-pilot':
            raise ValueError('Skill Pilot 自带的技能只能撤下，不能删除。请取消勾选后再删除。')
    titles = {name: label['title'] for group in labels.read(home)
              for name, label in group['skills'].items()}
    warnings = [f'「{titles.get(name, name)}」还要用「{titles.get(dependency, dependency)}」，删除后它可能用不了。'
                for name, skill in sorted(registry['skills'].items())
                if name not in selected and skill['assigned_agents']
                for dependency in sorted(set(skill.get('requires', [])) & set(selected))]
    removed = sorted({entry for name in selected for entry in registry['skills'][name]['entries']})
    return {'skills': selected, 'message': f'删除 {len(selected)} 个技能',
            'dependency_warnings': warnings,
            'irreplaceable': [name for name in selected
                              if normalize(registry['skills'][name]['source'])['kind'] == 'skip' or
                              normalization.tree(home / '.skill-pilot/store' / name) !=
                              normalization.tree(home / '.skill-pilot/bases' / name)],
            'path_warnings': operations.path_warnings(removed, home),
            'revision': operations.revision(home, agents)}


def delete(names, confirmed=False, home=None, expected=None):
    home, registry, agents = operations.load(home)
    operations.guard(home / '.skill-pilot', home)
    view = delete_preview(names, home)
    if expected is not None and expected != view['revision']:
        raise ValueError('目录内容已经变了，请刷新后重新核对。')
    if confirmed is not True:
        return {'needs_confirmation': True, **view}
    selected = view['skills']
    def mutate(current, checked):
        for name in selected:
            current['skills'][name]['assigned_agents'] = []
        reconcile_assignments(current, home, checked, selected)
        for name in selected:
            for relative in ('store/' + name, 'bases/' + name, 'store/.skill-pilot-state/' + name + '.json'):
                remove(home / '.skill-pilot' / relative, checked)
            del current['skills'][name]
        changed = set(selected)
        for name, skill in current['skills'].items():
            if set(skill.get('requires', [])) & set(selected):
                skill['requires'] = [n for n in skill['requires'] if n not in selected]
                changed.add(name)
        return sorted(changed), selected, {'deleted': selected, 'dependency_warnings': view['dependency_warnings'],
                                          'path_warnings': view['path_warnings'], 'irreplaceable': view['irreplaceable']}
    return operations._run(home, registry, agents, view['revision'], f'删除了 {len(selected)} 个技能', mutate)


def restore(names, home=None, expected=None):
    home, registry, agents = operations.load(home)
    operations.guard(home/'.skill-pilot',home)
    names = sorted(set(names))
    if not names or any(n not in registry['skills'] or not registry['skills'][n].get('withdrawn') for n in names):
        raise ValueError('请选择已撤下的技能。')
    changes = [{'skill':n,'agent':a,'enabled':True} for n in names for a in registry['skills'][n].get('withdrawn_agents',[])]
    plan = operations.assignment_plan(registry,agents,changes,home)
    def mutate(current, checked):
        current.update(plan['registry'])
        for name in names:
            current['skills'][name].pop('withdrawn',None)
            current['skills'][name].pop('withdrawn_at',None)
        changed = sorted(set(names)|set(plan['changed']))
        reconcile_assignments(current,home,checked,changed,{n:current['skills'][n]['withdrawn_entries'] for n in names})
        return changed, [], {'restored':names,'reasons':plan['reasons']}
    return operations._run(home,registry,agents,expected or operations.revision(home,agents),f'恢复了 {len(names)} 个已撤下的技能',mutate)
