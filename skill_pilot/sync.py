"""M2 reconciliation keeps existing entry paths, including compatibility aliases."""
import os
from pathlib import Path

from . import profiles


def desired_links(registry, home):
    home = Path(home)
    links = {}
    catalog = profiles.by_id(home)
    for name, skill in registry["skills"].items():
        assigned = set(skill["assigned_agents"])
        entries = set(skill.get("entries", []))
        # Keep every original entry for hard-coded paths and existing disable
        # settings. For missing coverage, derive placement from assignments.
        for agent in sorted(assigned):
            profile = catalog[agent]
            covered = any(profiles.reads(profile, entry) for entry in entries)
            if covered:
                continue
            # First migration retains the original private-root preference;
            # explicit later assignments use the profile's link_root.
            readers = {r: {p['id'] for p in catalog.values() if r in p['skill_roots']}
                       for r in profile['skill_roots']}
            root = max(profile['skill_roots'], key=lambda r: (-len(readers[r] - assigned), len(readers[r] & assigned)))
            entries.add(str(Path(root) / name))
        for relative in entries:
            links[str(home / relative)] = str(home / ".skill-pilot/store" / name)
    return links


def reconcile(registry, home, guard):
    """No new assignments in M2: preserve roots, nesting and disabled entries."""
    from .store import remove
    changed = []
    for source, target in sorted(desired_links(registry, home).items()):
        path = guard(Path(source))
        guard(Path(target))
        if path.is_symlink() and os.readlink(path) == target:
            continue
        remove(path, guard)
        guard(path.parent).mkdir(parents=True, exist_ok=True)
        guard(path).symlink_to(target, target_is_directory=True)
        changed.append(source)
    return changed


def visibility(home, catalog=None):
    """User skills only; no discovery, plugin execution or session startup."""
    from .scan import is_disabled, scan_root
    home = Path(home)
    catalog = catalog if catalog is not None else profiles.all_profiles(home)
    disabled = {p['id']: profiles.adapter(p).read(home) for p in catalog if p['disable']}
    for p in catalog:
        if disabled.get(p['id'], {}).get('error'):
            raise ValueError(p['name'] + ' 的停用设置没能读懂，请先修复后再预览。')
    result = {}
    cache = {}
    for profile in catalog:
        available, automatic, versions = {}, {}, {}
        for relative in profile["skill_roots"]:
            root = home / relative
            inventory = scan_root(root, profile['nested'], cache, excluded=profiles.exclusions(root, home))
            if inventory["errors"]:
                raise ValueError("有技能目录没能完整读取，请解决后重新预览。")
            for skill in inventory["skills"]:
                if not skill["valid"] or (profile['disable'] and is_disabled(skill, disabled[profile['id']])):
                    continue
                available.setdefault(skill["name"], skill)
                versions.setdefault(skill["name"], set()).add(skill["digest"])
                if profiles.manual_only(skill, profile):
                    continue
                automatic.setdefault(skill["name"], skill)
        result[profile["id"]] = {"available": sorted(available), "automatic": sorted(automatic),
                                  "versions": {n: sorted(v) for n, v in sorted(versions.items())}}
    return result


def assignment_entries(name, agents, home=None, catalog=None):
    """Cover recipients through safe roots, preferring their default placement."""
    catalog = catalog if catalog is not None else (profiles.managed(home) if home is not None else profiles.PROFILES)
    assigned = set(agents)
    if not assigned <= {p['id'] for p in catalog}:
        raise ValueError('请选择已接入的 agent。')
    recipients = [p for p in catalog if p['id'] in assigned]
    roots = list(dict.fromkeys([p['link_root'] for p in recipients] +
                              [r for p in recipients for r in p['skill_roots']]))
    readers = {r: {p['id'] for p in catalog if profiles.reads(p, Path(r) / name)} for r in roots}
    blocked = {p['id'] for p in catalog if p['id'] not in assigned and not p['disable']}
    safe = [r for r in roots if not readers[r] & blocked]
    missing = assigned - set().union(*(readers[r] for r in safe))
    if missing:
        root = next(r for r in roots if readers[r] & missing)
        other = next(p for p in catalog if p['id'] in readers[root] & blocked)
        names = ' 和 '.join(p['name'] for p in recipients if p['id'] in readers[root])
        raise ValueError(names + ' 和 ' + other['name'] + ' 共用一个技能目录，没法只给 ' + names + '。')
    uncovered, chosen = set(assigned), []
    while uncovered:
        root = max(safe, key=lambda r: len(readers[r] & uncovered))
        chosen.append(str(Path(root) / name))
        uncovered -= readers[root]
    return sorted(chosen)


def disabled_lists(registry, home, agent_ids=None):
    catalog = profiles.managed(home, agent_ids)
    return {p['id']: sorted(name for name, skill in registry['skills'].items()
                           if p['id'] not in skill['assigned_agents'] and
                           any(profiles.reads(p, entry) and
                               any(q['id'] in skill['assigned_agents'] and profiles.reads(q, entry) for q in catalog)
                               for entry in skill.get('entries', [])))
            for p in catalog if p['disable']}


def reconcile_disabled(registry, home, checked):
    desired = disabled_lists(registry, home)
    for p in profiles.managed(home):
        if p['disable']:
            profiles.adapter(p).write(home, registry, desired[p['id']], checked)


def cell_notes(registry, home, agent_ids=None):
    catalog = profiles.managed(home, agent_ids)
    result = {}
    for name, skill in registry['skills'].items():
        assigned = set(skill['assigned_agents'])
        entries = assignment_entries(name, assigned, home, catalog)
        result[name] = {p['id']: p['name'] + ' 里已自动停用' for p in catalog
                        if p['disable'] and p['id'] not in assigned and
                        any(profiles.reads(p, entry) for entry in entries)}
    return result


def notes(home, agent_ids=None):
    catalog = profiles.managed(home, agent_ids)
    result = []
    for group in profiles.shared_groups(catalog):
        readers = [p for p in catalog if p['id'] in group]
        names = ' 和 '.join(p['name'] for p in readers)
        for p in readers:
            recipients = [q for q in readers if q != p]
            others = '、'.join(q['name'] for q in recipients)
            try:
                entries = assignment_entries('example', [q['id'] for q in recipients], home, catalog)
            except ValueError:
                result.append(names + ' 共用一个技能目录：只给 ' + others + ' 的技能，需要同时分配给 ' + p['name'] + '。')
            else:
                if p['disable'] and any(profiles.reads(p, entry) for entry in entries):
                    result.append(names + ' 共用一个技能目录：只给 ' + others + ' 的技能，会在 ' + p['name'] + ' 里自动停用。')
    return result


def reconcile_assignments(registry, home, checked, changed_names, entries_override=None):
    from .store import check_links
    for name in sorted(changed_names):
        skill = registry['skills'][name]
        target = home / '.skill-pilot/store' / name
        check_links(target, home)
        desired = (entries_override[name] if entries_override and name in entries_override else
                   assignment_entries(name, skill['assigned_agents'], home))
        for relative in sorted(set(skill.get('entries', [])) | set(desired)):
            path = checked(home / relative)
            if path.exists() or path.is_symlink():
                if not path.is_symlink() or path.resolve() != target.resolve():
                    raise ValueError('技能入口出现了外部改动，请先收编或刷新后再应用。')
                if relative not in desired:
                    path.unlink()
            elif relative in desired:
                checked(path.parent).mkdir(parents=True, exist_ok=True)
                path.symlink_to(str(target), target_is_directory=True)
        skill['entries'] = desired
    reconcile_disabled(registry, home, checked)
