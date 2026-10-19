from tests import set_up_home
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import codex_config, operations, store
from skill_pilot.scan import tokens
from skill_pilot.sources import github
from skill_pilot.sync import visibility


REAL_CURATED = operations.curated_dependencies


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot m3 test ')
        self.addCleanup(self.temp.cleanup)
        # 手动确认流程的用例不受 docs/dependency-decisions.json 影响；判定表本身另有用例覆盖。
        curated=patch.object(operations,'curated_dependencies',return_value=({},{}))
        curated.start(); self.addCleanup(curated.stop)
        set_up_home(self, Path(self.temp.name))
        self.skill('.codex/skills/grill-me', 'grill-me', '调用 grilling')
        self.skill('.codex/skills/grilling', 'grilling', '提问')
        self.skill('.codex/skills/other', 'other', '其他')
        (self.home / '.codex/config.toml').write_bytes(b'# private comments\n[skills]\nconfig = [{name="plugin:original",enabled=false}] # keep\n[other]\nvalue = "keep exactly"\n')
        plan = store.preview(self.home)
        store.apply(plan['plan_id'], self.home)

    def skill(self, relative, name, body='正文'):
        path = self.home / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例技能\n---\n{body}\n')
        return path

    def fingerprint(self):
        paths = ['.codex/skills', '.agents/skills', '.claude/skills', '.gemini/config/skills', '.dsh/skills',
                 '.codex/config.toml', '.skill-pilot/registry.json', '.skill-pilot/agents.json', '.skill-pilot/bases']
        return {p: store.physical_state(self.home / p) for p in paths} | {
            'store': operations.content_digest(self.home / '.skill-pilot/store')}

    def assign(self, name, agent, enabled=True, **kw):
        return operations.apply_assign([{'skill': name, 'agent': agent, 'enabled': enabled}], home=self.home, **kw)

    def test_manual_dependency_auto_assign_and_undo(self):
        operations.decide_dependencies([{'skill': 'grill-me', 'requires': 'grilling', 'confirmed': True}], home=self.home)
        before = self.fingerprint()
        result = self.assign('grill-me', 'claude')
        self.assertEqual(visibility(self.home)['claude']['available'], ['grill-me', 'grilling'])
        self.assertIn('因为 grill-me', result['reasons'][0])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_dependency_removal_requires_consent(self):
        operations.decide_dependencies([{'skill':'grill-me','requires':'grilling','confirmed':True}], home=self.home)
        self.assign('grill-me', 'claude')
        before = self.fingerprint()
        result = self.assign('grilling', 'claude', False)
        self.assertTrue(result['needs_confirmation'])
        self.assertTrue(result['dependency_warnings'])
        self.assertEqual(self.fingerprint(), before)
        self.assign('grilling', 'claude', False, confirmed=True)
        self.assertNotIn('grilling', visibility(self.home)['claude']['available'])

    def test_dsh_only_and_config_exact_undo(self):
        before = self.fingerprint()
        original = (self.home / '.codex/config.toml').read_bytes()
        self.assign('other', 'codex', False)
        point = self.fingerprint()
        result = self.assign('other', 'dsh')
        self.assertNotIn('other', visibility(self.home)['codex']['available'])
        self.assertIn('other', visibility(self.home)['dsh']['available'])
        config = (self.home / '.codex/config.toml').read_bytes()
        self.assertIn(b'{ name = "other", enabled = false }', config)
        self.assertEqual(config.replace(b', { name = "other", enabled = false }', b''), original)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), point)
        self.assertEqual((self.home / '.codex/config.toml').read_bytes(), original)

    def test_pending_revision_and_path_warning(self):
        with patch.object(operations, 'path_warnings', return_value=[{'file':'~/workflow.md','entries':['.codex/skills/other']}]) as warning:
            result = self.assign('other', 'codex', False)
            self.assertTrue(result['needs_confirmation'])
            self.assertEqual(result['path_warnings'][0]['file'], '~/workflow.md')
        view = operations.table(self.home)
        (self.home / '.codex/config.toml').write_text('# external\n' + (self.home / '.codex/config.toml').read_text())
        with self.assertRaisesRegex(ValueError, '刷新'):
            self.assign('other', 'claude', expected=view['revision'])

    def test_path_warnings_check_this_users_files_not_a_fixed_directory(self):
        removed = ['.codex/skills/alpha', '.codex/skills/beta', '.agents/skills/sample-skill']
        self.assertEqual(operations.path_warnings(removed, self.home), [])
        own = self.home / 'notes/own.md'
        own.parent.mkdir()
        own.write_text('引用')
        listed = self.home / 'Documents/Example-Notes/notes/sample-note.md'
        listed.parent.mkdir(parents=True)
        listed.write_text('引用')
        project = self.home / 'reference project'
        companion = project / 'work/path-references.json'
        companion.parent.mkdir(parents=True)
        companion.write_text(json.dumps({'records': [
            {'file': '~/notes/own.md', 'references': ['~/.codex/skills/beta']},
            {'file': '~/notes/own.md', 'references': ['/Users/example/.codex/skills/alpha']},
            {'file': '~/notes/missing.md', 'references': ['~/.codex/skills/beta']},
        ]}, ensure_ascii=False))
        with patch.object(operations, 'PROJECT', project):
            warnings = operations.path_warnings(removed, self.home)
        self.assertEqual(warnings, [{'file': '~/notes/own.md', 'entries': ['.codex/skills/beta']}])
        companion.unlink()
        warnings = operations.path_warnings(removed, self.home)
        self.assertEqual(warnings, [{
            'file': '~/Documents/Example-Notes/notes/sample-note.md',
            'entries': ['.agents/skills/sample-skill']}])

    def repository(self, name='installed', body='新安装'):
        path = self.home / 'repository'
        path.mkdir(exist_ok=True)
        self.skill('repository/skills/' + name, name, body)
        github.git(path, 'init', '-q')
        github.git(path, 'add', '.')
        github.git(path, '-c','user.name=Test','-c','user.email=test@local','-c','commit.gpgsign=false','commit','-qm','fixture')
        return path.as_uri()

    def test_file_install_provenance_base_conflict_and_undo(self):
        review = github.inspect(self.repository())
        self.addCleanup(lambda: __import__('shutil').rmtree(review['temporary'], ignore_errors=True))
        before = self.fingerprint()
        result = operations.install(review, ['installed'], ['claude'], home=self.home)
        record = operations.load(self.home)[1]['skills']['installed']
        self.assertTrue(record['source']['commit'])
        self.assertTrue(record['source']['tree_sha'])
        self.assertEqual(record['base_digest'], operations.content_digest(self.home / '.skill-pilot/bases/installed'))
        self.assertIn('installed', visibility(self.home)['claude']['available'])
        current = self.fingerprint()
        self.assertTrue(operations.install(review, ['installed'], home=self.home)['conflicts'])
        self.assertEqual(self.fingerprint(), current)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_absorb_lock_and_exact_undo(self):
        source = self.skill('.codex/skills/new', 'new')
        lock = self.home / '.agents/.skill-lock.json'
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({'skills': {'new': {'sourceUrl':'https://github.com/test/repo','skillPath':'skills/new','sourceType':'github'}}}))
        before = self.fingerprint()
        with patch.object(github, 'verify_source') as verify:
            result = operations.absorb(all_skills=True, home=self.home)
            verify.assert_not_called()
        self.assertTrue(source.is_symlink())
        self.assertEqual(operations.load(self.home)[1]['skills']['new']['assigned_agents'], ['codex'])
        operations.undo(result['id'], home=self.home)
        self.assertFalse(source.is_symlink())
        self.assertEqual(self.fingerprint(), before)

    def test_absorb_verified_self_made_and_conflict(self):
        self.skill('.codex/skills/new', 'new')
        with patch.object(github, 'verify_source', return_value={'source':'https://github.com/test/upstream','commit':'abc','tree_sha':'def'}):
            result = operations.absorb(all_skills=True, home=self.home)
        self.assertEqual(operations.load(self.home)[1]['skills']['new']['source']['commit'], 'abc')
        operations.undo(result['id'], home=self.home)
        with patch.object(github, 'verify_source', return_value={'source':'自制'}):
            result = operations.absorb(all_skills=True, home=self.home)
        self.assertEqual(operations.load(self.home)[1]['skills']['new']['source']['source'], '自制')
        self.skill('.claude/skills/new', 'new', '不同内容')
        before = self.fingerprint()
        self.assertTrue(operations.absorb(all_skills=True, home=self.home)['conflicts'])
        self.assertEqual(self.fingerprint(), before)

    def test_three_operations_reverse_exactly(self):
        points, records = [], []
        for name, agent in [('grilling','claude'),('other','antigravity'),('grill-me','claude')]:
            points.append(self.fingerprint())
            records.append(self.assign(name, agent)['id'])
        for operation, point in reversed(list(zip(records, points))):
            operations.undo(operation, home=self.home)
            self.assertEqual(self.fingerprint(), point)
        self.assertEqual(sum(r['undone'] for r in operations.history(self.home)), 3)

    def test_failure_rolls_back_config_roots_and_store(self):
        before = self.fingerprint()
        with patch.object(operations, '_commit', side_effect=OSError('failed')):
            with self.assertRaisesRegex(ValueError, '恢复原样'):
                self.assign('other', 'dsh')
        self.assertEqual(self.fingerprint(), before)

    def test_rejected_dependency_and_readonly(self):
        operations.decide_dependencies([{'skill':'grill-me','requires':'grilling','confirmed':False}], home=self.home)
        with patch.object(store, 'write_allowed', return_value=False):
            with self.assertRaises(ValueError):
                self.assign('other', 'claude')


class ConfigTests(unittest.TestCase):
    def test_lossless_multiple_owned_and_external_entries(self):
        initial = b'[skills]\nconfig = [ { name="existing", enabled=false } ] # unchanged\n'
        result, ownership = codex_config.render(initial, {}, {'one','two'})
        result, ownership = codex_config.render(result, ownership, {'two'})
        self.assertNotIn(b'"one"', result)
        result, ownership = codex_config.render(result, ownership, set())
        self.assertEqual(result, initial)

    def test_create_missing_table_and_exact_removal(self):
        for initial in (b'', b'[other]\na=1', b'[skills]\n# comment\n[other]\na=1\n'):
            result, owner = codex_config.render(initial, {}, {'one'})
            result, owner = codex_config.render(result, owner, set())
            self.assertEqual(initial, result)

    def test_existing_disable_never_owned_or_changed(self):
        text = b'[skills]\nconfig=[{name="one",enabled=false}]\n'
        result, owner = codex_config.render(text, {}, {'one'})
        self.assertEqual(text, result)
        self.assertEqual(owner['fragments'], {})

    def test_malformed_config_and_external_owned_edit(self):
        with self.assertRaises(ValueError):
            codex_config.render(b'[skills]\nconfig=[ broken', {}, {'one'})
        result, owner = codex_config.render(b'[skills]\nconfig=[]\n', {}, {'one'})
        with self.assertRaisesRegex(ValueError, '外部改过'):
            codex_config.render(result.replace(b'"one"', b'"changed"'), owner, set())


class AdditionalOperationsTests(unittest.TestCase):
    setUp = OperationsTests.setUp
    skill = OperationsTests.skill
    fingerprint = OperationsTests.fingerprint
    assign = OperationsTests.assign

    def test_latest_undo_preserves_external_new_directory(self):
        result=self.assign('other','claude')
        self.skill('.codex/skills/external','external')
        before=self.fingerprint()
        with self.assertRaisesRegex(ValueError,'外部改动'):
            operations.undo(result['id'],home=self.home)
        self.assertEqual(self.fingerprint(),before)
        self.assertTrue((self.home/'.codex/skills/external').is_dir())

    def test_nonlatest_disjoint_undo_keeps_later_assignment(self):
        first=self.assign('grilling','claude')
        second=self.assign('other','antigravity')
        operations.undo(first['id'],home=self.home)
        visible=visibility(self.home)
        self.assertNotIn('grilling',visible['claude']['available'])
        self.assertIn('other',visible['antigravity']['available'])

    def test_skip_readonly_empty_and_broken(self):
        self.skill('.claude/skills/synced/account/official','official')
        self.skill('.codex/skills/.system/official','official')
        (self.home/'.codex/skills/empty').mkdir()
        (self.home/'.codex/skills/broken').symlink_to(self.home/'missing')
        self.assertEqual(operations.new_skills(self.home,operations.load(self.home)[2]),[])

    def test_table_carries_display_labels_and_groups(self):
        (self.home/'.skill-pilot/labels.json').write_text(json.dumps({'groups': [
            {'name': '提问', 'skills': {'grilling': ['追问想法', '帮你把想法问清楚'],
                                        'other': {'title': '其他', 'summary': '别的', 'description': '中文说明', 'tags': ['杂项']}}}]}))
        view=operations.table(self.home)
        rows={row['name']:row for row in view['skills']}
        self.assertTrue(rows['grilling']['title'] and rows['grilling']['summary'])
        self.assertEqual((rows['grilling']['description_zh'],rows['grilling']['tags']),('',[]))  # 旧格式只有名字和一句话
        self.assertEqual((rows['other']['description_zh'],rows['other']['tags']),('中文说明',[]))  # 旧 AI 标签不显示
        self.assertEqual((rows['grill-me']['title'],rows['grill-me']['summary'],rows['grill-me']['tags']),('','',[]))
        self.assertTrue(any('grilling' in group['skills'] for group in view['groups']))
        self.assertEqual(view['labels_pending'],2)  # other 有中文名和说明，没有用户标签也不算待整理

    def test_token_estimate_in_table_agents_and_assignment_preview(self):
        cost={n: tokens(n+' 示例技能') for n in ('grill-me','grilling','other')}
        self.assign('grill-me','claude'); self.assign('other','claude')
        view=operations.table(self.home)
        self.assertEqual({r['name']: r['description_tokens'] for r in view['skills']},cost)
        self.assertTrue(all('description_chars' not in r for r in view['skills']) and all('description_chars' not in a for a in view['agents']))
        agents={a['id']: a for a in view['agents']}
        self.assertEqual((agents['claude']['count'],agents['claude']['description_tokens']),(2,cost['grill-me']+cost['other']))
        after=operations.preview_assign([{'skill':'grilling','agent':'claude','enabled':True}],self.home)['after']
        self.assertEqual(after['claude'],{'count':3,'description_tokens':sum(cost.values())})

    def test_skill_labels_list_each_skill_once_with_title_and_summary(self):
        path=self.home/'.skill-pilot/labels.json'
        path.write_text(json.dumps({'groups': [{'name': '提问', 'skills': {
            'grilling': ['追问想法', '把想法问清楚'], 'grill-me': ['提问入口', '启动追问']}}]}))
        groups=operations.labels.read(self.home)
        names=[name for group in groups for name in group['skills']]
        self.assertEqual(len(names),len(set(names)))
        group_names=[group['name'] for group in groups]
        self.assertEqual(len(group_names),len(set(group_names)))
        self.assertNotIn('未分组',group_names)  # 看板用它放没写进对照表的技能
        for group in groups:
            for name,label in group['skills'].items():
                self.assertEqual(set(label),{'title','summary','description'},name)
                self.assertTrue(label['title'] and label['summary'],name)

    @patch.object(operations, 'curated_dependencies', REAL_CURATED)
    def test_curated_writer_dependencies_expand_transitively_without_prompts(self):
        names=['ascentiz-blog-writer','natural-text-writing','unslop','stss','ascentiz-blog-editor']
        for name in names:
            self.skill('.codex/skills/'+name,name,'正文')
        with patch.object(github,'verify_source',return_value={'source':'自制'}):
            operations.absorb(all_skills=True,home=self.home)
        view=operations.table(self.home)
        plan=operations.preview_assign([{'skill':'ascentiz-blog-writer','agent':'claude','enabled':True}],home=self.home)
        self.assertTrue(all('claude' in plan['assignments'][n] for n in names))

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'skill_pilot', *args], cwd=store.PROJECT,
                              env={**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'},
                              capture_output=True, text=True, timeout=20)

    def test_local_update_of_homemade_skill_is_confirmed_and_undoable(self):
        registry_path = self.home / '.skill-pilot/registry.json'
        registry = json.loads(registry_path.read_text())
        registry['skills']['other']['source'] = {'source': '自制'}
        registry['skills']['grilling']['source'] = {'source': 'https://github.com/example/skills', 'source_path': 'grilling'}
        registry_path.write_text(json.dumps(registry))
        incoming = self.home / 'Drafts/other skill'
        incoming.mkdir(parents=True)
        (incoming / 'SKILL.md').write_text('---\nname: other\ndescription: 新说明\n---\n新正文\n')
        (incoming / 'notes.txt').write_text('资料\n')
        before = self.fingerprint()
        preview = self.command('update-local', 'other', str(incoming))
        self.assertEqual(preview.returncode, 0, preview.stderr + preview.stdout)
        self.assertIn('请核对', preview.stdout)
        self.assertEqual(self.fingerprint(), before)
        rejected = self.command('update-local', 'grilling', str(incoming), '--yes')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('上游', rejected.stdout + rejected.stderr)
        self.assertEqual(self.fingerprint(), before)
        updated = self.command('update-local', 'other', str(incoming), '--yes', '--json')
        self.assertEqual(updated.returncode, 0, updated.stderr + updated.stdout)
        record = json.loads(updated.stdout)
        self.assertIn('新正文', (self.home / '.skill-pilot/store/other/SKILL.md').read_text())
        self.assertEqual((self.home / '.skill-pilot/store/other/notes.txt').read_text(), '资料\n')
        undone = self.command('undo', record['id'], '--json')
        self.assertEqual(undone.returncode, 0, undone.stderr + undone.stdout)
        self.assertEqual(self.fingerprint(), before)
        self.assertNotIn('新正文', (self.home / '.skill-pilot/store/other/SKILL.md').read_text())

