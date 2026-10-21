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
                       'id="sp-team"', 'id="sp-star"'):
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
            line(source, 'const storeTitle = '),
            line(source, 'const storeQueueItem = '),
            extract_function(source, 'const STORE_STATUS = {') + ';',
            line(source, 'const starIcon = '),
            extract_function(source, 'function storeRatingHtml('),
            extract_function(source, 'function storeStatusTag('),
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
            extract_function(source, 'function storeQueueRowHtml('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
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
            extract_function(source, 'function storeReviewHtml('),
        ]
        script = '\n'.join(parts) + r'''
function fail(message) { console.error(message); process.exit(1); }
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


if __name__ == '__main__':
    unittest.main()
