"""Reload the idle Windows dashboard while retaining its socket and session."""
import importlib
import json
import os
import sys
import threading
import time
import webbrowser


def run(json_output=False):
    from . import server as module
    from ..platform_support import fcntl
    from ..store import target_home
    fd = os.open(module.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if module.open_existing(lock):
                return
            raise SystemExit('已有看板正在启动或退出，请稍后再打开。')
        module.operations.bootstrap(target_home())
        state = None
        while True:
            server = module.Dashboard(state['inventory'] if state else {'home': str(target_home()), 'deferred': True}, state)
            restarting = threading.Event()

            def restart():
                from ..self_update import validate
                validate(module.runtime.PACKAGE.parent, server.home)
                # Automatic update checks must finish before replacing their modules.
                if server.program_thread and server.program_thread.is_alive():
                    server.program_thread.join()
                restarting.set()
                server.shutdown()

            server.reload_callback = restart
            server.start_agents_scan()
            lock.seek(0)
            lock.truncate()
            json.dump({'pid': os.getpid(), 'url': server.url}, lock)
            lock.flush()
            if state is None:
                server.auto_check_program()
                webbrowser.open(server.url)
                print(json.dumps({'message': '看板已打开；程序更新后刷新页面即可应用。', 'pid': os.getpid(), 'url': server.url}, ensure_ascii=False)
                      if json_output else '看板已打开；程序更新后刷新页面即可应用。', flush=True)
            server.last_heartbeat = time.monotonic()
            threading.Thread(target=server.watch, daemon=True).start()
            try:
                server.serve_forever(poll_interval=0.1)
            except KeyboardInterrupt:
                pass
            if not restarting.is_set():
                server.server_close()
                from ..platform_support import rmtree
                for review in [*server.install_reviews.values(), *server.update_reviews.values()]:
                    if review.get('temporary'):
                        rmtree(review['temporary'], ignore_errors=True)
                lock.seek(0)
                lock.truncate()
                return
            state = module.runtime.snapshot(server)
            state.update(pages=sorted(server.pages), socket_fd=server.socket.dup().detach())
            # Transfer temporary downloads to the new server instead of deleting them.
            server.program_review = None
            server.server_close()
            previous = {name: value for name, value in sys.modules.items()
                        if name == 'skill_pilot' or name.startswith('skill_pilot.')}
            for name in previous:
                del sys.modules[name]
            importlib.invalidate_caches()
            try:
                module = importlib.import_module('skill_pilot.web.server')
            except Exception:
                for name in list(sys.modules):
                    if name == 'skill_pilot' or name.startswith('skill_pilot.'):
                        del sys.modules[name]
                sys.modules.update(previous)
                module = previous['skill_pilot.web.server']
                state['program_progress'].update(phase='error', error='新版没能载入，当前会话已恢复。请关闭看板后重开。')
