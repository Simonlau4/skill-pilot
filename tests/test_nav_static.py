"""页头导航和只读标记：只读本地文件，不启动看板，也不联网。"""
import re
import subprocess
import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / 'skill_pilot' / 'web' / 'static'


def extract_function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError('没有找到完整的函数：' + signature)


class NavStaticTests(unittest.TestCase):
    def test_header_items_and_old_addresses_open_settings(self):
        html = (STATIC / 'index.html').read_text()
        nav = re.search(r'<nav class="nav"[\s\S]*?</nav>', html)
        self.assertIsNotNone(nav, '页头导航不见了')
        items = re.findall(r'data-page="([^"]+)"[\s\S]*?<span>([^<]+)</span>', nav.group(0))
        self.assertEqual(items, [('skills', '技能'), ('cleanup', '清理'), ('team', '团队'), ('settings', '设置')])
        self.assertEqual(nav.group(0).count('class="nav-item"'), 4)
        # 「团队」只在连上分享站后出现，默认藏着。
        self.assertRegex(nav.group(0), r'data-page="team" hidden')
        self.assertIn('id="mode-badge"', html)
        self.assertIn('只读预览', html)

        source = (STATIC / 'app.js').read_text()
        blocks = re.search(r'const SETTINGS_BLOCKS = (\[[^\]]+\])', source)
        self.assertIsNotNone(blocks, '设置页的分块名单不见了')
        function = extract_function(source, 'function placeFromHash(')
        script = f'''
const SETTINGS_BLOCKS = {blocks.group(1)};
{function}
const cases = [
  ['#log', 'settings', 'log'],
  ['#diagnostics', 'settings', 'diagnostics'],
  ['#settings/log', 'settings', 'log'],
  ['#settings/diagnostics', 'settings', 'diagnostics'],
  ['#cleanup', 'cleanup', ''],
  ['#team', 'team', ''],
  ['#settings/team', 'settings', 'team'],
  ['#skills', 'skills', ''],
];
for (const [hash, page, block] of cases) {{
  globalThis.location = {{hash}};
  const place = placeFromHash();
  if (place.page !== page || (block && place.block !== block)) {{
    console.error(hash + ' -> ' + JSON.stringify(place));
    process.exit(1);
  }}
}}
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unorganized_readonly_user_sees_badge(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'async function refreshMigrationStatus(')
        script = function + r'''
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {hidden: true, disabled: false, textContent: ''});
  return elements.get(id);
}
const state = {table: null, writeAllowed: true};
let readOnly = true;
function renderMigrationMode() {}
function renderTeamSettings() {}
function migrationError(error) { throw error; }
async function api(path) {
  if (path !== '/api/status') throw new Error(path);
  return {read_only: readOnly};
}
function fail(message) { console.error(message); process.exit(1); }
(async () => {
  readOnly = true;
  state.table = null;
  state.writeAllowed = true;
  $('mode-badge').hidden = true;
  await refreshMigrationStatus();
  if ($('mode-badge').hidden !== false) fail('还没整理过、又是只读时，右上角应显示「只读预览」');
  if (state.writeAllowed !== false) fail('只读状态没有记下来');

  readOnly = false;
  state.table = null;
  $('mode-badge').hidden = true;
  await refreshMigrationStatus();
  if ($('mode-badge').hidden !== true) fail('还没整理过、但可以修改时，不应显示「只读预览」');

  readOnly = true;
  state.table = {write_allowed: false};
  $('mode-badge').hidden = false;
  await refreshMigrationStatus();
  if ($('mode-badge').hidden !== false) fail('已经整理过的只读标记不应被清掉');
})().catch(error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
