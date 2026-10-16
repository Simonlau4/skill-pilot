"""Stage official application updates independently of the user's skills."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .environment import tool_environment
from .sources.github import git as repository_git
from .store import PROJECT, write_allowed

SOURCE = 'https://github.com/Simonlau4/skill-pilot.git'
BRANCH = 'main'
RECORD = '.skill-pilot-install.json'
BUNDLE_DIRS = ('skill_pilot', 'scripts', 'assets', 'docs', 'skill', 'tests')


def git(project, *arguments, **options):
    try:
        return repository_git(project, *arguments, **options)
    except ValueError as error:
        raise ValueError('SP 更新没有完成，请检查网络和项目文件夹权限后重试。') from error


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
        for name in ('.gitignore', '.claude/launch.json', 'skill-pilot'):
            if (project / name).exists():
                paths.append(project / name)
    return {str(p.relative_to(project)): stamp(safe_path(project, str(p.relative_to(project))))
            for p in sorted(paths) if p.name != '.DS_Store' and p.suffix != '.pyc'}


def read_record(project):
    try:
        data = json.loads(safe_path(project, RECORD).read_text())
        if not isinstance(data['files'], dict) or not data['files']:
            raise ValueError()
        for name in data['files']:
            safe_path(project, name)
        return data
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('请先在 SP 项目文件夹运行一次 sh scripts/build-app.sh，再回来看板升级。') from error


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
    review = {'before': before, 'target': target, 'current': before.get('date', ''),
              'latest': before.get('date', ''), 'available': False, 'temporary': ''}
    if target == before.get('revision'):
        return review
    temporary = Path(tempfile.mkdtemp(prefix='skill-pilot-self-update-')).resolve()
    candidate = temporary / 'candidate'
    candidate.mkdir()
    try:
        git(candidate, 'init', '-q')
        git(candidate, 'fetch', '--no-tags', SOURCE, target, timeout=120)
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
        return {**review, 'temporary': str(temporary), 'candidate': str(candidate), 'files': files,
                'latest': git(candidate, 'show', '-s', '--format=%cI', target),
                'available': before['checkout'] or files != before['files']}
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
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
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('新版暂时无法在这台电脑运行，当前程序保持原样。请稍后重试或查看新版安装要求。')


def run_installer(project, home):
    env = {**tool_environment(home), 'SKILL_PILOT_HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'}
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-B', str(project / 'scripts/install.py')],
                            cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    if result.returncode:
        reason = result.stderr.decode('utf-8', 'replace').strip()
        if reason.startswith('安装没有完成：'):
            reason = reason.removeprefix('安装没有完成：').strip()
        reason = next((line.strip() for line in reversed(reason.splitlines()) if line.strip()), '')
        if reason:
            raise ValueError('新版的启动入口没能安装完成。' + reason[:200])
        raise ValueError('新版的启动入口没能安装完成。')


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
        for index, entry in enumerate(entries):
            if entry.is_symlink() or any(p.is_symlink() for p in entry.parents if p.is_relative_to(home)):
                raise ValueError('SP 启动入口指向了其他位置，请重新安装后再升级。')
            if entry.is_dir():
                shutil.copytree(entry, backup / str(index), symlinks=True)
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
                        shutil.rmtree(entry)
                    else:
                        entry.unlink(missing_ok=True)
                    saved = backup / str(index)
                    if saved.is_dir():
                        shutil.copytree(saved, entry, symlinks=True)
                    elif saved.exists():
                        copy_file(saved, entry)
            except Exception as recovery:
                retain_backup = True
                raise ValueError('升级没有完成，自动恢复也未完成。请先保留原文件再恢复使用。恢复文件夹：' + str(backup)) from recovery
            raise ValueError('升级没有完成，已恢复原版，可以继续使用。请稍后重试。') from error
    finally:
        if not retain_backup:
            shutil.rmtree(backup, ignore_errors=True)
    return {'available': False, 'current': review['latest'], 'latest': review['latest']}
