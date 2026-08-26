#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "========================================"
echo "Relay Monitor - macOS 一键安装"
echo "========================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 Python 3，请先安装 Python 3.10 或更高版本："
  echo "https://www.python.org/downloads/macos/"
  read -r -p "按回车键关闭..."
  exit 1
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "未找到 Node.js，请先安装 Node.js 18 或更高 LTS 版本："
  echo "https://nodejs.org/"
  read -r -p "按回车键关闭..."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] 正在创建 Python 虚拟环境..."
  python3 -m venv .venv
else
  echo "[1/3] Python 虚拟环境已存在，跳过创建。"
fi

echo "[2/3] 正在安装 Python 依赖..."
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt

echo "[3/3] 正在安装 promptfoo 评测引擎..."
npm install

echo
echo "安装完成。以后双击『启动-macOS.command』即可使用。"
read -r -p "按回车键关闭..."
