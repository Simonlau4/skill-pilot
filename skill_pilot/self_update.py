from .platform_support import copytree
"""Stage official application updates independently of the user's skills."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .environment import tool_environment
from .platform_support import rmtree
from .sources.github import git as repository_git
from .store import PROJECT, guard, write_allowed
from .version import REQUIRED_FILES, format_version, read_build

SOURCE = 'https://github.com/Simonlau4/skill-pilot.git'
BRANCH = 'main'
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
        names = git(project, 'ls-files', '-z').split('\0')
        paths = [safe_path(project, name) for name in names if name]
    else:
        paths = [p for name in BUNDLE_DIRS for p in (project / name).rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts]
        paths += list(project.glob('*.md'))
        for name in ('.gitignore', '.gitattributes', '.claude/launch.json', 'skill-pilot'):
            if (project / name).exists():
                paths.append(project / name)
    return {p.relative_to(project).as_posix(): stamp(safe_path(project, p.relative_to(project).as_posix()))
            for p in sorted(paths) if p.name != '.DS_Store' and p.suffix != '.pyc'}


def read_record(project):
    try:
        data = json.loads(safe_path(project, RECORD).read_text())
        if not isinstance(data['files'], dict) or not data['files']:
            raise ValueError()
        if not all(name in data['files'] for name in REQUIRED_FILES):
            raise ValueError()
        for name in data['files']:
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
            if old['files'] == files:
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
    for name, expected in data['files'].items():
        path = safe_path(project, name)
        if not path.is_file() or stamp(path) != expected:
            raise ValueError('SP 程序文件有本地改动，已保留当前版本。请先另存或恢复这些改动，再检查更新。')
    return {**data, 'checkout': False}


def check(home, project=PROJECT):
    project = Path(project).resolve()
    before = installed(project)
    remote = git(project, 'ls-remote', SOURCE, 'refs/heads/' + BRANCH, timeout=60).split()
    if len(remote) != 2 or len(remote[0]) != 40 or any(c not in '0123456789abcdef' for c in remote[0]):
        raise ValueError('没能读取 SP 的最新版本，请检查网络后重试。')
    target = remote[0]
    build = read_build(project)
    current_version = build['version'] or format_version(
        before.get('date', ''), before.get('revision', ''))
    review = {'before': before, 'target': target, 'current': before.get('date', ''),
              'latest': before.get('date', ''), 'current_version': current_version,
              'latest_version': current_version, 'available': False, 'temporary': '',
              'changes': [], 'changes_total': 0, 'changes_partial': False}
    if before['checkout'] and target == before.get('revision'):
        return review
    temporary = Path(tempfile.mkdtemp(prefix='skill-pilot-self-update-')).resolve()
    candidate = temporary / 'candidate'
    candidate.mkdir()
    try:
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
        git(candidate, 'fetch', '--no-tags', *depth, SOURCE, target, timeout=120)
        git(candidate, 'checkout', '--detach', target)
        if before['checkout']:
            try:
                git(candidate, 'merge-base', '--is-ancestor', before['revision'], target)
            except ValueError as error:
                raise ValueError('当前 SP 含有与公开版本不同的改动，已保留当前版本。请先另存后重新下载安装。') from error
        files = bundle_files(candidate)
        for name in ('skill_pilot/cli.py', 'skill_pilot/web/server.py', 'scripts/install.py', 'assets/SkillPilot.icns'):
            if name not in files:
                raise ValueError('下载的新版不完整，当前程序保持原样，请稍后重试。')
        validate(candidate, home)
        available = before['checkout'] or files != before['files']
        changes = []
        if available:
            try:
                titles = (git(candidate, 'log', '--format=%s', before['revision'] + '..' + target)
                          if before['checkout'] else git(candidate, 'show', '-s', '--format=%s', target))
                changes = list(dict.fromkeys(title for line in titles.splitlines()
                    if (title := re.sub(r' \(#\d+\)$', '', line.strip()).strip())))
            except Exception:
                # Release notes are optional; a missing title must not block an upgrade.
                pass
        latest_date = git(candidate, 'show', '-s', '--format=%cI', target)
        latest_version = read_build(candidate)['version'] or format_version(latest_date, target)
        if not available:
            rmtree(temporary, ignore_errors=True)
            return {**review, 'temporary': '', 'files': files,
                    'latest': latest_date, 'latest_version': latest_version,
                    'available': False, 'changes': [], 'changes_total': 0, 'changes_partial': False}
        return {**review, 'temporary': str(temporary), 'candidate': str(candidate), 'files': files,
                'latest': latest_date, 'latest_version': latest_version,
                'available': available, 'changes': changes[:10], 'changes_total': len(changes),
                'changes_partial': bool(available and not before['checkout'])}
    except BaseException:
        rmtree(temporary, ignore_errors=True)
        raise


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
    if not review or not review.get('available'):
        raise ValueError('请先检查 SP 更新。')
    before = installed(project)
    if before != review['before']:
        raise ValueError('SP 程序文件已经变了，请重新检查更新。')
    candidate = Path(review['candidate'])
    if bundle_files(candidate) != review['files']:
        raise ValueError('下载的文件已经变了，请重新检查更新。')
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
                    (project / name).unlink()
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
    return {'available': False, 'current': review['latest'], 'latest': review['latest'],
            'latest_version': installed_version, 'pending_version': installed_version}
