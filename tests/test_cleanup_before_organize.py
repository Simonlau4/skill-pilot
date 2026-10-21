"""还没整理过时打开清理页：不读清理名单，也不用旧说法报错。"""
import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from skill_pilot import cleanup, operations
from tests import set_up_home


STATIC = Path(__file__).resolve().parents[1] / 'skill_pilot' / 'web' / 'static'


def _skip_string(source, index):
    quote = source[index]
    index += 1
    while index < len(source):
        if source[index] == '\\':
            index += 2
            continue
        if quote == '`' and source[index] == '$' and source.startswith('{', index + 1):
            index += 2
            depth = 1
            while index < len(source) and depth:
                if source[index] in '"\'`':
                    index = _skip_string(source, index)
                    continue
                if source[index] == '{':
                    depth += 1
                elif source[index] == '}':
                    depth -= 1
                index += 1
            continue
        if source[index] == quote:
            return index + 1
        index += 1
    return index


def extract_function(source, signature):
    start = source.index(signature)
    index = source.index('{', start)
    depth = 0
    while index < len(source):
        char = source[index]
        if char in '"\'`':
            index = _skip_string(source, index)
            continue
        if char == '/' and source.startswith('/', index + 1):
            index = source.find('\n', index)
            index = len(source) if index < 0 else index + 1
            continue
        if char == '/' and source.startswith('*', index + 1):
            index = source.find('*/', index + 2)
            index = len(source) if index < 0 else index + 2
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise AssertionError('没有找到完整的函数：' + signature)


class CleanupBeforeOrganizeTests(unittest.TestCase):
    def test_missing_registry_uses_current_wording(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot cleanup before ')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, Path(temporary.name) / 'Another User')
        self.home.mkdir()
        for call in (operations.load, cleanup.list_withdrawn):
            with self.subTest(call=call.__name__):
                with self.assertRaises(ValueError) as caught:
                    call(self.home)
                self.assertEqual(str(caught.exception), '请先完成首次整理，再管理技能分配。')

    def test_cleanup_page_explains_instead_of_loading(self):
        html = (STATIC / 'index.html').read_text()
        source = (STATIC / 'app.js').read_text()
        page = html.split('id="page-cleanup"', 1)[1].split('id="page-settings"', 1)[0]
        self.assertIn('完成首次整理后，即可查看和清理已启用的技能', page)
        self.assertIn('开始整理技能库', page)
        self.assertIn("$('cleanup-organize').addEventListener('click', () => showPage('skills'));", source)
        self.assertNotIn('首次迁移', page)
        self.assertNotIn('正在读取', page)
        script = '\n'.join([
            extract_function(source, 'function awaitingFirstOrganization('),
            extract_function(source, 'function renderCleanup('),
            extract_function(source, 'async function loadCleanup('),
            r'''
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {
    hidden: id === 'cleanup-pending', innerHTML: '', value: '', textContent: '', disabled: false,
  });
  return elements.get(id);
}
const state = {page: 'cleanup', table: null, cleanup: null, saved: {}, cleanupSelected: new Set(), agentsView: null};
let writeBusy = null;
let homeVisible = false;
const calls = [];
const toasts = [];
function toast(item) { toasts.push(item.text); }
function hideActionBar() {}
function renderCleanupActionBar() { hideActionBar(); }
function cleanupRows() { return []; }
function escape(value) { return String(value); }
function nameHtml(value) { return String(value); }
function tokenText() { return ''; }
function swatch() { return ''; }
function nameOf(id) { return id; }
function emptyInner(_icon, text) { return text; }
function icon() { return ''; }
async function refreshAgents() { state.agentsView = {managed: []}; }
async function api(path) {
  calls.push(path);
  if (path === '/api/cleanup') throw new Error('请先完成首次迁移，再管理技能分配。');
  return {managed: []};
}
function fail(message) { console.error(message); process.exit(1); }
(async () => {
  await loadCleanup();
  const rows = $('cleanup-rows').innerHTML;
  const withdrawn = $('withdrawn-list').innerHTML;
  if (calls.includes('/api/cleanup')) fail('还没整理过不应去读清理名单：' + calls.join(','));
  if (toasts.length) fail('不应弹出报错：' + toasts.join('；'));
  if ($('cleanup-pending').hidden) fail('应显示还没整理过的说明');
  if (!$('cleanup-ready').hidden) fail('还没整理过时不应展示清理名单');
  if (rows.includes('正在读取') || withdrawn.includes('正在读取')) fail('列表不应停在正在读取');

  calls.length = 0;
  toasts.length = 0;
  state.agentsView = {managed: [{id: 'codex'}]};
  state.cleanup = null;
  await loadCleanup();
  if (!calls.includes('/api/cleanup')) fail('整理过以后仍应读取清理名单');
  if ($('cleanup-pending').hidden !== true) fail('整理过以后不应再显示首次整理说明');
})().catch(error => { console.error(error); process.exit(1); });
''',
        ])
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_user_visible_wording_does_not_say_migration(self):
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in (root / 'skill_pilot').rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and '迁移' in node.value:
                    offenders.append(f'{path.relative_to(root)}:{node.lineno}: {node.value}')
        pages = {
            'README.md': (root / 'README.md').read_text(),
            'skill/skill-pilot/SKILL.md': (root / 'skill/skill-pilot/SKILL.md').read_text(),
            'skill_pilot/web/static/index.html': re.sub(r'<!--.*?-->', '', (root / 'skill_pilot/web/static/index.html').read_text(), flags=re.S),
            'skill_pilot/web/static/app.js': re.sub(r'/\*.*?\*/|//.*?$', '', (root / 'skill_pilot/web/static/app.js').read_text(), flags=re.S | re.M),
        }
        for name, text in pages.items():
            if '迁移' in text:
                offenders.append(name)
        self.assertEqual(offenders, [])
