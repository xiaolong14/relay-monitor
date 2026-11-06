# -*- coding: utf-8 -*-
"""用临时数据库验证真实设置页面，不启动定时任务或调用外部渠道。"""

import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor
import server


def main():
    screenshot = Path(tempfile.gettempdir()) / "relay-monitor-reasoning-settings.png"
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config = root / "config.json"
        config.write_text('{"relays": [], "settings": {}}', encoding="utf-8")

        def connect():
            con = sqlite3.connect(root / "settings.db")
            monitor.init_relay_schema(con)
            con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
            return con

        with patch.object(server, "get_con", connect), patch.object(monitor, "CONFIG_PATH", config):
            # 不进入 TestClient 生命周期，以免启动应用的后台定时器。
            client = TestClient(server.app)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1280, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def serve_page(route):
                        request = route.request
                        url = urlparse(request.url)
                        if url.hostname != "relay-monitor.test":
                            route.fulfill(status=200, content_type="application/javascript", body="")
                        elif url.path == "/":
                            route.fulfill(status=200, content_type="text/html",
                                          body=server.INDEX_PATH.read_text("utf-8"))
                        elif url.path == "/api/settings" or url.path.startswith("/api/relays"):
                            response = client.request(
                                request.method, url.path, content=request.post_data,
                                headers={"Content-Type": "application/json"},
                            )
                            route.fulfill(status=response.status_code, content_type="application/json",
                                          body=response.content)
                        elif url.path == "/api/days":
                            route.fulfill(status=200, json=[])
                        elif url.path == "/api/run/status":
                            route.fulfill(status=200, json={"running": False})
                        elif url.path == "/api/svg-generations":
                            route.fulfill(status=200, json={"items": [], "state": {"enabled": False}})
                        else:
                            raise AssertionError(f"出现非预期请求：{request.method} {url.path}")

                    page.route("**/*", serve_page)
                    page.goto("http://relay-monitor.test/", wait_until="networkidle")
                    page.locator('nav button[data-tab="settings"]').click()
                    select = page.locator("#s_reasoning")
                    expect(select).to_have_value("xhigh")
                    expect(page.locator("#s_anthropic_reasoning")).to_have_value("xhigh")
                    expect(select.locator("option")).to_have_count(8)
                    for effort in ("max", "high", "none", "default", "xhigh"):
                        select.select_option(effort)
                        page.get_by_role("button", name="保存设置").click()
                        expect(page.locator("#toast")).to_have_text("设置已保存")
                        page.reload(wait_until="networkidle")
                        page.locator('nav button[data-tab="settings"]').click()
                        expect(select).to_have_value(effort)
                    page.locator('nav button[data-tab="relays"]').click()
                    expect(page.locator("#r_protocol option")).to_have_count(3)

                    def add_channel(name, label, protocol, effort):
                        page.get_by_role("button", name="新增中转站", exact=True).click()
                        expect(page.locator("#r_reasoning")).to_have_value("")
                        page.locator("#r_label").fill(label)
                        page.locator("#r_name").fill(name)
                        page.locator("#r_model").fill("mock-model")
                        page.locator("#r_baseUrl").fill("https://relay.invalid/v1")
                        page.locator("#r_apiKey").fill("mock-key-for-browser-test")
                        page.locator("#r_protocol").select_option(protocol)
                        if protocol == "anthropic":
                            expect(page.locator('#r_reasoning option[value="max"]')).to_have_count(1)
                            expect(page.locator('#r_reasoning option[value="minimal"]')).to_have_count(0)
                            page.locator("#r_reasoning").select_option("max")
                            page.locator("#r_protocol").select_option("openai")
                            expect(page.locator('#r_reasoning option[value="max"]')).to_have_count(1)
                            page.locator("#r_protocol").select_option("anthropic")
                            expect(page.locator("#r_reasoning")).to_have_value("max")
                        page.locator("#r_reasoning").select_option(effort)
                        page.locator("#relayDlg").get_by_role("button", name="保存", exact=True).click()
                        expect(page.locator("#relayDlg")).not_to_be_visible()
                        expect(page.locator("#relayTable")).to_contain_text(label)

                    add_channel("openai-fixed", "独立 OpenAI", "openai", "high")
                    add_channel("responses-fixed", "独立 Responses", "openai-responses", "minimal")
                    add_channel("anthropic-fixed", "独立 Anthropic", "anthropic", "max")
                    add_channel("openai-inherit", "继承 OpenAI", "openai", "")
                    page.locator('nav button[data-tab="settings"]').click()
                    select.select_option("low")
                    page.locator("#s_anthropic_reasoning").select_option("medium")
                    with page.expect_response(lambda response: response.request.method == "PUT" and
                                              response.url.endswith("/api/settings")) as saved:
                        page.get_by_role("button", name="保存设置").click()
                    assert saved.value.status == 200
                    page.reload(wait_until="networkidle")
                    page.locator('nav button[data-tab="relays"]').click()
                    for label, expected in (("独立 OpenAI", "高（high）"),
                                            ("独立 Responses", "最低（minimal）"),
                                            ("独立 Anthropic", "最高（max）"),
                                            ("继承 OpenAI", "跟随默认 · 低（low）")):
                        expect(page.locator("#relayTable tr").filter(has_text=label)).to_contain_text(expected)
                    responses_row = page.locator("#relayTable tr").filter(has_text="独立 Responses")
                    expect(responses_row).to_contain_text("OpenAI Responses")
                    responses_row.get_by_role("button", name="编辑", exact=True).click()
                    expect(page.locator("#r_protocol")).to_have_value("openai-responses")
                    expect(page.locator("#r_reasoning")).to_have_value("minimal")
                    page.locator("#relayDlg").get_by_role("button", name="取消", exact=True).click()
                    page.locator("#relayTable tr").filter(has_text="独立 Anthropic").get_by_role(
                        "button", name="编辑", exact=True).click()
                    expect(page.locator("#r_protocol")).to_have_value("anthropic")
                    expect(page.locator("#r_reasoning")).to_have_value("max")
                    page.screenshot(path=str(screenshot), full_page=True, animations="disabled")
                    page.set_viewport_size({"width": 390, "height": 844})
                    channel_select = page.locator("#r_reasoning")
                    channel_select.scroll_into_view_if_needed()
                    bounds = channel_select.bounding_box()
                    assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
                    assert not errors, errors
                    browser.close()
                print("浏览器检查通过：渠道新增和编辑、协议切换、独立配置、默认继承、刷新回显及移动端显示均正常。")
                print(f"截图：{screenshot}")
            finally:
                client.close()


if __name__ == "__main__":
    main()
