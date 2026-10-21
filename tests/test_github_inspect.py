import os
from skill_pilot.platform_support import rmtree
import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from skill_pilot.scan import content_digest
from skill_pilot.sources import github


class GitHubInspectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill-pilot-inspect-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        github.git(self.repo, 'init', '-q', '--initial-branch=main')

    def skill(self, folder, name='demo'):
        path = self.repo / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 示例技能\n---\n正文\n')
        (path / 'reference.txt').write_text('随技能一起读取')
        return path

    def commit(self):
        github.git(self.repo, 'add', '.')
        github.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@local',
                   '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        return github.git(self.repo, 'rev-parse', 'HEAD')

    def inspect(self, suffix=None):
        url = self.repo.as_uri() if suffix is None else (
            'https://github.com/fixture/repo/tree/' + urllib.parse.quote(suffix, safe='/'))
        real_git = github.git

        def local_git(directory, *args, **kwargs):
            args = [self.repo.as_uri() if a == 'https://github.com/fixture/repo.git' else a for a in args]
            return real_git(directory, *args, **kwargs)

        with patch.object(github, 'git', side_effect=local_git):
            review = github.inspect(url)
        self.addCleanup(shutil.rmtree, review['temporary'], True)
        return review

    def test_only_skill_folder_is_checked_out_and_metadata_matches_full_checkout(self):
        skill = self.skill('plugin/skills/demo')
        unrelated = self.repo / 'videos'
        unrelated.mkdir()
        (unrelated / 'large.bin').write_bytes(b'x' * (2 * 1024 * 1024))
        commit = self.commit()
        review = self.inspect()
        row, = review['skills']
        self.assertFalse((Path(review['temporary']) / 'repo/videos/large.bin').exists())
        self.assertEqual(row['digest'], content_digest(skill))
        self.assertEqual(row['source']['tree_sha'], github.git(self.repo, 'rev-parse', 'HEAD:plugin/skills/demo'))
        self.assertEqual(row['source']['commit'], commit)
        self.assertEqual((row['repository_path'], row['description']), ('plugin/skills/demo', '示例技能'))
        self.assertEqual((Path(row['path']) / 'reference.txt').read_text(), '随技能一起读取')

    def test_tree_subpath_and_branch_with_slash(self):
        self.skill('skills/demo')
        self.skill('skills/other', 'other')
        self.commit()
        github.git(self.repo, 'branch', 'feature/install')
        row, = self.inspect('feature/install/skills/demo')['skills']
        self.assertEqual((row['name'], row['repository_path']), ('demo', 'skills/demo'))

    def test_root_skill_keeps_full_checkout(self):
        self.skill('.')
        (self.repo / 'assets').mkdir()
        (self.repo / 'assets/data.txt').write_text('技能资料')
        self.commit()
        row, = self.inspect()['skills']
        self.assertEqual(row['repository_path'], '.')
        self.assertEqual(row['digest'], content_digest(self.repo))
        self.assertEqual(row['source']['tree_sha'], github.git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        self.assertTrue((Path(row['path']) / 'assets/data.txt').is_file())

    def test_requested_commit_is_used_instead_of_branch_tip(self):
        skill = self.skill('skills/demo')
        old_digest = content_digest(skill)
        commit = self.commit()
        (skill / 'reference.txt').write_text('新版资料')
        self.commit()
        row, = self.inspect(commit + '/skills/demo')['skills']
        self.assertEqual(row['source']['commit'], commit)
        self.assertEqual(row['digest'], old_digest)
        self.assertEqual(row['source']['tree_sha'], github.git(self.repo, 'rev-parse', commit + ':skills/demo'))

    def test_tag_is_used(self):
        skill = self.skill('skills/demo')
        commit = self.commit()
        github.git(self.repo, 'tag', 'v1')
        (skill / 'reference.txt').write_text('新版资料')
        self.commit()
        row, = self.inspect('v1/skills/demo')['skills']
        self.assertEqual(row['source']['commit'], commit)

    def test_no_skills_returns_empty_list(self):
        (self.repo / 'README.md').write_text('没有技能')
        (self.repo / 'NOT_SKILL.md').write_text('不是技能说明')
        self.commit()
        self.assertEqual(self.inspect()['skills'], [])

    def test_multiple_skills_keep_their_complete_folders(self):
        self.skill('skills/demo')
        self.skill('plugin/skills/other', 'other')
        commit = self.commit()
        rows = self.inspect()['skills']
        self.assertEqual({r['name'] for r in rows}, {'demo', 'other'})
        for row in rows:
            self.assertEqual(row['digest'], content_digest(self.repo / row['repository_path']))
            self.assertEqual(row['source']['commit'], commit)

    def test_folder_names_with_spaces_leading_dash_or_quotes(self):
        folders = ('-skill folder', ' leading space', '中文 folder') if os.name == 'nt' else ('-skill folder', ' leading space', 'trailing space ', '"quoted folder"', '中文 folder')
        for i, folder in enumerate(folders):
            self.skill(folder, f'skill-{i}')
        self.commit()
        rows = self.inspect()['skills']
        self.assertEqual(len(rows), len(folders))
        for row in rows:
            self.assertEqual(row['digest'], content_digest(self.repo / row['repository_path']))

    def test_missing_subpath_keeps_the_existing_error(self):
        self.skill('skills/demo')
        self.commit()
        with self.assertRaisesRegex(ValueError, '没有找到指定的文件夹'):
            self.inspect('main/missing')

    def test_duplicate_names_and_external_links_are_still_rejected(self):
        skill = self.skill('skills/demo')
        self.skill('skills/duplicate')
        self.commit()
        with self.assertRaisesRegex(ValueError, '多个同名技能'):
            self.inspect()
        rmtree(self.repo / 'skills/duplicate')
        (skill / 'outside').symlink_to('../../README.md')
        (self.repo / 'README.md').write_text('技能文件夹之外')
        self.commit()
        with self.assertRaisesRegex(ValueError, '文件夹外'):
            self.inspect()
