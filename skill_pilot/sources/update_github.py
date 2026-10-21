from ..platform_support import rmtree
"""Pinned upstream trees and full-directory historical baseline calibration."""
import difflib
import hashlib
import json
import os
import subprocess
import time
import urllib.parse
from pathlib import Path

from . import github
from ..scan import SKIP_CONTENT
from ..store import git_tree
from .. import normalization
from ..normalization import files


class Reader:
    def __init__(self, temporary, deadline=None):
        self.root = Path(temporary)
        self.root.mkdir(parents=True, exist_ok=True)
        self.deadline = deadline
        self.repos = {}
        self.repo_errors = {}
        self.api_calls = 0
        self.last_request = 0

    def remaining(self, maximum=120):
        left = maximum if self.deadline is None else min(maximum, self.deadline - time.monotonic())
        if left <= 0:
            raise ValueError('这个仓库检查超时，可以稍后重试。')
        return left

    def git(self, directory, *args):
        return github.git(directory, *args, timeout=self.remaining())

    def latest(self, source):
        if source['repository'].startswith('file:///'):
            _, commit = self.repository(source)
            return commit, self.tree(source, commit)
        item = self.api('repos/' + source['repository'] + '/commits?per_page=1')[0]
        return item['sha'], self.tree(source, item['sha'], item['commit']['tree']['sha'])

    def api(self, endpoint):
        self.remaining()
        cache = self.root / ('api-' + hashlib.sha256(endpoint.encode()).hexdigest() + '.json')
        if cache.exists() and time.time() - cache.stat().st_mtime < 300:
            return json.loads(cache.read_text())
        # At most five requests/second; all response caching stays in temporary storage.
        time.sleep(max(0, .2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        result = github.api(endpoint, timeout=self.remaining(45))
        self.api_calls += 1
        cache.write_text(json.dumps(result))
        return result

    def repository(self, source):
        repo = source['repository']
        if repo in self.repo_errors:
            raise ValueError(self.repo_errors[repo])
        try:
            return self._repository(source)
        except ValueError as error:
            self.repo_errors[repo] = str(error)
            raise

    def _repository(self, source):
        repo = source['repository']
        if repo in self.repos:
            return self.repos[repo]
        checkout = self.root / ('repo-' + hashlib.sha256(repo.encode()).hexdigest()[:16])
        local = repo.startswith('file:///')
        if local:
            self.git(self.root, 'clone', '--depth', '1', '--no-checkout', '--', repo, str(checkout))
            commit = self.git(checkout, 'rev-parse', 'HEAD')
        else:
            commit = self.api('repos/' + repo + '/commits?per_page=1')[0]['sha']
            checkout.mkdir()
            self.git(checkout, 'init', '-q')
            self.git(checkout, 'remote', 'add', 'origin', 'https://github.com/' + repo + '.git')
            self.git(checkout, 'fetch', '--depth', '1', 'origin', commit)
        self.repos[repo] = checkout, commit
        return checkout, commit

    def tree(self, source, commit, root=None):
        repo, folder = source['repository'], source['path']
        if repo.startswith('file:///'):
            return self.git(self.repository(source)[0], 'rev-parse', commit + ('^{tree}' if folder == '.' else ':' + folder))
        tree = root or self.api(f'repos/{repo}/git/commits/{commit}')['tree']['sha']
        for part in (() if folder == '.' else Path(folder).parts):
            data = self.api(f'repos/{repo}/git/trees/{tree}')
            if data.get('truncated'):
                raise ValueError('上游文件清单没有读完整，请稍后重试。')
            tree = next((e['sha'] for e in data['tree'] if e['path'] == part and e['type'] == 'tree'), None)
            if tree is None:
                raise ValueError('上游已没有这个技能文件夹，请先核对来源。')
        return tree

    def snapshot(self, checkout, tree):
        self.remaining()
        destination = self.root / ('tree-' + tree)
        if destination.exists():
            return destination
        staging = self.root / ('partial-tree-' + tree)
        if staging.exists():
            import shutil
            rmtree(staging)
        rows = self.git(checkout, 'ls-tree', '-rz', tree).split('\0')
        entries = []
        for row in filter(None, rows):
            header, name = row.split('\t', 1)
            mode, kind, sha = header.split()
            if any(part in SKIP_CONTENT for part in Path(name).parts):
                continue
            if kind != 'blob':
                raise ValueError('这个技能包含外部仓库，无法完整读取。')
            entries.append((mode, sha, name))
        # Batch blob reads preserve binary contents and avoid one process per file.
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
        try:
            result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(checkout), 'cat-file', '--batch'],
                                    input=''.join(sha + '\n' for _, sha, _ in entries).encode(),
                                    capture_output=True, env=env, timeout=self.remaining())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError('上游文件读取超时，请稍后重试。') from error
        if result.returncode:
            raise ValueError('上游文件内容没有读完整，请稍后重试。')
        staging.mkdir()
        offset = 0
        for mode, sha, name in entries:
            end = result.stdout.index(b'\n', offset)
            header = result.stdout[offset:end].split()
            if len(header) != 3 or header[1] != b'blob':
                raise ValueError('上游文件内容没有读完整，请稍后重试。')
            size = int(header[2]); data = result.stdout[end + 1:end + 1 + size]
            offset = end + 2 + size
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if mode == '120000':
                path.symlink_to(os.fsdecode(data))
            else:
                path.write_bytes(data); path.chmod(0o755 if mode == '100755' else 0o644)
        # Compare link text; installation checks boundaries before any home write.
        if (staging / 'SKILL.md').is_symlink() or not (staging / 'SKILL.md').is_file():
            raise ValueError('上游这一版没有完整的技能说明，请核对来源。')
        processing = normalization.apply(staging)
        (self.root / ('normalization-'+tree+'.json')).write_text(json.dumps(processing))
        staging.rename(destination)
        return destination

    def current(self, source, commit=None, tree=None):
        checkout, latest = self.repository(source)
        commit = commit or latest
        tree = tree or self.tree(source, commit)
        upstream = self.snapshot(checkout, tree)
        return {'commit': commit, 'tree_sha': tree, '_upstream_path': str(upstream),
                'updated_at': self.git(checkout, 'log', '-1', '--format=%cI', commit, '--', source['path']),
                'normalized_tree_sha': git_tree(upstream),
                'normalization': json.loads((self.root / ('normalization-'+tree+'.json')).read_text())}

    def inspect(self, source, local):
        current = self.current(source)
        checkout, latest = self.repository(source)
        folder = source['path']
        if source['repository'].startswith('file:///'):
            self.git(checkout, 'fetch', '--unshallow', 'origin')
            commits = self.git(checkout, 'rev-list', latest, '--', folder).splitlines()
        else:
            commits, page = [], 1
            while True:
                query = urllib.parse.urlencode({'path':folder,'sha':latest,'per_page':100,'page':page})
                batch = self.api('repos/'+source['repository']+'/commits?'+query)
                commits += [c['sha'] for c in batch]
                if len(batch) < 100:
                    break
                page += 1
        local_tree = normalization.tree(local)
        best, baseline, seen = -1, None, set()
        for commit in [latest] + commits:
            if not source['repository'].startswith('file:///') and commit != latest:
                self.git(checkout, 'fetch', '--depth', '1', 'origin', commit)
            try:
                tree = self.git(checkout, 'rev-parse', commit + ('^{tree}' if folder == '.' else ':' + folder))
            except ValueError:
                continue  # Directory did not exist in an earlier parent.
            if tree in seen or not self.git(checkout, 'ls-tree', tree, '--', 'SKILL.md'):
                continue
            seen.add(tree)
            candidate = self.snapshot(checkout, tree)
            score = 1.0 if git_tree(candidate) == local_tree else similarity_score(local, candidate)
            if score > best:
                best = score
                baseline, best_commit, best_tree = candidate, commit, tree
            if git_tree(candidate) == local_tree:
                break
        if baseline is None:
            raise ValueError('没找到可对比的上游历史版本。')
        return {**current, 'base_commit': best_commit, 'base_tree_sha': best_tree, 'similarity': round(best, 6),
                'local_modified': local_tree != git_tree(baseline), '_base_path': str(baseline)}


def similarity_score(left, right):
    one, two = files(left), files(right)
    matched, total = 0, 0
    for name in one.keys() | two.keys():
        a, am = one.get(name, (b'', 0)); b, bm = two.get(name, (b'', 0))
        weight = max(len(a), len(b), 1); total += weight
        ratio = 1 if a == b and am == bm else difflib.SequenceMatcher(None, a.splitlines(), b.splitlines()).ratio()
        matched += weight * ratio * (1 if am == bm else .99)
    return matched / total if total else 1.0
