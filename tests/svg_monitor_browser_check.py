# -*- coding: utf-8 -*-
"""运行中的本地服务 UI 冒烟检查（模拟生成接口，不触发真实渠道）。"""

import datetime
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


now = datetime.datetime.now().isoformat(timespec="seconds")
fake_state = {
    "items": [],
    "state": {
        "running": False, "started_at": None, "trigger": None,
        "current_id": None, "total": 0, "completed": 0, "failed": 0,
        "next_run_at": now, "enabled": True, "interval_minutes": 30,
    },
}
preview_requests = 0
generate_requests = 0
failure_log_requests = 0

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    def mock_svg_api(route):
        global preview_requests, generate_requests, failure_log_requests
        request = route.request
        if request.url.endswith("/failure-log"):
            failure_log_requests += 1
            route.fulfill(
                status=200, content_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="svg-generation-2-failure.log"'},
                body="模拟完整失败日志\ndata: 原始 SSE 事件\n",
            )
        elif request.url.endswith("/preview"):
            preview_requests += 1
            route.fulfill(
                status=200,
                content_type="image/svg+xml",
                body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"><animate attributeName="opacity" from=".5" to="1" dur="1s" repeatCount="indefinite"/></circle></svg>',
            )
        elif request.method == "POST":
            generate_requests += 1
            if fake_state["items"]:
                route.fulfill(status=200, json={
                    "ok": True, "code": "already_running",
                    "msg": "所选渠道已有生成任务进行中，未重复提交",
                })
                return
            fake_state["state"].update(running=True, total=1, completed=0, failed=0)
            fake_state["items"] = [{
                "id": 1, "relay": "relay-a", "relay_label": "模拟渠道",
                "model_id": "mock-model", "status": "generating", "trigger": "manual",
                "error_detail": None, "requested_at": now, "started_at": now,
                "completed_at": None,
            }]
            route.fulfill(status=200, json={"ok": True, "code": "started", "msg": "已开始模拟生成"})
        else:
            route.fulfill(status=200, json=fake_state)

    # 原生 download 请求可能不属于页面网络目标，需要在浏览器上下文拦截。
    page.context.route("**/api/svg-generations/**", mock_svg_api)
    page.context.route("**/api/svg-generations*", mock_svg_api)
    page.goto("http://127.0.0.1:8787/", wait_until="networkidle")
    page.locator("#svgMonitorCard").wait_for(state="visible")

    table = page.locator("#svgGenerationTable").inner_text()
    assert "生成渠道" in table and "完成时间" in table
    assert page.locator("#svgRelaySelect option").count() >= 1
    page.locator("#svgGenerateBtn").click()
    page.wait_for_timeout(500)
    page.get_by_text("生成中").first.wait_for(timeout=3000)
    assert fake_state["items"], "模拟生成接口没有创建记录"
    assert page.locator("#svgGenerateBtn").is_enabled(), "生成期间应可继续提交手动请求"
    page.locator("#svgGenerateBtn").click()
    page.wait_for_timeout(300)
    assert generate_requests == 2 and len(fake_state["items"]) == 1, "重复渠道请求未被正确去重"
    fake_state["items"][0].update(status="completed", completed_at=now)
    fake_state["state"].update(running=False, completed=1)
    page.wait_for_function("document.querySelectorAll('iframe.svg-preview').length === 1", timeout=5000)
    assert "已完成" in page.locator("#svgGenerationTable").inner_text()
    page.locator("iframe.svg-preview").evaluate("element => element.dataset.stable = 'yes'")
    page.wait_for_timeout(4500)
    assert preview_requests == 1, "状态轮询重复请求了已完成动画预览"
    assert page.locator("iframe.svg-preview").get_attribute("data-stable") == "yes", "状态轮询重建了未变化的预览行"

    fake_state["items"].extend([
        {"id": 2, "relay": "failed-test", "relay_label": "失败日志测试", "model_id": "mock-model",
         "status": "failed", "trigger": "manual", "error_detail": "模拟长度截断",
         "requested_at": now, "completed_at": now, "failure_log_available": True},
        {"id": 3, "relay": "old-failure", "relay_label": "历史失败测试", "model_id": "mock-model",
         "status": "failed", "trigger": "manual", "error_detail": "旧失败记录",
         "requested_at": now, "completed_at": now, "failure_log_available": False},
    ])
    page.get_by_role("link", name="下载完整失败日志").wait_for(timeout=5000)
    assert page.get_by_text("该记录未保留原始日志", exact=True).count() == 1
    with page.expect_download() as download_info:
        page.get_by_role("link", name="下载完整失败日志").click()
    download = download_info.value
    assert download.suggested_filename == "svg-generation-2-failure.log", (
        download.suggested_filename, download.url, failure_log_requests,
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        saved_path = Path(temp_dir) / download.suggested_filename
        download.save_as(saved_path)
        assert "原始 SSE 事件" in saved_path.read_text(encoding="utf-8")

    settings = page.request.get("http://127.0.0.1:8787/api/settings").json()
    page.locator('nav button[data-tab="settings"]').click()
    assert page.locator("#s_svg_interval").input_value() == str(settings["svg_interval_minutes"])
    assert page.locator("#s_svg_enabled").is_checked() == settings["svg_monitor_enabled"]
    print("浏览器检查通过：生成期间可手动触发、重复渠道防重、预览稳定、失败日志可下载、历史缺失日志说明及设置正常。")
    browser.close()
