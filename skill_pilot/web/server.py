from ..platform_support import rmtree
"""Loopback dashboard; every migration write requires an explicit confirmation."""
import difflib
import copy
from contextlib import contextmanager
from ..platform_support import fcntl, user_home, user_id, check_directory_links
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
from socketserver import TCPServer
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..scan import scan
from ..store import apply, backups, guard, preview, restore, target_home
from .. import diagnostics, operations, updates, cleanup, labels, tags, sharing, team, self_update, extra_sources, github_digest, normalization
from .. import agents as agent_management, agent_discovery, version as app_version
from ..sources import github
from ..environment import tool_environment
from . import runtime

STATIC = Path(__file__).parent / "static"
HEARTBEAT_TIMEOUT = 30
PROGRAM_AUTO_CHECK_INTERVAL = 600
# Browsers slow or pause timers in background tabs, so an open page keeps the
# dashboard alive until it says goodbye or has been silent this long.
OPEN_PAGE_TIMEOUT = 24 * 3600
ACTIVE_JOBS = {'waiting', 'running'}
TEAM_JOBS = {'team_install', 'github_install'}
DEMO_PAGE = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1"><title>成品演示</title>'
             '<style>html,body{margin:0;height:100%;background:#fff}iframe{display:block;width:100%;height:100%;border:0}</style>'
             '</head><body><iframe src="f/index.html" title="成品演示" sandbox="allow-scripts" '
             'allow="fullscreen; autoplay" allowfullscreen></iframe></body></html>')


class JobInputError(ValueError):
    """Malformed queue requests are reported as 400, before doing any work."""


def job_parameters(body):
    kind = body.get('kind')
    if not isinstance(kind, str) or kind not in {
            'assignment', 'install', 'update', 'cleanup_withdraw', 'cleanup_delete', 'restore', 'team_install', 'github_install', 'team_publish'}:
        raise JobInputError('请选择支持的任务类型。')
    result = {'kind': kind}
    def text(value):
        return isinstance(value, str) and bool(value.strip())
    def names(value, empty=False):
        return isinstance(value, list) and (empty or bool(value)) and all(text(n) for n in value)
    def changes(value):
        return isinstance(value, list) and all(isinstance(c, dict) and text(c.get('skill')) and
            text(c.get('agent')) and type(c.get('enabled')) is bool for c in value)
    if kind == 'team_install':
        if not text(body.get('key')):
            raise JobInputError('请选择要装上的团队技能。')
        result['key'] = body['key']
        return result
    if kind == 'github_install':
        if not text(body.get('repository')):
            raise JobInputError('请选择要安装的 GitHub 最近热门仓库。')
        result['repository'] = body['repository']
        return result
    if kind == 'assignment':
        value = body.get('changes')
        if not changes(value) or not value or len({(c['skill'], c['agent']) for c in value}) != len(value):
            raise JobInputError('分配改动需逐项填写技能、Agent 和是否启用，同一处只能选择一次。')
        result['changes'] = sorted([{k: c[k] for k in ('skill', 'agent', 'enabled')} for c in value],
                                   key=lambda c: (c['skill'], c['agent']))
    else:
        if not names(body.get('skills')):
            raise JobInputError('请选择要处理的技能，名称需放在列表中。')
        result['skills'] = sorted(set(body['skills']))
    if kind == 'team_publish':
        if not text(body.get('connection')):
            raise JobInputError('请回到我的上架重新更新。')
        result['connection'] = body['connection']
    if kind in {'install', 'update'}:
        if not text(body.get('review_id')):
            raise JobInputError('请带上这次已核对的预览编号。')
        result['review_id'] = body['review_id']
    if kind == 'install':
        if not names(body.get('agents'), empty=True):
            raise JobInputError('请把所选 Agent 放在列表中。')
        result['agents'] = sorted(set(body['agents']))
    if kind == 'update':
        for field in ('choices', 'merge_ids'):
            value = body.get(field, {})
            if not isinstance(value, dict) or any(not text(k) or not text(v) for k, v in value.items()):
                raise JobInputError('更新选择和合并编号需按技能名称逐项填写。')
            if field == 'choices' and any(v not in {'keep', 'upstream', 'merge'} for v in value.values()):
                raise JobInputError('请选择保留我的、用新版或使用已核对的合并结果。')
            result[field] = {k: v for k, v in value.items() if k in result['skills']}
        result['choices'] = {n: result['choices'].get(n, 'upstream') for n in result['skills']}
        result['merge_ids'] = {n: v for n, v in result['merge_ids'].items() if result['choices'][n] == 'merge'}
        if type(body.get('confirm_lark_cli', False)) is not bool:
            raise JobInputError('请明确是否已确认飞书工具一起升级。')
        result['confirm_lark_cli'] = body.get('confirm_lark_cli', False)
    if kind in {'assignment', 'cleanup_withdraw', 'cleanup_delete'}:
        seen = body.get('seen')
        fields = ['dependency_warnings', 'path_warnings']
        if kind == 'assignment': fields.append('changes')
        if kind == 'cleanup_delete': fields.append('irreplaceable')
        if not isinstance(seen, dict) or any(not isinstance(seen.get(k), list) for k in fields):
            raise JobInputError('请带上确认时看到的全部改动和提醒。')
        if (not names(seen['dependency_warnings'], empty=True) or
                any(not isinstance(w, dict) or not text(w.get('file')) or not names(w.get('entries'), empty=True)
                    for w in seen['path_warnings']) or
                ('changes' in fields and not changes(seen['changes'])) or
                ('irreplaceable' in fields and not names(seen['irreplaceable'], empty=True))):
            raise JobInputError('确认时的改动或提醒格式不正确，请重新核对。')
        result['seen'] = {k: copy.deepcopy(seen[k]) for k in fields}
    return result


class Dashboard(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # The dashboard only listens on loopback; reverse DNS must not delay startup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def __init__(self, inventory, resume=None):
        session = resume or {}
        self.token = session.get('token', secrets.token_urlsafe(32))
        self.inventory = inventory
        self.home = Path(inventory.get("home", target_home()))
        extra_sources.ensure(self.home)
        self.install_reviews = session.get('install_reviews', {})
        self.team_upload_reviews = session.get('team_upload_reviews', {})
        self.feedback_previews = {}
        self.feedback_lock = threading.Lock()
        self.jobs = []
        self.jobs_version = 0
        self.jobs_blocked = session.get('jobs_blocked', '')
        self.jobs_lock = threading.RLock()
        self.jobs_thread = None
        self.team_queue_lock = self.jobs_lock
        # Lock order: write_lock -> update_lock -> jobs_lock. Neither metadata
        # lock is held during filesystem work or network requests.
        self.write_lock = threading.RLock()
        self.update_reviews = session.get('update_reviews', {})
        self.update_lock = threading.Lock()
        self.update_users = {}
        self.update_thread = None
        self.labels_lock = threading.Lock()
        self.labels_thread = None
        self.labels_progress = session.get('labels_progress', {'running': False, 'done': 0, 'total': 0, 'message': '', 'error': ''})
        running = app_version.running_build()
        self.running_version = running['version']
        self.program_lock = threading.Lock()
        self.program_action = ''
        self.program_review = session.get('program_review')
        raw_progress = session.get('program_progress')
        saved_version = raw_progress.get('current_version') if isinstance(raw_progress, dict) else self.running_version
        self.program_progress = raw_progress if isinstance(raw_progress, dict) else {'running': False, 'phase': 'idle',
            'message': '从 GitHub 官方项目获取新版', 'error': '', 'available': False,
            'current': running['date'], 'latest': '',
            'current_version': self.running_version, 'latest_version': '', 'pending_version': ''}
        self.program_progress.setdefault('automatic', False)
        self.program_progress.setdefault('changes', [])
        self.program_progress.setdefault('changes_total', 0)
        self.program_progress.setdefault('changes_partial', False)
        self.program_progress.setdefault('current_version', self.running_version)
        self.program_progress.setdefault('latest_version', '')
        self.program_progress.setdefault('pending_version', '')
        self.program_progress.setdefault('failed_action', '')
        try:
            operations.sync_bundled_skill(self.home)
        except Exception as error:
            diagnostics.failure(self.home, 'self_update', '同步 Skill Pilot 技能说明', error)
            if isinstance(error, operations.RollbackError):
                self.jobs_blocked = str(error)
            self.program_progress.update(phase='error', error='程序可继续使用，但自带技能说明没能同步，请查看诊断后重新打开看板。')
        # 换进程/重载时没有还在跑的后台更新线程，先清理任何被中断的运行中状态。
        interrupted_auto_check = False
        if resume and self.program_progress.get('running'):
            interrupted_auto_check = bool(
                self.program_progress.get('automatic') and self.program_progress.get('phase') == 'check')
            self.program_progress.update(
                running=False, phase='idle', available=False, error='', failed_action='',
                current_version=self.running_version, latest_version='', pending_version='',
                changes=[], changes_total=0, changes_partial=False,
                message='还没查到新版信息' if interrupted_auto_check else '从 GitHub 官方项目获取新版',
            )
            self.program_progress.pop('checked_at', None)
            self.program_progress.pop('target', None)
        if self.program_progress['phase'] == 'ready':
            new_version = self.running_version or self.program_progress.get('pending_version', '')
            self.program_progress.update(phase='latest', message='SP 已升级到最新版',
                                         current_version=new_version,
                                         latest_version=new_version or self.program_progress.get('latest_version', ''),
                                         pending_version='', failed_action='',
                                         changes=[], changes_total=0, changes_partial=False)
        elif resume and self.running_version and not interrupted_auto_check:
            if not isinstance(saved_version, str) or not saved_version or saved_version != self.running_version:
                self._reset_stale_program_check(running)
            else:
                self.program_progress['current_version'] = self.running_version
        self.program_thread = None
        self.last_heartbeat = time.monotonic()
        self.pages = set(session.get('pages', []))
        self.code_version = runtime.version()
        self.runtime_lock = threading.Lock()
        self.active_requests = 0
        self.reload_callback = None
        self.reload_pending = False
        self.static_refresh_deferred = False
        self.reload_error = ''
        self.reload_error_id = ''
        self.reload_thread = None
        self.closing = False
        self.github_digests = github_digest.Digests(self.home, lambda: self.closing or self.reload_pending or
                                                  self.program_progress['phase'] in {'apply', 'ready'})
        self.agents_generation = 0
        self.agents_cache = None
        self.agents_lock = threading.Lock()
        self.agent_findings = agent_discovery.read(self.home)
        self.agents_progress = {'running': False, 'checked_at': self.agent_findings['checked_at'],
                                'message': '', 'error': ''}
        self.agents_thread = None
        self.inventory_lock = threading.Lock()
        self.preview_generation = 0
        self.preview_generation_lock = threading.Lock()
        self.preview_work_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), Handler, bind_and_activate=not bool(resume))
        if resume:
            self.socket.close()
            self.socket = socket.socket(fileno=resume['socket_fd'])
            self.server_address = self.socket.getsockname()
            self.server_name, self.server_port = self.server_address
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.url = f"{self.origin}/?token={self.token}"

    def server_close(self):
        with self.jobs_lock:
            self.closing = True
        super().server_close()
        with self.jobs_lock:
            queue_thread = self.jobs_thread
        if queue_thread: queue_thread.join()
        if self.update_thread: self.update_thread.join()
        if self.labels_thread: self.labels_thread.join()
        self.github_digests.close()
        if self.agents_thread: self.agents_thread.join()
        if self._program_thread_blocks_exit():
            self.program_thread.join()
        if self.reload_thread: self.reload_thread.join()
        if self.program_review:
            rmtree(self.program_review['temporary'], ignore_errors=True)

    def _program_thread_blocks_exit(self):
        # 打开看板时的自动检查不挡住关看板和刷新应用新版；手动检查和升级仍要等。
        thread = self.program_thread
        return bool(thread and thread.is_alive() and not self.program_progress.get('automatic'))

    def preview_assignment(self, changes, recheck):
        """Run one read-only assignment check. A newer check replaces an older one."""
        with self.preview_generation_lock:
            self.preview_generation += 1
            generation = self.preview_generation

        def cancelled():
            return generation != self.preview_generation

        with self.preview_work_lock:
            if cancelled():
                return {'superseded': True}
            try:
                return operations.preview_assign(changes, self.home, recheck, cancelled)
            except operations.PreviewSuperseded:
                return {'superseded': True}

    def background_busy(self):
        return (any(thread and thread.is_alive() for thread in (self.update_thread, self.labels_thread, self.agents_thread)) or
                self.jobs_busy() or
                self._program_thread_blocks_exit() or
                self.labels_progress['running'] or
                any(r['running'] for r in list(self.update_reviews.values())))

    def jobs_busy(self):
        with self.jobs_lock:
            return (any(item['status'] in ACTIVE_JOBS for item in self.jobs) or
                    bool(self.jobs_thread and self.jobs_thread.is_alive()))

    def jobs_status(self):
        with self.jobs_lock:
            return {'items': [self.public_job(item) for item in self.jobs],
                    'version': self.jobs_version, 'blocked': self.jobs_blocked}

    @staticmethod
    def public_job(item):
        return copy.deepcopy({k: v for k, v in item.items() if not k.startswith('_')})

    @property
    def team_queue(self):
        return [item for item in self.jobs if item['kind'] in TEAM_JOBS]

    @team_queue.setter
    def team_queue(self, items):
        # Keep the legacy team view backed by the single job list.
        self.jobs = [item for item in self.jobs if item['kind'] != 'team_install'] + [
            {**item, 'kind': 'team_install', 'skills': [item['name']], 'agents': [],
             'summary': '安装团队技能', 'operations': [],
             '_parameters': {'kind': 'team_install', 'key': item['key']},
             '_intent': json.dumps({'kind': 'team_install', 'key': item['key']}, sort_keys=True)} for item in items]

    def deliverable_key(self, member_id, identity):
        """Opens one demo for this dashboard session only; never the session token itself."""
        message = ('deliverable:' + member_id + ':' + identity).encode()
        return hmac.new(self.token.encode(), message, hashlib.sha256).hexdigest()[:32]

    def team_queue_status(self):
        fields = {'id', 'kind', 'key', 'name', 'title', 'repository', 'status', 'message', 'installed', 'reuse',
                  'skipped', 'operation', 'tag', 'pack', 'pack_title', 'error_id'}
        with self.jobs_lock:
            return {'items': [copy.deepcopy({k: v for k, v in item.items() if k in fields})
                              for item in self.team_queue]}

    def _accept_jobs(self):
        if self.closing:
            raise ValueError('看板正在关闭，请重新打开后再操作。')
        if self.jobs_blocked:
            raise ValueError(self.jobs_blocked)

    def _add_job(self, parameters, previous=None, team_info=None):
        kind = parameters['kind']
        intent = json.dumps({k: v for k, v in parameters.items() if k != 'seen'}, sort_keys=True)
        with self.jobs_lock:
            self._accept_jobs()
            existing = next((i for i in self.jobs if i.get('_intent') == intent and i['status'] in ACTIVE_JOBS), None)
            if existing:
                if previous:
                    self.jobs.remove(previous)
                    self.jobs_version += 1
                return {'job': self.public_job(existing), 'duplicate': True, 'version': self.jobs_version}
            skills = parameters.get('skills', sorted({c['skill'] for c in parameters.get('changes', [])}))
            agents = parameters.get('agents', sorted({c['agent'] for c in parameters.get('changes', [])}))
            titles = self.skill_titles()
            action = {'assignment': '调整分配', 'install': '安装', 'update': '更新', 'cleanup_withdraw': '撤下',
                      'cleanup_delete': '删除', 'restore': '恢复', 'team_install': '安装团队技能', 'github_install': '安装',
                      'team_publish': '更新我的上架'}[kind]
            item = {'id': secrets.token_urlsafe(16), 'kind': kind, 'status': 'waiting',
                    'skills': skills, 'agents': agents, 'summary': action + ('「' + '、'.join(titles.get(n, n) for n in skills) + '」' if skills else ''),
                    'message': '等待处理。', 'operations': [], '_parameters': copy.deepcopy(parameters), '_intent': intent}
            if kind == 'assignment': item['changes'] = copy.deepcopy(parameters['changes'])
            if kind == 'team_publish': item['summary'] = f'更新我的上架（{len(skills)} 个）'
            if 'review_id' in parameters: item['review_id'] = parameters['review_id']
            if kind == 'team_install':
                info = team_info or {}
                item.update(key=parameters['key'], name=info.get('name', ''), installed=[], reuse=[], message='等待安装。')
                if info.get('title') is not None: item['title'] = info['title']
                for field in ('pack', 'pack_title', '_pack_skills'):
                    if field in info: item[field] = copy.deepcopy(info[field])
                if info.get('name'):
                    item.update(skills=[info['name']], summary='安装「' + (info.get('title') or titles.get(info['name'], info['name'])) + '」')
            if kind == 'github_install':
                repository = parameters['repository']
                title = (team_info or {}).get('title') or repository
                item.update(key='github:' + repository, repository=repository, title=title,
                            summary='安装「' + title + '」', installed=[], reuse=[], skipped=[], operation=None,
                            message='等待安装。')
            if previous:
                if previous['status'] in {'failed', 'attention'}:
                    item['_batch'] = previous.get('_batch', previous['id'])
                    item['_completed_operations'] = list(previous['operations'])
                    item['operations'] = list(previous['operations'])
                    item['_completed'] = list(previous.get('completed', []))
                    item['completed'] = list(previous.get('completed', []))
                    if kind == 'github_install' and (previous.get('remaining') or previous.get('_retry_skills')):
                        item['_retry_skills'] = list(previous.get('remaining') or previous['_retry_skills'])
                    if kind == 'update':
                        item['_updated'] = list(previous.get('updated', []))
                self.jobs.remove(previous)
            self.jobs.append(item)
            self.jobs_version += 1
            if self.jobs_thread is None:
                self.jobs_thread = threading.Thread(target=self.jobs_work, daemon=True)
                self.jobs_thread.start()
            return {'job': self.public_job(item), 'duplicate': False, 'version': self.jobs_version}

    def queue_job(self, body):
        if body.get('confirmed') is not True:
            raise JobInputError('请先核对，并确认这次操作。')
        parameters = job_parameters(body)
        if parameters['kind'] == 'github_install':
            return self.queue_github(parameters['repository'])
        # Enqueue and pruning use this order so a referenced review cannot be
        # removed in the gap between validation and adding the waiting item.
        with self.update_lock:
            return self._add_job(parameters)

    def retry_job(self, body):
        if not isinstance(body.get('id'), str) or not body['id']:
            raise JobInputError('请选择要重试的任务。')
        with self.update_lock, self.jobs_lock:
            self._accept_jobs()
            previous = next((i for i in self.jobs if i['id'] == body['id']), None)
            if previous is None or previous['status'] not in {'failed', 'attention'}:
                raise JobInputError('只能重试失败或需要处理的任务。')
            parameters = copy.deepcopy(previous['_parameters'])
            if previous.get('remaining_changes'):
                parameters['changes'] = copy.deepcopy(previous['remaining_changes'])
            elif previous.get('remaining'):
                parameters['skills'] = list(previous['remaining'])
            if parameters['kind'] == 'update' and previous.get('failed'):
                parameters['skills'] = [r['name'] for r in previous['failed']]
            for field in ('seen', 'review_id', 'choices', 'merge_ids', 'confirm_lark_cli'):
                if field in body: parameters[field] = body[field]
            parameters = job_parameters(parameters)
            if parameters['kind'] != 'github_install':
                return self._add_job(parameters, previous, previous)
        return self.queue_github(parameters['repository'])

    def clear_jobs(self, team_only=False):
        with self.jobs_lock:
            remaining = [i for i in self.jobs if i['status'] in ACTIVE_JOBS or (team_only and i['kind'] not in TEAM_JOBS)]
            if len(remaining) != len(self.jobs):
                self.jobs = remaining
                self.jobs_version += 1
            return self.team_queue_status() if team_only else self.jobs_status()

    def queue_team(self, key, pack_info=None):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('请选择要装上的技能。')
        with self.jobs_lock:
            self._accept_jobs()
            previous = next((item for item in self.team_queue if item['key'] == key), None)
            if previous and previous['status'] in {'waiting', 'running'}:
                if pack_info: previous.update(copy.deepcopy(pack_info))
                return self.team_queue_status()
        if previous:
            name, title = previous['name'], previous.get('title')
        else:
            _, _, cards = team._fetch(self.home)
            card = next((c for c in cards if c['key'] == key), None)
            if card is None:
                raise ValueError('这个技能已经下架。')
            name = card['listings'][0]['name']
            title = next((r['title'] for r in card['listings'] if 'title' in r), None)
        with self.jobs_lock:
            previous = next((item for item in self.team_queue if item['key'] == key), None)
            if previous and previous['status'] in ACTIVE_JOBS:
                if pack_info: previous.update(copy.deepcopy(pack_info))
                return self.team_queue_status()
            info = {k: previous[k] for k in ('pack', 'pack_title', '_pack_skills') if previous and k in previous}
            self._add_job({'kind': 'team_install', 'key': key}, previous,
                          {**info, 'name': name, 'title': title, **(pack_info or {})})
            return self.team_queue_status()

    def queue_team_updates(self):
        with self.jobs_lock:
            self._accept_jobs()
        connection = team._connection_version(team._connection(self.home))
        candidates = team.listing_candidates(self.home)['skills']
        if connection != team._connection_version(team._connection(self.home)):
            raise ValueError('分享站连接已经变了，请刷新列表后重试。')
        with self.jobs_lock:
            pending = {name for item in self.jobs if item['kind'] == 'team_publish' and item['status'] in ACTIVE_JOBS
                       and item['_parameters']['connection'] == connection for name in item['skills']}
            names = sorted(row['name'] for row in candidates if row['listed'] and row['changed'] and row['name'] not in pending)
            if names:
                self._add_job({'kind': 'team_publish', 'skills': names, 'connection': connection})
            return {**self.jobs_status(), 'queued': len(names),
                    'message': f'已加入 {len(names)} 个上架更新。' if names else
                               '上架更新已在队列中。' if pending else '已上架的技能没有本地新版。'}

    def queue_github(self, repository):
        parameters = job_parameters({'kind': 'github_install', 'repository': repository})
        with self.jobs_lock:
            self._accept_jobs()
        catalog = team.catalog(self.home)
        row = next((r for r in catalog.get('github', {}).get('repos', [])
                    if r['repository'].lower() == repository.lower()), None)
        if row is None:
            raise ValueError('这个仓库已不在 GitHub 最近热门里，请刷新团队页。')
        repository = parameters['repository'] = row['repository']
        with self.github_digests.lock:
            title = (self.github_digests.cache.get(repository, {}).get('digest') or {}).get('title') or repository
        with self.jobs_lock:
            previous = next((i for i in self.team_queue if i['key'] == 'github:' + repository
                             and i['status'] not in ACTIVE_JOBS), None)
            return self._add_job(parameters, previous, {'title': title})

    def queue_team_pack(self, key, keys):
        if (not isinstance(key, str) or not isinstance(keys, list) or not keys or
                any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys)):
            raise ValueError('请从技能包里选择至少一个要安装的技能。')
        with self.jobs_lock:
            self._accept_jobs()
        _, _, data = team._fetch(self.home, with_packs=True)
        pack = next((p for p in data.get('packs', []) if p['key'] == key), None)
        if pack is None:
            raise ValueError('这个技能包已经拆开或不再上架，请刷新商店。')
        cards = {c['key']: c for c in data['cards']}
        if not set(keys) <= set(pack['cards']) & cards.keys():
            raise ValueError('选中的技能不全在这个技能包里，请重新选择。')
        registry = operations.load(self.home)[1]
        names = [team._local(row, registry)[1] for card_key in pack['cards'] if card_key in cards
                 for row in cards[card_key]['listings'] if row['member']['id'] == pack['member']['id']]
        info = {'pack': key, 'pack_title': pack['title'], '_pack_skills': list(dict.fromkeys(names))}
        for card_key in dict.fromkeys(pack['cards']):
            if card_key in keys and team._local(cards[card_key]['listings'][0], registry)[0] not in {'installed', 'update'}:
                self.queue_team(card_key, info)
        return self.team_queue_status()

    def clear_team_queue(self):
        return self.clear_jobs(team_only=True)

    def stop_jobs(self):
        with self.jobs_lock:
            self.jobs_blocked = '这项操作没能完全恢复，后续任务已停止，请先用备份还原。'
            for item in self.jobs:
                if item['status'] in ACTIVE_JOBS:
                    item.update(status='failed', message=self.jobs_blocked)
                    self.jobs_version += 1

    @contextmanager
    def local_write(self, restoring=False):
        with self.write_lock:
            if self.jobs_blocked and not restoring:
                raise ValueError(self.jobs_blocked)
            try:
                yield
            except operations.RollbackError:
                self.stop_jobs()
                raise

    def job_message(self, message):
        try:
            key = team.settings(self.home).get('key', '')
        except ValueError:
            key = ''
        message = team._redact(team._redact(message, self.token), key)
        return self_update._hide_paths_outside_home(message)

    def skill_titles(self):
        return {n: v['title'] for g in labels.read(self.home) for n, v in g['skills'].items()}

    def skill_message(self, message):
        titles = self.skill_titles()
        if not titles:
            return message
        names = '|'.join(re.escape(n) for n in sorted(titles, key=len, reverse=True))
        return re.sub(r'(?<![A-Za-z0-9_.-])(' + names + r')(?![A-Za-z0-9_.-])',
                      lambda match: titles[match[0]], message)

    def prune_update_reviews(self):
        with self.update_lock, self.jobs_lock:
            latest = next(reversed(self.update_reviews), None)
            protected = {i['_parameters']['review_id'] for i in self.jobs
                         if i['kind'] == 'update' and i['status'] in ACTIVE_JOBS}
            obsolete = [self.update_reviews.pop(key) for key, review in list(self.update_reviews.items())
                        if key != latest and key not in protected and not review['running'] and not self.update_users.get(key)]
        for review in obsolete:
            rmtree(review['temporary'], ignore_errors=True)

    @contextmanager
    def update_review(self, identifier):
        with self.update_lock:
            review = self.update_reviews.get(identifier)
            if review is None:
                raise ValueError('这次更新检查已失效，请重新检查更新。')
            self.update_users[identifier] = self.update_users.get(identifier, 0) + 1
        try:
            yield review
        finally:
            with self.update_lock:
                self.update_users[identifier] -= 1
            self.prune_update_reviews()

    def jobs_work(self):
        while True:
            with self.jobs_lock:
                if self.closing or self.jobs_blocked or not any(i['status'] == 'waiting' for i in self.jobs):
                    self.jobs_thread = None
                    return
            with self.write_lock:
                with self.jobs_lock:
                    if self.closing or self.jobs_blocked:
                        continue
                    item = next(i for i in self.jobs if i['status'] == 'waiting')
                    item.update(status='running', message='正在处理…')
                    self.jobs_version += 1
                try:
                    if not preview_allowed(self.home):
                        raise ValueError('当前处于只读模式，文件没有改动。')
                    result = self.execute_job(item)
                    result['operations'] = list(dict.fromkeys(item.get('_completed_operations', []) + result.get('operations', [])))
                    if item.get('_updated'):
                        result['updated'] = sorted(set(item['_updated'] + result.get('updated', [])))
                        result['message'] = '此前已更新：' + self.skill_message('、'.join(item['_updated'])) + '。' + result['message']
                    try:
                        records = [r for r in operations.history(self.home) if r['id'] in result.get('operations', [])]
                    except (OSError, ValueError):
                        records = []
                    if records:
                        result.update(skills=sorted(set(item['skills']) | {n for r in records for n in r['skills']}),
                                      agents=sorted(set(item['agents']) | {a for r in records for a in r['agents']}))
                except operations.RollbackError as error:
                    with self.jobs_lock:
                        item['operations'] += [r['id'] for r in getattr(error, 'operations', [])]
                        item['completed'] = sorted(set(item.get('completed', []) + getattr(error, 'completed', [])))
                        item['updated'] = item.get('_updated', []) + getattr(error, 'updated', [])
                    item.update(diagnostics.failure(self.home, 'job', item['kind'], error))
                    self.stop_jobs()
                    result = None
                except Exception as error:
                    result = {'status': 'failed', 'message': self.job_message(str(error))
                              if isinstance(error, ValueError) else '安装没能完成，请稍后重试。'
                              if item['kind'] in TEAM_JOBS else '任务没能完成，请重新核对后重试。'}
                    result.update(diagnostics.failure(self.home, 'job', item['kind'], error))
                finally:
                    self.invalidate_agents()
                    try:
                        self.inventory = scan(self.home)
                    except (OSError, ValueError) as error:
                        diagnostics.record(self.home, 'backend', '刷新技能名单', error)  # Preserve the task result.
                if result is not None:
                    if item.get('_completed'):
                        result['completed'] = sorted(set(item['_completed'] + result.get('completed', [])))
                        result['message'] = '此前已完成：' + self.skill_message('、'.join(item['_completed'])) + '。' + result['message']
                    with self.jobs_lock:
                        item.update({'status': 'done', **result})
                        self.jobs_version += 1
            self.prune_update_reviews()

    @staticmethod
    def confirmation_changes(plan, seen):
        reasons = {'changes': '执行前出现了新的分配改动。', 'dependency_warnings': '有技能需要使用它，影响有变化。',
                   'path_warnings': '有新的文件引用受到影响。', 'irreplaceable': '有新增的技能删除后无法重新获取。'}
        impacts = []
        for field, old in seen.items():
            for value in plan.get(field, []):
                if value not in old:
                    impacts.append({'field': field, 'value': value, 'reason': reasons[field]})
        if impacts:
            return {'status': 'attention', 'attention': 'confirmation', 'needs_confirmation': True,
                    'message': '影响有变化，文件没有改动，请重新核对并确认。', 'new_impacts': impacts,
                    'preview': {k: copy.deepcopy(plan[k]) for k in (*seen, 'revision') if k in plan}}

    def execute_job(self, item):
        parameters = item['_parameters']
        kind = item['kind']
        if kind not in {'assignment', 'install', 'cleanup_withdraw', 'cleanup_delete', 'restore'}:
            return self._execute_job(item)
        names = sorted({c['skill'] for c in parameters['changes']} if kind == 'assignment' else parameters['skills'])
        # Keep dependency-connected selections atomic, including selections that
        # share a dependency. Ordinary successful batches still use one write.
        graph = {}
        registry = operations.load(self.home)[1]['skills']
        dependencies = {n: set(s.get('requires', [])) for n, s in registry.items()}
        if kind == 'install':
            for name, required in operations.curated_dependencies()[0].items():
                dependencies.setdefault(name, set()).update(required)
        for name, required in dependencies.items():
            for dependency in required:
                graph.setdefault(name, set()).add(dependency)
                graph.setdefault(dependency, set()).add(name)
        pending, groups = set(names), []
        while pending:
            connected, todo = set(), [min(pending)]
            while todo:
                name = todo.pop()
                if name not in connected:
                    connected.add(name)
                    todo.extend(graph.get(name, set()) - connected)
            groups.append(sorted(connected & pending))
            pending -= connected
        token = operations.operation_batch.set(item.get('_batch', item['id']))
        try:
            try:
                result = self._execute_job(item)
            except operations.RollbackError:
                raise
            except (ValueError, OSError):
                if len(groups) < 2:
                    raise
            else:
                if result.get('status', 'done') == 'done':
                    return {**result, 'completed': names}
                if len(groups) < 2:
                    return result
            completed, failed, ids, conflicts, impacts = [], [], [], [], []
            preview = copy.deepcopy(parameters.get('seen', {}))
            for group in groups:
                child_parameters = copy.deepcopy(parameters)
                if kind == 'assignment':
                    child_parameters['changes'] = [c for c in parameters['changes'] if c['skill'] in group]
                else:
                    child_parameters['skills'] = group
                try:
                    result = self._execute_job({**item, '_parameters': child_parameters})
                except operations.RollbackError as error:
                    error.operations = [{'id': i} for i in ids] + getattr(error, 'operations', [])
                    error.completed = completed
                    raise
                except (ValueError, OSError) as error:
                    result = {'status': 'failed', 'message': str(error) if isinstance(error, ValueError)
                              else '文件没能读写，请核对后重试。'}
                    result.update(diagnostics.failure(self.home, 'job', kind, error))
                ids.extend(result.get('operations', []))
                if result.get('status', 'done') == 'done':
                    completed.extend(group)
                    continue
                for name in group:
                    reason = '；'.join(c['message'] for c in result.get('conflicts', [])
                                      if c.get('skill', c.get('name')) == name) or result['message']
                    failed.append({'name': name, 'error': self.job_message(self.skill_message(reason)),
                                   **({'error_id': result['error_id']} if result.get('error_id') else {})})
                conflicts.extend(result.get('conflicts', []))
                impacts.extend(result.get('new_impacts', []))
                for key in preview:
                    for value in result.get('preview', {}).get(key, []):
                        if value not in preview[key]:
                            preview[key].append(value)
            remaining = [r['name'] for r in failed]
            status = 'attention' if impacts or conflicts else 'failed' if failed else 'done'
            message = '已完成：' + ('、'.join(completed) or '无') + '。'
            if remaining:
                message += '未完成：' + '、'.join(remaining) + '。'
                if completed:
                    message += '已完成的部分已保留。'
            return {'status': status, 'message': self.skill_message(message), 'completed': completed,
                    'failed': failed, 'remaining': remaining, 'operations': ids, 'conflicts': conflicts,
                    **({'remaining_changes': [c for c in parameters['changes'] if c['skill'] in remaining]} if kind == 'assignment' else {}),
                    **({'attention': 'confirmation', 'new_impacts': impacts, 'preview': preview} if impacts else
                       {'attention': 'conflict'} if conflicts else {})}
        finally:
            operations.operation_batch.reset(token)

    def _execute_job(self, item):
        parameters = item['_parameters']
        kind = item['kind']
        names = parameters.get('skills', [])
        if kind == 'team_publish':
            completed, failed = [], []
            for index, name in enumerate(names):
                with self.jobs_lock:
                    item['message'] = f'正在更新 {index + 1}/{len(names)}：' + self.skill_message(name)
                    self.jobs_version += 1
                try:
                    team.update_listing(self.home, name, parameters['connection'])
                    completed.append(name)
                except Exception as error:
                    reason = str(error) if isinstance(error, ValueError) else '更新没能完成，请稍后重试。'
                    failed.append({'name': name, 'error': self.job_message(self.skill_message(reason)),
                                   **diagnostics.failure(self.home, 'job', '更新我的上架', error)})
            return {'status': 'failed' if failed else 'done', 'completed': completed, 'failed': failed,
                    'remaining': [row['name'] for row in failed],
                    'message': f'上架更新完成：成功 {len(completed)} 个，失败 {len(failed)} 个。'}
        if kind in {'update', 'cleanup_withdraw', 'cleanup_delete', 'restore'}:
            registry = operations.load(self.home)[1]['skills']
            with self.jobs_lock:
                item['agents'] = sorted({a for n in names for a in registry.get(n, {}).get('assigned_agents', [])} |
                                        {a for n in names for a in registry.get(n, {}).get('withdrawn_agents', [])})
        if kind == 'assignment':
            for _ in range(3):
                plan = operations.preview_assign(parameters['changes'], self.home, recheck=True)
                if plan['conflicts']:
                    return {'status': 'attention', 'attention': 'conflict', 'conflicts': plan['conflicts'],
                            'message': '部分选择与当前状态冲突，文件没有改动，请先处理这些选择。'}
                attention = self.confirmation_changes(plan, parameters['seen'])
                if attention: return attention
                try:
                    result = operations.apply_assign(parameters['changes'], plan['revision'], True, self.home, recheck=True,
                        review_check=lambda current: self.confirmation_changes(current, parameters['seen']))
                except operations.AssignmentChanged:
                    continue
                if result.get('needs_attention'):
                    return {'status': 'attention', 'attention': 'conflict', 'conflicts': result['conflicts'],
                            'message': '部分选择与当前状态冲突，文件没有改动，请先处理这些选择。'}
                if result.get('status') == 'attention': return result
                break
            else:
                raise ValueError('技能状态连续发生变化，文件没有改动，请重新核对后重试。')
        elif kind == 'install':
            review = item.get('_install_review') or self.install_reviews.get(parameters['review_id'])
            if review is None or not Path(review['temporary']).is_dir():
                raise ValueError('安装预览已失效，请重新读取安装来源。')
            _, _, agents = operations.load(self.home)
            if not set(parameters['agents']) <= set(agents):
                raise ValueError('所选 Agent 已不再接入，没有安装，请重新选择。')
            result = operations.install(review, names, parameters['agents'], home=self.home)
            if result.get('conflicts'):
                return {'status': 'attention', 'attention': 'conflict', 'review_id': parameters['review_id'],
                        'conflicts': [{'skill': c['name'], 'agent': None,
                                       **{k: c[k] for k in ('name', 'same_content', 'message')}} for c in result['conflicts']],
                        'message': result['message']}
        elif kind == 'update':
            with self.update_review(parameters['review_id']) as review:
                if review['running']:
                    raise ValueError('这次更新检查还没完成，请等检查完成后再提交。')
                registry = operations.load(self.home)[1]['skills']
                missing = [{'name': n, 'error': '这个技能已经删除，没有更新。'} for n in names if n not in registry]
                remaining = [n for n in names if n in registry]
                try:
                    result = updates.apply(review, remaining, parameters['choices'], True,
                                           parameters['merge_ids'], parameters['confirm_lark_cli']) if remaining else {
                                               'updated': [], 'failed': [], 'operations': []}
                except operations.RollbackError:
                    raise
                except (ValueError, OSError) as error:
                    reason = str(error) if isinstance(error, ValueError) else '文件没能读写，请重新检查更新后再试。'
                    captured = diagnostics.failure(self.home, 'job', '更新技能', error)
                    result = {'updated': [], 'failed': [{'name': n, 'error': reason, **captured} for n in remaining], 'operations': []}
                failed = result.get('failed', []) + missing
                ids = [r['id'] for r in result.get('operations', [])]
                message = '已更新：' + ('、'.join(result['updated']) or '无') + '。'
                if failed:
                    message += '未更新：' + '；'.join(r['name'] + '：' + r['error'] for r in failed)
                    message += ' 请重新检查更新后再试。'
                return {'status': 'failed' if failed else 'done', 'message': self.job_message(self.skill_message(message)),
                        'updated': result['updated'], 'failed': [
                            {'name': r['name'], 'error': self.job_message(self.skill_message(r['error'])), **({'error_id': r['error_id']} if r.get('error_id') else {})} for r in failed],
                        'operations': ids}
        elif kind in {'cleanup_withdraw', 'cleanup_delete'}:
            action = cleanup.delete_preview if kind == 'cleanup_delete' else cleanup.preview
            plan = action(names, self.home)
            attention = self.confirmation_changes(plan, parameters['seen'])
            if attention: return attention
            action = cleanup.delete if kind == 'cleanup_delete' else cleanup.apply
            result = action(names, True, self.home, plan['revision'],
                            review_check=lambda current: self.confirmation_changes(current, parameters['seen']))
            if result.get('status') == 'attention': return result
        elif kind == 'restore':
            registry = operations.load(self.home)[1]['skills']
            missing = [n for n in names if n not in registry]
            restored = [n for n in names if n in registry and not registry[n].get('withdrawn')]
            if missing or restored:
                raise ValueError(self.skill_message('；'.join(filter(None, [
                    '、'.join(missing) + '：已经删除，没有恢复。' if missing else '',
                    '、'.join(restored) + '：已经恢复或不在已撤下列表，无需重复恢复。' if restored else '']))))
            result = cleanup.restore(names, self.home)
        elif kind == 'github_install':
            review = github.inspect('https://github.com/' + parameters['repository'])
            try:
                missing = []
                if item.get('_retry_skills'):
                    missing = [{'name': n, 'error': '这个技能已不在本次安装来源中，请重新核对来源。'}
                               for n in item['_retry_skills'] if n not in {s['name'] for s in review['skills']}]
                    review['skills'] = [s for s in review['skills'] if s['name'] in item['_retry_skills']]
                if not review['skills'] and not missing:
                    raise ValueError('这个仓库里没有找到可安装的技能。')
                registry = operations.load(self.home)[1]
                collisions = {c['name']: c['same_content'] for c in operations.conflicts(review['skills'], registry, self.home)}
                installed = [s['name'] for s in review['skills'] if s['name'] not in collisions]
                reuse = [n for n, same in collisions.items() if same]
                skipped = [n for n, same in collisions.items() if not same]
                result = self.execute_job({**item, 'kind': 'install', '_install_review': review,
                    '_parameters': {'kind': 'install', 'skills': installed, 'agents': [],
                                    'review_id': review['id']}}) if installed else {}
                installed = [n for n in installed if n in result.get('completed', [])]
                failed = result.get('failed', []) + missing
                for conflict in result.get('conflicts', []):
                    if not any(r['name'] == conflict['name'] for r in failed):
                        failed.append({'name': conflict['name'], 'error': conflict['message']})
                message = (f'已安装 {len(installed)} 个技能。' if installed else '本机已经有了。' if reuse else
                           '安装失败。' if failed else '安装失败：同名技能内容不同，现有内容已保留。')
                if reuse and (installed or skipped):
                    message += f'沿用 {len(reuse)} 个技能：' + '、'.join(reuse) + '。'
                if skipped:
                    message += f'跳过 {len(skipped)} 个同名但内容不同的技能：' + '、'.join(skipped) + '。可到技能页粘贴仓库链接核对。'
                if failed:
                    message += '未完成：' + '；'.join(r['name'] + '：' + r['error'] for r in failed) + '。'
                tag = None
                if len(installed) >= 2:
                    name = item['title']
                    if name == parameters['repository'] and len(name) > 20:
                        name = name.rsplit('/', 1)[-1]
                    name = name.strip()[:20].strip()
                    warning = team.tag_pack(self.home, installed, name)
                    if warning: message += ' ' + warning
                    else:
                        tag = next((t for t in tags.read(self.home).get(installed[0], [])
                                    if t.casefold() == tags.clean_tag(name).casefold()), None)
                ids = result.get('operations', [])
                return {'status': 'done' if (installed or reuse) and not failed else 'failed', 'message': self.job_message(message),
                        'installed': installed, 'reuse': reuse, 'skipped': skipped, 'skills': installed + reuse,
                        'completed': installed, 'failed': failed, 'remaining': [r['name'] for r in failed],
                        **({'tag': tag} if tag else {}),
                        'operation': ids[0] if len(ids) == 1 else None, 'operations': ids}
            finally:
                rmtree(review['temporary'], ignore_errors=True)
        else:
            result = team.install(self.home, parameters['key'])
            if item.get('pack'):
                warning = team.tag_pack(self.home, item['_pack_skills'], item['pack_title'])
                if warning: result['message'] += ' ' + warning
            return {'message': result['message'], 'installed': result['installed'], 'reuse': result['reuse'],
                    **({'skills': sorted(set(result['installed'] + result['reuse']))} if result['installed'] else {}),
                    **({'operation': result['id']} if 'id' in result else {}),
                    'operations': [result['id']] if 'id' in result else []}
        return {'message': result['message'], 'operations': [result['id']] if 'id' in result else []}

    def auto_check_program(self):
        with self.program_lock:
            progress = self.program_progress.copy()
            checked_at = progress.get('checked_at')
            elapsed = time.time() - checked_at if checked_at is not None else PROGRAM_AUTO_CHECK_INTERVAL
            if (not preview_allowed(self.home) or progress['running'] or progress['phase'] in {'available', 'ready'}
                    or 0 <= elapsed < PROGRAM_AUTO_CHECK_INTERVAL):
                return progress
        return self.start_program('check', automatic=True)

    def start_program(self, action, target=None, automatic=False):
        with self.program_lock:
            if self.program_progress['running']:
                return self.program_progress.copy()
            if self.program_progress['phase'] == 'ready':
                if automatic:
                    return self.program_progress.copy()
                raise ValueError('新版已安装，请先刷新页面使新版生效，再检查更新。')
            if action == 'apply' and (not self.program_review or target != self.program_review['target'] or
                                      not self.program_review['available']):
                raise ValueError('请先检查 SP 更新，再升级。')
            self.program_action = action
            self.static_refresh_deferred = False
            self.program_progress.update(running=True, phase=action, error='', error_id='', automatic=automatic,
                failed_action='',
                message='正在下载并检查新版…' if action == 'check' else '正在升级，等当前操作完成…')
            self.program_thread = threading.Thread(target=self.program_work, args=(action, automatic), daemon=True)
            self.program_thread.start()
            return self.program_progress.copy()

    def _reset_stale_program_check(self, build):
        self.running_version = build['version']
        app_version.set_running_version(build['version'], date=build.get('date', ''))
        diagnostics.sync_running_build()
        if self.program_review:
            rmtree(self.program_review['temporary'], ignore_errors=True)
        self.program_review = None
        self.program_progress.update(
            running=False, phase='idle', available=False, error='', error_id='', failed_action='',
            current=build.get('date', ''), latest='',
            current_version=build['version'], latest_version='', pending_version='',
            changes=[], changes_total=0, changes_partial=False,
            message='从 GitHub 官方项目获取新版',
        )
        self.program_progress.pop('checked_at', None)
        self.program_progress.pop('target', None)

    def program_work(self, action, automatic=False):
        update_payload = {}
        try:
            if action == 'check':
                if self.program_review:
                    rmtree(self.program_review['temporary'], ignore_errors=True)
                self.program_review = None
                self.program_review = self_update.check(self.home)
                review = self.program_review
                current_version = (self.running_version or review.get('current_version')
                                   or app_version.format_version(review.get('current', ''),
                                                                 review.get('before', {}).get('revision', '')))
                latest_version = (review.get('latest_version')
                                  or app_version.format_version(review.get('latest', ''), review.get('target', '')))
                update_payload = dict(
                    phase='available' if review['available'] else 'latest',
                    target=review['target'], available=review['available'],
                    current=review['current'], latest=review['latest'],
                    current_version=current_version, latest_version=latest_version, pending_version='',
                    failed_action='',
                    changes=review.get('changes', []) if review['available'] else [],
                    changes_total=review.get('changes_total', 0) if review['available'] else 0,
                    changes_partial=review.get('changes_partial', False) if review['available'] else False,
                    message='有新版，可以一键升级' if review['available'] else 'SP 已是最新版本',
                )
            else:
                while not self.closing:
                    with self.runtime_lock:
                        idle = not self.active_requests and not (
                            any(t and t.is_alive() for t in (self.update_thread, self.labels_thread)) or
                            self.labels_progress['running'] or
                            any(r['running'] for r in list(self.update_reviews.values())) or
                            self.jobs_busy())
                    if idle:
                        break
                    time.sleep(.05)
                if self.closing:
                    return
                with self.program_lock:
                    self.program_progress['message'] = '正在安装 SP 新版…'
                result = self_update.apply(self.program_review, self.home)
                self.program_review['available'] = False
                pending_version = (result.get('pending_version')
                                   or result.get('latest_version')
                                   or self.program_review.get('latest_version')
                                   or self.program_progress.get('latest_version')
                                   or app_version.format_version(result.get('latest', self.program_review.get('latest', '')),
                                                                 self.program_review.get('target', '')))
                update_payload = {
                    **result,
                    'phase': 'ready', 'message': '新版已安装，刷新后生效',
                    'current_version': self.running_version,
                    'latest_version': pending_version,
                    'pending_version': pending_version,
                    'failed_action': '',
                    'changes': [], 'changes_total': 0, 'changes_partial': False,
                }
        except Exception as error:
            captured = self_update.record_failure(self.home, action, error) or {}
            update_payload = {'changes': [], 'changes_total': 0, 'changes_partial': False, 'pending_version': ''}
            if action == 'check':
                update_payload['latest_version'] = ''
            if captured:
                update_payload['error_id'] = captured['error_id']
            if automatic:
                update_payload.update(phase='idle', available=False, error='', failed_action='', message='还没查到新版信息')
            else:
                update_payload.update(
                    phase='error', available=False, failed_action=action,
                    error=str(error) if isinstance(error, ValueError) else 'Skill Pilot 更新没有完成，原因未知。请稍后重试，或点击「反馈问题」查看诊断。',
                    message='SP 更新没有完成',
                )
        finally:
            deferred = False
            try:
                with self.program_lock:
                    if update_payload:
                        self.program_progress.update(update_payload)
                    if action == 'check':
                        self.program_progress['checked_at'] = time.time()
                    if action == 'check' and self.static_refresh_deferred:
                        self.static_refresh_deferred = False
                        deferred = True
                    else:
                        self.program_action = ''
                        self.program_progress['running'] = False
                if deferred:
                    try:
                        refreshed = app_version.read_build()
                    except Exception:
                        refreshed = None
                    with self.program_lock:
                        if refreshed and refreshed.get('version') and refreshed['version'] != self.running_version:
                            self._reset_stale_program_check(refreshed)
                        self.program_action = ''
                        self.program_progress['running'] = False
            finally:
                with self.program_lock:
                    self.static_refresh_deferred = False
                    self.program_action = ''
                    self.program_progress['running'] = False

    def request_reload(self):
        changed = runtime.version() != self.code_version
        with self.runtime_lock:
            if not changed:
                self.reload_pending = False
                self.reload_error = ''
                self.reload_error_id = ''
                with self.program_lock:
                    if self.program_progress['phase'] == 'ready':
                        applied = (self.program_progress.get('pending_version')
                                   or app_version.read_build().get('version', '')
                                   or self.running_version)
                        self.running_version = applied
                        app_version.set_running_version(applied, date=self.program_progress.get('latest', ''))
                        diagnostics.sync_running_build()
                        self.program_progress.update(phase='latest', message='SP 已升级到最新版',
                                                     current_version=applied,
                                                     latest_version=applied or self.program_progress.get('latest_version', ''),
                                                     pending_version='', failed_action='',
                                                     changes=[], changes_total=0, changes_partial=False)
                    elif self.program_progress['running'] and (self.program_action == 'check' or self.program_progress['phase'] == 'check'):
                        self.static_refresh_deferred = True
                    elif not self.program_progress['running'] and self.program_action != 'apply' and self.program_progress['phase'] != 'apply':
                        refreshed = app_version.read_build()
                        if refreshed['version'] and refreshed['version'] != self.running_version:
                            self._reset_stale_program_check(refreshed)
            if changed and self.reload_callback and not self.reload_pending:
                self.reload_pending = True
                self.reload_error = ''
                self.reload_error_id = ''
                self.reload_thread = threading.Thread(target=self.reload_when_idle, daemon=True)
                self.reload_thread.start()
            return {'supported': bool(self.reload_callback), 'changed': changed,
                    'version': self.code_version, 'busy': self.active_requests > 1 or self.background_busy()}

    def reload_when_idle(self):
        while not self.closing:
            with self.runtime_lock:
                if not self.reload_pending:
                    return
                if not self.active_requests and not self.background_busy():
                    try:
                        self.reload_callback()
                    except Exception as error:
                        self.reload_error = str(error) if isinstance(error, ValueError) else '新版没能应用，当前程序保持运行，请稍后刷新重试。'
                        captured = self_update.record_failure(self.home, 'apply', error) or {}
                        self.reload_error_id = captured.get('error_id', '')
                    finally:
                        self.reload_pending = False
                    return
            time.sleep(.05)

    def watch(self):
        while not self.closing and (time.monotonic() - self.last_heartbeat < (OPEN_PAGE_TIMEOUT if self.pages else HEARTBEAT_TIMEOUT) or self.background_busy() or self.reload_pending):
            time.sleep(0.1)
        if not self.closing:
            self.shutdown()

    def agents_view(self):
        with self.agents_lock:
            generation = self.agents_generation
            cached = self.agents_cache
            findings = self.agent_findings['candidates']
        if cached is not None and cached[0] == generation:
            result = cached[1]
        else:
            result = agent_management.overview(self.home, discovered=findings)
            with self.agents_lock:
                if self.agents_generation == generation:
                    self.agents_cache = (generation, result)
        with self.agents_lock:
            changed = self.agents_generation != generation
            progress = self.agents_progress.copy()
        return self.agents_view() if changed else {**result, 'discovery': progress}

    def invalidate_agents(self):
        with self.agents_lock:
            self.agents_generation += 1
            self.agents_cache = None

    def start_agents_scan(self, full=False):
        with self.agents_lock:
            if self.agents_progress['running']:
                return self.agents_progress.copy()
            self.agents_progress.update(running=True, error='', error_id='',
                message='正在扫描 agent…' if full else '正在检查新安装的 agent…')
            self.agents_thread = threading.Thread(target=self.scan_agents, args=(full,), daemon=True)
            self.agents_thread.start()
            return self.agents_progress.copy()

    def scan_agents(self, full):
        try:
            with self.agents_lock:
                previous = self.agent_findings['candidates']
            findings = agent_discovery.find(self.home, full, previous)
            agent_discovery.save(self.home, findings)
            local = agent_management.profiles.state(self.home)
            unavailable = set(local['onboarded']) | set(local.get('ignored', []))
            current = {p['id'] for p in findings['candidates']}
            added = current - {p['id'] for p in previous} - unavailable
            pending = current - unavailable
            if added:
                message = f'发现 {len(added)} 个新 agent'
            elif pending:
                message = '没有更多新的 agent'
            else:
                message = '检查完成，没有发现新的 agent'
            with self.agents_lock:
                self.agent_findings = findings
                self.agents_generation += 1
                self.agents_cache = None
                self.agents_progress.update(checked_at=findings['checked_at'], message=message)
        except Exception as error:
            self.agents_progress.update(diagnostics.failure(self.home, 'backend', '扫描 Agent', error))
            with self.agents_lock:
                self.agents_progress.update(error=str(error) if isinstance(error, ValueError)
                    else 'agent 扫描没有完成，已保留上次名单，请重试。')
        finally:
            with self.agents_lock:
                self.agents_progress['running'] = False


class Handler(BaseHTTPRequestHandler):
    def parse_request(self):
        if not super().parse_request():
            return False
        # Unused browser connections must not delay applying a new program.
        with self.server.runtime_lock:
            self.server.active_requests += 1
            self.counted_request = True
        return True

    def handle_one_request(self):
        self.counted_request = False
        self.request_validation = False
        try:
            super().handle_one_request()
        except Exception as error:
            self.reply(500, {'error': '操作没有完成，可在设置中查看诊断记录。'})
        finally:
            if self.counted_request:
                with self.server.runtime_lock:
                    self.server.active_requests -= 1

    def log_message(self, *args):
        # URLs contain a session secret; never put them in access logs.
        pass

    def authorized(self):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        token = self.headers.get("X-Skill-Pilot-Token") or query.get("token", [""])[0]
        return secrets.compare_digest(token, self.server.token)

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        error = sys.exception()
        if isinstance(body, dict) and body.get('error') and status >= 400 and error is not None and not self.request_validation:
            route = urllib.parse.urlsplit(self.path).path.split('/')
            action = {'table': '读取技能名单', 'scan': '盘点技能', 'settings': '保存设置',
                      'team': '团队商店', 'updates': '技能更新', 'jobs': '执行任务',
                      'program': '程序升级', 'agents': '扫描 Agent', 'assignment': '分配技能',
                      'migration': '整理技能', 'cleanup': '清理技能', 'install': '安装技能',
                      'diagnostics': '生成诊断报告', 'log': '读取操作记录', 'backups': '读取备份'
                      }.get(route[2] if len(route) > 2 else '', '处理看板操作')
            captured = diagnostics.failure(self.server.home, 'backend', action, error)
            if captured:
                message = (diagnostics.sanitize(body['error'], self.server.home, 400)
                           if isinstance(error, ValueError) and re.search(r'[\u4e00-\u9fff]', body['error'])
                           else '操作没有完成，可在设置中查看诊断记录。')
                body = {**body, **captured, 'error': message}
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def serve_deliverable(self, path):
        """A demo runs in a sandboxed frame inside a page of ours. The sandbox gives its scripts an opaque
        origin, so they cannot read this dashboard, its token or other tabs; the frame page only lets the
        frame load this demo's own files, so the demo cannot navigate itself to another site either."""
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            self.reply(403, {"error": "此页面只允许本机本次打开的浏览器访问。"})
            return
        match = re.fullmatch(r'/d/([0-9a-f]{32})/([0-9a-f]{32})/(' + team.team_hub.DELIVERABLE_ID + r')/(?:f/(.+))?', path)
        if not match or not secrets.compare_digest(match[1], self.server.deliverable_key(match[2], match[3])):
            self.reply(404, {"error": "没有这个页面。"})
            return
        frame = f'/d/{match[1]}/{match[2]}/{match[3]}/'
        files = self.server.origin + frame + 'f/'
        if match[4] is None:
            self.send_demo_page(files)
            return
        if self.headers.get('Sec-Fetch-Dest') == 'document':
            # Opened on its own as a page, a demo file would run outside the frame: send it back in.
            self.send_response(303)
            self.send_header('Location', frame)
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        failure = None
        try:
            response = team.open_deliverable_file(self.server.home, match[2], match[3],
                                                  urllib.parse.unquote(match[4]), self.headers.get('Range'))
        except team.RequestError as error:
            failure = (error.status if error.status in {401, 404, 416} else 502, str(error))
        except (ValueError, OSError) as error:
            failure = (502, str(error) if isinstance(error, ValueError) else '没能读取成品文件，请稍后重试。')
        if failure:
            # Outside the except block: a missing asset is a page problem, not a dashboard failure to log.
            self.reply(failure[0], {'error': failure[1]})
            return
        with response:
            self.send_response(response.status)
            for name in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-DNS-Prefetch-Control', 'off')
            # Fonts and module scripts from the sandboxed page arrive with Origin "null"; the
            # unguessable per-session address is what protects these files.
            self.send_header('Access-Control-Allow-Origin', '*')
            # Everything, nested frames included, stays within this demo's own files.
            self.send_header('Content-Security-Policy',
                             f"sandbox allow-scripts; default-src {files} data: blob:; "
                             f"script-src {files} 'unsafe-inline'; style-src {files} 'unsafe-inline'; "
                             f"img-src {files} data: blob:; media-src {files} blob:; font-src {files} data:; "
                             f"connect-src {files}; form-action 'none'; base-uri {files}")
            self.end_headers()
            try:
                while chunk := response.read(1024 * 1024):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Videos are cancelled when scrolled away; a viewer leaving mid-file is normal.
                self.close_connection = True

    def send_demo_page(self, files):
        """The tab a demo opens in. Its frame may load only this demo's files: a script, link or refresh
        that points the demo anywhere else is refused, and the sandbox stops popups, forms and the tab itself."""
        body = DEMO_PAGE.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-DNS-Prefetch-Control', 'off')
        self.send_header('Content-Security-Policy', f"default-src 'none'; style-src 'unsafe-inline'; frame-src {files}; "
                                                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        # 浏览器会自己来要 /favicon.ico，不带本次口令。只放行这一个地址，避免控制台报错。
        if path == "/favicon.ico":
            self.reply(204, b"")
            return
        if path.startswith('/d/'):
            self.serve_deliverable(path)
            return
        if not self.authorized():
            self.reply(403, {"error": "此页面只允许本机本次打开的浏览器访问。"})
            return
        if path == '/api/diagnostics/errors':
            rows = diagnostics.recent(self.server.home)
            self.reply(200, [{k: v for k, v in row.items() if k != 'detail'} for row in rows])
        elif path == "/api/scan":
            with self.server.inventory_lock:
                if self.server.inventory.get('deferred'):
                    self.server.inventory = scan(self.server.home)
            self.reply(200, self.server.inventory)
        elif path == '/api/settings':
            self.reply(200, {**labels.public_settings(self.server.home),
                             'extra_sources': extra_sources.paths(self.server.home)})
        elif path == '/api/labels/progress':
            self.reply(200, self.server.labels_progress.copy())
        elif path == '/api/program/progress':
            self.reply(200, {**self.server.program_progress, 'write_allowed': preview_allowed(self.server.home)})
        elif path == '/api/config/export':
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.reply(200, sharing.export_config(self.server.home, query.get('agent', [None])[0]))
            except (ValueError, OSError, KeyError) as error:
                self.reply(409, {'error': str(error)})
        elif path == '/api/team/queue':
            self.reply(200, self.server.team_queue_status())
        elif path == '/api/team/deliverables/detail':
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            match = re.fullmatch(r'deliverable:([0-9a-f]{32}):(' + team.team_hub.DELIVERABLE_ID + ')', query.get('key', [''])[0])
            if not match:
                self.reply(400, {'error': '请从团队页的成品卡片打开。'})
                return
            try:
                detail = team.deliverable_detail(self.server.home, match[1], match[2])
            except (ValueError, OSError) as error:
                self.reply(409, {'error': str(error) if isinstance(error, ValueError) else '没能读取成品，请稍后重试。'})
                return
            key = self.server.deliverable_key(match[1], match[2])
            self.reply(200, {**detail, 'base_url': f'/d/{key}/{match[1]}/{match[2]}/f/'})
        elif path == '/api/tools/check':
            # Find known command entry points, including desktop installs; never run them.
            names = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('names', [''])[0].split(',')
            if (not 1 <= len(names) <= 30 or
                    any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}', name) for name in names)):
                self.reply(400, {'error': '工具名只能用字母、数字、点、加号、横线或下划线。'})
                return
            tool_path = tool_environment(self.server.home)['PATH']
            found = {}
            for name in names:
                candidates = ('playwright', 'playwright-cli') if name == 'playwright' else (name,)
                found[name] = any(shutil.which(candidate, path=tool_path) is not None for candidate in candidates)
            self.reply(200, found)
        elif path == '/api/jobs':
            self.reply(200, self.server.jobs_status())
        elif path == '/api/team/github/digests':
            self.reply(200, self.server.github_digests.status())
        elif path == '/api/team/github/detail':
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.reply(200, self.server.github_digests.detail(query.get('repository', [''])[0]))
            except github_digest.RepositoryMissing as error:
                self.reply(404, {'error': str(error)})
            except (ValueError, OSError) as error:
                self.reply(409, {'error': str(error) if isinstance(error, ValueError) else '没能读取仓库原文，请稍后重试。'})
        elif path in {'/api/team', '/api/team/catalog', '/api/team/listings/candidates'}:
            try:
                result = (team.status(self.server.home, strict=False) if path == '/api/team' else
                          team.catalog(self.server.home) if path == '/api/team/catalog' else
                          team.listing_candidates(self.server.home))
                if path == '/api/team/catalog':
                    result = self.server.github_digests.catalog(result)
                    for row in result.get('deliverables', []):
                        member, identity = row['member']['id'], row['id']
                        row['open_url'] = f'/d/{self.server.deliverable_key(member, identity)}/{member}/{identity}/'
                self.reply(200, result)
                if path == '/api/team/catalog':
                    self.server.github_digests.start()
            except (ValueError, OSError) as error:
                self.reply(409, {'error': str(error) if isinstance(error, ValueError) else '没能读取团队分享，请稍后重试。'})
        elif path == '/api/updates/progress':
            with self.server.update_lock:
                review = next(reversed(self.server.update_reviews.values()), None)
                self.reply(200, updates.public(review) if review else None)
        elif path in ('/api/table', '/api/log', '/api/agents', '/api/cleanup'):
            try:
                result = (operations.table(self.server.home) if path == '/api/table' else
                          self.server.agents_view() if path == '/api/agents' else
                          cleanup.list_withdrawn(self.server.home) if path == '/api/cleanup' else operations.history(self.server.home))
                self.reply(200, result)
            except (ValueError, OSError, KeyError) as error:
                self.reply(409, {'error': str(error)})
        elif path == "/api/status":
            self.reply(200, {"pid": os.getpid(), "read_only": not preview_allowed(self.server.home),
                             'version': self.server.code_version, 'reloading': self.server.reload_pending,
                             'reload_error': self.server.reload_error, 'error_id': self.server.reload_error_id})
        elif path in ("/api/migration/plan", "/api/backups", "/api/migration/compare"):
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                agents = query.get("agents", [None])[0]
                if path == "/api/backups":
                    self.reply(200, backups(self.server.home))
                    return
                plan = preview(self.server.home, agents.split(",") if agents is not None else None)
                if path == "/api/migration/plan":
                    self.reply(200, plan)
                    return
                if query.get("plan_id", [""])[0] != plan["plan_id"]:
                    raise ValueError("目录内容已经变了，请重新预览。")
                skill = next(s for s in plan["skills"] if s["name"] == query.get("name", [""])[0])
                baseline = next(o for o in skill["options"] if o["id"] == skill["default"])
                texts = []
                for option in skill["options"]:
                    if option == baseline:
                        continue
                    texts.append(compare_files(Path(baseline["path"]), Path(option["path"])))
                self.reply(200, {"comparison": "\n".join(texts)})
            except (ValueError, OSError, StopIteration) as error:
                self.reply(409, {"error": str(error) or "这个技能已不在预览里，请重新预览。"})
        elif path in ("/", "/app.js", "/diagnostics.js", "/app.css", "/favicon.svg",
                      "/vendor/gsap.min.js", "/vendor/ScrollTrigger.min.js"):
            file = STATIC / ("index.html" if path == "/" else path.removeprefix("/"))
            body = file.read_bytes().replace(b"__SESSION_TOKEN__", self.server.token.encode())
            kind = {"html": "text/html", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}[file.suffix.removeprefix(".")]
            self.reply(200, body, kind + "; charset=utf-8")
        else:
            self.reply(404, {"error": "没有这个页面。"})

    def do_POST(self):
        if not self.authorized() or self.headers.get("Origin") != self.server.origin:
            self.reply(403, {"error": "请求来源不属于本次看板。"})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in {'/api/diagnostics/bundle', '/api/diagnostics/client-error',
                    '/api/diagnostics/preview', '/api/diagnostics/submit'}:
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    self.reply(413, {'error': '诊断内容太长，请缩短后重试。'})
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or any(not isinstance(v, str) for v in body.values()):
                    raise ValueError('诊断内容格式不正确。')
                if path.endswith('/submit') and not preview_allowed(self.server.home):
                    self.reply(403, {'error': '只读模式不能发送反馈，请复制或导出诊断信息。'})
                    return
                pending_version = (self.server.program_progress.get('pending_version', '')
                                   if self.server.program_progress.get('phase') == 'ready' else '')
                if path.endswith('/bundle'):
                    self.reply(200, diagnostics.bundle(self.server.home, body.get('error_id'), body.get('note', ''),
                                                       prefer_running=True, pending_version=pending_version))
                elif path.endswith('/preview'):
                    prepared = team.feedback_preview(self.server.home, body.get('error_id', ''), body.get('note', ''),
                                                     prefer_running=True, pending_version=pending_version)
                    ticket = secrets.token_urlsafe(24)
                    with self.server.feedback_lock:
                        self.server.feedback_previews = {k: v for k, v in self.server.feedback_previews.items()
                                                        if v['expires'] > time.monotonic()}
                        if len(self.server.feedback_previews) >= 100:
                            self.server.feedback_previews.pop(next(iter(self.server.feedback_previews)))
                        self.server.feedback_previews[ticket] = {'expires': time.monotonic() + 600, 'prepared': prepared}
                    self.reply(200, {k: v for k, v in prepared.items() if k != '_connection'} | {'ticket': ticket})
                elif path.endswith('/submit'):
                    with self.server.feedback_lock:
                        entry = self.server.feedback_previews.get(body.get('ticket'))
                        if not entry or entry['expires'] <= time.monotonic():
                            raise ValueError('预览已过期，请重新打开反馈窗口。')
                        result = team.submit_feedback(self.server.home, entry['prepared'])
                        del self.server.feedback_previews[body['ticket']]
                    self.reply(200, result)
                elif not preview_allowed(self.server.home):
                    self.reply(403, {'error': '当前处于只读模式，错误记录未写入。'})
                else:
                    # A fixed action and a bounded message; never accept browser state or HTML.
                    error = RuntimeError(body.get('message', '')[:4000])
                    if body.get('error_type') in {'Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError'}:
                        error.diagnostic_type = body['error_type']
                    if re.fullmatch(r'(?:app|diagnostics)\.js:\d{1,6}:\d{1,6}', body.get('location', '')):
                        error.diagnostic_location = body['location']
                    row = diagnostics.record(self.server.home, 'frontend', '页面脚本',
                        error, '页面出了点问题，请刷新重试。', force=True)
                    self.reply(200, {'error_id': row.get('error_id', '')})
            except (ValueError, OSError) as error:
                message = (diagnostics.sanitize(str(error), self.server.home, 400)
                           if path.endswith('/submit') else '诊断内容格式不正确，或记录已过期。')
                self.request_validation = True
                self.reply(400, {'error': message})
            return
        if path in {'/api/heartbeat', '/api/runtime/refresh'}:
            # Consume small optional bodies before closing the HTTP/1.0
            # connection; unread request bytes can reset the reply on Windows.
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 1024 or len(self.rfile.read(length)) != length:
                    raise ValueError()
            except (ValueError, OSError):
                self.reply(400, {'error': '请求内容不正确，请刷新后重试。'})
                return
        if path == "/api/heartbeat":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            page = query.get("page", [""])[0]
            if page:
                (self.server.pages.discard if query.get("closing") else self.server.pages.add)(page)
            self.server.last_heartbeat = time.monotonic()
            self.reply(200, {"ok": True})
            return
        if path == '/api/runtime/refresh':
            try:
                self.reply(200, self.server.request_reload())
            except OSError:
                self.reply(409, {'error': '新版还没有准备完整，请稍后刷新重试。'})
            return
        if self.server.reload_pending:
            self.reply(503, {'error': '正在应用新版，请稍候再操作。'})
            return
        if (path != '/api/program/auto-check' and self.server.program_progress['running']
                and self.server.program_progress['phase'] == 'apply'):
            self.reply(503, {'error': '正在升级 SP，请完成后刷新页面再操作。'})
            return
        read_routes = {'/api/assignment/preview', '/api/absorb/preview', '/api/cleanup/preview', '/api/cleanup/delete/preview', '/api/install/inspect', '/api/incoming/compare',
                       '/api/updates/check', '/api/updates/explain', '/api/updates/merge', '/api/updates/calibrate',
                       '/api/agents/join/preview', '/api/agents/leave/preview', '/api/agents/ignore/preview', '/api/config/preview',
                       '/api/agents/scan', '/api/team/listings/preview', '/api/program/auto-check'}
        write_routes = {'/api/assignment/apply', '/api/dependencies', '/api/install/apply', '/api/absorb', '/api/undo',
                        '/api/updates/apply', '/api/updates/watch', '/api/cleanup/apply', '/api/cleanup/delete/apply', '/api/withdrawn/restore',
                        '/api/agents/join/apply', '/api/agents/leave/apply', '/api/agents/ignore/apply', '/api/config/apply',
                        '/api/team/listings/apply', '/api/team/listings/remove', '/api/team/listings/update', '/api/team/queue', '/api/team/queue/clear',
                        '/api/team/ratings', '/api/team/ratings/remove', '/api/team/packs', '/api/team/github/retry'}
        job_routes = {'/api/jobs', '/api/jobs/retry', '/api/jobs/clear'}
        write_routes |= job_routes
        read_routes.add('/api/external/preview')
        write_routes.add('/api/external/release')
        settings_routes = {'/api/settings/llm', '/api/settings/llm/test', '/api/labels/run',
                           '/api/settings/extra-sources', '/api/tags', '/api/team/settings'}
        program_routes = {'/api/program/check', '/api/program/apply'}
        if path.startswith(('/api/team/shares', '/api/team/upload/', '/api/team/import/')) or path == '/api/team/withdraw':
            self.reply(404, {'error': '没有这个团队商店操作。'})
            return
        if path not in {"/api/migration/apply", "/api/restore"} | read_routes | write_routes | settings_routes | program_routes:
            self.reply(405, {"error": "当前阶段没有这个操作。"})
            return
        try:
            self.request_validation = True
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("请求内容不完整或太长，请重新打开核对页。")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求内容格式不正确。')
            except ValueError:
                if path in job_routes:
                    raise JobInputError('请求内容格式不正确，请逐项填写任务参数。') from None
                if path == '/api/team/github/retry':
                    raise ValueError('请求内容格式不正确，请重新选择仓库后重试。') from None
                raise
            self.request_validation = False
            if path in job_routes:
                if not preview_allowed(self.server.home):
                    self.reply(403, {'error': '当前处于只读模式，任务没有加入队列，文件没有改动。'})
                    return
                result = (self.server.queue_job(body) if path == '/api/jobs' else
                          self.server.retry_job(body) if path == '/api/jobs/retry' else self.server.clear_jobs())
                self.reply(200, result)
                return
            if path.startswith('/api/team/'):
                if path not in read_routes and not preview_allowed(self.server.home):
                    self.reply(403, {'error': '当前处于只读模式，分享和本机文件没有改动。'})
                    return
                self.reply(200, self.team_post(path, body))
                return
            if path == '/api/agents/scan':
                self.reply(200, self.server.start_agents_scan(full=True))
                return
            if path == '/api/program/auto-check':
                self.reply(200, {**self.server.auto_check_program(), 'write_allowed': preview_allowed(self.server.home)})
                return
            if path in program_routes:
                if path.endswith('apply') and body.get('confirmed') is not True:
                    self.reply(400, {'error': '请点击“一键升级”确认这次升级。'})
                    return
                if not preview_allowed(self.server.home):
                    self.reply(403, {'error': '当前处于只读模式，程序没有改动。' if path.endswith('apply')
                                              else '当前处于只读模式，不能检查或下载新版程序。'})
                    return
                with self.server.program_lock:
                    if self.server.program_progress['phase'] == 'ready':
                        self.reply(409, {'error': '新版已安装，请先刷新页面使新版生效，再检查更新。',
                                         'program': {**self.server.program_progress, 'write_allowed': True}})
                        return
                result = self.server.start_program(path.rsplit('/', 1)[1], body.get('target'))
                self.reply(200, result)
                return
            if path in settings_routes:
                if not preview_allowed(self.server.home):
                    self.reply(403, {'error': '当前处于只读模式，文件没有改动。'})
                    return
                if path == '/api/settings/llm':
                    saved = labels.save_settings(self.server.home, body)
                    self.server.invalidate_agents()
                    self.reply(200, saved)
                elif path == '/api/settings/llm/test':
                    self.reply(200, labels.test(self.server.home))
                elif path == '/api/tags':
                    result = tags.update(self.server.home, body)
                    self.reply(200, result)
                elif path == '/api/settings/extra-sources':
                    folder = body.get('path', '')
                    if not isinstance(folder, str):
                        raise ValueError('请填写一个文件夹。')
                    with self.server.local_write():
                        message = (extra_sources.remove(self.server.home, folder) if body.get('remove') is True
                                   else extra_sources.add(self.server.home, folder))
                        self.server.inventory = scan(self.server.home)
                    self.reply(200, {'message': message, 'extra_sources': extra_sources.paths(self.server.home)})
                else:
                    mode = body.get('mode')
                    if mode not in ('missing', 'all'):
                        raise ValueError('请选择补齐中文名或重新整理全部。')
                    with self.server.labels_lock:
                        if self.server.labels_progress['running']:
                            raise ValueError('正在给技能起中文名，请等这次完成。')
                        if not labels.settings(self.server.home).get('api_key'):
                            raise ValueError('请先到设置里填好 AI 服务的 key。')
                        self.server.labels_progress.update(running=True, done=0, total=0, message='正在准备技能名单…', error='')
                        def run_labels():
                            try:
                                labels.run(self.server.home, mode, self.server.labels_progress)
                            finally:
                                self.server.invalidate_agents()
                        self.server.labels_thread = threading.Thread(target=run_labels, daemon=True)
                        self.server.invalidate_agents()
                        self.server.labels_thread.start()
                        self.reply(200, self.server.labels_progress.copy())
                return
            if path == '/api/updates/check':
                with self.server.update_lock:
                    review = next(reversed(self.server.update_reviews.values()), None)
                    if review is None or not review['running']:
                        review = updates.create(self.server.home)
                        self.server.update_reviews[review['id']] = review
                        self.server.update_thread = threading.Thread(target=updates.check, kwargs={'review': review, 'lock': self.server.update_lock}, daemon=True)
                        self.server.update_thread.start()
                    result = updates.public(review)
                self.server.prune_update_reviews()
                self.reply(200, result)
                return
            if path in {'/api/updates/explain', '/api/updates/merge', '/api/updates/calibrate'}:
                with self.server.update_review(body['review_id']) as review:
                    action = updates.explain if path.endswith('explain') else updates.calibrate if path.endswith('calibrate') else updates.prepare_merge
                    result = action(review, body['name'])
                self.reply(200, result)
                return
            if path == '/api/assignment/preview':
                self.reply(200, self.server.preview_assignment(body['changes'], body.get('recheck') is True))
                return
            if path == '/api/config/preview':
                self.reply(200, sharing.preview_import(body.get('config'), body.get('agents'), self.server.home))
                return
            if path == '/api/cleanup/preview':
                self.reply(200, cleanup.preview(body.get('skills'), self.server.home))
                return
            if path == '/api/external/preview':
                from .. import external
                self.reply(200, external.preview(body.get('name'), self.server.home))
                return
            if path == '/api/cleanup/delete/preview':
                self.reply(200, cleanup.delete_preview(body.get('skills'), self.server.home))
                return
            if path == '/api/agents/join/preview':
                self.reply(200, agent_management.preview_join(body['agents'], self.server.home))
                return
            if path in {'/api/agents/leave/preview', '/api/agents/ignore/preview'}:
                action = agent_management.preview_leave if path.endswith('leave/preview') else agent_management.preview_ignore
                self.reply(200, agent_management.public(action(body['agent'], self.server.home)))
                return
            if path == '/api/install/inspect':
                review = github.inspect(body['url'])
                self.server.install_reviews[review['id']] = review
                registry = operations.load(self.server.home)[1]
                self.reply(200, {**review, 'conflicts': operations.conflicts(review['skills'], registry, self.server.home)})
                return
            if path == '/api/absorb/preview':
                self.reply(200, preview_absorb(self.server.home, body.get('selected')))
                return
            if path == '/api/incoming/compare':
                existing = self.server.home / '.skill-pilot/store' / body['name']
                if body.get('review_id'):
                    review = self.server.install_reviews[body['review_id']]
                    incoming = Path(next(s['path'] for s in review['skills'] if s['name'] == body['name']))
                else:
                    home, registry, agents = operations.load(self.server.home)
                    incoming = Path(next(s['path'] for s in operations.external_skills(home, agents, registry)
                                         if s['relative'] == body['relative'] and s['name'] == body['name']))
                github.safe_skill(incoming)
                self.reply(200, {'comparison': compare_files(existing, incoming)})
                return
            if body.get("confirmed") is not True:
                if path == '/api/cleanup/apply':
                    try:
                        plan = cleanup.preview(body.get('skills'), self.server.home)
                    except ValueError as error:
                        self.reply(400, {'error': str(error)})
                        return
                    if plan['dependency_warnings'] or plan['path_warnings']:
                        self.reply(200, {'needs_confirmation': True, **plan})
                        return
                self.reply(400, {"error": "请先核对，并确认这次操作。"})
                return
            if not preview_allowed(self.server.home):
                self.reply(403, {"error": "当前处于只读模式，文件没有改动。"})
                return
            with self.server.local_write(restoring=path == '/api/restore'):
                if path in {'/api/cleanup/apply', '/api/cleanup/delete/apply'}:
                    try:
                        action = cleanup.delete_preview if path == '/api/cleanup/delete/apply' else cleanup.preview
                        action(body.get('skills'), self.server.home)
                    except ValueError as error:
                        self.reply(400, {'error': str(error)})
                        return
                if path in {'/api/agents/join/apply', '/api/agents/leave/apply', '/api/agents/ignore/apply', '/api/cleanup/apply', '/api/cleanup/delete/apply', '/api/config/apply'} and (not isinstance(body.get('revision'), str) or not body['revision']):
                    raise ValueError('请先读取当前状态，再确认这次操作。')
                if path == "/api/migration/apply":
                    result = apply(body["plan_id"], self.server.home, body["agents"], body.get("choices"), body.get("sources"))
                    operations.bootstrap(self.server.home)
                elif path == '/api/restore':
                    result = restore(body["backup_id"], self.server.home)
                    with self.server.jobs_lock:
                        self.server.jobs_blocked = ''
                        self.server.jobs_version += 1
                elif path == '/api/assignment/apply':
                    result = operations.apply_assign(body['changes'], body['revision'], True, self.server.home,
                                                     body.get('recheck') is True)
                elif path == '/api/config/apply':
                    result = sharing.import_config(body.get('config'), body.get('agents'), self.server.home, body['revision'])
                elif path == '/api/agents/join/apply':
                    result = agent_management.join(body['agents'], body['plan_id'], body['revision'], body.get('choices'), self.server.home)
                elif path == '/api/agents/leave/apply':
                    result = agent_management.leave(body['agent'], body['plan_id'], body['revision'], self.server.home)
                elif path == '/api/agents/ignore/apply':
                    result = agent_management.ignore(body['agent'], body['plan_id'], body['revision'], self.server.home)
                elif path == '/api/dependencies':
                    result = operations.decide_dependencies(body['decisions'], body['revision'], self.server.home)
                elif path == '/api/install/apply':
                    result = operations.install(self.server.install_reviews[body['review_id']], body['skills'],
                                                body.get('agents'), body['revision'], self.server.home)
                elif path == '/api/absorb':
                    result = operations.absorb(body.get('all', True), body.get('selected'), body['revision'], self.server.home,
                                               body.get('choices'))
                elif path == '/api/updates/watch':
                    result = updates.set_watch(body.get('name'), body.get('mode'), body.get('revision'), self.server.home, True)
                elif path == '/api/updates/apply':
                    with self.server.update_review(body['review_id']) as review:
                        result = updates.apply(review, body['skills'], body.get('choices'), True,
                                               body.get('merge_ids'), body.get('confirm_lark_cli', False))
                elif path == '/api/external/release':
                    from .. import external
                    if not body.get('revision') or not body.get('plan_id'):
                        raise ValueError('请先核对这次退出托管。')
                    result = external.release(body.get('name'), True, self.server.home, body['revision'], body['plan_id'])
                elif path == '/api/cleanup/apply':
                    result = cleanup.apply(body.get('skills'), True, self.server.home, body['revision'])
                elif path == '/api/cleanup/delete/apply':
                    result = cleanup.delete(body.get('skills'), True, self.server.home, body['revision'])
                elif path == '/api/withdrawn/restore':
                    result = cleanup.restore(body['skills'],self.server.home,body['revision'])
                else:
                    result = operations.undo(body['id'], body['revision'], self.server.home)
                    record = next((item for item in operations.history(self.server.home) if item['id'] == body['id']), None)
                    if record:
                        with self.server.update_lock:
                            for review in self.server.update_reviews.values():
                                updates.restore_arrows(review, record.get('skills'), body['id'])
                self.server.invalidate_agents()
                self.server.inventory = scan(self.server.home)
                self.reply(200, result)
        except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
            if isinstance(error, operations.RollbackError):
                self.server.stop_jobs()
            message = ('团队操作没能完成，请重新预览后重试。' if path.startswith('/api/team/') and not isinstance(error, ValueError) else
                       '设置没能保存，请核对后再试。' if path in settings_routes and not isinstance(error, ValueError) else str(error))
            self.reply(400 if isinstance(error, JobInputError) or path in {'/api/cleanup/preview', '/api/cleanup/delete/preview'} else 409, {
                "error": message or "操作没能完成，请重新预览。",
                **({'blocked': True} if path in job_routes and self.server.jobs_blocked else {}),
                **({'recheck': True} if isinstance(error, operations.AssignmentChanged) else {})})

    def team_post(self, path, body):
        home = self.server.home
        if path == '/api/team/listings/update':
            return self.server.queue_team_updates()
        if path == '/api/team/github/retry':
            return self.server.github_digests.retry(body.get('repository'))
        if path == '/api/team/settings':
            return team.save_settings(home, body, self.server.local_write())
        if path == '/api/team/listings/preview':
            saved, result = team.listings_preview(home, body.get('skills'), body.get('rejected'), body.get('packs'))
            review = secrets.token_urlsafe(16)
            self.server.team_upload_reviews[review] = saved
            return {**result, 'review': review}
        if path == '/api/team/queue':
            if 'github' in body:
                self.server.queue_github(body.get('github'))
                return self.server.team_queue_status()
            if 'pack' in body:
                return self.server.queue_team_pack(body.get('pack'), body.get('keys'))
            return self.server.queue_team(body.get('key'))
        if path == '/api/team/queue/clear':
            return self.server.clear_team_queue()
        if path == '/api/team/listings/remove':
            return team.listings_remove(home, body.get('names'))
        if path == '/api/team/packs':
            with self.server.local_write():
                return team.packs_apply(home, body.get('packs'))
        if path in {'/api/team/ratings', '/api/team/ratings/remove'}:
            return team.rate(home, body, path.endswith('/remove'))
        reviews = self.server.team_upload_reviews
        identifier = body.get('review')
        if not isinstance(identifier, str) or identifier not in reviews:
            raise ValueError('这份预览已失效，请重新预览。')
        review = reviews[identifier]
        result = team.listings_apply(home, review)
        reviews.pop(identifier, None)
        self.server.invalidate_agents()
        self.server.inventory = scan(home)
        return result


def preview_allowed(home):
    try:
        guard(home / ".skill-pilot", home)
        return True
    except ValueError:
        return False


def preview_absorb(home=None, selected=None):
    result = operations.absorb(all_skills=not selected, selected=selected, home=home, preview=True)
    for conflict in result.get('conflicts', []):
        left = Path(conflict['existing'])
        for option in conflict['options']:
            github.safe_skill(Path(option['path']))
            option['comparison'] = compare_files(left, Path(option['path'])) if option['path'] != str(left) else ''
    return result


def compare_files(left, right):
    one, two = normalization.files(left, normalize=False), normalization.files(right, normalize=False)
    result = [f"默认选择：{left}\n另一份：{right}\n"]
    for name in sorted(one.keys() | two.keys()):
        if one.get(name) == two.get(name):
            continue
        a, old_mode = one.get(name, (b'', None))
        b, new_mode = two.get(name, (b'', None))
        status = '新增' if name not in one else '删除' if name not in two else '改变'
        result.append(f"文件：{name}（{status}）" + ("（脚本或可执行文件）" if Path(name).suffix in {".py", ".js", ".sh"} else ""))
        if old_mode is not None and new_mode is not None and old_mode != new_mode:
            result.append('文件类型或执行权限不同。')
        try:
            result.extend(difflib.unified_diff(a.decode().splitlines(), b.decode().splitlines(), fromfile="默认选择", tofile="另一份", lineterm=""))
        except UnicodeError:
            result.append("此文件不是文本，内容不同。")
    return "\n".join(result)


def lock_path():
    identity = hashlib.sha256(str(target_home()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"skill-pilot-{user_id()}-{identity}.lock"


def open_existing(lock):
    # A simultaneous launcher may hold the lock just before writing its URL.
    for _ in range(50):
        lock.seek(0)
        try:
            state = json.loads(lock.read())
            url = urllib.parse.urlsplit(state["url"])
            if url.scheme != "http" or url.hostname != "127.0.0.1":
                return False
            request = urllib.request.Request(f"http://127.0.0.1:{url.port}/api/status?{url.query}")
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if json.load(response)["pid"] != state["pid"]:
                    return False
            webbrowser.open(state["url"])
            return True
        except (ValueError, KeyError, OSError, urllib.error.URLError):
            time.sleep(0.1)
    return False


def run(json_output=False):
    if os.name == 'nt':
        from .windows_runtime import run as windows_run
        return windows_run(json_output)
    resume = runtime.resume_state()
    fd = resume['lock_fd'] if resume else os.open(lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as lock:
        if os.name != "nt":
            os.fchmod(lock.fileno(), 0o600)
        if not resume:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if open_existing(lock):
                    print(json.dumps({"message": "已打开正在运行的看板。"}, ensure_ascii=False) if json_output else "已打开正在运行的看板。")
                    return
                raise SystemExit("已有看板正在启动或退出，请稍后再打开。")
            operations.bootstrap(target_home())
        server = Dashboard(resume['inventory'] if resume else {'home': str(target_home()), 'deferred': True}, resume)
        server.reload_callback = lambda: runtime.replace(server, lock)
        server.start_agents_scan()
        if not resume:
            lock.seek(0)
            lock.truncate()
            json.dump({"pid": os.getpid(), "url": server.url}, lock)
            lock.flush()
            server.auto_check_program()
        server.last_heartbeat = time.monotonic()
        threading.Thread(target=server.watch, daemon=True).start()
        if not resume:
            webbrowser.open(server.url)
            print(json.dumps({"message": "看板已打开；程序更新后刷新页面即可应用。", "pid": os.getpid(), "url": server.url}, ensure_ascii=False) if json_output else "看板已打开；程序更新后刷新页面即可应用。", flush=True)
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            import shutil
            for review in server.install_reviews.values():
                rmtree(review['temporary'], ignore_errors=True)
            for review in server.update_reviews.values():
                rmtree(review['temporary'], ignore_errors=True)
            lock.seek(0)
            lock.truncate()
            # Do not unlink: another launcher could be holding the same inode.
