# -*- coding: utf-8 -*-
"""在当前操作系统构建开箱即用的 Relay Monitor 桌面程序。"""

import platform
import shutil
import subprocess
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parent
DIST = BASE / "desktop-dist"


def run(command):
    """执行构建命令，并在命令失败时立即终止。"""
    print("执行:", " ".join(map(str, command)))
    subprocess.run(command, cwd=BASE, check=True)


def prepare_node_runtime():
    """复制当前平台 Node 可执行文件到应用内置运行时目录。"""
    node = shutil.which("node")
    if not node:
        raise SystemExit("未找到 Node.js，请先安装 Node.js 18-22 LTS")
    target = BASE / "node_runtime"
    if platform.system() == "Windows":
        target.mkdir(exist_ok=True)
        shutil.copy2(node, target / "node.exe")
    else:
        (target / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(node).resolve(), target / "bin" / "node")
        (target / "bin" / "node").chmod(0o755)


def ensure_dependencies():
    """安装锁定的 Node 依赖并确认 PyInstaller 可用。"""
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise SystemExit("未找到 npm")
    run([npm, "ci", "--omit=dev"])
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "pyinstaller>=6.10,<7"])


def build():
    """调用 PyInstaller 构建当前平台原生目录型应用。"""
    separator = ";" if platform.system() == "Windows" else ":"
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "RelayMonitor", "--onedir",
        "--distpath", str(DIST), "--workpath", str(BASE / "build-desktop"),
        "--specpath", str(BASE / "build-desktop"),
        "--add-data", f"{BASE / 'static'}{separator}static",
        "--add-data", f"{BASE / 'config.json'}{separator}.",
        "--add-data", f"{BASE / 'default_aime_30.json'}{separator}.",
        "--add-data", f"{BASE / 'node_modules'}{separator}node_modules",
        "--add-data", f"{BASE / 'node_runtime'}{separator}node_runtime",
        str(BASE / "launcher.py"),
    ]
    args.insert(8, "--windowed")
    run(args)
    print(f"构建完成: {DIST / 'RelayMonitor'}")


def main():
    """依次准备依赖、内置 Node 运行时并构建桌面程序。"""
    ensure_dependencies()
    prepare_node_runtime()
    build()


if __name__ == "__main__":
    main()
