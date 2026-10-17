"""安装摘要卡片：只读页面脚本，不启动看板，也不联网。"""
import subprocess
import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / 'skill_pilot' / 'web' / 'static'


def extract_function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 0
    index = opening
    while index < len(source):
        char = source[index]
        if char in '"\'':
            quote = char
            index += 1
            while index < len(source):
                if source[index] == '\\':
                    index += 2
                    continue
                if source[index] == quote:
                    break
                index += 1
        elif char == '`':
            index += 1
            while index < len(source) and source[index] != '`':
                if source[index] == '\\':
                    index += 2
                    continue
                index += 1
        elif source.startswith('//', index):
            index = source.index('\n', index)
        elif source.startswith('/*', index):
            index = source.index('*/', index) + 1
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise AssertionError('没有找到完整的函数：' + signature)


class InstallSummaryStaticTests(unittest.TestCase):
    def test_summary_card_shows_name_purpose_and_agent(self):
        source = (STATIC / 'app.js').read_text()
        escape = next(line for line in source.splitlines() if line.startswith('const escape = '))
        names = [
            'function installReadyNames(',
            'function installPurpose(',
            'function installSummary(',
            'function installSummaryCard(',
        ]
        functions = '\n'.join(extract_function(source, name) for name in names)
        install_all = extract_function(source, 'async function installAllForCurrent(')
        picker = extract_function(source, 'function renderInstallSkills(')
        self.assertNotIn('confirmDialog', install_all)
        self.assertNotIn('查看全部', picker)
        self.assertIn("button.dataset.installMore !== undefined", source)

        script = escape + '\n' + r'''
const icon = name => '';
function installDismissButton() { return '<button data-install-dismiss>关闭</button>'; }
''' + functions + r'''
const purpose = 'Prioritize a backlog of feature ideas based on impact, effort, risk, and strategic alignment with top 5 recommendations. Use when prioritizing a feature backlog, making scope decisions, or ranking product ideas.';
const single = {
  skills: [{name: 'prioritize-features', description: purpose}],
  conflicts: [],
};
const summary = installSummary(single);
if (summary.items.length !== 1) fail('单个技能应有一条摘要');
if (summary.items[0].name !== 'prioritize-features') fail('摘要没有技能名');
if (summary.items[0].purpose !== purpose.split('. ')[0] + '.') fail('摘要没有停在第一句：' + summary.items[0].purpose);
if (summary.items[0].purpose.includes('Use when')) fail('一句话用途带上了后面的句子');

const card = installSummaryCard(single, 'Codex', false, true);
for (const text of ['prioritize-features', summary.items[0].purpose, '装给 Codex', '全部装给 Codex', '挑一挑', '1 个可以直接装']) {
  if (!card.includes(text)) fail('卡片缺少：' + text);
}
if (card.includes('查看全部') || card.includes('收起')) fail('一个技能不应出现查看全部');
if (card.includes('confirm') || card.includes('确认安装')) fail('摘要卡片多了确认步骤');

const escaped = installSummaryCard({
  skills: [{name: 'a<b', description: '用途&说明。后面不用出现。'}],
  conflicts: [],
}, 'Codex "x"', false, true);
if (escaped.includes('a<b') || escaped.includes('用途&说明') || escaped.includes('Codex "x"')) fail('卡片没有转义技能名、用途或 agent');
if (!escaped.includes('a&lt;b') || !escaped.includes('用途&amp;说明') || !escaped.includes('Codex &quot;x&quot;')) fail('转义结果不对');
if (escaped.includes('后面不用出现')) fail('转义后的卡片仍带上了第二句');

const versioned = installPurpose('Use version 1.2 before the cutoff. Then stop.');
if (versioned !== 'Use version 1.2 before the cutoff.') fail('小数点被当成了句号：' + versioned);
const chinese = installPurpose('先排优先级。再决定范围。');
if (chinese !== '先排优先级。') fail('中文第一句不对：' + chinese);
const long = installPurpose('甲'.repeat(180) + '。后面');
if (long.length !== 161 || !long.endsWith('…') || long.includes('后面')) fail('过长的第一句没有截断：' + long.length);

const many = {
  skills: ['alpha', 'beta', 'gamma', 'delta'].map(name => ({name, description: name + ' 的用途。'})),
  conflicts: [{name: 'delta', same_content: false}],
};
const folded = installSummaryCard(many, 'Codex', false, true);
for (const name of ['alpha', 'beta', 'gamma']) {
  if (!folded.includes(name)) fail('前几项缺少 ' + name);
}
if (folded.includes('delta')) fail('未展开时不应出现第四个技能');
if (!folded.includes('查看全部') || folded.includes('收起')) fail('多于三个时应有查看全部');
if (!folded.includes('装给 Codex') || !folded.includes('要先对比')) fail('多个技能时应能看到装给谁和要先对比');

const opened = installSummaryCard(many, 'Codex', true, true);
if (!opened.includes('delta') || !opened.includes('delta 的用途')) fail('查看全部后应能核对第四个技能');
if (!opened.includes('收起') || opened.includes('>查看全部<')) fail('展开后应收起，而不是继续只给查看全部');

const installed = installSummaryCard({
  skills: [{name: 'prioritize-features', description: '已经有了。'}],
  conflicts: [{name: 'prioritize-features', same_content: true}],
}, 'Codex', false, true);
if (!installed.includes('已经装过')) fail('已经装过的技能卡片上应标明');
if (installed.includes('data-install-all') || installed.includes('装给 Codex') || installed.includes('挑一挑')) fail('都装过了不应再提示装给谁');

const locked = installSummaryCard(single, 'Codex', false, false);
if (!locked.includes('data-install-all disabled')) fail('不能修改时，一键安装应不可点');
if (locked.includes('data-install-pick disabled')) fail('挑一挑不该被这次改动禁用');

const empty = installSummaryCard({skills: [], conflicts: []}, 'Codex', false, true);
if (!empty.includes('这个链接里没找到技能')) fail('没找到技能时卡片没有说明');
if (empty.includes('install-summary-name') || empty.includes('查看全部')) fail('没找到技能时不应列出技能');

function fail(message) { console.error(message); process.exit(1); }
'''
        result = subprocess.run(['node', '--check', str(STATIC / 'app.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
