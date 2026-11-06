# -*- coding: utf-8 -*-
"""验收已有真实生成记录及动画预览；不创建任务，不调用收费上游接口。"""

import argparse
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from playwright.sync_api import sync_playwright


def get(base_url, path):
    with urllib.request.urlopen(base_url + path, timeout=15) as response:
        return response.read(), dict(response.headers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generation_id", type=int, help="已完成的真实生成记录 ID")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--output-dir", type=Path, required=True, help="验收报告与截图目录")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    data = json.loads(get(base, "/api/svg-generations?limit=100")[0])
    row = next((item for item in data["items"] if item["id"] == args.generation_id), None)
    assert row is not None, "最近 100 条记录中找不到该记录"
    assert row["status"] == "completed", f"记录尚未成功完成：{row['status']}，{row.get('error_detail')}"
    raw, headers = get(base, f"/api/svg-generations/{args.generation_id}/preview")
    root = ET.fromstring(raw)
    assert root.tag == "{http://www.w3.org/2000/svg}svg", "预览根节点不是 SVG"
    animations = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1]
                  in {"animate", "animateTransform", "set"}]
    assert animations, "预览没有动画节点"
    assert "image/svg+xml" in headers.get("content-type", ""), "接口未使用 SVG 类型"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "preview.svg").write_bytes(raw)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.goto(base + f"/api/svg-generations/{args.generation_id}/preview", wait_until="networkidle")
            assert page.locator("parsererror").count() == 0, "浏览器 SVG XML 解析失败"
            svg = page.locator("svg").first
            svg.wait_for(state="visible")
            snapshots = []
            for index, moment in enumerate((0.25, 1.1), 1):
                svg.evaluate("(svg, time) => { svg.pauseAnimations(); svg.setCurrentTime(time); }", moment)
                image = svg.screenshot(path=str(args.output_dir / f"animation-{index}.png"))
                snapshots.append(hashlib.sha256(image).hexdigest())
            assert snapshots[0] != snapshots[1], "两个时间点画面相同，动画未通过播放验收"
            page.goto(base, wait_until="networkidle")
            preview = page.locator(f'[data-svg-preview-id="{args.generation_id}"]')
            preview.wait_for(state="visible")
            preview.scroll_into_view_if_needed()
            assert "已完成" in preview.locator("xpath=ancestor::tr").inner_text()
            preview.click()
            page.locator("#svgPreviewDlg[open]").wait_for(state="visible")
            frame = page.frame_locator("#svgPreviewFrame")
            frame.locator("svg").wait_for(state="visible")
            page.screenshot(path=str(args.output_dir / "dashboard-preview.png"))
        finally:
            browser.close()
    report = {
        "generation": row, "preview_bytes": len(raw), "animation_nodes": len(animations),
        "preview_sha256": hashlib.sha256(raw).hexdigest(),
        "different_animation_frames": True, "dashboard_preview_passed": True,
    }
    (args.output_dir / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
