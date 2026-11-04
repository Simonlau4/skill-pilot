"""External shared entries must not change an unselected assistant."""
import json
import shutil
import unittest
from unittest.mock import patch

from skill_pilot import operations
from skill_pilot.sync import visibility
from tests import test_cli_sharing, test_jobs, test_operations


class SharedLinkTests(unittest.TestCase):
    setUp = test_operations.OperationsTests.setUp
    skill = test_operations.OperationsTests.skill
    assign = test_operations.OperationsTests.assign
    fingerprint = test_operations.OperationsTests.fingerprint

    def shared(self, indirect=False, alias=False):
        self.assign('other', 'dsh')
        entry = self.home / ('.claude/skills/nested/alias' if alias else '.claude/skills/other')
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.symlink_to(self.home / ('.agents/skills/other' if indirect else '.skill-pilot/store/other'),
                         target_is_directory=True)
        return entry

    def withdraw(self, recheck, indirect=False, alias=False):
        self.shared(indirect, alias)
        before = self.fingerprint()
        changes = [{'skill': 'other', 'agent': a, 'enabled': False} for a in ('codex', 'dsh')]
        review = operations.preview_assign(changes, self.home, recheck=recheck)
        result = operations.apply_assign(changes, review.get('revision'), True, self.home, recheck=recheck)
        self.assertIn('id', result)
        visible = visibility(self.home)
        self.assertIn('other', visible['claude']['available'])
        self.assertNotIn('other', visible['codex']['available'])
        self.assertNotIn('other', visible['dsh']['available'])
        self.assertEqual(operations.load(self.home)[1]['skills']['other']['assigned_agents'], ['claude'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_direct_assignment_preserves_external_claude(self):
        self.withdraw(False)

    def test_indirect_assignment_preserves_external_claude(self):
        self.withdraw(False, True)

    def test_dashboard_preserves_indirect_claude(self):
        self.withdraw(True, True)

    def test_dashboard_preserves_nested_alias(self):
        self.withdraw(True, True, True)

    def test_removing_claude_cleans_its_unregistered_alias_only(self):
        alias = self.shared(True, True)
        before = self.fingerprint()
        result = self.assign('other', 'claude', False, confirmed=True)
        actual = visibility(self.home)
        self.assertNotIn('other', actual['claude']['available'])
        self.assertIn('other', actual['codex']['available'])
        self.assertIn('other', actual['dsh']['available'])
        self.assertFalse(alias.is_symlink())
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_multilevel_alias_and_user_disabled_path_are_preserved(self):
        alias = self.shared(True)
        middle = self.home / '.dsh/skills/other'
        middle.parent.mkdir(parents=True, exist_ok=True)
        middle.symlink_to(self.home / '.agents/skills/other', target_is_directory=True)
        alias.unlink()
        alias.symlink_to(middle, target_is_directory=True)
        config = self.home / '.codex/config.toml'
        disabled = self.home / '.codex/skills/other'
        disabled.symlink_to(self.home / '.skill-pilot/store/other', target_is_directory=True)
        config.write_text('[skills]\nconfig = [{path=' + json.dumps(str(disabled / 'SKILL.md')) +
                          ', enabled=false}]\n')
        original = config.read_bytes()
        before = self.fingerprint()
        result = self.assign('other', 'dsh', False, confirmed=True)
        actual = visibility(self.home)
        self.assertIn('other', actual['claude']['available'])
        self.assertNotIn('other', actual['codex']['available'])
        self.assertNotIn('other', actual['dsh']['available'])
        self.assertEqual(config.read_bytes(), original)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_cli_preserves_shared_claude_and_can_undo(self):
        self.shared(True)
        before = self.fingerprint()
        ids = []
        for agent in ('codex', 'dsh'):
            result = test_cli_sharing.run(self.home, 'unassign', 'other', '--agent', agent, '--yes', '--json')
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            ids.append(json.loads(result.stdout)['id'])
            self.assertIn('other', visibility(self.home)['claude']['available'])
        for operation in reversed(ids):
            operations.undo(operation, home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_absorb_identical_copy_keeps_all_actual_readers_and_undo(self):
        self.shared(True)
        source = self.home / '.agents/skills/other'
        source.unlink()
        shutil.copytree(self.home / '.skill-pilot/store/other', source)
        before = self.fingerprint()
        result = operations.absorb(all_skills=True, home=self.home)
        self.assertIn('id', result)
        self.assertEqual(operations.load(self.home)[1]['skills']['other']['assigned_agents'],
                         ['claude', 'codex', 'dsh'])
        self.assertIn('other', visibility(self.home)['claude']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_changed_copy_still_blocks_without_changes(self):
        self.shared(True)
        source = self.home / '.agents/skills/other'
        source.unlink()
        self.skill('.agents/skills/other', 'other', '不同内容')
        before = self.fingerprint()
        self.assertTrue(operations.absorb(all_skills=True, home=self.home)['conflicts'])
        with self.assertRaises(ValueError):
            self.assign('other', 'dsh', False, confirmed=True)
        self.assertEqual(self.fingerprint(), before)

    def test_failure_restores_unregistered_alias_and_assignments(self):
        self.shared(True, True)
        before = self.fingerprint()
        with patch.object(operations, '_validate', side_effect=ValueError('injected validation failure')):
            with self.assertRaises(ValueError):
                self.assign('other', 'dsh', False, confirmed=True)
        self.assertEqual(self.fingerprint(), before)


class SharedLinkJobTests(unittest.TestCase):
    setUp = test_jobs.JobTests.setUp
    close = test_jobs.JobTests.close
    skill = test_jobs.JobTests.skill
    registry = test_jobs.JobTests.registry
    request = test_jobs.JobTests.request
    ok = test_jobs.JobTests.ok
    submit = test_jobs.JobTests.submit
    wait_jobs = test_jobs.JobTests.wait_jobs

    def test_queued_assignment_preserves_claude_and_undo(self):
        operations.apply_assign([{'skill': 'acme-b', 'agent': 'dsh', 'enabled': True}], home=self.home)
        alias = self.home / '.claude/skills/acme-b'
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(self.home / '.agents/skills/acme-b', target_is_directory=True)
        changes = [{'skill': 'acme-b', 'agent': a, 'enabled': False} for a in ('codex', 'dsh')]
        review = self.ok('/api/assignment/preview', {'changes': changes, 'recheck': True})
        self.assertEqual(review['conflicts'], [])
        queued = self.submit('assignment', changes=changes,
                             seen={k: review[k] for k in ('changes', 'dependency_warnings', 'path_warnings')})
        job = next(j for j in self.wait_jobs() if j['id'] == queued['id'])
        self.assertEqual(job['status'], 'done', job)
        actual = visibility(self.home)
        self.assertIn('acme-b', actual['claude']['available'])
        self.assertNotIn('acme-b', actual['codex']['available'])
        self.assertNotIn('acme-b', actual['dsh']['available'])
        record = operations.history(self.home)[0]
        self.ok('/api/undo', {'id': record['id'], 'confirmed': True,
                            'revision': operations.table(self.home)['revision']})
        actual = visibility(self.home)
        for agent in ('codex', 'dsh', 'claude'):
            self.assertIn('acme-b', actual[agent]['available'])


if __name__ == '__main__':
    unittest.main()
