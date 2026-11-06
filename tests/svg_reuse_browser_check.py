# -*- coding: utf-8 -*-
"""在与项目相同的 iframe 沙箱中验证复用轮子；不请求模型、不修改历史记录。"""

import io
import sys
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import svg_artwork


SOURCE = '''<svg xmlns="http://www.w3.org/2000/svg"
 xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 320 180" width="100%" height="100%">
 <defs><symbol id="wheel" viewBox="-45 -45 90 90">
  <circle r="40" fill="none" stroke="#183b2f" stroke-width="6"/>
  <circle r="35" fill="none" stroke="#e8dfc8" stroke-width="3"/>
  <g stroke="#183b2f" stroke-width="2">
   <animateTransform attributeName="transform" type="rotate" from="0" to="360"
    dur="2s" repeatCount="indefinite"/>
   <path d="M-35 0H35 M0 -35V35 M-25 -25L25 25 M-25 25L25 -25"/>
  </g>
 </symbol></defs>
 <rect width="320" height="180" fill="#f8f0dc"/>
 <use href="#wheel" x="35" y="55" width="90" height="90"/>
 <use xlink:href="#wheel" x="195" y="55" width="90" height="90"/>
 <path d="M80 100L130 45L165 100Z M130 45L220 45L165 100 M220 45L240 100"
  fill="none" stroke="#3b6b56" stroke-width="4"/>
</svg>'''


def main():
    output = Path(__file__).resolve().parents[1] / "logs" / "svg-wheel-investigation"
    output.mkdir(parents=True, exist_ok=True)
    cleaned = svg_artwork.sanitize_svg(SOURCE)
    # 模拟旧白名单的丢失效果，与修复后的真实绘制结果比较。
    import xml.etree.ElementTree as ET
    old = ET.fromstring(cleaned)
    for use in list(old.findall("{http://www.w3.org/2000/svg}use")):
        old.remove(use)
    svgs = {"original": SOURCE, "cleaned": cleaned, "old-filter": ET.tostring(old, encoding="unicode")}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 640, "height": 360}, device_scale_factor=1)

            def route_request(route):
                name = route.request.url.rsplit("/", 1)[-1].removesuffix(".svg")
                if name in svgs:
                    route.fulfill(status=200, content_type="image/svg+xml", body=svgs[name],
                                  headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:;"})
                else:
                    route.fulfill(status=200, content_type="text/html", body='''
                      <style>body{margin:0}iframe{width:640px;height:360px;border:0}</style>
                      <iframe sandbox src="/original.svg"></iframe>''')

            page.route("**/*", route_request)
            page.goto("http://127.0.0.1:8787/svg-reuse-check")
            page.wait_for_load_state("networkidle")
            screenshots = {}
            for name in svgs:
                with page.expect_event("framenavigated", predicate=lambda f: f.url.endswith(f"/{name}.svg")) as navigation:
                    page.locator("iframe").evaluate("(frame, url) => frame.src = url", f"/{name}.svg")
                navigation.value.wait_for_load_state("load")
                frame = page.frame_locator("iframe")
                frame.locator("svg").wait_for()
                frame.locator("svg").evaluate("svg => {svg.pauseAnimations(); svg.setCurrentTime(0.4);}")
                if name != "old-filter":
                    boxes = frame.locator("use").evaluate_all("nodes => nodes.map(n => {const b=n.getBBox();return [b.width,b.height];})")
                    assert len(boxes) == 2 and all(w > 70 and h > 70 for w, h in boxes), boxes
                screenshots[name] = page.screenshot(path=str(output / f"reuse-{name}.png"))
                if name == "cleaned":
                    frame.locator("svg").evaluate("svg => svg.setCurrentTime(0.8)")
                    next_frame = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                    current_frame = Image.open(io.BytesIO(screenshots[name])).convert("RGB")
                    assert ImageChops.difference(current_frame, next_frame).getbbox() is not None, "复用轮子的动画未播放"
            images = {name: Image.open(io.BytesIO(data)).convert("RGB") for name, data in screenshots.items()}
            assert ImageChops.difference(images["original"], images["cleaned"]).getbbox() is None, "净化前后绘制不一致"
            assert ImageChops.difference(images["old-filter"], images["cleaned"]).getbbox() is not None, "未复现旧过滤器丢轮子"
            print("浏览器验证通过：沙箱中两个引用轮子均可见，净化前后逐像素一致，旧过滤器丢轮子现象已复现。")
            print(f"验证图片：{output}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
