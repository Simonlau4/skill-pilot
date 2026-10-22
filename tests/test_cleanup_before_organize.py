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
        self.assertIn('撤下：保留文件，随时可恢复；删除：从技能库删掉文件。', page)
        self.assertIn('id="cleanup-tabs"', page)
        self.assertIn('id="cleanup-select-all"', page)
        self.assertIn('id="cleanup-filter-agent"', page)
        self.assertIn('id="cleanup-filter-source"', page)
        self.assertNotIn('id="withdrawn-list"', page)
        self.assertNotIn('id="withdrawn-refresh"', page)
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
    hidden: id === 'cleanup-pending', innerHTML: '', value: '', textContent: '', title: '',
    disabled: false, dataset: {}, setAttribute() {}, removeAttribute() {}, querySelectorAll() { return []; },
  });
  return elements.get(id);
}
const state = {
  page: 'cleanup', table: null, cleanup: null, saved: {}, cleanupSelected: new Set(), idleSelected: new Set(),
  cleanupTab: 'on', cleanupFilters: {agent: '', group: '', tag: '', source: ''}, skillIndex: new Map(), agents: [],
  agentsView: null,
};
let writeBusy = null;
let homeVisible = false;
const calls = [];
const toasts = [];
function toast(item) { toasts.push(item.text); }
function hideActionBar() {}
function renderCleanupActionBar() { hideActionBar(); }
function renderCleanupTabs() {}
function renderCleanupFilters() {}
function cleanupTabSkills() { return []; }
function cleanupVisibleRows() { return []; }
function syncCleanupSelectAll() {}
function cleanupFiltersActive() { return false; }
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
  if (calls.includes('/api/cleanup')) fail('还没整理过不应去读清理名单：' + calls.join(','));
  if (toasts.length) fail('不应弹出报错：' + toasts.join('；'));
  if ($('cleanup-pending').hidden) fail('应显示还没整理过的说明');
  if (!$('cleanup-ready').hidden) fail('还没整理过时不应展示清理名单');
  if (rows.includes('正在读取')) fail('列表不应停在正在读取');

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

    def test_cleanup_rows_sort_and_filters(self):
        source = (STATIC / 'app.js').read_text()
        start = source.index('const cleanupSourceKind')
        source_kind = source[start:source.index('\n', start)]
        start = source.index('const cleanupAgentIds')
        agent_ids = source[start:source.index('\n\n', start)]
        script = '\n'.join([
            "const CLEANUP_SOURCES = {github: 'GitHub', lark: '飞书官方', team: '团队商店', skip: '自制'};",
            extract_function(source, 'function cleanupMeta('),
            source_kind,
            extract_function(source, 'function cleanupTabSkills('),
            agent_ids,
            extract_function(source, 'function cleanupMatchesFilters('),
            extract_function(source, 'function cleanupVisibleRows('),
            extract_function(source, 'function cleanupFilterOptions('),
            extract_function(source, 'function cleanupOptionEntries('),
            r'''
const skills = [
  {name: 'a', title: '甲', summary: '写', tags: ['工作'], description_tokens: 100, source_kind: 'github'},
  {name: 'b', title: '乙', summary: '读', tags: ['学习'], description_tokens: 300, source_kind: 'lark'},
  {name: 'c', title: '丙', summary: '旧', tags: ['工作'], description_tokens: 200, source_kind: 'skip',
    withdrawn: true, withdrawn_at: '2026-10-20T10:00:00Z', previous_agents: ['codex']},
  {name: 'd', title: '丁', summary: '团队', tags: [], description_tokens: 50, source_kind: 'team',
    withdrawn: true, withdrawn_at: '2026-10-21T10:00:00Z', previous_agents: ['claude']},
  {name: 'e', title: '戊', summary: '没用过', tags: [], description_tokens: 400, source_kind: 'github'},
];
let searchValue = '';
const state = {
  cleanupTab: 'on',
  cleanupFilters: {agent: '', group: '', tag: '', source: ''},
  saved: {a: ['codex'], b: ['codex', 'claude'], c: [], d: [], e: []},
  skillIndex: new Map(skills.map(skill => [skill.name, skill])),
  groupIndex: new Map([['a', '写作'], ['c', '写作']]),
  table: {skills, groups: [{name: '写作', skills: ['a', 'c']}]},
  agents: [{id: 'codex'}, {id: 'claude'}],
};
function $(id) { return {value: id === 'cleanup-search' ? searchValue : ''}; }
const titleOf = name => state.skillIndex.get(name).title;
const groupOf = name => state.groupIndex.get(name) || '未分组';
const nameOf = id => id;
const matchesQuery = (skill, query) => !query || [skill.name, skill.title, skill.summary, ...(skill.tags || [])]
  .some(text => (text || '').toLowerCase().includes(query));
const names = rows => rows.map(skill => skill.name).join(',');
function fail(message) { console.error(message); process.exit(1); }

const blank = cleanupMeta({name: 'blank'});
if (blank.withdrawn !== false || blank.withdrawn_at !== null || blank.previous_agents.length) {
  fail('缺少撤下字段时应按 false、null、空名单处理：' + JSON.stringify(blank));
}
if (cleanupSourceKind({source: {kind: 'github'}}) !== '') fail('来源只应看技能行的 source_kind');

if (names(cleanupTabSkills()) !== 'a,b') fail('在用栏应为 a,b：' + names(cleanupTabSkills()));
if (names(cleanupTabSkills('off')) !== 'c,d,e') fail('未启用栏应为 c,d,e：' + names(cleanupTabSkills('off')));
if (names(cleanupVisibleRows()) !== 'b,a') fail('在用栏应按 Token 从高到低：' + names(cleanupVisibleRows()));

state.cleanupTab = 'off';
if (names(cleanupVisibleRows()) !== 'd,c,e') fail('未启用栏应按撤下时间新的在前、没用过的按 Token：' + names(cleanupVisibleRows()));
state.cleanupFilters.agent = 'codex';
if (names(cleanupVisibleRows()) !== 'c') fail('按 Agent 筛选应看撤下前的分配：' + names(cleanupVisibleRows()));
state.cleanupFilters.agent = '';
state.cleanupFilters.group = '写作';
if (names(cleanupVisibleRows()) !== 'c') fail('按分组筛选未生效：' + names(cleanupVisibleRows()));
state.cleanupFilters.group = '';
state.cleanupFilters.tag = '工作';
if (names(cleanupVisibleRows()) !== 'c') fail('按标签筛选未生效：' + names(cleanupVisibleRows()));
state.cleanupFilters.tag = '';
state.cleanupFilters.source = 'github';
if (names(cleanupVisibleRows()) !== 'e') fail('按来源筛选未生效：' + names(cleanupVisibleRows()));
state.cleanupFilters.source = '';
searchValue = '没';
if (names(cleanupVisibleRows()) !== 'e') fail('搜索说明未生效：' + names(cleanupVisibleRows()));
searchValue = '';

const off = cleanupFilterOptions(cleanupTabSkills());
const counts = map => [...map.entries()].map(([key, count]) => `${key}:${count}`).join(',');
if (counts(off.agent) !== 'codex:1,claude:1') fail('未启用栏的 Agent 数量不对：' + counts(off.agent));
if (counts(off.source) !== 'skip:1,team:1,github:1') fail('未启用栏的来源数量不对：' + counts(off.source));
const on = cleanupFilterOptions(cleanupTabSkills('on'));
if (counts(on.group) !== '写作:1,未分组:1') fail('在用栏的分组数量不对：' + counts(on.group));
if (counts(on.tag) !== '工作:1,学习:1') fail('在用栏的标签数量不对：' + counts(on.tag));
''',
        ])
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_action_bar_show_cancels_pending_hide(self):
        source = (STATIC / 'app.js').read_text()
        show = source[source.index('function showActionBar('):source.index('function hideActionBar(')].rstrip()
        script = '\n'.join([
            'let actionBarHideTimer = null;',
            show,
            extract_function(source, 'function hideActionBar('),
            r'''
class ClassList {
  constructor() { this.values = new Set(); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  contains(name) { return this.values.has(name); }
}
function element(extra = {}) {
  return {hidden: false, innerHTML: '', textContent: '', classList: new ClassList(), append() {}, querySelector() { return null; }, ...extra};
}
const bar = element({hidden: true});
const byId = {
  'action-bar': bar,
  'action-summary': element(),
  'action-note': element(),
  'action-buttons': element(),
  'action-list': element(),
  'action-extra': element(),
};
function $(id) { return byId[id]; }
const document = {body: {classList: new ClassList()}, createElement: () => element({addEventListener() {}})};
const frames = [];
function requestAnimationFrame(callback) { frames.push(callback); return frames.length; }
function runFrames() { while (frames.length) frames.shift()(); }
const timers = new Map();
let nextTimer = 1;
function setTimeout(callback) { const id = nextTimer++; timers.set(id, callback); return id; }
function clearTimeout(id) { timers.delete(id); }
function runTimers() { const pending = [...timers.values()]; timers.clear(); for (const callback of pending) callback(); }
function fail(message) { console.error(message); process.exit(1); }

hideActionBar();
showActionBar({text: '已选 1 个技能'});
if (bar.hidden) fail('重新显示托盘时不应留着旧的隐藏状态');
runTimers();
if (bar.hidden) fail('显示托盘后应取消尚未执行的隐藏');
runFrames();
if (!bar.classList.contains('is-open')) fail('显示托盘后应加上打开状态');

hideActionBar();
if (bar.classList.contains('is-open')) fail('收起托盘时应先移除打开状态');
runTimers();
if (!bar.hidden) fail('收起 200 毫秒后应真正隐藏');
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
