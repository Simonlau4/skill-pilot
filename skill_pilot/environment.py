"""Find user-installed tools from terminals and desktop launchers alike."""
import os
import re
from pathlib import Path


def tool_environment(home):
    home = Path(home).resolve()
    paths = [home / '.local/bin', home / 'bin', *os.get_exec_path(),
             home / '.npm-global/bin', home / '.volta/bin', home / 'AppData/Roaming/npm']
    versions = (home / '.nvm/versions/node').glob('*/bin')
    paths.extend(sorted(versions, key=lambda p: [int(n) for n in re.findall(r'\d+', p.parent.name)], reverse=True))
    paths.extend(['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'])
    return {**os.environ, 'HOME': str(home),
            'PATH': os.pathsep.join(dict.fromkeys(str(p) for p in paths if str(p)))}
