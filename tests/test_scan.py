from tests import set_up_home
import json
import tempfile
import unittest
from pathlib import Path

from skill_pilot.scan import classify, conflict_groups, is_disabled, metadata, read_disabled, scan, scan_root, tokens


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill-pilot-tests-")
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name))

    def skill(self, relative, name="lark-base", body="正文", description="处理表格"):
        path = self.home / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n")
        return path

    def test_real_path_shapes_are_classified(self):
        fixtures = {
            ".codex/skills": "user",
            ".agents/skills": "user",
            ".claude/skills": "user",
            ".gemini/config/skills": "user",
            ".dsh/skills": "user",
            ".codex/skills/.system": "builtin",
            ".codex/plugins/cache/openai-bundled/pdf/1.0/skills": "plugin_cache",
            ".claude/plugins/marketplaces/official/plugins/example/skills": "plugin_cache",
            ".gemini/antigravity/builtin/skills": "builtin",
            ".gemini/config/plugins/chrome/skills": "plugin_cache",
            "Library/Application Support/kimi-desktop/daimon-share/daimon/skills": "ignored",
            ".workbuddy/skills-marketplace/skills": "plugin_cache",
            ".workbuddy/connectors-marketplace/connectors": "plugin_cache",
            ".cursor/skills-cursor": "builtin",
            ".trae/builtin/code/default/skills": "builtin",
            ".local/backups/2026-10-10/.codex/skills": "backup_temp",
            ".codex/.tmp/old/skills": "backup_temp",
            ".codex/skills/example/references/nested": "reference",
            ".new-agent/skills": "unknown_agent",
        }
        for relative, expected in fixtures.items():
            with self.subTest(relative=relative):
                self.skill(relative + "/sample")
                self.assertEqual(classify(self.home / relative, self.home), expected)

    def test_links_broken_links_and_nested_skills(self):
        root = self.home / ".codex/skills"
        target = self.skill("outside", "shared")
        root.mkdir(parents=True)
        (root / "alias").symlink_to(target, target_is_directory=True)
        (root / "doc").symlink_to(self.home / "missing", target_is_directory=True)
        self.skill(".codex/skills/community/nested", "nested")
        self.skill(".codex/skills/.system/builtin", "builtin")
        self.skill(".codex/skills/example", "example")
        self.skill(".codex/skills/example/references/hidden", "hidden")
        inventory = scan_root(root)
        self.assertEqual(inventory["entry_count"], 4)
        self.assertEqual(inventory["broken_count"], 1)
        shared = next(s for s in inventory["skills"] if s["name"] == "shared")
        self.assertEqual(shared["kind"], "link")
        self.assertEqual(shared["resolved_path"], str(target.resolve()))
        self.assertTrue(shared["name_mismatch"])

    def test_conflicts_use_name_and_all_content_not_directory_name(self):
        one = self.skill(".agents/skills/lark-base", body="新版")
        self.skill(".gemini/config/skills/base-copy", body="旧版")
        alias = self.home / ".claude/skills/lark-base"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(one, target_is_directory=True)
        skills = []
        for root in [".agents/skills", ".gemini/config/skills", ".claude/skills"]:
            skills += scan_root(self.home / root)["skills"]
        conflicts = conflict_groups(skills)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["name"], "lark-base")
        self.assertEqual(sorted(len(v["copies"]) for v in conflicts[0]["versions"]), [1, 2])
        # Equal instructions with different supporting resources also conflict.
        (one / "data.txt").write_text("changed")
        self.assertNotEqual(scan_root(one)["skills"][0]["digest"], skills[0]["digest"])

    def test_disabled_inline_names_paths_and_enabled_entries(self):
        root = self.home / ".codex/skills"
        self.skill(".codex/skills/name-off", "name-off")
        path_off = self.skill(".codex/skills/path-off", "path-off")
        self.skill(".codex/skills/on", "on")
        config = self.home / ".codex/config.toml"
        config.write_text('[skills]\nconfig = [{ name = "name-off", enabled = false }, '
                          '{ name = "on", enabled = true }, '
                          '{ name = "plugin:foo", enabled = false }, '
                          '{ path = "~/'+path_off.relative_to(self.home).as_posix()+'/SKILL.md", enabled = false }]\n')
        before = config.read_bytes()
        disabled = read_disabled(config, self.home)
        skills = {s["name"]: s for s in scan_root(root)["skills"]}
        self.assertTrue(is_disabled(skills["name-off"], disabled))
        self.assertTrue(is_disabled(skills["path-off"], disabled))
        self.assertFalse(is_disabled(skills["on"], disabled))
        self.assertTrue(is_disabled({**skills["on"], "qualified_name": "plugin:foo"}, disabled))
        result = scan(self.home, [])
        codex = next(a for a in result["agents"] if a["id"] == "codex")
        self.assertEqual(codex["visible_names"], ["on"])
        self.assertEqual(codex["visible_skill_count"], 1)
        self.assertEqual(config.read_bytes(), before)

    def test_disabled_path_follows_directory_alias(self):
        target = self.skill(".codex/skills/target", "shared")
        alias = self.home / ".agents/skills/shared"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target)
        config = self.home / ".codex/config.toml"
        config.write_text('[skills]\nconfig = [{path='+json.dumps(str(target))+', enabled=false}]')
        disabled = read_disabled(config, self.home)
        self.assertTrue(is_disabled(scan_root(alias.parent)["skills"][0], disabled))

    def test_same_name_is_counted_once_and_dsh_does_not_recurse(self):
        self.skill(".codex/skills/first", "shared")
        self.skill(".agents/skills/second", "shared")
        self.skill(".agents/skills/community/nested", "nested")
        (self.home / ".dsh").mkdir()
        data = scan(self.home, [])
        agents = {a["id"]: a for a in data["agents"]}
        self.assertEqual(agents["codex"]["visible_names"], ["nested", "shared"])
        self.assertEqual(agents["dsh"]["visible_names"], ["shared"])

    def test_discovery_does_not_follow_skill_references_or_projects(self):
        self.skill(".new-agent/skills/example", "example")
        self.skill(".new-agent/skills/example/references/other", "other")
        self.skill("project/.claude/skills/project", "project")
        self.skill(".local/backups/old/skills/backup", "backup")
        data = scan(self.home, [])
        self.assertEqual([c["path"] for c in data["candidates"]], [str(self.home / ".new-agent/skills")])

    def test_descriptions_support_folded_literal_and_quotes(self):
        for description, expected in [('>\n  first\n  second', 'first second'),
                                      ('|-\n  第一行\n  第二行', '第一行\n第二行'),
                                      ('"quoted \\"word\\""', 'quoted "word"'),
                                      ("'it''s fine'", "it's fine")]:
            with self.subTest(description=description):
                text = f"---\nname: example\ndescription: {description}\n---\n"
                self.assertEqual(metadata(text)["description"], expected)

    def test_plugin_skill_named_share_is_not_confused_with_runtime_directory(self):
        root = self.home / ".codex/plugins/cache"
        self.skill(".codex/plugins/cache/product-design/0.1/skills/share", "share")
        (root / "old-version").symlink_to(self.home / "missing-version")
        data = scan_root(root, user_root=False)
        self.assertEqual(data["manifest_count"], 1)
        self.assertEqual(data["entry_count"], 1)
        self.assertEqual(data["skills"][0]["name"], "share")
        self.assertEqual(len(data["unresolved_links"]), 1)

    def test_invalid_config_is_unavailable_not_zero_disabled(self):
        self.skill(".codex/skills/example", "example")
        (self.home / ".codex/config.toml").write_text("[skills\n")
        data = scan(self.home, [])
        self.assertIsNotNone(data['disabled']['codex']["error"])
        self.assertIsNone(data["agents"][0]["visible_skill_count"])

    def test_manual_only_skills_do_not_inflate_model_description_count(self):
        path = self.skill(".codex/skills/grill-me", "grill-me")
        manifest = path / "SKILL.md"
        manifest.write_text(manifest.read_text().replace("description: 处理表格", "description: 处理表格\ndisable-model-invocation: true"))
        self.skill(".codex/skills/grilling", "grilling")
        codex = scan(self.home, [])["agents"][0]
        self.assertEqual(codex["visible_skill_count"], 1)
        self.assertEqual(codex["available_user_skill_count"], 2)
        self.assertEqual(codex["manual_only_names"], ["grill-me"])
        self.assertEqual(codex["description_chars"], len("处理表格"))
        self.assertEqual(codex["description_tokens"], tokens("grilling 处理表格"))

    def test_token_estimate_counts_wide_characters_one_each_and_others_quarter(self):
        self.assertEqual([tokens(t) for t in ("", "a", "ab", "abcdefgh", "处理表格", "处理，table", "Ｆｕｌｌ！")],
                         [0, 0, 1, 2, 4, 4, 5])
        self.assertEqual(tokens("grilling 处理表格"), 6)

    def test_codex_openai_policy_is_separate_from_dsh_invocation(self):
        path = self.skill(".agents/skills/critical-gate", "critical-gate")
        (path / "agents").mkdir()
        (path / "agents/openai.yaml").write_text('interface:\n  display_name: "Critical Gate"\npolicy:\n  allow_implicit_invocation: false\n')
        (self.home / ".codex").mkdir()
        (self.home / ".dsh").mkdir()
        agents = {a["id"]: a for a in scan(self.home, [])["agents"]}
        self.assertEqual(agents["codex"]["manual_only_names"], ["critical-gate"])
        self.assertEqual(agents["codex"]["visible_skill_count"], 0)
        self.assertEqual(agents["dsh"]["visible_skill_count"], 1)


if __name__ == "__main__":
    unittest.main()
