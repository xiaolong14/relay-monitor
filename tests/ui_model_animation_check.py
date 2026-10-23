import json
import time

from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto("http://127.0.0.1:8787/")
    page.wait_for_load_state("networkidle")
    assert page.locator("nav button[data-tab=svg]").count() == 1
    page.locator("nav button[data-tab=svg]").click()
    page.wait_for_timeout(500)
    assert page.locator("#tab-svg").is_visible()
    assert not page.locator("#tab-dash").is_visible()
    assert page.locator("#svgModelSelect").count() == 1
    svg_visible = page.locator("#tab-svg").is_visible()
    page.locator("nav button[data-tab=relays]").click()
    page.wait_for_timeout(500)
    assert page.locator("#tab-relays").is_visible()
    model_selects = page.locator(".relay-test-model")
    assert model_selects.count() > 0
    first_model_cell = model_selects.nth(0).locator("xpath=ancestor::td")
    reasoning_cell = first_model_cell.locator("xpath=following-sibling::td[1]")
    model_box = first_model_cell.bounding_box()
    reasoning_box = reasoning_cell.bounding_box()
    assert model_box["x"] + model_box["width"] <= reasoning_box["x"] + 1

    def fake_models(route):
        time.sleep(0.15)
        route.fulfill(json={"ok": True, "relay": "zuco", "models": ["model-a", "model-b"], "current_model": "model-a"})

    page.route("**/api/relays/zuco/models", fake_models)
    fetch_button = page.locator('.relay-fetch-models[data-relay-name="zuco"]')
    fetch_button.click()
    page.wait_for_timeout(300)
    assert "已完成" in fetch_button.inner_text()
    assert "已拉取 2 个模型" in page.locator('[data-relay-feedback="zuco"]').inner_text()
    assert model_selects.nth(0).locator("option").count() == 2
    print(json.dumps({
        "title": page.title(),
        "errors": errors,
        "svg_visible": svg_visible,
        "relay_rows": page.locator(".relay-test-model").count(),
    }, ensure_ascii=False))
    browser.close()
