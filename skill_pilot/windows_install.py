"""Install Windows desktop and command-line entry points for the current user."""
import os
from pathlib import Path
import shutil


def entries(home):
    return [Path(home) / '.local/bin/skill-pilot.cmd',
            Path(home) / 'AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Skill Pilot.lnk']


def create_shortcut(shortcut, executable, arguments, directory):
    """Use IShellLinkW/IPersistFile: WScript shortcuts lose non-ANSI paths.

    Interface layout: Windows SDK shobjidl_core.h (IShellLinkW) and
    objidl.h (IPersistFile); https://learn.microsoft.com/windows/win32/shell/links
    """
    import ctypes as c
    import uuid
    class GUID(c.Structure):
        _fields_ = [('data1', c.c_uint32), ('data2', c.c_uint16), ('data3', c.c_uint16), ('data4', c.c_ubyte * 8)]
    def guid(value):
        return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
    def checked(code):
        if code < 0:
            raise OSError(f'创建 Windows 快捷方式失败（0x{code & 0xffffffff:08x}）。')
    def call(pointer, slot, types=(), values=()):
        vtable = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
        function = c.WINFUNCTYPE(c.c_long, c.c_void_p, *types)(vtable[slot])
        checked(function(pointer, *values))
    ole = c.OleDLL('ole32')
    ole.CoInitializeEx.argtypes = [c.c_void_p, c.c_uint32]
    ole.CoInitializeEx.restype = c.c_long
    ole.CoCreateInstance.argtypes = [c.POINTER(GUID), c.c_void_p, c.c_uint32, c.POINTER(GUID), c.POINTER(c.c_void_p)]
    ole.CoCreateInstance.restype = c.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    checked(ole.CoInitializeEx(None, 2))
    shell, persist = c.c_void_p(), c.c_void_p()
    try:
        clsid = guid('00021401-0000-0000-c000-000000000046')
        shell_iid = guid('000214f9-0000-0000-c000-000000000046')
        checked(ole.CoCreateInstance(c.byref(clsid), None, 1, c.byref(shell_iid), c.byref(shell)))
        for slot, value in ((20, executable), (11, arguments), (9, directory), (7, 'Skill Pilot')):
            call(shell, slot, (c.c_wchar_p,), (str(value),))
        persist_iid = guid('0000010b-0000-0000-c000-000000000046')
        call(shell, 0, (c.POINTER(GUID), c.POINTER(c.c_void_p)), (c.byref(persist_iid), c.byref(persist)))
        call(persist, 6, (c.c_wchar_p, c.c_int), (str(shortcut), 1))
    finally:
        if persist: call(persist, 2)
        if shell: call(shell, 2)
        ole.CoUninitialize()


def install(project, home):
    import sys
    project, home = Path(project).resolve(), Path(home).resolve()
    if sys.version_info < (3, 12):
        raise ValueError('Windows 版需要 Python 3.12 或更高版本。')
    from .environment import tool_environment
    tools = tool_environment(home)
    git = shutil.which('git', path=tools['PATH'])
    if not git:
        raise ValueError('找不到 Git，请先安装 Git for Windows，再重试。')
    cli, shortcut = entries(home)
    cli.parent.mkdir(parents=True, exist_ok=True)
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).with_name('python.exe')
    pythonw = python.with_name('pythonw.exe')
    if not python.is_file() or not pythonw.is_file():
        raise ValueError('找不到 Python 的 Windows 启动入口。')
    if cli.exists() and 'SkillPilot Windows launcher' not in cli.read_text(encoding='utf-8-sig'):
        raise ValueError('命令入口已被其他程序使用，原入口保持不变。')
    search_path = os.pathsep.join(dict.fromkeys([str(home / '.local/bin'), str(python.parent), str(Path(git).parent)]))
    command = ('@echo off\nrem SkillPilot Windows launcher\nchcp 65001 >nul\nset PYTHONUTF8=1\n'
               f'set "PATH={search_path};%PATH%"\n'
               f'pushd "{project}"\n"{python}" -X utf8 -B -m skill_pilot %*\n'
               'set result=%errorlevel%\npopd\nexit /b %result%\n')
    temporary = cli.with_suffix('.cmd.tmp')
    temporary.write_text(command, encoding='utf-8', newline='\r\n')
    temporary.replace(cli)
    create_shortcut(shortcut, pythonw, '-X utf8 -B "' + str(project / 'scripts/launch-windows.py') + '"', project)
    from .self_update import record_installation
    record_installation(project)
