"""用浏览器渲染实际送评的 SVG，供核对可见细节。"""

from pathlib import Path
import argparse
import sqlite3
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from relay_monitor.svg_artwork import sanitize_svg

parser = argparse.ArgumentParser(description="将两张鹈鹕评分样本渲染成便于检查的 PNG")
parser.add_argument("--output-dir", type=Path, default=ROOT / "logs" / "svg-review-samples")
parser.add_argument("--history-id", type=int, default=679)
args = parser.parse_args()
args.output_dir.mkdir(parents=True, exist_ok=True)
samples = {
    name: sanitize_svg((ROOT / "tests" / "fixtures" / "svg" / f"pelican_ride_{name}.svg").read_text(encoding="utf-8"))
    for name in ("rough", "refined")
}
db = ROOT / "data" / "history.db"
with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
    row = con.execute(
        "SELECT svg_content FROM svg_generations WHERE id=?", (args.history_id,),
    ).fetchone()
    if row and row[0]:
        samples[f"history-{args.history_id}"] = row[0]

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 700, "height": 470}, device_scale_factor=1)
    for name, svg in samples.items():
        page.set_content(f'<body style="margin:0;height:100vh">{svg}</body>')
        page.locator("svg").evaluate("svg => {svg.pauseAnimations(); svg.setCurrentTime(0)}")
        page.screenshot(path=args.output_dir / f"pelican-{name}.png")
    browser.close()
