"""Pending intent is rebased onto live files without overwriting external work."""
import contextlib
import shutil
import unittest
from unittest.mock import patch

from tests import test_operations
from skill_pilot import operations, store
from skill_pilot.sync import visibility


class AssignmentRecheckTests(unittest.TestCase):
    setUp = test_operations.OperationsTests.setUp
    skill = test_operations.OperationsTests.skill
    fingerprint = test_operations.OperationsTests.fingerprint
    assign = test_operations.OperationsTests.assign

    def changes(self, name='other', agent='codex', enabled=False):
        return [{'skill': name, 'agent': agent, 'enabled': enabled}]

    def preview(self, changes):
        return operations.preview_assign(changes, self.home, recheck=True)

    def apply(self, changes, review=None):
        review = self.preview(changes) if review is None else review
        return operations.apply_assign(changes, review['revision'], True, self.home, recheck=True)

    def test_preview_is_read_only_and_only_replays_explicit_intent(self):
        self.skill('.claude/skills/external', 'external')
        before = store.physical_state(self.home)
        plan = self.preview(self.changes())
        self.assertEqual(plan['conflicts'], [])
        self.assertEqual(plan['changes'], self.changes())
        self.assertEqual(store.physical_state(self.home), before)

    def test_unrelated_content_and_new_files_survive_save_and_undo(self):
        changes = self.changes()
        self.preview(changes)
        skill = self.home / '.skill-pilot/store/grilling/SKILL.md'
        skill.write_text(skill.read_text() + '\nExternal update\n')
        self.skill('.claude/skills/external', 'external')
        before = self.fingerprint()
        result = self.apply(changes)
        self.assertIn('External update', skill.read_text())
        self.assertIn('external', visibility(self.home)['claude']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_external_link_removal_survives_save_and_exact_undo(self):
        (self.home / '.codex/skills/grilling').unlink()
        before = self.fingerprint()
        result = self.apply(self.changes())
        self.assertFalse((self.home / '.codex/skills/grilling').exists())
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_external_disabled_setting_survives_unrelated_assignment(self):
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text().replace('plugin:original', 'grilling'))
        before = self.fingerprint()
        result = self.apply(self.changes())
        self.assertNotIn('grilling', visibility(self.home)['codex']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_another_agent_assignment_on_same_skill_is_preserved(self):
        changes = self.changes()
        self.preview(changes)
        self.assign('other', 'claude')
        before = self.fingerprint()
        result = self.apply(changes)
        self.assertIn('other', visibility(self.home)['claude']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_same_change_already_done_needs_no_write(self):
        changes = self.changes()
        self.preview(changes)
        (self.home / '.codex/skills/other').unlink()
        before = store.physical_state(self.home)
        result = self.apply(changes)
        self.assertNotIn('id', result)
        self.assertIn('已经完成', result['message'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_selected_skill_content_update_can_be_used_without_overwrite(self):
        changes = self.changes(agent='claude', enabled=True)
        self.preview(changes)
        path = self.home / '.skill-pilot/store/other/SKILL.md'
        text = path.read_text() + '\nNew version\n'
        path.write_text(text)
        result = self.apply(changes)
        self.assertIn('id', result)
        self.assertEqual(path.read_text(), text)
        self.assertIn('other', visibility(self.home)['claude']['available'])

    def test_already_enabled_across_multiple_agents_needs_no_write(self):
        self.assign('other', 'claude')
        before = store.physical_state(self.home)
        result = self.apply(self.changes(agent='claude', enabled=True))
        self.assertNotIn('id', result)
        self.assertEqual(store.physical_state(self.home), before)

    def test_replaced_entry_is_named_as_conflict_and_never_overwritten(self):
        path = self.home / '.codex/skills/other'
        path.unlink()
        self.skill('.codex/skills/other', 'other', 'External replacement')
        before = store.physical_state(self.home)
        review = self.preview(self.changes())
        self.assertTrue(any(c['skill'] == 'other' and '替换' in c['message'] for c in review['conflicts']))
        result = self.apply(self.changes(), review)
        self.assertTrue(result['needs_attention'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_missing_target_does_not_drop_other_choices(self):
        shutil.rmtree(self.home / '.skill-pilot/store/other')
        changes = self.changes(agent='claude', enabled=True) + self.changes('grilling', 'claude', True)
        review = self.preview(changes)
        self.assertEqual(review['conflicts'][0]['skill'], 'other')
        self.assertIn('claude', review['assignments']['grilling'])
        self.assertTrue(operations.table(self.home)['skills'])
        result = self.apply(changes[1:])
        self.assertIn('id', result)
        self.assertIn('grilling', visibility(self.home)['claude']['available'])

    def test_agent_no_longer_available_is_a_specific_conflict(self):
        changes = self.changes(agent='removed-agent', enabled=True)
        review = self.preview(changes)
        self.assertIn('已不再接入', review['conflicts'][0]['message'])

    def test_external_disable_blocks_enabling_only_that_choice(self):
        self.assign('other', 'codex', False)
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text().replace('plugin:original', 'other'))
        review = self.preview(self.changes(enabled=True))
        self.assertTrue(any('独立停用' in c['message'] for c in review['conflicts']))

    def test_dependencies_are_recomputed_from_latest_rules(self):
        changes = self.changes('grill-me', 'claude', True)
        self.assertEqual(self.preview(changes)['auto_added'], [])
        operations.decide_dependencies([{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': True}], home=self.home)
        review = self.preview(changes)
        self.assertEqual(review['auto_added'], [{'skill': 'grilling', 'agent': 'claude', 'because': 'grill-me'}])
        result = self.apply(changes, review)
        self.assertIn('id', result)
        self.assertIn('grilling', visibility(self.home)['claude']['available'])

    def test_already_enabled_skill_with_disabled_unused_alias_is_satisfied(self):
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text().replace('name="plugin:original"', 'path="~/.agents/skills/other"'))
        review = self.preview(self.changes(enabled=True))
        self.assertEqual(review['conflicts'], [])
        self.assertEqual(review['changes'], [])

    def test_shared_placement_cannot_silently_disable_another_agent(self):
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text().replace('name="plugin:original"', 'path="~/.agents/skills/other"'))
        before = store.physical_state(self.home)
        review = self.preview(self.changes(agent='dsh', enabled=True))
        self.assertTrue(any(c['agent'] == 'dsh' and 'Codex' in c['message'] for c in review['conflicts']))
        self.assertTrue(self.apply(self.changes(agent='dsh', enabled=True), review)['needs_attention'])
        self.assertEqual(store.physical_state(self.home), before)

    def test_automatic_dependency_block_is_attached_to_parent_choice(self):
        self.assign('grill-me', 'codex', False)
        self.assign('grilling', 'codex', False)
        operations.decide_dependencies([{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': True}], home=self.home)
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text().replace('plugin:original', 'grilling'))
        review = self.preview(self.changes('grill-me', 'codex', True))
        self.assertTrue(any(c['skill'] == 'grill-me' and 'grilling' in c['message'] for c in review['conflicts']))

    def test_dependency_removal_still_requires_confirmation(self):
        operations.decide_dependencies([{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': True}], home=self.home)
        changes = self.changes('grill-me', 'claude', True) + self.changes('grilling', 'claude', False)
        review = self.preview(changes)
        before = self.fingerprint()
        result = operations.apply_assign(changes, review['revision'], False, self.home, recheck=True)
        self.assertTrue(result['needs_confirmation'])
        self.assertEqual(self.fingerprint(), before)

    def test_change_between_review_and_lock_requests_recheck_without_writing(self):
        changes = self.changes()
        review = self.preview(changes)
        transaction = operations.transaction
        @contextlib.contextmanager
        def changed_before_lock(home):
            with transaction(home) as lock:
                config = home / '.codex/config.toml'
                config.write_text(config.read_text() + '\n# concurrent change\n')
                yield lock
        logs = len(operations.history(self.home))
        with patch.object(operations, 'transaction', changed_before_lock):
            with self.assertRaises(operations.AssignmentChanged):
                self.apply(changes, review)
        self.assertEqual(len(operations.history(self.home)), logs)
        self.assertIn('other', visibility(self.home)['codex']['available'])
        result = self.apply(changes)
        self.assertIn('id', result)

    def test_failed_write_rolls_back_latest_external_state(self):
        (self.home / '.codex/skills/grilling').unlink()
        before = self.fingerprint()
        with patch.object(operations, '_commit', side_effect=OSError('failed')):
            with self.assertRaises(ValueError):
                self.apply(self.changes())
        self.assertEqual(self.fingerprint(), before)

    def test_undo_does_not_overwrite_subsequent_external_changes(self):
        result = self.apply(self.changes())
        config = self.home / '.codex/config.toml'
        config.write_text(config.read_text() + '\n# later external change\n')
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '外部改动'):
            operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)
