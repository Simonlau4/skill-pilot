"""Assistant behavior comes from the catalog and backward-compatible local data."""
import importlib
import json
import os
import pwd
import re
import shutil
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name('catalog.json').read_text())
PROFILES = CATALOG['profiles']
ADDITIONAL_PROFILES = CATALOG['additional_profiles']


def evidence(profile, home, applications=None):
    home = Path(home).resolve()
    real_user = home == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if applications is None:
        applications = ([Path('/Applications')] if real_user else []) + [home / 'Applications']
    detect = profile['detect']
    found = [str(home / r) for r in detect['config_dirs'] if (home / r).is_dir()]
    found += [str(Path(base) / app) for base in applications for app in detect['apps']
              if (Path(base) / app).is_dir()]
    cli = shutil.which(detect['cli']) if real_user and detect['cli'] else None
    if cli:
        found.append(cli)
    return found


def state(home):
    path = Path(home) / '.skill-pilot/agents.json'
    return (json.loads(path.read_text()) if path.is_file() else
            {'onboarded': [], 'ignored': list(CATALOG['default_ignored'])})


def validate(profile):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', profile.get('id', '')) or not profile.get('name', '').strip():
        raise ValueError('请填写 agent 的名字和有效编号。')
    roots = profile.get('skill_roots', [])
    if not roots or profile.get('link_root') not in roots:
        raise ValueError('agent 放置技能的目录必须是它读取的目录之一。')
    paths = roots + [r for r, _ in profile.get('readonly_roots', [])]
    paths += profile.get('detect', {}).get('config_dirs', [])
    if any(not r or Path(r).is_absolute() or '..' in Path(r).parts for r in paths):
        raise ValueError('agent 目录必须位于主目录内。')
    protected = [r for p in PROFILES + ADDITIONAL_PROFILES for r, _ in p['readonly_roots']]
    protected += [r for r, category in CATALOG['classified_roots'] if category != 'ignored']
    if any(Path(r) == Path('.') or Path(r).is_relative_to('.skill-pilot') or
           any(Path(r).is_relative_to(p) for p in protected) for r in roots):
        raise ValueError('agent 技能目录不能是统一仓库或软件自己管理的目录。')
    if type(profile.get('nested')) is not bool or profile.get('activation') not in {'immediate', 'new_session'}:
        raise ValueError('agent 的生效方式没有读懂，请核对档案。')
    adapters = {p['disable'] for p in PROFILES}
    if profile.get('disable') not in adapters:
        raise ValueError('agent 档案里的读取方式没有接入。')
    if not set(profile.get('manual_only', [])) <= {'disable-model-invocation', 'agents/openai.yaml'}:
        raise ValueError('agent 档案里的手动调用标记没有读懂。')
    return profile


def all_profiles(home=None, applications=None):
    local = state(home) if home is not None else {}
    custom = local.get('custom', [])
    # A previously enrolled agent keeps its saved roots and behavior.
    saved = set(local.get('onboarded', [])) | set(local.get('ignored', []))
    profiles = list(PROFILES) + [p for p in ADDITIONAL_PROFILES
                                if p['id'] not in {c['id'] for c in custom} and
                                (home is None or p['id'] in saved or evidence(p, home, applications))]
    for profile in custom:
        validate(profile)
        if profile['id'] in {p['id'] for p in profiles}:
            raise ValueError('agent 编号重复，请核对档案。')
        # Older custom profiles may contain fields that have since been retired.
        profiles.append({k: v for k, v in profile.items() if k in PROFILES[0]})
    return profiles


def by_id(home=None):
    return {p['id']: p for p in all_profiles(home)}


def managed(home, agent_ids=None):
    if agent_ids is None:
        path = Path(home) / '.skill-pilot/agents.json'
        agent_ids = state(home)['onboarded'] if path.is_file() else [p['id'] for p in PROFILES]
    profiles = by_id(home)
    if not set(agent_ids) <= profiles.keys():
        raise ValueError('有 agent 的接入档案没有读懂，请先核对。')
    builtin = [p for p in PROFILES if p['id'] in agent_ids]
    return builtin + [profiles[a] for a in agent_ids if a not in {p['id'] for p in PROFILES}]


def custom(identifier, name, root):
    return validate({'id': identifier, 'name': name.strip(), 'detect': {'config_dirs': [str(Path(root).parent)], 'apps': [], 'cli': None},
                     'skill_roots': [root], 'link_root': root, 'nested': False, 'readonly_roots': [],
                     'activation': 'new_session', 'activation_note': '新会话生效', 'manual_only': [],
                     'disable': None, 'disable_note': '没有独立停用设置',
                     'visibility_note': '按技能目录直属条目盘点。'})


def reads(profile, entry):
    return any(Path(entry).is_relative_to(root) and Path(entry) != Path(root) and
               (profile['nested'] or Path(entry).parent == Path(root)) for root in profile['skill_roots'])


def readonly_roots(home=None):
    return {r for p in all_profiles(home) for r, _ in p['readonly_roots']} | {
        r for r, category in CATALOG['classified_roots'] if category != 'ignored'}


def exclusions(root, home=None):
    root = Path(root)
    result = []
    for p in all_profiles(home):
        for user in p['skill_roots']:
            if root.parts[-len(Path(user).parts):] != Path(user).parts:
                continue
            result += [root / Path(r).relative_to(user) for r, _ in p['readonly_roots']
                       if Path(r).is_relative_to(user)]
    return result


def root_nested(root, profiles):
    return any(p['nested'] for p in profiles if root in p['skill_roots'])


def adapter(profile):
    return importlib.import_module('skill_pilot.' + profile['disable']) if profile['disable'] else None


def config_paths(home, agent_ids=None):
    return sorted({adapter(p).CONFIG_PATH for p in managed(home, agent_ids) if p['disable']})


def disabled(home):
    return {p['id']: adapter(p).read(home) for p in all_profiles(home) if p['disable']}


def manual_only(skill, profile):
    return bool(set(skill.get('manual_markers', [])) & set(profile['manual_only']))


def shared_groups(profiles, home=None):
    if home is not None:
        onboarded = state(home)['onboarded']
        profiles = [p for p in profiles if p['id'] in onboarded or evidence(p, home)]
    groups = []
    for root in sorted({r for p in profiles for r in p['skill_roots']}):
        ids = [p['id'] for p in profiles if root in p['skill_roots']]
        if len(ids) > 1 and ids not in groups:
            groups.append(ids)
    return groups


def require_shared(selected, profiles, already=(), home=None):
    if home is not None:
        profiles = [p for p in profiles if p['id'] in set(selected) | set(already) or evidence(p, home)]
    for group in shared_groups(profiles):
        if set(group) & set(selected) and not set(group) <= set(selected) | set(already):
            names = ' 和 '.join(p['name'] for p in profiles if p['id'] in group)
            raise ValueError(names + ' 共用一个技能目录，请一起选择并接入。')


def activation_text(profile):
    return '马上生效' if profile['activation'] == 'immediate' else '新会话生效'
