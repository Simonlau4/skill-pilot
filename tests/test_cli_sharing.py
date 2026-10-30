"""Command-line sharing: local folder installs, team publishing and branch-following sources."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from skill_pilot import operations, store, team, updates
from skill_pilot.sources import github, normalize
from skill_pilot.sources.update_github import Reader
from tests import test_github_inspect, test_operations, test_team, test_updates
from tests.test_team_hub import listing


# Fixtures are borrowed through their modules so discovery does not run those classes twice.
def run(home, *args):
    return subprocess.run([sys.executable, '-m', 'skill_pilot', *args], cwd=store.PROJECT,
                          env={**os.environ, 'SKILL_PILOT_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'},
                          capture_output=True, text=True, timeout=60)


class TeamCommandTests(unittest.TestCase):
    Team = test_team.TeamTests
    setUp, start, skill, make_home, registry = Team.setUp, Team.start, Team.skill, Team.make_home, Team.registry
    request, ok, configure, library, raw_put = Team.request, Team.ok, Team.configure, Team.library, Team.raw_put

    def shared(self, member=None):
        return {r['name']: r for card in team._catalog({'url': self.url, 'key': (member or self.first)['token']})['cards']
                for r in card['listings']}

    def test_local_folder_install_publish_and_named_install_through_the_command_line(self):
        self.configure()
        folder = self.root / 'Example drafts' / 'team notes'
        self.skill(folder, 'team-notes')
        (folder / '.env').write_text('example private value')
        (folder / 'guide.md').write_text('示例资料\n')
        preview = run(self.home, 'install', str(folder), '--agent', 'codex')
        self.assertEqual(preview.returncode, 2, preview.stdout + preview.stderr)
        self.assertIn('team-notes', preview.stdout)
        self.assertNotIn('team-notes', operations.load(self.home)[1]['skills'])
        installed = run(self.home, 'install', str(folder), '--agent', 'codex', '--yes', '--json')
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        skill = operations.load(self.home)[1]['skills']['team-notes']
        self.assertEqual(skill['assigned_agents'], ['codex'])
        self.assertEqual(normalize(skill['source'])['skip_reason'], 'self_made')
        self.assertTrue((self.home / '.codex/skills/team-notes/guide.md').is_file())
        self.assertTrue((folder / 'SKILL.md').is_file())

        before = self.shared()
        previewed = run(self.home, 'team', 'publish', 'team-notes', '--note', 'team-notes=示例推荐')
        self.assertEqual(previewed.returncode, 2, previewed.stdout + previewed.stderr)
        self.assertIn('不上传 .env', previewed.stdout)
        self.assertIn('--yes', previewed.stdout)
        self.assertEqual(self.shared(), before)
        published = run(self.home, 'team', 'publish', 'team-notes', '--note', 'team-notes=示例推荐', '--yes', '--json')
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        result = json.loads(published.stdout)
        self.assertEqual(result['published'], ['team-notes'])
        self.assertNotIn(self.first['token'], published.stdout)
        row = self.shared()['team-notes']
        self.assertEqual(row['note'], '示例推荐')
        self.assertEqual(row['source']['version'], result['listings'][0]['version'])

        listed = run(self.other, 'team', 'list')
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertIn('team-notes', listed.stdout)
        before = self.library(self.other)
        asked = run(self.other, 'team', 'install', 'team-notes', '--agent', 'claude')
        self.assertEqual(asked.returncode, 2, asked.stdout + asked.stderr)
        self.assertEqual(self.library(self.other), before)
        done = run(self.other, 'team', 'install', 'team-notes', '--agent', 'claude', '--yes', '--json')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result['installed'], ['team-notes'])
        self.assertEqual(len(result['operations']), 2)
        skill = operations.load(self.other)[1]['skills']['team-notes']
        self.assertEqual(skill['assigned_agents'], ['claude'])
        self.assertEqual(normalize(skill['source'])['kind'], 'team')
        self.assertTrue((self.other / '.claude/skills/team-notes/guide.md').is_file())
        self.assertFalse((self.other / '.claude/skills/team-notes/.env').exists())
        again = run(self.other, 'team', 'install', 'team-notes', '--json')
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(json.loads(again.stdout)['installed'], [])

        removing = run(self.home, 'team', 'unpublish', 'team-notes')
        self.assertEqual(removing.returncode, 2, removing.stdout + removing.stderr)
        self.assertIn('team-notes', self.shared())
        removed = run(self.home, 'team', 'unpublish', 'team-notes', '--yes')
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertNotIn('team-notes', self.shared())

    def test_named_install_resolves_titles_authors_and_rejects_unknown_agents_first(self):
        self.configure()
        self.raw_put([listing('acme-local', 'files', title='示例本地技能')])
        self.raw_put([listing('acme-local', 'files')], self.second)
        with self.assertRaisesRegex(ValueError, '--author'):
            team.find(self.other, 'acme-local')
        with self.assertRaisesRegex(ValueError, 'team list'):
            team.find(self.other, 'acme-missing')
        found = team.find(self.other, '示例本地技能')
        self.assertEqual((found['author'], found['installed']), ('示例成员 A', ['acme-local']))
        self.assertEqual(team.find(self.other, 'acme-local', author='示例成员 A')['key'], found['key'])
        # Your own listing is already in your library.
        self.assertEqual(team.find(self.home, 'acme-local', author='示例成员 A')['installed'], [])
        before = self.library(self.other)
        with patch.object(team, 'install', side_effect=AssertionError('must not install')):
            with self.assertRaisesRegex(ValueError, '已接入的 agent'):
                team.install_named(self.other, '示例本地技能', agents=['codex'], confirmed=True)
            preview = team.install_named(self.other, '示例本地技能', agents=['claude'])
        self.assertTrue(preview['needs_confirmation'])
        self.assertIn('claude', preview['message'])
        self.assertEqual(self.library(self.other), before)

    def test_publish_can_drop_wrong_dependencies_and_suggested_packs(self):
        self.configure()
        path = self.home / '.skill-pilot/store/acme-base/SKILL.md'
        path.write_text(path.read_text() + '\n任何 acme-* 技能都要先读这个\n')
        preview = team.publish(self.home, ['acme-extra'])
        self.assertEqual(preview['detected'][0]['requires'], 'acme-base')
        self.assertTrue(preview['needs_confirmation'])
        self.assertEqual(self.shared(), {})
        kept = team.publish(self.home, ['acme-extra'], rejected=[['acme-extra', 'acme-base']])
        self.assertEqual([r['name'] for r in kept['listings']], ['acme-extra'])
        self.assertFalse(kept['detected'][0]['adopted'])

        preview = team.publish(self.home, ['acme-main'])
        self.assertEqual({r['name'] for r in preview['listings']}, {'acme-main', 'acme-base'})
        pack = preview['packs'][0]
        self.assertEqual(set(pack['skills']), {'acme-main', 'acme-base'})
        with self.assertRaisesRegex(ValueError, '技能包'):
            team.publish(self.home, ['acme-main'], skip_packs=['missing-pack'])
        with self.assertRaisesRegex(ValueError, '推荐语'):
            team.publish(self.home, ['acme-main'], notes={'acme-other': '示例'})
        result = team.publish(self.home, ['acme-main'], skip_packs=[pack['id']], confirmed=True)
        self.assertEqual(result['packs'], [])
        self.assertEqual(sorted(result['published']), ['acme-base', 'acme-main'])
        self.assertEqual(set(self.shared()), {'acme-main', 'acme-base'})
        catalog = team._catalog({'url': self.url, 'key': self.first['token']})
        self.assertEqual(catalog.get('packs', []), [])
        with self.assertRaisesRegex(ValueError, '仍需要'):
            team.unpublish(self.home, ['acme-base'])
        self.assertEqual(team.unpublish(self.home, ['acme-main', 'acme-base'])['names'], ['acme-base', 'acme-main'])
        self.assertEqual(set(self.shared()), {'acme-main', 'acme-base'})


class LocalInstallTests(unittest.TestCase):
    setUp, skill, fingerprint = test_operations.OperationsTests.setUp, test_operations.OperationsTests.skill, test_operations.OperationsTests.fingerprint

    def test_preview_install_assign_and_undo_from_a_folder_with_spaces(self):
        folder = self.skill('My drafts/team handoff', 'team-handoff')
        (folder / 'notes.txt').write_text('资料\n')
        before = self.fingerprint()
        review = operations.inspect_local(folder, self.home)
        preview = operations.install(review, ['team-handoff'], ['claude'], home=self.home, confirmed=False)
        self.assertTrue(preview['needs_confirmation'])
        self.assertEqual(preview['skills'][0]['name'], 'team-handoff')
        self.assertEqual(self.fingerprint(), before)
        result = operations.install(review, ['team-handoff'], ['claude'], home=self.home)
        skill = operations.load(self.home)[1]['skills']['team-handoff']
        self.assertEqual(skill['assigned_agents'], ['claude'])
        self.assertEqual(normalize(skill['source'])['skip_reason'], 'self_made')
        self.assertEqual((self.home / '.claude/skills/team-handoff/notes.txt').read_text(), '资料\n')
        self.assertTrue(folder.is_dir())
        operations.undo(result['id'], home=self.home)
        self.assertEqual(self.fingerprint(), before)

    def test_folder_with_several_skills_and_conflicts(self):
        folder = self.home / 'Shared skills'
        self.skill('Shared skills/first one', 'first-one')
        self.skill('Shared skills/second', 'second-one')
        review = operations.inspect_local(folder, self.home)
        self.assertEqual(sorted(s['name'] for s in review['skills']), ['first-one', 'second-one'])
        clash = self.skill('Elsewhere/other', 'other', '不同的正文')
        result = operations.install(operations.inspect_local(clash, self.home), ['other'], home=self.home, confirmed=False)
        self.assertEqual(result['conflicts'][0]['name'], 'other')

    def test_agent_roots_store_and_folders_without_skills_are_refused(self):
        self.skill('.claude/skills/fresh', 'fresh')
        (self.home / 'Empty folder').mkdir()
        for folder, message in ((self.home / '.claude/skills/fresh', '收编'), (self.home / '.codex/skills/other', '统一仓库'),
                                (self.home / '.skill-pilot/store/other', '统一仓库'), (self.home / 'Empty folder', 'SKILL.md'),
                                (self.home / 'Missing', '找不到')):
            with self.assertRaisesRegex(ValueError, message):
                operations.inspect_local(folder, self.home)

    def test_command_line_preview_is_read_only(self):
        folder = self.skill('Drafts/new skill', 'new-skill')
        before = self.fingerprint()
        result = run(self.home, 'install', str(folder), '--json')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['agents'], [])
        self.assertEqual(self.fingerprint(), before)


class BranchSourceTests(unittest.TestCase):
    def test_old_records_are_unchanged_and_branch_names_are_checked(self):
        old = {'source': 'https://github.com/acme/skills', 'source_path': 'skills/demo'}
        self.assertEqual(normalize(old), {'kind': 'github', 'repository': 'acme/skills', 'path': 'skills/demo',
                                          'label': 'acme/skills', 'url': 'https://github.com/acme/skills'})
        branched = normalize({**old, 'branch': 'team/stable'})
        self.assertEqual((branched['branch'], branched['label']), ('team/stable', 'acme/skills（team/stable 分支）'))
        for name in ('-x', '../main', 'a..b', 'main/', 'a//b', 'x.lock', '', 'a b', 7):
            with self.assertRaisesRegex(ValueError, '分支'):
                normalize({**old, 'branch': name})
        self.assertEqual(Reader.head({'repository': 'acme/skills'}), 'repos/acme/skills/commits?per_page=1')
        self.assertEqual(Reader.head({'repository': 'acme/skills', 'branch': 'team/stable'}),
                         'repos/acme/skills/commits?per_page=1&sha=team%2Fstable')

    def test_set_source_records_the_branch_and_the_reader_receives_it(self):
        fixture = test_operations.OperationsTests(methodName='test_manual_dependency_auto_assign_and_undo')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        home = fixture.home
        calls = []
        def inspect(reader, source, local):
            calls.append(source)
            return {'_base_path': str(local), 'base_commit': 'a' * 40, 'base_tree_sha': 'b' * 40,
                    'similarity': 1.0, 'local_modified': False}
        with patch.object(Reader, 'inspect', inspect):
            with self.assertRaisesRegex(ValueError, '分支'):
                operations.set_source('other', 'acme/skills', 'other', True, home, branch='../x')
            operations.set_source('other', 'acme/skills', 'other', True, home, branch='stable')
        self.assertEqual(calls[-1]['branch'], 'stable')
        self.assertEqual(operations.load(home)[1]['skills']['other']['source']['branch'], 'stable')
        with patch.object(Reader, 'inspect', inspect):
            operations.set_source('other', 'acme/skills', 'other', True, home)
        self.assertNotIn('branch', calls[-1])
        self.assertNotIn('branch', operations.load(home)[1]['skills']['other']['source'])


class BranchInstallTests(unittest.TestCase):
    setUp, skill, commit, inspect = (test_github_inspect.GitHubInspectTests.setUp, test_github_inspect.GitHubInspectTests.skill,
                                     test_github_inspect.GitHubInspectTests.commit, test_github_inspect.GitHubInspectTests.inspect)

    def test_tree_links_record_only_a_non_default_branch(self):
        self.skill('skills/demo')
        self.commit()
        github.git(self.repo, 'checkout', '-q', '-b', 'team/stable')
        self.skill('skills/demo').joinpath('SKILL.md').write_text('---\nname: demo\ndescription: 分支版\n---\n分支正文\n')
        self.commit()
        github.git(self.repo, 'checkout', '-q', 'main')
        on_branch, = self.inspect('team/stable/skills/demo')['skills']
        self.assertEqual(on_branch['source']['branch'], 'team/stable')
        self.assertEqual(on_branch['description'], '分支版')
        on_default, = self.inspect('main/skills/demo')['skills']
        self.assertNotIn('branch', on_default['source'])
        github.git(self.repo, 'tag', 'v1')
        tagged, = self.inspect('v1/skills/demo')['skills']
        self.assertNotIn('branch', tagged['source'])


class BranchUpdateTests(unittest.TestCase):
    setUp, commit = test_updates.UpdateTests.setUp, test_updates.UpdateTests.commit
    check, local, fingerprint = test_updates.UpdateTests.check, test_updates.UpdateTests.local, test_updates.UpdateTests.fingerprint

    def test_updates_follow_the_recorded_branch_and_keep_it(self):
        default = github.git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD')
        github.git(self.repo, 'branch', 'stable', self.old)
        path = self.home / '.skill-pilot/registry.json'
        registry = json.loads(path.read_text())
        registry['skills']['sample']['source']['branch'] = 'stable'
        path.write_text(json.dumps(registry))
        operations._commit(self.home, registry, ['sample'], [], 'fixture branch')
        self.assertFalse(self.check()['skills'][0]['has_update'])
        github.git(self.repo, 'checkout', '-q', 'stable')
        self.manifest.write_text(self.original.replace('first', 'stable fix'))
        self.commit('stable')
        github.git(self.repo, 'checkout', '-q', default)
        review = self.check()
        self.assertTrue(review['skills'][0]['has_update'])
        updates.apply(review, ['sample'], confirmed=True)
        text = (self.local() / 'SKILL.md').read_text()
        self.assertIn('stable fix', text)
        self.assertNotIn('new upstream', text)
        self.assertEqual(operations.load(self.home)[1]['skills']['sample']['source']['branch'], 'stable')


if __name__ == '__main__':
    unittest.main()
