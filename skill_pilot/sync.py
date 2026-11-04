from .platform_support import readlink
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
            entries.add((Path(root) / name).as_posix())
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
        if path.is_symlink() and readlink(path) == target:
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


def external_disable_rules(registry, home):
    """User-owned disables are preferences, not evidence that an install failed."""
    result = {}
    for profile in profiles.managed(home):
        if profile['disable']:
            adapter = profiles.adapter(profile)
            rules = adapter.read_external(home, registry)
            if rules.get('error'):
                raise ValueError(profile['name'] + ' 的停用设置没能读懂，请先修复后再预览。')
            result[profile['id']] = rules
    return result


def externally_disabled(name, skill, agent, home, rules, planned=False):
    from .scan import is_disabled
    if agent not in rules:
        return False
    profile = profiles.by_id(home)[agent]
    entries = list(skill.get('entries', []))
    if planned:
        entries += assignment_entries(name, sorted(set(skill['assigned_agents']) | {agent}), home)
    paths = [Path(home) / '.skill-pilot/store' / name]
    paths += [Path(home) / e for e in entries if profiles.reads(profile, e)]
    return any(is_disabled({'name': name, 'path': str(path)}, rules[agent]) for path in paths)


def effective_assignments(registry, home, rules=None):
    rules = external_disable_rules(registry, home) if rules is None else rules
    return {name: [agent for agent in skill['assigned_agents']
                   if not externally_disabled(name, skill, agent, home, rules)]
            for name, skill in registry['skills'].items()}


def _follow_directory(path):
    """Resolve a directory. A link loop raises RuntimeError on Python 3.11+."""
    return Path(path).resolve()


def _reads_resolved(profile, entry, home):
    """Match readers by where the skill directory actually points.

    The skill entry itself is often a link to the shared copy. Follow the
    directory above it, so a fallback directory that only points at a shared
    folder is not treated as a separate place. A circular link cannot be
    followed; compare the written path instead of failing the request.
    """
    if home is None:
        return profiles.reads(profile, entry)
    entry = Path(entry)
    try:
        actual = _follow_directory(Path(home) / entry.parent) / entry.name
    except (OSError, RuntimeError):
        return profiles.reads(profile, entry)
    for root in profile['skill_roots']:
        try:
            relative = actual.relative_to(_follow_directory(Path(home) / root))
        except (OSError, RuntimeError, ValueError):
            continue
        if relative.parts and (profile['nested'] or len(relative.parts) == 1):
            return True
    return False


def assignment_entries(name, agents, home=None, catalog=None):
    """Cover recipients through safe roots, preferring their default placement."""
    catalog = catalog if catalog is not None else (profiles.managed(home) if home is not None else profiles.PROFILES)
    assigned = set(agents)
    if not assigned <= {p['id'] for p in catalog}:
        raise ValueError('请选择已接入的 agent。')
    recipients = [p for p in catalog if p['id'] in assigned]
    roots = list(dict.fromkeys([p['link_root'] for p in recipients] +
                              [r for p in recipients for r in p['skill_roots']]))
    readers = {r: {p['id'] for p in catalog if _reads_resolved(p, Path(r) / name, home)} for r in roots}
    blocked = {p['id'] for p in catalog if p['id'] not in assigned and not p['disable']}
    safe = [r for r in roots if not readers[r] & blocked]
    missing = assigned - set().union(*(readers[r] for r in safe))
    if missing:
        root = next(r for r in roots if readers[r] & missing)
        others = [p for p in catalog if p['id'] in readers[root] & blocked]
        names = ' 和 '.join(p['name'] for p in recipients if p['id'] in readers[root])
        blocked_names = '、'.join(p['name'] for p in others)
        raise ValueError(names + ' 和 ' + blocked_names + ' 共用一个技能目录，没法只给 ' + names + '。')
    uncovered, chosen = set(assigned), []
    while uncovered:
        root = max(safe, key=lambda r: len(readers[r] & uncovered))
        chosen.append((Path(root) / name).as_posix())
        uncovered -= readers[root]
    return sorted(chosen)


def disabled_lists(registry, home, agent_ids=None):
    catalog = profiles.managed(home, agent_ids)
    external = external_disable_rules(registry, home)
    return {p['id']: sorted(name for name, skill in registry['skills'].items()
                           if p['id'] not in skill['assigned_agents'] and
                           not externally_disabled(name, skill, p['id'], home, external) and
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
    from .scan import scan_root
    from .store import check_links
    external = external_disable_rules(registry, home)
    catalog = profiles.managed(home)
    entries = {name: set(registry['skills'][name].get('entries', [])) for name in changed_names}
    cache = {}
    # External installers can add aliases without updating the registry.
    for root in sorted({r for p in catalog for r in p['skill_roots']}):
        inventory = scan_root(home / root, profiles.root_nested(root, catalog), cache,
                              excluded=profiles.exclusions(home / root, home))
        if inventory['errors']:
            raise ValueError('有技能目录没能完整读取，请解决后重新预览。')
        for item in inventory['skills']:
            name = item['name']
            if (name in entries and item['valid'] and item['kind'] == 'link' and
                    Path(item['resolved_path']) == (home / '.skill-pilot/store' / name).resolve()):
                entries[name].add(Path(item['path']).relative_to(home).as_posix())
    for name in sorted(changed_names):
        skill = registry['skills'][name]
        target = home / '.skill-pilot/store' / name
        check_links(target, home)
        desired = (entries_override[name] if entries_override and name in entries_override else
                   assignment_entries(name, skill['assigned_agents'], home))
        if not entries_override or name not in entries_override:
            watched = {Path(path) for rules in external.values() for path in rules.get('entry_paths', [])}
            # Keep a user-disabled alias when adding another reader. Removing it
            # would make the old path rule stop applying to the shared copy.
            protected = [entry for entry in entries[name]
                         if home / entry in watched or home / entry / 'SKILL.md' in watched]
            desired = sorted(set(desired) | set(protected))
        # Validate the entire chain before rewriting or removing any entry.
        for relative in sorted(entries[name] | set(desired)):
            path = checked(home / relative)
            if path.exists() or path.is_symlink():
                if not path.is_symlink() or path.resolve() != target.resolve():
                    raise ValueError('技能入口出现了外部改动，请先收编或刷新后再应用。')
        # Kept aliases must point at the store before their intermediate links go.
        for relative in desired:
            path = checked(home / relative)
            if path.is_symlink():
                if readlink(path) == str(target):
                    continue
                path.unlink()
            checked(path.parent).mkdir(parents=True, exist_ok=True)
            path.symlink_to(str(target), target_is_directory=True)
        kept = {(home / e).parent.resolve() / Path(e).name for e in desired}
        for relative in sorted(entries[name] - set(desired)):
            path = checked(home / relative)
            if path.is_symlink() and path.parent.resolve() / path.name not in kept:
                path.unlink()
        skill['entries'] = desired
    reconcile_disabled(registry, home, checked)
