#!/bin/bash
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "尚未完成安装，请先双击『安装依赖-macOS.command』。"
  read -r -p "按回车键关闭..."
  exit 1
fi

if [ ! -d "node_modules/promptfoo" ]; then
  echo "未找到 promptfoo，请先双击『安装依赖-macOS.command』。"
  read -r -p "按回车键关闭..."
  exit 1
fi

".venv/bin/python" launcher.py
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "启动失败。若提示端口 8787 被占用，请关闭已运行的 Relay Monitor 后重试。"
  read -r -p "按回车键关闭..."
fi
exit $status
