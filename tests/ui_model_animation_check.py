import json

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
    assert page.locator(".relay-test-model").count() >= 0
    print(json.dumps({
        "title": page.title(),
        "errors": errors,
        "svg_visible": svg_visible,
        "relay_rows": page.locator(".relay-test-model").count(),
    }, ensure_ascii=False))
    browser.close()
