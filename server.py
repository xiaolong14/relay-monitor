# -*- coding: utf-8 -*-
"""
中转站质量监控系统 - Web 服务端

提供：
- REST API：中转站增删改查、题库管理、系统设置、评测触发、历史数据查询
- 内置定时器：按设置的时间每日自动评测（无需 Windows 任务计划）
- 静态页服务：index.html 管理界面

运行：python server.py  （默认 http://127.0.0.1:8787）
依赖：fastapi、uvicorn（pip install fastapi uvicorn）
"""

import datetime
import concurrent.futures
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import datasets
import monitor
import svg_artwork
from app_paths import RESOURCE_DIR
from request_options import (
    DEFAULT_MAX_INPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS, token_budget,
    DEFAULT_API_TIMEOUT_MINUTES, MAX_API_TIMEOUT_MINUTES, api_timeout_seconds,
    DEFAULT_REASONING_EFFORT, AnthropicReasoningEffort, OpenAIReasoningEffort, ReasoningEffort,
    reasoning_params, reasoning_setting_key, resolve_reasoning_effort,
)

BASE_DIR = RESOURCE_DIR
INDEX_PATH = BASE_DIR / "static" / "index.html"

app = FastAPI(title="中转站质量监控系统")

# 全局运行状态（防止并发评测 + 前端轮询进度）
RUN_STATE = {"running": False, "started_at": None, "log": [], "paused": False}
# 评测控制字典（与 monitor 共享：stop/paused/proc）
CONTROL = monitor.new_control()
RUN_LOCK = threading.Lock()
SVG_WORKER_COUNT = 3


# ---------------- 通用工具 ----------------

def get_con():
    """获取完成基础表、站点和初始题库初始化的数据库连接。"""
    con = monitor.init_db()
    monitor.load_relays(con)
    monitor.load_questions(con)
    return con


def load_settings(con):
    """读取并持久化系统默认设置，确保全新数据库和设置页面字段完整。"""
    con.execute(
        """CREATE TABLE IF NOT EXISTS settings(
               key TEXT PRIMARY KEY,
               value TEXT)"""
    )
    defaults = {
        "schedule_enabled": "1",
        "schedule_time": "09:00",
        "baseline_days": "7",
        "min_history_days": "3",
        "drop_threshold": "8",
        "max_tokens": str(DEFAULT_MAX_OUTPUT_TOKENS),
        "max_input_tokens": str(DEFAULT_MAX_INPUT_TOKENS),
        "api_timeout_minutes": str(DEFAULT_API_TIMEOUT_MINUTES),
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "anthropic_reasoning_effort": DEFAULT_REASONING_EFFORT,
        "concurrency": "5",
        "concurrency_fallback": "1",
        "station_parallel": "10",
        "svg_monitor_enabled": "1",
        "svg_interval_minutes": "30",
        "svg_next_run_at": "",
    }
    # 首次运行时用 config.json 的 settings 初始化缺省值
    rows = dict(con.execute("SELECT key, value FROM settings").fetchall())
    if not rows and monitor.CONFIG_PATH.exists():
        with open(monitor.CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg_raw = json.load(f).get("settings", {})
        for k in ("baseline_days", "min_history_days", "drop_threshold", "max_tokens", "reasoning_effort",
                  "anthropic_reasoning_effort", "api_timeout_minutes", "max_input_tokens"):
            if k in cfg_raw:
                defaults[k] = str(cfg_raw[k])
    rows = monitor.migrate_token_budget_settings(con)
    merged = {**defaults, **rows}
    for key, value in defaults.items():
        if key not in rows:
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (key, value))
    con.commit()
    return {
        "schedule_enabled": merged["schedule_enabled"] == "1",
        "schedule_time": merged["schedule_time"],
        "baseline_days": int(merged["baseline_days"]),
        "min_history_days": int(merged["min_history_days"]),
        "drop_threshold": float(merged["drop_threshold"]),
        "max_tokens": token_budget(merged["max_tokens"], "输出 Token 预算"),
        "max_input_tokens": token_budget(merged["max_input_tokens"], "输入 Token 预算"),
        "api_timeout_minutes": api_timeout_seconds(merged["api_timeout_minutes"]) // 60,
        "reasoning_effort": merged["reasoning_effort"],
        "anthropic_reasoning_effort": merged["anthropic_reasoning_effort"],
        "concurrency": int(merged["concurrency"]),
        "concurrency_fallback": int(merged["concurrency_fallback"]),
        "station_parallel": min(10, max(1, int(merged["station_parallel"]))),
        "svg_monitor_enabled": merged["svg_monitor_enabled"] == "1",
        "svg_interval_minutes": max(1, min(1440, int(merged["svg_interval_minutes"]))),
        "svg_next_run_at": merged["svg_next_run_at"],
        "last_auto_date": merged.get("last_auto_date", ""),
    }


def save_setting(con, key, value):
    """写入或更新单个设置项。"""
    con.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    con.commit()


SVG_STATE = {
    "running": False,
    "started_at": None,
    "trigger": None,
    "current_id": None,
    "total": 0,
    "completed": 0,
    "failed": 0,
}
SVG_LOCK = threading.Lock()
SVG_STATE_LOCK = threading.Lock()
SVG_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=SVG_WORKER_COUNT)


def _svg_state_update(**values):
    """线程安全地更新 SVG 生成状态，供前端实时轮询。"""
    with SVG_STATE_LOCK:
        SVG_STATE.update(values)


def _svg_row_status(con, generation_id, status, **values):
    """更新单条 SVG 记录，并保留每个状态节点的时间。"""
    fields = ["status=?"]
    params = [status]
    for key in ("started_at", "completed_at", "svg_content", "error_detail"):
        if key in values:
            fields.append(f"{key}=?")
            params.append(values[key])
    params.append(generation_id)
    con.execute(
        f"UPDATE svg_generations SET {', '.join(fields)} WHERE id=?", params
    )
    con.commit()


def _svg_failure_log_path(generation_id):
    """日志路径只由数据库记录 ID 生成，下载接口不接受外部文件路径。"""
    return monitor.DATA_DIR / "svg-failures" / f"{int(generation_id)}.log"


def _record_svg_worker_failure(generation_id, relay, exc):
    """取证兜底：配置、线程或数据库异常也保留堆栈，不覆盖已有上游证据。"""
    log_path = _svg_failure_log_path(generation_id)
    if log_path.is_file():
        return "生成任务异常；失败日志已保存，可在生成记录中下载"
    return str(svg_artwork.record_failure(
        svg_artwork.SVGGenerationError("生成任务异常，请查看失败日志"),
        log_path=log_path, cause=exc, secrets=(relay.get("apiKey"),),
        metadata={
            "generation_id": generation_id, "relay": relay.get("name"),
            "relay_label": relay.get("label"), "model": relay.get("model"),
            "protocol": relay.get("protocol", "openai"), "stage": "生成任务执行",
            "response_capture_end": "任务在响应接收流程之外失败，没有上游正文可用",
        },
    ))


def _run_one_svg_generation(generation_id, relay):
    """执行一个渠道的 SVG 生成，单渠道失败不影响其他渠道。"""
    started = datetime.datetime.now().isoformat(timespec="seconds")
    con = get_con()
    _svg_row_status(con, generation_id, "generating", started_at=started, error_detail=None)
    settings = load_settings(con)
    protocol = relay.get("protocol", "openai")
    reasoning_effort = resolve_reasoning_effort(relay, settings[reasoning_setting_key(protocol)])
    api_timeout_minutes = settings["api_timeout_minutes"]
    request_info = con.execute(
        "SELECT trigger, requested_at FROM svg_generations WHERE id=?", (generation_id,),
    ).fetchone()
    con.close()
    _svg_state_update(current_id=generation_id)
    try:
        print(f"[svg] 渠道 {relay['label']} {protocol} 请求推理等级：{reasoning_effort}")
        print(f"[svg] 渠道 {relay['label']} API 请求超时：{api_timeout_minutes} 分钟")
        print(f"[svg] Token 预算：输出 {settings['max_tokens']}，输入 {settings['max_input_tokens']}（完整发送输入）")
        svg = svg_artwork.generate_svg(
            relay, reasoning_effort=reasoning_effort, api_timeout_minutes=api_timeout_minutes,
            max_tokens=settings["max_tokens"], max_input_tokens=settings["max_input_tokens"],
            failure_log_path=_svg_failure_log_path(generation_id),
            failure_log_metadata={
                "generation_id": generation_id, "relay": relay["name"],
                "relay_label": relay["label"], "started_at": started,
                "trigger": request_info[0], "requested_at": request_info[1],
            },
        )
    except svg_artwork.SVGGenerationError as exc:
        message = str(exc)
        con = get_con()
        _svg_row_status(
            con, generation_id, "failed",
            completed_at=datetime.datetime.now().isoformat(timespec="seconds"),
            error_detail=message,
        )
        con.close()
        print(f"[svg] 渠道 {relay['label']} 生成失败：{message}")
        return False
    except Exception as exc:
        # 未知异常不向前端返回原始内容，避免意外泄露请求头、地址或密钥。
        message = _record_svg_worker_failure(generation_id, relay, exc)
        con = get_con()
        _svg_row_status(
            con, generation_id, "failed",
            completed_at=datetime.datetime.now().isoformat(timespec="seconds"),
            error_detail=message,
        )
        con.close()
        print(f"[svg] 渠道 {relay['label']} 发生内部异常：{type(exc).__name__}")
        return False

    con = get_con()
    _svg_row_status(
        con, generation_id, "completed",
        completed_at=datetime.datetime.now().isoformat(timespec="seconds"),
        svg_content=svg,
        error_detail=None,
    )
    con.close()
    print(f"[svg] 渠道 {relay['label']} 生成完成：记录 #{generation_id}")
    return True


def _svg_generation_worker(items):
    """所有请求共用有界线程池，生成期间也可追加手动请求并显示排队状态。"""
    def finish(future, item):
        try:
            succeeded = future.result()
        except Exception as exc:
            succeeded = False
            message = _record_svg_worker_failure(item[0], item[1], exc)
            try:
                con = get_con()
                try:
                    _svg_row_status(
                        con, item[0], "failed",
                        completed_at=datetime.datetime.now().isoformat(timespec="seconds"),
                        error_detail=message,
                    )
                finally:
                    con.close()
            except Exception as db_exc:
                print(f"[svg] 无法保存失败状态：{type(db_exc).__name__}")
            print(f"[svg] 记录 #{item[0]} 生成线程异常：{type(exc).__name__}")
        with SVG_STATE_LOCK:
            SVG_STATE["completed" if succeeded else "failed"] += 1
            SVG_STATE["running"] = SVG_STATE["completed"] + SVG_STATE["failed"] < SVG_STATE["total"]
            if not SVG_STATE["running"]:
                SVG_STATE["current_id"] = None

    for item in items:
        try:
            future = SVG_POOL.submit(_run_one_svg_generation, item[0], item[1])
        except Exception as exc:
            future = concurrent.futures.Future()
            future.set_exception(exc)
        future.add_done_callback(lambda done, current=item: finish(done, current))


def _insert_svg_generation(con, relay, trigger, requested):
    """插入渠道任务；活动任务部分唯一索引负责跨线程和跨进程去重。"""
    try:
        cursor = con.execute(
            """INSERT INTO svg_generations(
                   relay, relay_label, model_id, status, trigger, requested_at)
               VALUES(?,?,?,?,?,?)""",
            (relay["name"], relay["label"], relay["model"], "queued", trigger, requested),
        )
    except sqlite3.IntegrityError:
        # 只将同渠道活动任务冲突视为已存在；其他完整性错误仍交由调用方处理。
        active = con.execute(
            """SELECT id FROM svg_generations
               WHERE relay=? AND status IN ('queued','generating') LIMIT 1""",
            (relay["name"],),
        ).fetchone()
        if active:
            return None
        raise
    return cursor.lastrowid


def start_svg_generation(trigger="manual", relay_names=None, model_overrides=None):
    """创建一轮 SVG 生成任务；手动与定时入口共享同一门禁和状态机。"""
    with SVG_LOCK:
        con = get_con()
        configured_relays = monitor.load_relays(con)
        enabled_relays = [r for r in configured_relays if r.get("enabled", 1)]
        if relay_names:
            # 明确点选渠道属于手动请求，即使该渠道停用了自动监控也允许立即生成。
            selected_map = {r["name"]: r for r in configured_relays}
            missing = [name for name in relay_names if name not in selected_map]
            if missing:
                con.close()
                return {"ok": False, "code": "missing", "msg": f"渠道不存在：{', '.join(missing)}"}
            selected = [dict(selected_map[name]) for name in dict.fromkeys(relay_names)]
        else:
            selected = [dict(r) for r in enabled_relays]

        # 模型只覆盖本次任务，历史记录保存实际使用的模型，渠道默认配置保持不变。
        for relay in selected:
            override = (model_overrides or {}).get(relay["name"])
            if isinstance(override, str) and override.strip():
                relay["model"] = override.strip()

        if not selected:
            con.close()
            return {"ok": False, "code": "no_relays", "msg": "没有可用的启用渠道，请先在中转站管理中配置"}

        requested = datetime.datetime.now().isoformat(timespec="seconds")
        items = []
        skipped = 0
        for relay in selected:
            generation_id = _insert_svg_generation(con, relay, trigger, requested)
            if generation_id is None:
                skipped += 1
            else:
                items.append((generation_id, relay))
        con.commit()
        con.close()
        if not items:
            return {
                "ok": True,
                "code": "already_running",
                "msg": f"所选渠道已有 {skipped} 个生成任务进行中，未重复提交",
            }
        with SVG_STATE_LOCK:
            if not SVG_STATE["running"]:
                SVG_STATE.update(started_at=requested, trigger=trigger, current_id=None,
                                 total=0, completed=0, failed=0)
            SVG_STATE["running"] = True
            SVG_STATE["total"] += len(items)
        _svg_generation_worker(items)
        print(f"[svg] 开始{('自动' if trigger == 'scheduled' else '手动')}生成，渠道数：{len(items)}")
        message = f"已开始生成 {len(items)} 个渠道的鹈鹕骑车动画"
        if skipped:
            message += f"；另有 {skipped} 个渠道已有任务，未重复提交"
        return {"ok": True, "code": "started", "msg": message}


def list_svg_generations(limit=50):
    """返回 SVG 生成记录、实时状态和下一轮自动生成时间。"""
    limit = max(1, min(int(limit), 100))
    # 高频轮询只查询两张小表，避免每两秒重复加载完整题库。
    con = sqlite3.connect(monitor.DB_PATH, timeout=10)
    rows = con.execute(
        """SELECT id, relay, relay_label, model_id, status, trigger, error_detail,
                  requested_at, started_at, completed_at
           FROM svg_generations ORDER BY id DESC LIMIT ?""", (limit,)
    ).fetchall()
    settings = dict(con.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('svg_monitor_enabled','svg_interval_minutes','svg_next_run_at')"
    ).fetchall())
    con.close()
    items = [{
        "id": row[0], "relay": row[1], "relay_label": row[2] or row[1],
        "model_id": row[3] or "未记录", "status": row[4], "trigger": row[5],
        "error_detail": row[6], "requested_at": row[7], "started_at": row[8],
        "completed_at": row[9],
        "failure_log_available": row[4] == "failed" and _svg_failure_log_path(row[0]).is_file(),
    } for row in rows]
    with SVG_STATE_LOCK:
        state = dict(SVG_STATE)
    state["next_run_at"] = settings.get("svg_next_run_at", "")
    state["enabled"] = settings.get("svg_monitor_enabled", "1") == "1"
    state["interval_minutes"] = max(1, min(1440, int(settings.get("svg_interval_minutes", "30"))))
    return {"items": items, "state": state}


def recover_svg_generations():
    """服务重启时将中断任务标成失败，并恢复尚未开始的排队任务。"""
    con = get_con()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    con.execute(
        """UPDATE svg_generations
           SET status='failed', error_detail='服务重启时生成中断', completed_at=?
           WHERE status='generating'""", (now,)
    )
    con.commit()
    relays = {r["name"]: r for r in monitor.load_relays(con)}
    queued = con.execute(
        "SELECT id, relay, relay_label, model_id FROM svg_generations WHERE status='queued' ORDER BY id"
    ).fetchall()
    items = []
    for generation_id, relay_name, relay_label, model_id in queued:
        relay = relays.get(relay_name)
        if relay:
            items.append((generation_id, {**relay, "label": relay_label, "model": model_id}))
        else:
            _svg_row_status(
                con, generation_id, "failed", completed_at=now,
                error_detail="渠道已删除，无法恢复排队任务",
            )
    con.close()
    if items:
        _svg_state_update(
            running=True, started_at=now, trigger="恢复排队任务", current_id=None,
            total=len(items), completed=0, failed=0,
        )
        threading.Thread(target=_svg_generation_worker, args=(items,), daemon=True).start()


def mask_key(key):
    """API Key 脱敏展示：保留前 6 位与后 4 位。"""
    if not key or len(key) <= 12:
        return key
    return f"{key[:6]}****{key[-4:]}"


def evaluate_day(con, relays, date_str, settings):
    """对指定日期计算各站聚合状态行（一天多次评测取平均，含每次明细）。"""
    rows = []
    for relay in relays:
        agg = monitor.aggregate_day(con, relay["name"], date_str)
        if agg is None:
            # 无评测数据的中转站也显示，标注「无数据」
            rows.append({
                "name": relay["name"], "label": relay.get("label", relay["name"]),
                "score": None, "passed": 0, "total": 0, "latency": 0,
                "status": "无数据", "baseline": None, "diff": None,
                "event": "今日未评测", "error_detail": None,
                "run_count": 0, "runs": [], "model_ids": [relay.get("model", "未记录")] or [],
            })
            continue
        score = agg["score"]
        status, baseline, _ = monitor.day_status(con, relay["name"], date_str, settings)
        yesterday = (datetime.date.fromisoformat(date_str)
                     - datetime.timedelta(days=1)).isoformat()
        y_status, _, _ = monitor.day_status(con, relay["name"], yesterday, settings)

        if status == "评测异常":
            event = f"评测异常：{agg['error_detail'] or '全部题目出错'}"
        elif status == "观察期":
            event = "基线积累中"
        elif status == "疑似掺水" and y_status != "疑似掺水":
            event = "新增掺水嫌疑"
        elif status == "正常" and y_status == "疑似掺水":
            event = "已恢复正常"
        elif status == "疑似掺水":
            event = "持续掺水中"
        else:
            event = "稳定"

        # 附加标注：评测次数与错误说明
        notes = []
        if agg["count"] > 1:
            notes.append(f"今日{agg['count']}次评测取平均")
        if agg["error_detail"] and status != "评测异常":
            notes.append(f"{agg['error_detail']}（错误题不计分）")
        if notes:
            event += "（" + "，".join(notes) + "）"

        rows.append({
            "name": relay["name"],
            "label": agg["runs"][-1]["relay_label"] if agg["runs"] else relay["label"],
            "score": score, "passed": agg["passed"], "total": agg["total"],
            "latency": agg["latency"], "status": status, "baseline": baseline,
            "diff": round(score - baseline, 1)
                    if (score is not None and baseline is not None) else None,
            "event": event, "error_detail": agg["error_detail"],
            "run_count": agg["count"], "runs": agg["runs"],
            "model_ids": list(dict.fromkeys(
                run["model_id"] for run in agg["runs"] if run["model_id"]
            )),
        })
    return rows


def worker(log=print, relays=None):
    """后台评测线程体：持有锁运行 run_all，并记录日志到 RUN_STATE。"""
    with RUN_LOCK:
        if RUN_STATE["running"]:
            return False
        RUN_STATE.update(running=True, started_at=time.time(), log=[], paused=False)
    CONTROL.update(stop=False, paused=False, procs=[])
    try:
        def hook(msg):
            """评测日志双写：控制台 + 前端可轮询的列表。"""
            log(msg)
            RUN_STATE["log"].append(f"[{datetime.datetime.now():%H:%M:%S}] {msg}")

        con = get_con()
        if relays is None:
            relays = [r for r in monitor.load_relays(con) if r.get("enabled", 1)]
        settings = load_settings(con)
        con.close()
        monitor.run_all(relays=relays, settings=settings, log=hook,
                        control=CONTROL)
        return True
    finally:
        RUN_STATE["running"] = False
        RUN_STATE["paused"] = False
        # 确保暂停的进程被恢复后再结束（防止残留挂起进程）
        if CONTROL.get("procs"):
            monitor.suspend_process(CONTROL, suspend=False)
        CONTROL["procs"] = []


def scheduler_loop():
    """统一调度每日评测与 SVG 动画监控，每 30 秒检查一次。"""
    while True:
        try:
            con = get_con()
            s = load_settings(con)
            now = datetime.datetime.now()
            if s["schedule_enabled"]:
                if (now.strftime("%H:%M") >= s["schedule_time"]
                        and s["last_auto_date"] != now.date().isoformat()):
                    save_setting(con, "last_auto_date", now.date().isoformat())
                    con.close()
                    worker(print)
                    continue
            # SVG 监控按间隔运行；第一次启动先安排到 30 分钟后，避免服务启动即产生外部请求。
            if s["svg_monitor_enabled"]:
                next_run = None
                if s["svg_next_run_at"]:
                    try:
                        next_run = datetime.datetime.fromisoformat(s["svg_next_run_at"])
                    except ValueError:
                        next_run = None
                if next_run is None:
                    next_run = now + datetime.timedelta(minutes=s["svg_interval_minutes"])
                    save_setting(con, "svg_next_run_at", next_run.isoformat(timespec="seconds"))
                elif now >= next_run:
                    result = start_svg_generation(trigger="scheduled")
                    next_run = now + datetime.timedelta(minutes=s["svg_interval_minutes"])
                    save_setting(con, "svg_next_run_at", next_run.isoformat(timespec="seconds"))
                    if not result.get("ok") and result.get("code") != "running":
                        print(f"[svg] 自动监控未创建任务：{result['msg']}")
            con.close()
        except Exception as e:  # 定时器守护线程不允许退出
            print(f"[scheduler] {e}")
        time.sleep(30)


# ---------------- 数据模型 ----------------

class RelayIn(BaseModel):
    """中转站新增/编辑请求体。"""
    name: str
    label: str
    protocol: Literal["openai", "openai-responses", "anthropic"] = "openai"
    baseUrl: str
    apiKey: str
    model: str
    enabled: bool = True
    reasoning_effort: ReasoningEffort | None = None


def _models_endpoint(base_url: str, protocol: str = "openai") -> str:
    """根据渠道 Base URL 组装 OpenAI/Anthropic 兼容的模型列表地址。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "Base URL 不能为空")
    if protocol == "anthropic" and not base.lower().endswith("/v1"):
        base = f"{base}/v1"
    return base if base.lower().endswith("/models") else f"{base}/models"


def fetch_relay_models(relay: dict) -> list[str]:
    """从上游拉取模型 ID；只返回可用于下拉选择的稳定字符串。"""
    protocol = relay.get("protocol", "openai")
    headers = {"Accept": "application/json"}
    api_key = str(relay.get("apiKey") or "").strip()
    if protocol == "anthropic":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        _models_endpoint(relay.get("baseUrl", ""), protocol), headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", errors="replace")
        print(f"[模型] 渠道 {relay.get('label', relay.get('name'))} 返回 HTTP {exc.code}")
        raise HTTPException(502, f"上游模型列表请求失败（HTTP {exc.code}）：{detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"[模型] 渠道 {relay.get('label', relay.get('name'))} 拉取失败：{type(exc).__name__}")
        raise HTTPException(502, f"上游模型列表请求失败：{exc}") from exc

    raw_items = payload.get("data") if isinstance(payload, dict) else None
    if raw_items is None and isinstance(payload, dict):
        raw_items = payload.get("models")
    if not isinstance(raw_items, list):
        raise HTTPException(502, "上游返回格式不包含 data/models 模型列表")
    models = []
    for item in raw_items:
        value = item.get("id") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip() and value.strip() not in models:
            models.append(value.strip())
    if not models:
        raise HTTPException(502, "上游返回的模型列表为空")
    print(f"[模型] 渠道 {relay.get('label', relay.get('name'))} 已拉取 {len(models)} 个模型")
    return models


class SettingsIn(BaseModel):
    """系统设置更新请求体。"""
    schedule_enabled: bool = True
    schedule_time: str = "09:00"
    baseline_days: int = 7
    min_history_days: int = 3
    drop_threshold: float = 8
    max_tokens: int | None = Field(default=None, ge=1, strict=True)
    max_input_tokens: int | None = Field(default=None, ge=1, strict=True)
    api_timeout_minutes: int | None = Field(
        default=None, ge=1, le=MAX_API_TIMEOUT_MINUTES, strict=True,
    )
    reasoning_effort: OpenAIReasoningEffort | None = None
    anthropic_reasoning_effort: AnthropicReasoningEffort | None = None
    concurrency: int = 5
    concurrency_fallback: int = 1
    station_parallel: int = 10
    svg_monitor_enabled: bool | None = None
    svg_interval_minutes: int | None = None


class QuestionIn(BaseModel):
    """题库条目请求体。"""
    q: str
    answer: str


class ImportIn(BaseModel):
    """开源数据集导入请求体。"""
    source: str          # gsm8k / livebench-math
    count: int = 30      # 抽样题目数
    replace: bool = True # True=清空现有题库后导入


# ---------------- 中转站管理 API ----------------

def validate_relay_reasoning(body):
    """在写库之前校验协议与推理等级组合，继承默认值用空值表示。"""
    if body.reasoning_effort is not None:
        try:
            reasoning_params(body.protocol, body.reasoning_effort)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

@app.get("/api/relays")
def list_relays():
    """中转站列表（Key 脱敏返回）。"""
    con = get_con()
    relays = monitor.load_relays(con)
    settings = load_settings(con)
    con.close()
    return [{**r, "apiKey": mask_key(r["apiKey"]),
             "enabled": bool(r.get("enabled", 1)),
             "effective_reasoning_effort": resolve_reasoning_effort(
                 r, settings[reasoning_setting_key(r.get("protocol", "openai"))]
             )} for r in relays]


@app.get("/api/relays/{name}/models")
def list_relay_models(name: str):
    """按需从指定渠道拉取上游模型，前端只保存当前会话缓存。"""
    con = get_con()
    relay = next((r for r in monitor.load_relays(con) if r["name"] == name), None)
    con.close()
    if not relay:
        raise HTTPException(404, "中转站不存在")
    models = fetch_relay_models(relay)
    return {"ok": True, "relay": name, "models": models, "current_model": relay.get("model", "")}


@app.post("/api/relays")
def add_relay(body: RelayIn):
    """新增中转站。name 已存在时报 409。"""
    validate_relay_reasoning(body)
    con = get_con()
    if con.execute("SELECT 1 FROM relays WHERE name=?", (body.name,)).fetchone():
        con.close()
        raise HTTPException(409, f"标识 {body.name} 已存在")
    con.execute(
        "INSERT INTO relays(name, label, protocol, baseUrl, apiKey, model, enabled, reasoning_effort) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (body.name.strip(), body.label.strip(), body.protocol,
         body.baseUrl.strip(), body.apiKey.strip(), body.model.strip(),
         1 if body.enabled else 0, body.reasoning_effort),
    )
    con.commit()
    con.close()
    print(f"[渠道] 已新增 {body.name}，{body.protocol} 推理等级：{body.reasoning_effort or '继承系统默认'}")
    return {"ok": True}


@app.put("/api/relays/{name}")
def update_relay(name: str, body: RelayIn):
    """编辑中转站。apiKey 传空字符串表示保持原 Key 不变。"""
    validate_relay_reasoning(body)
    con = get_con()
    old = con.execute("SELECT apiKey, protocol, reasoning_effort FROM relays WHERE name=?", (name,)).fetchone()
    if not old:
        con.close()
        raise HTTPException(404, "中转站不存在")
    api_key = body.apiKey.strip()
    if not api_key:  # 空 Key = 沿用旧值
        api_key = old[0]
    fields = getattr(body, "model_fields_set", None) or getattr(body, "__fields_set__", set())
    effort = body.reasoning_effort
    if "reasoning_effort" not in fields and body.protocol == old[1]:
        effort = old[2]
    con.execute(
        "UPDATE relays SET name=?, label=?, protocol=?, baseUrl=?, apiKey=?, "
        "model=?, enabled=?, reasoning_effort=? WHERE name=?",
        (body.name.strip(), body.label.strip(), body.protocol,
         body.baseUrl.strip(), api_key, body.model.strip(),
         1 if body.enabled else 0, effort, name),
    )
    con.commit()
    con.close()
    print(f"[渠道] 已更新 {body.name}，{body.protocol} 推理等级：{effort or '继承系统默认'}")
    return {"ok": True}


@app.delete("/api/relays/{name}")
def delete_relay(name: str):
    """删除中转站（历史运行数据保留，可通过 name 关联查询）。"""
    con = get_con()
    if not con.execute("SELECT 1 FROM relays WHERE name=?", (name,)).fetchone():
        con.close()
        raise HTTPException(404, "中转站不存在")
    con.execute("DELETE FROM relays WHERE name=?", (name,))
    con.commit()
    con.close()
    return {"ok": True}


# ---------------- 题库 API ----------------

@app.get("/api/questions")
def list_questions():
    """返回「活动版本」的题目列表。"""
    con = get_con()
    monitor.ensure_question_versions(con)
    vid = monitor.active_question_version(con)
    items = []
    if vid is not None:
        items = [{"id": i, "q": q, "answer": a, "qtype": qt or "math",
                  "entry_point": ep}
                 for i, q, a, qt, ep in con.execute(
            "SELECT id, q, answer, qtype, entry_point FROM questions "
            "WHERE version_id=? ORDER BY id", (vid,))]
    con.close()
    return items


@app.post("/api/questions")
def add_question(body: QuestionIn):
    """手动新增题目（加入当前活动版本）。"""
    con = get_con()
    monitor.ensure_question_versions(con)
    vid = monitor.active_question_version(con)
    if vid is None:
        con.close()
        raise HTTPException(400, "没有可用版本，请先导入数据集")
    cur = con.execute(
        "INSERT INTO questions(q, answer, version_id) VALUES(?,?,?)",
        (body.q.strip(), body.answer.strip(), vid))
    con.commit()
    qid = cur.lastrowid
    con.close()
    return {"ok": True, "id": qid}


@app.delete("/api/questions/{qid}")
def delete_question(qid: int):
    """删除题目。"""
    con = get_con()
    con.execute("DELETE FROM questions WHERE id=?", (qid,))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/datasets")
def dataset_sources():
    """返回可选的开源数据源列表。"""
    return datasets.list_sources()


@app.get("/api/questions/versions")
def list_question_versions():
    """题库版本列表（含题数与是否活动版本，倒序）。"""
    con = get_con()
    monitor.ensure_question_versions(con)
    active = monitor.active_question_version(con)
    rows = con.execute(
        """SELECT v.id, v.label, v.source, v.created_at, COUNT(q.id) AS count
           FROM question_versions v
           LEFT JOIN questions q ON q.version_id = v.id
           GROUP BY v.id ORDER BY v.id DESC"""
    ).fetchall()
    con.close()
    return [{"id": r[0], "label": r[1], "source": r[2], "created_at": r[3],
             "count": r[4], "active": r[0] == active} for r in rows]


@app.post("/api/questions/versions/{vid}/activate")
def activate_question_version(vid: int):
    """恢复（切换活动）题库版本为指定历史版本。"""
    con = get_con()
    monitor.ensure_question_versions(con)
    if not con.execute("SELECT 1 FROM question_versions WHERE id=?", (vid,)).fetchone():
        con.close()
        raise HTTPException(404, "版本不存在")
    if con.execute("SELECT COUNT(*) FROM questions WHERE version_id=?", (vid,)).fetchone()[0] == 0:
        con.close()
        raise HTTPException(400, "该版本下没有题目")
    save_setting(con, "active_question_version", vid)
    con.close()
    return {"ok": True, "active_version": vid}


@app.delete("/api/questions/versions/{vid}")
def delete_question_version(vid: int):
    """删除非活动的题库版本及其题目。"""
    con = get_con()
    monitor.ensure_question_versions(con)
    active = monitor.active_question_version(con)
    if vid == active:
        con.close()
        raise HTTPException(400, "不能删除当前活动版本")
    if not con.execute("SELECT 1 FROM question_versions WHERE id=?", (vid,)).fetchone():
        con.close()
        raise HTTPException(404, "版本不存在")
    con.execute("DELETE FROM questions WHERE version_id=?", (vid,))
    con.execute("DELETE FROM question_versions WHERE id=?", (vid,))
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/questions/import")
def import_questions(body: ImportIn):
    """从开源数据集抽样，创建为「新版本」并设为活动（旧版本保留可恢复）。"""
    if body.count < 1 or body.count > 200:
        raise HTTPException(400, "抽样数量需在 1-200 之间")
    try:
        items = datasets.import_dataset(body.source, body.count)
    except Exception as e:
        raise HTTPException(502, f"数据集下载失败: {e}")
    if not items:
        raise HTTPException(502, "数据集中没有符合条件的题目")
    con = get_con()
    monitor.ensure_question_versions(con)
    now = datetime.datetime.now()
    label = f"{body.source} {len(items)}题 {now:%m-%d %H:%M}"
    cur = con.execute(
        "INSERT INTO question_versions(label, source, created_at) VALUES(?,?,?)",
        (label, body.source, now.isoformat(timespec="seconds")),
    )
    vid = cur.lastrowid
    for it in items:
        # 编程题额外存储判分 payload（test）与入口函数名
        con.execute(
            "INSERT INTO questions(q, answer, version_id, qtype, test, entry_point) "
            "VALUES(?,?,?,?,?,?)",
            (it["q"], it.get("answer", ""), vid, it.get("qtype", "math"),
             it.get("test"), it.get("entry_point")))
    save_setting(con, "active_question_version", vid)
    if not body.replace:
        # 兼容旧参数：追加模式同样落在新版本下（版本模型中无追加语义，等同替换）
        pass
    con.commit()
    con.close()
    return {"ok": True, "imported": len(items), "version_id": vid,
            "version_label": label, "source": body.source}


# ---------------- 设置 / 运行 / 数据 API ----------------

@app.get("/api/settings")
def read_settings():
    """读取系统设置。"""
    con = get_con()
    s = load_settings(con)
    con.close()
    return s


@app.put("/api/settings")
def update_settings(body: SettingsIn):
    """更新系统设置（含定时时间）。"""
    # 校验时间格式 HH:MM
    try:
        datetime.datetime.strptime(body.schedule_time, "%H:%M")
    except ValueError:
        raise HTTPException(400, "定时时间格式应为 HH:MM")
    con = get_con()
    save_setting(con, "schedule_enabled", 1 if body.schedule_enabled else 0)
    save_setting(con, "schedule_time", body.schedule_time)
    save_setting(con, "baseline_days", body.baseline_days)
    save_setting(con, "min_history_days", body.min_history_days)
    save_setting(con, "drop_threshold", body.drop_threshold)
    if body.max_tokens is not None:
        save_setting(con, "max_tokens", body.max_tokens)
        print(f"[设置] 全局最大输出 Token 已更新：{body.max_tokens}")
    if body.max_input_tokens is not None:
        save_setting(con, "max_input_tokens", body.max_input_tokens)
        print(f"[设置] 全局输入 Token 预算已更新：{body.max_input_tokens}（输入完整发送，不本地截断）")
    if body.api_timeout_minutes is not None:
        save_setting(con, "api_timeout_minutes", body.api_timeout_minutes)
        print(f"[设置] 全局 API 请求超时已更新：{body.api_timeout_minutes} 分钟（新请求生效）")
    if body.reasoning_effort is not None:
        save_setting(con, "reasoning_effort", body.reasoning_effort)
        print(f"[设置] OpenAI 默认推理等级已更新：{body.reasoning_effort}")
    if body.anthropic_reasoning_effort is not None:
        save_setting(con, "anthropic_reasoning_effort", body.anthropic_reasoning_effort)
        print(f"[设置] Anthropic 默认推理等级已更新：{body.anthropic_reasoning_effort}")
    save_setting(con, "concurrency", max(1, min(body.concurrency, 20)))
    save_setting(con, "concurrency_fallback", 1)
    save_setting(con, "station_parallel", max(1, min(body.station_parallel, 10)))
    if body.svg_monitor_enabled is not None:
        save_setting(con, "svg_monitor_enabled", 1 if body.svg_monitor_enabled else 0)
    if body.svg_interval_minutes is not None:
        save_setting(con, "svg_interval_minutes", max(1, min(body.svg_interval_minutes, 1440)))
        # 修改周期后从当前时间重新计算下一次自动生成，避免沿用旧周期。
        next_run = datetime.datetime.now() + datetime.timedelta(
            minutes=max(1, min(body.svg_interval_minutes, 1440))
        )
        save_setting(con, "svg_next_run_at", next_run.isoformat(timespec="seconds"))
    # 修改时间后允许当天重新触发（清空防重复标记）
    save_setting(con, "last_auto_date", "")
    con.close()
    return {"ok": True}


class RunIn(BaseModel):
    """触发评测请求体：允许本次任务按渠道临时指定模型。"""
    relays: list[str] | None = None
    models: dict[str, str] | None = None


class SVGGenerateIn(BaseModel):
    """SVG 动画生成请求；缺省时为全部启用渠道。"""
    relays: list[str] | None = None
    models: dict[str, str] | None = None


@app.get("/api/svg-generations")
def svg_generations(limit: int = 50):
    """返回 SVG 动画生成表格数据和当前轮询状态。"""
    return list_svg_generations(limit)


@app.post("/api/svg-generations/generate")
def generate_svg_animation(body: SVGGenerateIn | None = None):
    """手动生成鹈鹕骑车动画，可在非自动到期时间立即执行。"""
    relay_names = body.relays if body and body.relays else None
    return start_svg_generation(trigger="manual", relay_names=relay_names,
                                model_overrides=body.models if body else None)


@app.get("/api/svg-generations/{generation_id}/preview")
def svg_generation_preview(generation_id: int):
    """返回已完成 SVG；iframe 使用 sandbox，避免模型内容影响管理页面。"""
    con = get_con()
    row = con.execute(
        "SELECT status, svg_content FROM svg_generations WHERE id=?", (generation_id,)
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "生成记录不存在")
    if row[0] != "completed" or not row[1]:
        raise HTTPException(409, "该记录尚未生成完成")
    return Response(
        content=row[1],
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:;",
        },
    )


@app.get("/api/svg-generations/{generation_id}/failure-log")
def svg_generation_failure_log(generation_id: int):
    """下载本地失败证据；旧记录没有原始响应时不生成伪造日志。"""
    con = sqlite3.connect(monitor.DB_PATH, timeout=10)
    try:
        row = con.execute(
            "SELECT status FROM svg_generations WHERE id=?", (generation_id,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, "生成记录不存在")
    if row[0] != "failed":
        raise HTTPException(409, "该记录不是失败状态")
    path = _svg_failure_log_path(generation_id)
    if not path.is_file():
        raise HTTPException(404, "该记录没有保留失败日志，历史响应无法补回")
    return FileResponse(
        path, media_type="text/plain; charset=utf-8",
        filename=f"svg-generation-{generation_id}-failure.log",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/run")
def trigger_run(body: RunIn = None):
    """手动触发评测（可指定站点子集；后台执行，前端轮询进度）。"""
    if RUN_STATE["running"]:
        return {"ok": False, "msg": "评测正在进行中"}

    selected = None
    if body and body.relays:
        # 校验指定的站点都存在
        con = get_con()
        all_relays = {r["name"]: r for r in monitor.load_relays(con)}
        con.close()
        missing = [n for n in body.relays if n not in all_relays]
        if missing:
            return {"ok": False, "msg": f"站点不存在: {', '.join(missing)}"}
        selected = [dict(all_relays[n]) for n in body.relays]
        if not selected:
            return {"ok": False, "msg": "没有选中的站点"}

        # 单次评测允许选择刚从上游拉取的模型，不修改渠道默认模型。
        for relay in selected:
            override = (body.models or {}).get(relay["name"])
            if isinstance(override, str) and override.strip():
                relay["model"] = override.strip()

    def starter():
        """线程包装：捕获异常避免线程静默死亡。"""
        try:
            worker(relays=selected)
        except Exception as e:
            RUN_STATE["log"].append(f"[{datetime.datetime.now():%H:%M:%S}] 评测线程异常: {e}")
            RUN_STATE["running"] = False

    threading.Thread(target=starter, daemon=True).start()
    names = [r["label"] for r in selected] if selected else "全部启用站点"
    return {"ok": True, "msg": f"已开始评测：{names}"}


@app.post("/api/run/pause")
def pause_run():
    """暂停评测：优先挂起当前 promptfoo 子进程（真暂停），否则站点边界等待。"""
    if not RUN_STATE["running"]:
        return {"ok": False, "msg": "没有进行中的评测"}
    if RUN_STATE["paused"]:
        return {"ok": False, "msg": "已处于暂停状态"}
    RUN_STATE["paused"] = True
    CONTROL["paused"] = True
    hard = monitor.suspend_process(CONTROL, suspend=True)
    mode = f"已挂起 {hard} 个评测进程" if hard else "将在未开始的站点前等待"
    RUN_STATE["log"].append(f"[{datetime.datetime.now():%H:%M:%S}] [暂停] {mode}")
    return {"ok": True, "msg": f"暂停请求已生效（{mode}）"}


@app.post("/api/run/resume")
def resume_run():
    """继续评测：恢复被挂起的子进程并清除暂停标记。"""
    if not RUN_STATE["running"]:
        return {"ok": False, "msg": "没有进行中的评测"}
    if not RUN_STATE["paused"]:
        return {"ok": False, "msg": "当前不在暂停状态"}
    monitor.suspend_process(CONTROL, suspend=False)
    RUN_STATE["paused"] = False
    CONTROL["paused"] = False
    RUN_STATE["log"].append(f"[{datetime.datetime.now():%H:%M:%S}] [继续] 评测恢复运行")
    return {"ok": True, "msg": "已继续评测"}


@app.post("/api/run/stop")
def stop_run():
    """强制停止评测：杀掉全部并行子进程并跳过剩余站点。"""
    if not RUN_STATE["running"]:
        return {"ok": False, "msg": "没有进行中的评测"}
    CONTROL["stop"] = True
    # 若处于挂起状态先恢复再杀，避免杀不掉挂起进程
    if RUN_STATE["paused"]:
        monitor.suspend_process(CONTROL, suspend=False)
        RUN_STATE["paused"] = False
        CONTROL["paused"] = False
    killed = monitor.kill_all_processes(CONTROL)
    RUN_STATE["log"].append(f"[{datetime.datetime.now():%H:%M:%S}] [强制停止] "
                            f"已终止评测（{killed} 个进程）")
    return {"ok": True, "msg": "已请求强制停止"}


@app.get("/api/run/status")
def run_status():
    """查询评测运行状态与日志（含暂停标记）。"""
    return {**RUN_STATE, "can_pause": RUN_STATE["running"]}


@app.get("/api/days")
def list_days():
    """有历史数据的日期列表（倒序，供前端日期选择）。"""
    con = get_con()
    days = [d for (d,) in con.execute(
        "SELECT DISTINCT date FROM runs ORDER BY date DESC")]
    con.close()
    return days


@app.get("/api/day/{date_str}")
def day_detail(date_str: str):
    """某日各站详情 + 近 N 天走势 + 当日推荐。"""
    con = get_con()
    relays = monitor.load_relays(con)
    known = {relay["name"] for relay in relays}
    historical = con.execute(
        "SELECT DISTINCT relay FROM runs ORDER BY relay"
    ).fetchall()
    for (name,) in historical:
        if name in known:
            continue
        # 已删除的站点：仅当该日期有真实评测数据时才显示（保留历史可见性），
        # 没有数据的日期不显示，避免已删站点以「无数据」混入当日表格
        if monitor.aggregate_day(con, name, date_str) is None:
            continue
        snapshot = con.execute(
            "SELECT relay_label, model_id FROM runs WHERE relay=? "
            "ORDER BY id DESC LIMIT 1", (name,),
        ).fetchone()
        relays.append({
            "name": name,
            "label": (snapshot[0] if snapshot and snapshot[0] else name),
            "model": (snapshot[1] if snapshot and snapshot[1] else "未记录"),
            "enabled": 0,
        })
    settings = load_settings(con)
    rows = evaluate_day(con, relays, date_str, settings)
    days = [d for (d,) in con.execute(
        "SELECT DISTINCT date FROM runs ORDER BY date DESC LIMIT 30")]

    # 走势：每站全部历史 (date, 日均分) 序列（一天多次评测聚合为日均分）
    trends = {}
    for r in relays:
        raw = con.execute(
            "SELECT date, score, total FROM runs "
            "WHERE relay=? AND score IS NOT NULL ORDER BY date",
            (r["name"],),
        ).fetchall()
        if raw:
            by_date = {}
            for d, s, t in raw:
                by_date.setdefault(d, []).append((s, t or 0))
            pts = []
            for d in sorted(by_date):
                items = by_date[d]
                w = sum(t for _, t in items)
                pts.append((d, round(sum(s * t for s, t in items) / w, 1) if w > 0
                            else round(sum(s for s, _ in items) / len(items), 1)))
            trends[r["name"]] = {"label": r["label"], "points": pts}

    # 当日推荐（复用 monitor.pick_best 的行结构；可能返回 None=全部异常）
    best, note = None, ""
    if rows:
        fake_rows = [{**r} for r in rows]
        best, note = monitor.pick_best(fake_rows)
        if best is not None:
            best = {"label": best["label"], "name": best["name"],
                    "score": best["score"], "status": best["status"]}
    con.close()
    return {"date": date_str, "rows": rows, "trends": trends,
            "best": best, "note": note, "days": days}


@app.get("/api/report/{date_str}")
def get_report(date_str: str):
    """返回某日 HTML 日报文件。"""
    path = monitor.REPORT_DIR / f"{date_str}.html"
    if not path.exists():
        raise HTTPException(404, "该日期没有日报")
    return FileResponse(path, media_type="text/html")


# ---------------- 静态页面 ----------------

@app.get("/")
def index():
    """返回管理界面首页。"""
    if not INDEX_PATH.exists():
        return JSONResponse({"error": "static/index.html 不存在"}, status_code=500)
    return FileResponse(INDEX_PATH)


# ---------------- 启动 ----------------

@app.on_event("startup")
def on_startup():
    """服务启动时恢复 SVG 状态并拉起统一定时器线程。"""
    con = get_con()
    settings = load_settings(con)
    if settings["svg_monitor_enabled"] and not settings["svg_next_run_at"]:
        next_run = datetime.datetime.now() + datetime.timedelta(
            minutes=settings["svg_interval_minutes"]
        )
        save_setting(con, "svg_next_run_at", next_run.isoformat(timespec="seconds"))
    con.close()
    recover_svg_generations()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("内置定时器已启动（每 30 秒检查评测和 SVG 动画到期状态）")


if __name__ == "__main__":
    print("中转站质量监控系统: http://127.0.0.1:8787")
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
