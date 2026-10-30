"""团队页（团队技能商店）：只读本地文件，不启动看板，也不联网。"""
import re
import subprocess
import unittest

from tests.test_nav_static import STATIC, extract_function


def line(source, prefix):
    return re.search(re.escape(prefix) + r'[^\n]+', source).group(0)


def block(source, signature):
    """参数里带花括号的函数，按行首的右花括号取到函数结尾。"""
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 2]


class TeamStaticTests(unittest.TestCase):
    def test_store_page_parts_exist(self):
        html = (STATIC / 'index.html').read_text()
        for marker in ('id="page-team"', 'id="block-team"', 'data-block="team"', 'id="store-publish-dialog"',
                       'id="store-search"', 'id="store-shelves"', 'id="store-list"', 'id="store-queue"',
                       'id="sp-team"', 'id="sp-star"', 'id="sp-stack"',
                       'id="store-hints"', 'id="store-pack-dialog"', 'id="store-pack-list"'):
            self.assertIn(marker, html)
        # 第一期按人看名单的页面和对话框已经换成商店。
        for old in ('team-upload-dialog', 'team-import-dialog', 'team-shares', 'team-detail'):
            self.assertNotIn(old, html)
        # 安装队列夹在错误提示和推荐卡片之间，状态区域用礼貌播报。
        positions = [html.index(f'id="{name}"') for name in ('store-error', 'store-queue', 'store-shelves')]
        self.assertEqual(positions, sorted(positions), '安装队列要放在 #store-error 和 #store-shelves 之间')
        queue_items = re.search(r'<div[^>]*id="store-queue-items"[^>]*>', html)
        self.assertIsNotNone(queue_items, '安装队列的状态区域不见了')
        self.assertIn('aria-live="polite"', queue_items.group(0))
        self.assertRegex((STATIC / 'app.js').read_text(), r"const SETTINGS_BLOCKS = \[[^\]]*'team'")

    def test_store_github_trending_shelf(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const number = value => '),
            line(source, 'const SHELF_SIZE = '),
            line(source, 'const githubRepos = '),
            line(source, 'const queueActive = '),
            line(source, 'const storeQueueItem = '),
            line(source, 'const githubQueueKey = '),
            extract_function(source, 'const STORE_STATUS = {') + ';',
            extract_function(source, 'function githubCreatedText('),
            extract_function(source, 'function githubStatusTag('),
            extract_function(source, 'function githubInstallHtml('),
            extract_function(source, 'function githubCardHtml('),
            extract_function(source, 'function storeGithubShelfHtml('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
const state = {queue: {items: []}};
let readOnly = false;
const teamReadOnly = () => readOnly;
const now = Date.now();
const repo = (extra = {}) => ({
  repository: 'acme/example-skills', url: 'https://github.com/acme/example-skills',
  description: 'Example <b>agent</b> skills', stars: 1234,
  created_at: new Date(now - 3 * 86400000).toISOString(), pushed_at: new Date(now).toISOString(),
  skill_count: 3, local: {status: 'installable'}, ...extra,
});
const card = githubCardHtml(repo());
if (!card.startsWith('<div class="store-card github-card" data-github-card="acme/example-skills">')) fail('热门卡片外层不能用按钮：' + card);
if (card.startsWith('<button')) fail('热门卡片外层不该是按钮：' + card);
if (!card.includes('data-github-open="acme/example-skills"')) fail('标题要做成打开详情的按钮：' + card);
if (!card.includes('title="acme/example-skills"')) fail('完整 owner/name 要放进 title 属性，悬停能看到：' + card);
if (!card.includes('<span class="tag tag-quiet">可安装</span>')) fail('可安装的状态标签要沿用团队卡片写法：' + card);
if (!card.includes('1,234 星')) fail('底行要写星数：' + card);
if (!card.includes('3 天前新建')) fail('底行要写几天前新建：' + card);
if (!card.includes('含 3 个技能')) fail('有技能个数时要写含几个技能：' + card);
if (card.includes('sp-star')) fail('星数不能用团队评分的星形图标');
if (!card.includes('&lt;b&gt;agent&lt;/b&gt;')) fail('仓库说明要按纯文字转义：' + card);
if (!card.includes('data-github-install="acme/example-skills"')) fail('可安装的卡片要有安装入口：' + card);
if (!card.includes('>安装</button>') || card.includes('前往安装')) fail('安装入口写“安装”，点了直接排队，不再跳到技能页：' + card);
if (!card.includes('aria-label="安装 acme/example-skills"')) fail('安装按钮的读屏文字要带仓库名：' + card);

state.queue.items = [{kind: 'github_install', key: 'github:acme/example-skills', repository: 'acme/example-skills', status: 'waiting'}];
const waiting = githubCardHtml(repo());
if (!waiting.includes('<span class="tag tag-quiet">排队中</span>') || waiting.includes('可安装')) fail('排队中时标签写排队中：' + waiting);
if (waiting.includes('data-github-install')) fail('排队中时不再显示安装按钮：' + waiting);
state.queue.items[0].status = 'running';
const running = githubCardHtml(repo());
if (!running.includes('正在安装') || running.includes('data-github-install')) fail('正在安装时写正在安装、不显示按钮：' + running);
state.queue.items[0].status = 'failed';
if (!githubCardHtml(repo()).includes('data-github-install')) fail('装失败后还能再点安装');
state.queue.items = [{kind: 'team_install', key: 'github:acme/example-skills:skills/a', status: 'waiting'}];
if (!githubCardHtml(repo()).includes('可安装')) fail('团队技能的队列编号不能算到热门仓库上');
state.queue.items = [];
if (!card.includes('target="_blank"') || !card.includes('rel="noopener noreferrer"')) fail('GitHub 链接要在新窗口打开');
if (!card.includes('href="https://github.com/acme/example-skills"')) fail('链接地址要用后台给的 url');
if (!card.includes('打开 GitHub')) fail('卡片要有打开 GitHub 页面的链接');

const installed = githubCardHtml(repo({local: {status: 'installed'}}));
if (!installed.includes('<span class="tag tag-ok">已安装</span>')) fail('已安装的状态标签不对：' + installed);
if (installed.includes('data-github-install')) fail('已安装的卡片不该再显示安装按钮：' + installed);
const bare = githubCardHtml(repo({description: '', skill_count: null}));
if (!bare.includes('仓库没有写说明')) fail('没有说明时要写“仓库没有写说明”');
if (bare.includes('含 ')) fail('技能个数未知时不该写含几个技能：' + bare);

readOnly = true;
const locked = githubCardHtml(repo());
if (!locked.includes('disabled') || !locked.includes('只读预览，无法安装技能。')) fail('只读预览时安装按钮要禁用并写原因：' + locked);
readOnly = false;

const repos = Array.from({length: 6}, (_, index) => repo({repository: `acme/skills-${index}`, stars: 6 - index}));
Object.assign(state, {store: {github: {repos}}, storeGithubExpanded: false});
let shelf = storeGithubShelfHtml();
if (!shelf.includes('<h2 id="shelf-github">GitHub 最近热门</h2>')) fail('缺少“GitHub 最近热门”标题');
if ((shelf.match(/class="store-card github-card"/g) || []).length !== 4) fail('默认只显示 4 张：' + shelf);
if (!shelf.includes('展开全部 6 个') || !shelf.includes('aria-expanded="false"')) fail('默认要有展开全部按钮：' + shelf);
if (shelf.includes('>收起<')) fail('默认不该显示收起');
state.storeGithubExpanded = true;
shelf = storeGithubShelfHtml();
if ((shelf.match(/class="store-card github-card"/g) || []).length !== 6) fail('展开后要显示全部 6 张：' + shelf);
if (!shelf.includes('收起') || !shelf.includes('aria-expanded="true"') || !shelf.includes('is-expanded')) fail('展开后要有收起状态：' + shelf);
state.store = null;
if (storeGithubShelfHtml() !== '') fail('没有 github 字段时不该显示这一排');
state.store = {github: {repos: []}};
if (storeGithubShelfHtml() !== '') fail('名单为空时不该显示这一排');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # 空状态、技能少、正常三处都要接在后面；搜索、选分组、看「我的上架」和读取中不显示。
        shelves = extract_function(source, 'function renderStoreShelves(')
        self.assertIn("+ (storeBrowsing() ? storeGithubShelfHtml() : '')", shelves)
        self.assertIn('box.innerHTML = packShelf + githubShelf;', shelves)
        self.assertIn("}).join('') + githubShelf;", shelves)
        self.assertLess(shelves.index('if (!storeBrowsing())'), shelves.index('const githubShelf'),
                        '要先判断是不是浏览状态，再准备这一排')
        loading = shelves[:shelves.index('if (!cards.length)')]
        self.assertNotIn('storeGithubShelfHtml', loading, '读取目录时不该显示这一排')

        # 标题只占一行，超出省略；完整 owner/name 靠 title 属性。
        css = (STATIC / 'app.css').read_text()
        title_rule = re.search(r'\.github-card \.store-card-title\s*\{([^}]*)\}', css)
        self.assertIsNotNone(title_rule, '热门卡片的标题样式不见了')
        for declaration in ('display: block', 'white-space: nowrap', 'overflow: hidden',
                            'text-overflow: ellipsis', 'overflow-wrap: normal'):
            self.assertIn(declaration, title_rule.group(1), f'标题没写 {declaration}，会在单词中间断行')
        self.assertNotIn('-webkit-line-clamp', title_rule.group(1), '标题不该再保留两行截断')
        # 中英文两种卡片都留出同样两行标题位，换成中文时不改变卡片高度。
        card_rule = re.search(r'\.github-card\s*\{([^}]*)\}', css)
        self.assertIn('min-height: 192px', card_rule.group(1), '热门卡片要定住高度，换成中文时不能变高')
        heading_rule = re.search(r'\.github-card-heading\s*\{([^}]*)\}', css)
        self.assertIsNotNone(heading_rule, '卡片标题区样式不见了')
        self.assertIn('min-height: 42px', heading_rule.group(1), '标题区要给英文和中文都留出两行位置')
        repo_rule = re.search(r'\.github-card-repo\s*\{([^}]*)\}', css)
        self.assertIsNotNone(repo_rule, '中文卡片标题下的仓库名样式不见了')
        for declaration in ('white-space: nowrap', 'overflow: hidden', 'text-overflow: ellipsis'):
            self.assertIn(declaration, repo_rule.group(1), f'仓库名没写 {declaration}，不会单行省略')

    def test_store_github_install_queues_on_team_page(self):
        source = (STATIC / 'app.js').read_text()
        self.assertNotIn('openGithubInstall', source, '热门仓库不再跳到技能页安装')
        parts = [
            line(source, 'const storeQueueItem = '),
            extract_function(source, 'async function addToStoreQueue('),
            extract_function(source, 'async function retryStoreQueue('),
            extract_function(source, 'async function queueGithubInstall('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
const sent = [], applied = [], toasts = [];
let queueVersion = 0;
let blocked = false;
const jobsBlocked = () => blocked;
const state = {queue: {items: [
  {kind: 'github_install', key: 'github:acme/example', repository: 'acme/example', status: 'failed'},
  {kind: 'team_install', key: 'github:acme/example:skills/one', status: 'failed'},
]}};
let refuse = '';
const api = async (path, body) => {
  if (refuse) throw new Error(refuse);
  sent.push([path, body]);
  return {items: []};
};
const applyQueue = (result, options) => applied.push(options);
const toast = item => toasts.push(item);
const withBusy = async (button, label, work) => work();
const detailBody = {contains: () => false};
const shelves = {querySelector: () => null};
const $ = id => id === 'detail-body' ? detailBody : shelves;
const CSS = {escape: value => value};
const focusStorePanel = () => {};
const button = {isConnected: true};

(async () => {
  await queueGithubInstall('acme/example', button);
  if (JSON.stringify(sent[0]) !== '["/api/team/queue",{"github":"acme/example"}]') fail('安装要直接加入团队安装队列：' + JSON.stringify(sent));
  if (!applied.length || !applied[0].force) fail('加入后要立即用返回的队列刷新页面');
  await retryStoreQueue('github:acme/example', button);
  if (JSON.stringify(sent[1][1]) !== '{"github":"acme/example"}') fail('热门仓库的重试要按仓库重新排队：' + JSON.stringify(sent[1]));
  await retryStoreQueue('github:acme/example:skills/one', button);
  if (JSON.stringify(sent[2][1]) !== '{"key":"github:acme/example:skills/one"}') fail('团队技能的重试照旧按编号：' + JSON.stringify(sent[2]));
  blocked = true;
  await queueGithubInstall('acme/example', button);
  if (sent.length !== 3) fail('任务停止时不该再加入队列');
  blocked = false;
  refuse = '这个仓库已不在 GitHub 最近热门里，请刷新团队页。';
  await queueGithubInstall('acme/example', button);
  if (toasts.at(-1)?.text !== refuse || toasts.at(-1)?.tone !== 'error') fail('加入失败要把原因醒目提示出来：' + JSON.stringify(toasts));
})().catch(error => { console.error(error); process.exit(1); });
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        detail = source[source.index("$('detail-body').addEventListener('click', async event => {"):]
        self.assertIn('queueGithubInstall(button.dataset.githubInstall, button)', detail[:600], '详情里的安装也直接排队')
        self.assertNotIn('closeDetail();\n    openGithub', detail[:600])

        handler = source[source.index("$('page-team').addEventListener('click'"):]
        self.assertIn("target.closest('[data-github-install]')", handler[:1600], '团队页点击要接上热门卡片的安装按钮')
        self.assertIn('queueGithubInstall(githubInstall.dataset.githubInstall, githubInstall)', handler[:1600])
        self.assertIn("target.closest('[data-github-open]')", handler[:1600], '标题按钮要打开详情')
        self.assertIn("target.closest('[data-github-card]')", handler[:1600], '卡片空白处也要能打开详情')
        self.assertIn("target.closest('[data-github-more]')", handler[:1600], '团队页点击要接上展开按钮')
        self.assertIn('state.storeGithubExpanded = !state.storeGithubExpanded', handler[:2000])
        self.assertLess(handler.index('if (githubInstall)'), handler.index('else if (githubCard'),
                        '安装按钮要先响应，不能顺带打开详情')

    def test_store_github_digest_cards_and_detail(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const number = value => '),
            line(source, 'const queueActive = '),
            line(source, 'const storeQueueItem = '),
            line(source, 'const githubQueueKey = '),
            extract_function(source, 'const STORE_STATUS = {') + ';',
            extract_function(source, 'function githubCreatedText('),
            extract_function(source, 'function githubStatusTag('),
            extract_function(source, 'function githubInstallHtml('),
            extract_function(source, 'function githubCardHtml('),
            extract_function(source, 'function githubListHtml('),
            extract_function(source, 'function githubSkillsHtml('),
            extract_function(source, 'function githubDigestStatusHtml('),
            extract_function(source, 'function githubDetailHtml('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
let readOnly = false;
const state = {table: {llm_configured: true}, queue: {items: []}};
const teamReadOnly = () => readOnly;
const now = Date.now();
const repo = (extra = {}) => ({
  repository: 'acme/example', url: 'https://github.com/acme/example',
  description: 'Example <b>skills</b>', stars: 1234,
  created_at: new Date(now - 3 * 86400000).toISOString(), pushed_at: new Date(now).toISOString(),
  skill_count: 2, local: {status: 'installable'}, digest_status: 'pending', ...extra,
});
const materials = {
  readme: '第一行\n<b>第二行</b>',
  skills: [{name: 'acme-example', description: '原始说明'},
           {name: 'acme-other', description: '<i>另一份说明</i>'}],
};
const digest = {
  title: '<b>示例工具</b>', summary: '整理 <i>公开</i> 资料。',
  uses: ['阅读资料', '归纳用途'],
  skills: [{name: 'acme-example', title: '示例整理', purpose: '汇总资料。'}],
  notes: ['需要联网'],
};

const readyCard = githubCardHtml(repo({digest_status: 'ready', digest}));
if (!readyCard.includes('&lt;b&gt;示例工具&lt;/b&gt;')) fail('中文名没有按纯文字转义：' + readyCard);
if (!readyCard.includes('<span class="github-card-repo" title="acme/example">acme/example</span>')) {
  fail('中文卡片要在标题下面用小字写 owner/name：' + readyCard);
}
if (readyCard.includes('Example &lt;b&gt;skills')) fail('有中文介绍时不该再显示英文说明：' + readyCard);
if (!readyCard.includes('&lt;i&gt;公开&lt;/i&gt;')) fail('中文一句话没有转义：' + readyCard);
const titleButton = readyCard.match(/<button[^>]*data-github-open[^>]*>[\s\S]*?<\/button>/);
if (!titleButton || titleButton[0].includes('data-github-install')) fail('标题按钮不能套安装按钮：' + readyCard);

const pendingCard = githubCardHtml(repo());
if (!pendingCard.includes('Example &lt;b&gt;skills')) fail('还没整理时保持原来的英文说明：' + pendingCard);
if (pendingCard.includes('github-card-repo')) fail('还没整理时不在标题下加仓库名：' + pendingCard);
if (pendingCard.includes('aria-busy') || pendingCard.includes('正在用 AI')) fail('等待整理时卡片不加转圈或提示：' + pendingCard);
for (const status of ['failed', 'off']) {
  const card = githubCardHtml(repo({digest_status: status, digest_error: '<img src=x> 下载失败'}));
  if (card.includes('<img') || card.includes('下载失败')) fail(`卡片上不写 ${status} 的原因：` + card);
}

const ready = githubDetailHtml({...repo({digest_status: 'ready'}), materials, digest});
for (const text of ['acme/example', '1,234 星', '3 天前新建', '含 2 个技能', '能做什么', '阅读资料',
                    '归纳用途', '仓库里的技能', '示例整理', '汇总资料。', 'acme-other', '另一份说明',
                    '用之前要知道', '需要联网', 'AI 整理，仅供参考。', '原文说明', '>安装</button>', '打开 GitHub']) {
  if (!ready.includes(text)) fail('整理结果详情缺了：' + text);
}
if (ready.includes('原始说明')) fail('有中文技能说明时不该再显示原始说明：' + ready);
if (!ready.includes('第一行\n&lt;b&gt;第二行')) fail('原文说明要保留换行并转义：' + ready);
if (ready.includes('<b>第二行') || ready.includes('<i>另一份')) fail('原文和 AI 文字都要按纯文字显示：' + ready);
if (!ready.includes('data-github-install="acme/example"')) fail('详情底部要有安装：' + ready);

const pending = githubDetailHtml({...repo({digest_status: 'pending'}), materials, digest: null});
if (!pending.includes('正在用 AI 整理中文介绍…')) fail('等待整理时要写正在整理：' + pending);
if (!pending.includes('acme-example') || !pending.includes('原始说明')) fail('没有整理结果时列原名和原始说明：' + pending);
if (pending.includes('data-github-digest-retry')) fail('等待整理时不该显示重试：' + pending);

const failed = githubDetailHtml({...repo({digest_status: 'failed', digest_error: '<img src=x> 下载失败'}), materials, digest: null});
if (!failed.includes('&lt;img src=x&gt; 下载失败')) fail('失败原因没有转义：' + failed);
if (!failed.includes('data-github-digest-retry="acme/example"')) fail('整理失败要有重试：' + failed);
readOnly = true;
const failedLocked = githubDetailHtml({...repo({digest_status: 'failed', digest_error: '下载失败'}), materials, digest: null});
if (failedLocked.includes('data-github-digest-retry')) fail('只读预览不显示整理重试：' + failedLocked);
readOnly = false;

state.table.llm_configured = false;
const off = githubDetailHtml({...repo({digest_status: 'off'}), materials, digest: null});
if (!off.includes('在设置里填好 AI 服务，就能看到中文介绍。')) fail('没填 AI 服务时要提示去设置：' + off);
if (!off.includes('data-goto="settings/ai"')) fail('没填 AI 服务时要能跳到设置：' + off);
state.table.llm_configured = true;
const legacy = githubDetailHtml({...repo({digest_status: 'off'}), materials, digest: null});
if (legacy.includes('在设置里填好 AI 服务')) fail('旧分享站等 off 情况不写提示：' + legacy);
state.table.llm_configured = false;
readOnly = true;
const offReadOnly = githubDetailHtml({...repo({digest_status: 'off'}), materials, digest: null});
if (offReadOnly.includes('在设置里填好 AI 服务')) fail('只读预览的 off 不写提示：' + offReadOnly);
readOnly = false;

const noSkills = githubDetailHtml({...repo({digest_status: 'off'}), materials: {readme: '', skills: []}, digest: null});
if (!noSkills.includes('没有读到仓库里的技能。')) fail('没有技能文件时说明当前情况：' + noSkills);
if (noSkills.includes('原文说明')) fail('原文为空时整块不显示：' + noSkills);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # 读详情的占位、失败原因和重试都要接上；所有文字按纯文字显示。
        detail = extract_function(source, 'function renderGithubDetail(')
        self.assertIn('正在读取仓库详情…', detail)
        self.assertIn('data-github-detail-retry', detail)
        self.assertIn('escape(detail.error)', detail)
        self.assertIn('button.dataset.githubDetailRetry', source)
        self.assertIn('button.dataset.githubDigestRetry', source)
        self.assertIn('/api/team/github/detail?repository=${encodeURIComponent(repository)}', source)
        self.assertIn("api('/api/team/github/retry', {repository})", source)
        html = (STATIC / 'index.html').read_text()
        self.assertIn('也会在团队页把 GitHub 最近热门整理成中文介绍，会产生少量费用', html,
                      '设置页 AI 整理的说明要补上团队页用途和费用')

    def test_store_github_digest_polling_only_updates_changed_cards(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const GITHUB_DIGEST_POLL_MS = '),
            line(source, 'const githubRepos = '),
            line(source, 'const githubRepo = '),
            line(source, 'const githubHasPending = '),
            extract_function(source, 'function githubApplyEntry('),
            extract_function(source, 'function applyGithubDigests('),
            extract_function(source, 'function scheduleGithubDigestPoll('),
            extract_function(source, 'function stopGithubDigestPolling('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
let githubDigestTimer = null;
let githubDigestSequence = 0;
let githubDetail = null;
const timers = [];
const cleared = [];
const setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
const clearTimeout = id => cleared.push(id);
const readGithubDigests = () => {};
const updated = [];
const updateGithubCard = repository => updated.push(repository);
const loadGithubDetail = repository => updated.push('detail:' + repository);
const state = {page: 'team', store: {github: {repos: [
  {repository: 'acme/a', digest_status: 'pending'},
  {repository: 'acme/b', digest_status: 'ready', digest: {title: '乙', summary: '旧'}},
]}}};

if (GITHUB_DIGEST_POLL_MS !== 3000) fail('轮询间隔要是 3 秒：' + GITHUB_DIGEST_POLL_MS);
scheduleGithubDigestPoll();
if (timers.length !== 1 || timers[0][1] !== 3000) fail('有等待中的仓库时要排一次 3 秒后的轮询：' + JSON.stringify(timers));
scheduleGithubDigestPoll();
if (timers.length !== 1) fail('已经排了轮询就不要重复排');
stopGithubDigestPolling();
if (!cleared.length || githubDigestTimer !== null) fail('离开团队页要停掉轮询');
timers.length = 0;

state.store.github.repos[0].digest_status = 'ready';
scheduleGithubDigestPoll();
if (timers.length !== 0) fail('没有等待中的仓库时不该继续轮询');

state.store.github.repos[0].digest_status = 'pending';
if (!githubHasPending()) fail('应该能看出还有等待中的仓库');
applyGithubDigests({repos: {'acme/a': {digest_status: 'ready', digest: {title: '甲', summary: '新'}}}});
if (state.store.github.repos[0].digest_status !== 'ready') fail('轻量状态没有合并进目录：' + JSON.stringify(state.store));
if (!updated.includes('acme/a')) fail('变了的卡片要单独更新：' + JSON.stringify(updated));
applyGithubDigests({repos: {'acme/b': {digest_status: 'failed', digest_error: '下载失败'}}});
if (!updated.includes('acme/b')) fail('第二张变了的卡片也要更新：' + JSON.stringify(updated));
if (state.store.github.repos[1].digest_status !== 'failed' || state.store.github.repos[1].digest_error !== '下载失败') {
  fail('失败状态没有合并：' + JSON.stringify(state.store.github.repos[1]));
}
if ('digest' in state.store.github.repos[1]) fail('失败后要清掉旧的中文内容');

githubDetail = {repository: 'acme/b', data: null};
applyGithubDigests({repos: {'acme/b': {digest_status: 'ready', digest: {title: '乙', summary: '新'}}}});
if (!updated.includes('detail:acme/b')) fail('打开着的详情要跟着重新读：' + JSON.stringify(updated));
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # 轮询只读轻量接口，不重读目录；没有等待中的条目或离开团队页就停。
        read = extract_function(source, 'async function readGithubDigests(')
        self.assertIn("api('/api/team/github/digests')", read)
        self.assertIn('state.page !== \'team\'', read)
        self.assertIn('!githubHasPending()', read)
        self.assertNotIn('/api/team/catalog', read)
        self.assertIn('if (page !== \'team\') stopGithubDigestPolling()', source[
            source.index('function showPage('):source.index('function gotoPlace(')])
        load_store = source[source.index('async function loadStore('):source.index('/* 自己能上架的技能')]
        self.assertIn('scheduleGithubDigestPoll()', load_store)
        self.assertIn('stopGithubDigestPolling()', load_store)

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
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const number = value => '),
            line(source, 'const storeTitle = '),
            line(source, 'const storeQueueItem = '),
            extract_function(source, 'const STORE_STATUS = {') + ';',
            line(source, 'const starIcon = '),
            extract_function(source, 'function storeRatingHtml('),
            extract_function(source, 'function storeInstallsHtml('),
            extract_function(source, 'function storeStatusTag('),
            extract_function(source, 'function storeNeedsHtml('),
            extract_function(source, 'function storeSummary('),
            extract_function(source, 'function storeRecommenders('),
            extract_function(source, 'function storeCardHtml('),
            line(source, 'const storeTime = '),
            line(source, 'const storeScore = '),
            extract_function(source, 'const STORE_SORTS = {') + ';',
            block(source, 'function storeFilter('),
            extract_function(source, 'function storeGroups('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
const state = {queue: null};
const member = name => ({id: name, name});
const cards = [
  {key: 'a', name: 'acme-a', title: '<b>示例</b>', summary: '周报', group: '写作', updated_at: '2026-10-01T00:00:00Z',
   listings: [{member: member('Acme 甲'), note: '<img src=x onerror=alert(1)> 好用'}], rating: {average: 4.5, count: 2}, local: {status: 'installable'}},
  {key: 'b', name: 'acme-b', summary: '小工具', description: 'internal <script>', updated_at: '2026-10-03T00:00:00Z',
   listings: [{member: member('Acme 乙')}, {member: member('Acme 丙')}, {member: member('Acme 丁')}], rating: {average: null, count: 0}, local: {status: 'mine'}},
  {key: 'c', name: 'acme-c', title: '整理', group: '写作', updated_at: '2026-10-02T00:00:00Z',
   listings: [{member: member('Acme 乙')}, {member: member('Acme 甲')}], rating: {average: 4.9, count: 1}, local: {status: 'installed'}},
];

const html = storeCardHtml(cards[0]);
if (html.includes('<b>示例') || !html.includes('&lt;b&gt;示例')) fail('同事给的中文名没有转成纯文字：' + html);
if (html.includes('<img') || !html.includes('&lt;img')) fail('推荐语没有转成纯文字：' + html);
if (!html.includes('data-store-open="a"')) fail('卡片要能打开详情');
if (!storeCardHtml(cards[1]).includes('Acme 乙 等 3 人推荐')) fail('多人推荐要写成“某某等 N 人推荐”');
if (storeSummary({description: '把周报整理好。再发出去'}) !== '把周报整理好') fail('没有一句话时应取说明的第一句');

const names = list => list.map(card => card.name).join(',');
if (names(storeFilter(cards, {group: '写作'})) !== 'acme-a,acme-c') fail('按分组筛选不对');
if (names(storeFilter(cards, {group: '-'})) !== 'acme-b') fail('“没分组”应只留下没有分组的技能');
if (names(storeFilter(cards, {query: 'INTERNAL'})) !== 'acme-b') fail('搜索应能搜到说明，且不分大小写');
if (names(storeFilter(cards, {query: '好用'})) !== 'acme-a') fail('搜索应能搜到推荐语');
if (names(storeFilter(cards, {query: 'acme 丁'})) !== 'acme-b') fail('搜索应能搜到推荐人');
if (names(storeFilter(cards, {mine: true})) !== 'acme-b') fail('“我上架的”只留自己上架的');

const sorted = sort => names([...cards].sort(STORE_SORTS[sort]));
if (sorted('latest') !== 'acme-b,acme-c,acme-a') fail('最新上架排序不对：' + sorted('latest'));
if (sorted('rating') !== 'acme-c,acme-a,acme-b') fail('最受好评排序不对，没评价的应排最后：' + sorted('rating'));
if (sorted('popular') !== 'acme-b,acme-c,acme-a') fail('推荐最多排序不对：' + sorted('popular'));
if (JSON.stringify(storeGroups(cards)) !== JSON.stringify([['写作', 2], ['-', 1]])) fail('分组计数不对，没分组的排最后');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_install_counts_only_show_above_zero(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const number = value => '),
            line(source, 'const starIcon = '),
            line(source, 'const storeTitle = '),
            line(source, 'const storeQueueItem = '),
            line(source, 'const storePack = '),
            extract_function(source, 'function storePackOf('),
            extract_function(source, 'const STORE_STATUS = {') + ';',
            extract_function(source, 'function storeRatingHtml('),
            extract_function(source, 'function storeInstallsHtml('),
            extract_function(source, 'function storeSummary('),
            extract_function(source, 'function storeRecommenders('),
            extract_function(source, 'function storeFactsHtml('),
            extract_function(source, 'function storeStatusTag('),
            extract_function(source, 'function storeNeedsHtml('),
            extract_function(source, 'function storeCardHtml('),
            extract_function(source, 'function storeRowHtml('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
const state = {queue: {items: []}, storeMine: false};
const teamCanWrite = () => true;
function card(installs) {
  const item = {key: 'a', name: 'acme-a', title: '示例', summary: '周报', group: '',
    listings: [{member: {id: 'm1', name: 'Acme 甲'}, note: '好用'}],
    rating: {average: 4.5, count: 3}, local: {status: 'installable'}};
  if (installs !== undefined) item.installs = installs;
  return item;
}
const html = card => ({卡片: storeCardHtml(card), 列表: storeRowHtml(card), 详情: storeFactsHtml(card)});

for (const installs of [0, undefined]) {
  const views = html(card(installs));
  for (const [where, text] of Object.entries(views)) {
    if (text.includes('人已安装')) fail(`${where}不该显示 0 或没有字段的人数：` + text);
    if (text.includes('store-installs')) fail(`${where}不该留下人数空位：` + text);
  }
}

const views = html(card(2));
for (const [where, text] of Object.entries(views)) {
  if (!text.includes('<span class="store-installs">2 人已安装</span>')) fail(`${where}没有显示“2 人已安装”：` + text);
  if (text.indexOf('2 人已安装') < text.indexOf('4.5')) fail(`${where}的人数应排在评分后面：` + text);
}
if (!storeCardHtml(card(1200)).includes('1,200 人已安装')) fail('人数要沿用现有的数字格式');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        detail = extract_function(source, 'function renderStoreDetail(')
        self.assertIn('${storeFactsHtml(card)}', detail)

    def test_store_row_rating_aligns_and_shares_separator(self):
        css = (STATIC / 'app.css').read_text()

        rating_rule = re.search(r'\.store-row-rating\s*\{([^}]*)\}', css)
        self.assertIsNotNone(rating_rule, '列表里的评分栏样式不见了')
        self.assertIn('display: flex', rating_rule.group(1), '评分和人数的容器应横向排列')
        self.assertIn('align-items: center', rating_rule.group(1), '评分和人数应垂直居中')
        self.assertIn('gap: var(--sp-2)', rating_rule.group(1), '评分和人数之间应留统一间距')

        installs_rule = re.search(r'\.store-row-rating \.store-installs\s*\{([^}]*)\}', css)
        self.assertIsNotNone(installs_rule, '人数那一段的收缩样式不见了')
        self.assertIn('min-width: 0', installs_rule.group(1), '人数应能收缩，不挤到推荐人那一栏')
        self.assertIn('text-overflow: ellipsis', installs_rule.group(1), '人数太长时应收成省略号')

        # 卡片底部、详情和列表评分栏共用同一条分隔点规则，不再各写一套。
        separator_rule = re.search(r'([^{}]+)\{\s*content:\s*"·"[^}]*\}', css)
        self.assertIsNotNone(separator_rule, '人数前的分隔样式不见了')
        self.assertEqual(css.count('content: "·"'), 1, '分隔点只保留一套规则')
        for selector in ('.store-card-foot > span + span::before',
                         '.store-facts > span + span::before',
                         '.store-row-rating > span + span::before'):
            self.assertIn(selector, separator_rule.group(1), f'分隔点规则漏了：{selector}')
        self.assertNotIn('.store-row-rating .store-installs::before', css, '不要再用单独两边留白的分隔写法')

        # 「人已安装」比原来多一个字，评分栏一起加宽，四种排法共用同一个列宽。
        self.assertIn('--store-rating-col: 184px', css)
        self.assertEqual(css.count('var(--store-rating-col)'), 4, '普通列表、「我的上架」和窄屏两种排法都要用新列宽')

    def test_store_queue_rows_escape_and_tag_waiting(self):
        source = (STATIC / 'app.js').read_text()
        # 旧的“先核对、再确认装上”已经去掉，页面只走安装队列。
        for old in ('/api/team/install/preview', '/api/team/install/apply',
                    'data-store-agent', 'data-store-install-apply', 'data-store-plan-cancel'):
            self.assertNotIn(old, source)
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const storeQueueItem = '),
            extract_function(source, 'const STORE_STATUS = {') + ';',
            extract_function(source, 'function storeStatusTag('),
            extract_function(source, 'function githubQueueNote('),
            extract_function(source, 'function githubQueueAction('),
            extract_function(source, 'function storeQueueRowHtml('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
const github = extra => ({kind: 'github_install', key: 'github:acme/example', repository: 'acme/example',
  title: '示例仓库', status: 'done', message: '已安装 2 个技能。', installed: [], reuse: [], skipped: [], ...extra});
const many = storeQueueRowHtml(github({installed: ['acme-a', 'acme-b'], skipped: ['acme-c'], tag: '示例仓库',
  message: '已安装 2 个技能。跳过 1 个同名但内容不同的技能：acme-c。可到技能页粘贴仓库链接核对。'}));
if (!many.includes('安装完成') || !many.includes('装上 2 个，1 个同名没装')) fail('仓库装完要写装上和没装的个数：' + many);
if (!many.includes('title="已安装 2 个技能。跳过 1 个')) fail('完整说明要放在悬停提示里：' + many);
if (!many.includes('data-store-goto-tag="示例仓库"') || !many.includes('去启用这 2 个')) fail('装上多个时按标签去启用：' + many);
if (many.includes('前置依赖')) fail('仓库安装不该写成前置依赖：' + many);
const one = storeQueueRowHtml(github({installed: ['acme-a']}));
if (!one.includes('data-store-goto="acme-a"') || !one.includes('前往分配')) fail('装上一个时直接打开它：' + one);
const reused = storeQueueRowHtml(github({reuse: ['acme-a'], message: '本机已经有了。'}));
if (!reused.includes('1 个本机已有') || !reused.includes('data-store-goto="acme-a"')) fail('本机已有的一个也能直接打开：' + reused);
const untagged = storeQueueRowHtml(github({installed: ['acme-a', 'acme-b']}));
if (untagged.includes('data-store-goto')) fail('没加上标签时不给筛不出来的入口：' + untagged);
const broken = storeQueueRowHtml(github({status: 'failed', message: '下载失败'}));
if (!broken.includes('安装失败：下载失败') || !broken.includes('data-store-queue-retry="github:acme/example"')) fail('仓库装失败要能重试：' + broken);
const state = {queue: {items: [
  {key: 'a', name: 'acme-a', title: '<b>示例</b>', status: 'waiting'},
  {key: 'b', name: 'acme-b', status: 'failed', message: '<img src=x onerror=alert(1)> 连不上分享站'},
]}};
const waiting = storeQueueRowHtml(state.queue.items[0]);
if (waiting.includes('<b>') || !waiting.includes('&lt;b&gt;示例')) fail('队列里的中文名没有转成纯文字：' + waiting);
if (!waiting.includes('排队中')) fail('排队中的技能应写“排队中”');
const failed = storeQueueRowHtml(state.queue.items[1]);
if (failed.includes('<img') || !failed.includes('&lt;img')) fail('队列里的失败原因没有转成纯文字：' + failed);
if (!failed.includes('安装失败：') || !failed.includes('data-store-queue-retry="b"')) fail('失败的行要写原因并能重试：' + failed);
const tag = storeStatusTag({key: 'a', local: {status: 'installable'}});
if (!tag.includes('排队中') || tag.includes('可安装')) fail('排队中的卡片标签应盖过“可安装”：' + tag);
state.queue = {items: [{key: 'a', status: 'running'}]};
if (!storeStatusTag({key: 'a', local: {status: 'installable'}}).includes('正在安装')) fail('正在安装时应写“正在安装”');
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_review_and_source_describe_github_files(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const number = value => '),
            line(source, 'const sizeText = '),
            extract_function(source, 'function storeSourceText('),
            line(source, 'const reviewPairKey = '),
            line(source, 'const reviewTitle = '),
            extract_function(source, 'function storeDetectedHtml('),
            extract_function(source, 'function storeReviewPacksHtml('),
            extract_function(source, 'function storeReviewHtml('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
const storePick = {rejected: new Set(), packs: []};
const html = storeReviewHtml({listings: [
  {kind: 'github', name: 'gh-files', title: '网上带文件', version: 'v1',
   files: {count: 3, size: 2048}, note: ''},
  {kind: 'github', name: 'gh-note', title: '网上不带文件', version: null,
   download_note: '同事装的时候从 GitHub 下载。', note: ''},
  {kind: 'files', name: 'mine', title: '自制的', files: {count: 2, size: 1024,
   blocked: [{path: '.env', reason: '像密钥'}, {path: 'key.txt', reason: '像密码'}]}, note: ''},
  {kind: 'github', name: 'gh-blocked', title: '网上被拦', version: null,
   download_note: '同事装的时候从 GitHub 下载。', files: {blocked: [{path: 'secret.txt', reason: '像密钥'}]}, note: ''},
]});
for (const text of [
  '公开技能，包含技能文件：共 3 个文件 (2 KB)；同事安装时从分享站下载',
  '公开技能，仅上传名称与来源。同事装的时候从 GitHub 下载。',
  '包含技能文件：共 2 个文件 (1 KB)',
  '已拦截 2 个疑似敏感凭据文件（不予上传）',
  '已拦截 1 个疑似敏感凭据文件（此技能仅上传名称与来源）',
]) {
  if (!html.includes(text)) fail('上架核对缺了：' + text);
}
const shared = storeSourceText({kind: 'github', listings: [{file_count: 3, size: 2048}]});
if (shared !== '公开技能，文件已随上架保存至分享站，安装时从分享站下载。') fail('来源说明不对：' + shared);
const online = storeSourceText({kind: 'github', listings: [{}]});
if (online !== '公开技能，安装时从原作者的来源下载。') fail('没有文件时来源说明不对：' + online);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


    def test_store_pack_card_row_and_needs(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const number = value => '),
            extract_function(source, 'const tokenText = '),
            line(source, 'const storeTitle = '),
            line(source, 'const storeQueueItem = '),
            line(source, 'const storePack = '),
            extract_function(source, 'function storePackOf('),
            line(source, 'const storePackTime = '),
            extract_function(source, 'function storePackTitles('),
            extract_function(source, 'function storePackSummary('),
            extract_function(source, 'function storePackStatus('),
            extract_function(source, 'function storePackTag('),
            extract_function(source, 'function storeNeedsHtml('),
            extract_function(source, 'const STORE_STATUS = {') + ';',
            line(source, 'const starIcon = '),
            extract_function(source, 'function storeRatingHtml('),
            extract_function(source, 'function storeInstallsHtml('),
            extract_function(source, 'function storeSummary('),
            extract_function(source, 'function storeRecommenders('),
            extract_function(source, 'function storeStatusTag('),
            extract_function(source, 'function storeCardHtml('),
            extract_function(source, 'function storePackCardHtml('),
            extract_function(source, 'function storeRowHtml('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
const teamCanWrite = () => true;
const member = name => ({id: name, name});
const author = member('Acme 甲');
const cards = [
  {key: 'a', name: 'acme-a', title: '甲技能', packs: ['pack:p:1'],
   listings: [{member: author, note: '好用'}], rating: {average: null, count: 0}, local: {status: 'installable'},
   needs: [{name: 'dep', title: '<b>依赖</b>', status: 'with'}, {name: 'have', title: '已有依赖', status: 'have'}]},
  {key: 'b', name: 'acme-b', title: '乙技能', packs: ['pack:p:1'], listings: [{member: author}], rating: {average: null, count: 0}, local: {status: 'installable'}},
  {key: 'c', name: 'acme-c', title: '丙技能', packs: ['pack:p:1'], listings: [{member: author}], rating: {average: null, count: 0}, local: {status: 'installable'}},
  {key: 'd', name: 'acme-d', title: '丁技能', packs: ['pack:p:1'], listings: [{member: author}], rating: {average: null, count: 0}, local: {status: 'installable'}},
];
const pack = {key: 'pack:p:1', id: 'p', title: '<b>写作包</b>', note: '<img src=x onerror=alert(1)> 好用', member: author,
  cards: ['a', 'b', 'c', 'd'], local: {total: 4, installed: 1, installable: 3, blocked: 0}, tokens: 1200};
const state = {queue: {items: []}, storeMine: false, store: {member: author, cards, packs: [pack], packs_supported: true}};

const needs = storeNeedsHtml(cards[0]);
if (!needs.includes('需要 1 个')) fail('依赖标记只应数没有的：' + needs);
if (!needs.includes('&lt;b&gt;依赖')) fail('依赖名字没有转成纯文字：' + needs);
if (needs.includes('已有依赖')) fail('已有的依赖不该出现在标记里：' + needs);
if (!needs.includes('安装时一起装上：&lt;b&gt;依赖')) fail('读屏文字应写安装时一起装上：' + needs);

const html = storePackCardHtml(pack);
if (html.includes('<b>写作包') || !html.includes('&lt;b&gt;写作包')) fail('包名没有转成纯文字：' + html);
if (html.includes('<img') || !html.includes('&lt;img')) fail('推荐语没有转成纯文字：' + html);
if (!html.includes('甲技能 · 乙技能 · 丙技能…')) fail('包卡片应写前三个技能并加省略号：' + html);
if (!html.includes('已装 1/4')) fail('装了一部分应写已装 x/N：' + html);
if (!html.includes('每个 Agent 约 +1.2k Token')) fail('包卡片应写还没装的部分的 Token：' + html);
if (!html.includes('data-store-open="pack:p:1"')) fail('包卡片要能打开包详情');
if (html.includes('store-pack-layer is-far') === false) fail('包卡片后面应有两层错开的边');

const mine = {...pack, mine: true, tokens: 32};
if (!storePackCardHtml(mine).includes('我的上架')) fail('自己上架的包应写“我的上架”');
if (storePackCardHtml(mine).includes('每个 Agent 约')) fail('没有要装的就不写 Token');
const installed = {...pack, local: {total: 4, installed: 4, installable: 0, blocked: 0}};
if (!storePackCardHtml(installed).includes('已安装')) fail('全装了应写“已安装”');
const fresh = {...pack, local: {total: 4, installed: 0, installable: 4, blocked: 0}};
if (!storePackCardHtml(fresh).includes('4 个技能')) fail('还没装应写“N 个技能”');

const row = storeRowHtml(cards[1]);
if (!row.includes('属于「&lt;b&gt;写作包&lt;/b&gt;」')) fail('平铺的技能行应写属于哪个包：' + row);
const child = storeRowHtml(cards[1], {child: true, hidePack: true});
if (!child.includes('is-pack-child') || child.includes('属于「')) fail('展开的包成员应缩进，且不重复写属于哪个包：' + child);
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_pack_queue_groups(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const queueActive = '),
            extract_function(source, 'function storeQueueRowHtml('),
            extract_function(source, 'function storeQueueGroups('),
            extract_function(source, 'function storeQueuePackStatus('),
            extract_function(source, 'function storeQueuePackHtml('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
let queueExpanded = new Set();
const items = [
  {key: 'x', name: 'acme-x', status: 'waiting'},
  {key: 'a', name: 'acme-a', title: '甲', status: 'waiting', pack: 'pack:p:1', pack_title: '<b>写作包</b>'},
  {key: 'b', name: 'acme-b', title: '乙', status: 'running', pack: 'pack:p:1', pack_title: '<b>写作包</b>'},
  {key: 'y', name: 'acme-y', status: 'done'},
];
const groups = storeQueueGroups(items);
if (groups.length !== 3 || groups[0].items[0].key !== 'x' || groups[2].items[0].key !== 'y') fail('没包的条目应保持原来的位置');
if (groups[1].items.length !== 2) fail('同一个包的条目应合成一组');
const waiting = storeQueuePackHtml(groups[1]);
if (!waiting.includes('正在安装 0/2')) fail('有人开始装时应写正在安装 x/N：' + waiting);
if (!waiting.includes('&lt;b&gt;写作包') || waiting.includes('<b>写作包')) fail('包名没有转成纯文字：' + waiting);
if (!waiting.includes('data-store-queue-pack="pack:p:1"') || !waiting.includes('aria-expanded="false"')) fail('包组头要有展开按钮：' + waiting);
if (!waiting.includes('href="#sp-stack"')) fail('包组头要用叠起来的图标：' + waiting);
group = {pack: 'pack:p:1', pack_title: '写作包', items: [{key: 'a', status: 'done'}, {key: 'b', status: 'done'}]};
const done = storeQueuePackHtml(group);
if (!done.includes('安装完成 2/2') || !done.includes('去启用这 2 个')) fail('全完成应写安装完成 N/N 和去启用：' + done);
const failed = {pack: 'pack:p:1', pack_title: '写作包', items: [{key: 'a', status: 'failed', message: '<img>'}]};
if (!storeQueuePackHtml(failed).includes('1 个没装上')) fail('有失败应写几个没装上');
queueExpanded.add('pack:p:1');
const open = storeQueuePackHtml(groups[1]);
if (!open.includes('aria-expanded="true"') || !open.includes('排队中')) fail('展开后应能看到每个技能的原有行：' + open);
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_review_detected_and_packs_escape(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const reviewPairKey = '),
            line(source, 'const reviewTitle = '),
            extract_function(source, 'function storeDetectedHtml('),
            extract_function(source, 'function storeReviewPacksHtml('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
const storeCandidates = [{name: 'acme-a', title: '<b>甲</b>'}, {name: 'acme-b', title: '乙'}];
const storePick = {rejected: new Set(), packs: [
  {id: 'p', title: '<b>包</b>', note: '<img src=x>', skills: ['acme-a', 'acme-b'], why: '名字开头相同', enabled: true, error: ''},
]};
const detected = storeDetectedHtml({detected: [
  {skill: 'acme-a', requires: 'acme-b', evidence: '<img src=x onerror=alert(1)>', adopted: true},
  {skill: 'acme-b', requires: 'acme-a', evidence: '不要用', adopted: false},
]});
if (detected.includes('<img') || !detected.includes('&lt;img')) fail('证据原文没有转成纯文字：' + detected);
if (detected.includes('<b>甲') || !detected.includes('&lt;b&gt;甲')) fail('技能中文名没有转成纯文字：' + detected);
if (!detected.includes('甲') || !detected.includes('需要')) fail('识别结果应写“A 需要 B”');
if (!detected.includes('type="checkbox"') || !detected.includes('checked')) fail('识别结果默认应勾上');
if ((detected.match(/checked/g) || []).length !== 1) fail('已取消的识别结果不该勾上');
if (!detected.includes('没识别出来的，可以在技能页的技能详情里添加依赖项后再上架。')) fail('识别块尾缺说明');

const packs = storeReviewPacksHtml({packs_supported: true});
if (packs.includes('<b>包') || !packs.includes('&lt;b&gt;包')) fail('包名没有转成纯文字：' + packs);
if (packs.includes('<img') || !packs.includes('&lt;img')) fail('推荐语没有转成纯文字：' + packs);
if (!packs.includes('名字开头相同') || !packs.includes('data-review-pack-title') || !packs.includes('data-review-pack-remove')) fail('包小卡缺名字、原因或可去掉的技能：' + packs);
if (storeReviewPacksHtml({packs_supported: false}) !== '') fail('分享站不支持时不该显示技能包块');
storePick.packs = [];
if (storeReviewPacksHtml({packs_supported: true}) !== '') fail('没有包时不该显示技能包块');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_pack_field_validation(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const packFieldBad = '),
            extract_function(source, 'function packFieldError('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
if (packFieldError({title: '', note: ''}) !== '请填写包名。') fail('空包名应提示');
if (packFieldError({title: '一'.repeat(21), note: ''}) !== '包名最多 20 个字。') fail('超长包名应提示');
if (packFieldError({title: '包', note: '一'.repeat(201)}) !== '推荐语最多 200 个字。') fail('超长推荐语应提示');
if (!packFieldError({title: '包\n名', note: ''}).includes('看不见的字符')) fail('换行包名应提示');
if (!packFieldError({title: '包', note: '推荐\n语'}).includes('看不见的字符')) fail('换行推荐语应提示');
if (packFieldError({title: '写作包', note: '一起用'}) !== '') fail('正常的包名和推荐语不该报错');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_pack_locks_direct_and_indirect_needs(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const storeQueueItem = '),
            line(source, 'const storeCard = '),
            extract_function(source, 'function storePackCards('),
            extract_function(source, 'function storePackNeeds('),
            extract_function(source, 'function storePackSelection('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
const cards = [
  {key: 'a', name: 'acme-a', requires: ['acme-b'], local: {status: 'installable'}},
  {key: 'b', name: 'acme-b', requires: ['acme-c'], local: {status: 'installable'}},
  {key: 'c', name: 'acme-c', requires: [], local: {status: 'installable'}},
  {key: 'd', name: 'acme-d', requires: [], local: {status: 'installed'}},
];
const state = {queue: {items: []}, store: {cards}};
let storeGet = {packSelected: new Set(['a'])};
const pack = {cards: ['a', 'b', 'c', 'd']};
let result = storePackSelection(pack);
if (!['a', 'b', 'c'].every(key => result.selected.has(key))) fail('勾选的技能应自动带上直接和间接依赖');
if (result.locked.get('acme-b') !== 1 || result.locked.get('acme-c') !== 1) fail('直接和间接依赖都应锁住');
if (result.selected.has('d')) fail('本机已有的技能不该勾上');
if (!storeGet.packSelected.has('b') || !storeGet.packSelected.has('c')) fail('锁住的依赖要记进当前勾选');
storeGet = {packSelected: new Set()};
result = storePackSelection(pack);
if (result.selected.size || result.locked.size) fail('全部取消后锁应自动解开');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_pack_toggle_arrows_point_right_then_down(self):
        css = (STATIC / 'app.css').read_text()
        closed = re.search(r'\.store-pack-toggle \.ico\s*\{([^}]*)\}', css)
        self.assertIsNotNone(closed, '包组头的箭头样式不见了')
        self.assertIn('rotate(180deg)', closed.group(1), '收起时箭头应朝右')
        open_rule = re.search(r'\.store-pack-toggle\.is-open \.ico\s*\{([^}]*)\}', css)
        self.assertIsNotNone(open_rule, '包组头展开时的箭头样式不见了')
        self.assertIn('rotate(270deg)', open_rule.group(1), '展开时箭头应朝下')

    def test_store_pack_batch_toast_text_and_readiness(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const packBatchToast = '),
            extract_function(source, 'function packBatchReady('),
            line(source, 'const JOB_ACTIVE = '),
            line(source, 'const jobActive = '),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
if (packBatchToast('写作包', 3) !== '已从团队商店装上「写作包」里的 3 个技能') fail('整包完成提示文字不对：' + packBatchToast('写作包', 3));
const batch = {ids: new Set(['a', 'b'])};
if (packBatchReady(batch, [{id: 'a', status: 'done'}, {id: 'b', status: 'running'}])) fail('还有条目在装时不该整批提示');
if (!packBatchReady(batch, [{id: 'a', status: 'done'}, {id: 'b', status: 'done'}])) fail('整批结束后应提示');
if (!packBatchReady(batch, [{id: 'x', status: 'running'}])) fail('别的包的条目不该拖住这一批');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finish = extract_function(source, 'function finishJobs(')
        self.assertIn("const isPack = job.kind === 'team_install'", finish)
        self.assertIn('batch.installed.add(name)', finish)
        self.assertIn('announcePackBatch(key, state.jobs)', finish)

    def test_store_pack_batch_merges_success_toasts(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const JOB_ACTIVE = '),
            line(source, 'const jobActive = '),
            line(source, 'const packBatches = '),
            line(source, 'const packBatchToast = '),
            extract_function(source, 'function packBatchReady('),
            extract_function(source, 'function announcePackBatch('),
            extract_function(source, 'function finishJobs('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
const toasts = [];
const toast = item => toasts.push(item);
const pastTense = text => text;
const jobStatusLine = job => '安装失败：' + (job.message || '原因未知');
const jobUndoAvailable = () => false;
const undoJob = async () => true;
const refreshTable = async () => {};
const refreshLog = async () => {};
const loadCleanup = async () => {};
const refreshUpdates = async () => {};
const startLabels = () => {};
const jobsHasActive = () => false;
const renderJobs = () => {};
const invalidatePreview = () => {};
const schedulePreview = () => {};
let assignmentSubmitting = false;
const state = {jobs: [], assignmentIntents: new Map()};

const first = {id: 'a', kind: 'team_install', pack: 'pack:p:1', pack_title: '写作包', status: 'done',
  name: 'acme-a', installed: ['acme-a', 'acme-shared'], operations: ['op-1']};
const second = {id: 'b', kind: 'team_install', pack: 'pack:p:1', pack_title: '写作包', status: 'done',
  name: 'acme-b', installed: ['acme-b', 'acme-shared'], operations: ['op-2']};
state.jobs = [first, second];
packBatches.set('pack:p:1', {ids: new Set(['a', 'b']), title: '写作包', installed: new Set(), announced: false});
finishJobs([first, second]);
setTimeout(() => {
  if (toasts.length !== 1) fail('整包完成只应弹一条提示：' + JSON.stringify(toasts));
  if (toasts[0].text !== '已从团队商店装上「写作包」里的 3 个技能') fail('整包完成提示不对：' + toasts[0].text);
  if (toasts[0].undo) fail('整包完成提示不该只撤销其中一个技能');

  toasts.length = 0;
  const third = {id: 'c', kind: 'team_install', pack: 'pack:p:2', pack_title: '第二个包', status: 'done',
    name: 'acme-c', installed: ['acme-c'], operations: []};
  const failed = {id: 'd', kind: 'team_install', pack: 'pack:p:2', pack_title: '第二个包', status: 'failed',
    name: 'acme-d', message: '连不上分享站'};
  state.jobs = [third, failed];
  packBatches.set('pack:p:2', {ids: new Set(['c', 'd']), title: '第二个包', installed: new Set(), announced: false});
  finishJobs([third, failed]);
  if (toasts.length !== 2) fail('有失败时应整包成功提示加逐条失败提示：' + JSON.stringify(toasts));
  if (!toasts.some(item => item.text === '已从团队商店装上「第二个包」里的 1 个技能')) fail('有失败时也应提示成功的数量');
  if (!toasts.some(item => item.tone === 'error' && item.text.includes('连不上分享站'))) fail('失败原因应逐条提示');

  toasts.length = 0;
  finishJobs([{id: 'x', kind: 'install', status: 'done', message: '装上了「单技能」', operations: []}]);
  if (toasts.length !== 1 || toasts[0].text !== '装上了「单技能」') fail('不带包的安装应保持原来的成功提示：' + JSON.stringify(toasts));
}, 10);
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_pack_detail_partial_install_shows_enable(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const number = value => '),
            extract_function(source, 'const tokenText = '),
            line(source, 'const storeTitle = '),
            line(source, 'const storeQueueItem = '),
            line(source, 'const queueActive = '),
            line(source, 'const storeCard = '),
            extract_function(source, 'function storePackCards('),
            extract_function(source, 'function storePackNeeds('),
            extract_function(source, 'function storePackSelection('),
            extract_function(source, 'function storeSummary('),
            extract_function(source, 'function packSkillStateHtml('),
            extract_function(source, 'function packSkillRowHtml('),
            extract_function(source, 'function storePackGoHtml('),
            extract_function(source, 'function storePackGetHtml('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
const teamReadOnly = () => false;
const jobsBlocked = () => false;
const card = (key, name, status) => ({key, name, title: name, requires: [], local: {status}, tokens: 8});
const cards = [
  card('a', 'acme-a', 'installed'),
  card('b', 'acme-b', 'update'),
  card('c', 'acme-c', 'installed'),
  card('d', 'acme-d', 'installable'),
];
const state = {queue: {items: []}, store: {cards}};
let storeGet = {packSelected: new Set(['d'])};
const pack = {key: 'pack:p:1', title: '写作包', mine: false, cards: ['a', 'b', 'c', 'd'],
  local: {total: 4, installed: 3, installable: 1, blocked: 0}};
const partial = storePackGetHtml(pack);
if (!partial.includes('去启用已装的 3 个')) fail('只装了一部分时应能去启用已装的技能：' + partial);
if (!partial.includes('安装 1 个技能')) fail('还有可装技能时主按钮仍是安装 N 个：' + partial);
storeGet = {packSelected: new Set()};
const fullPack = {...pack, local: {total: 4, installed: 4, installable: 0, blocked: 0},
  cards: ['a', 'b', 'c', 'd']};
const full = storePackGetHtml({...fullPack, cards: ['a', 'b', 'c', 'd']});
if (!full.includes('去启用这 4 个')) fail('全部已装时主按钮应保持不变：' + full);
if (full.includes('安装 4 个技能')) fail('全部已装时不该再出现安装按钮：' + full);
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_store_pack_detail_lists_skills_and_uses(self):
        source = (STATIC / 'app.js').read_text()
        parts = [
            line(source, 'const escape = value => '),
            line(source, 'const icon = (name'),
            line(source, 'const number = value => '),
            extract_function(source, 'const tokenText = '),
            line(source, 'const storeTitle = '),
            line(source, 'const queueActive = '),
            line(source, 'const storeQueueItem = '),
            line(source, 'const storeCard = '),
            extract_function(source, 'function storePackCards('),
            extract_function(source, 'function storePackNeeds('),
            extract_function(source, 'function storePackSelection('),
            extract_function(source, 'function storeSummary('),
            extract_function(source, 'function packSkillStateHtml('),
            extract_function(source, 'function packSkillRowHtml('),
            extract_function(source, 'function packCanSelect('),
            extract_function(source, 'function storePackIntroHtml('),
            extract_function(source, 'function storePackGoHtml('),
            extract_function(source, 'function storePackGetHtml('),
        ]
        script = '\n'.join(parts) + r"""
function fail(message) { console.error(message); process.exit(1); }
const teamReadOnly = () => false;
const teamCanWrite = () => true;
const jobsBlocked = () => false;
const author = {id: 'acme-owner', name: 'Acme 甲'};
function card(key, name, status, extra = {}) {
  return {key, name, title: name, summary: `用途-${name}`, requires: [], local: {status},
    listings: [{member: author}], packs: ['pack:p:1'], tokens: 8, ...extra};
}
const state = {queue: {items: []}, store: {cards: [], member: author}};
let storeGet = {packSelected: new Set()};
const pack = {key: 'pack:p:1', id: 'p', title: '写作包', note: '', mine: false, member: author,
  cards: ['a', 'b', 'c'], local: {total: 3, installed: 0, installable: 3, blocked: 0},
  updated_at: '2026-10-01T00:00:00Z'};

// 自己的包、没写推荐语：状态提示补写，只读列出 3 个技能和用途，不出勾选框。
const mineCards = [
  card('a', 'acme-a', 'mine', {title: '甲技能', summary: '整理周报'}),
  card('b', 'acme-b', 'mine', {title: '乙技能', summary: '', description: '把会议记录整理成要点。再发出去',
    required_by: ['acme-a']}),
  card('c', 'acme-c', 'mine', {title: '丙技能', summary: '<img src=x onerror=alert(1)> 写摘要'}),
];
state.store.cards = mineCards;
const minePack = {...pack, mine: true, local: {total: 3, installed: 3, installable: 0, blocked: 0}};
const mine = storePackGetHtml(minePack);
if (!mine.includes('还没写推荐语')) fail('自己的包没写推荐语时应提示补写：' + mine);
if (!mine.includes('包里的 3 个技能')) fail('自己的包也要写包里有几个技能：' + mine);
if (!mine.includes('整理周报') || !mine.includes('把会议记录整理成要点') || !mine.includes('写摘要')) {
  fail('只读清单要列出每个技能的用途：' + mine);
}
if (mine.includes('data-pack-check')) fail('只读清单不该出现勾选框：' + mine);
if (!mine.includes('is-readonly')) fail('自己的包清单要用只读样式：' + mine);
if (mine.includes('<img') || !mine.includes('&lt;img')) fail('用途里的标签要转成纯文字：' + mine);
if (mine.includes('我上架的') || mine.includes('tag-warn')) fail('自己的包只读行不显示状态标签：' + mine);
if ((mine.match(/包里 1 个技能要用它/g) || []).length !== 1) {
  fail('自己的包被需要时仍要写包里几个技能要用它：' + mine);
}
const mineWithNote = storePackGetHtml({...minePack, note: '一起用'});
if (!mineWithNote.includes('我的技能包') || mineWithNote.includes('还没写推荐语')) {
  fail('写了推荐语时应保持原来的状态说明：' + mineWithNote);
}

// 同事的包、全部装好：状态照旧，只读列出技能；被包里两个技能需要时写清楚。
const doneCards = [
  card('a', 'acme-a', 'installed', {title: '甲技能', summary: '整理周报', required_by: ['acme-b', 'acme-c', 'acme-x']}),
  card('b', 'acme-b', 'installed', {title: '乙技能', summary: '写会议纪要', required_by: ['acme-a']}),
  card('c', 'acme-c', 'installed', {title: '丙技能', summary: '发周报'}),
];
state.store.cards = doneCards;
const done = storePackGetHtml({...pack, local: {total: 3, installed: 3, installable: 0, blocked: 0}});
if (!done.includes('已全部在技能库中')) fail('全部装好时状态照旧：' + done);
if (!done.includes('包里的 3 个技能') || !done.includes('整理周报') || !done.includes('写会议纪要') || !done.includes('发周报')) {
  fail('全部装好时也要列出技能和用途：' + done);
}
if (done.includes('data-pack-check')) fail('全部装好时只读清单不该出现勾选框：' + done);
if (!done.includes('已有')) fail('同事的包全部装好时只读行保持「已有」：' + done);
if ((done.match(/包里 2 个技能要用它/g) || []).length !== 1) fail('只数属于本包的需要者，并写包里 N 个技能要用它：' + done);

// 同事的包、可以安装：保持勾选清单，每行多一句用途。
const freshCards = [
  card('a', 'acme-a', 'installable', {title: '甲技能', summary: '整理周报'}),
  card('b', 'acme-b', 'installable', {title: '乙技能', summary: '写会议纪要'}),
  card('c', 'acme-c', 'installable', {title: '丙技能', summary: '发周报'}),
];
state.store.cards = freshCards;
storeGet = {packSelected: new Set(['a', 'b', 'c'])};
const fresh = storePackGetHtml(pack);
if ((fresh.match(/data-pack-check/g) || []).length !== 3) fail('可以安装时每行都要有勾选框：' + fresh);
if ((fresh.match(/pack-skill-sum/g) || []).length !== 3) fail('可以安装时每行都要有一句用途：' + fresh);

// 标题下的一句介绍。
const quoted = storePackIntroHtml({...pack, note: '一起用'});
if (!quoted.includes('“一起用”')) fail('有推荐语时照旧显示引号推荐语：' + quoted);
const intro = storePackIntroHtml(pack);
if (!intro.includes('这几个技能配合着用，建议一起装。')) fail('没有推荐语时应写固定说明：' + intro);
if (!intro.includes('不需要的可以取消勾选。')) fail('现在能勾选时应提示可以取消：' + intro);
const allDoneIntro = storePackIntroHtml({...pack, local: {total: 3, installed: 3, installable: 0, blocked: 0}});
if (allDoneIntro.includes('不需要的可以取消勾选。')) fail('全部装好后不该再说可以取消勾选：' + allDoneIntro);
state.queue = {items: [{key: 'x', pack: 'pack:p:1', status: 'waiting'}]};
if (storePackIntroHtml(pack).includes('不需要的可以取消勾选。')) fail('这个包有进行中的安装时不该催着取消：' + storePackIntroHtml(pack));
state.queue = {items: []};
if (storePackIntroHtml({...pack, mine: true}) !== '') fail('自己的包没写推荐语时标题下不显示这句');
"""
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(source.count('placeholder="写一句这套技能能做什么，同事更容易看懂"'), 2,
                         '打包窗口和上架核对的推荐语输入框都要加新的提示文字')

    def test_store_pack_frontend_markers(self):
        source = (STATIC / 'app.js').read_text()
        html = (STATIC / 'index.html').read_text()
        for name in ('storePackCardHtml', 'storeNeedsHtml', 'storePackRowHtml', 'renderStorePackDetail',
                     'storePackGetHtml', 'storePackSelection', 'storePackNeeds', 'storeQueueGroups',
                     'storeQueuePackHtml', 'storeDetectedHtml', 'storeReviewPacksHtml', 'openPackDialog',
                     'savePackDialog', 'gotoStoreTag'):
            self.assertIn(f'function {name}(', source)
        for text in ('安装（含 ', '安装时一起装上', '去启用这 ', '属于「', '请至少选一个',
                     '已全部在技能库中', '已更新技能包', '整理成技能包', '补上前置依赖',
                     '个技能都要用它', '只是不再打包，包里的技能照常上架', '去启用已装的 '):
            self.assertIn(text, source)
        for pattern in (r'escape\(pack\.title\)', r'escape\(pack\.note\)', r'escape\(pack\.member\.name\)',
                        r'escape\(item\.evidence\)'):
            self.assertRegex(source, pattern)
        for marker in ('id="sp-stack"', 'id="store-pack-dialog"', 'id="store-pack-list"', 'id="store-hints"'):
            self.assertIn(marker, html)
        detail = extract_function(source, 'function storePackGetHtml(') + extract_function(source, 'function packSkillRowHtml(') + extract_function(source, 'function storePackGoHtml(')
        for marker in ('data-pack-check', 'data-pack-install', 'data-store-goto-tag',
                       'data-store-pack-unpack', 'data-store-pack-edit'):
            self.assertIn(marker, detail)
        queue = extract_function(source, 'function storeQueuePackHtml(')
        self.assertIn('data-store-queue-pack', queue)
        self.assertIn('aria-expanded', queue)
        handlers = source[source.index("$('detail-body').addEventListener('click'"):]
        self.assertIn("target.closest('[data-store-goto-tag]')", handlers[:1500], '包详情里的去启用按钮要接上标签筛选')


if __name__ == '__main__':
    unittest.main()
