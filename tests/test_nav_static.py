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
    def test_header_starts_at_home_and_start_always_shows_home(self):
        html = (STATIC / 'index.html').read_text()
        nav = re.search(r'<nav class="nav"[\s\S]*?</nav>', html)
        self.assertIsNotNone(nav, '页头导航不见了')
        items = re.findall(r'data-page="([^"]+)"[\s\S]*?<span>([^<]+)</span>', nav.group(0))
        self.assertEqual(items, [('home', '首页'), ('skills', '技能'), ('cleanup', '清理'), ('team', '团队'), ('settings', '设置')])
        self.assertEqual(nav.group(0).count('class="nav-item"'), 5)
        self.assertLess(nav.group(0).index('data-page="home"'), nav.group(0).index('data-page="skills"'))
        # 「团队」只在连上分享站后出现，默认藏着。
        self.assertRegex(nav.group(0), r'data-page="team" hidden')
        self.assertIn('data-page="home" aria-current="page"', nav.group(0))
        self.assertIn('id="brand-home"', html)
        self.assertIn('aria-label="回到首页"', html)
        self.assertIn('id="mode-badge"', html)
        self.assertIn('只读预览', html)

        source = (STATIC / 'app.js').read_text()
        self.assertIn("page: 'home'", source)
        self.assertNotIn('function placeFromHash(', source)
        self.assertNotIn('placeFromHash()', source)
        start = extract_function(source, 'async function start(')
        self.assertIn("showPage('home')", start)
        self.assertNotIn('startPlace', start)

        function = extract_function(source, 'function rememberPlace(')
        script = function + r'''
const state = {page: 'home', agent: null, settingsBlock: 'ai'};
const location = {pathname: '/', search: '?token=secret'};
let written = '';
const history = {replaceState: (unused, title, url) => { written = url; }};
rememberPlace();
if (!written.endsWith('#home')) { console.error('首页地址应记成 #home：' + written); process.exit(1); }
state.page = 'skills';
state.agent = 'Acme';
rememberPlace();
if (!written.endsWith('#skills/Acme')) { console.error('技能页地址没写对：' + written); process.exit(1); }
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_home_section_and_gsap_scripts_are_wired(self):
        html = (STATIC / 'index.html').read_text()
        self.assertIn('id="page-home"', html)
        self.assertIn('id="home-guide"', html)
        self.assertIn('id="home-main"', html)
        self.assertIn('id="home-map"', html)
        self.assertIn('id="home-agents"', html)
        self.assertIn('id="home-todos"', html)
        self.assertIn('id="home-log"', html)
        # 页面脚本等待读取数据时，导航和内容也都停在首页。
        self.assertRegex(html, r'<section id="page-home" class="page-section">')
        self.assertRegex(html, r'<section id="page-skills" class="page-section" hidden>')
        self.assertRegex(html, r'<p id="home-loading"[^>]*role="status">正在读取技能…</p>')

        app = html.index('/app.js?token=__SESSION_TOKEN__')
        gsap = html.index('/vendor/gsap.min.js?token=__SESSION_TOKEN__')
        trigger = html.index('/vendor/ScrollTrigger.min.js?token=__SESSION_TOKEN__')
        self.assertLess(gsap, trigger)
        self.assertLess(trigger, app)
        for path in ('/vendor/gsap.min.js', '/vendor/ScrollTrigger.min.js'):
            tag = re.search(r'<script[^>]*src="' + re.escape(path) + r'[^"]*"[^>]*>', html)
            self.assertIsNotNone(tag, path + ' 没有加载')
            self.assertIn('defer', tag.group(0), path + ' 应该用 defer')
        self.assertNotRegex(html, r'<script(?![^>]*\bsrc=)[^>]*>', '首页不能写内联脚本')

        for name, marker in (('gsap.min.js', 'GSAP 3.13.0'), ('ScrollTrigger.min.js', 'ScrollTrigger 3.13.0')):
            text = (STATIC / 'vendor' / name).read_text()
            self.assertTrue(text.startswith('/*!'), name + ' 开头的版权说明不见了')
            self.assertIn('@license', text[:400])
            self.assertIn(marker, text[:400])

        source = (STATIC / 'app.js').read_text()
        # 星图的点不进 Tab 顺序，鼠标照样能点；分组名按钮仍然可以 Tab 到。
        self.assertIn('class="home-dot ${kind}" tabindex="-1"', source)
        self.assertIn('class="home-cluster-name"', source)
        self.assertNotIn("$('home-map').addEventListener('focusin'", source)
        self.assertNotIn("$('home-map').addEventListener('focusout'", source)

    def test_program_messages_use_full_name_without_changing_backend_state(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'function renderProgram(')
        script = function + r'''
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {setAttribute() {}, querySelector() { return this; }});
  return elements.get(id);
}
const tasks = new Map();
const setTask = (key, task) => tasks.set(key, task);
const clearTask = key => tasks.delete(key);
const renderRailNotes = () => {};
const homeVisible = false, programActionRunning = false, writeBusy = false;
let programUpdate = {phase: 'latest', message: 'SP 已是最新版本', write_allowed: true};
renderProgram();
if ($('program-message').textContent !== 'Skill Pilot 已是最新版本') throw new Error('版本提示仍显示旧简称');
if (programUpdate.message !== 'SP 已是最新版本') throw new Error('不应改写后台返回的数据');
programUpdate = {...programUpdate, phase: 'check', running: true, message: '正在检查 SP 版本'};
renderProgram();
if (tasks.get('self-update').text !== '正在检查 Skill Pilot 版本') throw new Error('页头进度仍显示旧简称');
programUpdate = {phase: 'latest', message: 'Skill Pilot 已是最新版本'};
renderProgram();
if ($('program-message').textContent !== programUpdate.message) throw new Error('完整名称不应重复替换');
programUpdate = {phase: 'idle'};
renderProgram();
if ($('program-message').textContent !== '从 Skill Pilot 官方项目获取新版') throw new Error('缺少默认提示');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_home_navigation_auto_checks_program(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'function showPage(')
        script = function + r'''
let checks = 0;
async function autoCheckProgram() { checks++; }
function fail(message) { console.error(message); process.exit(1); }
const state = {page: 'skills', agent: 'Acme'};
let homeVisible = true;
let homeIntroPlayed = true;
const window = {scrollTo() {}};
const document = {
  querySelectorAll: () => [],
  querySelector: () => null,
};
const requestAnimationFrame = () => {};
function rememberPlace() {}
function glideSelection() {}
function renderCleanup() {}
function loadCleanup() {}
function renderSettings() {}
function enterTeam() {}
function renderHome() {}
function homeSetScrollEnabled() {}
function ensureHomeCleanup() {}
function homeHideTip() {}
function renderActionBar() {}
function fitFilterRail() {}
function refreshHomeScroll() {}

showPage('home');
if (checks !== 1) fail('回到首页应自动检查一次新版，实际 ' + checks);
showPage('skills');
showPage('cleanup');
showPage('settings');
showPage('team');
if (checks !== 1) fail('切到其他页不应再检查新版，实际 ' + checks);
showPage('home');
if (checks !== 2) fail('再次回到首页应再问一次，实际 ' + checks);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_auto_check_program_posts_and_falls_back_quietly(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'async function autoCheckProgram(')
        script = function + r'''
const requests = [];
let mode = 'ok';
async function api(path, body) {
  if (body === undefined) throw new Error('自动检查应发 POST');
  requests.push({path, body});
  if (mode === 'fail') throw new Error('网络断了');
  return {available: true, running: false, phase: 'available', write_allowed: true};
}
let programUpdate = {running: false, phase: 'idle', available: false, write_allowed: false};
let renders = 0;
function renderProgram() { renders++; }
let refreshes = 0;
async function refreshProgramUpdate() { refreshes++; }
const touched = [];
function $(id) { touched.push(id); return {textContent: '', hidden: true}; }
function fail(message) { console.error(message); process.exit(1); }

(async () => {
  await autoCheckProgram();
  if (requests.length !== 1) fail('自动检查应只发一次请求，实际 ' + requests.length);
  if (requests[0].path !== '/api/program/auto-check') fail('自动检查的接口不对：' + requests[0].path);
  if (JSON.stringify(requests[0].body) !== '{}') fail('自动检查的请求体应为空对象：' + JSON.stringify(requests[0].body));
  if (programUpdate.available !== true) fail('后台返回的新版进度没有并进来');
  if (renders !== 1) fail('拿到进度后应重画一次，实际 ' + renders);
  if (refreshes !== 0) fail('成功时不应再读一次进度');

  mode = 'fail';
  renders = 0;
  await autoCheckProgram();
  if (refreshes !== 1) fail('接口出错时应退回读一次当前进度，实际 ' + refreshes);
  if (renders !== 0) fail('接口出错时不应重画成错误');
  if (touched.includes('program-error')) fail('接口出错时不应写页头错误');
  if (programUpdate.available !== true) fail('出错不应丢掉已有的新版进度');
})().catch(error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_home_todo_rules(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'function homeTodos(')
        script = function + r'''
const homeNumber = value => Number(value || 0).toLocaleString('zh-CN');
function fail(message) { console.error(message); process.exit(1); }
function keys(items) { return items.map(item => item.key).join(','); }
function only(items, key) { return items.length === 1 && items[0].key === key; }

const checked = extra => homeTodos({llm_configured: true, updates: {review: {update_count: 0}, count: 0}, ...extra});

const never = checked({updates: {review: null, starting: false}});
if (!only(never, 'updates-unchecked')) fail('从没检查过时应只有「尚未检查技能更新」：' + keys(never));
if (never[0].label !== '立即检查' || never[0].action !== 'check-updates') fail('没检查过时应给「立即检查」按钮');

const checking = checked({updates: {starting: true, review: null}});
if (!only(checking, 'updates-checking')) fail('正在检查时应只有「正在检查更新」：' + keys(checking));
if (checking[0].action || checking[0].target) fail('正在检查时不应放按钮');

// 数量用当前技能表里还有新版的个数，不用 review.update_count（更新完一个以后它会变少）。
const found = checked({updates: {review: {running: false, total: 3, done: 3, update_count: 2}, count: 1}});
if (keys(found) !== 'updates-found') fail('有新版时应显示新版条：' + keys(found));
if (found[0].title !== '1 个技能有可用更新' || found[0].target !== 'skills/updates') fail('新版条应按当前表里的数量写：' + found[0].title);

const latest = checked({updates: {review: {running: false, total: 3, done: 3, update_count: 3}, count: 0}});
if (latest.length) fail('检查后没有新版时不应显示更新条：' + keys(latest));

const suggested = checked({suggested: ['Acme', '示例 agent']});
if (keys(suggested) !== 'suggested') fail('发现新 agent 时应显示接入条：' + keys(suggested));
if (!suggested[0].text.includes('Acme') || !suggested[0].text.includes('示例 agent')) fail('新 agent 条应写出名字');
if (suggested[0].target !== 'settings/agents') fail('新 agent 条应去设置的 agent 一块');

const aiPending = homeTodos({llm_configured: false, labels_pending: 4, updates: {review: {update_count: 0}}});
if (keys(aiPending) !== 'ai') fail('没设 AI 服务时应提醒：' + keys(aiPending));
if (aiPending[0].title !== '未配置 AI 整理服务' || aiPending[0].target !== 'settings/ai') fail('AI 提醒的标题或去处不对：' + JSON.stringify(aiPending[0]));
if (!aiPending[0].text.includes('4 个技能缺少中文名称或说明')) fail('有缺中文名的技能时应写数量：' + aiPending[0].text);
const aiNoPending = homeTodos({llm_configured: false, labels_pending: 0, updates: {review: {update_count: 0}}});
if (keys(aiNoPending) !== 'ai') fail('没设 AI 服务时应提醒：' + keys(aiNoPending));
if (aiNoPending[0].text !== '配置 AI 服务后，将自动为新技能生成中文名称、说明与用途分组。') fail('没有缺中文名的技能时说明不对：' + aiNoPending[0].text);
const aiConfigured = homeTodos({llm_configured: true, labels_pending: 5, updates: {review: {update_count: 0}}});
if (aiConfigured.length) fail('已经设置 AI 服务时不应提醒：' + keys(aiConfigured));

const withdrawn = checked({withdrawn: 3});
if (keys(withdrawn) !== 'withdrawn' || !withdrawn[0].title.includes('3')) fail('撤下过的技能应显示数量：' + JSON.stringify(withdrawn));
if (withdrawn[0].target !== 'cleanup') fail('撤下过的技能应去清理页');

const unset = checked({team: {configured: false, member: null}});
if (keys(unset) !== 'team' || unset[0].title !== '未连接团队分享站') fail('没连分享站时应提醒连接：' + JSON.stringify(unset));
if (unset[0].target !== 'settings/team') fail('没连分享站时应去设置的团队分享一块');
const connected = checked({team: {configured: true, member: {name: 'Acme'}}});
if (connected.length) fail('连上分享站后不应再占一条团队提醒：' + keys(connected));

const program = checked({program: {write_allowed: true, available: true, phase: 'idle'}});
if (keys(program) !== 'program' || program[0].target !== 'settings/version') fail('SP 有新版时应去设置的版本一块：' + JSON.stringify(program));
const programHidden = checked({program: {write_allowed: false, available: true, phase: 'idle'}});
if (programHidden.length) fail('不能升级时不应显示 SP 新版条');

const all = homeTodos({
  program: {write_allowed: true, phase: 'ready'},
  updates: {review: {running: false, total: 5, done: 5, update_count: 5}, count: 1},
  suggested: ['Acme'],
  llm_configured: false,
  labels_pending: 2,
  withdrawn: 2,
  team: {configured: false, member: null},
});
const expected = 'program,updates-found,suggested,ai,withdrawn,team';
if (keys(all) !== expected) fail('条目顺序应按版本、更新、新 agent、AI、撤下、团队排：' + keys(all));

const none = checked({updates: {review: {running: false, total: 5, done: 5, update_count: 0}}, team: {configured: true, member: {name: 'Acme'}}});
if (none.length) fail('全都没有时不应有提醒条：' + keys(none));
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_home_intro_stays_short_for_many_agents_and_groups(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'function playHomeIntro(')
        script = '(function () {\n' + function + r'''
const window = {gsap: require(process.argv[1]).gsap};
const node = () => ({querySelector: () => ({}), querySelectorAll: () => []});
const hero = node();
let groupCount = 0;
hero.querySelectorAll = name => name === '.home-cluster-name' ? Array.from({length: groupCount}, node) : [];
const $ = id => id === 'home-hero' ? hero : node();
const document = {querySelector: node};
let state = {table: {skills: []}, agents: []};
let homeAgentCards = [], homeDots = [], homeIntroPlayed, homeIntroDone, homeIntroTimeline;
const homeMotionOn = true;
const homeSetNumbersZero = () => {};
const homeInUseCount = () => 0;
const homeCountTo = () => {};
for (const [groups, agents] of [[0, 0], [3, 4], [80, 60]]) {
  groupCount = groups;
  homeAgentCards = Array.from({length: agents}, node);
  state.agents = homeAgentCards;
  playHomeIntro();
  homeIntroTimeline.pause();
  const duration = homeIntroTimeline.totalDuration();
  homeIntroTimeline.kill();
  if (duration >= 2) throw new Error(`${groups} 个分组、${agents} 个 agent 的开场长达 ${duration} 秒`);
}
window.gsap.ticker.sleep();
})();
'''
        result = subprocess.run(['node', '-e', script, str(STATIC / 'vendor' / 'gsap.min.js')], capture_output=True, text=True)
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
