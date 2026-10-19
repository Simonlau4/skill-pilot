from tests import set_up_home
from concurrent.futures import ThreadPoolExecutor
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from skill_pilot import labels, operations, store, tags
from skill_pilot.web.server import Dashboard


def fixture(case):
    temporary = tempfile.TemporaryDirectory(prefix='skill pilot user tags ')
    case.addCleanup(temporary.cleanup)
    set_up_home(case, Path(temporary.name) / 'home with spaces')
    for name in ('alpha', 'beta', 'gamma'):
        path = case.home / '.codex/skills' / name
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例技能\n---\n正文\n')
    plan = store.preview(case.home)
    store.apply(plan['plan_id'], case.home)
    case.path = case.home / '.skill-pilot/tags.json'


class UserTagsTests(unittest.TestCase):
    def setUp(self):
        fixture(self)

    def test_missing_file_and_removing_absent_tag_are_empty_without_a_write(self):
        self.assertEqual(tags.read(self.home), {})
        self.assertTrue(all(row['tags'] == [] for row in operations.table(self.home)['skills']))
        self.assertEqual(tags.update(self.home, {'skills': ['alpha'], 'remove': '博客'}), {'tags': {'alpha': []}})
        self.assertFalse(self.path.exists())

    def test_normalization_length_order_and_case_reuse_across_skills(self):
        tags.update(self.home, {'skills': ['alpha'], 'add': '  # Acme  '})
        self.assertEqual(tags.update(self.home, {'skills': ['beta'], 'add': 'acme'}),
                         {'tags': {'beta': ['Acme']}})
        before = self.path.read_bytes()
        tags.update(self.home, {'skills': ['alpha', 'beta'], 'add': '＃ ACME '})
        self.assertEqual(self.path.read_bytes(), before)
        for value in (' ＃ 博客 \t', ' # ＃ # 博客 ', '字', '字' * 20, '# 内部 空格 '):
            tags.update(self.home, {'skills': ['alpha'], 'add': value})
        self.assertEqual(tags.read(self.home)['alpha'], ['Acme', '博客', '字', '字' * 20, '内部 空格'])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved, {'version': 1, 'skills': tags.read(self.home)})
        self.assertEqual(list(self.path.parent.glob('.tags.json.*')), [])

    def test_invalid_tag_values_are_rejected_without_changing_bytes(self):
        tags.update(self.home, {'skills': ['alpha'], 'add': '博客'})
        before = self.path.read_bytes()
        for value in ('', '  ', '#', ' ＃ ', '字' * 21, None, 3, True, [], {}):
            for action in ('add', 'remove'):
                with self.subTest(value=value, action=action):
                    with self.assertRaisesRegex(ValueError, '标签'):
                        tags.update(self.home, {'skills': ['alpha'], action: value})
                    self.assertEqual(self.path.read_bytes(), before)

    def test_control_and_invisible_characters_are_rejected_without_changing_bytes(self):
        tags.update(self.home, {'skills': ['alpha'], 'add': '博客'})
        before = self.path.read_bytes()
        for value in ('a\nb', '\x00', '\u200b', 'a\tb', 'a\u2028b', 'a\u2029b'):
            for action in ('add', 'remove'):
                with self.subTest(value=value, action=action):
                    with self.assertRaisesRegex(ValueError, '标签里不能有换行或看不见的字符'):
                        tags.update(self.home, {'skills': ['alpha'], action: value})
                    self.assertEqual(self.path.read_bytes(), before)

    def test_batch_add_remove_and_readding_preserve_order_and_limit_response_scope(self):
        tags.update(self.home, {'skills': ['gamma'], 'add': '其他'})
        self.assertEqual(tags.update(self.home, {'skills': ['alpha', 'beta'], 'add': '博客'}),
                         {'tags': {'alpha': ['博客'], 'beta': ['博客']}})
        tags.update(self.home, {'skills': ['alpha'], 'add': 'Acme'})
        tags.update(self.home, {'skills': ['alpha'], 'add': '独立站'})
        self.assertEqual(tags.update(self.home, {'skills': ['alpha', 'beta'], 'remove': 'acme'}),
                         {'tags': {'alpha': ['博客', '独立站'], 'beta': ['博客']}})
        tags.update(self.home, {'skills': ['alpha'], 'add': 'Acme'})
        self.assertEqual(tags.read(self.home), {'gamma': ['其他'], 'alpha': ['博客', '独立站', 'Acme'], 'beta': ['博客']})

    def test_invalid_selection_and_action_reject_the_whole_batch(self):
        tags.update(self.home, {'skills': ['alpha'], 'add': '博客'})
        before = self.path.read_bytes()
        bodies = [None, [], {}, {'skills': ['alpha']}, {'skills': ['alpha'], 'add': '甲', 'remove': '乙'}]
        bodies += [{'skills': names, 'add': '新标签'} for names in (
            None, [], 'alpha', {}, [None], [1], [''], ['alpha', 'alpha'], ['beta', 'unknown'])]
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    tags.update(self.home, body)
                self.assertEqual(self.path.read_bytes(), before)

    def test_ten_tag_limit_rejects_entire_batch_and_allows_existing_tag(self):
        self.path.write_text(json.dumps({'version': 1, 'skills': {'alpha': [f'标签{i}' for i in range(10)]}}))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'alpha.*10'):
            tags.update(self.home, {'skills': ['beta', 'alpha'], 'add': '新标签'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(tags.update(self.home, {'skills': ['alpha'], 'add': '# 标签0'}),
                         {'tags': {'alpha': [f'标签{i}' for i in range(10)]}})
        self.assertEqual(self.path.read_bytes(), before)
        tags.update(self.home, {'skills': ['alpha'], 'remove': '标签0'})
        tags.update(self.home, {'skills': ['alpha', 'beta'], 'add': '新标签'})
        self.assertEqual(tags.read(self.home)['alpha'], [f'标签{i}' for i in range(1, 10)] + ['新标签'])

    def test_readonly_refuses_creation_and_preserves_existing_file_bytes(self):
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            with self.assertRaisesRegex(ValueError, '只读'):
                tags.update(self.home, {'skills': ['alpha'], 'add': '博客'})
        self.assertFalse(self.path.exists())
        tags.update(self.home, {'skills': ['alpha'], 'add': '博客'})
        before = self.path.read_bytes()
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertEqual(tags.read(self.home), {'alpha': ['博客']})
            for action in ('add', 'remove'):
                with self.assertRaisesRegex(ValueError, '只读'):
                    tags.update(self.home, {'skills': ['alpha'], action: '博客'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_file_and_directory_links_outside_home_are_refused(self):
        with tempfile.TemporaryDirectory(prefix='skill pilot outside ') as temporary:
            outside = Path(temporary)
            external_file = outside / 'tags.json'
            external_file.write_text('{"version":1,"skills":{"alpha":["保留"]}}\n')
            before = external_file.read_bytes()
            self.path.symlink_to(external_file)
            with self.assertRaisesRegex(ValueError, '之外'):
                tags.update(self.home, {'skills': ['alpha'], 'add': '博客'})
            self.assertTrue(self.path.is_symlink())
            self.assertEqual(external_file.read_bytes(), before)
            other_home = self.home / 'another home'
            other_home.mkdir()
            (other_home / '.skill-pilot').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, '之外'):
                tags.update(other_home, {'skills': ['alpha'], 'remove': '保留'})
            self.assertEqual(external_file.read_bytes(), before)

    def test_unreadable_file_displays_empty_but_add_and_remove_preserve_original_bytes(self):
        malformed = [b'{', b'\xff', b'null', b'[]']
        malformed += [json.dumps(value).encode() for value in (
            {}, {'version': 2, 'skills': {}}, {'version': True, 'skills': {}}, {'version': 1, 'skills': []},
            *({'version': 1, 'skills': {'alpha': value}} for value in (
                '博客', [None], [''], ['字' * 21], ['# 博客'], ['甲'] * 2, ['Acme', 'acme'],
                ['a\nb'], ['\x00'], ['\u200b'], ['a\u2028b'], ['a\u2029b'],
                [f'标签{i}' for i in range(11)])))]
        for content in malformed:
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assertEqual(tags.read(self.home), {})
                self.assertTrue(all(row['tags'] == [] for row in operations.table(self.home)['skills']))
                for action in ('add', 'remove'):
                    with self.assertRaisesRegex(ValueError, '标签文件读不懂.*保留原文件'):
                        tags.update(self.home, {'skills': ['alpha'], action: '博客'})
                    self.assertEqual(self.path.read_bytes(), content)

    def test_reorganizing_all_ai_labels_leaves_user_tags_byte_for_byte(self):
        tags.update(self.home, {'skills': ['alpha', 'beta'], 'add': '博客'})
        before = self.path.read_bytes()
        (self.path.parent / 'labels.json').write_text(json.dumps({'groups': [{'name': '旧组', 'skills': {
            'alpha': {'title': '旧名', 'summary': '旧说明', 'description': '旧中文说明', 'tags': ['旧 AI 标签']}}}]}))
        reply = {'skills': [{'name': name, 'title': '新名', 'summary': '新说明', 'description': '中文说明', 'group': '新组'}
                            for name in ('alpha', 'beta', 'gamma')]}
        progress = {'running': True}
        with patch.object(labels, 'completion', return_value=reply):
            labels.run(self.home, 'all', progress)
        self.assertEqual((progress['done'], progress['error']), (3, ''))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn('tags', (self.path.parent / 'labels.json').read_text())

    def test_removed_registry_entry_is_retained_hidden_and_restored_with_its_tags(self):
        tags.update(self.home, {'skills': ['alpha'], 'add': 'Acme'})
        before = self.path.read_bytes()
        registry_path = self.path.parent / 'registry.json'
        registry_bytes = registry_path.read_bytes()
        registry = json.loads(registry_bytes)
        del registry['skills']['alpha']
        registry_path.write_text(json.dumps(registry))
        self.assertNotIn('alpha', {row['name'] for row in operations.table(self.home)['skills']})
        self.assertEqual(tags.read(self.home)['alpha'], ['Acme'])
        with self.assertRaisesRegex(ValueError, '不在登记表'):
            tags.update(self.home, {'skills': ['alpha'], 'remove': 'Acme'})
        self.assertEqual(self.path.read_bytes(), before)
        tags.update(self.home, {'skills': ['beta'], 'add': 'acme'})
        self.assertEqual(tags.read(self.home)['alpha'], ['Acme'])
        self.assertEqual(tags.read(self.home)['beta'], ['Acme'])
        registry_path.write_bytes(registry_bytes)
        rows = {row['name']: row for row in operations.table(self.home)['skills']}
        self.assertEqual(rows['alpha']['tags'], ['Acme'])

    def test_table_uses_only_user_tags_and_does_not_mark_untagged_labels_pending(self):
        old = {'title': '中文名', 'summary': '一句话', 'description': '中文说明', 'tags': ['旧 AI 标签']}
        (self.path.parent / 'labels.json').write_text(json.dumps({'groups': [{'name': '分组', 'skills': {
            name: old for name in ('alpha', 'beta', 'gamma')}}]}))
        before = operations.table(self.home)
        self.assertEqual(before['labels_pending'], 0)
        self.assertTrue(all(row['tags'] == [] for row in before['skills']))
        tags.update(self.home, {'skills': ['beta'], 'add': '用户标签'})
        view = operations.table(self.home)
        self.assertEqual({r['name']: r['tags'] for r in view['skills']}, {'alpha': [], 'beta': ['用户标签'], 'gamma': []})
        self.assertEqual(view['revision'], before['revision'])

    def test_concurrent_edits_to_one_skill_keep_both_tags(self):
        original_read = tags._read
        entered, release, second_started, second_read = (threading.Event() for _ in range(4))

        def delayed_read(home):
            result = original_read(home)
            if not entered.is_set():
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('测试等待超时')
            else:
                second_read.set()
            return result

        def second_update():
            second_started.set()
            return tags.update(self.home, {'skills': ['alpha'], 'add': '第二个'})

        with patch.object(tags, '_read', side_effect=delayed_read), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(tags.update, self.home, {'skills': ['alpha'], 'add': '第一个'})
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(second_update)
                self.assertTrue(second_started.wait(2))
                self.assertFalse(second_read.wait(.1))
            finally:
                release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(tags.read(self.home)['alpha'], ['第一个', '第二个'])


class UserTagsWebTests(unittest.TestCase):
    def setUp(self):
        fixture(self)
        self.server = Dashboard({'home': str(self.home)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, body=None, path='/api/tags', extra=None):
        headers = {'X-Skill-Pilot-Token': self.server.token, 'Origin': self.server.origin, 'Content-Type': 'application/json'}
        headers.update(extra or {})
        request = urllib.request.Request(self.server.origin + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_http_single_and_batch_edits_and_table_readback(self):
        self.assertEqual(self.request({'skills': ['alpha', 'beta'], 'add': '# 博客'}),
                         (200, {'tags': {'alpha': ['博客'], 'beta': ['博客']}}))
        self.assertEqual(json.loads(self.path.read_text()), {'version': 1, 'skills': {'alpha': ['博客'], 'beta': ['博客']}})
        self.assertEqual(self.request({'skills': ['alpha'], 'remove': '博客'}), (200, {'tags': {'alpha': []}}))
        status, view = self.request(path='/api/table')
        self.assertEqual(status, 200)
        self.assertEqual({r['name']: r['tags'] for r in view['skills']}, {'alpha': [], 'beta': ['博客'], 'gamma': []})

    def test_http_invalid_edits_and_wrong_origin_leave_bytes_unchanged(self):
        self.request({'skills': ['alpha'], 'add': '博客'})
        before = self.path.read_bytes()
        for body in ({'skills': [], 'add': '新'}, {'skills': ['beta', 'unknown'], 'add': '新'},
                     {'skills': ['alpha'], 'add': ' '}, {'skills': ['alpha'], 'add': '新', 'remove': '旧'}):
            self.assertEqual(self.request(body)[0], 409)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.request({'skills': ['alpha'], 'remove': '博客'}, extra={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.path.read_bytes(), before)

    def test_http_readonly_uses_403_and_preserves_bytes(self):
        self.request({'skills': ['alpha'], 'add': '博客'})
        before = self.path.read_bytes()
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            self.assertEqual(self.request({'skills': ['alpha'], 'remove': '博客'}),
                             (403, {'error': '当前处于只读模式，文件没有改动。'}))
        self.assertEqual(self.path.read_bytes(), before)

    def test_http_unreadable_file_returns_error_and_preserves_bytes(self):
        self.path.write_bytes(b'{')
        status, result = self.request({'skills': ['alpha'], 'add': '博客'})
        self.assertEqual(status, 409)
        self.assertIn('为保留原文件', result['error'])
        self.assertEqual(self.path.read_bytes(), b'{')

    def test_http_reload_and_upgrade_keep_the_503_write_gate(self):
        self.request({'skills': ['alpha'], 'add': '博客'})
        before = self.path.read_bytes()
        with patch.object(self.server, 'reload_pending', True):
            self.assertEqual(self.request({'skills': ['alpha'], 'remove': '博客'})[0], 503)
        with patch.dict(self.server.program_progress, {'running': True, 'phase': 'apply'}):
            self.assertEqual(self.request({'skills': ['alpha'], 'remove': '博客'})[0], 503)
        self.assertEqual(self.path.read_bytes(), before)
