"""Trending installs use synthetic downloads, isolated homes and loopback services."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tests import set_up_home
from tests.test_github_trending import github_catalog
from tests.test_team_hub import listing
from skill_pilot import github_digest, operations, store, tags, team, team_hub
from skill_pilot.scan import content_digest, scan
from skill_pilot.sources import github
from skill_pilot.sync import visibility
from skill_pilot.web.server import Dashboard


class GitHubQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skill pilot github queue ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        set_up_home(self, self.root / 'Acme user')
        curated = patch.object(operations, 'curated_dependencies', return_value=({}, {}))
        curated.start()
        self.addCleanup(curated.stop)
        for name in ('acme-same', 'acme-different'):
            self.skill(self.home / '.codex/skills' / name, name)
        store.apply(store.preview(self.home)['plan_id'], self.home)
        self.hub = self.start(team_hub.HubServer(self.root / 'Share data', '127.0.0.1', 0))
        self.member = self.hub.store.add_member('Acme 示例成员')
        self.config = {'url': f'http://127.0.0.1:{self.hub.server_port}', 'key': self.member['token']}
        self.server = self.start(Dashboard(scan(self.home)))
        self.ok('/api/team/settings', self.config)
        self.catalog('acme/skills', 'acme/second', 'acme/third')

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        def stop():
            server.shutdown()
            thread.join(5)
            server.server_close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return server

    def skill(self, path, name, body='示例正文'):
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Acme 示例技能\n---\n{body}\n')
        return path

    def catalog(self, *repositories):
        with self.hub.store.locked():
            team_hub.atomic_write(self.hub.store.data / 'github-trending.json', github_catalog(*repositories))

    def digest_title(self, title='示例技能包'):
        with self.server.github_digests.lock:
            self.server.github_digests.cache['acme/skills'] = {
                'digest': {'title': title, 'summary': '示例用途'}}

    def review(self, *names, repository='acme/skills', changed=()):
        temporary = Path(tempfile.mkdtemp(prefix='Download ', dir=self.root))
        rows = []
        for name in names:
            path = self.skill(temporary / name, name, '不同的示例正文' if name in changed else '示例正文')
            rows.append({'name': name, 'path': str(path), 'digest': content_digest(path),
                'description': 'Acme 示例技能', 'repository_path': 'skills/' + name,
                'source': {'source': 'https://github.com/' + repository + '.git', 'repository': repository,
                           'source_path': 'skills/' + name}})
        return {'id': temporary.name, 'temporary': str(temporary), 'skills': rows}

    def request(self, path, body=None, timeout=12):
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Origin': self.server.origin, 'X-Skill-Pilot-Token': self.server.token,
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def ok(self, path, body=None, **kw):
        status, result = self.request(path, body, **kw)
        self.assertEqual(status, 200, result)
        return result

    def enqueue(self, repository='acme/skills'):
        items = self.ok('/api/team/queue', {'github': repository})['items']
        return next(i for i in items if i.get('repository', '').lower() == repository.lower())

    def wait_jobs(self):
        deadline = time.monotonic() + (30 if os.name == 'nt' else 8)
        while time.monotonic() < deadline:
            items = self.ok('/api/jobs')['items']
            if all(i['status'] not in {'waiting', 'running'} for i in items) and not self.server.jobs_busy():
                return items
            time.sleep(.01)
        self.fail('任务没有按时完成：' + str(items))

    def test_enqueue_waiting_duplicates_titles_and_both_public_views(self):
        review = self.review('acme-new')
        self.digest_title()
        with patch.object(github, 'inspect', return_value=review) as download:
            with self.server.write_lock:
                first = self.enqueue()
                self.assertEqual(first['status'], 'waiting')
                self.assertEqual(first['key'], 'github:acme/skills')
                self.assertEqual(first['kind'], 'github_install')
                self.assertEqual(first['title'], '示例技能包')
                self.assertEqual(first['message'], '等待安装。')
                self.assertEqual([first[k] for k in ('installed', 'reuse', 'skipped', 'operation')], [[], [], [], None])
                self.assertNotIn('tag', first)
                duplicate = self.ok('/api/jobs', {'kind': 'github_install', 'repository': 'ACME/SKILLS', 'confirmed': True})
                self.assertTrue(duplicate['duplicate'])
                self.assertEqual(duplicate['job']['id'], first['id'])
                self.assertEqual(self.enqueue()['id'], first['id'])
                jobs = self.ok('/api/jobs')['items']
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]['summary'], '安装「示例技能包」')
                self.assertTrue(all(not k.startswith('_') for k in jobs[0]))
                download.assert_not_called()
            done = self.wait_jobs()[0]
        download.assert_called_once_with('https://github.com/acme/skills')
        self.assertEqual(done['status'], 'done', done)
        visible = self.ok('/api/team/queue')['items'][0]
        for field in ('id', 'key', 'repository', 'title', 'status', 'message', 'installed', 'reuse', 'skipped', 'operation'):
            self.assertEqual(visible[field], done[field])
        self.assertEqual(done['operations'], [visible['operation']])
        self.assertFalse(Path(review['temporary']).exists())

    def test_installs_new_reuses_identical_skips_different_tags_and_can_undo(self):
        review = self.review('acme-new', 'acme-extra', 'acme-same', 'acme-different', changed=['acme-different'])
        self.digest_title()
        before = {n: store.physical_state(self.home / '.skill-pilot/store' / n)
                  for n in ('acme-same', 'acme-different')}
        self.assertEqual(self.ok('/api/team/catalog')['github']['repos'][0]['local']['status'], 'installable')
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual(item['installed'], ['acme-new', 'acme-extra'])
        self.assertEqual(item['reuse'], ['acme-same'])
        self.assertEqual(item['skipped'], ['acme-different'])
        self.assertEqual(item['tag'], '示例技能包')
        self.assertEqual(self.ok('/api/team/queue')['items'][0]['tag'], '示例技能包')
        self.assertEqual(item['message'], '已安装 2 个技能。沿用 1 个技能：acme-same。'
                         '跳过 1 个同名但内容不同的技能：acme-different。可到技能页粘贴仓库链接核对。')
        registry = operations.load(self.home)[1]['skills']
        for name in item['installed']:
            self.assertEqual(registry[name]['assigned_agents'], [])
            self.assertEqual(registry[name]['entries'], [])
            self.assertEqual(registry[name]['source']['repository'], 'acme/skills')
            self.assertEqual(tags.read(self.home)[name], ['示例技能包'])
            self.assertNotIn(name, visibility(self.home)['codex']['available'])
        for name, state in before.items():
            self.assertEqual(store.physical_state(self.home / '.skill-pilot/store' / name), state)
            self.assertNotIn(name, tags.read(self.home))
        self.assertFalse(Path(review['temporary']).exists())
        self.assertEqual(self.ok('/api/team/catalog')['github']['repos'][0]['local']['status'], 'installed')
        self.ok('/api/undo', {'id': item['operation'], 'confirmed': True,
                             'revision': operations.table(self.home)['revision']})
        self.assertNotIn('acme-new', operations.load(self.home)[1]['skills'])
        self.assertEqual(self.ok('/api/team/catalog')['github']['repos'][0]['local']['status'], 'installable')

    def test_materials_only_cache_with_null_digest_uses_repository_title(self):
        snapshot = github_catalog('acme/skills')
        snapshot['repos'][0].update(branch='main', readme_path='README.md',
                                    skill_paths=['skills/acme-new/SKILL.md'])
        with self.hub.store.locked():
            team_hub.atomic_write(self.hub.store.data / 'github-trending.json', snapshot)
        self.ok('/api/team/catalog')
        materials = {'readme': 'Acme example', 'skills': [{'name': 'acme-new', 'description': '示例技能'}]}
        with patch.object(github_digest, 'fetch_materials', return_value=materials):
            detail = self.ok('/api/team/github/detail?repository=acme/skills')
        self.assertIsNone(detail['digest'])
        self.assertIsNone(self.server.github_digests.cache['acme/skills']['digest'])
        review = self.review('acme-new')
        with patch.object(github, 'inspect', return_value=review):
            queued = self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(queued['title'], 'acme/skills')
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual(item['summary'], '安装「acme/skills」')

    def test_without_chinese_title_tags_repository_name_and_returns_it_in_both_views(self):
        repository = 'acme-example/example-tools'
        self.catalog(repository)
        review = self.review('acme-new', 'acme-extra', repository=repository)
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue(repository)
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual(item['title'], repository)
        self.assertEqual(item['summary'], '安装「' + repository + '」')
        self.assertEqual(item['tag'], 'example-tools')
        self.assertEqual(self.ok('/api/team/queue')['items'][0]['tag'], 'example-tools')
        for name in item['installed']:
            self.assertEqual(tags.read(self.home)[name], ['example-tools'])

    def test_short_repository_name_uses_full_owner_and_name_as_tag(self):
        repository = 'acme/example-skills2'
        self.assertEqual(len(repository), 20)
        self.catalog(repository)
        review = self.review('acme-new', 'acme-extra', repository=repository)
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue(repository)
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual(item['title'], repository)
        self.assertEqual(item['summary'], '安装「' + repository + '」')
        self.assertEqual(item['tag'], repository)
        self.assertEqual(self.ok('/api/team/queue')['items'][0]['tag'], repository)
        for name in item['installed']:
            self.assertEqual(tags.read(self.home)[name], [repository])

    def test_short_repositories_with_same_name_have_separate_batch_tags(self):
        self.catalog('acme/skills', 'example/skills')
        reviews = {
            'https://github.com/acme/skills': self.review('acme-first', 'acme-second'),
            'https://github.com/example/skills': self.review('example-first', 'example-second', repository='example/skills')}
        with patch.object(github, 'inspect', side_effect=lambda url: reviews[url]):
            with self.server.write_lock:
                self.enqueue('acme/skills')
                self.enqueue('example/skills')
            items = self.wait_jobs()
        self.assertEqual([item['status'] for item in items], ['done', 'done'], items)
        self.assertEqual([item['tag'] for item in items], ['acme/skills', 'example/skills'])
        self.assertEqual([item['tag'] for item in self.ok('/api/team/queue')['items']],
                         ['acme/skills', 'example/skills'])
        saved = tags.read(self.home)
        for item in items:
            for name in item['installed']:
                self.assertEqual(saved[name], [item['repository']])
            matches = {name for name, values in saved.items() if item['tag'] in values}
            self.assertEqual(matches, set(item['installed']))

    def test_long_repository_name_is_truncated_for_tag_but_title_stays_complete(self):
        repository = 'acme/abcdefghijklmnopqrs-example-skills'
        self.catalog(repository)
        review = self.review('acme-new', 'acme-extra', repository=repository)
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue(repository)
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual(item['title'], repository)
        self.assertEqual(item['summary'], '安装「' + repository + '」')
        self.assertEqual(item['tag'], 'abcdefghijklmnopqrs-')
        self.assertEqual(len(item['tag']), 20)
        self.assertEqual(self.ok('/api/team/queue')['items'][0]['tag'], item['tag'])
        for name in item['installed']:
            self.assertEqual(tags.read(self.home)[name], [item['tag']])

    def test_tag_returns_saved_spelling_and_trims_title_whitespace(self):
        self.digest_title('  示例 Skills  ')
        tags.update(self.home, {'skills': ['acme-same'], 'add': '示例 skills'})
        review = self.review('acme-new', 'acme-extra')
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual(item['title'], '  示例 Skills  ')
        self.assertEqual(item['tag'], '示例 skills')
        self.assertEqual(self.ok('/api/team/queue')['items'][0]['tag'], item['tag'])
        for name in item['installed']:
            self.assertEqual(tags.read(self.home)[name], [item['tag']])

    def test_all_present_finishes_without_writes_or_new_operation(self):
        review = self.review('acme-same', 'acme-different')
        before = store.physical_state(self.home / '.skill-pilot')
        with patch.object(github, 'inspect', return_value=review), patch.object(operations, 'install') as install:
            self.enqueue()
            item = self.wait_jobs()[0]
        install.assert_not_called()
        self.assertEqual(item['status'], 'done')
        self.assertEqual(item['message'], '本机已经有了。')
        self.assertEqual(item['installed'], [])
        self.assertEqual(item['reuse'], ['acme-same', 'acme-different'])
        self.assertEqual(item['skipped'], [])
        self.assertIsNone(item['operation'])
        self.assertEqual(item['operations'], [])
        self.assertNotIn('tag', item)
        self.assertEqual(store.physical_state(self.home / '.skill-pilot'), before)
        self.assertFalse(Path(review['temporary']).exists())

    def test_reuse_and_skip_without_new_install_finishes_and_explains_both(self):
        review = self.review('acme-same', 'acme-different', changed=['acme-different'])
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done')
        self.assertEqual(item['installed'], [])
        self.assertIn('本机已经有了。沿用 1 个技能：acme-same。', item['message'])
        self.assertIn('跳过 1 个同名但内容不同的技能：acme-different。', item['message'])
        self.assertIn('可到技能页粘贴仓库链接核对', item['message'])
        self.assertEqual(tags.read(self.home), {})

    def test_all_conflicting_fails_without_overwriting_and_cleans_download(self):
        review = self.review('acme-different', changed=['acme-different'])
        before = store.physical_state(self.home / '.skill-pilot')
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['installed'], [])
        self.assertEqual(item['reuse'], [])
        self.assertEqual(item['skipped'], ['acme-different'])
        self.assertIn('同名技能内容不同', item['message'])
        self.assertIn('可到技能页粘贴仓库链接核对', item['message'])
        self.assertEqual(store.physical_state(self.home / '.skill-pilot'), before)
        self.assertFalse(Path(review['temporary']).exists())

    def test_no_skills_fails_and_cleans_download(self):
        review = self.review()
        with patch.object(github, 'inspect', return_value=review):
            self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['message'], '这个仓库里没有找到可安装的技能。')
        self.assertFalse(Path(review['temporary']).exists())

    def test_download_failure_reason_and_job_retry_replace_the_old_item(self):
        reason = '仓库读取没能完成，请检查链接和网络后重试。'
        with patch.object(github, 'inspect', side_effect=ValueError(reason)):
            first = self.enqueue()
            failed = self.wait_jobs()[0]
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['message'], reason)
        review = self.review('acme-new')
        self.digest_title('示例新标题')
        with patch.object(github, 'inspect', return_value=review):
            retried = self.ok('/api/jobs/retry', {'id': first['id']})['job']
            item = self.wait_jobs()[0]
        self.assertNotEqual(retried['id'], first['id'])
        self.assertEqual(item['status'], 'done', item)
        self.assertEqual(item['title'], '示例新标题')
        self.assertEqual(len(self.ok('/api/team/queue')['items']), 1)
        self.assertEqual(item['installed'], ['acme-new'])

    def test_team_request_replaces_failed_and_completed_items(self):
        with patch.object(github, 'inspect', side_effect=ValueError('示例下载失败')):
            first = self.enqueue()
            self.assertEqual(self.wait_jobs()[0]['status'], 'failed')
        identifiers = {first['id']}
        for expected in (['acme-new'], []):
            review = self.review('acme-new')
            with patch.object(github, 'inspect', return_value=review):
                queued = self.enqueue()
                item = self.wait_jobs()[0]
            self.assertNotIn(queued['id'], identifiers)
            identifiers.add(queued['id'])
            self.assertEqual(len(self.ok('/api/jobs')['items']), 1)
            self.assertEqual(item['status'], 'done')
            self.assertEqual(item['installed'], expected)

    def test_rejects_unknown_and_team_listed_repositories_without_downloading(self):
        team._request(self.config, '/api/listings/me', 'PUT', {'listings': [listing('acme-published')]})
        self.assertNotIn('acme/skills', [r['repository'] for r in self.ok('/api/team/catalog')['github']['repos']])
        with patch.object(github, 'inspect') as download:
            for repository in ('acme/missing', 'acme/skills', 'https://github.com/acme/second', '../../private'):
                with self.subTest(repository=repository):
                    status, result = self.request('/api/team/queue', {'github': repository})
                    self.assertEqual(status, 409)
                    self.assertEqual(result['error'], '这个仓库已不在 GitHub 最近热门里，请刷新团队页。')
            for value in (None, '', 12, [], {}):
                self.assertEqual(self.request('/api/team/queue', {'github': value})[0], 400)
        download.assert_not_called()
        self.assertEqual(self.ok('/api/jobs')['items'], [])

    def test_readonly_rejects_queue_submit_and_retry_without_changes(self):
        with patch.object(github, 'inspect', side_effect=ValueError('示例下载失败')):
            first = self.enqueue()
            self.wait_jobs()
        before = store.physical_state(self.home)
        jobs = self.ok('/api/jobs')
        with patch.dict(os.environ, {'SKILL_PILOT_READ_ONLY': '1'}), patch.object(github, 'inspect') as download:
            for path, body in [('/api/team/queue', {'github': 'acme/skills'}),
                               ('/api/jobs', {'kind': 'github_install', 'repository': 'acme/skills', 'confirmed': True}),
                               ('/api/jobs/retry', {'id': first['id']})]:
                self.assertEqual(self.request(path, body)[0], 403)
            self.assertEqual(self.ok('/api/jobs'), jobs)
        download.assert_not_called()
        self.assertEqual(store.physical_state(self.home), before)

    def test_job_submit_validation_and_retry_recheck_current_trending_list(self):
        body = {'kind': 'github_install', 'repository': 'acme/skills', 'confirmed': True}
        self.assertEqual(self.request('/api/jobs', {**body, 'confirmed': False})[0], 400)
        for repository in (None, '', []):
            self.assertEqual(self.request('/api/jobs', {**body, 'repository': repository})[0], 400)
        with patch.object(github, 'inspect', side_effect=ValueError('示例下载失败')):
            first = self.ok('/api/jobs', body)['job']
            self.wait_jobs()
        self.catalog('acme/second')
        before = self.ok('/api/jobs')
        with patch.object(github, 'inspect') as download:
            self.assertEqual(self.request('/api/jobs/retry', {'id': first['id']})[0], 409)
            self.assertEqual(self.request('/api/team/queue', {'github': 'acme/skills'})[0], 409)
            self.assertEqual(self.request('/api/jobs', body)[0], 409)
        download.assert_not_called()
        self.assertEqual(self.ok('/api/jobs'), before)

    def test_slow_download_does_not_block_duplicates_browsing_or_more_submissions(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def download(url):
            repository = url.removeprefix('https://github.com/')
            calls.append(repository)
            if repository == 'acme/skills':
                entered.set()
                self.assertTrue(release.wait(12))
            return self.review('acme-' + repository.split('/')[1], repository=repository)
        with patch.object(github, 'inspect', side_effect=download):
            first = self.enqueue()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.enqueue()['id'], first['id'])
                self.assertEqual(self.ok('/api/jobs')['items'][0]['status'], 'running')
                second = self.enqueue('acme/second')
                third = self.enqueue('acme/third')
                self.assertEqual([second['status'], third['status']], ['waiting', 'waiting'])
                for path in ('/api/team/catalog', '/api/team/queue', '/api/jobs', '/api/table'):
                    self.ok(path, timeout=3)
                self.assertEqual(calls, ['acme/skills'])
            finally:
                release.set()
            items = self.wait_jobs()
        self.assertEqual(calls, ['acme/skills', 'acme/second', 'acme/third'])
        self.assertEqual([i['status'] for i in items], ['done'] * 3)

    def test_simultaneous_requests_only_create_one_waiting_job(self):
        review = self.review('acme-new')
        with patch.object(github, 'inspect', return_value=review) as download:
            with self.server.write_lock, ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(self.request, '/api/team/queue', {'github': 'acme/skills'}) for _ in range(4)]
                results = [future.result(timeout=10) for future in futures]
                self.assertEqual([status for status, _ in results], [200] * 4)
                self.assertEqual(len({data['items'][0]['id'] for _, data in results}), 1)
            self.assertEqual(self.wait_jobs()[0]['status'], 'done')
        download.assert_called_once()

    def test_shares_job_order_with_team_installs_and_team_clear_preserves_other_jobs(self):
        review = self.review('acme-new')
        execute = self.server.execute_job
        def run(item):
            if item['kind'] == 'team_install':
                return {'message': '本机已经有了。', 'installed': [], 'reuse': ['acme-same'], 'operations': []}
            return execute(item)
        with patch.object(github, 'inspect', return_value=review), patch.object(self.server, 'execute_job', side_effect=run):
            with self.server.write_lock:
                self.ok('/api/jobs', {'kind': 'restore', 'skills': ['acme-missing'], 'confirmed': True})
                self.enqueue()
                self.ok('/api/jobs', {'kind': 'team_install', 'key': 'Acme:key', 'confirmed': True})
                self.assertEqual(len(self.ok('/api/team/queue/clear', {})['items']), 2)
                self.assertEqual(len(self.ok('/api/jobs')['items']), 3)
            items = self.wait_jobs()
        self.assertEqual([i['kind'] for i in items], ['restore', 'github_install', 'team_install'])
        self.assertEqual([i['status'] for i in items], ['failed', 'done', 'done'])
        self.assertEqual(self.ok('/api/team/queue/clear', {})['items'], [])
        self.assertEqual([i['kind'] for i in self.ok('/api/jobs')['items']], ['restore'])

    def test_single_new_skill_does_not_tag_reused_or_skipped_skills(self):
        review = self.review('acme-new', 'acme-same')
        with patch.object(github, 'inspect', return_value=review), patch.object(team, 'tag_pack') as tag:
            self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done')
        self.assertEqual(item['title'], 'acme/skills')
        self.assertEqual(item['summary'], '安装「acme/skills」')
        self.assertEqual(item['installed'], ['acme-new'])
        self.assertNotIn('tag', item)
        self.assertNotIn('tag', self.ok('/api/team/queue')['items'][0])
        tag.assert_not_called()

    def test_install_error_or_new_conflict_cleans_download_and_does_not_claim_success(self):
        errors = [ValueError('示例安装失败，已恢复原样。'), RuntimeError('示例内部信息'),
                  {'conflicts': [{'name': 'acme-new'}], 'message': '有同名技能需要先对比，文件没有改动。'}]
        for result in errors:
            review = self.review('acme-new')
            mock = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with self.subTest(result=result), patch.object(github, 'inspect', return_value=review), \
                    patch.object(operations, 'install', **mock):
                self.enqueue()
                item = self.wait_jobs()[0]
            self.assertEqual(item['status'], 'failed')
            self.assertEqual(item['installed'], [])
            self.assertIsNone(item['operation'])
            self.assertFalse(Path(review['temporary']).exists())
            self.assertNotIn('acme-new', operations.load(self.home)[1]['skills'])
            if isinstance(result, RuntimeError):
                self.assertEqual(item['message'], '安装没能完成，请稍后重试。')

    def test_tag_warning_keeps_successful_install_and_operation(self):
        review = self.review('acme-new', 'acme-extra')
        with patch.object(github, 'inspect', return_value=review), patch.object(
                team, 'tag_pack', return_value='没能加上标签：标签文件没能保存。'):
            self.enqueue()
            item = self.wait_jobs()[0]
        self.assertEqual(item['status'], 'done')
        self.assertIn('没能加上标签：标签文件没能保存。', item['message'])
        self.assertEqual(item['operations'], [item['operation']])
        self.assertNotIn('tag', item)
        self.assertNotIn('tag', self.ok('/api/team/queue')['items'][0])
        self.assertFalse(Path(review['temporary']).exists())
