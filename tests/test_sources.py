import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_pilot.sources import github
from skill_pilot.store import git_tree


class SourceTests(unittest.TestCase):
    def test_url_scope_and_slash_refs(self):
        self.assertEqual(github.parse_url('https://github.com/owner/repo/tree/feature/x/skills/demo'),
                         ('https://github.com/owner/repo.git','owner/repo','feature/x/skills/demo'))
        for url in ('https://evil.example/repo','https://token@github.com/o/r','https://github.com/o/r/tree/main/../secret','https://github.com/o/r/blob/main/SKILL.md'):
            with self.assertRaises(ValueError):
                github.parse_url(url)

    def test_historical_full_tree_and_no_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)
            (path/'SKILL.md').write_text('---\nname: demo\ndescription: test\n---\nbody\n')
            expected=git_tree(path)
            def api(endpoint):
                if '/commits?' in endpoint:
                    return [{'sha':'new'},{'sha':'old'}]
                if '/git/commits/' in endpoint:
                    return {'tree':{'sha':endpoint.rsplit('/',1)[-1]}}
                if endpoint.endswith('/new') or endpoint.endswith('/old'):
                    return {'tree':[{'path':'skills','type':'tree','sha':endpoint.rsplit('/',1)[-1]+'-skills'}]}
                return {'tree':[{'path':'demo','type':'tree','sha':expected if endpoint.endswith('/old-skills') else 'different'}]}
            with patch.object(github,'search_code',return_value=[{'repository':{'full_name':'owner/repo'},'path':'skills/demo/SKILL.md'}]),patch.object(github,'api',side_effect=api):
                result=github.verify_source(path,'demo')
            self.assertEqual(result['commit'],'old')
            self.assertEqual(result['tree_sha'],expected)
            with patch.object(github,'search_code',return_value=[]):
                self.assertEqual(github.verify_source(path,'demo')['source'],'自制')

    def test_incomplete_upstream_is_not_misclassified(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)
            (path/'SKILL.md').write_text('name')
            with patch.object(github,'search_code',return_value=[{'repository':{'full_name':'o/r'},'path':'skills/demo/SKILL.md'}]),patch.object(github,'api',side_effect=[[{'sha':'new'}],{'tree':{'sha':'root'}},{'truncated':True}]):
                with self.assertRaisesRegex(ValueError,'没有读完整'):
                    github.verify_source(path,'demo')

    def test_external_link_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)
            (path/'outside').symlink_to('/etc/hosts')
            with self.assertRaisesRegex(ValueError,'文件夹外'):
                github.safe_skill(path)
