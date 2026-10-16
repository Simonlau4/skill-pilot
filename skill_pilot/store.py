"""Backed-up local migration and guarded, reversible writes."""
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import profiles
from .scan import SKIP_CONTENT, content_digest, scan_root
from .sync import reconcile, visibility

PROJECT = Path(__file__).resolve().parents[1]


def target_home():
    return Path(os.environ.get("SKILL_PILOT_HOME", Path.home())).resolve()


def write_allowed(home):
    """允许真实主目录和系统临时目录；显式只读时一律拒绝写入。"""
    home = Path(home).resolve()
    return os.environ.get("SKILL_PILOT_READ_ONLY") != "1" and (
        home == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() or
        home.is_relative_to(Path(tempfile.gettempdir()).resolve()))


def guard(path, home):
    home = Path(home).resolve()
    path = Path(path).absolute()
    if not write_allowed(home):
        raise ValueError("当前处于只读模式或目录不受管理，文件没有改动。")
    # 先换成真实路径再比较。macOS 临时目录的 /var 指向 /private/var，
    # 未展开时会和已经展开的主目录对不上，被误判成只读或越界。
    if not path.resolve().is_relative_to(home):
        raise ValueError("这个位置指向主目录之外，已停止操作，文件没有改动。")
    return path


def check_links(path, home):
    """Check even broken links, and do not walk through directory links."""
    if path.is_symlink():
        if not path.resolve().is_relative_to(home):
            raise ValueError("目录中的链接指向主目录之外，已停止操作。")
    elif path.is_dir():
        for child in path.iterdir():
            check_links(child, home)


def physical_state(path, include_times=False):
    """Fingerprint bytes, modes and link text without following links."""
    rows = []
    def visit(p, relative):
        info = p.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if include_times:
            rows.append((relative, "modified", info.st_mtime_ns))
        if p.is_symlink():
            rows.append((relative, "link", mode, os.readlink(p)))
        elif p.is_dir():
            rows.append((relative, "dir", mode))
            for child in sorted(p.iterdir()):
                visit(child, relative + "/" + child.name)
        else:
            rows.append((relative, "file", mode, hashlib.sha256(p.read_bytes()).hexdigest()))
    if path.exists() or path.is_symlink():
        visit(path, "")
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def git_object(kind, payload):
    return hashlib.sha1(kind.encode() + b" " + str(len(payload)).encode() + b"\0" + payload).digest()


def git_tree(path):
    """Compute git objects in memory; never create a repository in a source."""
    entries = []
    for child in path.iterdir():
        if child.name in SKIP_CONTENT:
            continue
        if child.is_symlink():
            mode, digest, suffix = "120000", git_object("blob", os.fsencode(os.readlink(child))), ""
        elif child.is_dir():
            mode, digest, suffix = "40000", bytes.fromhex(git_tree(child)), "/"
        else:
            mode = "100755" if child.stat().st_mode & 0o111 else "100644"
            digest, suffix = git_object("blob", child.read_bytes()), ""
        entries.append((os.fsencode(child.name + suffix), mode.encode() + b" " + os.fsencode(child.name) + b"\0" + digest))
    return git_object("tree", b"".join(value for _, value in sorted(entries))).hex()


def attribution():
    result = {}
    for line in (PROJECT / "docs/source-attribution.md").read_text().splitlines():
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) == 5 and cells[3] in {"高", "中", "低"}:
            name = re.search(r"`([^`]+)`", cells[0])[1]
            result[name] = {"source": cells[1], "source_path": cells[2].strip("`"),
                            "confidence": cells[3], "evidence": cells[4]}
    return result


def paired_lark(home):
    evidence = json.loads((PROJECT / "docs/lark-paired-snapshot.json").read_text())
    package = home / ".local/lib/node_modules/@larksuite/cli/package.json"
    fixture = home / ".skill-pilot-fixture.json"
    version = (json.loads(package.read_text()).get("version") if package.is_file() else
               json.loads(fixture.read_text()).get("lark_cli_version") if fixture.is_file() else None)
    lock = home / ".agents/.skill-lock.json"
    if not lock.is_file():
        return {}
    rows = [[lock.name, stat.S_IMODE(lock.stat().st_mode), "file", hashlib.sha256(lock.read_bytes()).hexdigest()]]
    fingerprint = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
    return evidence["skills"] if version == evidence["cli_version"] and fingerprint == evidence["lock_fingerprint"] else {}


def preview(home=None, agent_ids=None):
    home = target_home() if home is None else Path(home).resolve()
    if (home / '.skill-pilot/registry.json').is_file():
        from .agents import preview_join
        return preview_join(agent_ids or [], home)
    catalog = profiles.all_profiles(home)
    selected = sorted(p["id"] for p in profiles.PROFILES) if agent_ids is None else sorted(set(agent_ids))
    known = {p["id"] for p in catalog}
    if not selected or not set(selected) <= known:
        raise ValueError("请至少选择一个有接入档案的 agent。")
    profiles.require_shared(selected, catalog)
    before = visibility(home)
    lock_path = home / ".agents/.skill-lock.json"
    lock = json.loads(lock_path.read_text()).get("skills", {}) if lock_path.exists() else {}
    origins = attribution()
    lark = paired_lark(home)
    grouped, entries, skipped, state = defaultdict(list), defaultdict(list), [], {}
    roots = sorted({r for p in catalog if p["id"] in selected for r in p["skill_roots"]})
    for relative in roots:
        root = home / relative
        state[relative] = physical_state(root, include_times=True)
        inventory = scan_root(root, profiles.root_nested(relative, catalog), excluded=profiles.exclusions(root, home))
        if inventory["errors"]:
            raise ValueError("有技能目录没能完整读取，暂时不能整理。")
        for skill in inventory["skills"]:
            if not skill["valid"]:
                skipped.append({"path": skill["path"], "reason": skill.get("warning") or "技能不可用，保留原样"})
                continue
            name = skill["name"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                raise ValueError("有技能名称不能安全用作文件夹名称，请先核对。")
            grouped[name].append(skill)
            entries[name].append(str(Path(skill["path"]).relative_to(home)))
    from .extra_sources import paths as extra_source_paths
    for relative in extra_source_paths(home):
        folder = home / relative
        if not folder.is_dir():
            continue
        state[relative] = physical_state(folder)
        for skill in scan_root(folder)["skills"]:
            if skill["valid"] and skill["name"] in grouped:
                grouped[skill["name"]].append(skill)
    state['configs'] = {r: physical_state(home / r) for r in profiles.config_paths(home, selected)}
    state["lock"] = physical_state(lock_path)
    # Include data already managed by this tool and the source evidence itself.
    for relative in (".skill-pilot/store", ".skill-pilot/bases", ".skill-pilot/registry.json", ".skill-pilot/agents.json"):
        state[relative] = physical_state(home / relative)
    state["attribution"] = physical_state(PROJECT / "docs/source-attribution.md")
    state["lark_evidence"] = physical_state(PROJECT / "docs/lark-paired-snapshot.json")
    state["lark_package"] = physical_state(home / ".local/lib/node_modules/@larksuite/cli/package.json")
    state["fixture"] = physical_state(home / ".skill-pilot-fixture.json")
    skills = []
    for name, copies in sorted(grouped.items()):
        variants = defaultdict(list)
        for copy in copies:
            variants[copy["digest"]].append(copy)
            source = Path(copy["resolved_path"])
            state[str(source)] = {"physical": physical_state(source, include_times=True),
                                  "modified": max((p.lstat().st_mtime_ns for p in source.rglob("*")
                                                   if p.is_file() and not any(x in SKIP_CONTENT for x in p.relative_to(source).parts)), default=source.stat().st_mtime_ns)}
        record = lock.get(name)
        source = ({"source": record["sourceUrl"], "source_path": record.get("skillPath", "."),
                   "confidence": "高", "evidence": "现有安装来源记录", "record": record} if record
                  else origins.get(name, {"source": "来源不明", "source_path": "", "confidence": "低", "evidence": "没有来源记录"}))
        options = []
        for digest, items in sorted(variants.items()):
            matched = False
            reason = ""
            if record and record.get("skillFolderHash"):
                matched = any(git_tree(Path(c["resolved_path"])) == record["skillFolderHash"] for c in items)
                reason = "内容与安装来源记录一致" if matched else ""
            if record and record.get("sourceType") == "well-known" and name in lark:
                matched = digest == lark[name]["digest"] and items[0].get("version") == lark[name]["version"]
                reason = "与已核实配套的飞书工具版本一致，内容也与当时记录一致" if matched else reason
            # 工作台源码只是候选副本，可能比已安装副本旧（2026-09-16 曾直接改安装副本），按最近修改比较。
            newest = max(state[c["resolved_path"]]["modified"] for c in items)
            options.append({"id": digest, "paths": sorted(c["path"] for c in items),
                            "path": sorted(c["path"] for c in items)[0], "version": items[0].get("version"),
                            "source_match": matched, "match_reason": reason, "modified": newest})
        chosen = max(options, key=lambda o: (o["source_match"], o["modified"], o["id"]))
        reason = chosen["match_reason"] or ("内容相同，直接合并" if len(options) == 1 else "未能离线证明与来源记录一致，默认选最近修改的那份")
        skills.append({"name": name, "options": options, "default": chosen["id"], "reason": reason,
                       "source": source, "source_needs_confirmation": source["confidence"] != "高",
                       "entries": sorted(set(entries[name])),
                       "assigned_agents": [a for a in selected if name in before[a]["available"]],
                       "name_mismatch": any(c["name_mismatch"] for c in copies)})
    for p in catalog:
        for relative, _ in p["readonly_roots"]:
            if (home / relative).exists():
                skipped.append({"path": str(home / relative), "reason": "内置、插件或桌面同步目录，只查看"})
        if p["id"] not in selected:
            skipped.extend({"path": str(home / r), "reason": "未选中的 agent，保留原样"} for r in p["skill_roots"])
    for relative in profiles.readonly_roots(home) | {r for r, _ in profiles.CATALOG['classified_roots']}:
        if (home / relative).exists() and str(home / relative) not in {s['path'] for s in skipped}:
            skipped.append({"path": str(home / relative), "reason": "未接入或只读的目录，保留原样"})
    payload = {"home": str(home), "agents": selected, "state": state, "skills": skills}
    plan_id = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    after = {a: {"available": v["available"], "automatic": v["automatic"]} for a, v in before.items()}
    return {"plan_id": plan_id, "home": str(home), "agents": selected, "skills": skills,
            "conflicts": [s for s in skills if len(s["options"]) > 1], "before": before, "after": after,
            "roots": roots, "skipped": skipped, "read_only": True,
            "write_allowed": write_allowed(home), 'agent_catalog': catalog,
            'must_select_together': profiles.shared_groups(catalog)}


def remove(path, checked):
    checked(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        for base, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                checked(Path(base) / name)
        shutil.rmtree(path)


def write_json(path, value, checked):
    checked(path.parent).mkdir(parents=True, exist_ok=True)
    checked(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def backups(home=None):
    home = target_home() if home is None else Path(home).resolve()
    root = home / ".skill-pilot/backups"
    return [json.loads(p.read_text()) for p in sorted(root.glob("*/manifest.json"), reverse=True)]


def backup(home, plan, checked):
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    directory = checked(home / ".skill-pilot/backups" / backup_id)
    directory.mkdir(parents=True)
    paths = plan["roots"] + [".skill-pilot/" + n for n in ("store", "bases", "registry.json", "agents.json")]
    for relative in list(paths):
        real = (home / relative).resolve()
        if real != home / relative and real.is_dir():
            paths.append(str(real.relative_to(home)))
    paths = sorted(set(paths))
    protected_roots = profiles.readonly_roots(home)
    protected = sorted(protected_roots | {str((home / p).resolve().relative_to(home)) for p in protected_roots
                                          if (home / p).resolve().is_relative_to(home)})
    manifest = {"backup_id": backup_id, "home": str(home), "plan_id": plan["plan_id"], "paths": paths,
                "existing": [r for r in paths if (home / r).exists() or (home / r).is_symlink()],
                "before": plan["before"], "protected": protected}
    with tarfile.open(checked(directory / "files.tar"), "w", dereference=False) as archive:
        for relative in manifest["existing"]:
            def included(info):
                return None if any(info.name == p or info.name.startswith(p + "/") for p in protected) else info
            archive.add(home / relative, arcname=relative, filter=included)
    write_json(directory / "manifest.json", manifest, checked)
    return manifest


def _restore(home, backup_id, checked):
    if not re.fullmatch(r"[0-9T]+", backup_id):
        raise ValueError("没有找到这份备份。")
    directory = checked(home / ".skill-pilot/backups" / backup_id)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["home"] != str(home):
        raise ValueError("这份备份属于另一个目录，不能在这里还原。")
    protected = manifest.get("protected", sorted(profiles.readonly_roots(home)))
    with tarfile.open(directory / "files.tar") as archive:
        for relative in manifest["paths"]:
            checked(home / relative)
            if any(relative == p or relative.startswith(p + "/") for p in protected):
                raise ValueError("备份涉及只读目录，已停止还原。")
        for member in archive.getmembers():
            destination = checked(home / member.name)
            if (not destination.is_relative_to(home) or ".." in Path(member.name).parts or
                    Path(member.name).is_absolute() or
                    not any(member.name == p or member.name.startswith(p + "/") for p in manifest["paths"]) or
                    any(member.name == p or member.name.startswith(p + "/") for p in protected)):
                raise ValueError("备份里有不安全的位置，已经停止还原。")
            if member.issym():
                target = Path(member.linkname)
                checked(target if target.is_absolute() else destination.parent / target)
            if member.islnk() or member.isdev() or member.isfifo():
                raise ValueError("备份里有不支持的文件，已经停止还原。")
        for relative in manifest["paths"]:
            path = checked(home / relative)
            if path.is_dir() and not path.is_symlink() and relative in manifest["existing"]:
                for child in path.iterdir():
                    if str(child.relative_to(home)) not in protected:
                        remove(child, checked)
            else:
                remove(path, checked)
        # Archive links and destinations were checked above; data filter would
        # reject the intentionally absolute in-home links and change permissions.
        for member in archive.getmembers():
            checked(home / member.name)
            archive.extract(member, home, filter="fully_trusted")
        # Restore directory attributes after their children have been written.
        for member in reversed(archive.getmembers()):
            if member.isdir():
                path = checked(home / member.name)
                os.chmod(path, member.mode)
                os.utime(path, (member.mtime, member.mtime))
    return {"backup_id": backup_id, "restored": True, "message": "已还原到整理前。"}


def transaction(home):
    """The operation lock is outside home, so rejected writes create no state."""
    identity = hashlib.sha256(str(home).encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"skill-pilot-write-{os.getuid()}-{identity}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "r+")


def restore(backup_id, home=None):
    home = target_home() if home is None else Path(home).resolve()
    checked = lambda p: guard(p, home)
    checked(home / ".skill-pilot")
    with transaction(home) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _restore(home, backup_id, checked)


def apply(plan_id, home=None, agent_ids=None, choices=None, sources=None):
    home = target_home() if home is None else Path(home).resolve()
    if (home / '.skill-pilot/registry.json').is_file():
        from .agents import join
        return join(agent_ids or [], plan_id, home=home, choices=choices)
    checked = lambda p: guard(p, home)
    checked(home / ".skill-pilot")
    with transaction(home) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plan = preview(home, agent_ids)
        if plan["plan_id"] != plan_id:
            raise ValueError("目录内容已经变了，请重新预览并核对后再整理。")
        choices, sources = choices or {}, sources or {}
        if not isinstance(choices, dict) or not isinstance(sources, dict):
            raise ValueError("核对结果的格式不对，请重新打开核对页。")
        if not set(choices) <= {s["name"] for s in plan["skills"]} or not set(sources) <= {s["name"] for s in plan["skills"]}:
            raise ValueError("核对页包含未知技能，请重新预览。")
        chosen = {}
        for skill in plan["skills"]:
            option_id = choices.get(skill["name"], skill["default"])
            option = next((o for o in skill["options"] if o["id"] == option_id), None)
            if option is None:
                raise ValueError("所选版本不在本次预览中，请重新核对。")
            chosen[skill["name"]] = option
            check_links(Path(option["path"]).resolve(), home)
        for relative in plan["roots"]:
            checked(home / relative)
            check_links(home / relative, home)
        snapshot = backup(home, plan, checked)
        try:
            registry = {"schema_version": 1, "skills": {}}
            for skill in plan["skills"]:
                name, option = skill["name"], chosen[skill["name"]]
                destination = checked(home / ".skill-pilot/store" / name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(option["path"], destination, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_CONTENT))
                from . import normalization
                processing = normalization.apply(destination)
                check_links(destination, home)
                source = skill["source"]
                if skill["source_needs_confirmation"]:
                    confirmed = sources.get(name)
                    source = ({**source, "confirmed": True} if confirmed is True else
                              {"source": confirmed, "confidence": "用户确认", "confirmed": True} if isinstance(confirmed, str) and confirmed.strip() else
                              {"source": "来源不明", "confidence": "未确认", "confirmed": False})
                base = checked(home / ".skill-pilot/bases" / name)
                base.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(destination, base, symlinks=True)
                registry["skills"][name] = {"source": source, "base_digest": content_digest(destination),
                                            "requires": [], "assigned_agents": skill["assigned_agents"],
                                            "entries": skill["entries"], "installed_at": source.get("record", {}).get("installedAt", datetime.now(timezone.utc).isoformat()),
                                            "normalization": processing}
            write_json(home / ".skill-pilot/registry.json", registry, checked)
            write_json(home / ".skill-pilot/agents.json", {**profiles.state(home), 'onboarded': plan['agents']}, checked)
            changed = reconcile(registry, home, checked)
            store = checked(home / ".skill-pilot/store")
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            for arguments in (["init", "-q"], ["add", "."],
                              ["-c", "user.name=Skill Pilot", "-c", "user.email=local@skill-pilot.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "首次收进统一仓库"]):
                subprocess.run(["git", "-C", str(store), *arguments], env=env, check=True, capture_output=True)
            after = visibility(home)
            for agent, original in plan["before"].items():
                if any(after[agent][k] != original[k] for k in ("available", "automatic")):
                    raise ValueError("整理后有 agent 能看到的技能名单变了")
                for name in original["available"]:
                    expected = ([registry['skills'][name]['base_digest']] if name in chosen and agent in plan["agents"]
                                else original["versions"][name])
                    if after[agent]["versions"][name] != expected:
                        raise ValueError("整理后的内容与核对页选择不一致")
            return {"backup_id": snapshot["backup_id"], "message": "整理完成，各 agent 的技能名单已核对一致。",
                    "changed_entries": len(changed), "after": after,
                    "normalization": {n: s['normalization'] for n, s in registry['skills'].items()}}
        except Exception as error:
            try:
                _restore(home, snapshot["backup_id"], checked)
            except Exception as rollback:
                raise ValueError(f"整理没能完成，自动还原也未完成；请用备份 {snapshot['backup_id']} 还原。") from rollback
            raise ValueError("整理没能完成，已经自动恢复原样。") from error
