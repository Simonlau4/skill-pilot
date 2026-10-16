#!/usr/bin/env python3
"""Repeatable acceptance on an existing fake home; no real sessions or network."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from skill_pilot.extra_sources import paths as extra_source_paths
from skill_pilot.store import apply, check_links, guard, physical_state, preview
from skill_pilot.sync import reconcile, visibility


def verify(home):
    home = Path(home).resolve()
    check_links(home, home)
    env = {**os.environ, "HOME": str(home), "SKILL_PILOT_HOME": str(home),
           "CODEX_HOME": str(home / ".codex"), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".state")}
    roots = [".codex/skills", ".agents/skills", ".claude/skills", ".gemini/config/skills", ".dsh/skills",
             ".codex/config.toml", ".agents/.skill-lock.json", *extra_source_paths(home),
             "Developer/vendor/stop-that-shit-local/skills/stss", ".skill-pilot-fixture.json"]
    def fingerprints():
        return {r: physical_state(home / r) for r in roots}
    def cli(*arguments):
        process = subprocess.run([sys.executable, "-m", "skill_pilot", *arguments, "--json"],
                                 cwd=PROJECT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
        return json.loads(process.stdout)
    def codex_names():
        process = subprocess.run(["/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                                  "debug", "prompt-input", "probe"], cwd=home, env=env,
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45, check=True)
        messages = json.loads(process.stdout)
        prompt = "\n".join(c.get("text", "") for m in messages for c in m.get("content", []) if isinstance(c, dict))
        skills = re.findall(r"^- ([^:\n]+): .*?\(file: ([^)]+)\)$", prompt, re.M)
        names = sorted({name for name, path in skills if not path.startswith("r2/") and "/.system/" not in path})
        assert names == visibility(home)["codex"]["automatic"], "Codex prompt and scanner differ"
        return names
    baseline = fingerprints()
    names_before = codex_names()
    assert baseline == fingerprints(), "Codex debug changed protected paths"
    before = visibility(home)
    results, contents = [], []
    for index in range(2):
        plan = cli("migrate", "--plan")
        result = cli("migrate", "--apply", "--plan-id", plan["plan_id"])
        assert codex_names() == names_before
        registry = json.loads((home / ".skill-pilot/registry.json").read_text())
        contents.append({n: s["base_digest"] for n, s in registry["skills"].items()})
        checked = lambda p: guard(p, home)
        assert reconcile(registry, home, checked) == []
        store = home / ".skill-pilot/store"
        commits = subprocess.check_output(["git", "-C", str(store), "rev-list", "--count", "HEAD"], env=env, text=True).strip()
        assert commits == "1"
        assert not subprocess.check_output(["git", "-C", str(store), "status", "--porcelain"], env=env)
        assert not subprocess.check_output(["git", "-C", str(store), "remote"], env=env)
        assert any(b["backup_id"] == result["backup_id"] for b in cli("backups"))
        restored = cli("restore", result["backup_id"])
        assert restored["restored"]
        assert baseline == fingerprints(), "Restored fingerprints differ"
        assert not (home / ".skill-pilot/store").exists()
        results.append({"round": index + 1, "backup_id": result["backup_id"],
                        "changed_entries": result["changed_entries"], "codex_names_unchanged": True,
                        "fingerprints_restored": True, "reconciliation_idempotent": True,
                        "store_commit_count": int(commits), "no_remotes": True})
    assert contents[0] == contents[1], {n: (contents[0][n], contents[1][n]) for n in contents[0] if contents[0][n] != contents[1][n]}
    plan = preview(home)
    def fail_after_entries(*args):
        reconcile(*args)
        raise PermissionError("验收故障：入口替换后，后续目标不可写")
    with patch("skill_pilot.store.reconcile", side_effect=fail_after_entries):
        try:
            apply(plan["plan_id"], home)
        except ValueError as error:
            failure = str(error)
        else:
            raise AssertionError("Injected failure did not fail")
    assert failure == "整理没能完成，已经自动恢复原样。"
    assert baseline == fingerprints(), "Failure did not restore protected paths"
    assert not (home / ".skill-pilot/registry.json").exists()
    return {"home": str(home), "baseline": baseline, "rounds": results, "same_contents_on_second_migration": True,
            "before": before, "codex_user_names": names_before, "codex_user_count": len(names_before),
            "failure": {"injected": "入口已替换后的写入权限失败", "message": failure, "fingerprints_restored": True},
            "final_restored": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("home")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = verify(args.home)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"两轮迁移与还原通过，Codex {result['codex_user_count']} 个技能一致；故障自动还原通过。")
