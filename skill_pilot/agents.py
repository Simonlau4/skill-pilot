"""Repeatable assistant enrollment and departure using the operation transaction."""
import copy
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from . import operations, profiles, normalization
from .scan import scan_root, suggestions, content_digest
from .store import check_links, guard, physical_state, remove, target_home, write_json
from .sync import visibility, reconcile_disabled


def overview(home=None, discovered=None):
    home = target_home() if home is None else Path(home).resolve()
    local = profiles.state(home)
    catalog = profiles.all_profiles(home)
    managed = profiles.managed(home, local['onboarded'])
    ignored = [{'id': p['id'], 'name': p['name'], 'skill_roots': p['skill_roots'],
                **({'skill_count': sum(scan_root(home / r, p['nested'], excluded=profiles.exclusions(home / r, home))['valid_count'] for r in p['skill_roots'])}
                   if discovered is None else {}),
                'reason': '你之前选了忽略或不再管理。'} for p in catalog
               if p['id'] in local.get('ignored', []) and p['id'] not in local['onboarded'] and
               (profiles.evidence(p, home) or any((home / r).is_dir() for r in p['skill_roots'])) and
               all(not (home / r).exists() or (home / r).is_dir() for r in p['skill_roots'])]
    unavailable = set(local['onboarded']) | set(local.get('ignored', []))
    known = {p['id']: p for p in catalog}
    suggested = suggestions(home) if discovered is None else [
        {**item, **{key: known[item['id']][key] for key in ('name', 'skill_roots')
                   if item['id'] in known}}
        for item in discovered if item['id'] not in unavailable
    ]
    return {'revision': operations.revision(home, local['onboarded'])
            if discovered is None and (home / '.skill-pilot/registry.json').is_file() else None,
            'managed': [{'id': p['id'], 'name': p['name'], 'skill_roots': p['skill_roots'],
                         'activation_text': profiles.activation_text(p),
                         'shared_with': [q['id'] for q in managed if q != p and set(q['skill_roots']) & set(p['skill_roots'])],
                         'can_leave': len(managed) > 1} for p in managed],
            'suggested': suggested, 'ignored': ignored,
            'must_select_together': profiles.shared_groups(catalog, home)}


def _root(value, home):
    path = Path(str(value).removeprefix('~/'))
    path = path if path.is_absolute() else home / path
    if '..' in path.parts or not path.is_relative_to(home) or not path.resolve().is_relative_to(home):
        raise ValueError('技能目录必须在你的主目录内。')
    relative = str(path.relative_to(home))
    if relative == '.' or path.is_relative_to(home / '.skill-pilot'):
        raise ValueError('不能把统一仓库或主目录当作 agent 的技能目录。')
    if any(path.is_relative_to(home / r) for r in profiles.readonly_roots(home)):
        raise ValueError('这个目录由软件自己管理，只能查看，不能接入。')
    known = {r for p in profiles.all_profiles(home) for r in p['skill_roots']}
    if any((Path(relative).is_relative_to(r) or Path(r).is_relative_to(relative)) and relative != r for r in known):
        raise ValueError('这个目录和已有 agent 的技能目录重叠，请填写完整的共用目录。')
    if path.exists() and not path.is_dir():
        raise ValueError('技能目录必须是文件夹。')
    check_links(path, home)
    return relative


def _selection(requests, home):
    if not isinstance(requests, list) or not requests:
        raise ValueError('请至少选择一个要接入的 agent。')
    local = profiles.state(home)
    catalog = profiles.all_profiles(home)
    known = {p['id']: p for p in catalog}
    suggested = {p['id']: p for p in suggestions(home)}
    selected = []
    for request in requests:
        if isinstance(request, str):
            identifier = request
            if identifier in known:
                p = known[identifier]
            elif identifier in suggested:
                s = suggested[identifier]
                p = profiles.custom(identifier, s['name'], _root(s['skill_roots'][0], home))
            else:
                raise ValueError('这个 agent 不在建议里，请填写名字和技能目录。')
        elif isinstance(request, dict):
            root = _root(request.get('skill_root', ''), home)
            identifier = request.get('id') or re.sub(r'[^a-z0-9_-]', '-', Path(root).parts[0].lstrip('.').lower())
            p = known.get(identifier) or profiles.custom(identifier, request.get('name', ''), root)
            if root not in p['skill_roots'] or request.get('name', p['name']) != p['name']:
                raise ValueError('这个 agent 已保存为 ' + p['name'] + '，请用原来的名字和技能文件夹重新接入。')
        else:
            raise ValueError('请选择要接入的 agent，或填写名字和技能文件夹。')
        for root in p['skill_roots']:
            _root(root, home)
        if p['id'] in local['onboarded']:
            raise ValueError(p['name'] + ' 已经接入了。')
        if p['id'] not in {q['id'] for q in selected}:
            selected.append(p)
        if p['id'] not in known:
            catalog.append(p)
            known[p['id']] = p
    profiles.require_shared([p['id'] for p in selected], catalog, local['onboarded'], home)
    return selected, catalog


def preview_join(requests, home=None):
    home, registry, managed = operations.load(home)
    selected, catalog = _selection(requests, home)
    ids = [p['id'] for p in selected]
    before = visibility(home, catalog)
    roots = sorted({r for p in selected for r in p['skill_roots']})
    grouped, skipped = {}, []
    for root in roots:
        inventory = scan_root(home / root, profiles.root_nested(root, selected), excluded=profiles.exclusions(home / root, home))
        if inventory['errors']:
            raise ValueError('新 agent 的技能目录没有完整读出，请先核对。')
        for skill in inventory['skills']:
            if not skill['valid']:
                skipped.append({'path': skill['path'], 'reason': skill.get('warning') or '技能不可用，原样留着'})
                continue
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', skill['name']):
                raise ValueError('技能名称不能安全用作文件夹名称，请先核对。')
            grouped.setdefault(skill['name'], []).append(skill)
    skills = []
    from .web.server import compare_files
    for name, copies in sorted(grouped.items()):
        existing = home / '.skill-pilot/store' / name
        options = []
        if name in registry['skills']:
            options.append({'id': 'store', 'path': str(existing), 'origin': 'store', 'digest': content_digest(existing)})
        for item in copies:
            if not any(normalization.tree(Path(o['path'])) == normalization.tree(Path(item['path'])) for o in options):
                options.append({'id': physical_state(Path(item['path']).resolve()), 'path': item['path'],
                                'origin': 'incoming', 'digest': item['digest']})
        linked = all(Path(c['path']).is_symlink() and Path(c['path']).resolve() == existing for c in copies)
        action = 'choose' if len(options) > 1 else 'linked' if linked else 'link' if existing.exists() else 'import'
        reason = {'choose': '内容不同，默认保留仓库里的版本；选择新 agent 的版本，会更新所有 agent 共用的这份技能。',
                  'linked': '已经使用仓库里的版本，直接登记分配。', 'link': '内容相同，改为使用仓库里的版本。',
                  'import': '仓库里还没有，收进仓库。'}[action]
        if linked and any(os.readlink(c['path']) != str(existing) for c in copies):
            reason = '已经使用仓库里的版本，接入时会改为直接指向统一仓库并登记分配。'
        skills.append({'name': name, 'entries': sorted({str(Path(c['path']).relative_to(home)) for c in copies}),
                       'assigned_agents': [p['id'] for p in selected if name in before[p['id']]['available']],
                       'action': action, 'default': options[0]['id'], 'options': options, 'reason': reason,
                       'comparison': '\n'.join(compare_files(Path(options[0]['path']), Path(o['path'])) for o in options[1:])})
    rev = operations.revision(home, managed)
    payload = {'revision': rev, 'profiles': selected, 'roots': {r: physical_state(home / r, include_times=True) for r in roots}, 'skills': skills}
    return {'plan_id': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), 'revision': rev,
            'agents': ids, 'profiles': selected, 'skills': skills, 'conflicts': [s for s in skills if len(s['options']) > 1],
            'skipped': skipped, 'roots': roots, 'before': before,
            'must_select_together': profiles.shared_groups(catalog, home), 'read_only': True}


def join(requests, plan_id, expected=None, choices=None, home=None):
    home, registry, managed = operations.load(home)
    guard(home / '.skill-pilot', home)
    plan = preview_join(requests, home)
    if plan['plan_id'] != plan_id:
        raise ValueError('目录内容已经变了，请重新预览。')
    choices = choices or {}
    if not isinstance(choices, dict) or not set(choices) <= {s['name'] for s in plan['skills']}:
        raise ValueError('选择包含未知技能，请重新核对。')
    chosen = {}
    for skill in plan['skills']:
        option = next((o for o in skill['options'] if o['id'] == choices.get(skill['name'], skill['default'])), None)
        if option is None:
            raise ValueError('所选版本不在本次预览里，请重新核对。')
        chosen[skill['name']] = option
    def current():
        if operations.revision(home, managed) != (expected or plan['revision']) or preview_join(requests, home)['plan_id'] != plan_id:
            raise ValueError('目录内容已经变了，请重新预览。')
        return registry
    def mutate(current_registry, checked):
        local = profiles.state(home)
        custom = list(local.get('custom', []))
        builtin = {p['id'] for p in profiles.PROFILES + profiles.ADDITIONAL_PROFILES}
        for p in plan['profiles']:
            if p['id'] not in builtin | {q['id'] for q in custom}:
                custom.append(p)
        local.update(onboarded=local['onboarded'] + plan['agents'], custom=custom,
                     ignored=[a for a in local.get('ignored', []) if a not in plan['agents']])
        write_json(home / '.skill-pilot/agents.json', local, checked)
        changed, content = [], []
        for skill in plan['skills']:
            name, option = skill['name'], chosen[skill['name']]
            target = home / '.skill-pilot/store' / name
            if name not in current_registry['skills']:
                operations._put(home, current_registry, {'name': name, 'path': option['path'], 'source': {'source': '来源不明'}}, [], [], checked, normalize_content=False)
                content.append(name)
            elif option['id'] != 'store':
                remove(target, checked)
                shutil.copytree(option['path'], checked(target), symlinks=True)
                content.append(name)
            record = current_registry['skills'][name]
            record['assigned_agents'] = sorted(set(record['assigned_agents']) | set(skill['assigned_agents']))
            record['entries'] = sorted(set(record['entries']) | set(skill['entries']))
            for entry in skill['entries']:
                path = checked(home / entry)
                if path.is_symlink() and os.readlink(path) == str(target):
                    continue
                remove(path, checked)  # The complete original is in this operation's backup.
                checked(path.parent).mkdir(parents=True, exist_ok=True)
                path.symlink_to(str(target), target_is_directory=True)
            changed.append(name)
        reconcile_disabled(current_registry, home, checked)
        after = visibility(home)
        for agent, original in plan['before'].items():
            if after[agent]['available'] != original['available'] or after[agent]['automatic'] != original['automatic']:
                raise ValueError('接入后 agent 能看到的技能名单变了，已经恢复原样。')
            for name, versions in original['versions'].items():
                expected_versions = [content_digest(home / '.skill-pilot/store' / name)] if name in chosen and (agent in plan['agents'] or chosen[name]['id'] != 'store') else versions
                if after[agent]['versions'][name] != expected_versions:
                    raise ValueError('接入后的内容与所选版本不一致，已经恢复原样。')
        return changed, content, {'agents': plan['agents'], 'after': after, '_agents_changed': True}
    return operations._run(home, registry, managed, current, '接入了 ' + '、'.join(p['name'] for p in plan['profiles']), mutate, extra_paths=plan['roots'])


def preview_leave(agent, home=None):
    home, registry, managed = operations.load(home)
    if agent not in managed or len(managed) <= 1:
        raise ValueError('至少要保留一个已接入 agent。')
    remaining = [p for p in profiles.managed(home) if p['id'] != agent]
    leaving = profiles.by_id(home)[agent]
    before = visibility(home)
    result = copy.deepcopy(registry)
    removed, kept, changed, removed_skills = [], [], [], {}
    for name, skill in result['skills'].items():
        previous = copy.deepcopy(skill)
        skill['assigned_agents'] = [a for a in skill['assigned_agents'] if a != agent]
        entries = []
        for entry in skill['entries']:
            if not profiles.reads(leaving, entry):
                entries.append(entry)
            elif not any(profiles.reads(p, entry) for p in remaining):
                kept.append(entry)
            elif any(p['id'] in skill['assigned_agents'] and profiles.reads(p, entry) for p in remaining):
                entries.append(entry)
            else:
                removed.append(entry)
                removed_skills[entry] = name
        skill['entries'] = entries
        if skill != previous:
            changed.append(name)
    lost = sorted(set(removed_skills.values()) & set(before[agent]['available']))
    rev = operations.revision(home, managed)
    payload = [rev, agent, result, removed]
    return {'plan_id': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), 'revision': rev,
            'agent': agent, 'name': leaving['name'], 'remaining': [p['id'] for p in remaining], 'kept_entries': sorted(kept),
            'removed_shared_entries': sorted(removed), 'lost_skills': lost, 'changed': changed, 'before': before,
            'message': '不再管理 ' + leaving['name'] + ('；它以后看不到这些技能：' + '、'.join(lost) if lost else '；它文件夹里的技能原样留着，照常能用。'),
            '_registry': result, '_removed_skills': removed_skills}


def leave(agent, plan_id, expected=None, home=None):
    home, registry, managed = operations.load(home)
    plan = preview_leave(agent, home)
    if plan['plan_id'] != plan_id:
        raise ValueError('目录内容已经变了，请重新预览。')
    def mutate(current, checked):
        current.clear(); current.update(plan['_registry'])
        for entry in plan['removed_shared_entries']:
            path = checked(home / entry)
            target = home / '.skill-pilot/store' / plan['_removed_skills'][entry]
            if not path.is_symlink() or path.resolve() != target:
                raise ValueError('技能入口出现外部改动，请先核对。')
            path.unlink()
        local = profiles.state(home)
        local['onboarded'] = [a for a in local['onboarded'] if a != agent]
        local['ignored'] = list(dict.fromkeys(local.get('ignored', []) + [agent]))
        write_json(home / '.skill-pilot/agents.json', local, checked)
        reconcile_disabled(current, home, checked)
        after = visibility(home)
        if any(after[a] != plan['before'][a] for a in plan['remaining']):
            raise ValueError('其他 agent 的技能名单或内容变了，已经恢复原样。')
        return plan['changed'], [], {'agent': agent, 'lost_skills': plan['lost_skills'], '_agents_changed': True}
    return operations._run(home, registry, managed, expected or plan['revision'], plan['message'], mutate)


def preview_ignore(agent, home=None):
    home, _, managed = operations.load(home)
    suggestion = next((p for p in suggestions(home) if p['id'] == agent), None)
    if suggestion is None:
        raise ValueError('这个 agent 已不在建议里，请刷新。')
    profile = profiles.by_id(home).get(agent) or profiles.custom(agent, suggestion['name'], _root(suggestion['skill_roots'][0], home))
    rev = operations.revision(home, managed)
    return {'agent': agent, 'revision': rev, 'message': '忽略 ' + suggestion['name'] + ' 的接入建议。',
            'plan_id': hashlib.sha256(json.dumps([rev, suggestion], sort_keys=True).encode()).hexdigest(),
            '_profile': profile}


def ignore(agent, plan_id, expected=None, home=None):
    home, registry, managed = operations.load(home)
    plan = preview_ignore(agent, home)
    if plan['plan_id'] != plan_id:
        raise ValueError('目录内容已经变了，请重新预览。')
    def mutate(current, checked):
        local = profiles.state(home)
        if agent not in profiles.by_id(home):
            local.setdefault('custom', []).append(plan['_profile'])
        local['ignored'] = list(dict.fromkeys(local.get('ignored', []) + [agent]))
        write_json(home / '.skill-pilot/agents.json', local, checked)
        return [], [], {'agent': agent, '_agents_changed': True}
    return operations._run(home, registry, managed, expected or plan['revision'], plan['message'], mutate)


def public(plan):
    return {k: v for k, v in plan.items() if not k.startswith('_')}
