"""Read-only discovery. Files are never imported or executed."""
import hashlib
import json
import os
import re
import tomllib
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import profiles
from .extra_sources import paths as extra_source_paths

CATEGORIES = {
    "user": "用户技能", "builtin": "软件内置", "plugin": "插件技能",
    "plugin_cache": "插件或市场缓存", "vendor": "下载的候选库",
    "unknown_agent": "疑似新 agent", "ignored": "已忽略",
    "backup_temp": "备份或临时目录", "reference": "技能附带的参考资料",
}
SKIP_CONTENT = {".git", "__pycache__", ".DS_Store", "node_modules"}
SKIP_DISCOVERY = SKIP_CONTENT | {
    "references", "scripts", "assets", "Cache", "cache", "Caches", "CachedData",
    "GPUCache", "DawnGraphiteCache", "logs", "sessions", "archived_sessions",
    "projects", "conversations", "extensions", "Frameworks", "venv", ".venv",
    "site-packages", "browser-profiles", "private-env", "backups", ".tmp", "scratch",
    "share", "lib", "bin", ".npm", ".cache", ".Trash", ".local",
    ".skill-pilot",
}


def metadata(text):
    """Parse the manifest's scalar fields without a third-party YAML dependency.

    Supports plain/quoted values and YAML folded/literal descriptions. This is
    deliberately not a general YAML parser; uncommon syntax gets a warning.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}
    lines = text.splitlines()[1:]
    result = {}
    for i, line in enumerate(lines):
        if line in ("---", "..."):
            break
        match = re.match(r"^(name|description|disable-model-invocation):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        tail = []
        for following in lines[i + 1:]:
            if following and not following[0].isspace():
                break
            tail.append(following.strip())
        if value in (">", ">-", ">+", "|", "|-", "|+"):
            value = ("\n" if value.startswith("|") else " ").join(tail).strip()
        elif value.startswith(('"', "'")):
            quote = value[0]
            if len(value) == 1 or not value.endswith(quote):
                value = " ".join([value, *tail])
            if value.endswith(quote) and len(value) > 1:
                if quote == '"':
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        value = value[1:-1]
                else:
                    value = value[1:-1].replace("''", "'")
            else:
                continue
        else:
            value = value.split(" #", 1)[0].strip()
        result[key] = value
    version = re.search(r"(?m)^\s*version:\s*['\"]?([^\s'\"]+)", "\n".join(lines).split("\n---", 1)[0])
    if version:
        result["version"] = version[1]
    result["model_invocable"] = result.get("disable-model-invocation", "false").lower() not in {"true", "yes", "on", "1"}
    return result


def tokens(text):
    """粗估 Token：中日韩和全角字符各算 1，其余字符每 4 个算 1，四舍五入。"""
    wide = sum(unicodedata.east_asian_width(c) in "FW" for c in text)
    return int(wide + (len(text) - wide) / 4 + .5)


def classify(path, home):
    parts = Path(path).relative_to(home).parts
    if "references" in parts:
        return "reference"
    if "memories" in parts:
        return "reference"
    if any(p in parts for p in ("backups", ".tmp", "scratch", ".Trash", "migration")):
        return "backup_temp"
    if ".system" in parts or "builtin" in parts:
        return "builtin"
    # More specific catalog roots take priority over a containing user root.
    classified = [(r, c) for p in profiles.all_profiles(home) for r, c in p['readonly_roots']]
    classified += profiles.CATALOG['classified_roots']
    for relative, category in sorted(classified, key=lambda item: len(item[0]), reverse=True):
        if Path(path).is_relative_to(home / relative):
            return 'plugin_cache' if category == 'plugin' and 'plugins' in parts else category
    if any(p in parts for p in ("plugins", "skills-marketplace", "marketplaces", "connectors-marketplace", "skills-plugin")):
        return "plugin_cache"
    if "vendor_imports" in parts:
        return "vendor"
    for profile in profiles.all_profiles(home):
        if any(Path(path).is_relative_to(home / r) for r in profile["skill_roots"]):
            return "user"
    return "unknown_agent"


def content_digest(directory):
    digest = hashlib.sha256()

    def visit(path, relative, ancestors):
        real = path.resolve()
        if real in ancestors:
            raise OSError("目录中的链接形成循环")
        if path.is_dir():
            digest.update(("D\0" + relative + "\0").encode())
            for child in sorted(path.iterdir()):
                if child.name not in SKIP_CONTENT:
                    visit(child, relative + "/" + child.name, ancestors | {real})
        elif path.is_file():
            digest.update(("F\0" + relative + "\0").encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        elif path.is_symlink():
            digest.update(("L\0" + relative + "\0" + os.readlink(path)).encode())

    visit(directory, "", set())
    return digest.hexdigest()


def read_disabled(config_path, home, text=None):
    if text is None and not config_path.exists():
        return {"names": [], "paths": [], "error": None}
    try:
        config = tomllib.loads(config_path.read_text() if text is None else text)
    except (OSError, tomllib.TOMLDecodeError) as error:
        return {"names": [], "paths": [], "error": str(error)}
    names, paths = set(), set()
    for entry in config.get("skills", {}).get("config", []):
        if entry.get("enabled") is not False:
            continue
        if "name" in entry:
            names.add(entry["name"])
        if "path" in entry:
            value = entry["path"]
            path = home / value[2:] if value.startswith("~/") else Path(value)
            if not path.is_absolute():
                path = config_path.parent / path
            paths.add(str(path.resolve()))
    return {"names": sorted(names), "paths": sorted(paths), "error": None}


def is_disabled(skill, disabled):
    return (skill["name"] in disabled["names"] or
            skill.get("qualified_name", skill["name"]) in disabled["names"] or
            str(Path(skill["path"]).resolve()) in disabled["paths"] or
            str((Path(skill["path"]) / "SKILL.md").resolve()) in disabled["paths"])


def scan_root(root, recursive=True, digest_cache=None, user_root=True, excluded=None):
    root = Path(root)
    skills, errors, unresolved = [], [], []
    digest_cache = {} if digest_cache is None else digest_cache
    excluded = profiles.exclusions(root) if excluded is None and user_root else excluded or []

    def visit(path, ancestors):
        try:
            if any(path.is_relative_to(p) for p in excluded):
                return
            real = path.resolve()
            if path.is_symlink() and not path.exists():
                if not user_root and path.parent.name != "skills":
                    unresolved.append({"path": str(path), "link_target": os.readlink(path),
                                       "message": "目录链接已失效，无法确定其中有哪些技能"})
                    return
                skills.append({"name": path.name, "directory_name": path.name,
                               "path": str(path), "kind": "link", "link_target": os.readlink(path),
                               "resolved_path": str(real), "broken": True, "valid": False,
                               "description": "", "description_chars": 0, "description_tokens": 0, "digest": None,
                               "manual_only_for": [],
                               "name_mismatch": False, "warning": "链接指向的位置已不存在"})
                return
            if not path.is_dir() or real in ancestors:
                return
            manifest = path / "SKILL.md"
            if manifest.is_file():
                info = metadata(manifest.read_text(encoding="utf-8-sig"))
                warning = None
                valid = bool(info.get("name") and info.get("description"))
                if not valid:
                    warning = "技能说明缺少可识别的名称或描述，未计入可见数量"
                if real not in digest_cache:
                    try:
                        digest_cache[real] = content_digest(path)
                    except OSError as error:
                        digest_cache[real] = None
                        errors.append({"path": str(path), "message": str(error)})
                name = info.get("name", path.name)
                markers = [] if info.get('model_invocable', True) else ['disable-model-invocation']
                policy_file = path / "agents/openai.yaml"
                if policy_file.is_file():
                    policy = re.search(r"(?m)^policy:[ \t]*\n((?:[ \t]+[^\n]*\n?)*)", policy_file.read_text())
                    if policy and re.search(r"(?m)^[ \t]+allow_implicit_invocation:[ \t]*(?:false|no|off|0)\b", policy[1], re.I):
                        markers.append('agents/openai.yaml')
                skills.append({"name": name, "directory_name": path.name, "path": str(path),
                               "kind": "link" if path.is_symlink() else "directory",
                               "link_target": os.readlink(path) if path.is_symlink() else None,
                               "resolved_path": str(real), "broken": False, "valid": valid,
                               "description": info.get("description", ""),
                               "model_invocable": info.get("model_invocable", True),
                               "manual_markers": markers,
                               "manual_only_for": [],
                               "description_chars": len(info.get("description", "")),
                               "description_tokens": tokens(name + " " + info.get("description", "")),
                               "version": info.get("version"), "digest": digest_cache[real],
                               "name_mismatch": name != path.name, "warning": warning})
                return
            if recursive or path == root:
                for child in sorted(path.iterdir()):
                    if child.name not in SKIP_CONTENT | {"references", "scripts", "assets"} and (not child.name.startswith(".") or child.name == ".curated"):
                        visit(child, ancestors | {real})
        except (OSError, RuntimeError, UnicodeError) as error:
            errors.append({"path": str(path), "message": str(error)})

    if root.exists() or root.is_symlink():
        visit(root, set())
    return {"path": str(root), "exists": root.exists(), "entry_count": len(skills),
            "valid_count": sum(s["valid"] for s in skills),
            "manifest_count": sum(not s["broken"] for s in skills),
            "broken_count": sum(s["broken"] for s in skills), "skills": skills, "errors": errors,
            "unresolved_links": unresolved}


def conflict_groups(skills):
    grouped = defaultdict(lambda: defaultdict(list))
    for skill in skills:
        if skill["valid"] and skill["digest"]:
            grouped[skill["name"]][skill["digest"]].append(skill)
    return [{"name": name, "versions": [{"digest": digest, "copies": copies}
             for digest, copies in versions.items()]} for name, versions in sorted(grouped.items())
            if len(versions) > 1]


def discover(home, known_roots, errors, linked_roots=None):
    candidates = set()

    def visit(path, depth):
        if path in known_roots or depth > 7 or path.is_symlink():
            return
        try:
            children = list(path.iterdir())
            # Identify a skill directory even when every entry refers to an
            # existing skill; do not recurse through those links. Keep these
            # new assistant findings separate from the existing other finds.
            if linked_roots is not None and path.name == 'skills' and any(
                    c.is_symlink() and c.resolve().is_relative_to(home) and (c / 'SKILL.md').is_file()
                    for c in children):
                linked_roots.add(path)
            if any(c.name == "SKILL.md" and c.is_file() for c in children):
                candidates.add(path.parent)
                return
            for child in children:
                if child.is_dir() and child.name not in SKIP_DISCOVERY:
                    visit(child, depth + 1)
        except (OSError, RuntimeError) as error:
            errors.append({"path": str(path), "message": str(error)})

    for path in sorted(home.iterdir()):
        if path.name.startswith(".") and path.is_dir() and path.name not in SKIP_DISCOVERY:
            visit(path, 0)
    support = home / "Library/Application Support"
    if support.exists():
        for path in sorted(support.iterdir()):
            if path.is_dir():
                visit(path, 0)
    # A plugin can contain both a top-level manifest and nested skills. Its
    # inventory already includes descendants; do not count them again.
    return [p for p in sorted(candidates) if not any(parent in candidates for parent in p.parents)]


def scan(home=None, applications=None):
    home = Path(os.environ.get("SKILL_PILOT_HOME", Path.home())) if home is None else Path(home)
    catalog = profiles.all_profiles(home, applications)
    disabled = profiles.disabled(home)
    roots, agents, all_user_skills, errors = {}, [], [], []
    cache = {}
    for profile in catalog:
        detect = profile['detect']
        evidence = profiles.evidence(profile, home, applications)
        if not evidence:
            continue
        ids, readonly, visible, available, manual_only, excluded = [], [], {}, {}, set(), []
        for relative in profile["skill_roots"]:
            path = home / relative
            key = str(path)
            if key not in roots:
                roots[key] = scan_root(path, profiles.root_nested(relative, catalog), cache,
                                       excluded=profiles.exclusions(path, home))
                roots[key].update(category="user", readers=[])
            roots[key]["readers"].append(profile["id"])
            ids.append(key)
            for skill in roots[key]["skills"]:
                if not skill["valid"]:
                    continue
                if not profile['nested'] and Path(skill["path"]).parent != path:
                    continue
                if profile['disable'] and is_disabled(skill, disabled[profile['id']]):
                    excluded.append(skill["path"])
                else:
                    available.setdefault(skill["name"], skill)
                    if profiles.manual_only(skill, profile):
                        manual_only.add(skill["name"])
                        continue
                    visible.setdefault(skill["name"], skill)
        for relative, category in profile["readonly_roots"]:
            key = str(home / relative)
            if key not in roots:
                roots[key] = scan_root(home / relative, True, cache, user_root=False)
                roots[key].update(category=category, readers=[profile["id"]])
            readonly.append(key)
        agents.append({**profile, **detect, 'disable': profile['disable_note'],
                       'activation': profile['activation_note'], 'activation_text': profiles.activation_text(profile),
                       "status": "profile_available", "evidence": evidence,
                       "skill_roots": ids, "readonly_roots": readonly,
                       "visible_skill_count": len(visible),
                       "available_user_skill_count": len(available),
                       "manual_only_names": sorted(manual_only - visible.keys()),
                       "description_chars": sum(s["description_chars"] for s in visible.values()),
                       "description_tokens": sum(s["description_tokens"] for s in visible.values()),
                       "visible_names": sorted(visible), "disabled_paths": excluded})
        if profile['disable'] and disabled[profile['id']]["error"]:
            agents[-1].update(visible_skill_count=None, description_chars=None, description_tokens=None,
                             visibility_note="配置文件未能读取，可见数量暂时无法确认。")
            errors.append({"path": str(home / profiles.adapter(profile).CONFIG_PATH), "message": disabled[profile['id']]["error"]})
    for root in roots.values():
        for skill in root['skills']:
            skill['manual_only_for'] = [p['id'] for p in catalog if p['id'] in root['readers'] and
                                       (root['category'] != 'user' or profiles.reads(p, str(Path(skill['path']).relative_to(home)))) and
                                       profiles.manual_only(skill, p)]
        errors.extend(root["errors"])
        if root["category"] == "user":
            all_user_skills.extend({**s, "root": root["path"], "agents": root["readers"]} for s in root["skills"])
    source_copies = []
    for relative in extra_source_paths(home):
        folder = home / relative
        if folder.is_dir():
            source_copies.extend(scan_root(folder, True, cache)["skills"])
    candidates = []
    linked_roots = set()
    for path in discover(home, {Path(p) for p in roots}, errors, linked_roots):
        category = classify(path, home)
        if category == "user":
            continue
        inventory = scan_root(path, True, cache, user_root=category not in {"plugin_cache", "plugin", "vendor"})
        candidates.append({**inventory, "category": category,
                           "default_hidden": category in {"backup_temp", "reference"}})
    for prefix, name in profiles.CATALOG['discovery_names'].items():
        if name.lower() in {a['id'] for a in agents}:
            continue
        matched = [c["path"] for c in candidates if prefix in c["path"]]
        if matched:
            agents.append({"id": name.lower(), "name": name, "status": 'ignored' if name.lower() in profiles.state(home).get('ignored', []) else 'discovered', "evidence": matched,
                           "skill_roots": [], "readonly_roots": [], "visible_skill_count": None,
                           "description_chars": None, "description_tokens": None, "visibility_note": "已发现，尚未接入；目录文件数不等于实际可见数。"})
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "home": str(home), "read_only": True, "agents": agents, "roots": list(roots.values()),
            "candidates": candidates, "conflicts": conflict_groups(all_user_skills + source_copies),
            'discovered_skill_roots': [str(p) for p in sorted(linked_roots) if classify(p, home) == 'unknown_agent'],
            "source_copies": source_copies, "disabled": disabled, "errors": errors,
            "count_basis": "按名称去重的全局自动可用技能，扣除断链、无效说明、agent 停用项和仅限手动调用的条目；内置和插件另列。",
            "discovery_note": "只查用户主目录下的隐藏目录和应用数据，最多七层；跳过缓存、依赖、参考资料、备份与临时目录，不查项目技能。"}


def suggestions(home, inventory=None):
    home = Path(home)
    inventory = scan(home) if inventory is None else inventory
    local = profiles.state(home)
    unavailable = set(local.get('onboarded', [])) | set(local.get('ignored', []))
    catalog = profiles.all_profiles(home)
    found = []
    for p in catalog:
        detected = any(a['id'] == p['id'] and a['status'] == 'profile_available' for a in inventory['agents'])
        if (p['id'] not in unavailable and (detected or any((home / r).is_dir() for r in p['skill_roots']))
                and all(not (home / r).exists() or (home / r).is_dir() for r in p['skill_roots'])):
            found.append({'id': p['id'], 'name': p['name'], 'skill_roots': p['skill_roots'],
                          'skill_count': sum(scan_root(home / r, p['nested'], excluded=profiles.exclusions(home / r, home))['valid_count'] for r in p['skill_roots']),
                          'reason': '已识别到这个 agent，可以接入；还没有用户技能也能使用。'})
    known = {r for p in catalog for r in p['skill_roots']}
    candidates = list(inventory['candidates'])
    for path in inventory.get('discovered_skill_roots', []):
        if path not in {c['path'] for c in candidates}:
            candidates.append({'path': path, 'category': classify(Path(path), home)})
    for item in candidates:
        path = Path(item['path'])
        relative = path.relative_to(home)
        if (item['category'] != 'unknown_agent' or str(relative) in known or path.name != 'skills' or
                not relative.parts[0].startswith('.') or len(relative.parts) > 4):
            continue
        identifier = re.sub(r'[^a-z0-9_-]', '-', relative.parts[0].lstrip('.').lower())
        if identifier in unavailable or identifier in {p['id'] for p in catalog}:
            continue
        count = scan_root(path, False)['valid_count']
        if count:
            found.append({'id': identifier, 'name': profiles.CATALOG['discovery_names'].get(relative.parts[0], relative.parts[0].lstrip('.').capitalize()),
                          'skill_roots': [str(relative)], 'skill_count': count,
                          'reason': '这个目录直属放着技能文件夹，可能属于一个新 agent。'})
    return found
