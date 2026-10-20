"""团队页：只读本地文件，不启动看板，也不联网。"""
import re
import subprocess
import unittest
from pathlib import Path

from tests.test_nav_static import STATIC, extract_function


class TeamStaticTests(unittest.TestCase):
    def test_team_page_parts_exist(self):
        html = (STATIC / 'index.html').read_text()
        for marker in ('id="page-team"', 'id="block-team"', 'data-block="team"', 'id="team-upload-dialog"',
                       'id="team-import-dialog"', 'id="sp-team"'):
            self.assertIn(marker, html)
        self.assertRegex((STATIC / 'app.js').read_text(), r"const SETTINGS_BLOCKS = \[[^\]]*'team'")

    def test_team_settings_support_open_join(self):
        html = (STATIC / 'index.html').read_text()
        name_input = re.search(r'<input[^>]*id="team-name"[^>]*>', html)
        self.assertIsNotNone(name_input, '设置页没有“你的名字”输入框')
        self.assertIn('type="text"', name_input.group(0))
        self.assertIn('maxlength="40"', name_input.group(0))
        self.assertIn('autocomplete="off"', name_input.group(0))

        key_box = re.search(r'<div id="team-key-box"[^>]*>', html)
        self.assertIsNotNone(key_box, '口令区域不见了')
        self.assertIn('hidden', key_box.group(0), '口令区域应该默认收起')
        toggle = re.search(r'<button[^>]*id="team-key-toggle"[^>]*>', html)
        self.assertIsNotNone(toggle, '没有展开口令的按钮')
        self.assertIn('aria-controls="team-key-box"', toggle.group(0))
        self.assertIn('aria-expanded="false"', toggle.group(0))

        source = (STATIC / 'app.js').read_text()
        self.assertRegex(source, r"api\('/api/team/settings', \{[^}]*name: \$\('team-name'\)\.value\.trim\(\)[^}]*\}\)")

    def test_shared_text_is_escaped_and_filters_work(self):
        source = (STATIC / 'app.js').read_text()
        escape = re.search(r'const escape = value => [^\n]+', source).group(0)
        name_html = re.search(r'const teamNameHtml = [^\n]+', source).group(0)
        rows = extract_function(source, 'function teamRows(')
        script = f'''
{escape}
{name_html}
{rows}
const inputs = {{'team-search': {{value: ''}}, 'team-group': {{value: ''}}}};
const $ = id => inputs[id];
const state = {{teamDetail: {{config: {{skills: [
  {{name: 'acme-a', title: '<b>示例</b>', summary: '周报', group: '写作'}},
  {{name: 'acme-b', summary: '小工具', description: 'internal <script>'}},
]}}}}}};
function fail(message) {{ console.error(message); process.exit(1); }}
const html = teamNameHtml(state.teamDetail.config.skills[0]);
if (html.includes('<b>') || !html.includes('&lt;b&gt;')) fail('分享者给的中文名没有转成纯文字：' + html);
if (teamNameHtml({{name: '<i>x</i>'}}).includes('<i>')) fail('没有中文名时，英文名也要转义');
const names = () => teamRows().map(row => row.name).join(',');
inputs['team-group'].value = '写作';
if (names() !== 'acme-a') fail('按分组筛选不对：' + names());
inputs['team-group'].value = '-';
if (names() !== 'acme-b') fail('“没分组”应只留下没有分组的技能：' + names());
inputs['team-group'].value = '';
inputs['team-search'].value = 'INTERNAL';
if (names() !== 'acme-b') fail('搜索应能搜到说明，且不分大小写：' + names());
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
