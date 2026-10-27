"""独立副本提示按技能计数，已有技能变化不能再显示为新安装。"""
import subprocess
import unittest

from tests.test_nav_static import STATIC, extract_function


class ExternalSkillsStaticTests(unittest.TestCase):
    def test_banner_distinguishes_new_and_existing_copies(self):
        source = (STATIC / 'app.js').read_text()
        script = extract_function(source, 'function renderNewSkills(') + r'''
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {});
  return elements.get(id);
};
const hint = () => '';
const escape = value => value;
const pathLabel = value => value;
let writeBusy = null;
const state = {table: {new_skills: [], changed_skills: [], write_allowed: true}};
const fail = message => { throw new Error(message); };
renderNewSkills();
if (!$('new-banner').hidden) fail('没有独立副本时不应显示提醒');
const fresh = {name: 'new', relative: '.codex/skills/new', registered: false};
state.table.new_skills = [fresh, {...fresh, relative: '.claude/skills/new'}];
renderNewSkills();
if ($('new-text').innerHTML !== '发现 1 个未纳入管理的新技能') fail('多份新技能应只算一个');
if ($('new-absorb').textContent !== '一键收编') fail('新技能收编入口不应改变');
const existing = {name: 'existing', relative: '.codex/skills/existing', registered: true, same_content: true};
state.table.new_skills = [];
state.table.changed_skills = [existing, {...existing, relative: '.claude/skills/existing', same_content: false}];
renderNewSkills();
if ($('new-text').innerHTML !== '1 个已有技能出现外部副本') fail('已有技能不能当成新安装，也不能按副本数计数');
if ($('new-absorb').textContent !== '统一管理') fail('已有技能应提示统一管理');
if ($('new-absorb').disabled) fail('核对完成后应可继续统一管理');
writeBusy = '正在收编…';
renderNewSkills();
if (!$('new-absorb').disabled) fail('正在写入时应保持禁用');
writeBusy = null;
renderNewSkills();
if ($('new-absorb').disabled) fail('写入结束后应重新启用按钮');
state.table.write_allowed = false;
renderNewSkills();
if (!$('new-absorb').disabled) fail('只读时不能统一管理');
state.table.write_allowed = true;
if (!$('new-list').innerHTML.includes('内容相同') || !$('new-list').innerHTML.includes('内容有变化')) fail('应显示各副本的内容状态');
state.table.new_skills = [fresh];
renderNewSkills();
if ($('new-text').innerHTML !== '发现 1 个未纳入管理的新技能；1 个已有技能出现外部副本') fail('混合出现时应分别计数');
for (const path of ['.codex/skills/new', '.codex/skills/existing', '.claude/skills/existing']) {
  if (!$('new-list').innerHTML.includes(path)) fail('明细不能遗漏副本位置');
}
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('renderNewSkills();', extract_function(source, 'function renderBusyAreas('))
