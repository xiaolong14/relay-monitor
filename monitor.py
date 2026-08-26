# -*- coding: utf-8 -*-
"""
中转站质量每日监控（promptfoo 封装脚本）

整体流程：
1. 读取 config.json（中转站列表 + 固定题库 + 阈值设置）
2. 为每个中转站生成单 provider 的 promptfoo 配置，调用 promptfoo eval 跑固定题库
3. 解析评分结果，写入本地 SQLite（data/history.db）
4. 将今日分数与近 N 天滑动基线对比，判断「正常 / 疑似掺水 / 恢复」
5. 生成 HTML 日报（reports/日期.html），并推荐当前最优中转站

依赖：Node.js 18+ 与 promptfoo（npm install -g promptfoo）、Python 3.8+（仅标准库）
"""

import collections
import datetime
import html
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from concurrent import futures
from pathlib import Path

from app_paths import RESOURCE_DIR, USER_DATA_DIR

# ---------------- 目录与文件路径常量 ----------------
BASE_DIR = RESOURCE_DIR
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_QUESTIONS_PATH = BASE_DIR / "default_aime_30.json"
GEN_DIR = USER_DATA_DIR / "generated"      # promptfoo 生成的配置与结果
DATA_DIR = USER_DATA_DIR / "data"          # SQLite 历史库目录
REPORT_DIR = USER_DATA_DIR / "reports"     # HTML 日报输出目录
DB_PATH = DATA_DIR / "history.db"


def load_config():
    """读取并校验 config.json，缺少文件或关键配置时给出明确提示后退出。"""
    if not CONFIG_PATH.exists():
        sys.exit("缺少 config.json，请在 relay-monitor 目录下创建并填写中转站信息")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("relays"):
        sys.exit("config.json 中没有配置任何中转站（relays 为空）")
    if not cfg.get("questions"):
        sys.exit("config.json 中没有题库（questions 为空）")
    return cfg


def yaml_str(value):
    """把任意值序列化为 YAML 安全的双引号字符串（JSON 双引号串是合法 YAML）。"""
    return json.dumps(str(value), ensure_ascii=False)


def gen_provider_yaml(relay, questions, max_tokens):
    """为单个中转站生成 promptfoo 的 YAML 配置内容（单 provider + 固定测试题）。

    protocol 为 openai 时 baseUrl 需带 /v1；为 anthropic 时 baseUrl 不带 /v1。
    """
    if relay.get("protocol", "openai") == "anthropic":
        provider_id = f"anthropic:messages:{relay['model']}"
    else:
        provider_id = f"openai:chat:{relay['model']}"

    lines = [
        "description: " + yaml_str(f"每日监控-{relay['label']}"),
        "prompts:",
        "  - " + yaml_str("{{question}}"),
        "providers:",
        "  - id: " + yaml_str(provider_id),
        "    label: " + yaml_str(relay["label"]),
        "    config:",
        "      apiBaseUrl: " + yaml_str(relay["baseUrl"]),
        "      apiKey: " + yaml_str(relay["apiKey"]),
        "      temperature: 0",
        "      max_tokens: " + str(int(max_tokens)),
        "tests:",
    ]
    for item in questions:
        ans = str(item["answer"]).strip()
        # 纯数字答案用数值精确匹配（防止答案 72 误匹配 172）；其他答案用 contains
        # 注意：promptfoo 的 javascript 断言要求代码以 return 开头（函数体形式）
        if re.fullmatch(r"-?\d+(\.\d+)?", ans):
            # 注意：promptfoo 仅当代码包含换行时才按"函数体"解析，
            # 单行会被包成 return (表达式) 导致语法错误，因此前置一个换行
            js_code = (
                "\nreturn ((output||'').match(/-?\\d+(?:\\.\\d+)?/g) || [])"
                f".map(Number).includes(Number('{ans}'));"
            )
            assert_lines = [
                "      - type: javascript",
                "        value: " + yaml_str(js_code),
            ]
        else:
            assert_lines = [
                "      - type: contains",
                "        value: " + yaml_str(ans),
            ]
        lines += [
            "  - vars:",
            "      question: " + yaml_str(item["q"]),
            "    assert:",
        ] + assert_lines
    return "\n".join(lines) + "\n"


def _result_rows(data):
    """返回 promptfoo 结果对象中的逐题结果列表。"""
    rows = data.get("results") or []
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    return rows if isinstance(rows, list) else []


def _rate_limited_questions(out_json):
    """返回真正因 429 且未获得模型输出的失败题目文本。"""
    try:
        with open(out_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    questions = []
    for r in _result_rows(data):
        err = str(r.get("error") or "")
        output = (r.get("response") or {}).get("output")
        is_rate_limit = "429" in err or "rate limit" in err.lower() or "too many requests" in err.lower()
        if is_rate_limit and (output is None or not str(output).strip()):
            question = ((r.get("vars") or {}).get("question") or
                        ((r.get("testCase") or {}).get("vars") or {}).get("question"))
            if question:
                questions.append(question)
    return list(dict.fromkeys(questions))


def _merge_retry_results(original_path, retry_path, retry_questions):
    """用并发 1 的重测结果替换原结果中对应的 429 失败题。"""
    with open(original_path, "r", encoding="utf-8") as f:
        original = json.load(f)
    with open(retry_path, "r", encoding="utf-8") as f:
        retry = json.load(f)
    replacement = {}
    for row in _result_rows(retry):
        question = ((row.get("vars") or {}).get("question") or
                    ((row.get("testCase") or {}).get("vars") or {}).get("question"))
        if question:
            replacement[question] = row
    rows = _result_rows(original)
    for index, row in enumerate(rows):
        question = ((row.get("vars") or {}).get("question") or
                    ((row.get("testCase") or {}).get("vars") or {}).get("question"))
        if question in retry_questions and question in replacement:
            rows[index] = replacement[question]
    with open(original_path, "w", encoding="utf-8") as f:
        json.dump(original, f, ensure_ascii=False)


def new_control():
    """创建一个评测控制字典（线程间共享）。

    键说明：stop=请求停止；paused=请求暂停；procs=当前全部 promptfoo 子进程列表
    （并行评测时多个站点各一个进程，暂停/停止作用于全部）。
    """
    return {"stop": False, "paused": False, "procs": []}


def suspend_process(control, suspend=True):
    """挂起/恢复当前全部 promptfoo 子进程（真·进程级暂停，依赖 psutil）。

    返回成功操作的进程数；无子进程/无 psutil 时返回 0
    （此时暂停退化为「站点边界协作式暂停」）。
    """
    procs = (control or {}).get("procs") or []
    if not procs:
        return 0
    try:
        import psutil
    except ImportError:
        return 0
    ok = 0
    for proc in list(procs):
        try:
            p = psutil.Process(proc.pid)
            if p.status() == psutil.STATUS_ZOMBIE:
                continue
            if suspend:
                p.suspend()
            else:
                p.resume()
            ok += 1
        except Exception:
            continue
    return ok


def kill_all_processes(control):
    """杀掉控制字典中登记的全部子进程（强制停止用）。"""
    procs = (control or {}).get("procs") or []
    killed = 0
    for proc in list(procs):
        try:
            proc.kill()
            killed += 1
        except Exception:
            continue
    return killed


def _run_streaming(cmd, log=print, control=None, pf_config_dir=None):
    """流式执行 promptfoo 命令：逐行读取输出，解析逐题进度并按批回报。

    进度来源：LOG_LEVEL=debug 时 promptfoo 逐题打印
    「Eval #N complete (M of T)」。每完成 5 题打一条进度，结束时打最终进度。
    control 非空时：把子进程登记到 control["procs"]（支持并行多进程），
    读行循环中检测 control["stop"] 即 kill 子进程实现强制停止。
    pf_config_dir 指定 promptfoo 独立配置目录（并行时隔离各自的 SQLite DB）。
    返回 (returncode, 输出末尾文本)；输出仅保留最近 60 行用于错误诊断。
    """
    env = {**os.environ, "LOG_LEVEL": "debug", "PROMPTFOO_LOG_LEVEL": "debug"}
    if pf_config_dir:
        Path(pf_config_dir).mkdir(parents=True, exist_ok=True)
        env["PROMPTFOO_CONFIG_DIR"] = str(pf_config_dir)
    popen_options = {}
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        popen_options["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd + ["--no-progress-bar"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        cwd=str(GEN_DIR),
        env=env,
        **popen_options,
    )
    if control is not None:
        control["procs"].append(proc)
    progress_re = re.compile(r"Eval #\d+ complete \((\d+) of (\d+)\)")
    tail = collections.deque(maxlen=60)  # 保留最近 60 行用于报错
    done = last_reported = 0
    total = None
    retry_count = 0   # 瞬时失败但已自动重试的次数（非题目失败）
    fatal_count = 0   # 重试耗尽、题目彻底失败的次数
    try:
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            # 强制停止：杀掉子进程并退出读取循环
            if control is not None and control.get("stop"):
                proc.kill()
                break
            # 区分瞬时重试与彻底失败：
            # - promptfoo 自动重试（默认4次）打 "Request ... failed (attempt #N), retrying"
            #   和 "Rate limited on URL ... waiting before retry" —— 大多能重试成功
            # - 重试耗尽才打 "Request failed after N retries" —— 这才是题目失败
            low = line.lower()
            if "failed (attempt #" in low or "rate limited on url" in low \
                    or "rate limited, waiting" in low:
                retry_count += 1
            elif "request failed after" in low and "retries" in low:
                fatal_count += 1
            m = progress_re.search(line)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                # 每完成 5 题汇报一次；最后一题必汇报
                if done % 5 == 0 or done == total:
                    if done != last_reported:
                        parts = []
                        if retry_count:
                            parts.append(f"自动重试 {retry_count} 次")
                        if fatal_count:
                            parts.append(f"{fatal_count} 题请求失败")
                        extra = f"（{'，'.join(parts)}）" if parts else ""
                        log(f"进度 {done}/{total}{extra}")
                        last_reported = done
    finally:
        proc.wait()
        if control is not None and proc in control["procs"]:
            control["procs"].remove(proc)
    return proc.returncode, "\n".join(tail)


def run_promptfoo(yaml_path, concurrency=5, fallback_concurrency=1, log=print,
                  control=None, tag=None, relay=None, questions=None, max_tokens=512):
    """调用 promptfoo eval 执行测试并解析结果。

    返回 (score, passed, total, latency_ms, error_info)。
    - tag 为站点标识：并行评测时用独立的输出文件（results_{tag}.json）与
      promptfoo 配置目录（pfhome_{tag}，隔离各自的 SQLite DB，避免锁冲突）
    - 通过 node 直接运行本地 promptfoo 入口（规避含空格路径下 cmd /c 的解析问题）
    - 实时进度：流式读取输出，每完成 5 题通过 log 回调打一条进度
    - 并发控制：先用 concurrency（默认 5）执行；若有题目因 429 未获得输出，
      立即将这些失败题以并发 1 增量重测，成功题和普通答错题不会重复请求
    - 退出码约定：0=全部通过，100=完成但有失败题（正常计分），其余视为执行错误
    - 优先带 --no-cache 保证真实调用 API；旧版本不识别时自动去掉该参数重试
    - control["stop"] 被置位时：杀掉子进程并以 SystemExit("已停止") 终止
    """
    bundled_node = BASE_DIR / "node_runtime" / ("node.exe" if sys.platform == "win32" else "bin/node")
    node_exe = str(bundled_node) if bundled_node.exists() else shutil.which("node")
    main_js = BASE_DIR / "node_modules" / "promptfoo" / "dist" / "src" / "main.js"
    if node_exe and main_js.exists():
        launcher = [node_exe, str(main_js)]
    else:
        exe = shutil.which("promptfoo")
        if not exe:
            sys.exit("未找到 promptfoo，请在项目目录执行: npm install promptfoo（需要 Node.js 18+）")
        launcher = [exe]

    # 并行隔离：每站独立的结果文件与 promptfoo 配置目录
    if tag:
        out_json = GEN_DIR / f"results_{tag}.json"
        pf_dir = GEN_DIR / f"pfhome_{tag}"
    else:
        out_json = GEN_DIR / "results.json"
        pf_dir = None
    base_cmd = launcher + ["eval", "-c", str(yaml_path), "-o", str(out_json),
                           "-j", str(concurrency)]

    # 首轮：流式执行（实时进度）；--no-cache 不被识别时去掉该参数重试
    rc, out_tail = 1, ""
    for extra in (["--no-cache"], []):
        rc, out_tail = _run_streaming(base_cmd + extra, log=log, control=control,
                                      pf_config_dir=pf_dir)
        # 用户请求停止：不再重试/降级，直接终止
        if control is not None and control.get("stop"):
            sys.exit("已停止")
        if rc in (0, 100):
            if not extra:
                print("警告：当前 promptfoo 不支持 --no-cache，若每日结果完全相同请升级")
            break
    if rc not in (0, 100):
        print("[promptfoo]", out_tail[-1500:])
        sys.exit(f"promptfoo eval 执行失败（{yaml_path.name}），请检查 baseUrl/key/模型名是否正确")

    rate_questions = _rate_limited_questions(out_json)
    if rate_questions and relay and questions:
        retry_items = [item for item in questions if item.get("q") in set(rate_questions)]
        retry_yaml = GEN_DIR / f"retry_429_{tag or 'single'}.yaml"
        retry_json = GEN_DIR / f"retry_429_{tag or 'single'}.json"
        retry_yaml.write_text(gen_provider_yaml(relay, retry_items, max_tokens), encoding="utf-8")
        log(f"检测到 {len(retry_items)} 题遭遇 429，立即降为并发 1，仅重新评测失败题")
        fallback_cmd = launcher + ["eval", "-c", str(retry_yaml), "-o", str(retry_json),
                                   "-j", "1", "--no-cache"]
        rc, out_tail = _run_streaming(fallback_cmd, log=log, control=control,
                                      pf_config_dir=pf_dir)
        if control is not None and control.get("stop"):
            sys.exit("已停止")
        if rc not in (0, 100):
            print("[promptfoo]", out_tail[-1500:])
            sys.exit(f"promptfoo eval 执行失败（{yaml_path.name}），请检查 baseUrl/key/模型名是否正确")
        _merge_retry_results(out_json, retry_json, set(rate_questions))
        remaining = len(_rate_limited_questions(out_json))
        log(f"429 失败题增量重测完成，恢复 {len(retry_items) - remaining} 题，仍失败 {remaining} 题")

    return parse_results(out_json)


def classify_error(err_text):
    """把 promptfoo 的错误文本归类为简短的中文说明（用于日志/标注）。"""
    t = (err_text or "").lower()
    if "timeout" in t or "timed out" in t or "etimedout" in t:
        return "请求超时"
    if "429" in t or "rate limit" in t or "too many requests" in t:
        return "限流(429)"
    if "401" in t or "unauthorized" in t or "api key" in t or "invalid_api_key" in t:
        return "认证失败(Key无效)"
    if "404" in t or "not found" in t or "model" in t and ("not" in t and "exist" in t):
        return "模型或路径不存在"
    if "econnrefused" in t or "enotfound" in t or "network" in t or "fetch failed" in t or "econnreset" in t:
        return "网络连接失败"
    if "abort" in t or "cancelled" in t or "canceled" in t:
        return "请求被中断"
    return "API错误"


def parse_results(path):
    """解析 promptfoo 输出的 JSON，汇总为分数、通过数、延迟与错误统计。

    返回 (score, passed, total, latency, error_info)：
    - 真错误题（超时/限流/网络失败等模型未作答：output 为空）不计入分母
    - 注意：promptfoo 会把「断言失败（答错）」也写入 error 字段——这类题
      模型有正常输出，必须按 0 分计入分母（否则分数虚高）
    - error_info 为 None（无真错误）或 dict{count, detail, ratio}
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = data.get("results") or []
    # 兼容不同版本输出结构：results 可能是列表，也可能嵌套在 results.results
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    if not rows:
        sys.exit("promptfoo 结果为空，请检查题库与中转站配置")

    scores, latencies = [], []
    passed = 0
    errors = []  # [(错误类型, 原始文本前80字符)] — 仅真错误（模型未作答）
    for r in rows:
        err = r.get("error")
        output = (r.get("response") or {}).get("output")
        has_output = output is not None and str(output).strip() != ""
        if err and not has_output:
            # 真错误（无模型输出）：不参与计分
            err_text = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
            errors.append((classify_error(err_text), err_text[:80]))
            continue
        # 正常作答（含答错）：计入分母
        s = r.get("score")
        if s is None:
            s = 1 if r.get("success") else 0
        scores.append(float(s))
        if r.get("success"):
            passed += 1
        if r.get("latencyMs"):
            latencies.append(float(r["latencyMs"]))

    valid = len(scores)
    if valid == 0:
        # 全部出错：无有效作答，分数置 None 由上层标注
        score = None
    else:
        score = round(sum(scores) / valid * 100, 1)
    latency = round(sum(latencies) / len(latencies)) if latencies else 0

    error_info = None
    if errors:
        # 汇总错误类型计数，如 "请求超时×3"
        counter = collections.Counter(et for et, _ in errors)
        detail = "、".join(f"{et}×{n}" for et, n in counter.most_common())
        error_info = {
            "count": len(errors),
            "detail": detail,
            "ratio": round(len(errors) / len(rows), 3),
        }
    return score, passed, valid, latency, error_info


def init_db():
    """初始化全部 SQLite 基础表，并兼容历史运行表结构迁移。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)"
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS relays(
               name TEXT PRIMARY KEY,
               label TEXT NOT NULL,
               protocol TEXT NOT NULL DEFAULT 'openai',
               baseUrl TEXT NOT NULL,
               apiKey TEXT NOT NULL,
               model TEXT NOT NULL,
               enabled INTEGER NOT NULL DEFAULT 1)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS questions(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               q TEXT NOT NULL,
               answer TEXT NOT NULL,
               version_id INTEGER)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS question_versions(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               label TEXT NOT NULL,
               source TEXT DEFAULT '',
               created_at TEXT NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS runs(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               run_id INTEGER NOT NULL,
               date TEXT NOT NULL,
               relay TEXT NOT NULL,
               relay_label TEXT,
               model_id TEXT,
               score REAL,
               passed INTEGER,
               total INTEGER,
               latency_ms REAL,
               error_detail TEXT,
               created_at TEXT NOT NULL)"""
    )
    # 旧表迁移：无 run_id 列（旧主键 date+relay，一天只存一次）→ 重建为多次记录表
    cols = [r[1] for r in con.execute("PRAGMA table_info(runs)")]
    if cols and "run_id" not in cols:
        con.execute("ALTER TABLE runs RENAME TO runs_old")
        con.execute(
            """CREATE TABLE runs(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   run_id INTEGER NOT NULL,
                   date TEXT NOT NULL,
                   relay TEXT NOT NULL,
                   relay_label TEXT,
                   model_id TEXT,
                   score REAL,
                   passed INTEGER,
                   total INTEGER,
                   latency_ms REAL,
                   error_detail TEXT,
                   created_at TEXT NOT NULL)"""
        )
        # 旧数据每行视为独立一次评测；error_detail 列不存在时补 NULL
        has_err = "error_detail" in cols
        err_col = "error_detail" if has_err else "NULL"
        con.execute(
            f"""INSERT INTO runs(run_id, date, relay, score, passed, total,
                                latency_ms, error_detail, created_at)
                SELECT rowid, date, relay, score, passed, total,
                       latency_ms, {err_col}, date || ' 00:00:00'
                FROM runs_old"""
        )
        con.execute("DROP TABLE runs_old")
    cols = [r[1] for r in con.execute("PRAGMA table_info(runs)")]
    if "relay_label" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN relay_label TEXT")
    if "model_id" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN model_id TEXT")
    con.commit()
    return con


def aggregate_day(con, relay_name, date_str):
    """聚合某站某天的全部评测记录。

    返回 dict：score=加权平均分（按有效题数加权），passed/total=求和，
    latency=平均延迟，error_detail=去重合并的错误说明，count=评测次数，
    runs=每次评测明细列表；无记录返回 None。
    """
    rows = con.execute(
        "SELECT run_id, score, passed, total, latency_ms, error_detail, created_at, "
        "relay_label, model_id "
        "FROM runs WHERE relay=? AND date=? ORDER BY id",
        (relay_name, date_str),
    ).fetchall()
    if not rows:
        return None

    valid = [r for r in rows if r[1] is not None]
    if valid:
        # 加权平均：题目多的那次评测权重更大
        weights = [(r[3] or 0) for r in valid]
        if sum(weights) > 0:
            score = round(sum(r[1] * (r[3] or 0) for r in valid) / sum(weights), 1)
        else:
            score = round(sum(r[1] for r in valid) / len(valid), 1)
    else:
        score = None  # 全部出错，无有效分数

    passed_sum = sum(r[2] or 0 for r in rows)
    total_sum = sum(r[3] or 0 for r in rows)
    lats = [r[4] for r in rows if r[4]]
    latency = round(sum(lats) / len(lats)) if lats else 0
    errors = [r[5] for r in rows if r[5]]
    error_detail = "；".join(dict.fromkeys(errors)) if errors else None

    runs = [{
        "run_id": r[0], "score": r[1], "passed": r[2], "total": r[3],
        "latency": r[4], "error": r[5], "time": (r[6] or "")[11:16],
        "relay_label": r[7] or relay_name, "model_id": r[8] or "未记录",
    } for r in rows]
    return {
        "score": score, "passed": passed_sum, "total": total_sum,
        "latency": latency, "error_detail": error_detail,
        "count": len(rows), "runs": runs,
    }


def ensure_question_versions(con):
    """迁移题库表以支持多版本：建 question_versions 表、questions 增加
    version_id 列，并把存量题目打包为一个「初始题库」版本（只执行一次）。"""
    con.execute(
        """CREATE TABLE IF NOT EXISTS question_versions(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               label TEXT NOT NULL,
               source TEXT DEFAULT '',
               created_at TEXT NOT NULL)"""
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(questions)")]
    if cols and "version_id" not in cols:
        con.execute("ALTER TABLE questions ADD COLUMN version_id INTEGER")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = con.execute(
            "INSERT INTO question_versions(label, source, created_at) VALUES(?,?,?)",
            ("初始题库", "config", now),
        )
        con.execute("UPDATE questions SET version_id=?", (cur.lastrowid,))
    con.commit()


def active_question_version(con):
    """返回当前活动的题库版本 id（settings 里未指定时取最新版本）。"""
    row = con.execute(
        "SELECT value FROM settings WHERE key='active_question_version'"
    ).fetchone()
    if row and row[0]:
        return int(row[0])
    row = con.execute("SELECT MAX(id) FROM question_versions").fetchone()
    return row[0] if row and row[0] else None


def load_questions(con):
    """读取活动题库；全新数据库默认导入固定的 AIME 30 题版本。"""
    con.execute(
        "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)"
    )
    ensure_question_versions(con)
    vid = active_question_version(con)
    if vid is not None:
        rows = con.execute(
            "SELECT q, answer FROM questions WHERE version_id=? ORDER BY id", (vid,)
        ).fetchall()
        if rows:
            return [{"q": q, "answer": a} for q, a in rows]
    if DEFAULT_QUESTIONS_PATH.exists():
        with open(DEFAULT_QUESTIONS_PATH, "r", encoding="utf-8") as f:
            items = json.load(f)
        label = "aime 30题 默认题库"
        source = "aime"
    else:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            items = json.load(f).get("questions", [])
        label = "config 初始题库"
        source = "config"
    if not items:
        return []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    cur = con.execute(
        "INSERT INTO question_versions(label, source, created_at) VALUES(?,?,?)",
        (label, source, now),
    )
    vid = cur.lastrowid
    for it in items:
        con.execute("INSERT INTO questions(q, answer, version_id) VALUES(?,?,?)",
                    (it["q"], it["answer"], vid))
    con.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES('active_question_version',?)",
        (str(vid),),
    )
    con.commit()
    return items


def load_relays(con):
    """优先从数据库读中转站列表；库为空时用 config.json 初始化并返回。"""
    con.execute(
        """CREATE TABLE IF NOT EXISTS relays(
               name TEXT PRIMARY KEY,
               label TEXT NOT NULL,
               protocol TEXT NOT NULL DEFAULT 'openai',
               baseUrl TEXT NOT NULL,
               apiKey TEXT NOT NULL,
               model TEXT NOT NULL,
               enabled INTEGER NOT NULL DEFAULT 1)"""
    )
    rows = con.execute(
        "SELECT name, label, protocol, baseUrl, apiKey, model, enabled FROM relays ORDER BY rowid"
    ).fetchall()
    if rows:
        return [dict(zip(("name", "label", "protocol", "baseUrl", "apiKey",
                          "model", "enabled"), r)) for r in rows]
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for r in cfg.get("relays", []):
        con.execute(
            "INSERT INTO relays(name, label, protocol, baseUrl, apiKey, model, enabled) "
            "VALUES(?,?,?,?,?,?,1)",
            (r["name"], r["label"], r.get("protocol", "openai"),
             r["baseUrl"], r["apiKey"], r["model"]),
        )
    con.commit()
    return cfg.get("relays", [])


def scores_before(con, relay_name, date_str, limit):
    """取某日期之前（不含当天）最近 N 天的 (date, 日均分) 记录，按日期倒序。

    一天多次评测时按当日加权平均聚合为单日分数。
    """
    rows = con.execute(
        "SELECT date, score, total FROM runs "
        "WHERE relay=? AND date<? AND score IS NOT NULL ORDER BY date DESC",
        (relay_name, date_str),
    ).fetchall()
    # 按日期聚合加权平均分
    by_date = {}
    for d, s, t in rows:
        by_date.setdefault(d, []).append((s, t or 0))
    daily = []
    for d, items in by_date.items():
        w = sum(t for _, t in items)
        if w > 0:
            daily.append((d, round(sum(s * t for s, t in items) / w, 1)))
        else:
            daily.append((d, round(sum(s for s, _ in items) / len(items), 1)))
    daily.sort(key=lambda x: x[0], reverse=True)
    return daily[:limit]


def day_status(con, relay_name, date_str, settings):
    """计算某站点某一天的状态（基于当天全部评测的聚合分）。

    返回 (状态, 基线, 当日聚合分)。状态取值：无数据 / 观察期 / 正常 / 疑似掺水 / 评测异常。
    基线 = 该日之前 baseline_days 天的日均分均值；历史不足 min_history_days 天记为观察期。
    全天错误（无有效分数）时返回「评测异常」，不参与掺水判定。
    """
    agg = aggregate_day(con, relay_name, date_str)
    if agg is None:
        return ("无数据", None, None)

    day_score = agg["score"]
    if day_score is None:  # 全部题目出错，无有效作答
        return ("评测异常", None, None)

    hist = scores_before(con, relay_name, date_str, settings["baseline_days"])
    if len(hist) < settings["min_history_days"]:
        return ("观察期", None, day_score)

    baseline = statistics.mean(s for _, s in hist)
    if day_score < baseline - settings["drop_threshold"]:
        return ("疑似掺水", round(baseline, 1), day_score)
    return ("正常", round(baseline, 1), day_score)


def evaluate(con, relay, today, score, passed, total, latency, settings,
             error_info=None, run_id=None):
    """插入一次评测记录（同一天多次评测各自保留），返回当日聚合行。

    error_info 非 None 时（存在错误题）：error_detail 存入 runs 表；
    score 为 None（全部出错）时不写分数（NULL 表示无有效数据）。
    run_id 标识同一轮评测（并行时多个站点共享）；缺省按时间戳生成。
    """
    if run_id is None:
        run_id = int(time.time())
    error_detail = error_info["detail"] if error_info else None
    created = datetime.datetime.now().isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO runs(run_id, date, relay, relay_label, model_id, score, passed, "
        "total, latency_ms, error_detail, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, today, relay["name"], relay["label"], relay["model"], score,
         passed, total, latency, error_detail, created),
    )
    con.commit()

    # 状态基于当天全部评测的聚合结果
    agg = aggregate_day(con, relay["name"], today)
    status, baseline, _ = day_status(con, relay["name"], today, settings)

    # 计算昨日状态，用于识别「状态切换」事件（新发掺水 / 恢复）
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    y_status, _, _ = day_status(con, relay["name"], yesterday, settings)

    if status == "评测异常":
        event = f"评测异常：{agg['error_detail'] or '全部题目出错'}"
    elif status == "观察期":
        event = "基线积累中"
    elif status == "疑似掺水" and y_status != "疑似掺水":
        event = "新增掺水嫌疑：今日分数明显低于基线"
    elif status == "正常" and y_status == "疑似掺水":
        event = "已恢复正常"
    elif status == "疑似掺水":
        event = "持续掺水中"
    else:
        event = "稳定"

    # 附加说明：多次评测与错误剔除
    notes = []
    if agg["count"] > 1:
        notes.append(f"今日{agg['count']}次评测取平均")
    if error_info and status != "评测异常":
        notes.append(f"本次{error_info['count']}题出错已剔除")
    if notes:
        event += "（" + "，".join(notes) + "）"

    agg_score = agg["score"]
    diff = round(agg_score - baseline, 1) if (agg_score is not None and baseline is not None) else None
    return {
        "label": relay["label"],
        "name": relay["name"],
        "score": agg_score,
        "baseline": baseline,
        "diff": diff,
        "passed": agg["passed"],
        "total": agg["total"],
        "latency": agg["latency"],
        "status": status,
        "event": event,
        "error_detail": agg["error_detail"],
        "run_count": agg["count"],
        "runs": agg["runs"],
    }


def pick_best(rows):
    """挑选当日推荐站点：优先「正常」中分数最高者，其次观察期，全部掺水则选相对最优。

    「评测异常」的站点不参与推荐（无有效分数）。返回 (最佳站点行, 备注说明)。
    """
    # 有效行：有分数且不是评测异常
    valid = [r for r in rows if r.get("score") is not None and r["status"] != "评测异常"]
    if not valid:
        return None, "（所有站点评测异常，无可推荐站点）"
    rank = lambda r: (r["score"], -r["latency"])
    normal = [r for r in valid if r["status"] == "正常"]
    if normal:
        return max(normal, key=rank), ""
    observing = [r for r in valid if r["status"] == "观察期"]
    if observing:
        return max(observing, key=rank), "（当前无完全正常站点，从观察期站点中选出）"
    return max(valid, key=rank), "（所有站点均疑似掺水，以下为相对最优，建议谨慎使用）"


def history_rows(con, relay_name, today, days=7):
    """取某站最近 N 天（含当天）的日均分序列（多次评测聚合），用于日报走势。"""
    rows = con.execute(
        "SELECT date, score, total FROM runs "
        "WHERE relay=? AND date<=? AND score IS NOT NULL ORDER BY date DESC",
        (relay_name, today),
    ).fetchall()
    by_date = {}
    for d, s, t in rows:
        by_date.setdefault(d, []).append((s, t or 0))
    daily = []
    for d, items in by_date.items():
        w = sum(t for _, t in items)
        if w > 0:
            daily.append((d, round(sum(s * t for s, t in items) / w, 1)))
        else:
            daily.append((d, round(sum(s for s, _ in items) / len(items), 1)))
    daily.sort(key=lambda x: x[0], reverse=True)
    return daily[:days]


def render_html(today, rows, best, note, con):
    """拼接当日 HTML 日报字符串（含总表、推荐卡片、近 7 天走势，Lucide 图标）。"""
    esc = html.escape
    css = (
        "body{font-family:'Microsoft YaHei',sans-serif;max-width:960px;margin:24px auto;"
        "padding:0 16px;color:#222}"
        "h1{font-size:20px}"
        "table{border-collapse:collapse;width:100%;margin:12px 0}"
        "th,td{border:1px solid #ddd;padding:8px;text-align:center;font-size:14px}"
        "th{background:#f5f5f5}"
        ".ok{color:#1a7f37;font-weight:bold}.bad{color:#d1242f;font-weight:bold}"
        ".warn{color:#9a6700;font-weight:bold}"
        ".card{border:1px solid #d0d7de;border-radius:8px;padding:14px 18px;"
        "margin:14px 0;background:#f6f8fa}"
        ".rec{border-color:#1a7f37;background:#dafbe1;font-size:15px}"
        ".muted{color:#666;font-size:12px}"
        ".lucide{width:14px;height:14px;vertical-align:-2px;margin-right:2px}"
    )
    ICON_CDN = ("https://cdn.jsdelivr.net/npm/lucide@0.462.0/dist/umd/lucide.min.js")

    def status_class(status):
        """根据状态返回对应的 CSS 颜色类名。"""
        if status == "疑似掺水":
            return "bad"
        if status == "正常":
            return "ok"
        if status == "评测异常":
            return "bad"
        return "warn"

    def status_icon(status):
        """根据状态返回 Lucide 图标名。"""
        if status == "疑似掺水":
            return "circle-x"
        if status == "正常":
            return "circle-check"
        if status == "评测异常":
            return "circle-alert"
        return "eye"

    def event_icon(event):
        """根据事件文本返回 Lucide 图标名。"""
        if "异常" in event:
            return "octagon-alert"
        if "新增" in event:
            return "triangle-alert"
        if "恢复" in event:
            return "trending-up"
        if "持续" in event:
            return "trending-down"
        if "稳定" in event:
            return "circle-check"
        return "clock"

    trs = []
    for r in rows:
        diff = "—" if r["diff"] is None else f"{r['diff']:+.1f}"
        base = "—" if r["baseline"] is None else r["baseline"]
        # 评测异常：分数列显示错误说明而非数字
        score_cell = "未计分" if r["score"] is None else r["score"]
        cls = status_class(r["status"])
        trs.append(
            f"<tr><td>{esc(r['label'])}</td><td>{score_cell}</td><td>{base}</td>"
            f"<td>{diff}</td><td>{r['passed']}/{r['total']}</td>"
            f"<td>{r['latency']:.0f} ms</td>"
            f"<td class='{cls}'><i data-lucide='{status_icon(r['status'])}'></i>{r['status']}</td>"
            f"<td class='{cls}'><i data-lucide='{event_icon(r['event'])}'></i>{r['event']}</td></tr>"
        )

    hist_parts = []
    for r in rows:
        pts = history_rows(con, r["name"], today)
        seq = " → ".join(str(s) for _, s in reversed(pts))
        hist_parts.append(
            f"<p><b>{esc(r['label'])}</b> 近{len(pts)}天分数：{seq}</p>"
        )

    # 推荐卡：全部异常时显示警示说明
    if best is not None:
        reco_html = (
            f"<div class='card rec'><i data-lucide='award'></i><b>今日推荐：</b>"
            f"{esc(best['label'])}（今日分数 {best['score']}，状态 {best['status']}）"
            f"{esc(note)}</div>"
        )
    else:
        reco_html = (
            f"<div class='card' style='border-color:#d1242f'>"
            f"<i data-lucide='octagon-alert'></i><b>今日无推荐：</b>{esc(note)}</div>"
        )

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>中转站质量日报 {today}</title>"
        f"<script src='{ICON_CDN}'></script>"
        f"<style>{css}</style></head><body>"
        f"<h1>中转站质量日报 · {today}</h1>"
        + reco_html
        + "<table><tr><th>站点</th><th>今日分数</th><th>基线</th><th>差值</th>"
        "<th>通过题数</th><th>平均延迟</th><th>状态</th><th>事件</th></tr>"
        + "".join(trs)
        + "</table>"
        + "<div class='card'><b>近 7 天走势</b>" + "".join(hist_parts) + "</div>"
        + "<p class='muted'>判定规则：今日分数低于滑动基线超过阈值记为「疑似掺水」；"
        "历史数据不足记为「观察期」；全部题目出错记为「评测异常」不参与判定。</p>"
        + "<script>lucide.createIcons()</script>"
        + "</body></html>"
    )


def run_all(relays=None, settings=None, questions=None, log=print, control=None):
    """执行一轮完整评测（可被 CLI 与 Web 服务共用）。

    参数 relays/settings/questions 缺省时从 DB（首次回落 config.json）读取。
    control 为共享控制字典（见 new_control）：支持强制停止与协作式暂停。
    返回 (结果行列表, 最优站点, 备注)。每个中转站评测失败时记 0 分并继续。
    """
    con = init_db()
    if relays is None:
        relays = [r for r in load_relays(con) if r.get("enabled", 1)]
    if questions is None:
        questions = load_questions(con)
    if settings is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f).get("settings", {})
        settings = {
            "baseline_days": raw.get("baseline_days", 7),
            "min_history_days": raw.get("min_history_days", 3),
            "drop_threshold": raw.get("drop_threshold", 8),
        }
    max_tokens = settings.get("max_tokens", 512)
    concurrency = int(settings.get("concurrency", 5))
    fallback_cc = 1
    station_parallel = int(settings.get("station_parallel", 10))  # 站级并行上限

    GEN_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    today = datetime.date.today().isoformat()
    run_id = int(time.time())  # 本轮共享 run_id（同轮多站并行）

    def eval_relay(relay):
        """并行 worker：执行单站评测（独立输出文件/配置目录），返回原始结果。

        不做任何 DB 写入（SQLite 连接仅主线程使用）。
        返回 (relay, (score,passed,total,latency,error_info) 或 None, 失败原因或 None)。
        """
        if control is not None and control.get("stop"):
            return relay, None, "stopped"
        # 协作式暂停：未开始的站点等待恢复（进行中的由进程挂起处理）
        while control is not None and control.get("paused") and not control.get("stop"):
            time.sleep(0.5)
        if control is not None and control.get("stop"):
            return relay, None, "stopped"
        label = relay["label"]
        try:
            yaml_path = GEN_DIR / f"config_{relay['name']}.yaml"
            yaml_path.write_text(
                gen_provider_yaml(relay, questions, max_tokens), encoding="utf-8"
            )
            result = run_promptfoo(
                yaml_path, concurrency=concurrency,
                fallback_concurrency=fallback_cc,
                log=lambda m, lbl=label: log(f"[{lbl}] {m}"),
                control=control, tag=relay["name"], relay=relay,
                questions=questions, max_tokens=max_tokens,
            )
            return relay, result, None
        except SystemExit as e:
            if control is not None and control.get("stop"):
                return relay, None, "stopped"
            return relay, None, str(e)

    log(f"开始评测（{len(relays)} 个站点并行，站内并发 {concurrency}）")
    rows = []
    max_workers = max(1, min(station_parallel, len(relays))) if relays else 1
    with futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(eval_relay, r) for r in relays]
        for fut in futures.as_completed(futs):
            relay, result, err = fut.result()
            label = relay["label"]
            if err == "stopped":
                log(f"[{label}] 已被用户停止")
                continue
            if err is not None:
                # 单站整体失败（如 Key 无效/站点不可达）：记评测异常，不写分数
                log(f"[{label}] 评测失败: {err}")
                score, passed, total, latency, error_info = (
                    None, 0, 0, 0,
                    {"count": len(questions), "detail": err, "ratio": 1.0},
                )
            else:
                score, passed, total, latency, error_info = result
            # 有错误题时：打印错误统计（不计入分数）
            if error_info:
                log(f"[{label}] [错误] {error_info['count']} 题出错："
                    f"{error_info['detail']}（错误题不计入分数）")
            if score is None:
                log(f"[{label}] 全部题目出错，本次不记分（评测异常）")
            else:
                log(f"[{label}] 本次分数: {score}（{passed}/{total}）"
                    f"  平均延迟: {latency:.0f}ms")
            # 主线程写库与判定（聚合当天全部评测）
            row = evaluate(con, relay, today, score, passed, total, latency,
                           settings, error_info=error_info, run_id=run_id)
            if row:
                log(f"[{label}] 今日聚合: {row['score']}（{row['passed']}/{row['total']}，"
                    f"今日第 {row['run_count']} 次评测）")
            rows.append(row)

    best, note = (None, "")
    stopped = control is not None and control.get("stop")
    if rows:
        best, note = pick_best(rows)
        html_text = render_html(today, rows, best, note, con)
        (REPORT_DIR / f"{today}.html").write_text(html_text, encoding="utf-8")
        (REPORT_DIR / "latest.html").write_text(html_text, encoding="utf-8")
        if best is not None:
            log(f"日报已生成，今日推荐: {best['label']}（{best['score']}）{note}")
        else:
            log(f"日报已生成，但无可推荐站点{note}")
    elif stopped:
        log("本轮已被停止，未产生有效结果")
    else:
        log("没有可用的中转站（列表为空或全部停用）")
    con.close()
    return rows, best, note


def main():
    """命令行入口：读取配置后执行一轮评测并打印结论。"""
    rows, best, note = run_all()
    if best:
        print(f"今日推荐站点: {best['label']}（今日分数 {best['score']}）{note}")


if __name__ == "__main__":
    main()
