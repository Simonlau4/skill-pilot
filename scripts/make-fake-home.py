#!/usr/bin/env python3
"""Copy only skills and their in-home link dependencies; never copy auth."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from skill_pilot.extra_sources import paths as extra_source_paths
from skill_pilot.store import check_links, guard
from skill_pilot import profiles


def make_fake_home(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    guard(destination, destination)
    if destination == source or destination.is_relative_to(source) and not destination.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("模拟目录必须放在系统临时目录，不能放在真实主目录里。")
    if not destination.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("请把模拟目录放在系统临时目录。")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("目标目录已有文件，请换一个空目录。")
    destination.mkdir(parents=True, exist_ok=True)
    catalog = profiles.all_profiles(source)
    skill_roots = {r for p in catalog for r in p['skill_roots']}
    skill_roots.update(str(p.relative_to(source)) for p in source.glob('.*/skills') if p.is_dir() and
                       not any(p.is_relative_to(source / r) for r in profiles.readonly_roots(source)))
    roots = ['.skill-pilot', *sorted(skill_roots),
             'Developer/vendor/stop-that-shit-local/skills/stss', *extra_source_paths(source), '.agents/.skill-lock.json']
    copied, dependencies = set(), []
    def copy(relative):
        src, dst = source / relative, destination / relative
        if relative == '.skill-pilot/settings.json' or Path(relative).parent == Path('.skill-pilot') and Path(relative).name.startswith('usage.sqlite'):
            copied.add(relative)
            return
        if relative in copied or dst.exists() or dst.is_symlink():
            return
        copied.add(relative)
        if not src.exists() and not src.is_symlink():
            return
        if not src.resolve().is_relative_to(source):
            raise ValueError("有来源链接指向真实主目录之外，已停止复制。")
        guard(dst.parent, destination).mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            guard(dst, destination).symlink_to(os.readlink(src), target_is_directory=src.is_dir())
        elif src.is_dir():
            shutil.copytree(src, guard(dst, destination), symlinks=True,
                            ignore=(lambda directory, names: [n for n in names if n in {"backups", "settings.json"} or n.startswith('usage.sqlite')]
                                    if Path(directory) == source / ".skill-pilot" else []))
        else:
            shutil.copy2(src, guard(dst, destination))
    for relative in roots:
        copy(relative)
    # Resolve links using source paths, not the currently unrewritten copy.
    while True:
        pending = []
        for base, dirs, files in os.walk(destination, followlinks=False):
            for name in dirs + files:
                dst = Path(base) / name
                if not dst.is_symlink():
                    continue
                src = source / dst.relative_to(destination)
                resolved = src.resolve()
                if not resolved.is_relative_to(source):
                    raise ValueError("有来源链接指向真实主目录之外，已停止复制。")
                relative = str(resolved.relative_to(source))
                if resolved.exists() and not (destination / relative).exists() and relative not in copied:
                    pending.append(relative)
                text = os.readlink(dst)
                if os.path.isabs(text) and Path(text).is_relative_to(source):
                    # Unlinking an untrusted link does not follow its target.
                    guard(dst.parent, destination)
                    dst.unlink()
                    dst.symlink_to(destination / Path(text).relative_to(source), target_is_directory=src.is_dir())
        if not pending:
            break
        for relative in sorted(set(pending)):
            dependencies.append(relative)
            copy(relative)
    for profile in catalog:
        if profile['disable']:
            profiles.adapter(profile).copy_skills_config(source, destination, lambda p: guard(p, destination))
        # Simulate installation evidence without copying private sessions.
        for relative in profile['detect']['config_dirs']:
            if (source / relative).is_dir():
                guard(destination / relative, destination).mkdir(parents=True, exist_ok=True)
    package = source / ".local/lib/node_modules/@larksuite/cli/package.json"
    fixture = {"lark_cli_version": json.loads(package.read_text()).get("version") if package.is_file() else None}
    guard(destination / ".skill-pilot-fixture.json", destination).write_text(json.dumps(fixture) + "\n")
    check_links(destination, destination)
    return {"home": str(destination), "copied_roots": roots, "link_dependencies": dependencies,
            "links_within_home": True, "config_contains_only_skills": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读复制技能，建立安全的模拟目录")
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--source-home", default=str(Path.home()))
    args = parser.parse_args()
    destination = args.destination or tempfile.mkdtemp(prefix="skill-pilot-home-")
    try:
        print(json.dumps(make_fake_home(args.source_home, destination), ensure_ascii=False, indent=2))
    except (ValueError, OSError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(str(error))
