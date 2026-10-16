"""Build desktop and CLI entry points for the user running this installer."""
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from skill_pilot.environment import tool_environment
from skill_pilot.self_update import record_installation


def install():
    if sys.platform != 'darwin':
        raise ValueError('当前安装器支持 macOS 桌面。')
    if sys.version_info < (3, 11, 4):
        raise ValueError('需要 Python 3.11.4 或更高版本。')
    home = Path.home().resolve()
    env = tool_environment(home)
    git = shutil.which('git', path=env['PATH'])
    if not git or subprocess.run([git, '--version'], env=env, stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=15).returncode:
        raise ValueError('找不到可用的 Git，请按安装说明装好后重试。')
    # Record this user's actual tool locations so Finder need not load a shell profile.
    paths = [str(home / '.local/bin'), str(Path(sys.executable).parent)]
    for name in ('git', 'node', 'npm', 'lark-cli'):
        executable = shutil.which(name, path=env['PATH'])
        if executable:
            paths.append(str(Path(executable).parent))
    search_path = os.pathsep.join(dict.fromkeys(paths))
    app = home / 'Applications/Skill Pilot.app'
    cli = home / '.local/bin/skill-pilot'
    launcher = app / 'Contents/MacOS/SkillPilot'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    (app / 'Contents/Resources').mkdir(parents=True, exist_ok=True)
    cli.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROJECT / 'assets/SkillPilot.icns', app / 'Contents/Resources/SkillPilot.icns')
    quote = shlex.quote
    cli.write_text(f'''#!/bin/sh
export PATH={quote(search_path)}:"${{PATH:-/usr/bin:/bin}}"
if [ ! -x {quote(sys.executable)} ] || [ ! -d {quote(str(PROJECT / 'skill_pilot'))} ]; then
  echo '找不到 Skill Pilot 或 Python，请在项目文件夹重新运行 sh scripts/build-app.sh。' >&2
  /usr/bin/osascript -e 'display alert "Skill Pilot 暂时打不开" message "请在项目文件夹重新运行安装脚本。"' >/dev/null 2>&1
  exit 1
fi
cd {quote(str(PROJECT))} || exit 1
exec {quote(sys.executable)} -B -m skill_pilot "$@"
''')
    launcher.write_text(f'''#!/bin/sh
/usr/bin/nohup {quote(str(cli))} ui >/dev/null 2>&1 < /dev/null &
exit 0
''')
    with (app / 'Contents/Info.plist').open('wb') as stream:
        plistlib.dump({'CFBundleName': 'Skill Pilot', 'CFBundleDisplayName': 'Skill Pilot',
                      'CFBundleIdentifier': 'local.skill-pilot.dashboard', 'CFBundleVersion': '9',
                      'CFBundleExecutable': 'SkillPilot', 'CFBundleIconFile': 'SkillPilot.icns',
                      'CFBundlePackageType': 'APPL', 'LSUIElement': True}, stream)
    cli.chmod(0o755)
    launcher.chmod(0o755)
    record_installation(PROJECT)
    print('已安装 Skill Pilot 应用和命令行入口。双击应用即可打开看板。')
    if not all(shutil.which(name, path=env['PATH']) for name in ('node', 'npm')):
        print('飞书技能更新还需要 Node.js 和 npm，请按安装说明补齐。')


if __name__ == '__main__':
    try:
        install()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('安装没有完成：' + str(error), file=sys.stderr)
        sys.exit(1)
