from .platform_support import copytree
"""Stage official application updates independently of the user's skills."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import stat
import urllib.request
import zipfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .environment import tool_environment
from .platform_support import is_junction, rmtree
from .sources.github import git as repository_git
from .store import PROJECT, guard, write_allowed
from .version import (OFFICIAL_DIRS, OFFICIAL_FILES, REQUIRED_FILES, format_version,
                      is_official_path, is_safe_manifest_path, read_build, read_git_tree_version)

SOURCE = 'https://github.com/Simonlau4/skill-pilot.git'
BRANCH = 'main'
OFFICIAL_API = 'https://api.github.com/repos/Simonlau4/skill-pilot/'
PACKAGE_LIMIT = 64 * 1024 * 1024
REVISION = re.compile(r'[0-9a-f]{40}')
RECORD = '.skill-pilot-install.json'
BUNDLE_DIRS = ('skill_pilot', 'scripts', 'assets', 'docs', 'skill', 'tests', '.github')


def git(project, *arguments, **options):
    command = arguments[0]
    remote = (command in ('ls-remote', 'fetch') and SOURCE in arguments
              and SOURCE.startswith(('https://', 'http://')))
    for attempt in range(2 if remote else 1):
        try:
            return repository_git(project, *arguments, **options)
        except ValueError as error:
            cause = error.__cause__ or error
            detail = getattr(cause, 'stderr', '') or str(cause)
            if isinstance(detail, bytes):
                detail = detail.decode('utf-8', 'replace')
            denied = isinstance(cause, PermissionError) or bool(re.search(
                r'permission denied|operation not permitted|read-only file system', detail, re.I))
            certificate = bool(re.search(r'certificate|SSL peer', detail, re.I))
            timed_out = isinstance(cause, subprocess.TimeoutExpired) or bool(re.search(
                r'operation timed out', detail, re.I))
            interrupted = bool(re.search(
                r'connection (reset|timed out)|could not resolve host|failed to connect|empty reply|'
                r'HTTP/2 stream\b.*(not closed cleanly|was reset)|'
                r'Error in the HTTP2 framing layer|transfer closed with outstanding read data remaining|'
                r'GnuTLS recv error|TLS.*(terminated|handshake)|SSL_(read|connect)|'
                r'requested URL returned error: 50[234]',
                detail, re.I))
            if remote and not denied and not certificate and attempt == 0 and (timed_out or interrupted):
                continue
            step = {'ls-remote': '检查新版', 'fetch': '下载或准备新版', 'merge': '安装新版',
                    'reset': '恢复原版', 'checkout': '展开新版文件'}.get(command, '读取程序文件')
            if denied:
                reason = '访问权限不足，当前用户无法完成这一步'
            elif isinstance(cause, FileNotFoundError):
                reason = '找不到更新所需的工具，请按安装说明补齐'
            elif timed_out:
                reason = '连接更新服务器超时' if remote else '读取程序超时'
            elif certificate:
                reason = '安全连接验证失败，请检查系统时间和网络代理'
            elif interrupted:
                reason = '与更新服务器的连接中断'
            else:
                reason = '当前操作失败，请查看本机更新日志中的详细原因'
            retry = '，自动重试一次后仍未完成' if attempt else ''
            failure = ValueError(f'SP {step}没有完成：{reason}{retry}。')
            failure.diagnostic_detail = f'程序升级现场\n失败步骤：{step}\n原因：{reason}\n尝试次数：{attempt + 1}'
            raise failure from error


from .redaction import _abbreviate_home, _hide_paths_outside_home, hide_credentials


def record_failure(home, action, error):
    """Keep failure diagnostics outside the reversible skill-operation history."""
    from . import diagnostics
    message = str(error) if isinstance(getattr(error, 'diagnostic_detail', None), str) else '程序升级没有完成，请重试。'
    captured = diagnostics.record(home, 'self_update', action, error, message)
    home = Path(home).resolve()
    if not write_allowed(home):
        return captured
    cause = error
    while cause.__cause__ is not None:
        cause = cause.__cause__
    detail = getattr(cause, 'stderr', '') or str(error)
    if isinstance(detail, bytes):
        detail = detail.decode('utf-8', 'replace')
    detail = hide_credentials(detail)
    detail = _abbreviate_home(detail, home)
    detail = _hide_paths_outside_home(detail)
    command = getattr(cause, 'cmd', [])
    step = command[command.index('-C') + 2] if isinstance(command, list) and '-C' in command else action
    record = {'time': datetime.now(timezone.utc).isoformat(), 'error_id': captured.get('error_id', ''),
              'action': action, 'step': step,
              'error_type': type(cause).__name__, 'returncode': getattr(cause, 'returncode', None),
              'timeout': getattr(cause, 'timeout', None), 'detail': detail[:4000]}
    try:
        path = guard(home / '.skill-pilot/logs/program-update.jsonl', home)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, 'w') as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    except (OSError, ValueError):
        # A log-folder problem must not hide the original update failure.
        pass
    return captured


def safe_path(root, name):
    path = root / name
    if not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('程序文件的位置不正确，请重新下载安装。')
    if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)):
        raise ValueError('程序文件指向了其他位置，请重新下载安装。')
    if not path.is_relative_to(root) or path.is_dir():
        raise ValueError('程序文件的位置发生冲突，请重新下载安装。')
    return path


def stamp(path):
    return [hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o111]


def is_checkout(project):
    if not (project / '.git').exists():
        return False
    if Path(git(project, 'rev-parse', '--show-toplevel')).resolve() != project.resolve():
        raise ValueError('这个文件夹不是独立的 SP 安装，请重新下载安装。')
    return True


def bundle_files(project):
    if is_checkout(project):
        names = [name for name in git(project, 'ls-files', '-z').split('\0') if name and is_official_path(name)]
        paths = [safe_path(project, name) for name in names]
    else:
        paths = [p for name in OFFICIAL_DIRS if (project / name).is_dir()
                 for p in (project / name).rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts]
        for name in OFFICIAL_FILES:
            if (project / name).exists():
                paths.append(project / name)
    return {rel: stamp(safe_path(project, rel))
            for p in sorted(paths)
            if is_official_path(rel := p.relative_to(project).as_posix())}


def read_record(project):
    try:
        data = json.loads(safe_path(project, RECORD).read_text(encoding='utf-8'))
        if not isinstance(data['files'], dict) or not data['files']:
            raise ValueError()
        if not all(name in data['files'] for name in REQUIRED_FILES):
            raise ValueError()
        for name in data['files']:
            if not is_safe_manifest_path(name):
                raise ValueError()
            if not is_official_path(name):
                continue
            safe_path(project, name)
        return data
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise ValueError('缺少安装记录，请按安装说明在项目文件夹重新运行一次安装程序，再回来看板升级。') from error


def record_installation(project=PROJECT, revision='', date='', files=None):
    project = Path(project).resolve()
    files = bundle_files(project) if files is None else files
    if is_checkout(project):
        revision = git(project, 'rev-parse', 'HEAD')
        date = git(project, 'show', '-s', '--format=%cI', 'HEAD')
    elif not revision:
        try:
            old = read_record(project)
            old_official = {name: expected for name, expected in old['files'].items() if is_official_path(name)}
            if old_official == files:
                revision, date = old.get('revision', ''), old.get('date', '')
        except ValueError:
            pass
    path = safe_path(project, RECORD)
    temporary = path.with_suffix('.tmp')
    safe_path(project, temporary.name).write_text(json.dumps(
        {'files': files, 'revision': revision, 'date': date}, ensure_ascii=False))
    temporary.replace(path)


def installed(project):
    if is_checkout(project):
        if git(project, 'status', '--porcelain', '--untracked-files=normal'):
            raise ValueError('SP 程序文件有本地改动，已保留当前版本。请先另存或恢复这些改动，再检查更新。')
        return {'files': bundle_files(project), 'revision': git(project, 'rev-parse', 'HEAD'),
                'date': git(project, 'show', '-s', '--format=%cI', 'HEAD'), 'checkout': True}
    data = read_record(project)
    official_files = {name: expected for name, expected in data['files'].items() if is_official_path(name)}
    for name, expected in official_files.items():
        path = safe_path(project, name)
        if not path.is_file() or stamp(path) != expected:
            raise ValueError('SP 程序文件有本地改动，已保留当前版本。请先另存或恢复这些改动，再检查更新。')
    return {**data, 'files': official_files, 'checkout': False}


def official_metadata(endpoint):
    request = urllib.request.Request(OFFICIAL_API + endpoint,
                                     headers={'User-Agent': 'Skill-Pilot', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.url != request.full_url:
                raise ValueError()
            return json.loads(response.read(4 * 1024 * 1024))
    except (OSError, ValueError) as error:
        raise ValueError('没能读取 Skill Pilot 的新版信息，请检查网络后重试。') from error


def cache_root(home, project):
    key = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:24]
    directory = guard(Path(home) / '.skill-pilot/program-update' / key, home)
    if any(p.is_symlink() or is_junction(p) for p in (directory, directory.parent)):
        raise ValueError('更新包保存位置发生冲突，请检查本机设置。')
    return directory


def check(home, project=PROJECT):
    project = Path(project).resolve()
    before = installed(project)
    start_build = read_build(project)
    if SOURCE.startswith('file:'):
        target = git(project, 'ls-remote', SOURCE, 'refs/heads/' + BRANCH, timeout=20).split()[0]
        from urllib.request import url2pathname
        upstream = Path(url2pathname(SOURCE.removeprefix('file://')))
        tree = git(upstream, 'rev-parse', target + '^{tree}')
        date = git(upstream, 'show', '-s', '--format=%cI', target)
    else:
        result = official_metadata('commits?sha=' + BRANCH + '&per_page=1')[0]
        target, tree, date = result['sha'], result['commit']['tree']['sha'], result['commit']['committer']['date']
    if not REVISION.fullmatch(target) or not REVISION.fullmatch(tree):
        raise ValueError('没能读取 Skill Pilot 的最新版本，请检查网络后重试。')
    latest_version = format_version(date, target)
    if before['checkout'] and target == before['revision']:
        available = False
        latest_version = read_git_tree_version(project, target)
        end_build = read_build(project)
        if (git(project, 'status', '--porcelain', '--untracked-files=normal')
                or git(project, 'rev-parse', 'HEAD') != before['revision']
                or end_build['version'] != start_build['version']):
            raise ValueError('SP 程序文件在检查期间发生了改动，已保留当前版本。请先确认本地文件后再重新检查更新。')
    elif not before['checkout']:
        # A ZIP installed for the first time has no recorded revision. Compare
        # the official file list and blob identities without downloading content.
        if SOURCE.startswith('file:'):
            entries = (line.split('\t', 1) for line in git(upstream, 'ls-tree', '-rz', tree).split('\0') if line)
            remote = {name: (parts[2], parts[0]) for header, name in entries
                      if is_official_path(name) and (parts := header.split())[1] == 'blob'}
        else:
            listing = official_metadata('git/trees/' + tree + '?recursive=1')
            if listing.get('truncated'):
                raise ValueError('官方文件清单没有读完整，请重新检查。')
            remote = {e['path']: (e['sha'], e['mode']) for e in listing['tree']
                      if e['type'] == 'blob' and is_official_path(e['path'])}
        available = set(remote) != set(before['files']) or any(
            blob_id((project / name).read_bytes()) != expected or
            (os.name != 'nt' and bool(before['files'][name][1]) != (mode == '100755'))
            for name, (expected, mode) in remote.items())
        if not available:
            latest_version = start_build['version']
    else:
        available = True
    review = {'before': before, 'target': target, 'tree': tree, 'current': before.get('date', ''),
              'latest': date, 'current_version': start_build['version'],
              'latest_version': latest_version, 'available': available, 'temporary': '',
              'changes': [], 'changes_total': 0, 'changes_partial': False}
    if available and write_allowed(home):
        directory = cache_root(home, project)
        directory.mkdir(parents=True, exist_ok=True)
        pin = {name: review[name] for name in ('target', 'tree', 'latest', 'current_version')}
        fd, name = tempfile.mkstemp(prefix='official-', dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(pin, output)
            temporary.replace(directory / 'official.json')
        finally:
            temporary.unlink(missing_ok=True)
        review = cached_package(review, directory) or review
    return review


def blob_id(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def cached_package(review, directory):
    candidate = directory / 'package' / 'candidate'
    try:
        record = json.loads((candidate.parent / 'package.json').read_text(encoding='utf-8'))
        if (candidate.is_symlink() or candidate.parent.is_symlink() or
                record['target'] != review['target'] or record['before'] != review['before'] or
                bundle_files(candidate) != record['files']):
            return None
        return {**review, **record['details'], 'files': record['files'], 'candidate': str(candidate)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def import_zip(source, candidate, expected_tree):
    # Hash the entire archive as a Git tree before any candidate code is loaded.
    # GitHub ZIPs preserve Git's file modes but omit repository metadata.
    with zipfile.ZipFile(source) as package:
        entries = package.infolist()
        if len(entries) > 5000 or sum(entry.file_size for entry in entries) > PACKAGE_LIMIT * 4:
            raise ValueError('更新包超出大小限制，请重新获取官方压缩包。')
        roots = {entry.filename.partition('/')[0] for entry in entries}
        if len(roots) != 1:
            raise ValueError('请选择完整的官方源码压缩包。')
        folders = {(): {}}
        names = set()
        for entry in entries:
            if entry.is_dir():
                continue
            _, _, name = entry.filename.partition('/')
            parts = name.split('/')
            mode = entry.external_attr >> 16
            if (not is_safe_manifest_path(name) or any(
                    not part or part in {'.', '..'} or part.casefold() == '.git'
                    or part[-1] in '. ' or re.search(r'[\\:*?"<>|\x00-\x1f]', part) for part in parts)
                    or name.casefold() in names or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError('压缩包含有不支持的文件，请重新获取官方压缩包。')
            names.add(name.casefold())
            data = package.read(entry)
            file_mode = '100755' if mode & 0o111 else '100644'
            key = tuple(parts)
            folders.setdefault(key[:-1], {})[key[-1]] = (file_mode, blob_id(data))
            for index in range(len(key) - 1):
                folders.setdefault(key[:index], {})
            if is_official_path(name):
                path = safe_path(candidate, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o755 if mode & 0o111 else 0o644)
        for key in sorted(folders, key=len, reverse=True):
            rows = sorted(folders[key].items(), key=lambda row: (row[0] + ('/' if row[1][0] == '40000' else '')).encode())
            data = b''.join(mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)
                            for name, (mode, oid) in rows)
            tree = hashlib.sha1(b'tree ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if key:
                folders[key[:-1]][key[-1]] = ('40000', tree)
        if tree != expected_tree:
            raise ValueError('压缩包与查到的官方版本不一致，请选择同一版本的完整压缩包。')


def pinned_review(home, project=PROJECT):
    project = Path(project).resolve()
    try:
        pin = json.loads((cache_root(home, project) / 'official.json').read_text(encoding='utf-8'))
        if not all(REVISION.fullmatch(pin[key]) for key in ('target', 'tree')):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('请先联网检查一次新版，再选择对应的官方更新包。') from error
    before = installed(project)
    if read_build(project)['version'] != pin.get('current_version'):
        raise ValueError('当前程序版本已经变了，请重新检查后再选择本地更新包。')
    return {**pin, 'before': before, 'available': True, 'temporary': '', 'current': before.get('date', '')}


def restore_download(home, project=PROJECT):
    project = Path(project).resolve()
    try:
        directory = cache_root(home, project)
        if not (directory / 'package' / 'package.json').is_file():
            return None
        review = cached_package(pinned_review(home, project), directory)
        return review if review and review['available'] else None
    except (OSError, ValueError):
        return None


def download(review, home, project=PROJECT, *, package=None):
    project = Path(project).resolve()
    home = Path(home).resolve()
    directory = cache_root(home, project)
    if package is not None:
        review = pinned_review(home, project)
    if not review or not review['available']:
        raise ValueError('请先检查 Skill Pilot 更新。')
    before, target = review['before'], review['target']
    if installed(project) != before:
        raise ValueError('程序文件已经变了，请重新检查更新。')
    directory.mkdir(parents=True, exist_ok=True)
    if package is None and (cached := cached_package(review, directory)):
        return cached
    temporary = Path(tempfile.mkdtemp(prefix='partial-', dir=directory))
    candidate = temporary / 'candidate'
    candidate.mkdir()
    try:
        if package is not None and zipfile.is_zipfile(package):
            if before['checkout']:
                raise ValueError('此安装需要带版本历史的更新包，请选择官方仓库的 .bundle 文件。')
            import_zip(package, candidate, review['tree'])
        else:
            git(candidate, 'init', '-q')
            if before['checkout']:
                # Borrow the history already on this computer; only new commits are downloaded.
                objects = (project / git(project, 'rev-parse', '--git-path', 'objects')).resolve()
                (candidate / '.git/objects/info/alternates').write_text(str(objects) + '\n')
                # A recent-history-only install also lends where its history stops.
                shallow = project / git(project, 'rev-parse', '--git-path', 'shallow')
                if shallow.is_file():
                    shutil.copyfile(shallow, candidate / '.git/shallow')
                depth = ()
            else:
                depth = ('--depth', '1')
            if package is not None:
                git(candidate, 'bundle', 'verify', str(package))
                depth = ()
            git(candidate, 'fetch', '--no-tags', *depth, str(package) if package is not None else SOURCE, target, timeout=120)
            if git(candidate, 'rev-parse', target + '^{tree}') != review['tree']:
                raise ValueError('更新包与查到的官方版本不一致。')
            git(candidate, 'checkout', '--detach', target)
        if before['checkout']:
            try:
                git(candidate, 'merge-base', '--is-ancestor', before['revision'], target)
            except ValueError as error:
                raise ValueError('当前程序含有与公开版本不同的改动，请先另存后重新下载安装。') from error
        files = bundle_files(candidate)
        if not all(name in files for name in REQUIRED_FILES):
            raise ValueError('下载的新版不完整，请重新获取更新包。')
        validate(candidate, home)
        available = target != before['revision'] if before['checkout'] else files != before['files']
        changes = []
        if available and is_checkout(candidate):
            try:
                titles = (git(candidate, 'log', '--format=%s', before['revision'] + '..' + target)
                          if before['checkout'] else git(candidate, 'show', '-s', '--format=%s', target))
                changes = list(dict.fromkeys(title for line in titles.splitlines()
                    if (title := re.sub(r' \(#\d+\)$', '', line.strip()).strip())))
            except Exception:
                # Release notes are optional; a missing title must not block an upgrade.
                pass
        details = {'latest_version': read_build(candidate)['version'], 'available': available,
                   'changes': changes[:10], 'changes_total': len(changes),
                   'changes_partial': bool(changes and not before['checkout'])}
        (temporary / 'package.json').write_text(json.dumps(
            {'before': before, 'target': target, 'files': files, 'details': details}), encoding='utf-8')
        saved = directory / 'package'
        if saved.exists():
            rmtree(saved)
        temporary.rename(saved)
        return {**review, **details, 'files': files, 'candidate': str(saved / 'candidate')}
    except (zipfile.BadZipFile, NotImplementedError) as error:
        raise ValueError('压缩包不完整或格式不受支持，请重新获取官方压缩包。') from error
    finally:
        rmtree(temporary, ignore_errors=True)


def validate(project, home):
    env = {**tool_environment(home), 'SKILL_PILOT_HOME': str(home),
           'SKILL_PILOT_READ_ONLY': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('PYTHONPATH', None)
    code = ("from pathlib import Path; "
            "[compile(p.read_bytes(), str(p), 'exec') for p in Path('skill_pilot').rglob('*.py')]; "
            "compile(Path('scripts/install.py').read_bytes(), 'scripts/install.py', 'exec'); "
            "import skill_pilot.cli, skill_pilot.web.server")
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=project, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('新版暂时无法在这台电脑运行，当前程序保持原样。请稍后重试或查看新版安装要求。') from subprocess.CalledProcessError(result.returncode, result.args)


def run_installer(project, home):
    env = {**tool_environment(home), 'SKILL_PILOT_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-B', str(project / 'scripts/install.py')],
                            cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        reason = result.stderr.decode('utf-8', 'replace').strip()
        if reason.startswith('安装没有完成：'):
            reason = reason.removeprefix('安装没有完成：').strip()
        reason = next((line.strip() for line in reversed(reason.splitlines()) if line.strip()), '')
        raise ValueError('新版的启动入口没能安装完成。' + reason[:200]) from subprocess.CalledProcessError(result.returncode, result.args)


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Each replacement is atomic; a browser can never read half a script.
    fd, name = tempfile.mkstemp(prefix='.sp-update-', dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, name)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def apply(review, home, project=PROJECT):
    project, home = Path(project).resolve(), Path(home).resolve()
    if not write_allowed(home):
        raise ValueError('当前处于只读模式，程序没有改动。')
    if not review or not review.get('available') or not review.get('candidate'):
        raise ValueError('请先下载或导入更新包。')
    before = installed(project)
    if before != review['before']:
        raise ValueError('SP 程序文件已经变了，请重新检查更新。')
    candidate = Path(review['candidate'])
    if bundle_files(candidate) != review['files']:
        raise ValueError('下载的文件已经变了，请重新下载或导入更新包。')
    validate(candidate, home)
    names = before['files'].keys() | review['files'].keys()
    for name in names:
        path = safe_path(project, name)
        if name not in before['files'] and path.exists():
            raise ValueError('新版与这个文件夹里的其他文件重名，已保留原文件。请换一个文件夹重新安装 SP。')
    backup = Path(tempfile.mkdtemp(prefix='skill-pilot-self-backup-')).resolve()
    retain_backup = False
    try:
        for name in before['files']:
            copy_file(project / name, backup / 'files' / name)
        marker = safe_path(project, RECORD)
        if marker.exists():
            shutil.copy2(marker, backup / 'record')
        entries = [home / 'Applications/Skill Pilot.app', home / '.local/bin/skill-pilot']
        if os.name == 'nt':
            from .windows_install import entries as windows_entries
            entries = windows_entries(home)
        for index, entry in enumerate(entries):
            if entry.is_symlink() or any(p.is_symlink() for p in entry.parents if p.is_relative_to(home)):
                raise ValueError('SP 启动入口指向了其他位置，请重新安装后再升级。')
            if entry.is_dir():
                copytree(entry, backup / str(index), symlinks=True)
            elif entry.exists():
                shutil.copy2(entry, backup / str(index))
        # Recheck after staging/backup: edits made while downloading remain intact.
        if installed(project) != before:
            raise ValueError('SP 程序文件已经变了，请重新检查更新。')
        changed = False
        try:
            if before['checkout']:
                git(project, 'fetch', '--no-tags', str(candidate), review['target'])
                if installed(project) != before:
                    raise ValueError('SP 程序文件已经变了，请重新检查更新。')
                git(project, 'merge', '--ff-only', '--no-edit', review['target'])
                changed = True
            else:
                changed = True
                for name in review['files']:
                    copy_file(candidate / name, project / name)
                for name in before['files'].keys() - review['files'].keys():
                    if is_official_path(name):
                        (project / name).unlink(missing_ok=True)
            for name, expected in review['files'].items():
                if stamp(project / name) != expected:
                    raise ValueError('新版文件没有保存完整。')
            validate(project, home)
            run_installer(project, home)
            record_installation(project, review['target'], review['latest'], review['files'])
        except Exception as error:
            if not changed:
                raise
            try:
                if before['checkout']:
                    git(project, 'reset', '--keep', before['revision'])
                else:
                    for name in before['files']:
                        copy_file(backup / 'files' / name, project / name)
                    for name in review['files'].keys() - before['files'].keys():
                        (project / name).unlink(missing_ok=True)
                if (backup / 'record').exists():
                    copy_file(backup / 'record', marker)
                else:
                    marker.unlink(missing_ok=True)
                for index, entry in enumerate(entries):
                    if entry.is_dir():
                        rmtree(entry)
                    else:
                        entry.unlink(missing_ok=True)
                    saved = backup / str(index)
                    if saved.is_dir():
                        copytree(saved, entry, symlinks=True)
                    elif saved.exists():
                        copy_file(saved, entry)
            except Exception as recovery:
                retain_backup = True
                raise ValueError('升级没有完成，自动恢复也未完成。请先保留原文件再恢复使用。恢复文件夹：' + str(backup)) from recovery
            raise ValueError('升级没有完成，已恢复原版，可以继续使用。请稍后重试。') from error
    finally:
        if not retain_backup:
            rmtree(backup, ignore_errors=True)
    installed_version = review.get('latest_version') or format_version(review.get('latest', ''), review.get('target', ''))
    actual_version = ''
    version_verified = False
    has_unmanaged_files = False
    verify_error_id = ''
    try:
        actual_version = read_build(project)['version']
        version_verified = bool(installed_version and actual_version and actual_version == installed_version)
        if not version_verified and not before['checkout']:
            expected_files = set(review['files'])
            has_unmanaged_files = any(
                rel not in expected_files
                for directory in OFFICIAL_DIRS
                if (project / directory).is_dir() and not (project / directory).is_symlink()
                for path in (project / directory).rglob('*')
                if path.is_file() and is_official_path(rel := path.relative_to(project).as_posix())
            )
    except Exception as verify_error:
        actual_version = ''
        version_verified = False
        has_unmanaged_files = False
        try:
            failure = RuntimeError('新版文件已写入，但未能核实安装后的版本，请刷新页面或重新检查。')
            failure.diagnostic_detail = f'程序升级后版本核对失败：{type(verify_error).__name__}'
            failure.__cause__ = verify_error
            captured = record_failure(home, 'apply', failure) or {}
            verify_error_id = captured.get('error_id', '')
        except Exception:
            verify_error_id = ''
    return {'available': False, 'current': review['latest'], 'latest': review['latest'],
            'latest_version': installed_version, 'pending_version': installed_version,
            'installed_version': actual_version, 'version_verified': version_verified,
            'has_unmanaged_files': has_unmanaged_files,
            **({'error_id': verify_error_id} if verify_error_id else {})}
