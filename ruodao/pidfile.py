"""PID file helpers shared by CLI and GUI entry points."""

import os

from .config import DATA_DIR

PID_PATH = os.path.join(str(DATA_DIR), "ruodao.pid")
_STILL_ACTIVE = 259


def read():
    try:
        with open(PID_PATH, "r", encoding="ascii") as fh:
            return int(fh.read().strip() or 0)
    except Exception:
        return 0


def alive(pid):
    if not pid:
        return False
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok and code.value == _STILL_ACTIVE)
    except Exception:
        return False


def write():
    current = read()
    if current and current != os.getpid() and alive(current):
        return False
    try:
        with open(PID_PATH, "w", encoding="ascii") as fh:
            fh.write(str(os.getpid()))
        return True
    except Exception:
        return False


def clear():
    if read() != os.getpid():
        return
    try:
        if os.path.isfile(PID_PATH):
            os.remove(PID_PATH)
    except Exception:
        pass
