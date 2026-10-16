from tests import set_up_home
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import agents, cleanup, operations, store
from skill_pilot.scan import content_digest, scan
from skill_pilot.sync import visibility


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='skill-pilot-cleanup-')
        self.addCleanup(self.tmp.cleanup)
        set_up_home(self, Path(self.tmp.name))
        self.names = ['candidate', 'unknown', 'parent', 'dep', 'deep', 'unassigned', 'dsh-only']
        for name in self.names:
            path = self.home/'.codex/skills'/name
            path.mkdir(parents=True)
            (path/'SKILL.md').write_text(f'---\nname: {name}\ndescription: hello\n---\ncontent\n')
        (self.home/'.codex/config.toml').write_text('# preserve\n[skills]\nconfig=[{name="plugin:own",enabled=false}]\n')
        store.apply(store.preview(self.home)['plan_id'], self.home)
        operations.apply_assign([{'skill': 'unassigned', 'agent': 'codex', 'enabled': False},
            {'skill': 'unknown', 'agent': 'antigravity', 'enabled': True},
            {'skill': 'dsh-only', 'agent': 'codex', 'enabled': False},
            {'skill': 'dsh-only', 'agent': 'dsh', 'enabled': True}], confirmed=True, home=self.home)
        _, registry, _ = operations.load(self.home)
        registry['skills']['parent']['requires'] = ['dep']
        registry['skills']['dep']['requires'] = ['deep']
        registry['skills']['deep']['requires'] = ['dep']
        (self.home/'.skill-pilot/registry.json').write_text(json.dumps(registry))
        operations._commit(self.home, registry, self.names, [], 'fixture metadata')
        (self.home/'.skill-pilot/labels.json').write_text(json.dumps({'groups': [
            {'name': '示例', 'skills': {'parent': ['多智能体分派', '分派任务'], 'dep': ['营销技能路由', '选择技能']}}]}))

    def fingerprint(self):
        paths = ['.codex/skills', '.agents/skills', '.claude/skills', '.gemini/config/skills', '.dsh/skills',
                 '.codex/config.toml', '.skill-pilot/registry.json', '.skill-pilot/agents.json', '.skill-pilot/bases']
        return {p: store.physical_state(self.home/p) for p in paths} | {'store': content_digest(self.home/'.skill-pilot/store')}

    def test_preview_is_readonly_and_counts_only_selected_skills(self):
        before = self.fingerprint()
        view = cleanup.preview(['unknown', 'dsh-only', 'candidate', 'candidate'], self.home)
        self.assertEqual(set(view), {'skills', 'message', 'dependency_warnings', 'path_warnings', 'revision'})
        self.assertEqual(view['skills'], ['candidate', 'dsh-only', 'unknown'])
        self.assertEqual(view['message'], '从所有 agent 撤下 3 个技能')
        self.assertEqual(view['dependency_warnings'], [])
        self.assertEqual(cleanup.list_withdrawn(self.home)['withdrawn'], [])
        self.assertEqual(self.fingerprint(), before)

    def test_dependency_warnings_use_titles_and_ignore_selected_dependents(self):
        warnings = cleanup.preview(['dep'], self.home)['dependency_warnings']
        self.assertEqual(warnings, ['「deep」还要用「营销技能路由」，撤下后它可能用不了。',
                                   '「多智能体分派」还要用「营销技能路由」，撤下后它可能用不了。'])
        self.assertEqual(cleanup.preview(['dep', 'deep'], self.home)['dependency_warnings'], warnings[1:])
        self.assertEqual(cleanup.preview(['parent', 'dep', 'deep'], self.home)['dependency_warnings'], [])
        operations.apply_assign([{'skill': 'parent', 'agent': 'codex', 'enabled': False}], confirmed=True, home=self.home)
        self.assertEqual(cleanup.preview(['dep'], self.home)['dependency_warnings'], warnings[:1])

    def test_path_warnings_receive_exact_removed_entries_and_require_confirmation(self):
        expected = [{'file': '~/workflow.md', 'entries': ['.codex/skills/candidate']}]
        before = self.fingerprint()
        with patch.object(operations, 'path_warnings', return_value=expected) as warnings:
            result = cleanup.apply(['candidate'], home=self.home)
        warnings.assert_called_once_with(['.codex/skills/candidate'], self.home)
        self.assertTrue(result['needs_confirmation'])
        self.assertEqual(result['path_warnings'], expected)
        self.assertEqual(self.fingerprint(), before)

    def test_withdraw_restore_and_both_undos_are_exact(self):
        before = self.fingerprint()
        config = (self.home/'.codex/config.toml').read_bytes()
        _, registry, _ = operations.load(self.home)
        original = registry['skills']['dsh-only']
        result = cleanup.apply(['dsh-only'], True, self.home)
        self.assertEqual(result['withdrawn'], ['dsh-only'])
        self.assertEqual(result['message'], '撤下了 1 个技能')
        self.assertTrue((self.home/'.skill-pilot/store/dsh-only/SKILL.md').exists())
        self.assertFalse((self.home/'.agents/skills/dsh-only').exists())
        skill = operations.load(self.home)[1]['skills']['dsh-only']
        self.assertTrue(skill['withdrawn'])
        self.assertEqual(skill['withdrawn_agents'], original['assigned_agents'])
        self.assertEqual(skill['withdrawn_entries'], original['entries'])
        view = cleanup.list_withdrawn(self.home)
        self.assertEqual(set(view), {'revision', 'write_allowed', 'withdrawn'})
        self.assertEqual(view['withdrawn'], [{'name': 'dsh-only', 'previous_agents': ['dsh'], 'withdrawn_at': skill['withdrawn_at']}])
        withdrawn = self.fingerprint()
        restored = cleanup.restore(['dsh-only'], self.home)
        self.assertIn('dsh-only', visibility(self.home)['dsh']['available'])
        self.assertNotIn('dsh-only', visibility(self.home)['codex']['available'])
        self.assertEqual((self.home/'.codex/config.toml').read_bytes(), config)
        operations.undo(restored['id'], home=self.home)
        self.assertEqual(self.fingerprint(), withdrawn)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_manual_withdrawal_from_all_agents_and_batch_undo(self):
        before = self.fingerprint()
        result = cleanup.apply(['unknown', 'candidate', 'unknown'], True, self.home)
        self.assertEqual(result['message'], '撤下了 2 个技能')
        for name in ['unknown', 'candidate']:
            self.assertEqual(operations.load(self.home)[1]['skills'][name]['assigned_agents'], [])
            for view in visibility(self.home).values():
                self.assertNotIn(name, view['available'])
        self.assertIn('parent', visibility(self.home)['codex']['available'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_confirmation_stale_revision_and_failure_leave_state_unchanged(self):
        before = self.fingerprint()
        for confirmed in [False, None, 1, 'true']:
            self.assertTrue(cleanup.apply(['dep'], confirmed, self.home)['needs_confirmation'])
        with self.assertRaisesRegex(ValueError, '目录内容已经变了'):
            cleanup.apply(['candidate'], True, self.home, expected='stale')
        with patch.object(cleanup, 'reconcile_assignments', side_effect=ValueError('injected')):
            with self.assertRaisesRegex(ValueError, 'injected'):
                cleanup.apply(['candidate'], True, self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_invalid_selections_are_rejected_before_any_withdrawal(self):
        before = self.fingerprint()
        for names in [None, 'candidate', {}, [], [None], [1], [[]], ['missing'], ['unassigned'], ['candidate', 'missing']]:
            with self.subTest(names=names):
                with self.assertRaises(ValueError):
                    cleanup.preview(names, self.home)
                with self.assertRaises(ValueError):
                    cleanup.apply(names, True, self.home)
        self.assertEqual(self.fingerprint(), before)
        cleanup.apply(['candidate'], True, self.home)
        with self.assertRaisesRegex(ValueError, '已经撤下'):
            cleanup.preview(['candidate'], self.home)
        for names in [[], ['missing'], ['unassigned']]:
            with self.assertRaisesRegex(ValueError, '请选择已撤下'):
                cleanup.restore(names, self.home)
        registry = operations.load(self.home)[1]
        registry['skills']['candidate'].pop('withdrawn_at')
        (self.home/'.skill-pilot/registry.json').write_text(json.dumps(registry))
        self.assertIsNone(cleanup.list_withdrawn(self.home)['withdrawn'][0]['withdrawn_at'])

    def test_legacy_database_is_never_opened_or_modified(self):
        legacy = [self.home/('.skill-pilot/usage.sqlite'+suffix) for suffix in ['', '-wal', '-shm']]
        for path in legacy:
            path.write_bytes(b'legacy database, not valid SQLite')
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in legacy]
        original_open = Path.open
        def checked_open(path, *args, **kwargs):
            if path in legacy:
                raise AssertionError('不能读取或改写旧用量数据库')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', checked_open):
            operations.table(self.home)
            agents.overview(self.home)
            scan(self.home)
            cleanup.list_withdrawn(self.home)
            cleanup.preview(['candidate'], self.home)
            result = cleanup.apply(['candidate'], True, self.home)
            restored = cleanup.restore(['candidate'], self.home)
            operations.undo(restored['id'], home=self.home)
            operations.undo(result['id'], home=self.home)
        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in legacy], before)


if __name__ == '__main__':
    unittest.main()
