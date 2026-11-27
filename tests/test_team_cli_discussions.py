"""Command-line access to team discussions, shared doc reading and the deliverable list."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from skill_pilot import team
from tests import test_team_docs


class TeamDiscussionCliTests(unittest.TestCase):
    Base = test_team_docs.DocClientWebCliTests
    setUp, tearDown = Base.setUp, Base.tearDown

    def cli(self, home, *args, status=0, as_json=True, read_only=False):
        env = {**os.environ, 'SKILL_PILOT_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('SKILL_PILOT_READ_ONLY', None)
        if read_only:
            env['SKILL_PILOT_READ_ONLY'] = '1'
        result = subprocess.run([sys.executable, '-m', 'skill_pilot', 'team', *args, *(['--json'] if as_json else [])],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, env=env,
                                timeout=30)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        return json.loads(result.stdout) if as_json else result.stdout

    def seed(self):
        self.assertTrue(team.publish(self.alice_home, ['demo-skill'], confirmed=True)['listings'])
        team.publish_doc(self.alice_home, identity='doc-1', title='分享纪要', category='Workshop 纪要',
                         content='# 分享纪要\n\n第一部分正文\n', confirmed=True)
        folder = Path(self.temp.name) / 'case folder'
        folder.mkdir()
        (folder / 'index.html').write_text('<!doctype html><p>demo</p>', encoding='utf-8')
        team.publish_deliverable(self.alice_home, folder, 'case-1', '案例一', 'v1', '成品摘要', confirmed=True)

    def test_comment_read_reply_delete_and_overview(self):
        self.seed()
        bob, alice = self.bob_home, self.alice_home
        self.assertEqual(self.cli(bob, 'comments')['targets'], [])
        self.assertIn('还没有留言', self.cli(bob, 'comments', as_json=False))

        # Posting previews first and writes nothing without --yes.
        preview = self.cli(bob, 'comment', 'skill', 'demo-skill', '很好用', status=2)
        self.assertTrue(preview['needs_confirmation'])
        self.assertIn('技能「demo-skill」', preview['message'])
        self.assertEqual(self.cli(bob, 'comments', 'skill', 'demo-skill')['comments'], [])
        first = self.cli(bob, 'comment', 'skill', 'demo-skill', '很好用', '--yes')['comment']

        # Docs resolve by id or title; replies are checked against the thread before posting.
        question = self.cli(bob, 'comment', 'doc', 'doc-1', '第二部分怎么跑？\n能举个例子吗', '--yes')['comment']
        self.assertIn('请先填写留言内容', self.cli(bob, 'comment', 'doc', 'doc-1', '   ', '--yes', status=1)['error'])
        missing = self.cli(alice, 'comment', 'doc', 'doc-1', '回复', '--reply-to', first['id'], '--yes', status=1)
        self.assertIn('没有编号为', missing['error'])
        reply_preview = self.cli(alice, 'comment', 'doc', '分享纪要', '见附件', '--reply-to', question['id'],
                                 status=2, as_json=False)
        self.assertIn('回复 Bob 的留言', reply_preview)
        reply = self.cli(alice, 'comment', 'doc', '分享纪要', '见附件', '--reply-to', question['id'], '--yes')['comment']
        self.assertEqual(reply['reply_to_id'], question['id'])

        overview = self.cli(alice, 'comments')['targets']
        self.assertEqual([(t['target_type'], t['name'], t['count']) for t in overview],
                         [('doc', 'doc-1', 2), ('skill', 'demo-skill', 1)])
        text = self.cli(alice, 'comments', as_json=False)
        self.assertIn('skill-pilot team comments doc doc-1', text)

        thread = self.cli(alice, 'comments', 'doc', 'doc-1', as_json=False)
        for expected in ('「分享纪要」的讨论，共 2 条', '  能举个例子吗', '回复 Bob：第二部分怎么跑？', question['id']):
            self.assertIn(expected, thread)
        self.assertIn('还没有留言', self.cli(bob, 'comments', 'deliverable', 'case-1', as_json=False))
        self.assertIn('没有找到', self.cli(bob, 'comments', 'doc', 'missing', status=1)['error'])
        self.assertEqual(self.cli(bob, 'comments', 'doc', status=2, as_json=False), '')

        # Deleting previews, honours read-only mode, then removes the comment.
        self.assertTrue(self.cli(bob, 'comment-delete', question['id'], status=2)['needs_confirmation'])
        self.cli(bob, 'comment-delete', question['id'], '--yes', status=1, read_only=True)
        self.cli(bob, 'comment', 'skill', 'demo-skill', '再来一条', '--yes', status=1, read_only=True)
        self.assertEqual(self.cli(bob, 'comment-delete', question['id'], '--yes')['count'], 1)
        self.assertTrue(self.cli(bob, 'comments', 'doc', 'doc-1')['comments'][0]['reply_to_deleted'])
        self.assertIn('  回复的留言已删除\n  见附件', self.cli(bob, 'comments', 'doc', 'doc-1', as_json=False))
        self.assertIn('留言编号不正确', self.cli(bob, 'comment-delete', 'abc', status=1)['error'])

    def test_doc_show_and_deliverable_list(self):
        self.seed()
        shown = self.cli(self.bob_home, 'doc-show', 'doc-1')
        self.assertEqual((shown['title'], shown['category'], shown['member']['name']), ('分享纪要', 'Workshop 纪要', 'Alice'))
        text = self.cli(self.bob_home, 'doc-show', '分享纪要', as_json=False)
        for expected in ('[Workshop 纪要] 分享纪要（编号：doc-1，Alice 发布', '第一部分正文',
                         'skill-pilot team comments doc doc-1'):
            self.assertIn(expected, text)
        self.assertIn('没有找到', self.cli(self.bob_home, 'doc-show', 'doc-1', '--author', 'Bob', status=1)['error'])

        listed = self.cli(self.bob_home, 'deliverable-list')['deliverables']
        self.assertEqual([(r['id'], r['title'], r['mine']) for r in listed], [('case-1', '案例一', False)])
        self.assertTrue(self.cli(self.alice_home, 'deliverable-list')['deliverables'][0]['mine'])
        self.assertIn('案例一 v1（编号：case-1，Alice 上架', self.cli(self.bob_home, 'deliverable-list', as_json=False))


if __name__ == '__main__':
    unittest.main()
