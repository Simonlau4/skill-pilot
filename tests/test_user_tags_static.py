"""用户标签界面：只读页面文件，不启动看板，也不联网。"""
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


class UserTagsStaticTests(unittest.TestCase):
    def test_ai_copy_no_longer_says_it_adds_tags(self):
        html = (STATIC / 'index.html').read_text()
        source = (STATIC / 'app.js').read_text()
        combined = html + source
        removed = [
            '设置好以后，技能会自动有中文名、中文说明、分组和标签。',
            '再打上标签',
            '按用途分组和打标签',
            '还差的是缺中文名、中文说明或标签的技能。',
            '删除后不会再自动整理技能的中文名、说明和标签',
            '正在整理中文名和标签',
            '会重新整理全部中文名、分组、中文说明和标签',
            '技能的中文名、说明和标签',
        ]
        for text in removed:
            self.assertNotIn(text, combined, 'AI 整理不该再提标签：' + text)
        self.assertIn('正在整理中文名和说明', source)
        self.assertIn('待处理技能尚缺少中文名称或中文说明。', source)
        self.assertIn('删除后不会再自动整理技能的中文名、说明和分组', source)
        self.assertIn('会重新整理全部中文名、分组和中文说明', source)

    def test_detail_tag_editor_uses_user_tags_and_local_refresh(self):
        html = (STATIC / 'index.html').read_text()
        source = (STATIC / 'app.js').read_text()
        self.assertIn('id="used-tags"', html)
        self.assertIn('api(\'/api/tags\'', source)
        self.assertIn('data-tag-remove', source)
        self.assertIn('去掉标签「', source)
        self.assertIn('list="used-tags"', source)
        self.assertIn('添加自定义标签（如项目、领域）', source)
        self.assertIn('添加标签后可快速筛选技能，留空则不设标签。', source + html)
        self.assertIn('if (!canWrite) return tagList;', source)

        apply = extract_function(source, 'function applyTagResult(')
        self.assertIn('renderUsedTags();', apply)
        self.assertIn('renderSkillTable();', apply)
        self.assertIn('if (state.detail) renderDetail();', apply)
        self.assertNotIn('refreshTable', apply)
        self.assertNotIn('refreshEverything', apply)
        self.assertIn("if (state.tag && !(state.table?.skills || []).some(skill => (skill.tags || []).includes(state.tag))) state.tag = '';", apply)

    def test_batch_tag_dialog_defaults_to_every_skill_and_uses_same_endpoint(self):
        html = (STATIC / 'index.html').read_text()
        source = (STATIC / 'app.js').read_text()
        self.assertIn('<button id="tags-batch" class="link-btn" hidden>批量打标签</button>', html)
        self.assertIn('<dialog id="tag-dialog" class="dialog dialog-wide"', html)
        self.assertIn('已选 0 个', html)
        self.assertIn('移除标签', html)
        self.assertIn('$(\'tag-dialog\').showModal();', source)
        self.assertIn('listedSkillNames.length < 2', source)
        self.assertIn('!state.table.write_allowed || listedSkillNames.length < 2', source)
        self.assertIn('data-tag-skill="${escape(name)}" checked', source)
        self.assertIn('给这 ${tagDialogNames.length} 个技能打标签', source)
        self.assertIn('已选 ${checked.length} 个', source)
        self.assertIn('hasTag', source)
        self.assertIn('已给 ${names.length} 个技能添加标签「${canonical}」', source)
        self.assertIn('已从 ${names.length} 个技能去掉「${canonical}」', source)

    def test_tag_input_cleaning_matches_backend_basics(self):
        source = (STATIC / 'app.js').read_text()
        function = extract_function(source, 'function cleanTagInput(')
        script = function + r'''
const cases = [
  ['  博客  ', '博客'],
  ['#项目', '项目'],
  ['＃＃ 客户 ', '客户'],
  ['#  # 工具集', '工具集'],
  ['', ''],
];
for (const [input, expected] of cases) {
  const actual = cleanTagInput(input);
  if (actual !== expected) {
    console.error(JSON.stringify(input) + ' -> ' + JSON.stringify(actual) + '，应为 ' + JSON.stringify(expected));
    process.exit(1);
  }
}
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
