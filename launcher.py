# -*- coding: utf-8 -*-
"""跨平台启动器：启动 Web 服务并自动打开默认浏览器。"""

import threading
import webbrowser
import ctypes
import sys
import traceback

from app_paths import USER_DATA_DIR


def configure_frozen_output():
    """为无控制台桌面程序配置日志输出，避免依赖空的标准输出流。"""
    if not getattr(sys, "frozen", False):
        return None
    log_dir = USER_DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    return stream


LOG_STREAM = configure_frozen_output()

import uvicorn

import server


def open_browser():
    """在服务开始监听后打开本地管理界面。"""
    webbrowser.open("http://127.0.0.1:8787/")


def main():
    """初始化数据库、启动浏览器定时任务并运行 FastAPI 服务。"""
    try:
        con = server.get_con()
        con.close()
    except Exception as exc:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                0, f"数据库初始化失败：\n{exc}", "Relay Monitor", 0x10
            )
        raise
    threading.Timer(1.2, open_browser).start()
    uvicorn.run(server.app, host="127.0.0.1", port=8787, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                0, f"软件启动失败：\n{exc}\n\n详细信息已写入 logs/app.log",
                "Relay Monitor", 0x10,
            )
        raise
