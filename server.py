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
import json
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import datasets
import monitor
from app_paths import RESOURCE_DIR

BASE_DIR = RESOURCE_DIR
INDEX_PATH = BASE_DIR / "static" / "index.html"

app = FastAPI(title="中转站质量监控系统")

# 全局运行状态（防止并发评测 + 前端轮询进度）
RUN_STATE = {"running": False, "started_at": None, "log": [], "paused": False}
# 评测控制字典（与 monitor 共享：stop/paused/proc）
CONTROL = monitor.new_control()
RUN_LOCK = threading.Lock()


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
        "max_tokens": "512",
        "concurrency": "5",
        "concurrency_fallback": "1",
        "station_parallel": "10",
    }
    # 首次运行时用 config.json 的 settings 初始化缺省值
    rows = dict(con.execute("SELECT key, value FROM settings").fetchall())
    if not rows and monitor.CONFIG_PATH.exists():
        with open(monitor.CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg_raw = json.load(f).get("settings", {})
        for k in ("baseline_days", "min_history_days", "drop_threshold", "max_tokens"):
            if k in cfg_raw:
                defaults[k] = str(cfg_raw[k])
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
        "max_tokens": int(merged["max_tokens"]),
        "concurrency": int(merged["concurrency"]),
        "concurrency_fallback": int(merged["concurrency_fallback"]),
        "station_parallel": min(10, max(1, int(merged["station_parallel"]))),
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
    """内置定时器：每 30 秒检查一次是否到达每日评测时刻。

    到达设置时间且当日未自动运行过时触发评测（记录 last_auto_date 防重复）。
    """
    while True:
        try:
            con = get_con()
            s = load_settings(con)
            if s["schedule_enabled"]:
                now = datetime.datetime.now()
                if (now.strftime("%H:%M") >= s["schedule_time"]
                        and s["last_auto_date"] != now.date().isoformat()):
                    save_setting(con, "last_auto_date", now.date().isoformat())
                    con.close()
                    worker(print)
                    continue
            con.close()
        except Exception as e:  # 定时器守护线程不允许退出
            print(f"[scheduler] {e}")
        time.sleep(30)


# ---------------- 数据模型 ----------------

class RelayIn(BaseModel):
    """中转站新增/编辑请求体。"""
    name: str
    label: str
    protocol: str = "openai"
    baseUrl: str
    apiKey: str
    model: str
    enabled: bool = True


class SettingsIn(BaseModel):
    """系统设置更新请求体。"""
    schedule_enabled: bool = True
    schedule_time: str = "09:00"
    baseline_days: int = 7
    min_history_days: int = 3
    drop_threshold: float = 8
    max_tokens: int = 512
    concurrency: int = 5
    concurrency_fallback: int = 1
    station_parallel: int = 10


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

@app.get("/api/relays")
def list_relays():
    """中转站列表（Key 脱敏返回）。"""
    con = get_con()
    relays = monitor.load_relays(con)
    con.close()
    return [{**r, "apiKey": mask_key(r["apiKey"]),
             "enabled": bool(r.get("enabled", 1))} for r in relays]


@app.post("/api/relays")
def add_relay(body: RelayIn):
    """新增中转站。name 已存在时报 409。"""
    con = get_con()
    if con.execute("SELECT 1 FROM relays WHERE name=?", (body.name,)).fetchone():
        con.close()
        raise HTTPException(409, f"标识 {body.name} 已存在")
    con.execute(
        "INSERT INTO relays(name, label, protocol, baseUrl, apiKey, model, enabled) "
        "VALUES(?,?,?,?,?,?,?)",
        (body.name.strip(), body.label.strip(), body.protocol,
         body.baseUrl.strip(), body.apiKey.strip(), body.model.strip(),
         1 if body.enabled else 0),
    )
    con.commit()
    con.close()
    return {"ok": True}


@app.put("/api/relays/{name}")
def update_relay(name: str, body: RelayIn):
    """编辑中转站。apiKey 传空字符串表示保持原 Key 不变。"""
    con = get_con()
    if not con.execute("SELECT 1 FROM relays WHERE name=?", (name,)).fetchone():
        con.close()
        raise HTTPException(404, "中转站不存在")
    api_key = body.apiKey.strip()
    if not api_key:  # 空 Key = 沿用旧值
        old = con.execute("SELECT apiKey FROM relays WHERE name=?", (name,)).fetchone()
        api_key = old[0]
    con.execute(
        "UPDATE relays SET name=?, label=?, protocol=?, baseUrl=?, apiKey=?, "
        "model=?, enabled=? WHERE name=?",
        (body.name.strip(), body.label.strip(), body.protocol,
         body.baseUrl.strip(), api_key, body.model.strip(),
         1 if body.enabled else 0, name),
    )
    con.commit()
    con.close()
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
        items = [{"id": i, "q": q, "answer": a} for i, q, a in con.execute(
            "SELECT id, q, answer FROM questions WHERE version_id=? ORDER BY id", (vid,))]
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
        con.execute("INSERT INTO questions(q, answer, version_id) VALUES(?,?,?)",
                    (it["q"], it["answer"], vid))
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
    save_setting(con, "max_tokens", body.max_tokens)
    save_setting(con, "concurrency", max(1, min(body.concurrency, 20)))
    save_setting(con, "concurrency_fallback", 1)
    save_setting(con, "station_parallel", max(1, min(body.station_parallel, 10)))
    # 修改时间后允许当天重新触发（清空防重复标记）
    save_setting(con, "last_auto_date", "")
    con.close()
    return {"ok": True}


class RunIn(BaseModel):
    """触发评测请求体：relays 为空/缺省时测全部启用站点。"""
    relays: list = None


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
        selected = [all_relays[n] for n in body.relays]
        if not selected:
            return {"ok": False, "msg": "没有选中的站点"}

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
    """服务启动时拉起定时器线程。"""
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("内置定时器已启动（每 30 秒检查一次触发时刻）")


if __name__ == "__main__":
    print("中转站质量监控系统: http://127.0.0.1:8787")
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
