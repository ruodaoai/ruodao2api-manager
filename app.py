"""Ruodao2API Manager entry point (Windows friendly)."""

import argparse
import atexit
import os
import sys
import time
import traceback

from ruodao import __version__
from ruodao.config import DATA_DIR, FROZEN
from ruodao.gateway import serve
from ruodao.pidfile import alive as pid_alive
from ruodao.pidfile import clear as clear_pid
from ruodao.pidfile import read as read_pid
from ruodao.pidfile import write as write_pid
from ruodao.runtime import create_runtime
from ruodao.scheduler import Scheduler


def _fatal(message):
    text = "%s\n\n%s" % (message, traceback.format_exc())
    try:
        os.makedirs(str(DATA_DIR), exist_ok=True)
        with open(os.path.join(str(DATA_DIR), "ruodao-error.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("\n[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, message, "Ruodao2API 启动失败", 0x10)
    except Exception:
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


def _activate_window(pid):
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(proc(callback), 0)
        if found:
            user32.ShowWindow(found[0], 9)
            user32.SetForegroundWindow(found[0])
            return True
    except Exception:
        pass
    return False


def main(argv=None):
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception as exc:
        _fatal("启动失败：%s" % exc)
        return 1


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ruodao2api",
        description="把 Qoder / WorkBuddy / ZCode / GPT 账户聚合成本地 OpenAI 兼容网关")
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认 8388")
    parser.add_argument("--gui", action="store_true",
                        help="启动桌面管理窗口（Windows）")
    parser.add_argument("--headless", action="store_true",
                        help="仅运行网关服务，不打开桌面窗口")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--no-scheduler", action="store_true",
                       help="不启动定时签到/刷新任务")
    parser.add_argument("--checkin", action="store_true",
                       help="立即执行一次签到后退出")
    parser.add_argument("--version", action="version",
                        version="ruodao2api %s" % __version__)
    args = parser.parse_args(argv)

    runtime = create_runtime()
    if args.host:
        runtime.config.values["host"] = args.host
    if args.port:
        runtime.config.values["port"] = args.port

    if args.checkin:
        result = _run_checkin(runtime)
        print("签到完成：%s" % result)
        runtime.close()
        return 0

    use_gui = bool(args.gui or (FROZEN and not args.headless))

    if use_gui:
        existing = read_pid()
        if existing and existing != os.getpid() and pid_alive(existing):
            if not _activate_window(existing):
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0, "Ruodao2API 已在运行，请从任务栏打开管理窗口。",
                        "Ruodao2API", 0x40)
                except Exception:
                    pass
            runtime.close()
            return 0

    if not args.no_scheduler:
        runtime.scheduler = Scheduler(runtime)
        runtime.scheduler.start()

    write_pid()
    atexit.register(clear_pid)
    try:
        if use_gui:
            from ruodao.gui import run_gui
            run_gui(runtime)
            return 0
        serve(runtime, open_browser=args.open)
    except OSError as exc:
        runtime.log("ERROR", "启动失败：%s（端口可能已被占用）" % exc)
        if not use_gui:
            print("启动失败：%s" % exc, file=sys.stderr)
            print("可尝试：python app.py --port 8389", file=sys.stderr)
        return 1
    except Exception as exc:
        if not use_gui:
            raise
        _fatal("桌面窗口启动失败：%s" % exc)
        return 1
    finally:
        clear_pid()
        runtime.close()
    return 0


def _run_checkin(runtime):
    from ruodao.scheduler import Scheduler as _Scheduler
    runner = _Scheduler(runtime)
    return runner.run_checkin()


if __name__ == "__main__":
    sys.exit(main())
