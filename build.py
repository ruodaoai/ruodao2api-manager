"""Build the Ruodao2API backend and Windows desktop executables."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV = BASE / ".build" / "venv"
VENV_PY = VENV / "Scripts" / "python.exe"
ELECTRON = BASE / "electron"
DIST = BASE / "dist"
DIST_ELECTRON = BASE / "dist-electron"
BACKEND = DIST / "Ruodao2API-backend.exe"


def log(message):
    print(message, flush=True)


def run(args, cwd=None, env=None):
    log("> " + subprocess.list2cmdline([str(item) for item in args]))
    subprocess.run([str(item) for item in args], cwd=str(cwd or BASE),
                   check=True, env=env)


def prepare_python():
    if not VENV_PY.exists():
        log("创建独立打包环境...")
        run([sys.executable, "-m", "venv", str(VENV)])
        run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"])
    probe = subprocess.run(
        [str(VENV_PY), "-c", "import PyInstaller"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        run([VENV_PY, "-m", "pip", "install", "pyinstaller"])
    return VENV_PY


def ensure_backend_unlocked():
    if not BACKEND.exists():
        return
    try:
        with open(str(BACKEND), "r+b"):
            pass
    except OSError:
        log("")
        log("dist\\Ruodao2API-backend.exe 正被占用，无法覆盖。")
        log("正在运行的 Ruodao2API 进程：")
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Ruodao2API-backend.exe", "/FO", "LIST"],
            capture_output=True)
        try:
            log(result.stdout.decode("mbcs", errors="replace"))
        except LookupError:
            log(result.stdout.decode("utf-8", errors="replace"))
        log("请先退出 Ruodao2API 桌面端后重试。")
        raise SystemExit(1)


def build_backend():
    python = prepare_python()
    ensure_backend_unlocked()
    log("构建后端 dist\\Ruodao2API-backend.exe...")
    run([
        python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--windowed", "--name", "Ruodao2API-backend", "--icon", "app.ico",
        "--hidden-import", "ruodao.gui",
        "--add-data", "static;static",
        "--add-data", r"ruodao\qoder\baseprompt.json;ruodao\qoder",
        "--add-data", r"ruodao\qoder\catalog_cn.json;ruodao\qoder",
        "--add-data", r"ruodao\qoder\catalog_intl.json;ruodao\qoder",
        "app.py",
    ])


def ensure_node():
    if not shutil.which("node"):
        raise SystemExit("未找到 Node.js，请安装 Node.js 18 或更高版本后重试。")
    electron_exe = ELECTRON / "node_modules" / "electron" / "dist" / "electron.exe"
    if not electron_exe.exists():
        if not shutil.which("npm"):
            raise SystemExit("未找到 npm，请重新安装 Node.js 后重试。")
        log("安装 Electron 构建依赖，首次可能需要几分钟...")
        subprocess.run("npm install", cwd=str(ELECTRON), shell=True, check=True)


def build_desktop(target):
    ensure_node()
    out = Path(tempfile.gettempdir()) / ("ruodao2api-desktop-" + target)
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    if out.exists():
        # 上次构建残留文件被安全软件占用时，换一个全新目录继续
        out = out.with_name(out.name + "-" + str(int(time.time())))
    log("打包桌面端（%s）..." % ("便携版" if target == "portable" else "安装包"))
    env = None
    if target == "installer":
        # 360 等安全软件会独占扫描新写入的 exe，NSIS 紧随其后的
        # 归档与卸载器嵌入会失败；留出扫描窗口（见 build-retry.js）。
        env = dict(os.environ)
        env.setdefault("RUDAO_SCAN_DELAY_MS", "15000")
    run([
        "node", "-r", "./build-retry.js", "./node_modules/electron-builder/cli.js",
        "--win", "portable" if target == "portable" else "nsis",
        "--config.directories.output=" + str(out),
    ], cwd=ELECTRON, env=env)
    DIST_ELECTRON.mkdir(parents=True, exist_ok=True)
    if target == "portable":
        source = out / "Ruodao2API.exe"
        if not source.exists():
            raise SystemExit("未找到便携版产物：%s" % source)
        destination = DIST_ELECTRON / "Ruodao2API.exe"
    else:
        matches = sorted(out.glob("Ruodao2API-Setup-*.exe"))
        if not matches:
            raise SystemExit("未找到安装包产物：%s" % out)
        source = matches[-1]
        destination = DIST_ELECTRON / source.name
    shutil.copy2(str(source), str(destination))
    log("已生成：%s" % destination)


def cleanup_legacy():
    for path in [DIST / "Ruodao2API.exe", BASE / "Ruodao2API.spec"]:
        try:
            if path.exists():
                path.unlink()
                log("已清理旧文件：%s" % path)
        except OSError:
            pass
    for path in DIST_ELECTRON.glob("Ruodao2API-Desktop-*.exe"):
        try:
            path.unlink()
            log("已清理旧文件：%s" % path)
        except OSError:
            pass


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="构建 Ruodao2API 后端与 Windows 桌面端 EXE")
    parser.add_argument(
        "target", nargs="?", default="all",
        choices=("portable", "installer", "all"),
        help="portable=便携版 EXE，installer=安装包，all=两者都构建（默认）")
    args = parser.parse_args()
    try:
        build_backend()
        if args.target in ("portable", "all"):
            build_desktop("portable")
        if args.target in ("installer", "all"):
            build_desktop("installer")
        cleanup_legacy()
    except subprocess.CalledProcessError as exc:
        log("")
        log("构建失败：命令退出码 %s" % exc.returncode)
        return 1
    except KeyboardInterrupt:
        return 130
    log("")
    log("构建完成：")
    for path in [BACKEND, DIST_ELECTRON / "Ruodao2API.exe",
                 DIST_ELECTRON / "Ruodao2API-Setup-0.1.0.exe"]:
        if path.exists():
            log("  %s（%.1f MB）" % (path, path.stat().st_size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
