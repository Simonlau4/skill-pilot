from tests import set_up_home
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import store
from skill_pilot.scan import scan, scan_root
from skill_pilot.sync import desired_links, reconcile, visibility


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill-pilot-m2-tests-")
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name))
        for name in (".agents", ".codex", ".claude", ".gemini/config", ".dsh"):
            (self.home / name).mkdir(parents=True)

    def skill(self, relative, name="sample", body="新版"):
        path = self.home / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 示例技能\n---\n{body}\n")
        return path

    def fixture(self):
        first = self.skill(".codex/skills/sample")
        shared = self.home / ".agents/skills"
        shared.mkdir()
        (shared / "sample").symlink_to(first)
        (shared / "doc").symlink_to(self.home / "missing")
        claude = self.home / ".claude/skills"
        claude.mkdir()
        (claude / "sample").symlink_to("../../.codex/skills/sample")
        self.skill(".gemini/config/skills/sample", body="旧版")
        self.skill(".codex/skills/community/nested", "nested")
        self.skill(".claude/skills/synced/account/builtin", "builtin")
        self.skill(".codex/skills/.system/inner", "inner")
        self.skill(".agents/skills/off", "off")
        (self.home / ".codex/config.toml").write_text('[skills]\nconfig=[{name="off",enabled=false}]\n')
        (self.home / ".agents/.skill-lock.json").write_text('{"version":3,"skills":{}}')

    def fingerprint(self):
        return {r: store.physical_state(self.home / r) for r in
                (".codex/skills", ".agents/skills", ".claude/skills", ".gemini/config/skills",
                 ".dsh/skills", ".codex/config.toml", ".agents/.skill-lock.json")}

    def test_source_match_beats_newest_and_equal_content_merges(self):
        official = self.skill(".agents/skills/sample", body="official")
        other = self.skill(".gemini/config/skills/sample", body="recent local edit")
        os.utime(official / "SKILL.md", (1, 1))
        record = {"sourceType": "github", "sourceUrl": "https://example.invalid/repo", "skillFolderHash": store.git_tree(official)}
        (self.home / ".agents/.skill-lock.json").write_text(json.dumps({"skills":{"sample":record}}))
        plan = store.preview(self.home)
        skill = plan["skills"][0]
        chosen = next(o for o in skill["options"] if o["id"] == skill["default"])
        self.assertTrue(chosen["source_match"])
        self.assertEqual(chosen["path"], str(official))
        (other / "SKILL.md").write_bytes((official / "SKILL.md").read_bytes())
        self.assertEqual(len(store.preview(self.home)["conflicts"]), 0)

    def test_newest_is_fallback(self):
        old = self.skill(".agents/skills/sample", body="old")
        new = self.skill(".codex/skills/sample", body="new")
        os.utime(old / "SKILL.md", (1, 1))
        os.utime(new / "SKILL.md", (2000000000, 2000000000))
        skill = store.preview(self.home)["skills"][0]
        chosen = next(o for o in skill["options"] if o["id"] == skill["default"])
        self.assertEqual(chosen["path"], str(new))

    def test_roundtrip_twice_preserves_all_roots_and_skipped_entries(self):
        self.fixture()
        before, visible = self.fingerprint(), visibility(self.home)
        results = []
        for _ in range(2):
            plan = store.preview(self.home)
            result = store.apply(plan["plan_id"], self.home)
            registry = json.loads((self.home / ".skill-pilot/registry.json").read_text())
            self.assertNotIn("off", registry["skills"]["off"]["assigned_agents"])
            self.assertNotIn("codex", registry["skills"]["off"]["assigned_agents"])
            self.assertNotIn("builtin", registry["skills"])
            self.assertNotIn("inner", registry["skills"])
            self.assertEqual(visibility(self.home)["codex"]["available"], visible["codex"]["available"])
            self.assertEqual(visibility(self.home)["dsh"]["available"], ["off", "sample"])
            self.assertEqual((self.home / ".codex/skills/community/nested").readlink(), self.home / ".skill-pilot/store/nested")
            self.assertEqual(os.readlink(self.home / ".agents/skills/doc"), str(self.home / "missing"))
            checked = lambda p: store.guard(p, self.home)
            self.assertEqual(reconcile(registry, self.home, checked), [])
            results.append({k: v["base_digest"] for k, v in registry["skills"].items()})
            store.restore(result["backup_id"], self.home)
            self.assertEqual(self.fingerprint(), before)
            self.assertFalse((self.home / ".skill-pilot/store").exists())
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(store.backups(self.home)), 2)

    def test_stale_preview_rejects_before_creating_state(self):
        path = self.skill(".codex/skills/sample")
        plan = store.preview(self.home)
        (path / "new.txt").write_text("changed")
        with self.assertRaisesRegex(ValueError, "重新预览"):
            store.apply(plan["plan_id"], self.home)
        self.assertFalse((self.home / ".skill-pilot").exists())
        plan = store.preview(self.home)
        os.utime(path / "SKILL.md", (1, 1))
        with self.assertRaisesRegex(ValueError, "重新预览"):
            store.apply(plan["plan_id"], self.home)

    def test_failure_after_link_replacement_rolls_everything_back(self):
        self.fixture()
        before = self.fingerprint()
        original = reconcile
        def fail(*args):
            original(*args)
            raise PermissionError("injected after all entries changed")
        with patch("skill_pilot.store.reconcile", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "已经自动恢复原样"):
                store.apply(store.preview(self.home)["plan_id"], self.home)
        self.assertEqual(before, self.fingerprint())
        self.assertFalse((self.home / ".skill-pilot/registry.json").exists())

    def test_invalid_choice_rejected_before_backup(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, "所选版本"):
            store.apply(store.preview(self.home)["plan_id"], self.home, choices={"sample":"invalid"})
        self.assertFalse((self.home / ".skill-pilot").exists())

    def test_path_disabled_stays_disabled_after_canonicalization(self):
        path = self.skill(".codex/skills/sample")
        config = self.home / ".codex/config.toml"
        config.write_text('[skills]\nconfig=[{path='+json.dumps(str(path / 'SKILL.md'))+',enabled=false}]')
        data = config.read_bytes()
        result = store.apply(store.preview(self.home)["plan_id"], self.home)
        self.assertEqual(visibility(self.home)["codex"]["available"], [])
        self.assertEqual(config.read_bytes(), data)
        store.restore(result["backup_id"], self.home)

    def test_real_home_default_writable_and_explicit_readonly(self):
        import pwd
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with patch.object(store.os.environ, "get", return_value="1"):
            self.assertFalse(store.write_allowed(real))
            with self.assertRaises(ValueError):
                store.guard(real / ".skill-pilot", real)
        # 只验证权限判定，不向真实目录写入任何文件。
        with patch.object(store.os.environ, "get", return_value=""):
            self.assertTrue(store.write_allowed(real))
            self.assertEqual(store.guard(real / ".skill-pilot", real), real / ".skill-pilot")
            with self.assertRaises(ValueError):
                store.guard(real / "../outside", real)

    def test_unresolved_temp_symlink_home_with_spaces_is_writable(self):
        # macOS 的临时目录常以 /var 指向 /private/var。安全检查要先换成真实路径，
        # 否则这种模拟 home 会被当成只读，真实主目录的保护不能因此放宽。
        from skill_pilot.web.server import preview_allowed
        fresh = tempfile.TemporaryDirectory(prefix="skill pilot ")
        self.addCleanup(fresh.cleanup)
        root = Path(fresh.name)
        (root / "private" / "agent home").mkdir(parents=True)
        (root / "var").symlink_to(root / "private", target_is_directory=True)
        home = root / "var" / "agent home"
        self.assertIn(" ", str(home))
        self.assertNotEqual(home.absolute(), home.resolve())
        self.assertTrue(store.write_allowed(home))
        self.assertTrue(preview_allowed(home))
        target = store.guard(home / ".skill-pilot" / "registry.json", home)
        target.parent.mkdir()
        target.write_text("{}\n")
        self.assertEqual((home.resolve() / ".skill-pilot" / "registry.json").read_text(), "{}\n")
        outside = root / "outside"
        outside.mkdir()
        (home / "escape").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "之外"):
            store.guard(home / "escape" / "file", home)
        with patch.dict(os.environ, {"SKILL_PILOT_READ_ONLY": "1"}):
            self.assertFalse(store.write_allowed(home))
            with self.assertRaisesRegex(ValueError, "只读"):
                store.guard(home / ".skill-pilot", home)

    def test_guard_denies_parent_and_internal_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            (self.home / "escape").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "之外"):
                store.guard(self.home / "escape/file", self.home)
            with self.assertRaisesRegex(ValueError, "之外"):
                store.guard(self.home / "../file", self.home)
            skill = self.skill(".codex/skills/sample")
            (skill / "external").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "之外"):
                store.apply(store.preview(self.home)["plan_id"], self.home)
            self.assertFalse((self.home / ".skill-pilot").exists())

    def test_root_escape_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as outside:
            root = Path(outside)
            (root / "sample").mkdir()
            (root / "sample/SKILL.md").write_text('---\nname: sample\ndescription: example\n---\n')
            (self.home / ".codex/skills").symlink_to(root)
            before = store.physical_state(root)
            with self.assertRaisesRegex(ValueError, "之外"):
                store.apply(store.preview(self.home)["plan_id"], self.home)
            self.assertEqual(before, store.physical_state(root))

    def test_shared_readers_must_be_selected_together_and_claude_can_be_separate(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, "一起选择"):
            store.preview(self.home, ["dsh"])
        before = store.physical_state(self.home / ".codex/skills")
        plan = store.preview(self.home, ["claude"])
        result = store.apply(plan["plan_id"], self.home, ["claude"])
        self.assertEqual(before, store.physical_state(self.home / ".codex/skills"))
        store.restore(result["backup_id"], self.home)

    def test_manual_only_survives_deduplication(self):
        path = self.skill(".agents/skills/sample")
        manifest = path / "SKILL.md"
        manifest.write_text(manifest.read_text().replace('description:', 'disable-model-invocation: true\ndescription:'))
        result = store.apply(store.preview(self.home)["plan_id"], self.home)
        self.assertEqual(visibility(self.home)["codex"]["available"], ["sample"])
        self.assertEqual(visibility(self.home)["codex"]["automatic"], [])
        store.restore(result["backup_id"], self.home)

    def test_assignments_choose_shared_or_private_roots_and_keep_aliases(self):
        registry = {"skills":{
            "both":{"assigned_agents":["codex","dsh"],"entries":[]},
            "only-codex":{"assigned_agents":["codex"],"entries":[]},
            "only-dsh":{"assigned_agents":["dsh"],"entries":[]},
            "private":{"assigned_agents":["claude","antigravity"],"entries":[]},
            "alias":{"assigned_agents":["codex","dsh"],"entries":[".agents/skills/alias",".codex/skills/community/alias"]}}}
        links = desired_links(registry, self.home)
        self.assertEqual(links[str(self.home/'.agents/skills/both')],str(self.home/'.skill-pilot/store/both'))
        self.assertIn(str(self.home/'.codex/skills/only-codex'),links)
        self.assertIn(str(self.home/'.dsh/skills/only-dsh'),links)
        self.assertIn(str(self.home/'.claude/skills/private'),links)
        self.assertIn(str(self.home/'.gemini/config/skills/private'),links)
        self.assertNotIn(str(self.home/'.dsh/skills/alias'),links)

    def test_restore_rejects_archive_escape_before_changing_roots(self):
        import io
        import tarfile
        self.fixture()
        result=store.apply(store.preview(self.home)['plan_id'],self.home)
        before=self.fingerprint()
        archive=self.home/'.skill-pilot/backups'/result['backup_id']/'files.tar'
        with tarfile.open(archive,'a') as tar:
            entry=tarfile.TarInfo('../escape')
            entry.size=1
            tar.addfile(entry,io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError,'之外|不安全'):
            store.restore(result['backup_id'],self.home)
        self.assertEqual(before,self.fingerprint())

    def test_git_tree_matches_git_for_modes_directories_and_links(self):
        import subprocess
        path=self.skill('tree',body='upstream')
        (path/'folder').mkdir()
        (path/'folder/file.txt').write_text('content')
        (path/'run.sh').write_text('#!/bin/sh\nexit 0\n')
        (path/'run.sh').chmod(0o755)
        (path/'alias').symlink_to('folder/file.txt')
        subprocess.run(['git','init','-q',str(path)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(path),'add','.'],check=True,capture_output=True)
        expected=subprocess.check_output(['git','-C',str(path),'write-tree'],text=True).strip()
        self.assertEqual(store.git_tree(path),expected)

    def test_symlinked_claude_root_preserves_readonly_synced_on_restore(self):
        root=self.home/'claude-backing'
        root.mkdir()
        (self.home/'.claude/skills').symlink_to(root)
        self.skill('claude-backing/sample')
        self.skill('claude-backing/synced/account/builtin','builtin')
        before=store.physical_state(root)
        plan=store.preview(self.home,['claude'])
        self.assertEqual([s['name'] for s in plan['skills']],['sample'])
        result=store.apply(plan['plan_id'],self.home,['claude'])
        store.restore(result['backup_id'],self.home)
        self.assertEqual(before,store.physical_state(root))
        self.assertTrue((self.home/'.claude/skills').is_symlink())

    @patch.object(store, "attribution", lambda: {})
    def test_source_confirmation_or_unknown_is_persisted(self):
        self.skill(".codex/skills/unslop", "unslop")
        plan = store.preview(self.home)
        result = store.apply(plan["plan_id"], self.home)
        registry = json.loads((self.home / ".skill-pilot/registry.json").read_text())
        self.assertEqual(registry["skills"]["unslop"]["source"]["source"], "来源不明")
        store.restore(result["backup_id"], self.home)
        store.apply(store.preview(self.home)["plan_id"], self.home, sources={"unslop":"本机自写"})
        registry = json.loads((self.home / ".skill-pilot/registry.json").read_text())
        self.assertEqual(registry["skills"]["unslop"]["source"]["source"], "本机自写")

    def test_simulator_copies_links_and_only_skills_config(self):
        self.fixture()
        config = self.home / ".codex/config.toml"
        config.write_text('secret="must not copy"\n[skills]\nconfig=[]\n')
        (self.home / ".agents/skills/sample").unlink()
        (self.home / ".agents/skills/sample").symlink_to(self.home / ".codex/skills/sample")
        legacy_root = self.home / '.skill-pilot'
        legacy_root.mkdir(exist_ok=True)
        legacy = [legacy_root / ('usage.sqlite' + suffix) for suffix in ['', '-wal', '-shm']]
        for path in legacy:
            path.write_bytes(b'legacy database')
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in legacy]
        spec = importlib.util.spec_from_file_location("fake", store.PROJECT / "scripts/make-fake-home.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as target:
            result = module.make_fake_home(self.home, target)
            fake = Path(result["home"])
            store.check_links(fake, fake)
            self.assertNotIn("secret", (fake / ".codex/config.toml").read_text())
            self.assertEqual((fake / ".agents/skills/sample").resolve(), fake / ".codex/skills/sample")
            self.assertFalse(any((fake / '.skill-pilot').glob('usage.sqlite*')))
        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in legacy], before)


if __name__ == "__main__":
    unittest.main()
