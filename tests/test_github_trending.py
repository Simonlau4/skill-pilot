"""GitHub discovery uses synthetic responses and temporary data folders only."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from skill_pilot import team_hub
from tests import set_up_home


STAMP = '2026-10-29T12:00:00+00:00'


def github_catalog(*repositories):
    return {'fetched_at': STAMP, 'repos': [
        {'repository': repository, 'description': 'Acme example skills', 'stars': 100 - index,
         'created_at': '2026-10-20T00:00:00Z', 'pushed_at': STAMP, 'skill_count': 2}
        for index, repository in enumerate(repositories)]}


def repository(name, stars=1, **extra):
    return {'name': name, 'full_name': 'acme/' + name, 'topics': ['agent-skills'],
            'description': None, 'stargazers_count': stars, 'default_branch': 'main',
            'created_at': '2026-10-20T00:00:00Z', 'pushed_at': STAMP, **extra}


def tree(*paths, truncated=False):
    return {'truncated': truncated, 'tree': [{'type': 'blob', 'path': path} for path in paths]}


class GitHubTrendingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot github ')
        self.addCleanup(temporary.cleanup)
        set_up_home(self, Path(temporary.name) / 'Share data')
        self.store = team_hub.HubStore(self.home)
        self.path = self.home / 'github-trending.json'

    def save(self, data):
        with self.store.locked():
            team_hub.atomic_write(self.path, data)

    def search(self, items, trees):
        def read(path, limit):
            if path.startswith('/search/repositories?'):
                self.assertEqual(limit, team_hub.GITHUB_SEARCH_LIMIT)
                return {'items': items}
            self.assertEqual(limit, team_hub.GITHUB_TREE_LIMIT)
            name = path.split('/')[3]
            result = trees.get(name, tree('SKILL.md'))
            if isinstance(result, Exception):
                raise result
            return result
        return read

    def test_whitelist_strips_extra_fields_and_cleans_description(self):
        data = github_catalog('acme/example-skills')
        data['token'] = 'discard'
        data['repos'][0].update(description='A\n\x00\u202eB' + 'x' * 600,
                                html_url='https://example.invalid', local={'status': 'installed'})
        clean = team_hub.clean_github(data)
        self.assertEqual(set(clean), {'fetched_at', 'repos'})
        self.assertEqual(set(clean['repos'][0]), {'repository', 'description', 'stars', 'created_at',
                                                'pushed_at', 'skill_count'})
        self.assertEqual(clean['repos'][0]['description'], ('AB' + 'x' * 600)[:500])
        data['repos'][0]['description'] = None
        self.assertEqual(team_hub.clean_github(data)['repos'][0]['description'], '')

    def test_whitelist_rejects_invalid_shapes_names_numbers_and_times(self):
        original = github_catalog('acme/example-skills')
        invalid = [None, [], {}, {**original, 'repos': {}}, {**original, 'repos': [None]},
                   {**original, 'repos': original['repos'] * 21}]
        for field, values in {
            'repository': ('https://github.com/acme/skills', 'acme/../skills', 'acme/.', 'acme/..',
                           '-acme/skills', 'acme-/skills', 'ac--me/skills', 'ac_me/skills',
                           'a' * 40 + '/skills', 'acme/' + 's' * 101, 'acme/skills?x', 1),
            'stars': (-1, True, 1.0, '1', None), 'skill_count': (0, -1, True, 1.0, '2'),
            'description': ([], 1), 'created_at': ('invalid', '', '2026-10-29', None),
            'pushed_at': ('2026-10-29T00:00:00', '2026-13-29T00:00:00Z', '2026-10-29\n00:00:00Z')
        }.items():
            for value in values:
                data = copy.deepcopy(original)
                data['repos'][0][field] = value
                invalid.append(data)
        invalid.append({**original, 'fetched_at': 'invalid'})
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                team_hub.clean_github(data)
        for name in ('a/s', 'a-b/skills._-', 'a' * 39 + '/' + 's' * 100):
            self.assertEqual(team_hub.clean_github(github_catalog(name))['repos'][0]['repository'], name)
        self.assertIsNone(team_hub.clean_github(github_catalog('acme/skills'))['repos'][0].get('url'))

    def test_query_filters_counts_and_keeps_star_order_after_a_single_failure(self):
        items = [repository('lower', 5), repository('Awesome-list', 100),
                 repository('topic-list', 90, topics=['agent-skills', 'AWESOME-tools']),
                 repository('forked', 80, fork=True), repository('archived', 70, archived=True),
                 repository('empty', 60), repository('broken', 50),
                 repository('invalid', 40, stargazers_count=True),
                 repository('higher', 10, default_branch='feature/skills')]
        trees = {'empty': tree('README.md', 'notSKILL.md', 'example/SKILL.md.bak'),
                 'broken': ValueError('暂时无法读取。'),
                 'higher': tree('SKILL.md', 'skills/example/SKILL.md', 'skill.md')}
        with patch.object(team_hub, 'now', return_value=STAMP), patch.object(
                team_hub, '_github_json', side_effect=self.search(items, trees)) as request:
            result = team_hub.fetch_github_trending()
        self.assertEqual([r['repository'] for r in result['repos']], ['acme/higher', 'acme/lower'])
        self.assertEqual([r['skill_count'] for r in result['repos']], [2, 1])
        self.assertEqual(result['repos'][0]['description'], '')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args_list[0].args[0]).query)
        self.assertEqual(query, {'q': ['topic:agent-skills created:>2026-09-29 fork:false archived:false'],
                                 'sort': ['stars'], 'order': ['desc'], 'per_page': ['50']})
        self.assertEqual(request.call_count, 5)
        self.assertTrue(any('/git/trees/feature%2Fskills?recursive=1' in c.args[0] for c in request.call_args_list))

    def test_locations_choose_root_readme_by_priority_and_limit_sorted_skills(self):
        paths = ['docs/README.md', 'README.txt', 'ReadMe.MARKDOWN', 'readme.MD', 'README', 'README.rst']
        paths += [f'skills/skill-{i:02d}/SKILL.md' for i in reversed(range(35))]
        with patch.object(team_hub, '_github_json', side_effect=self.search(
                [repository('example', default_branch='release/v1')], {'example': tree(*paths)})):
            row = team_hub.fetch_github_trending()['repos'][0]
        self.assertEqual(row['branch'], 'release/v1')
        self.assertEqual(row['readme_path'], 'readme.MD')
        self.assertEqual(row['skill_paths'], sorted(p for p in paths if p.endswith('/SKILL.md'))[:30])
        self.assertEqual(row['skill_count'], 35)
        self.assertEqual(team_hub.clean_github({'fetched_at': STAMP, 'repos': [row]})['repos'], [row])

    def test_locations_validate_new_fields_and_keep_legacy_rows(self):
        old = github_catalog('acme/example')['repos'][0]
        self.assertEqual(team_hub._github_repo(old), old)
        new = {**old, 'branch': 'main', 'readme_path': 'README.md', 'skill_paths': ['skills/example/SKILL.md']}
        self.assertEqual(team_hub._github_repo(new), new)
        for field, values in {
            'branch': (None, '', '/main', 'main/', 'a..b', 'x?y', 'x#y', 'x%2Fy', 'a' * 101, 'x\ny'),
            'readme_path': ('/README.md', '../README.md', './README.md', 'docs/README.md', 'README.md\x00'),
            'skill_paths': (None, 'SKILL.md', ['../SKILL.md'], ['/SKILL.md'], ['./SKILL.md'],
                            ['skills//SKILL.md'], ['skills/../SKILL.md'], ['skills\\SKILL.md'],
                            ['skills/\x00/SKILL.md'], ['a' * 300 + '/SKILL.md'], ['skill.md'],
                            ['SKILL.md'] * 2, [f'{i}/SKILL.md' for i in range(31)])
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    team_hub._github_repo({**new, field: value})
        for field in ('branch', 'readme_path', 'skill_paths'):
            with self.subTest(missing=field), self.assertRaises(ValueError):
                team_hub._github_repo({k: v for k, v in new.items() if k != field})
        # Quoted URL segments can safely represent spaces and Unicode filenames.
        self.assertEqual(team_hub.github_path('技能 folder/SKILL.md'), '技能 folder/SKILL.md')

    def test_search_discards_malicious_branch_and_tree_paths(self):
        items = [repository('bad-branch', default_branch='../main'), repository('bad-path'), repository('good')]
        with patch.object(team_hub, '_github_json', side_effect=self.search(
                items, {'bad-path': tree('../SKILL.md')})):
            self.assertEqual([r['repository'] for r in team_hub.fetch_github_trending()['repos']], ['acme/good'])

    def test_stops_at_twenty_repositories(self):
        items = [repository('skills-' + str(i), 100 - i) for i in range(50)]
        with patch.object(team_hub, '_github_json', side_effect=self.search(items, {})) as request:
            result = team_hub.fetch_github_trending()
        self.assertEqual(len(result['repos']), 20)
        self.assertEqual(request.call_count, 21)
        self.assertEqual(result['repos'][-1]['repository'], 'acme/skills-19')

    def test_reads_no_more_than_forty_trees(self):
        items = [repository('skills-' + str(i), 100 - i) for i in range(50)]
        trees = {item['name']: tree('README.md') for item in items}
        with patch.object(team_hub, '_github_json', side_effect=self.search(items, trees)) as request:
            self.assertEqual(team_hub.fetch_github_trending()['repos'], [])
        self.assertEqual(request.call_count, 41)

    def test_truncated_or_oversized_trees_keep_unknown_counts(self):
        items = [repository('truncated', 3), repository('large', 2), repository('malformed', 1)]
        trees = {'truncated': tree('README.md', 'SKILL.md', truncated=True),
                 'large': team_hub._GitHubResponseTooLarge('内容太大。'),
                 'malformed': {'truncated': False, 'tree': [None]}}
        with patch.object(team_hub, '_github_json', side_effect=self.search(items, trees)):
            result = team_hub.fetch_github_trending()
        self.assertEqual([r['repository'] for r in result['repos']], ['acme/truncated', 'acme/large'])
        self.assertEqual([r['skill_count'] for r in result['repos']], [None, None])
        self.assertTrue(all(r['readme_path'] is None and r['skill_paths'] == [] for r in result['repos']))

    def test_malformed_search_preserves_the_previous_file_and_logs_once(self):
        previous = github_catalog('acme/previous')
        self.save(previous)
        before = self.path.read_bytes()
        for response in (None, [], {}, {'items': {}}, {'items': None}):
            with self.subTest(response=response), patch.object(team_hub, 'now', return_value='2026-10-30T12:00:00Z'), \
                    patch.object(team_hub, '_github_json', return_value=response), redirect_stderr(io.StringIO()) as output:
                self.store.refresh_github()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertIn('格式不正确', output.getvalue())

    def test_search_failure_keeps_old_file_and_empty_cache_stays_absent(self):
        for previous in (None, github_catalog('acme/previous')):
            if previous:
                self.save(previous)
            with self.subTest(previous=bool(previous)), patch.object(team_hub, 'now', return_value='2026-10-30T12:00:00Z'), \
                    patch.object(team_hub, '_github_json', side_effect=ValueError('暂时连不上 GitHub 公开接口。')), \
                    redirect_stderr(io.StringIO()) as output:
                self.store.refresh_github()
            self.assertEqual(self.store._github(), previous)
            self.assertEqual(self.path.exists(), bool(previous))
            self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_fresh_cache_is_not_refetched_and_exactly_twenty_four_hours_is_due(self):
        previous = github_catalog('acme/previous')
        self.save(previous)
        with patch.object(team_hub, 'now', return_value='2026-10-30T11:59:59Z'), \
                patch.object(team_hub, 'fetch_github_trending') as fetch:
            self.store.refresh_github()
        fetch.assert_not_called()
        updated = github_catalog('acme/updated') | {'fetched_at': '2026-10-30T12:00:00Z'}
        with patch.object(team_hub, 'now', return_value=updated['fetched_at']), \
                patch.object(team_hub, 'fetch_github_trending', return_value=updated) as fetch:
            self.store.refresh_github()
        fetch.assert_called_once_with()
        self.assertEqual(self.store._github(), updated)

    def test_missing_or_corrupt_cache_is_fetched_immediately(self):
        for content in (None, '{', '{"fetched_at": "invalid", "repos": []}'):
            if content is not None:
                self.path.write_text(content)
            with self.subTest(content=content), patch.object(team_hub, 'fetch_github_trending',
                    return_value=github_catalog('acme/updated')) as fetch:
                self.store.refresh_github()
            fetch.assert_called_once_with()
            self.assertEqual(self.store._github(), github_catalog('acme/updated'))

    def test_failed_refresh_retries_at_the_next_hour(self):
        stopped = Mock()
        stopped.is_set.side_effect = [False, False, True]
        with patch.object(team_hub, 'fetch_github_trending', side_effect=[
                ValueError('暂时连不上 GitHub 公开接口。'), github_catalog('acme/updated')]) as fetch, \
                redirect_stderr(io.StringIO()) as output:
            team_hub.github_worker(self.store, stopped)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([c.args for c in stopped.wait.call_args_list], [(3600,), (3600,)])
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(self.store._github(), github_catalog('acme/updated'))

    def test_all_failed_trees_preserve_previous_results(self):
        self.save(github_catalog('acme/previous'))
        read = self.search([repository('unavailable')], {'unavailable': ValueError('读取失败。')})
        with patch.object(team_hub, 'now', return_value='2026-10-30T12:00:00Z'), \
                patch.object(team_hub, '_github_json', side_effect=read), redirect_stderr(io.StringIO()):
            self.store.refresh_github()
        self.assertEqual(self.store._github(), github_catalog('acme/previous'))

    def test_failed_atomic_write_keeps_the_previous_file(self):
        self.save(github_catalog('acme/previous'))
        before = self.path.read_bytes()
        with patch.object(team_hub, 'now', return_value='2026-10-30T12:00:00Z'), \
                patch.object(team_hub, 'fetch_github_trending', return_value=github_catalog('acme/updated')), \
                patch.object(team_hub.os, 'replace', side_effect=OSError('写入失败。')), \
                redirect_stderr(io.StringIO()) as output:
            self.store.refresh_github()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.home.glob('.github-trending.json.*')), [])
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_http_request_has_public_headers_timeout_and_bounded_read(self):
        response = io.BytesIO(b'{"items": []}')
        with patch.object(team_hub.urllib.request, 'build_opener') as build, patch.object(
                response, 'read', wraps=response.read) as read:
            build.return_value.open.return_value = response
            self.assertEqual(team_hub._github_json('/search/repositories?q=test', 100), {'items': []})
            read.assert_called_once_with(101)
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/search/repositories?q=test')
        self.assertEqual(request.get_header('User-agent'), 'skill-pilot-team-hub')
        self.assertEqual(request.get_header('Accept'), 'application/vnd.github+json')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(build.return_value.open.call_args.kwargs, {'timeout': 15})
        self.assertIsInstance(build.call_args.args[0], team_hub._GitHubRedirect)

    def test_http_errors_and_invalid_or_oversized_responses_do_not_expose_bodies(self):
        bodies = [b'not JSON: private response', b'\xff', b'123456']
        for body in bodies:
            with self.subTest(body=body), patch.object(team_hub.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value = io.BytesIO(body)
                with self.assertRaises(ValueError) as error:
                    team_hub._github_json('/search/repositories', 5)
                self.assertNotIn('private response', str(error.exception))
        for failure in (urllib.error.HTTPError('https://api.github.com/test', 403, 'private response', {},
                                               io.BytesIO(b'private response')),
                        urllib.error.URLError('private response')):
            with self.subTest(failure=type(failure)), patch.object(team_hub.urllib.request, 'build_opener') as build:
                build.return_value.open.side_effect = failure
                with self.assertRaises(ValueError) as error:
                    team_hub._github_json('/search/repositories', 100)
                self.assertNotIn('private response', str(error.exception))
                self.assertEqual(build.return_value.open.call_count, 1)
        with patch.object(team_hub.urllib.request, 'build_opener') as build, \
                patch.object(team_hub.json, 'loads', side_effect=RecursionError('private response')):
            build.return_value.open.return_value = io.BytesIO(b'{}')
            with self.assertRaisesRegex(ValueError, '格式不正确'):
                team_hub._github_json('/search/repositories', 4000)

    def test_redirects_allow_only_the_github_https_api(self):
        handler = team_hub._GitHubRedirect()
        request = urllib.request.Request('https://api.github.com/search/repositories')
        for url in ('https://example.invalid/repos', 'http://api.github.com/repos',
                    'https://api.github.com.example.invalid/repos', 'https://api.github.com:443/repos',
                    'https://user@api.github.com/repos'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, 'Found', {}, url)
        redirected = handler.redirect_request(request, None, 302, 'Found', {}, 'https://api.github.com/repos/acme/skills')
        self.assertEqual(redirected.full_url, 'https://api.github.com/repos/acme/skills')

    def test_server_construction_does_not_start_discovery(self):
        with patch.object(team_hub.threading, 'Thread') as thread, \
                patch.object(team_hub, '_github_json') as request:
            with team_hub.HubServer(self.home, '127.0.0.1', 0):
                pass
        thread.assert_not_called()
        request.assert_not_called()
