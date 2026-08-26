#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 build_desktop.py
echo "macOS APP 已生成到 desktop-dist/RelayMonitor.app"
read -r -p "按回车键关闭..."
