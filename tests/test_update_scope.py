"""更新结果说明检查范围：只读页面脚本，不启动看板，也不联网。"""
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'skill_pilot' / 'web' / 'static'


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


class UpdateScopeTests(unittest.TestCase):
    def test_result_sentence_covers_all_partial_and_none(self):
        source = (STATIC / 'app.js').read_text()
        html = (STATIC / 'index.html').read_text()
        self.assertNotIn('没有要查的技能', source)
        toolbar = html.split('class="toolbar-end"', 1)[1].split('</span>', 1)[0]
        self.assertNotIn('updates-status', toolbar)
        between = html.split('id="updates-check"', 1)[1].split('id="updates-failed"', 1)[0]
        self.assertIn('id="updates-status"', between)
        bar = extract_function(source, 'function renderUpdateBar(')
        self.assertIn('updateResultSentence(review)', bar)
        self.assertNotIn('均为最新版本', bar)

        number = next(line for line in source.splitlines() if line.startswith('const number = '))
        functions = '\n'.join(extract_function(source, name) for name in (
            'function friendlyDate(',
            'function uncheckedReasons(',
            'function updateCheckedAtText(',
            'function updateResultSentence(',
        ))
        script = number + '\n' + functions + r'''
const stamp = '2026-10-17T08:09:00Z';
const when = updateCheckedAtText(stamp);
if (!when || when.includes('盘点') || when.includes('名单读取')) fail('检查时间不对：' + when);

const all = updateResultSentence({
  total: 3,
  checked_at: stamp,
  unchecked: [],
  skills: [{has_update: false}, {has_update: false}, {has_update: false}],
});
for (const text of ['已检查 3 个技能', '均为最新版本', when + ' 检查']) {
  if (!all.includes(text)) fail('全部已查缺少：' + text + ' / ' + all);
}
if (all.includes('没查') || all.includes('没有要查')) fail('全部已查不该说没查：' + all);

const partial = updateResultSentence({
  total: 2,
  checked_at: stamp,
  unchecked: [{reason: '没有上游来源', count: 3}],
  skills: [{has_update: false}, {has_update: false}],
});
for (const text of ['已检查 2 个技能', '其余 3 个未检查', '没有上游来源', when + ' 检查']) {
  if (!partial.includes(text)) fail('部分未查缺少：' + text + ' / ' + partial);
}
if (partial.includes('最新')) fail('没查的被说成最新：' + partial);

const mixed = updateResultSentence({
  total: 1,
  checked_at: stamp,
  unchecked: [
    {reason: '没有上游来源', count: 2},
    {reason: '没有 agent 在用', count: 4},
  ],
  skills: [{has_update: true}],
});
for (const text of ['1 个有可用更新', '其余 6 个未检查', '2 个没有上游来源', '4 个没有 agent 在用']) {
  if (!mixed.includes(text)) fail('多种原因缺少：' + text + ' / ' + mixed);
}
if (mixed.includes('最新')) fail('有没查的还说最新：' + mixed);

const none = updateResultSentence({
  total: 0,
  checked_at: stamp,
  unchecked: [{reason: '没有上游来源', count: 5}],
  skills: [],
});
for (const text of ['暂无可检查的技能', '5 个技能没有上游来源', '已跳过检查', when + ' 检查']) {
  if (!none.includes(text)) fail('没有可查缺少：' + text + ' / ' + none);
}
if (none.includes('最新') || none.includes('没有要查的技能') || none.includes('都是最新')) fail('没有可查的说法不对：' + none);

const both = updateResultSentence({
  total: 0,
  checked_at: stamp,
  unchecked: [
    {reason: '没有上游来源', count: 1},
    {reason: '没有 agent 在用', count: 2},
  ],
  skills: [],
});
if (!both.includes('1 个没有上游来源') || !both.includes('2 个没有 agent 在用') || !both.includes('已跳过检查')) {
  fail('两种原因都不可查时没写全：' + both);
}
if (both.includes('最新')) fail('都不可查时被说成最新：' + both);

const empty = updateResultSentence({total: 0, checked_at: stamp, unchecked: [], skills: []});
if (!empty.includes('暂无技能') || empty.includes('最新')) fail('空技能库说法不对：' + empty);

const unknown = updateResultSentence({total: 4, checked_at: stamp, skills: [{has_update: false}]});
if (unknown.includes('最新')) fail('没记下范围时不能说最新：' + unknown);

const failed = updateResultSentence({
  total: 2,
  checked_at: stamp,
  unchecked: [],
  skills: [{error: '没连上'}, {has_update: false}],
});
if (failed.includes('最新') || !failed.includes('未发现可用更新')) fail('有没查成的不能说最新：' + failed);

function fail(message) { console.error(message); process.exit(1); }
'''
        checked = subprocess.run(['node', '--check', str(STATIC / 'app.js')], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
