import sys
from skill_pilot.platform_support import rmtree
from tests import set_up_home
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot import operations, research, store, updates
from skill_pilot.sources import normalize, github, lark


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill pilot m4 test ')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        set_up_home(self, root / 'home')
        self.repo = root / 'upstream'
        self.repo.mkdir()
        self.folder = self.repo / 'skills/sample'
        self.folder.mkdir(parents=True)
        self.manifest = self.folder / 'SKILL.md'
        self.original = '---\nname: sample\ndescription: example\n---\nfirst\nlocal line\n'
        self.manifest.write_text(self.original, newline='\n')
        (self.folder / 'scripts').mkdir()
        (self.folder / 'scripts/task.py').write_text('raise RuntimeError("never run this script")\n')
        github.git(self.repo, 'init', '-q')
        self.commit('old')
        self.old = github.git(self.repo, 'rev-parse', 'HEAD')
        target = self.home / '.codex/skills/sample'
        target.parent.mkdir(parents=True)
        shutil.copytree(self.folder, target)
        (self.home / '.codex/config.toml').write_text('# preserve\n')
        plan = store.preview(self.home)
        store.apply(plan['plan_id'], self.home)
        registry_path = self.home / '.skill-pilot/registry.json'
        registry = json.loads(registry_path.read_text())
        registry['skills']['sample']['source'] = {'source': self.repo.as_uri(), 'repository': self.repo.as_uri(),
                                                 'source_path': 'skills/sample/SKILL.md'}
        registry['skills']['sample']['base_tree_sha'] = store.git_tree(self.home / '.skill-pilot/bases/sample')
        registry_path.write_text(json.dumps(registry))
        # Persist fixture metadata as a starting operation, like an installed skill.
        operations._commit(self.home, registry, ['sample'], [], 'fixture source')
        self.manifest.write_text(self.original.replace('first', 'new upstream'))
        (self.folder / 'scripts/task.py').write_text('raise RuntimeError("new version; never run")\n')
        self.commit('new')

    def commit(self, message):
        github.git(self.repo, 'add', '.')
        github.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@local',
                   '-c', 'commit.gpgsign=false', 'commit', '-qm', message)

    def check(self):
        review = updates.check(self.home)
        self.addCleanup(shutil.rmtree, review['temporary'], True)
        self.assertFalse(review['errors'], review['errors'])
        return review

    def local(self):
        return self.home / '.skill-pilot/store/sample'

    def fingerprint(self):
        paths = ['.skill-pilot/registry.json', '.skill-pilot/bases', '.codex/config.toml',
                 '.codex/skills', '.agents/skills', '.claude/skills', '.gemini/config/skills', '.dsh/skills']
        return {p: store.physical_state(self.home / p) for p in paths} | {'skill': store.physical_state(self.local())}

    def test_old_version_is_unmodified_and_check_is_read_only(self):
        before = self.fingerprint()
        review = self.check(); row = review['skills'][0]
        self.assertTrue(row['has_update'])
        self.assertFalse(row['local_modified'])
        self.assertFalse(row['baseline_uncertain'])
        self.assertEqual(row['your_changes']['file_count'], 0)
        self.assertEqual(before, self.fingerprint())
        self.assertIn('scripts/task.py', row['script_files'])
        self.assertNotIn('_base_path', updates.public(review)['skills'][0])

    def test_direct_update_single_commit_and_exact_undo(self):
        before = self.fingerprint()
        head = github.git(self.home / '.skill-pilot/store', 'rev-parse', 'HEAD')
        result = updates.apply(self.check(), ['sample'], confirmed=True)
        self.assertIn('new upstream', (self.local() / 'SKILL.md').read_text())
        self.assertEqual(github.git(self.home / '.skill-pilot/store', 'rev-list', '--count', head + '..HEAD'), '1')
        self.assertFalse(self.check()['skills'][0]['has_update'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fingerprint())

    def modified(self):
        p = self.local() / 'SKILL.md'
        p.write_text(p.read_text().replace('local line', 'my local edit'))
        # Deliberately make the migration baseline the modified local copy.
        base = self.home / '.skill-pilot/bases/sample/SKILL.md'
        base.write_bytes(p.read_bytes())

    def test_closest_history_only_corrects_baseline_when_keep_selected(self):
        self.modified()
        review = self.check(); row = review['skills'][0]
        self.assertIsNone(row['local_modified'])
        updates.calibrate(review, 'sample')
        self.assertTrue(row['local_modified'])
        self.assertEqual(row['base_commit'], self.old)
        self.assertIn('my local edit', row['your_changes']['comparison'])
        self.assertLess(row['similarity'], 1)

    def test_modified_defaults_upstream_but_requires_confirmation(self):
        self.modified(); before = self.fingerprint(); review = self.check()
        with self.assertRaisesRegex(ValueError, '确认'):
            updates.apply(review, ['sample'])
        self.assertEqual(before, self.fingerprint())
        result = updates.apply(review, ['sample'], confirmed=True)
        self.assertNotIn('my local edit', (self.local() / 'SKILL.md').read_text())
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fingerprint())

    def test_keep_suppresses_this_version_and_undo_restores(self):
        self.modified(); before = self.fingerprint()
        result = updates.apply(self.check(), ['sample'], {'sample': 'keep'}, True)
        self.assertIn('my local edit', (self.local() / 'SKILL.md').read_text())
        self.assertFalse(self.check()['skills'][0]['has_update'])
        self.assertEqual(self.check()['skills'][0]['status'], '这一版已看过')
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fingerprint())
        self.assertTrue(self.check()['skills'][0]['has_update'])

    def test_use_upstream_replaces_local_edit(self):
        self.modified(); before = self.fingerprint()
        result = updates.apply(self.check(), ['sample'], {'sample': 'upstream'}, True)
        text = (self.local() / 'SKILL.md').read_text()
        self.assertIn('new upstream', text)
        self.assertNotIn('my local edit', text)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fingerprint())

    def mock_merge(self, material):
        items = {s['path']: s for s in material['upstream']}
        items['SKILL.md']['content'] = items['SKILL.md']['content'].replace('local line', 'my local edit')
        return {'files': list(items.values()), 'explanation': '保留你的修改，也加入上游新增内容。'}

    def test_merge_preview_needs_separate_confirmation(self):
        self.modified(); before = self.fingerprint(); review = self.check()
        with patch.object(research, 'merge', side_effect=self.mock_merge):
            merged = updates.prepare_merge(review, 'sample')
        self.assertEqual(before, self.fingerprint())
        self.assertIn('new upstream', merged['comparison'])
        with self.assertRaisesRegex(ValueError, '未确认'):
            updates.apply(review, ['sample'], {'sample': 'merge'}, True)
        result = updates.apply(review, ['sample'], {'sample': 'merge'}, True, {'sample': merged['merge_id']})
        text = (self.local() / 'SKILL.md').read_text()
        self.assertIn('my local edit', text); self.assertIn('new upstream', text)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fingerprint())

    def test_unsafe_and_changed_merge_results_rejected(self):
        review = self.check()
        with patch.object(research, 'merge', return_value={'files': [{'path': '../escape', 'content': 'bad', 'executable': False}], 'explanation': 'bad'}):
            with self.assertRaisesRegex(ValueError, '不安全'):
                updates.prepare_merge(review, 'sample')
        with patch.object(research, 'merge', side_effect=self.mock_merge):
            merged = updates.prepare_merge(review, 'sample')
        Path(review['skills'][0]['_merge']['path'], 'SKILL.md').write_text('tampered')
        with self.assertRaisesRegex(ValueError, '已经变了'):
            updates.apply(review, ['sample'], {'sample': 'merge'}, True, {'sample': merged['merge_id']})

    @unittest.skipIf(os.name == 'nt', 'Windows has no POSIX executable permission bits')
    def test_external_mode_change_invalidates_review(self):
        review = self.check()
        (self.local() / 'SKILL.md').chmod(0o755)
        with self.assertRaisesRegex(ValueError, '已经变了'):
            updates.apply(review, ['sample'], confirmed=True)

    def test_existing_private_and_shared_links_stay_exactly_in_place(self):
        registry = operations.load(self.home)[1]
        skill = registry['skills']['sample']
        shared = '.agents/skills/sample'
        (self.home / shared).parent.mkdir(parents=True, exist_ok=True)
        (self.home / shared).symlink_to(str(self.local()), target_is_directory=True)
        skill['entries'].append(shared); skill['assigned_agents'].append('dsh')
        store.write_json(self.home / '.skill-pilot/registry.json', registry, lambda p: store.guard(p, self.home))
        operations._commit(self.home, registry, ['sample'], [], 'two existing roots')
        roots = ['.codex/skills', '.agents/skills']
        before = {p: store.physical_state(self.home / p) for p in roots}
        updates.apply(self.check(), ['sample'], confirmed=True)
        self.assertEqual(before, {p: store.physical_state(self.home / p) for p in roots})
        self.assertEqual(skill['entries'], operations.load(self.home)[1]['skills']['sample']['entries'])

    @unittest.skipIf(os.name == 'nt', 'Windows has no POSIX executable permission bits')
    def test_mode_only_and_nonstandard_script_detection(self):
        a, b = self.home / 'one', self.home / 'two'
        a.mkdir(); b.mkdir()
        for name in ['run', 'thing.ts', 'thing.mjs', 'thing.rb']:
            (a / name).write_text('old\n'); (b / name).write_text('new\n')
        (a / 'permission').write_text('same'); (b / 'permission').write_text('same'); (b / 'permission').chmod(0o755)
        (b / 'run').chmod(0o755)
        diff = updates.comparison(a, b)
        self.assertEqual(len(diff['script_files']), 5)
        self.assertIn('执行权限', diff['comparison'])

    def test_explanation_only_passes_comparison(self):
        review = self.check()
        with patch.object(research, 'explain', return_value='三句说明') as fake:
            self.assertEqual(updates.explain(review, 'sample')['explanation'], '三句说明')
            self.assertNotIn(str(self.home), str(fake.call_args))

    def test_source_failure_is_reported_not_latest(self):
        with patch('skill_pilot.sources.update_github.Reader.current', side_effect=ValueError('查询失败')):
            result = updates.check(self.home)
            self.addCleanup(shutil.rmtree, result['temporary'], True)
        self.assertTrue(result['errors']); self.assertEqual(result['skills'][0]['status'], '没查清楚')

    def test_external_upstream_link_is_reported_without_installing(self):
        (self.folder / 'data').symlink_to('../../../outside')
        self.commit('external compatibility link')
        before = self.fingerprint()
        row = self.check()['skills'][0]
        self.assertTrue(row['has_update']); self.assertIn('文件夹外', row['blocked_reason'])
        review = self.check()
        with self.assertRaisesRegex(ValueError, '停止安装'):
            updates.apply(review, ['sample'], confirmed=True)
        self.assertEqual(before, self.fingerprint())

    def test_batch_rolls_back_on_validation_failure(self):
        before = self.fingerprint(); review = self.check()
        with patch.object(operations, '_validate', side_effect=ValueError('verification failed')):
            with self.assertRaises(ValueError): updates.apply(review, ['sample'], confirmed=True)
        self.assertEqual(before, self.fingerprint())

    def test_cli_check_readonly_and_update_requires_yes(self):
        before = self.fingerprint()
        env = {**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-m', 'skill_pilot', 'check-updates', '--json'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['update_count'], 1)
        result = subprocess.run([sys.executable, '-m', 'skill_pilot', 'update', 'sample', '--json'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1); self.assertIn('明确同意', result.stdout)
        self.assertEqual(before, self.fingerprint())

    def test_undo_update_after_independent_assignment_restores_base(self):
        self.modified(); before_base = store.physical_state(self.home / '.skill-pilot/bases/sample')
        result = updates.apply(self.check(), ['sample'], {'sample': 'upstream'}, True)
        # Add another managed skill, then make an independent assignment.
        path = self.repo / 'another'
        path.mkdir(); (path / 'SKILL.md').write_text('---\nname: another\ndescription: other\n---\nbody\n')
        self.commit('another')
        review = github.inspect(self.repo.as_uri())
        try:
            operations.install(review, ['another'], home=self.home)
        finally: rmtree(review['temporary'])
        operations.undo(result['id'], home=self.home)
        self.assertEqual(before_base, store.physical_state(self.home / '.skill-pilot/bases/sample'))
        self.assertIn('my local edit', (self.local() / 'SKILL.md').read_text())
        self.assertIn('another', operations.load(self.home)[1]['skills'])

    def test_undo_rejects_later_assignment_of_the_same_skill(self):
        result = updates.apply(self.check(), ['sample'], confirmed=True)
        later = operations.apply_assign([{'skill': 'sample', 'agent': 'claude', 'enabled': True}], home=self.home)
        before = self.fingerprint()
        with self.assertRaisesRegex(ValueError, '先撤销后来的'):
            operations.undo(result['id'], home=self.home)
        self.assertEqual(before, self.fingerprint())
        operations.undo(later['id'], home=self.home)
        operations.undo(result['id'], home=self.home)
        self.assertEqual((self.local() / 'SKILL.md').read_text(), self.original)

    def test_real_home_update_rejected_without_subprocess(self):
        review = self.check()
        with patch.object(updates.operations, 'load', return_value=(Path('/Users/example'), {}, [])), \
                patch('skill_pilot.store.write_allowed', return_value=False), \
                patch.object(lark, 'install_cli') as install:
            with self.assertRaisesRegex(ValueError, '只读'):
                updates.apply(review, ['sample'], confirmed=True)
            install.assert_not_called()


class NormalizationTests(unittest.TestCase):
    def test_all_source_forms(self):
        cases = [
            ({'source': '[owner/repo](https://github.com/owner/repo)', 'source_path': 'skills/demo/SKILL.md'}, 'github', 'skills/demo'),
            ({'source': 'https://github.com/owner/repo.git', 'source_path': '.'}, 'github', '.'),
            ({'record': {'sourceType': 'github', 'source': 'owner/repo', 'skillPath': 'SKILL.md'}}, 'github', '.'),
            ({'sourceType': 'github', 'sourceUrl': 'https://github.com/owner/repo', 'skillPath': 'demo'}, 'github', 'demo'),
            ({'record': {'sourceType': 'well-known', 'sourceUrl': 'https://open.feishu.cn/lark-cli/skills/regular/.well-known/agent-skills/lark-doc.tar.gz'}}, 'lark', None)]
        for source, kind, path in cases:
            with self.subTest(source=source):
                normalized = normalize(source); self.assertEqual(normalized['kind'], kind)
                if path: self.assertEqual(normalized['path'], path)
        skipped = [
            ({'source': '自制'}, 'self_made', '自制，不检查更新'),
            ({'source': '本机源码'}, 'local', '本机维护，不检查外部更新'),
            ({'source': '本机文档生成'}, 'local', '本机维护，不检查外部更新'),
            ({'source': '来源不明'}, 'unknown', '来源未记录'),
            ({}, 'unknown', '来源未记录'),
            ({'source': '没有证据'}, 'unsupported', '暂不支持检查此来源'),
            ({'record': {'sourceType': 'well-known', 'sourceUrl': 'https://example.test/skills/demo'}},
             'unsupported', '暂不支持检查此来源'),
        ]
        for source, reason, label in skipped:
            with self.subTest(source=source):
                self.assertEqual(normalize(source), {'kind': 'skip', 'label': label, 'skip_reason': reason})
        with self.assertRaises(ValueError): normalize({'source': 'https://github.com/o/r', 'source_path': '../bad'})


class LarkTests(unittest.TestCase):
    def test_latest_and_update_available(self):
        for action in ['already_up_to_date', 'update_available']:
            stdout=json.dumps({'ok': True, 'action': action, 'current_version': '1.0.97', 'latest_version': '1.0.98'})
            with patch.object(lark.shutil, 'which', return_value='/temporary user/bin/lark-cli'), \
                    patch.object(lark.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout, '')) as fake:
                data = lark.check()
                self.assertEqual(data['available'], action == 'update_available')
                self.assertEqual(fake.call_args.args[0][-3:], ['update', '--check', '--json'])

    def archive(self, unsafe=False):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w:gz') as archive:
            text = b'---\nname: lark-doc\ndescription: demo\n---\nnew official\n'
            member = tarfile.TarInfo('../escape' if unsafe else 'SKILL.md'); member.size = len(text)
            archive.addfile(member, io.BytesIO(text))
        return out.getvalue()

    def test_download_digest_and_safe_extraction(self):
        import hashlib
        for bad_digest, unsafe in [(False,False), (True,False), (False,True)]:
            with tempfile.TemporaryDirectory() as temporary:
                archive=self.archive(unsafe)
                index={'skills':[{'name':'lark-doc','type':'archive','url':'./lark-doc.tar.gz','digest':'sha256:'+('0'*64 if bad_digest else hashlib.sha256(archive).hexdigest())}]}
                with patch.object(lark,'download',side_effect=[json.dumps(index).encode(),archive]):
                    if bad_digest or unsafe:
                        with self.assertRaises((ValueError,tarfile.FilterError)):
                            lark.prepare({'lark-doc': {'base_url':'https://open.feishu.cn/lark-cli/skills/regular'}},temporary,'1.0.98')
                    else:
                        result=lark.prepare({'lark-doc': {'base_url':'https://open.feishu.cn/lark-cli/skills/regular'}},temporary,'1.0.98')
                        self.assertTrue(Path(result['lark-doc']['path'],'SKILL.md').exists())

    def test_full_isolated_cli_and_skill_update_and_undo(self):
        import hashlib
        with tempfile.TemporaryDirectory(prefix='skill-pilot-m4-lark-') as temporary:
            home = Path(temporary).resolve() / 'home'
            path = home / '.codex/skills/lark-doc'
            path.mkdir(parents=True)
            (path / 'SKILL.md').write_text('---\nname: lark-doc\ndescription: demo\n---\nold official\n')
            (home / '.codex/config.toml').write_text('# keep\n')
            plan = store.preview(home); store.apply(plan['plan_id'], home)
            registry = operations.load(home)[1]
            base_url = 'https://open.feishu.cn/lark-cli/skills/regular'
            registry['skills']['lark-doc']['source'] = {'record': {'sourceType': 'well-known',
                'sourceUrl': base_url+'/.well-known/agent-skills/lark-doc.tar.gz', 'sourceBaseUrl': base_url}}
            store.write_json(home / '.skill-pilot/registry.json', registry, lambda p: store.guard(p, home))
            operations._commit(home, registry, ['lark-doc'], [], 'fixture')
            unused = home / '.gemini/antigravity/skills'
            unused.mkdir(parents=True); (unused / 'keep').write_text('unmanaged original')
            archive = self.archive()
            index = json.dumps({'skills':[{'name':'lark-doc','type':'archive','url':'./lark-doc.tar.gz',
                                         'digest':'sha256:'+hashlib.sha256(archive).hexdigest()}]}).encode()
            snapshot_paths = ['.codex/skills','.agents/skills','.claude/skills','.gemini/config/skills',
                              '.skill-pilot/bases','.skill-pilot/registry.json',*lark.CLI_PATHS,str(unused.relative_to(home))]
            before = {p:store.physical_state(home/p) for p in snapshot_paths}
            check_result={'available':True,'latest_version':'1.0.98','current_version':'1.0.97','message':'lark-cli 有新版（含 28 个飞书技能）'}
            def fake_npm(command, **kwargs):
                self.assertEqual(command,['npm','install','-g','@larksuite/cli@1.0.98'])
                root=Path(kwargs['env']['HOME'])
                self.assertNotEqual(root,home)
                package=root/'prefix/lib/node_modules/@larksuite/cli'
                (package/'scripts').mkdir(parents=True)
                (package/'package.json').write_text('{"version":"1.0.98"}')
                (package/'scripts/run.js').write_text('cli replacement')
                # Even if a lifecycle program puts entries in its own home,
                # they are discarded with the staging workspace.
                extra=root/'.gemini/antigravity/skills/lark-doc'
                extra.mkdir(parents=True); (extra/'SKILL.md').write_text('extra')
                return subprocess.CompletedProcess(command,0,b'',b'')
            with patch.object(lark,'check',return_value=check_result), \
                    patch.object(lark,'download',side_effect=lambda url, **kwargs:index if url.endswith('index.json') else archive):
                review=updates.check(home)
            self.addCleanup(shutil.rmtree,review['temporary'],True)
            self.assertFalse(review['errors'],review['errors'])
            self.assertTrue(review['skills'][0]['has_update'])
            with self.assertRaisesRegex(ValueError,'单独确认'):
                updates.apply(review,['lark-doc'],confirmed=True)
            if os.name == 'nt':
                with self.assertRaisesRegex(ValueError, '不自动升级飞书 CLI'):
                    updates.apply(review, ['lark-doc'], confirmed=True, confirm_lark=True)
                self.assertEqual(before, {p: store.physical_state(home/p) for p in snapshot_paths})
                return
            real_run=subprocess.run
            def dispatch(command, **kwargs):
                return fake_npm(command,**kwargs) if command[0]=='npm' else real_run(command,**kwargs)
            with patch.object(lark.subprocess,'run',side_effect=dispatch):
                result=updates.apply(review,['lark-doc'],confirmed=True,confirm_lark=True)
            self.assertIn('new official',(home/'.skill-pilot/store/lark-doc/SKILL.md').read_text())
            self.assertEqual(before[str(unused.relative_to(home))],store.physical_state(unused))
            self.assertTrue((home/lark.CLI_PATHS[1]).is_symlink())
            operations.undo(result['id'],home=home)
            self.assertEqual(before,{p:store.physical_state(home/p) for p in snapshot_paths})

class UpdateWebTests(unittest.TestCase):
    def setUp(self):
        import threading
        from skill_pilot.web.server import Dashboard
        self.fixture = UpdateTests(methodName='test_old_version_is_unmodified_and_check_is_read_only')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.server = Dashboard({'home': str(self.fixture.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.05})
        self.thread.start(); self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        for review in self.server.update_reviews.values(): rmtree(review['temporary'], ignore_errors=True)

    def request(self, path, body=None):
        import urllib.request, urllib.error
        request = urllib.request.Request(self.server.origin+path,data=json.dumps(body).encode() if body is not None else None,headers={
            'X-Skill-Pilot-Token':self.server.token,'Origin':self.server.origin,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=15 if os.name == "nt" else 3) as r: return r.status,json.load(r)
        except urllib.error.HTTPError as e: return e.code,json.load(e)

    def wait_review(self):
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, review = self.request('/api/updates/progress')
            if review and not review['running']: return review
            time.sleep(.01)
        self.fail('后台检查没有完成')

    def test_update_check_only_on_request_and_confirmed_apply(self):
        before=self.fixture.fingerprint()
        self.assertEqual(self.server.update_reviews,{})
        status,review=self.request('/api/updates/check',{})
        self.assertEqual(status,200);self.assertEqual(before,self.fixture.fingerprint())
        review=self.wait_review()
        body={'review_id':review['id'],'skills':['sample']}
        self.assertEqual(self.request('/api/updates/apply',body)[0],400)
        with patch('skill_pilot.web.server.preview_allowed',return_value=False):
            self.assertEqual(self.request('/api/updates/apply',{**body,'confirmed':True})[0],403)
        status,result=self.request('/api/updates/apply',{**body,'confirmed':True})
        self.assertEqual(status,200,result)
        self.assertIn('new upstream',(self.fixture.local()/'SKILL.md').read_text())

    def test_undo_brings_the_update_mark_back_without_checking_again(self):
        self.request('/api/updates/check', {})
        review = self.wait_review()
        self.assertTrue(any(row['name'] == 'sample' and row['has_update'] for row in review['skills']))
        status, result = self.request('/api/updates/apply', {
            'review_id': review['id'], 'skills': ['sample'], 'confirmed': True})
        self.assertEqual(status, 200, result)
        _, progress = self.request('/api/updates/progress')
        row = next(item for item in progress['skills'] if item['name'] == 'sample')
        self.assertFalse(row['has_update'])
        _, table = self.request('/api/table')
        status, undone = self.request('/api/undo', {
            'id': result['id'], 'revision': table['revision'], 'confirmed': True})
        self.assertEqual(status, 200, undone)
        _, progress = self.request('/api/updates/progress')
        row = next(item for item in progress['skills'] if item['name'] == 'sample')
        self.assertTrue(row['has_update'])
        self.assertEqual(row['status'], '有更新')

    def test_undoing_a_later_assignment_does_not_restore_the_update_mark(self):
        self.request('/api/updates/check', {})
        review = self.wait_review()
        status, updated = self.request('/api/updates/apply', {
            'review_id': review['id'], 'skills': ['sample'], 'confirmed': True})
        self.assertEqual(status, 200, updated)
        _, table = self.request('/api/table')
        status, assigned = self.request('/api/assignment/apply', {
            'changes': [{'skill': 'sample', 'agent': 'claude', 'enabled': True}],
            'revision': table['revision'], 'confirmed': True})
        self.assertEqual(status, 200, assigned)
        self.assertNotEqual(assigned['id'], updated['id'])
        _, table = self.request('/api/table')
        status, undone = self.request('/api/undo', {
            'id': assigned['id'], 'revision': table['revision'], 'confirmed': True})
        self.assertEqual(status, 200, undone)
        _, progress = self.request('/api/updates/progress')
        row = next(item for item in progress['skills'] if item['name'] == 'sample')
        self.assertFalse(row['has_update'])
        self.assertEqual(row['status'], '已更新')
        _, table = self.request('/api/table')
        status, undone = self.request('/api/undo', {
            'id': updated['id'], 'revision': table['revision'], 'confirmed': True})
        self.assertEqual(status, 200, undone)
        _, progress = self.request('/api/updates/progress')
        row = next(item for item in progress['skills'] if item['name'] == 'sample')
        self.assertTrue(row['has_update'])
        self.assertEqual(row['status'], '有更新')

    def test_merge_endpoint_is_readonly_and_needs_review_id(self):
        self.fixture.modified();before=self.fixture.fingerprint()
        _,review=self.request('/api/updates/check',{})
        review=self.wait_review()
        with patch.object(research,'merge',side_effect=self.fixture.mock_merge):
            status,merged=self.request('/api/updates/merge',{'review_id':review['id'],'name':'sample'})
        self.assertEqual(status,200,merged);self.assertEqual(before,self.fixture.fingerprint())
        body={'confirmed':True,'review_id':review['id'],'skills':['sample'],'choices':{'sample':'merge'}}
        self.assertEqual(self.request('/api/updates/apply',body)[0],409)
        self.assertEqual(self.request('/api/updates/apply',{**body,'merge_ids':{'sample':merged['merge_id']}})[0],200)

    def test_api_commit_is_not_root_tree_and_cache_avoids_repeat(self):
        from skill_pilot.sources.update_github import Reader
        with tempfile.TemporaryDirectory() as temporary:
            reader=Reader(temporary)
            with patch.object(github,'api',return_value={'tree':{'sha':'directory-tree'}}) as api:
                source={'repository':'owner/repo','path':'.'}
                self.assertEqual(reader.tree(source,'commit-sha'),'directory-tree')
                self.assertEqual(reader.tree(source,'commit-sha'),'directory-tree')
                self.assertEqual(api.call_count,1)


if __name__ == '__main__':
    unittest.main()
