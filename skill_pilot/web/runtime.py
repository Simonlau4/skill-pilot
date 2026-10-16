"""Replace an idle dashboard while retaining its listener and session."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
RESUME_FD = 'SKILL_PILOT_RESUME_FD'


def version():
    digest = hashlib.sha256()
    for path in sorted(p for p in PACKAGE.rglob('*') if p.suffix in {'.py', '.json'} and '__pycache__' not in p.parts):
        digest.update(str(path.relative_to(PACKAGE)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def resume_state():
    descriptor = os.environ.pop(RESUME_FD, None)
    if descriptor is None:
        return None
    with os.fdopen(int(descriptor), 'r') as stream:
        state = json.load(stream)
    for name in ('socket_fd', 'lock_fd'):
        os.set_inheritable(state[name], False)
    return state


def replace(server, lock):
    candidate = version()
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    check = subprocess.run([sys.executable, '-B', '-c', 'import skill_pilot.cli, skill_pilot.web.server'],
                           cwd=PACKAGE.parent, env={**env, 'SKILL_PILOT_READ_ONLY': '1'},
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    if check.returncode:
        raise ValueError('新版还没有准备完整，当前程序保持运行，请稍后刷新重试。')
    if candidate != version():
        raise ValueError('新版仍在修改中，当前程序保持运行，请稍后刷新重试。')
    if server.closing:
        return
    state = {name: getattr(server, name) for name in
             ('token', 'inventory', 'install_reviews', 'update_reviews', 'labels_progress', 'program_review', 'program_progress')}
    state.update(pages=sorted(server.pages), socket_fd=server.socket.fileno(), lock_fd=lock.fileno())
    # Session data stays in an unlinked temporary file. The listener and
    # singleton lock survive the replacement alongside it.
    with tempfile.TemporaryFile(mode='w+') as stream:
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        stream.seek(0)
        descriptors = [stream.fileno(), state['socket_fd'], state['lock_fd']]
        for descriptor in descriptors:
            os.set_inheritable(descriptor, True)
        env[RESUME_FD] = str(stream.fileno())
        try:
            os.chdir(PACKAGE.parent)
            os.execve(sys.executable, [sys.executable, '-B', '-m', 'skill_pilot', 'ui'], env)
        finally:
            for descriptor in descriptors:
                os.set_inheritable(descriptor, False)
