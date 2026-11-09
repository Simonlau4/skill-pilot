"""分配预检查应在模拟技能库上很快返回，并拦住真实的外部副本。"""
import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, profiles, scan, store
from skill_pilot.web.server import Dashboard
from tests import set_up_home
from tests import test_operations as operation_fixtures


class PreviewSpeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot preview ')
        self.addCleanup(self.temp.cleanup)
        set_up_home(self, Path(self.temp.name) / 'home with spaces')
        for relative in ('.codex', '.claude', '.gemini/config', '.gemini/antigravity', '.dsh', '.cursor'):
            (self.home / relative).mkdir(parents=True, exist_ok=True)
        (self.home / '.codex/config.toml').write_bytes(b'[skills]\nconfig = []\n')

    def skill(self, relative, name, body='正文'):
        path = self.home / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例技能\n---\n{body}\n')
        return path

    def organize(self, count):
        for index in range(count):
            self.skill(f'.codex/skills/skill-{index:03d}', f'skill-{index:03d}')
        plan = store.preview(self.home)
        store.apply(plan['plan_id'], self.home)
        registry = json.loads((self.home / '.skill-pilot/registry.json').read_text())
        return sorted(registry['skills'])

    def test_recheck_does_not_resolve_the_same_directory_once_per_skill(self):
        names = self.organize(48)
        self.assertGreaterEqual(len(profiles.managed(self.home)), 4)
        changes = [{'skill': names[0], 'agent': 'codex', 'enabled': False}]
        before = store.physical_state(self.home)
        counts, digests = Counter(), {'n': 0}
        real_resolve, real_digest = Path.resolve, scan.content_digest

        def counting_resolve(path, *args, **kwargs):
            counts[str(path)] += 1
            return real_resolve(path, *args, **kwargs)

        def counting_digest(directory):
            digests['n'] += 1
            return real_digest(directory)

        Path.resolve = counting_resolve
        scan.content_digest = counting_digest
        try:
            started = time.perf_counter()
            review = operations.preview_assign(changes, self.home, recheck=True)
            elapsed = time.perf_counter() - started
        finally:
            Path.resolve = real_resolve
            scan.content_digest = real_digest
        self.assertEqual(store.physical_state(self.home), before)
        self.assertEqual(review['conflicts'], [])
        self.assertEqual(review['changes'], changes)
        self.assertIn('revision', review)
        self.assertIn('cell_notes', review)
        roots = [str(self.home / root) for profile in profiles.managed(self.home) for root in profile['skill_roots']]
        root_hits = max((counts[path] for path in roots), default=0)
        # 修复前，同一个技能目录会按技能个数重复解析，48 个技能能到几百次。
        self.assertLess(root_hits, 12, f'{elapsed:.3f}s roots={root_hits} digests={digests["n"]}')
        self.assertLessEqual(digests['n'], len(names) + 8, digests['n'])

    def test_real_folder_outside_the_store_still_blocks_the_change(self):
        self.skill('.codex/skills/other', 'other')
        plan = store.preview(self.home)
        store.apply(plan['plan_id'], self.home)
        self.skill('.claude/skills/other', 'other', '外部副本')
        before = store.physical_state(self.home)
        review = operations.preview_assign(
            [{'skill': 'other', 'agent': 'codex', 'enabled': False}], self.home, recheck=True)
        self.assertTrue(any('替换' in item['message'] for item in review['conflicts']))
        self.assertEqual(store.physical_state(self.home), before)


class PreviewSupersedeTests(unittest.TestCase):
    def test_a_newer_preview_stops_the_older_one_before_it_finishes(self):
        temp = tempfile.TemporaryDirectory(prefix='skill pilot preview server ')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / 'home'
        home.mkdir()
        set_up_home(self, home)
        server = Dashboard({'read_only': True, 'home': str(home)})
        self.addCleanup(server.server_close)
        started = threading.Event()
        calls = []

        def fake(changes, home=None, recheck=False, cancelled=None):
            calls.append(changes[0]['skill'])
            if changes[0]['skill'] == 'first':
                started.set()
                deadline = time.time() + 2
                while time.time() < deadline:
                    if cancelled():
                        raise operations.PreviewSuperseded()
                    time.sleep(0.01)
                raise AssertionError('较早的预检查没有停下来')
            return {'assignments': {'second': []}, 'changes': [], 'conflicts': []}

        results, errors = [], []

        def run(skill):
            try:
                results.append(server.preview_assignment(
                    [{'skill': skill, 'agent': 'codex', 'enabled': False}], True))
            except Exception as error:
                errors.append(error)

        with patch.object(operations, 'preview_assign', fake):
            first = threading.Thread(target=run, args=('first',), daemon=True)
            first.start()
            self.assertTrue(started.wait(2))
            second = threading.Thread(target=run, args=('second',), daemon=True)
            second.start()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertIn('first', calls)
        self.assertIn('second', calls)
        self.assertTrue(any(item.get('superseded') for item in results))
        self.assertTrue(any(item.get('assignments') for item in results if not item.get('superseded')))


class SmallPreviewTests(unittest.TestCase):
    setUp = operation_fixtures.OperationsTests.setUp
    skill = operation_fixtures.OperationsTests.skill

    def test_recheck_keeps_dependency_warnings_and_does_not_write(self):
        operations.decide_dependencies(
            [{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': True}], home=self.home)
        before = store.physical_state(self.home)
        review = operations.preview_assign(
            [{'skill': 'grill-me', 'agent': 'claude', 'enabled': True}], self.home, recheck=True)
        self.assertEqual(store.physical_state(self.home), before)
        self.assertTrue(review['auto_added'])
        self.assertIn('revision', review)
        self.assertFalse(review['conflicts'])
