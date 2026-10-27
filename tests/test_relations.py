import json
import tempfile
import time
import unittest
from pathlib import Path

from skill_pilot import relations, team_hub
from tests import set_up_home


class RelationsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot relations ')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, temporary.name)
        self.registry = {'skills': {}}

    def skill(self, name, text='', **extra):
        path = self.home / '.skill-pilot/store' / name
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(text, encoding='utf-8')
        self.registry['skills'][name] = {'requires': [], **extra}

    def detected(self, *names):
        return relations.detect_requires(self.home, self.registry, names)

    def test_every_forward_keyword_and_sentence_boundaries(self):
        self.skill('acme-main')
        self.skill('acme-base')
        for word in relations.MUST_WORDS:
            for separator in ('\n', '。', '；', ';', '！', '？', '!', '?', '. '):
                with self.subTest(word=word, separator=separator):
                    self.skill('acme-main', word.upper() + ' acme-base' + separator + 'another sentence')
                    self.assertEqual(self.detected('acme-main'), {'acme-main': [
                        {'requires': 'acme-base', 'evidence': word.upper() + ' acme-base'}]})
                    self.skill('acme-main', word + separator + 'acme-base')
                    self.assertEqual(self.detected('acme-main'), {})

    def test_every_reverse_keyword_and_both_groups(self):
        for name in ('acme-notes', 'acme-tables', 'acme-cli', 'acme-shared', 'other-main'):
            self.skill(name)
        for word in relations.FIRST_WORDS:
            for reference in ('acme-*', 'acme-notes/tables/cli'):
                with self.subTest(word=word, reference=reference):
                    self.skill('acme-shared', reference + ' ' + word.upper())
                    result = self.detected(*self.registry['skills'])
                    self.assertEqual(set(result), {'acme-notes', 'acme-tables', 'acme-cli'})
                    self.assertTrue(all(r == [{'requires': 'acme-shared', 'evidence': reference + ' ' + word.upper()}]
                                        for r in result.values()))

    def test_routes_are_excluded_but_unified_handoff_is_required(self):
        self.skill('acme-main')
        self.skill('acme-base')
        for word in (*relations.ROUTING_WORDS, '走', '转', '交给'):
            with self.subTest(word=word):
                self.skill('acme-main', '必须 ' + word.upper() + ' acme-base')
                self.assertEqual(self.detected('acme-main'), {})
        self.skill('acme-main', '统一交由 acme-base 处理')
        self.assertEqual(self.detected('acme-main')['acme-main'][0]['requires'], 'acme-base')
        self.skill('acme-main', 'acme-* 必须先读')
        self.assertEqual(self.detected('acme-main'), {})  # Group notation is reverse-only.

    def test_paths_whole_words_rejections_existing_and_short_names(self):
        for name in ('acme', 'acme-cli', 'acme-main', 'ab'):
            self.skill(name)
        self.skill('acme-main', 'must acme-cli-extra acme_extra acme-cli2 ab')
        self.assertEqual(self.detected('acme-main'), {})
        for path in ('../acme-cli/', '../acme-cli'):
            self.skill('acme-main', 'must read ' + path)
            self.assertEqual(self.detected('acme-main')['acme-main'][0]['requires'], 'acme-cli')
        self.registry['skills']['acme-main']['dependency_decisions'] = {'acme-cli': 'rejected'}
        self.assertEqual(self.detected('acme-main'), {})
        self.registry['skills']['acme-main'].update(dependency_decisions={}, requires=['acme-cli'])
        self.assertEqual(self.detected('acme-main'), {})

    def test_cycles_stable_order_first_evidence_and_limit(self):
        self.skill('acme-main', 'must acme-base\nmust acme-base again\n必须 acme-other ' + '证' * 150)
        self.skill('acme-base', 'must acme-main')
        self.skill('acme-other', requires=['acme-main'])
        result = self.detected('acme-other', 'acme-main', 'acme-base')
        self.assertEqual(result, {'acme-base': [{'requires': 'acme-main', 'evidence': 'must acme-main'}]})
        self.registry['skills']['acme-base']['requires'] = ['acme-main']
        self.assertEqual(self.detected('acme-main'), {})
        self.registry['skills']['acme-other']['requires'] = []
        evidence = self.detected('acme-main')['acme-main'][0]['evidence']
        self.assertEqual(len(evidence), 120)
        self.skill('acme-main', 'must acme-other\nmust acme-other again')
        self.assertEqual(self.detected('acme-main')['acme-main'][0]['evidence'], 'must acme-other')

    def test_names_are_case_sensitive_and_plain_words_need_skill_context(self):
        for name in ('acme-main', 'acme-drive', 'pdf', 'image'):
            self.skill(name)
        for text in ('PDF output is required', 'must use pdf and image output',
                     'must use PDF skill', 'must read ../PDF/', 'must read ACME-DRIVE'):
            with self.subTest(text=text):
                self.skill('acme-main', text)
                self.assertEqual(self.detected('acme-main'), {})
        for text in ('must use `pdf`', 'must read ../pdf/references/rules.md',
                     'must use pdf技能', '必须先读技能 pdf', 'must use pdf skill', 'must use skill pdf'):
            with self.subTest(text=text):
                self.skill('acme-main', text)
                self.assertEqual(self.detected('acme-main')['acme-main'][0]['requires'], 'pdf')
        self.skill('acme-main', 'must read `use image`')
        self.assertEqual(self.detected('acme-main')['acme-main'][0]['requires'], 'image')

    def test_invoked_by_another_skill_does_not_reverse_direction(self):
        self.skill('acme-main', 'If acme-router invoked this skill, return the result to the caller')
        self.skill('acme-router')
        self.assertEqual(self.detected('acme-main'), {})

    def test_optional_prerequisites_are_not_dependencies(self):
        self.skill('acme-main')
        self.skill('acme-cli')
        for text in ('REQUIRED AS NEEDED: Use acme-cli or direct filesystem search',
                     'required if needed: acme-cli', 'optional prerequisite acme-cli',
                     '可选：必须先读 acme-cli', '按需先加载 acme-cli', '如需，先调用 acme-cli'):
            with self.subTest(text=text):
                self.skill('acme-main', text)
                self.assertEqual(self.detected('acme-main'), {})

    def test_reverse_before_alone_is_not_a_preread_instruction(self):
        self.skill('acme-main')
        for text in ('Use acme-main to return chapter and PDF page anchors before applying the idea',
                     '某某功能已并入 acme-main，before applying the idea return chapter anchors'):
            with self.subTest(text=text):
                self.skill('acme-shared', text)
                self.assertEqual(self.detected('acme-main'), {})

    def test_explicit_relative_drive_rule_is_a_dependency(self):
        text = '必须先阅读 ../acme-drive/references/policy.md 的规则'
        self.skill('acme-main', text)
        self.skill('acme-drive')
        self.assertEqual(self.detected('acme-main'), {'acme-main': [{'requires': 'acme-drive', 'evidence': text}]})

    def test_detects_150_skills_with_50_sentences_each(self):
        for index in range(150):
            text = '\n'.join(['must keep acme-unrelated-example descriptions unchanged'] * 49 + [
                'must read acme-skill-' + str(index + 1).zfill(3) if index < 149 else 'No prerequisites'])
            self.skill('acme-skill-' + str(index).zfill(3), text)
        started = time.perf_counter()
        result = self.detected(*self.registry['skills'])
        elapsed = time.perf_counter() - started
        self.assertEqual(result, {'acme-skill-' + str(index).zfill(3): [{
            'requires': 'acme-skill-' + str(index + 1).zfill(3),
            'evidence': 'must read acme-skill-' + str(index + 1).zfill(3)}] for index in range(149)})
        self.assertLess(elapsed, 10, '合成技能库识别超过宽松的 10 秒上限')

    def test_only_skill_markdown_is_read_without_mutating_registry(self):
        self.skill('acme-main')
        self.skill('acme-base')
        path = self.home / '.skill-pilot/store/acme-main/other.md'
        path.write_text('must acme-base')
        before = json.dumps(self.registry)
        self.assertEqual(self.detected('acme-main'), {})
        self.assertEqual(json.dumps(self.registry), before)

    def test_suggestions_prefix_links_exclusions_and_determinism(self):
        for name in ('acme', 'acme-cli', 'acme-notes', 'other-main', 'third-tool', 'lark-official'):
            self.skill(name)
        self.skill('other-main', '[link](../third-tool/SKILL.md)')
        self.registry['skills']['lark-official']['source'] = {
            'record': {'sourceType': 'well-known', 'sourceUrl': 'https://open.feishu.cn/lark-cli/skills/lark-official'}}
        names = list(self.registry['skills'])
        result = relations.suggest_packs(self.home, self.registry, names)
        self.assertEqual([p['skills'] for p in result], [['acme', 'acme-cli', 'acme-notes'], ['other-main', 'third-tool']])
        self.assertEqual([p['why'] for p in result], ['名字开头相同', '互相链接'])
        self.assertEqual(result, relations.suggest_packs(self.home, self.registry, reversed(names)))
        existing = [{'id': 'acme', 'title': '旧包', 'skills': ['acme', 'acme-cli']}]
        self.assertEqual(relations.suggest_packs(self.home, self.registry, names, existing)[0]['skills'], ['other-main', 'third-tool'])

    def test_suggestion_ids_titles_and_valid_limits(self):
        for name in ('acme-one', 'acme-two'):
            self.skill(name)
        path = self.home / '.skill-pilot/labels.json'
        path.write_text(json.dumps({'groups': [{'name': '示例', 'skills': {
            'acme-one': ['Acme 第一', '示例'], 'acme-two': ['Acme 第二', '示例']}}]}))
        existing = [{'id': 'acme', 'skills': []}, {'id': 'acme-2', 'skills': []}]
        pack = relations.suggest_packs(self.home, self.registry, self.registry['skills'], existing)[0]
        self.assertEqual((pack['id'], pack['title']), ('acme-3', 'Acme 技能包'))
        for title in ('ABCDEFGHIJKLMNOPQR', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
            path.write_text(json.dumps({'groups': [{'name': '示例', 'skills': {
                'acme-one': [title + ' 第一', '示例'], 'acme-two': [title + ' 第二', '示例']}}]}))
            pack = relations.suggest_packs(self.home, self.registry, self.registry['skills'])[0]
            self.assertEqual(pack['title'], title[:20])
            team_hub.clean_pack(pack)
        self.skill('different-main', '../acme-one/')
        path.unlink()
        pack = relations.suggest_packs(self.home, self.registry, self.registry['skills'])[0]
        self.assertTrue(pack['id'])  # No common name prefix; still a valid identifier.
        team_hub.clean_pack(pack)

    def test_prefix_does_not_join_different_github_repositories_but_links_do(self):
        self.skill('acme-browser', source={'source': 'https://github.com/Acme/browser'})
        self.skill('acme-reach', source={'source': 'https://github.com/Acme/reach'})
        self.assertEqual(relations.suggest_packs(self.home, self.registry, self.registry['skills']), [])
        self.skill('acme-browser', '../acme-reach/SKILL.md', source={'source': 'https://github.com/Acme/browser'})
        result = relations.suggest_packs(self.home, self.registry, self.registry['skills'])
        self.assertEqual(result[0]['skills'], ['acme-browser', 'acme-reach'])
        self.assertEqual(result[0]['why'], '互相链接')
        self.registry['skills']['acme-reach']['source'] = {'source': 'https://github.com/acme/BROWSER'}
        self.skill('acme-browser', source={'source': 'https://github.com/Acme/browser'})
        result = relations.suggest_packs(self.home, self.registry, self.registry['skills'])
        self.assertEqual(result[0]['why'], '名字开头相同')

    def test_long_identifier_title_uses_first_segment(self):
        for name in ('acme-design-taste-frontend-one', 'acme-design-taste-frontend-two'):
            self.skill(name)
        (self.home / '.skill-pilot/labels.json').write_text(json.dumps({'groups': [{'name': '示例', 'skills': {
            'acme-design-taste-frontend-one': ['设计一', '示例'],
            'acme-design-taste-frontend-two': ['设计二', '示例']}}]}))
        pack = relations.suggest_packs(self.home, self.registry, self.registry['skills'])[0]
        self.assertEqual(pack['id'], 'acme-design-taste-frontend')
        self.assertEqual(pack['title'], 'Acme 技能包')

    def test_default_pack_title_uses_only_real_labels(self):
        for name in ('acme-design-one', 'acme-design-two'):
            self.skill(name)
        path = self.home / '.skill-pilot/labels.json'
        for entries, expected in (
                ({}, 'Acme 技能包'),
                ({'acme-design-one': ['GSAP 动效一', '示例'],
                  'acme-design-two': ['GSAP 动效二', '示例']}, 'GSAP 技能包'),
                ({'acme-design-one': ['GSAP 动效一', '示例']}, 'Acme 技能包')):
            with self.subTest(labels=entries):
                if entries:
                    path.write_text(json.dumps({'groups': [{'name': '示例', 'skills': entries}]}))
                else:
                    path.unlink(missing_ok=True)
                pack = relations.suggest_packs(self.home, self.registry, self.registry['skills'])[0]
                self.assertEqual(pack['id'], 'acme-design')
                self.assertEqual(pack['title'], expected)

    def test_large_families_respect_pack_size(self):
        for index in range(51):
            self.skill('acme-' + str(index))
        result = relations.suggest_packs(self.home, self.registry, self.registry['skills'])
        self.assertEqual([len(p['skills']) for p in result], [49, 2])
        team_hub.clean_packs(result)
