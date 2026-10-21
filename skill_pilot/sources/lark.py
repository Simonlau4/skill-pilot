from ..platform_support import rmtree
"""CLI-only staged installation; official archives never run an installer."""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ..environment import tool_environment
from ..scan import metadata
from ..store import guard, target_home
from .github import safe_skill

CLI_PATHS = ['.local/lib/node_modules/@larksuite/cli', '.local/bin/lark-cli']


def check(timeout=60, home=None):
    env = tool_environment(target_home() if home is None else home)
    executable = shutil.which('lark-cli', path=env['PATH'])
    if not executable:
        raise ValueError('找不到飞书工具，请先按安装说明安装，再重新检查更新。')
    try:
        result = subprocess.run([executable, 'update', '--check', '--json'], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise ValueError('飞书版本没能查到，请稍后重试。') from error
    if result.returncode or not data.get('ok'):
        raise ValueError('飞书版本没能查到，请稍后重试。')
    return {'available': data.get('action') == 'update_available',
            'current_version': data['current_version'], 'latest_version': data['latest_version'],
            'message': 'lark-cli 有新版（含 28 个飞书技能）' if data.get('action') == 'update_available' else '已是最新',
            'skills_status': data.get('skills_status', {})}


def download(url, timeout=45):
    if not url.startswith('https://open.feishu.cn/lark-cli/skills/'):
        raise ValueError('飞书技能下载地址不属于官方来源。')
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if not response.url.startswith('https://open.feishu.cn/lark-cli/skills/'):
            raise ValueError('飞书技能下载跳转到其他来源，已停止。')
        return response.read()


def prepare(sources, temporary, version, deadline=None):
    result, indexes = {}, {}
    def get(url):
        if deadline is None: return download(url)
        left = deadline - time.monotonic()
        if left <= 0: raise ValueError('飞书检查超时，可以稍后重试。')
        return download(url, timeout=min(45, left))
    for name, source in sources.items():
        index_url = source['base_url'].rstrip('/') + '/.well-known/agent-skills/index.json'
        if index_url not in indexes:
            indexes[index_url] = {s['name']: s for s in json.loads(get(index_url))['skills']}
        entry = indexes[index_url].get(name)
        if not entry or entry.get('type') != 'archive' or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', entry.get('digest', '')):
            raise ValueError('飞书官方技能清单不完整，已停止更新。')
        url = urllib.parse.urljoin(index_url, entry['url'])
        archive = get(url)
        if 'sha256:' + hashlib.sha256(archive).hexdigest() != entry['digest'].lower():
            raise ValueError('飞书技能包校验没通过，已停止更新。')
        destination = Path(temporary) / ('lark-' + name)
        destination.mkdir()
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(destination, filter='data')
        except tarfile.TarError as error:
            raise ValueError('飞书技能包内容不安全或未读完整，已停止更新。') from error
        safe_skill(destination)
        if metadata((destination / 'SKILL.md').read_text()).get('name') != name:
            raise ValueError('飞书技能包的名称与清单不一致，已停止更新。')
        result[name] = {'path': str(destination), 'digest': entry['digest'], 'url': url, 'cli_version': version}
    return result


def install_cli(home, version, checked):
    guard(home / '.skill-pilot', home)
    if os.name == 'nt':
        raise ValueError('Windows 版不自动升级飞书 CLI；请按本机飞书规则手动升级后，再检查技能版本。')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('飞书版本号不正确。')
    env = tool_environment(home)
    if not all(shutil.which(name, path=env['PATH']) for name in ('node', 'npm')):
        raise ValueError('飞书更新需要 Node.js 和 npm，请按安装说明装好后重试。这组技能保持原样。')
    # Package lifecycle code can only see this isolated home and prefix. It
    # never receives an agent root or the user's npm configuration.
    with tempfile.TemporaryDirectory(prefix='skill-pilot-lark-install-') as temporary:
        root = Path(temporary)
        env = {k: v for k, v in env.items() if not k.lower().startswith(('npm_config_', 'pnpm_'))}
        env.update(HOME=str(root), npm_config_prefix=str(root / 'prefix'), npm_config_cache=str(root / 'cache'),
                   npm_config_userconfig=str(root / 'user.npmrc'), npm_config_globalconfig=str(root / 'global.npmrc'),
                   XDG_CONFIG_HOME=str(root / '.config'), XDG_STATE_HOME=str(root / '.state'))
        result = subprocess.run(['npm', 'install', '-g', '@larksuite/cli@' + version],
                                cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        if result.returncode:
            error = result.stderr.decode(errors='replace')
            reason = ('安装设置互相冲突' if 'double-loading config' in error else
                      '下载没有成功，请检查网络后重试' if any(code in error for code in
                          ('ENOTFOUND', 'EAI_AGAIN', 'ECONNRESET', 'ETIMEDOUT', 'ECONNREFUSED')) else
                      '安装程序报错，请稍后重试')
            raise ValueError('飞书工具' + reason + '。这组技能保持原样。')
        package = root / 'prefix/lib/node_modules/@larksuite/cli'
        if json.loads((package / 'package.json').read_text())['version'] != version:
            raise ValueError('飞书工具版本没有核对通过，技能保持原样。')
        target = checked(home / CLI_PATHS[0])
        if target.exists():
            rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package, target, symlinks=True)
        entry = checked(home / CLI_PATHS[1])
        entry.parent.mkdir(parents=True, exist_ok=True)
        if entry.exists() or entry.is_symlink():
            entry.unlink()
        entry.symlink_to('../lib/node_modules/@larksuite/cli/scripts/run.js')
