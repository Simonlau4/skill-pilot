"""Small OS boundary for user identity and process locks."""
import os
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


def create_links(links):
    """Create staged links after files, resolving chains before Windows types them."""
    pending = list(links)
    while pending:
        remaining = []
        for path, target in pending:
            destination = path.parent / target
            if destination.exists():
                path.symlink_to(str(Path(target)) if os.name == 'nt' else target,
                                target_is_directory=destination.is_dir())
            else:
                remaining.append((path, target))
        if len(remaining) == len(pending):
            # Dangling links retain their text; the existing source-boundary
            # validation still decides whether they may be installed.
            for path, target in remaining:
                path.symlink_to(str(Path(target)) if os.name == 'nt' else target)
            break
        pending = remaining


def copytree(source, destination, **options):
    """Preserve Windows directory-link types instead of copying them as files."""
    import shutil
    import stat
    if os.name != 'nt' or not options.get('symlinks'):
        return shutil.copytree(source, destination, **options)
    source, destination = Path(source), Path(destination)
    ignore = options.pop('ignore', None)
    links = []
    def skip_links(directory, names):
        skipped = set(ignore(directory, names) if ignore else [])
        for name in names:
            path = Path(directory) / name
            if name not in skipped and path.is_symlink():
                links.append((path, destination / path.relative_to(source), readlink(path),
                              bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY)))
                skipped.add(name)
        return skipped
    result = shutil.copytree(source, destination, ignore=skip_links, **options)
    for original, path, target, directory in links:
        path.symlink_to(str(Path(target)), target_is_directory=directory)
        shutil.copystat(original, path, follow_symlinks=False)
    return result


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
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _LockOffset(ctypes.Structure):
        _fields_ = (
            ('Internal', ctypes.c_size_t),
            ('InternalHigh', ctypes.c_size_t),
            ('Offset', wintypes.DWORD),
            ('OffsetHigh', wintypes.DWORD),
            ('hEvent', wintypes.HANDLE),
        )

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_LockOffset))
    _kernel32.LockFileEx.restype = wintypes.BOOL

    class WindowsLocks:
        LOCK_EX = 2
        LOCK_NB = 4
        _EXCLUSIVE = 0x2
        _IMMEDIATE = 0x1
        _LOCK_VIOLATION = 33

        @staticmethod
        def flock(file, operation):
            # Lock one byte past anything the launcher needs to read. The CRT
            # file pointer is not a cross-process lock: two writers can both
            # believe they hold it and then replace the same log.
            offset = _LockOffset()
            offset.Offset = 0x7fffffff
            flags = WindowsLocks._EXCLUSIVE
            if operation & WindowsLocks.LOCK_NB:
                flags |= WindowsLocks._IMMEDIATE
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(file.fileno()))
            if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(offset)):
                error = ctypes.get_last_error()
                if error == WindowsLocks._LOCK_VIOLATION and operation & WindowsLocks.LOCK_NB:
                    raise BlockingIOError('Another process holds the lock')
                raise OSError(error, '无法锁定这份文件')
            file._skill_pilot_lock = offset

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
