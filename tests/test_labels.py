from tests import set_up_home
import importlib.util
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from skill_pilot import labels, operations, store
from skill_pilot.web.server import Dashboard

KEY = 'fixture-secret-1234'


class FakeAI(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.calls, self.replies = [], []
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        super().__init__(('127.0.0.1', 0), FakeHandler)
        self.url = f'http://127.0.0.1:{self.server_port}/v1'


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': body})
        self.server.entered.set()
        self.server.release.wait(5)
        payload = json.loads(body['messages'][1]['content'])
        result = {'ok': True} if 'skills' not in payload else {'skills': [
            {'name': s['name'], 'title': '技能中文名', 'summary': '帮你整理材料', 'group': '整理',
             'description': '中文说明'} for s in payload['skills']]}
        reply = self.server.replies.pop(0) if self.server.replies else json.dumps(result, ensure_ascii=False)
        status = reply if isinstance(reply, int) else 200
        self.send_response(status)
        if status == 302:
            self.send_header('Location', self.server.url + '/redirected')
        self.end_headers()
        if reply is None:  # 排队时只发空行，直到客户端断开。
            try:
                while True:
                    self.wfile.write(b'\n')
                    self.wfile.flush()
                    time.sleep(.05)
            except (BrokenPipeError, ConnectionResetError):
                return
        self.wfile.write(reply if isinstance(reply, bytes) else
                         json.dumps({'choices': [{'message': {'content': reply}}]}).encode())


def fixture(case, count=3, description='材料' * 500):
    temporary = tempfile.TemporaryDirectory(prefix='skill-pilot-labels-')
    case.addCleanup(temporary.cleanup)
    set_up_home(case, temporary.name)
    for i in range(count):
        skill = case.home / f'.codex/skills/skill-{i:02}'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: {skill.name}\ndescription: {description}\n---\n正文不发送\n')
    plan = store.preview(case.home)
    store.apply(plan['plan_id'], case.home)
    case.ai = FakeAI()
    thread = threading.Thread(target=case.ai.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    def close():
        case.ai.release.set()
        case.ai.shutdown()
        thread.join()
        case.ai.server_close()
    case.addCleanup(close)
    case.config = {'provider': 'other', 'base_url': case.ai.url + '/', 'model': 'fixture-model', 'api_key': KEY}


class LabelsTests(unittest.TestCase):
    def setUp(self):
        fixture(self)

    def run_labels(self, mode='missing'):
        progress = {'running': True, 'done': 0, 'total': 0, 'message': '', 'error': ''}
        labels.run(self.home, mode, progress)
        self.assertFalse(progress['running'])
        return progress

    def configure(self):
        return labels.save_settings(self.home, self.config)

    def write_labels(self, groups):
        path = self.home / '.skill-pilot/labels.json'
        path.write_text(json.dumps({'groups': groups}))
        return path

    def test_settings_permissions_and_private_readback(self):
        result = self.configure()
        path = self.home / '.skill-pilot/settings.json'
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(result['llm']['key_tail'], '1234')
        self.assertTrue(result['llm']['has_key'])
        path.chmod(0o644)
        labels.save_settings(self.home, {**self.config, 'api_key': ''})
        self.assertEqual(labels.settings(self.home)['api_key'], KEY)
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        body = {k: v for k, v in self.config.items() if k != 'api_key'}
        labels.save_settings(self.home, body)
        self.assertEqual(labels.settings(self.home)['api_key'], KEY)
        labels.save_settings(self.home, {**body, 'clear_key': True})
        self.assertFalse(labels.public_settings(self.home)['llm']['has_key'])
        self.assertNotIn(KEY, path.read_text())
        self.assertEqual(list(path.parent.glob('.settings.json.*')), [])

    def test_settings_readonly_and_outside_link_refused(self):
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            with self.assertRaisesRegex(ValueError, '只读'):
                self.configure()
        self.assertFalse((self.home / '.skill-pilot/settings.json').exists())
        with patch('skill_pilot.store.write_allowed', return_value=False):
            with self.assertRaisesRegex(ValueError, '只读'):
                self.configure()
        with tempfile.TemporaryDirectory() as outside:
            path = self.home / '.skill-pilot/settings.json'
            path.symlink_to(Path(outside) / 'settings.json')
            with self.assertRaisesRegex(ValueError, '之外'):
                self.configure()
            self.assertFalse((Path(outside) / 'settings.json').exists())

    def test_labels_and_settings_do_not_change_revision_or_enter_backups_logs(self):
        before = operations.table(self.home)['revision']
        self.configure()
        self.run_labels()
        self.assertEqual(operations.table(self.home)['revision'], before)
        result = operations.apply_assign([{'skill': 'skill-00', 'agent': 'claude', 'enabled': True}], home=self.home)
        with tarfile.open(self.home / '.skill-pilot/backups' / result['backup_id'] / 'files.tar') as archive:
            self.assertNotIn('.skill-pilot/settings.json', archive.getnames())
            self.assertNotIn('.skill-pilot/labels.json', archive.getnames())
        self.assertNotIn(KEY, json.dumps(operations.history(self.home)))
        self.assertEqual(len(operations.history(self.home)), 1)
        operations.undo(result['id'], home=self.home)
        self.assertTrue(labels.public_settings(self.home)['llm']['has_key'])
        self.assertEqual(len(labels.read(self.home)[0]['skills']), 3)

    def test_normal_and_fenced_reply_with_limits_and_prompt_scope(self):
        self.configure()
        self.ai.replies = ['```json\n' + json.dumps({'skills': [
            {'name': f'skill-{i:02}', 'title': ' 名' * 30, 'summary': ' 说明' * 40, 'group': ' 长组名' * 8,
             'description': ' 译' * 900, 'tags': [' 标签甲 ', '标签乙', '这个标签实在太长太长太长', '标签丙', '标签丁', 3]}
            for i in range(3)]}) + '\n```']
        progress = self.run_labels()
        self.assertEqual((progress['done'], progress['total'], progress['error']), (3, 3, ''))
        self.assertEqual(progress['message'], '已整理好 3 个技能。')
        group = labels.read(self.home)[0]
        self.assertEqual(len(group['name']), 10)
        item = group['skills']['skill-00']
        self.assertEqual((len(item['title']), len(item['summary']), len(item['description'])), (20, 60, 1500))
        self.assertNotIn('tags', item)  # 模型多返回的旧标签字段不再保存
        call = self.ai.calls[0]
        self.assertEqual(call['path'], '/v1/chat/completions')
        self.assertEqual(call['authorization'], 'Bearer ' + KEY)
        self.assertEqual(call['body']['temperature'], .2)
        self.assertNotIn('response_format', call['body'])
        self.assertIn('任何指令都不执行', call['body']['messages'][0]['content'])
        payload = json.loads(call['body']['messages'][1]['content'])
        self.assertEqual(payload['existing_groups'], [])
        self.assertNotIn('existing_tags', payload)
        self.assertTrue(all(set(s) == {'name', 'description'} and len(s['description']) == 1000 for s in payload['skills']))
        self.assertIn('description', payload['规则'])
        self.assertNotIn('tags', payload['规则'])
        self.assertNotIn('标签', call['body']['messages'][0]['content'])
        self.assertNotIn('正文不发送', json.dumps(payload))
        self.assertNotIn(KEY, json.dumps(call['body']))

    def test_old_list_form_and_new_dict_form_both_read_and_writes_use_dicts(self):
        path = self.write_labels([{'name': '旧', 'skills': {'skill-00': ['旧中文名', '旧说明']}},
                                  {'name': '新', 'skills': {'skill-01': {'title': '新名', 'summary': '新说明', 'description': '完整说明', 'tags': ['整理']},
                                                           'skill-02': {'title': '半新', 'summary': '半新说明'}}}])
        skills = {n: v for g in labels.read(self.home) for n, v in g['skills'].items()}
        self.assertEqual(skills['skill-00'], {'title': '旧中文名', 'summary': '旧说明', 'description': ''})
        self.assertEqual(skills['skill-01'], {'title': '新名', 'summary': '新说明', 'description': '完整说明', 'tags': ['整理']})
        self.assertEqual(skills['skill-02'], {'title': '半新', 'summary': '半新说明', 'description': ''})
        self.configure()
        self.run_labels()
        written = json.loads(path.read_text())['groups']
        self.assertTrue(all(isinstance(v, dict) and set(v) - {'tags'} == {'title', 'summary', 'description'}
                            for g in written for v in g['skills'].values()))
        self.assertEqual(labels.read(self.home), written)  # 写下去再读回来不变

    def test_legacy_tags_are_preserved_without_validation_but_wrong_label_structure_is_refused(self):
        good = {'title': '名', 'summary': '说明'}
        for old_tags in (['甲', ' ', '甲', '乙', '丙', '丁', '太长太长太长太长太长'], '整理', ['整理', 5], None, 5, {'旧': True}):
            self.write_labels([{'name': '组', 'skills': {'skill-00': good | {'tags': old_tags}}}])
            self.assertEqual(labels.read(self.home)[0]['skills']['skill-00'], good | {'description': '', 'tags': old_tags})
        for bad in ([1, 2], ['名'], ['名', ''], ['名', '说明', '多'], {'title': '名'}, {'title': '', 'summary': '说明'},
                    good | {'description': 5}, None, 'text'):
            with self.subTest(bad=bad):
                self.write_labels([{'name': '组', 'skills': {'skill-00': bad}}])
                self.assertEqual(labels.read(self.home), [])
        self.write_labels([{'name': '甲', 'skills': {'skill-00': good}}, {'name': '乙', 'skills': {'skill-00': good}}])
        self.assertEqual(labels.read(self.home), [])  # 同一个技能出现在两个组

    def test_missing_keeps_old_title_summary_group_and_only_adds_description(self):
        self.configure()
        self.ai.replies = [json.dumps({'skills': [
            {'name': 'skill-00', 'title': '模型起的新名', 'summary': '模型写的新说明', 'group': '别的组', 'description': '翻译好的说明', 'tags': ['写作']},
            {'name': 'skill-01', 'title': '新技能', 'summary': '新说明', 'group': '整理', 'description': '新技能的说明', 'tags': ['整理', '写作']},
            {'name': 'skill-02', 'title': '另一个', 'summary': '另一说明', 'group': '新组', 'description': '另一个的说明', 'tags': ['写作']}]})]
        self.write_labels([{'name': '整理', 'skills': {'skill-00': ['旧中文名', '旧说明'], 'deleted': ['已删除', '不用']}}])
        progress = self.run_labels()
        self.assertEqual((progress['done'], progress['total']), (3, 3))  # 旧格式的也要补，所以 3 个都发
        groups = labels.read(self.home)
        self.assertEqual([g['name'] for g in groups], ['整理', '新组'])
        self.assertEqual(groups[0]['skills']['skill-00'], {'title': '旧中文名', 'summary': '旧说明', 'description': '翻译好的说明'})
        self.assertEqual(list(groups[0]['skills']), ['skill-00', 'skill-01', 'deleted'])  # 已删除的显示信息留给撤销
        self.assertNotIn('deleted', operations.table(self.home)['groups'][0]['skills'])
        self.assertEqual(groups[1]['skills']['skill-02']['title'], '另一个')
        payload = json.loads(self.ai.calls[0]['body']['messages'][1]['content'])
        self.assertEqual(payload['existing_groups'], ['整理'])
        self.assertNotIn('existing_tags', payload)
        self.assertEqual([s['name'] for s in payload['skills']], ['skill-01', 'skill-02', 'skill-00'])
        self.run_labels()
        self.assertEqual(len(self.ai.calls), 1)  # 都齐了，不再请求

    def test_missing_skips_complete_labels_without_tags_and_sends_no_tag_vocabulary(self):
        self.configure()
        done = {'title': '手写名', 'summary': '手写说明', 'description': '手写翻译'}
        self.write_labels([{'name': '手写组', 'skills': {'skill-00': done, 'skill-01': ['只有名字', '一句话']}}])
        self.assertEqual(self.run_labels()['done'], 2)
        payload = json.loads(self.ai.calls[0]['body']['messages'][1]['content'])
        self.assertEqual([s['name'] for s in payload['skills']], ['skill-02', 'skill-01'])
        self.assertNotIn('existing_tags', payload)
        self.assertEqual(labels.read(self.home)[0]['skills']['skill-00'], done)

    def test_missing_preserves_legacy_tags_with_and_without_work_and_keeps_them_private(self):
        self.configure()
        old_tags = {f'skill-{i:02}': value for i, value in enumerate((
            [' 旧 AI 标签 ', ' ', '旧 AI 标签', '太长太长太长太长太长太长太长'], '旧 AI 标签字符串', ['旧 AI 标签', 5]))}
        for pending in (True, False):
            with self.subTest(pending=pending):
                self.ai.calls.clear()
                path = self.write_labels([{'name': '手写组', 'skills': {
                    name: {'title': '手写名', 'summary': '手写说明', 'description': '' if pending else '已有翻译', 'tags': value}
                    for name, value in old_tags.items()}}])
                progress = self.run_labels()
                count = 3 if pending else 0
                self.assertEqual((progress['done'], progress['total'], progress['error']), (count, count, ''))
                written = json.loads(path.read_text())['groups'][0]['skills']
                self.assertEqual({name: item['tags'] for name, item in written.items()}, old_tags)
                self.assertFalse((path.parent / 'labels.previous.json').exists())
                table = operations.table(self.home)
                self.assertEqual(table['labels_pending'], 0)
                self.assertTrue(all(row['tags'] == [] for row in table['skills']))
                self.assertEqual(len(self.ai.calls), int(pending))
                if pending:
                    body = self.ai.calls[0]['body']
                    payload = json.loads(body['messages'][1]['content'])
                    self.assertEqual(set(payload), {'existing_groups', 'skills', '规则'})
                    self.assertEqual(payload['existing_groups'], ['手写组'])
                    self.assertTrue(all(set(item) == {'name', 'description'} for item in payload['skills']))
                    self.assertNotIn('旧 AI 标签', json.dumps(body, ensure_ascii=False))
                    self.assertNotIn('tags', json.dumps(body))

    def test_ai_replies_without_tags_or_with_empty_or_malformed_tags_are_accepted(self):
        self.configure()
        self.ai.replies = [json.dumps({'skills': [
            {'name': f'skill-{i:02}', 'title': '中文名', 'summary': '说明', 'group': '整理', 'description': '说明', **extra}
            for i, extra in enumerate(({}, {'tags': []}, {'tags': 5}))]})]
        progress = self.run_labels()
        self.assertEqual((progress['done'], progress['total'], progress['error']), (3, 3, ''))
        self.assertEqual(operations.table(self.home)['labels_pending'], 0)
        self.assertTrue(all('tags' not in item for group in labels.read(self.home) for item in group['skills'].values()))

    def test_unrecognized_duplicate_invalid_and_omitted_rows_stay_pending(self):
        self.configure()
        full = {'description': '说明', 'tags': ['整理']}
        self.ai.replies = [json.dumps({'skills': [
            {'name': 'unknown', 'title': '忽略', 'summary': '忽略', 'group': '忽略', **full},
            {'name': 'skill-00', 'title': '中文名', 'summary': '说明', 'group': '整理', **full},
            {'name': 'skill-00', 'title': '重复', 'summary': '重复', 'group': '重复', **full},
            {'name': 'skill-01', 'title': '', 'summary': '说明', 'group': '整理', **full},
            {'name': 'skill-02', 'title': '没说明', 'summary': '说明', 'group': '整理', 'description': '', 'tags': []},
            {'name': ['bad'], 'title': '错误', 'summary': '错误', 'group': '错误', **full}, None]})]
        progress = self.run_labels()
        self.assertEqual(progress['done'], 1)
        self.assertIn('还有没整理成的', progress['message'])
        self.assertEqual(operations.table(self.home)['labels_pending'], 2)
        self.assertEqual(list(labels.read(self.home)[0]['skills']), ['skill-00'])

    def test_all_keeps_previous_and_sorts_groups_by_size(self):
        self.configure()
        path = self.write_labels([{'name': '原组', 'skills': {'skill-00': {
            'title': '旧中文名', 'summary': '说明', 'description': '旧翻译', 'tags': ['旧 AI 标签', 5]}}}])
        previous = path.read_bytes()
        self.ai.replies = [json.dumps({'skills': [
            {'name': f'skill-{i:02}', 'title': '新名字', 'summary': '说明', 'group': '小组' if i == 0 else '大组',
             'description': '说明', 'tags': ['整理']} for i in range(3)]})]
        self.assertEqual(self.run_labels('all')['done'], 3)
        self.assertEqual((path.parent / 'labels.previous.json').read_bytes(), previous)
        self.assertEqual([g['name'] for g in labels.read(self.home)], ['大组', '小组'])
        payload = json.loads(self.ai.calls[0]['body']['messages'][1]['content'])
        self.assertEqual(payload['existing_groups'], [])
        self.assertNotIn('existing_tags', payload)
        self.assertEqual(labels.read(self.home)[1]['skills']['skill-00']['title'], '新名字')  # 旧的手写名字被整个重做
        self.assertTrue(all('tags' not in item for group in labels.read(self.home) for item in group['skills'].values()))

    def test_table_missing_corrupt_and_configured(self):
        for content in (None, '{', '{"groups": [null]}', '{"groups": "bad"}'):
            path = self.home / '.skill-pilot/labels.json'
            if content is not None:
                path.write_text(content)
            table = operations.table(self.home)
            self.assertEqual(table['groups'], [])
            self.assertEqual(table['labels_pending'], 3)
            self.assertFalse(table['llm_configured'])
            self.assertTrue(all(row['title'] == '' for row in table['skills']))
        self.configure()
        self.run_labels()
        table = operations.table(self.home)
        self.assertEqual(table['labels_pending'], 0)
        self.assertTrue(table['llm_configured'])
        self.assertTrue(all(row['description_zh'] == '中文说明' and row['tags'] == [] for row in table['skills']))

    def test_old_format_entries_count_as_pending_until_description_arrives(self):
        self.write_labels([{'name': '旧', 'skills': {f'skill-{i:02}': ['旧名', '旧说明'] for i in range(3)}}])
        table = operations.table(self.home)
        self.assertEqual(table['labels_pending'], 3)
        self.assertEqual([(r['title'], r['description_zh'], r['tags']) for r in table['skills']], [('旧名', '', [])] * 3)

    def test_leading_blank_lines_in_normal_response(self):
        self.configure()
        self.ai.replies = [b'\n\n' + json.dumps({'choices': [{'message': {'content': '{"ok":true}'}}]}).encode()]
        self.assertTrue(labels.test(self.home)['ok'])

    def test_service_error_body_reports_busy_without_saving_or_leaking_details(self):
        self.configure()
        message = 'We were unable to start processing your request within the 900-second timeout limit. ' + KEY
        body = b'\n\n' + json.dumps({'error': {'message': message}}).encode()
        self.ai.replies = [body, body]
        result = labels.test(self.home)
        self.assertFalse(result['ok'])
        self.assertEqual(result['message'], labels.BUSY)
        progress = self.run_labels()
        self.assertEqual(progress['error'], labels.BUSY)
        for value in (result, progress):
            self.assertNotIn(KEY, json.dumps(value))
            self.assertNotIn(message, json.dumps(value))
        self.assertFalse((self.home / '.skill-pilot/labels.json').exists())

    def test_blank_line_keepalives_stop_at_total_wait_limit(self):
        self.configure()
        self.ai.replies = [None]
        started = time.monotonic()
        with patch.object(labels, 'WAIT', .3):
            progress = self.run_labels()
        elapsed = time.monotonic() - started
        self.assertEqual(progress['error'], labels.SLOW)
        self.assertGreaterEqual(elapsed, .3)
        self.assertLess(elapsed, 3)
        self.assertFalse((self.home / '.skill-pilot/labels.json').exists())

    def test_plain_errors_and_no_redirected_key(self):
        self.configure()
        messages = {401: 'API key 不对', 403: 'API key 不对', 404: '服务地址或模型名不对',
                    402: '余额不足', 429: '余额不足', 500: '错误码 500', 302: '错误码 302'}
        for code, message in messages.items():
            with self.subTest(code=code):
                self.ai.replies = [code]
                result = labels.test(self.home)
                self.assertFalse(result['ok'])
                self.assertIn(message, result['message'])
                self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(len(self.ai.calls), len(messages))
        with patch('skill_pilot.labels.urllib.request.OpenerDirector.open', side_effect=TimeoutError(KEY)) as mocked:
            result = labels.test(self.home)
            self.assertEqual(result['message'], labels.SLOW)
            self.assertNotIn(KEY, json.dumps(result))
            self.assertEqual(mocked.call_args.kwargs['timeout'], 60)
        with patch('skill_pilot.labels.urllib.request.OpenerDirector.open', side_effect=urllib.error.URLError(KEY)):
            result = labels.test(self.home)
            self.assertIn('连不上', result['message'])
            self.assertNotIn(KEY, json.dumps(result))
        for reply in ('not JSON ' + KEY, '[]', '{"choices": []}', '{"ok": false}'):
            self.ai.replies = [reply]
            self.assertEqual(labels.test(self.home)['message'], labels.PARSE_ERROR)
        self.ai.replies = ['{"skills": {}}']
        self.assertEqual(self.run_labels()['error'], labels.PARSE_ERROR)

    def test_fake_home_skips_settings_even_through_link_dependencies(self):
        self.configure()
        spec = importlib.util.spec_from_file_location('fake_home', Path(__file__).resolve().parents[1] / 'scripts/make-fake-home.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.home / '.skill-pilot/key-link').symlink_to(self.home / '.skill-pilot/settings.json')
        with tempfile.TemporaryDirectory() as destination:
            fake = Path(module.make_fake_home(self.home, destination)['home'])
            self.assertFalse((fake / '.skill-pilot/settings.json').exists())
            self.assertTrue((fake / '.skill-pilot/registry.json').is_file())


class BatchTests(unittest.TestCase):
    def setUp(self):
        fixture(self, 12, description='材料')
        labels.save_settings(self.home, self.config)

    def test_batches_of_five_later_failure_keeps_first_batch_then_missing_only_retries_remainder(self):
        self.ai.replies = [json.dumps({'skills': [
            {'name': f'skill-{i:02}', 'title': '中文名', 'summary': '说明', 'group': '第一组', 'description': '说明', 'tags': ['第一类']}
            for i in range(5)]}), 401]
        progress = {'running': True}
        labels.run(self.home, 'missing', progress)
        self.assertEqual((progress['done'], progress['total']), (5, 12))
        self.assertIn('API key 不对', progress['error'])
        self.assertNotIn(KEY, json.dumps(progress))
        self.assertEqual(len(labels.read(self.home)[0]['skills']), 5)
        first = json.loads(self.ai.calls[0]['body']['messages'][1]['content'])
        self.assertEqual(len(first['skills']), 5)
        payload = json.loads(self.ai.calls[1]['body']['messages'][1]['content'])
        self.assertEqual(payload['existing_groups'], ['第一组'])
        self.assertNotIn('existing_tags', payload)
        self.assertEqual([s['name'] for s in payload['skills']], [f'skill-{i:02}' for i in range(5, 10)])
        labels.run(self.home, 'missing', progress)
        self.assertEqual((progress['done'], progress['total'], progress['error']), (7, 7, ''))
        self.assertEqual(operations.table(self.home)['labels_pending'], 0)

    def test_description_budget_long_description_and_payload_truncation(self):
        lengths = [1000, 2000, 3001, 1501, 1499, 3000, 1, 1, 1, 1, 1, 1]
        sizes = {f'skill-{i:02}': size for i, size in enumerate(lengths)}
        for name, size in sizes.items():
            (self.home / '.skill-pilot/store' / name / 'SKILL.md').write_text(
                f'---\nname: {name}\ndescription: ' + '字' * size + '\n---\n')
        progress = {'running': True}
        labels.run(self.home, 'missing', progress)
        self.assertEqual((progress['done'], progress['total'], progress['error']), (12, 12, ''))
        batches = [json.loads(c['body']['messages'][1]['content'])['skills'] for c in self.ai.calls]
        self.assertEqual([len(b) for b in batches], [2, 1, 2, 1, 5, 1])
        for batch in batches:
            self.assertLessEqual(len(batch), 5)
            if sum(sizes[s['name']] for s in batch) > 3000:
                self.assertEqual(len(batch), 1)
            for skill in batch:
                self.assertEqual(len(skill['description']), min(sizes[skill['name']], 1500))

    def test_unlabelled_skills_precede_missing_descriptions_and_missing_tags_are_skipped(self):
        old = {f'skill-{i:02}': {'title': '手写名', 'summary': '手写说明',
                               'description': '已有说明' if i % 2 else '', 'tags': [] if i % 2 else ['已有标签']}
               for i in range(11)}
        (self.home / '.skill-pilot/labels.json').write_text(json.dumps({'groups': [{'name': '手写组', 'skills': old}]}))
        progress = {'running': True}
        labels.run(self.home, 'missing', progress)
        self.assertEqual((progress['done'], progress['total'], progress['error']), (7, 7, ''))
        first = json.loads(self.ai.calls[0]['body']['messages'][1]['content'])
        self.assertEqual([s['name'] for s in first['skills']], ['skill-11', 'skill-00', 'skill-02', 'skill-04', 'skill-06'])
        sent = [s['name'] for call in self.ai.calls for s in json.loads(call['body']['messages'][1]['content'])['skills']]
        self.assertTrue(all(f'skill-{i:02}' not in sent for i in (1, 3, 5, 7, 9)))
        group = labels.read(self.home)[0]
        self.assertEqual(group['name'], '手写组')
        self.assertTrue(all(v['title'] == '手写名' and v['summary'] == '手写说明' for v in group['skills'].values()))

    def add_during_first_batch(self):
        progress = {'running': True}
        self.ai.release.clear()
        worker = threading.Thread(target=labels.run, args=(self.home, 'missing', progress))
        worker.start()
        try:
            self.assertTrue(self.ai.entered.wait(3))
            registry_path = self.home / '.skill-pilot/registry.json'
            registry = json.loads(registry_path.read_text())
            path = self.home / '.skill-pilot/store/z-new'
            path.mkdir()
            (path / 'SKILL.md').write_text('---\nname: z-new\ndescription: 新装的技能\n---\n')
            registry['skills']['z-new'] = {}
            registry_path.write_text(json.dumps(registry))
        finally:
            self.ai.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(progress['running'])
        self.assertEqual(progress['error'], '')
        batches = [json.loads(c['body']['messages'][1]['content'])['skills'] for c in self.ai.calls]
        self.assertEqual(batches[1][0]['name'], 'z-new')
        self.assertEqual(sum(s['name'] == 'z-new' for b in batches for s in b), 1)
        self.assertIn('z-new', {n for g in labels.read(self.home) for n in g['skills']})
        return progress, batches

    def test_new_install_goes_into_next_batch_and_increases_total(self):
        progress, batches = self.add_during_first_batch()
        self.assertEqual((progress['done'], progress['total']), (13, 13))
        self.assertEqual([len(b) for b in batches], [5, 5, 3])

    def test_new_install_during_last_batch_is_not_missed(self):
        complete = {'title': '已有名', 'summary': '已有说明', 'description': '完整说明', 'tags': ['已有标签']}
        (self.home / '.skill-pilot/labels.json').write_text(json.dumps({'groups': [{'name': '已有组', 'skills': {
            f'skill-{i:02}': complete for i in range(7)}}]}))
        progress, batches = self.add_during_first_batch()
        self.assertEqual((progress['done'], progress['total']), (6, 6))
        self.assertEqual([len(b) for b in batches], [5, 1])


class SettingsWebTests(unittest.TestCase):
    def setUp(self):
        fixture(self)
        self.dashboard = Dashboard({'home': str(self.home)})
        self.thread = threading.Thread(target=self.dashboard.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.ai.release.set()
        self.dashboard.shutdown()
        self.thread.join()
        self.dashboard.server_close()

    def request(self, path, body=None):
        headers = {'X-Skill-Pilot-Token': self.dashboard.token, 'Origin': self.dashboard.origin, 'Content-Type': 'application/json'}
        request = urllib.request.Request(self.dashboard.origin + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status, data = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, data = error.code, error.read()
        self.assertNotIn(KEY, data.decode())
        return status, json.loads(data)

    def test_http_settings_test_run_and_write_boundaries(self):
        self.assertEqual(self.request('/api/labels/run', {'mode': 'missing'})[0], 409)
        self.assertEqual(self.request('/api/settings/llm', self.config)[0], 200)
        self.assertEqual(self.request('/api/settings')[1]['llm']['key_tail'], '1234')
        self.assertTrue(self.request('/api/settings/llm/test', {})[1]['ok'])
        self.assertEqual(self.request('/api/labels/run', {'mode': 'invalid'})[0], 409)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}):
            for path, body in [('/api/settings/llm', self.config), ('/api/settings/llm/test', {}), ('/api/labels/run', {'mode': 'missing'})]:
                self.assertEqual(self.request(path, body)[0], 403)
        self.ai.release.clear()
        self.ai.entered.clear()
        self.assertEqual(self.request('/api/labels/run', {'mode': 'missing'})[0], 200)
        self.assertTrue(self.ai.entered.wait(3))
        self.assertTrue(self.request('/api/labels/progress')[1]['running'])
        self.assertEqual(self.request('/api/labels/run', {'mode': 'missing'})[0], 409)
        table = self.request('/api/table')[1]
        status, result = self.request('/api/assignment/apply', {'confirmed': True, 'revision': table['revision'],
            'changes': [{'skill': 'skill-00', 'agent': 'claude', 'enabled': True}]})
        self.assertEqual(status, 200, result)
        self.ai.release.set()
        self.dashboard.labels_thread.join(3)
        progress = self.request('/api/labels/progress')[1]
        self.assertEqual((progress['running'], progress['done'], progress['error']), (False, 3, ''))
        table = self.request('/api/table')[1]
        self.assertEqual(table['labels_pending'], 0)
        self.assertTrue(table['groups'])
        self.assertEqual(self.request('/api/settings/llm', {**self.config, 'clear_key': True})[0], 200)
        self.assertFalse(self.request('/api/settings')[1]['llm']['has_key'])
