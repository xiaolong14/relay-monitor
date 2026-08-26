# -*- coding: utf-8 -*-
"""
演示数据生成脚本：模拟 7 天的中转站监控数据并生成日报。

用途：在填入真实 API Key 之前，先预览日报前端效果。
场景模拟：
  - A站：前 4 天正常，第 5 天起掺水（分数大跌）
  - B站：前 5 天正常，第 6 天起掺水
  - C站：一直保持稳定

运行：python seed_demo.py
注意：会清空 data/history.db 中的历史数据，正式使用前请勿重复运行。
"""

import datetime
import random

import monitor

# 模拟场景：每站 7 天的分数走势（百分比）
DEMO_SCORES = {
    "relay-a": [91, 90, 92, 89, 61, 58, 60],   # 第5天起掺水
    "relay-b": [90, 88, 91, 89, 90, 52, 55],   # 第6天起掺水
    "relay-c": [89, 90, 91, 88, 90, 92, 91],   # 一直稳定
}


def seed():
    """生成 7 天模拟数据并输出最终日报。"""
    cfg = monitor.load_config()
    settings = {
        "baseline_days": cfg["settings"].get("baseline_days", 7),
        "min_history_days": cfg["settings"].get("min_history_days", 3),
        "drop_threshold": cfg["settings"].get("drop_threshold", 8),
    }
    random.seed(42)  # 固定随机种子，保证演示数据可复现

    monitor.REPORT_DIR.mkdir(exist_ok=True)
    con = monitor.init_db()
    con.execute("DELETE FROM runs")  # 清空旧数据
    con.commit()

    today = datetime.date.today()
    rows_by_day = {}
    total_q = len(cfg["questions"])

    # 按时间顺序逐日写入数据，保证 evaluate 的基线/事件逻辑与真实运行一致
    for day_offset in range(7):
        date_str = (today - datetime.timedelta(days=6 - day_offset)).isoformat()
        day_rows = []
        for relay in cfg["relays"]:
            score = DEMO_SCORES[relay["name"]][day_offset]
            passed = round(total_q * score / 100)
            latency = random.uniform(700, 1600)  # 模拟平均延迟
            day_rows.append(
                monitor.evaluate(con, relay, date_str, score, passed, total_q,
                                 latency, settings)
            )
        rows_by_day[date_str] = day_rows

    # 用最后一天的数据渲染日报
    final_date = today.isoformat()
    rows = rows_by_day[final_date]
    best, note = monitor.pick_best(rows)
    html_text = monitor.render_html(final_date, rows, best, note, con)

    out_path = monitor.REPORT_DIR / f"{final_date}.html"
    out_path.write_text(html_text, encoding="utf-8")
    (monitor.REPORT_DIR / "latest.html").write_text(html_text, encoding="utf-8")
    con.close()

    print("演示日报已生成:", out_path)
    print(f"今日推荐站点: {best['label']}（今日分数 {best['score']}）{note}")
    print("\n各站今日状态:")
    for r in rows:
        print(f"  {r['label']}: 分数 {r['score']} | 状态 {r['status']} | {r['event']}")


if __name__ == "__main__":
    seed()
