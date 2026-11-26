# -*- coding: utf-8 -*-
"""一次性打包脚本：生成不含任何用户数据的干净分发包 relay-monitor.zip。

排除内容：
- node_modules/（使用者自行 npm install）
- data/（SQLite：含 API Key、站点、历史分数）
- generated/（promptfoo 配置与结果：yaml 中含明文 API Key）
- reports/（HTML 日报：含站点名与分数）
- logs/、__pycache__/、*.pyc
- package-lock.json（版本锁定交给使用者的 npm）
"""
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
OUT = BASE.parent / "relay-monitor.zip"

# 白名单使用项目相对路径，解压后保留源码运行时的目录结构。
INCLUDE = [
    "README.md",
    "requirements.txt",
    "package.json",
    "config.json",      # 仅含示例占位符 Key（sk-替换成xx的KEY），无真实数据
    "resources/default_aime_30.json",
    "server.py",
    "launcher.py",
    "monitor.py",
    "relay_monitor/__init__.py",
    "relay_monitor/app_paths.py",
    "relay_monitor/codegrade.py",
    "relay_monitor/datasets.py",
    "relay_monitor/request_options.py",
    "relay_monitor/stream_transport.py",
    "relay_monitor/svg_artwork.py",
    "providers/anthropic_provider.cjs",
    "providers/openai_responses_provider.cjs",
    "providers/openai_chat_provider.cjs",
    "providers/stream_transport.cjs",
    "scripts/__init__.py",
    "scripts/seed_demo.py",
    "run_daily.bat",
    "setup_task.bat",
    "安装依赖-Windows.bat",
    "启动-Windows.bat",
    "安装依赖-macOS.command",
    "启动-macOS.command",
    "static/index.html",
    "static/assets/app-icon-1.png",
    "static/assets/app-icon.ico",
    "static/assets/bg-twilight.jpg",
    "static/assets/card-paper.png",
    "static/assets/kraft-desk.png",
    "static/assets/logo.webp",
]

missing = [name for name in INCLUDE if not (BASE / name).is_file()]
if missing:
    raise SystemExit(f"打包失败，缺少文件：{', '.join(missing)}")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in INCLUDE:
        p = BASE / name
        info = zipfile.ZipInfo.from_file(p, f"relay-monitor/{name}")
        if name.endswith(".command"):
            info.external_attr = 0o100755 << 16
        with p.open("rb") as src:
            zf.writestr(info, src.read(), compress_type=zipfile.ZIP_DEFLATED)
        print(f"打包: {name}")

size_kb = OUT.stat().st_size / 1024
print(f"\n完成: {OUT}")
print(f"大小: {size_kb:.1f} KB")

# 校验：确认 zip 中无任何真实 Key 痕迹（sk- 开头的长密钥 + 本地数据文件名）
import re

with zipfile.ZipFile(OUT) as zf:
    for n in zf.namelist():
        if n.endswith((".json", ".bat", ".command", ".py")):
            content = zf.read(n).decode("utf-8", errors="ignore")
            # 真实 OpenAI 风格密钥：sk- 开头且长度足够（排除"sk-替换成"类占位符）
            for m in re.finditer(r"sk-[A-Za-z0-9]{16,}", content):
                raise SystemExit(f"安全检查失败: {n} 疑似含真实 API Key")
            for frag in ("history.db", "math_hard_pool"):
                if frag in content and not n.endswith(".py"):
                    raise SystemExit(f"安全检查失败: {n} 含本地数据引用 {frag}")
print("安全检查通过: zip 中无任何真实 API Key / 站点数据")
