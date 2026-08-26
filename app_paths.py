# -*- coding: utf-8 -*-
"""统一管理源码运行与冻结程序的资源目录和用户数据目录。"""

import os
import platform
import sys
from pathlib import Path


def resource_dir():
    """返回只读资源目录，兼容源码运行和 PyInstaller 冻结程序。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def user_data_dir():
    """返回当前系统的 Relay Monitor 可写用户数据目录。"""
    override = os.environ.get("RELAY_MONITOR_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    system = platform.system()
    if system == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif system == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "RelayMonitor"


RESOURCE_DIR = resource_dir()
USER_DATA_DIR = user_data_dir()
