"""Small OS boundary for user identity and process locks."""
import os
import time
from pathlib import Path


def readlink(path):
    value = os.readlink(path)
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            return '\\\\' + value[8:]
        return value.removeprefix('\\\\?\\')
    return value


def rmtree(path, ignore_errors=False):
    import shutil
    import stat
    if os.name != 'nt':
        return shutil.rmtree(path, ignore_errors=ignore_errors)
    def retry(function, name, error):
        try:
            if os.name != 'nt' or not isinstance(error, PermissionError):
                raise error
            os.chmod(name, stat.S_IWRITE)
            function(name)
        except OSError:
            if not ignore_errors:
                raise
    return shutil.rmtree(path, onexc=retry)


def is_junction(path):
    return os.name == 'nt' and Path(path).is_junction()


def user_home():
    if os.name == 'nt':
        return Path(os.environ['USERPROFILE']).resolve()
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def user_id():
    return os.environ.get('USERNAME', 'user') if os.name == 'nt' else os.getuid()


if os.name != 'nt':
    import fcntl
else:
    import msvcrt

    class WindowsLocks:
        LOCK_EX = 2
        LOCK_NB = 4

        @staticmethod
        def flock(file, operation):
            # Lock beyond the payload: another launcher must still read the URL.
            # Windows releases the byte-range lock when the descriptor closes.
            position = file.tell()
            try:
                file.seek(0x7fffffff)
                while True:
                    try:
                        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                        return
                    except OSError as error:
                        if error.errno not in (13, 36):
                            raise
                        if operation & WindowsLocks.LOCK_NB:
                            raise BlockingIOError('Another process holds the lock') from error
                        time.sleep(0.05)
            finally:
                file.seek(position)

    fcntl = WindowsLocks()


def check_directory_links():
    """Fail before a migration touches skills when Windows denies links."""
    if os.name != 'nt':
        return
    import tempfile
    with tempfile.TemporaryDirectory(prefix='skill-pilot-link-check-') as directory:
        root = Path(directory)
        (root / 'target').mkdir()
        try:
            (root / 'link').symlink_to(root / 'target', target_is_directory=True)
        except OSError as error:
            raise ValueError('请先在 Windows 设置中开启开发者模式，再重试；技能文件没有改动。') from error
