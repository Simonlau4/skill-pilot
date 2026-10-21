from ..platform_support import rmtree
from ..platform_support import fcntl, user_home, user_id, check_directory_links
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from ..scan import content_digest, scan_root
from ..store import attribution, git_tree


def git(directory, *args, timeout=120, input=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    command = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
               '-C', str(directory), *args]
    try:
        command[1:1] = ['-c', 'core.symlinks=true', '-c', 'core.autocrlf=false']
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout, input=input,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('仓库读取超时或工具暂时不可用，请稍后重试。') from error
    if result.returncode:
        raise ValueError('仓库读取没能完成，请检查链接和网络后重试。') from subprocess.CalledProcessError(
            result.returncode, command, output=result.stdout, stderr=result.stderr)
    return result.stdout if '-z' in args else result.stdout.strip()


def parse_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请使用不带登录信息的仓库链接。')
    if parsed.scheme == 'file' and not parsed.netloc:
        return url, None, ''
    parts = urllib.parse.unquote(parsed.path).strip('/').split('/')
    if parsed.scheme != 'https' or parsed.netloc != 'github.com' or len(parts) < 2:
        raise ValueError('请粘贴 GitHub 仓库链接或技能文件夹链接。')
    if any(p in {'.', '..', ''} for p in parts):
        raise ValueError('仓库链接中的位置不正确。')
    repo = '/'.join(parts[:2]).removesuffix('.git')
    clone_url = 'https://github.com/' + repo + '.git'
    if len(parts) == 2:
        return clone_url, repo, ''
    if len(parts) < 4 or parts[2] != 'tree':
        raise ValueError('请使用仓库链接或 tree 文件夹链接。')
    return clone_url, repo, '/'.join(parts[3:])


def safe_skill(path):
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            child = Path(base) / name
            if child.is_symlink() and not child.resolve().is_relative_to(path.resolve()):
                raise ValueError('这个技能引用了文件夹外的文件，已停止安装。')


def inspect(url):
    clone_url, repository, suffix = parse_url(url)
    temporary = Path(tempfile.mkdtemp(prefix='skill-pilot-install-')).resolve()
    checkout = temporary / 'repo'
    try:
        ref, subpath = None, ''
        if suffix:
            refs = git(temporary, 'ls-remote', '--heads', '--tags', clone_url)
            names = [line.split('\t')[1].removeprefix('refs/heads/').removeprefix('refs/tags/')
                     for line in refs.splitlines() if '\t' in line and not line.endswith('^{}')]
            match = sorted((r for r in names if suffix == r or suffix.startswith(r + '/')), key=len, reverse=True)
            ref = match[0] if match else suffix.split('/')[0]
            subpath = suffix[len(ref):].lstrip('/')
        args = ['clone', '--depth', '1', '--no-local', '--filter=blob:none', '--no-checkout']
        if ref and not re.fullmatch(r'[0-9a-f]{7,40}', ref):
            args += ['--branch', ref]
        git(temporary, *args, '--', clone_url, str(checkout))
        commit = 'HEAD'
        if ref and re.fullmatch(r'[0-9a-f]{7,40}', ref):
            git(checkout, 'fetch', '--depth', '1', 'origin', ref)
            commit = 'FETCH_HEAD'
        files = git(checkout, 'ls-tree', '-r', '-z', '--name-only', commit, '--', subpath or '.').split('\0')
        if not any(files):
            raise ValueError('这个仓库里没有找到指定的文件夹。')
        folders = sorted({Path(p).parent.as_posix() for p in files if Path(p).name == 'SKILL.md'})
        if not folders:
            return {'id': temporary.name, 'temporary': str(temporary), 'url': url, 'skills': []}
        if '.' not in folders:
            git(checkout, 'sparse-checkout', 'set', '--cone', '--stdin',
                input=''.join(json.dumps('./' + p + '/', ensure_ascii=False) + '\n' for p in folders))
        git(checkout, 'checkout', '-q', '--detach', commit)
        root = checkout / subpath
        if not root.resolve().is_relative_to(checkout) or not root.is_dir():
            raise ValueError('这个仓库里没有找到指定的文件夹。')
        skills = scan_root(root, user_root=False)
        if skills['errors']:
            raise ValueError('下载的技能没有读完整，已停止安装。')
        found = []
        for skill in skills['skills']:
            if not skill['valid']:
                continue
            name = skill['name']
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
                raise ValueError('下载的技能名称不能安全用作文件夹名称。')
            path = Path(skill['path'])
            safe_skill(path)
            relative = path.relative_to(checkout).as_posix()
            tree = git(checkout, 'rev-parse', 'HEAD^{tree}' if relative == '.' else 'HEAD:' + relative)
            found.append({'name': name, 'path': str(path), 'repository_path': relative,
                          'digest': content_digest(path), 'description': skill['description'],
                          'source': {'source': clone_url, 'repository': repository or clone_url,
                                     'source_path': relative, 'commit': git(checkout, 'rev-parse', 'HEAD'),
                                     'tree_sha': tree, 'confidence': '高', 'evidence': '从仓库直接读取，未执行其中的程序'}})
        if len({s['name'] for s in found}) != len(found):
            raise ValueError('仓库里有多个同名技能，请粘贴具体技能文件夹的链接。')
        return {'id': temporary.name, 'temporary': str(temporary), 'url': url, 'skills': found}
    except Exception:
        rmtree(temporary)
        raise


def api(endpoint, timeout=45):
    try:
        result = subprocess.run(['gh', 'api', '--method', 'GET', endpoint], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('GitHub 来源查询超时或工具暂时不可用，请稍后重试。') from error
    if result.returncode:
        raise ValueError('GitHub 来源查询暂时没能完成。')
    return json.loads(result.stdout)


def search_code(name):
    # Cross-process throttle: never more than ten code searches in one minute.
    path = Path(tempfile.gettempdir()) / 'skill-pilot-github-search.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        recent = [t for t in json.loads(lock.read() or '[]') if time.time() - t < 60]
        if len(recent) >= 10:
            raise ValueError('来源搜索次数已到本分钟上限，请稍后重新收编。')
        recent.append(time.time())
        lock.seek(0); lock.truncate(); json.dump(recent, lock); lock.flush()
        query = urllib.parse.urlencode({'q': f'"name: {name}" filename:SKILL.md', 'per_page': 30})
        return api('search/code?' + query).get('items', [])


def verify_source(path, name):
    """Only attribute an identical full skill tree, including historical trees."""
    local_tree = git_tree(path)
    candidates = sorted(search_code(name), key=lambda item: Path(item['path']).parent.name != name)
    # Previously verified source hints only prioritize a candidate; attribution
    # still requires a fresh complete-tree content match.
    hint = attribution().get(name, {})
    match = re.search(r'https://github.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)', hint.get('source', ''))
    if match and hint.get('source_path'):
        path_hint = hint['source_path']
        manifest_hint = path_hint if path_hint.endswith('SKILL.md') else str(Path(path_hint) / 'SKILL.md')
        candidates.insert(0, {'repository': {'full_name': match[1].removesuffix('.git')}, 'path': manifest_hint})
    visited = set()
    standardized = any(p.name in {'SKILL.md', 'SKILL.example.md'} and p.parent != path for p in path.rglob('*'))
    for item in candidates:
        repo = item['repository']['full_name']
        manifest = item['path']
        folder = str(Path(manifest).parent)
        if (repo, folder) in visited:
            continue
        visited.add((repo, folder))
        if standardized:
            from .update_github import Reader
            with tempfile.TemporaryDirectory(prefix='skill-pilot-attribution-') as temporary:
                row = Reader(temporary).inspect({'repository': repo, 'path': folder}, path)
                if not row['local_modified']:
                    return {'source': 'https://github.com/' + repo, 'repository': repo,
                            'source_path': folder, 'commit': row['base_commit'], 'tree_sha': row['base_tree_sha'],
                            'confidence': '高', 'evidence': '标准处理后与上游这个历史版本的全部文件一致'}
            continue
        page = 1
        while True:
            query = urllib.parse.urlencode({'path': manifest, 'per_page': 100, 'page': page})
            commits = api(f'repos/{repo}/commits?' + query)
            for commit in commits:
                sha = commit['sha']
                upstream = api(f'repos/{repo}/git/commits/{sha}')['tree']['sha']
                # Walk just the ancestor trees; large unrelated repository trees
                # cannot truncate the skill's content comparison.
                for component in (() if folder == '.' else Path(folder).parts):
                    tree = api(f'repos/{repo}/git/trees/{upstream}')
                    if tree.get('truncated'):
                        raise ValueError('上游文件清单没有读完整，请稍后重新收编。')
                    upstream = next((e['sha'] for e in tree['tree'] if e['path'] == component and e['type'] == 'tree'), None)
                    if upstream is None:
                        break
                if upstream == local_tree:
                    return {'source': 'https://github.com/' + repo, 'repository': repo,
                            'source_path': folder, 'commit': sha, 'tree_sha': upstream,
                            'confidence': '高', 'evidence': '与上游这个历史版本的全部文件一致'}
            if len(commits) < 100:
                break
            page += 1
    return {'source': '自制', 'source_path': '本机维护', 'confidence': '高',
            'evidence': '未找到内容一致的上游版本，自制技能不检查更新'}
