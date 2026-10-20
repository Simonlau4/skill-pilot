import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from skill_pilot import operations, sharing, store
from skill_pilot.scan import content_digest
from skill_pilot.sync import visibility
from skill_pilot.web.server import Dashboard
from tests import test_operations


def validate_legacy_config(data):
    """冻结新增说明前的 1 版校验，确认旧接收端仍能接受新导出。"""
    def names(value):
        return (isinstance(value, list) and all(isinstance(n, str) and
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', n) for n in value) and len(set(value)) == len(value))
    if (not isinstance(data, dict) or data.get('format') != 'skill-pilot-config' or type(data.get('version')) is not int or
            data['version'] != 1 or not isinstance(data.get('skills'), list)):
        raise ValueError('旧版不支持的配置。')
    seen = set()
    for row in data['skills']:
        if (not isinstance(row, dict) or not names([row.get('name')]) or row['name'] in seen or
                not names(row.get('assigned_agents')) or not names(row.get('requires')) or
                type(row.get('local_modified')) is not bool or not isinstance(row.get('source'), dict)):
            raise ValueError('旧版不支持的技能条目。')
        seen.add(row['name'])
        source = row['source']
        if source.get('kind') == 'manual':
            continue
        repository, folder, commit = source.get('repository'), source.get('path'), source.get('commit', '')
        if (source.get('kind') != 'github' or not isinstance(repository, str) or
                not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*', repository) or
                not isinstance(folder, str) or not folder or '\\' in folder or any(ord(c) < 32 for c in folder) or
                PurePosixPath(folder).is_absolute() or '..' in PurePosixPath(folder).parts or
                not isinstance(commit, str) or (commit and not re.fullmatch(r'[0-9a-f]{40}', commit))):
            raise ValueError('旧版不支持的来源。')
    if any(set(row['requires']) - seen for row in data['skills']):
        raise ValueError('旧版缺少所需技能。')
    return data


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_operations.OperationsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home

    def registry(self, change):
        path = self.home / '.skill-pilot/registry.json'
        data = json.loads(path.read_text())
        change(data['skills'])
        path.write_text(json.dumps(data))

    def config(self, manual=False):
        def row(name, assigned, requires=(), kind='github'):
            return {'name': name, 'assigned_agents': assigned, 'requires': list(requires), 'local_modified': False,
                    'source': {'kind': kind, 'repository': 'demo/skills', 'path': 'skills/' + name, 'commit': 'a' * 40}}
        rows = [row('alpha', ['claude'], ['beta']), row('beta', []), row('other', ['claude'], ['beta'])]
        if manual:
            rows += [row('local', ['claude'], kind='manual'), row('needs-local', ['claude'], ['local'])]
        return {'format': sharing.FORMAT, 'version': 1, 'skills': rows}

    def write_labels(self, name='other'):
        display = {'title': '示例技能', 'summary': '整理 Acme 示例资料', 'group': '示例分组'}
        (self.home / '.skill-pilot/labels.json').write_text(json.dumps({'groups': [{
            'name': display['group'], 'skills': {name: {
                'title': display['title'], 'summary': display['summary'],
                'description': 'AI_DESCRIPTION_SECRET', 'tags': ['OLD_AI_TAG_SECRET']}}}]}))
        return display

    def download(self, url):
        directory = Path(tempfile.mkdtemp(prefix='skill-pilot-share-download-')).resolve()
        self.addCleanup(lambda: __import__('shutil').rmtree(directory, ignore_errors=True))
        skills = []
        for name in ('alpha', 'beta'):
            path = directory / name
            path.mkdir()
            (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 测试技能\n---\n仅作为文件复制。\n')
            skills.append({'name': name, 'repository_path': 'skills/' + name, 'path': str(path),
                           'digest': content_digest(path), 'source': {'source': 'https://github.com/demo/skills',
                           'repository': 'demo/skills', 'source_path': 'skills/' + name, 'commit': 'a' * 40}})
        return {'temporary': str(directory), 'skills': skills}

    def test_export_whitelist_active_dependency_closure_and_readonly(self):
        def change(skills):
            skills['grill-me']['requires'] = ['grilling']
            skills['grill-me']['source'] = {'source': 'https://github.com/demo/skills?token=SOURCE_SECRET',
                                           'source_path': 'skills/grill-me/SKILL.md', 'commit': 'a' * 40,
                                           'record': {'private_path': str(self.home), 'api_key': 'SOURCE_SECRET'}}
            skills['grilling']['assigned_agents'] = []
            skills['other']['assigned_agents'] = []
        self.registry(change)
        self.write_labels('grill-me')
        (self.home / '.skill-pilot/settings.json').write_text('{"api_key":"SETTINGS_SECRET"}')
        (self.home / '.skill-pilot/tags.json').write_text('{"version":1,"skills":{"grill-me":["USER_TAG_SECRET"]}}')
        (self.home / '.skill-pilot/usage.sqlite').write_bytes(b'PRIVATE_HISTORY')
        path = self.home / '.skill-pilot/store/grill-me/SKILL.md'
        path.write_text(path.read_text() + '\nPRIVATE_SKILL_BODY\n')
        before = store.physical_state(self.home / '.skill-pilot')
        original = Path.read_text
        def read(path, *args, **kwargs):
            self.assertNotIn(path.name, {'settings.json', 'tags.json', 'usage.sqlite'})
            return original(path, *args, **kwargs)
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), patch.object(Path, 'read_text', read):
            result = sharing.export_config(self.home, 'codex')
        self.assertEqual([s['name'] for s in result['skills']], ['grill-me', 'grilling'])
        self.assertEqual(result['skills'][0]['source'], {'kind': 'github', 'repository': 'demo/skills',
                                                       'path': 'skills/grill-me', 'commit': 'a' * 40})
        self.assertEqual(result['skills'][1]['assigned_agents'], [])
        serialized = json.dumps(result)
        for text in ('SOURCE_SECRET', 'SETTINGS_SECRET', 'PRIVATE_HISTORY', 'PRIVATE_SKILL_BODY',
                     'USER_TAG_SECRET', 'OLD_AI_TAG_SECRET', 'AI_DESCRIPTION_SECRET', str(self.home), '.codex/skills'):
            self.assertNotIn(text, serialized)
        self.assertEqual(store.physical_state(self.home / '.skill-pilot'), before)
        with self.assertRaises(ValueError):
            sharing.export_config(self.home, 'unknown')

    def test_export_optional_display_fields_and_original_description(self):
        display = self.write_labels()
        result = sharing.export_config(self.home)
        self.assertEqual((result['format'], result['version']), ('skill-pilot-config', 1))
        rows = {row['name']: row for row in result['skills']}
        fields = {'name', 'assigned_agents', 'requires', 'source', 'local_modified', 'description'}
        self.assertEqual(set(rows['other']), fields | display.keys())
        self.assertEqual({key: rows['other'][key] for key in display}, display)
        for name in ('grill-me', 'grilling'):
            self.assertEqual(set(rows[name]), fields)
        self.assertTrue(all(row['description'] == '示例技能' for row in rows.values()))

    def test_export_description_truncates_at_1500_characters(self):
        path = self.home / '.skill-pilot/store/other/SKILL.md'
        for size in (1499, 1500, 1501, 2000):
            description = '示例\n' + '字' * (size - 3)
            path.write_text('---\nname: other\ndescription: |\n  ' + description.replace('\n', '\n  ') +
                            '\n---\nPRIVATE_SKILL_BODY\n')
            with self.subTest(size=size):
                row = next(row for row in sharing.export_config(self.home)['skills'] if row['name'] == 'other')
                self.assertEqual(row['description'], description[:1500])

    def export_and_preview(self):
        before = store.physical_state(self.home / '.skill-pilot')
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), patch.object(sharing.github, 'inspect') as download:
            data = sharing.export_config(self.home)
            self.assertIs(sharing.validate(data), data)
            path = self.home / sharing.FILENAME
            path.write_text(json.dumps(data, ensure_ascii=False))
            loaded = sharing.read_config(path)
            self.assertEqual(loaded, data)
            plan = sharing.preview_import(loaded, home=self.home)
            self.assertIn('other', plan['reuse'])
            self.assertEqual(plan['install'], [])
            self.assertEqual(plan['skipped'], [])
            download.assert_not_called()
        self.assertEqual(store.physical_state(self.home / '.skill-pilot'), before)
        return next(row for row in loaded['skills'] if row['name'] == 'other')

    def test_export_cleans_description_before_validation_and_preview(self):
        path = self.home / '.skill-pilot/store/other/SKILL.md'
        cases = [('开发 👩\u200d💻 技能', '开发 👩💻 技能'),
                 ('示例 soft\u00adhyphen 说明', '示例 softhyphen 说明'),
                 ('  第一行\t示例\n第二行\t说明  ', '第一行 示例\n第二行 说明'),
                 (' \t👩\u200d💻\u00ad\t示例\n下一行\u200b\t ', '👩💻 示例\n下一行'),
                 ('\u200d' * 1600 + '有效说明', '有效说明')]
        for description, expected in cases:
            with self.subTest(description=description[:40], length=len(description)):
                path.write_text('---\nname: other\ndescription: ' + json.dumps(description) + '\n---\n示例正文\n')
                self.assertEqual(self.export_and_preview()['description'], expected)

    def test_export_cleans_handwritten_labels_before_validation_and_preview(self):
        path = self.home / '.skill-pilot/labels.json'
        for field, limit in (('title', 40), ('summary', 120), ('group', 20)):
            cases = [('  示例\n名称\t👩\u200d💻\u00ad  ', '示例 名称 👩💻'),
                     (' \u200b' + '字' * (limit + 10) + '  ', '字' * limit),
                     ('\u00ad' * limit + '有效内容', '有效内容'),
                     ('字' * (limit - 1) + ' 后', '字' * (limit - 1))]
            for value, expected in cases:
                with self.subTest(field=field, value=value):
                    display = self.write_labels()
                    groups = json.loads(path.read_text())['groups']
                    if field == 'group':
                        groups[0]['name'] = value
                    else:
                        groups[0]['skills']['other'][field] = value
                    path.write_text(json.dumps({'groups': groups}))
                    row = self.export_and_preview()
                    self.assertEqual({key: row[key] for key in display}, display | {field: expected})

    def test_export_omits_metadata_empty_after_cleaning(self):
        self.write_labels()
        controls = ' \t\n\u00ad\u200b\u200d\x00 '
        path = self.home / '.skill-pilot/labels.json'
        groups = json.loads(path.read_text())['groups']
        groups[0]['name'] = controls
        groups[0]['skills']['other'].update(title=controls, summary=controls)
        path.write_text(json.dumps({'groups': groups}))
        (self.home / '.skill-pilot/store/other/SKILL.md').write_text(
            '---\nname: other\ndescription: ' + json.dumps(controls) + '\n---\n示例正文\n')
        row = self.export_and_preview()
        self.assertFalse({'title', 'summary', 'group', 'description'} & row.keys())

    def test_export_omits_missing_empty_or_undecodable_description(self):
        path = self.home / '.skill-pilot/store/other/SKILL.md'
        for content in (b'PRIVATE_SKILL_BODY', b'---\nname: other\n---\nPRIVATE_SKILL_BODY',
                        b'---\nname: other\ndescription: "  "\n---\nPRIVATE_SKILL_BODY', b'\xff'):
            path.write_bytes(content)
            with self.subTest(content=content):
                row = next(row for row in sharing.export_config(self.home)['skills'] if row['name'] == 'other')
                self.assertNotIn('description', row)
        path.unlink()
        row = next(row for row in sharing.export_config(self.home)['skills'] if row['name'] == 'other')
        self.assertNotIn('description', row)

    def test_export_omits_unreadable_description(self):
        path = self.home / '.skill-pilot/store/other/SKILL.md'
        original, original_bytes = Path.read_text, Path.read_bytes
        def read(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError('模拟无法读取')
            return original(candidate, *args, **kwargs)
        def read_bytes(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError('模拟无法读取')
            return original_bytes(candidate, *args, **kwargs)
        with patch.object(Path, 'read_text', read), patch.object(Path, 'read_bytes', read_bytes):
            row = next(row for row in sharing.export_config(self.home)['skills'] if row['name'] == 'other')
        self.assertNotIn('description', row)
        self.assertTrue(row['local_modified'])

    def test_validate_optional_metadata_boundaries_and_read_config(self):
        for field, limit in (('title', 40), ('summary', 120), ('group', 20), ('description', 1500)):
            for value in (' 示例 ', '字' * limit):
                data = self.config()
                data['skills'][0][field] = value
                with self.subTest(field=field, value=value):
                    self.assertIs(sharing.validate(data), data)
        data['skills'][0].update(title='示例技能', summary='示例说明', group='示例分组', description='第一行\n第二行')
        path = self.home / sharing.FILENAME
        path.write_text(json.dumps(data))
        self.assertEqual(sharing.read_config(path), data)

    def test_invalid_metadata_rejects_entire_file_before_download_or_write(self):
        before = self.fixture.fingerprint(), store.physical_state(self.home / '.skill-pilot')
        path = self.home / sharing.FILENAME
        controls = ('\x00', '\t', '\r', '\x1b', '\x7f', '\x85', '\u200b', '\u202e', '\u2028', '\u2029', '\ufeff')
        with patch.object(sharing.github, 'inspect') as download:
            for field, limit in (('title', 40), ('summary', 120), ('group', 20), ('description', 1500)):
                values = [None, True, 7, 1.5, [], {}, '', ' \u3000 ', '字' * (limit + 1)]
                values += ['前' + char + '后' for char in controls]
                values += ['\t示例', '示例\r']
                if field != 'description':
                    values += ['第一行\n第二行', '\n示例', '示例\n']
                for kind in ('github', 'manual'):
                    for value in values:
                        data = self.config()
                        data['skills'][-1][field] = value
                        data['skills'][-1]['source']['kind'] = kind
                        path.write_text(json.dumps(data))
                        with self.subTest(field=field, value=value, kind=kind):
                            with self.assertRaises(ValueError):
                                sharing.validate(data)
                            with self.assertRaises(ValueError):
                                sharing.read_config(path)
                            with self.assertRaises(ValueError):
                                sharing.preview_import(data, home=self.home)
                            with self.assertRaises(ValueError):
                                sharing.import_config(data, home=self.home)
            download.assert_not_called()
        self.assertEqual((self.fixture.fingerprint(), store.physical_state(self.home / '.skill-pilot')), before)

    def test_new_export_passes_legacy_validation_and_unknown_fields_are_ignored(self):
        self.write_labels()
        def change(skills):
            skills['other']['source'] = {'source': 'https://github.com/acme/skills',
                                         'source_path': 'skills/other/SKILL.md', 'commit': 'a' * 40}
            skills['grilling']['source'] = {}
        self.registry(change)
        data = sharing.export_config(self.home)
        self.assertEqual({row['source']['kind'] for row in data['skills']}, {'github', 'manual'})
        self.assertIs(validate_legacy_config(data), data)
        data['future_field'] = ['示例资料']
        data['skills'][0]['future_field'] = {'value': None}
        data['skills'][0]['source']['future_field'] = False
        self.assertIs(sharing.validate(data), data)
        self.assertIs(validate_legacy_config(data), data)

    def test_metadata_keeps_import_plan_and_local_labels_unchanged(self):
        self.write_labels()
        labels_path = self.home / '.skill-pilot/labels.json'
        original = labels_path.read_bytes()
        data = self.config()
        old_plan = sharing.preview_import(data, home=self.home)
        for row in data['skills']:
            row.update(title='分享的示例技能', summary='分享的示例摘要', group='分享分组', description='分享者的说明')
        self.assertEqual(sharing.preview_import(data, home=self.home), old_plan)
        with patch.object(sharing.github, 'inspect', side_effect=self.download):
            result = sharing.import_config(data, home=self.home)
        self.assertTrue(result['complete'])
        self.assertEqual(visibility(self.home)['claude']['available'], ['alpha', 'beta', 'other'])
        self.assertEqual(labels_path.read_bytes(), original)
        self.assertNotIn('分享者的说明', (self.home / '.skill-pilot/store/alpha/SKILL.md').read_text())

    def test_export_marks_local_changes_and_hides_local_source(self):
        self.registry(lambda skills: skills['grill-me'].update(source={'repository': (self.home / 'private').as_uri()}))
        (self.home / '.skill-pilot/store/grill-me/SKILL.md').write_text('本机修改')
        rows = {s['name']: s for s in sharing.export_config(self.home)['skills']}
        self.assertTrue(rows['grill-me']['local_modified'])
        self.assertEqual(rows['grill-me']['source']['kind'], 'manual')
        self.assertNotIn(str(self.home), json.dumps(rows))

    def test_preview_is_local_and_reports_blocked_dependents_and_mapping(self):
        data = self.config(manual=True)
        data['skills'][0]['assigned_agents'].append('missing-agent')
        before = self.fixture.fingerprint()
        with patch.object(sharing.github, 'inspect') as download:
            result = sharing.preview_import(data, home=self.home)
            download.assert_not_called()
        self.assertEqual(result['install'], ['alpha', 'beta'])
        self.assertEqual(result['reuse'], ['other'])
        self.assertEqual(result['dependencies'], {'other': ['beta']})
        self.assertEqual([s['name'] for s in result['skipped']], ['local', 'needs-local'])
        self.assertEqual(result['unmapped_agents'], ['missing-agent'])
        self.assertEqual(self.fixture.fingerprint(), before)
        mapped = sharing.preview_import(data, ['codex'], self.home)
        self.assertEqual(mapped['assignments']['beta'], ['codex'])
        self.assertEqual(mapped['unmapped_agents'], [])

    def test_import_batch_reuses_existing_content_is_idempotent_and_undoes(self):
        original = (self.home / '.skill-pilot/store/other/SKILL.md').read_bytes()
        before = self.fixture.fingerprint()
        data = self.config()
        with patch.object(sharing.github, 'inspect', side_effect=self.download) as download:
            result = sharing.import_config(data, home=self.home)
            download.assert_called_once_with('https://github.com/demo/skills/tree/' + 'a' * 40)
        self.assertTrue(result['complete'])
        self.assertEqual(visibility(self.home)['claude']['available'], ['alpha', 'beta', 'other'])
        self.assertEqual((self.home / '.skill-pilot/store/other/SKILL.md').read_bytes(), original)
        self.assertIn('codex', operations.load(self.home)[1]['skills']['other']['assigned_agents'])
        self.assertEqual(operations.load(self.home)[1]['skills']['other']['requires'], ['beta'])
        self.assertEqual(operations.load(self.home)[1]['skills']['alpha']['requires'], ['beta'])
        after = self.fixture.fingerprint()
        with patch.object(sharing.github, 'inspect') as download:
            again = sharing.import_config(data, home=self.home)
            download.assert_not_called()
        self.assertNotIn('id', again)
        self.assertEqual(self.fixture.fingerprint(), after)
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fixture.fingerprint(), before)

    def test_partial_import_and_readonly_guard(self):
        with patch.object(sharing.github, 'inspect', side_effect=self.download):
            result = sharing.import_config(self.config(manual=True), home=self.home)
        self.assertFalse(result['complete'])
        self.assertEqual([s['name'] for s in result['skipped']], ['local', 'needs-local'])
        self.assertNotIn('needs-local', operations.load(self.home)[1]['skills'])
        before = self.fixture.fingerprint()
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), patch.object(sharing.github, 'inspect') as download:
            with self.assertRaisesRegex(ValueError, '只读'):
                sharing.import_config(self.config(), home=self.home)
            download.assert_not_called()
        self.assertEqual(self.fixture.fingerprint(), before)

    def test_invalid_config_cannot_download_or_write(self):
        candidates = [None, {'format': sharing.FORMAT, 'version': 2, 'skills': []}]
        for field, value in [('name', '../outside'), ('name', []), ('assigned_agents', 'codex'),
                             ('requires', [None]), ('local_modified', 'false'), ('source', [])]:
            data = self.config()
            data['skills'][0][field] = value
            candidates.append(data)
        for field, value in [('repository', 'file:///private'), ('repository', 'user:secret@github.com/repo'),
                             ('path', '../private'), ('path', '/private'), ('commit', '--exec=anything')]:
            data = self.config()
            data['skills'][0]['source'][field] = value
            candidates.append(data)
        duplicate = self.config()
        duplicate['skills'].append(copy.deepcopy(duplicate['skills'][0]))
        candidates.append(duplicate)
        incomplete = self.config()
        incomplete['skills'].pop(1)
        candidates.append(incomplete)
        before = self.fixture.fingerprint()
        with patch.object(sharing.github, 'inspect') as download:
            for data in candidates:
                with self.subTest(data=data), self.assertRaises(ValueError):
                    sharing.import_config(data, home=self.home)
            download.assert_not_called()
        self.assertEqual(self.fixture.fingerprint(), before)

    def test_failed_download_and_failed_apply_leave_library_unchanged(self):
        before = self.fixture.fingerprint()
        with patch.object(sharing.github, 'inspect', side_effect=ValueError('下载失败')):
            with self.assertRaisesRegex(ValueError, '下载失败'):
                sharing.import_config(self.config(), home=self.home)
        self.assertEqual(self.fixture.fingerprint(), before)
        directories = []
        def download(url):
            review = self.download(url)
            directories.append(Path(review['temporary']))
            return review
        with patch.object(sharing.github, 'inspect', side_effect=download), patch.object(operations, '_validate', side_effect=ValueError('校验失败')):
            with self.assertRaisesRegex(ValueError, '校验失败'):
                sharing.import_config(self.config(), home=self.home)
        self.assertEqual(self.fixture.fingerprint(), before)
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_changed_library_during_download_is_not_overwritten(self):
        def download(url):
            review = self.download(url)
            self.registry(lambda skills: skills['other'].update(external='保留这个变化'))
            return review
        with patch.object(sharing.github, 'inspect', side_effect=download):
            with self.assertRaisesRegex(ValueError, '已经变了'):
                sharing.import_config(self.config(), home=self.home)
        self.assertEqual(operations.load(self.home)[1]['skills']['other']['external'], '保留这个变化')
        self.assertFalse((self.home / '.skill-pilot/store/alpha').exists())

    def test_cli_export_preview_reuse_and_no_overwrite(self):
        display = self.write_labels()
        path = self.home / sharing.FILENAME
        def command(*args):
            return subprocess.run([sys.executable, '-m', 'skill_pilot', *args, '--json'], cwd=store.PROJECT,
                                  env={**os.environ, 'SKILL_PILOT_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'},
                                  capture_output=True, text=True, timeout=15)
        self.assertEqual(command('export-config', '--output', str(path)).returncode, 0)
        row = next(row for row in sharing.read_config(path)['skills'] if row['name'] == 'other')
        self.assertEqual({key: row[key] for key in display}, display)
        self.assertEqual(row['description'], '示例技能')
        self.assertEqual(command('export-config', '--output', str(path)).returncode, 1)
        result = command('import-config', str(path))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['install'], [])
        result = command('import-config', str(path), '--agent', 'claude', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)['complete'])
        path.write_text(json.dumps(self.config(manual=True)))
        result = command('import-config', str(path), '--agent', 'not-managed', '--yes')
        self.assertEqual(result.returncode, 1)
        data = self.config(manual=True)
        data['skills'] = data['skills'][-2:]
        data['skills'][-1]['source']['kind'] = 'manual'
        path.write_text(json.dumps(data))
        result = command('import-config', str(path), '--yes')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)['complete'])

    def test_blocked_parent_does_not_add_extra_dependency_assignments(self):
        data = self.config(manual=True)
        data['skills'][-1]['assigned_agents'] = ['codex']
        data['skills'][-1]['requires'].append('beta')
        plan = sharing.preview_import(data, home=self.home)
        self.assertEqual(plan['assignments']['beta'], ['claude'])

    def test_real_local_repository_to_receiving_agent(self):
        repository_url = self.fixture.repository('alpha')
        self.fixture.skill('repository/skills/beta', 'beta')
        repository = self.home / 'repository'
        sharing.github.git(repository, 'add', '.')
        sharing.github.git(repository, '-c', 'user.name=Test', '-c', 'user.email=test@local',
                           '-c', 'commit.gpgsign=false', 'commit', '-qm', 'add dependency')
        commit = sharing.github.git(repository, 'rev-parse', 'HEAD')
        data = self.config()
        for row in data['skills']:
            row['source']['commit'] = commit
        with patch.object(sharing.github, 'parse_url', return_value=(repository_url, 'demo/skills', commit)):
            result = sharing.import_config(data, home=self.home)
        self.assertTrue(result['complete'])
        self.assertEqual(visibility(self.home)['claude']['available'], ['alpha', 'beta', 'other'])
        self.assertEqual(operations.load(self.home)[1]['skills']['alpha']['source']['commit'], commit)

    def test_web_export_requires_session_and_works_readonly(self):
        display = self.write_labels()
        server = Dashboard({'home': str(self.home)})
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(server.origin + '/api/config/export')
            self.assertEqual(denied.exception.code, 403)
            request = urllib.request.Request(server.origin + '/api/config/export?agent=codex',
                                             headers={'X-Skill-Pilot-Token': server.token})
            before = self.fixture.fingerprint()
            with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), urllib.request.urlopen(request) as response:
                data = json.load(response)
            self.assertEqual(data['agents'], [{'id': 'codex', 'name': 'Codex'}])
            self.assertEqual(len(data['skills']), 3)
            row = next(row for row in data['skills'] if row['name'] == 'other')
            self.assertEqual({key: row[key] for key in display}, display)
            self.assertEqual(row['description'], '示例技能')
            self.assertEqual(self.fixture.fingerprint(), before)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
