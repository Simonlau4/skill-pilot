"""Windowless desktop startup, including failures before the package imports."""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get('SKILL_PILOT_HOME') or Path.home())
    sys.path.insert(0, str(root))
    try:
        os.chdir(root)
        from skill_pilot.cli import main as start
        sys.argv = ['skill-pilot', 'ui']
        start()
        return 0
    except Exception as error:
        try:
            from skill_pilot.diagnostics import record
            record(home, 'startup', '打开看板', error, '看板未能打开，请运行 skill-pilot doctor。', force=True)
        except Exception:
            # Even a broken installation can keep a safe startup marker.
            # No exception text, output or paths are included in this fallback.
            if os.environ.get('SKILL_PILOT_READ_ONLY') != '1':
                try:
                    path = home / '.skill-pilot/logs/errors.jsonl'
                    if not path.resolve().is_relative_to(home.resolve()):
                        raise ValueError()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    row = {'error_id': uuid.uuid4().hex, 'fingerprint': 'startup-import',
                           'time': datetime.now(timezone.utc).isoformat(), 'source': 'startup',
                           'action': '打开看板', 'user_message': '程序文件不完整，请重新安装。',
                           'error_type': type(error).__name__, 'detail': '启动阶段无法载入程序。'}
                    old = []
                    if path.is_file():
                        with path.open('rb') as stream:
                            stream.seek(0, 2)
                            start = max(0, stream.tell() - 900000)
                            stream.seek(start)
                            if start:
                                stream.readline()
                            old = stream.read(900000).splitlines()[-199:]
                    content = b'\n'.join(old + [json.dumps(row, ensure_ascii=False).encode()]) + b'\n'
                    fd, temporary = tempfile.mkstemp(prefix='.startup-', dir=path.parent)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(content)
                        os.replace(temporary, path)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
        message = '看板未能打开。请在命令行运行 skill-pilot doctor 查看诊断，或查看用户目录下 .skill-pilot/logs/errors.jsonl。'
        try:
            if sys.platform == 'darwin':
                subprocess.run(['/usr/bin/osascript', '-e',
                    'display alert "Skill Pilot 暂时打不开" message ' + json.dumps(message, ensure_ascii=False)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            elif os.name == 'nt':
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, message, 'Skill Pilot 暂时打不开', 0x10)
        except (OSError, subprocess.SubprocessError):
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
