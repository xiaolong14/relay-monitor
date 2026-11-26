"""用只读历史记录和手绘样本做真实渠道评分对照。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from relay_monitor import svg_review
from relay_monitor.svg_artwork import sanitize_svg

V1 = """你在评审拟人化鹈鹕沿海岸公路骑复古自行车的最终画面。只依据随附的渲染图片判断，不读取或推断任何源码、坐标、路径或图层信息。

先检查骑乘是否成立：臀部与鞍座承托、翼尖与车把接触、蹼足落在对应踏板、车架前叉连接车轴，画面实际能看清这些关系。再检查鹈鹕头颈与长嘴喉囊造型、车与鸟的比例、海岸/道路前中后景、线条和局部收边。把轮辐、大片平涂、太阳、云和草的简单重复视为一个层级的常规描绘。若有动画，检查实际方向、接触和循环连贯性，不给只有标签但看不出有效运动的动画加分。

从最贴近的档位起评：50–69 是能清楚认出主题的草图或基础插画，画面平、像符号拼接、骑乘造型或道路生硬；70–79 是可发布的常规插图，构图完整但结构或完成度仍粗；80–89 是成熟插画，有稳定的造型、空间与局部细节；90–94 要求骑乘关系可信、结构清晰、画面有组织且近看有精修；95+ 接近专业成品。只要明显悬脚、主要握把或座位无法解释、车架断开，最高 79。结构生硬且环境仅为平带或零散装饰，最高 69。不要默认从满分起扣。根据明显缺点定档，不把主要结构错误当成小瑕疵。

只输出 JSON：{\"score\": 0 到 100 的数字, \"problems\": [\"最多 5 条可定位的可见问题\"], \"comment\": \"40 字以内总评\"}。"""

VISION_PROMPT = """你是鹈鹕骑车 SVG 插画的视觉评审员。你只会看到 SVG 在浏览器中的真实渲染图片。仅凭图片判断最终画面，不假设不可见的代码结构、指定坐标或部件存在。小部件在画面里被合理遮住时，不应当作断裂。

请评价观众实际看见的整体绘画质量。比较主体是否好看、鹈鹕是否清楚可辨、身体和自行车的比例与造型、骑车姿态是否自然、笔触和线条的精细程度、背景的空间层次、色彩协调性和完成度。写缺点前先判断其是否足以明显降低实际观感。仅当图片确实让人看出角色离座、明显悬空、鸟和车分离、或车体明显破损时，才把骑行关系记为 broken。含糊或微小的重叠只能记为 unclear。点式或平涂风格只要设计成熟、轮廓和空间有组织，仍可得高分。

总分等于以下分项的总和。独立评价骑行造型与整体画面，让有美感、完成度好的插画得到配得上的分数：
- riding_and_form，35 分。主体残缺或姿态明显僵硬约 18–23 分；主体造型协调、姿态大致可信约 26–30 分；鸟的造型、身体比例、头部表情和动作组织成熟约 31–35 分。明确姿态缺陷只扣本项，不能抹掉其余分项的视觉完成度。
- composition_and_depth，25 分。构图失衡或空间混乱约 10–15 分；主体清楚、环境完整、层次基本自然约 17–20 分；前中后景组织、留白、空间和视觉引导都成熟约 21–25 分。
- finish，25 分。主体轮廓粗糙、部件形状紊乱或明显残缺约 10–15 分；整体完成、轮廓清楚、可见细节充足约 17–20 分；主要局部精致清晰、画面细节和线条有完整取舍约 21–25 分。
- color_and_readability，15 分。色彩混乱或主体难辨约 6–9 分；配色协调、轮廓好读约 10–12 分；配色、对比和色调控制成熟约 13–15 分。

检查鞍座承托、翅膀或前肢与把手、双脚与脚踏、自行车整体完整性。按图片的实际可见证据描述。骑乘图里两个关键关系确实明显错误时，可以指出并适当降低造型分，但应给海岸背景、配色、轮廓与工艺等独立优点相应分数。不要让一项可争议的遮挡将整幅画限到低分。severe_presentation 仅在画面几乎不能识别题材、主体严重残缺或整体显著失衡时为 true。评分必须反映整体图片观感，不得从满分机械地按缺点扣分。

只输出一个 JSON 对象。score 等于四项 dimensions 的分项总和。broken 仅表示图片确实清楚显示断接，不能只凭代码坐标推测。视觉证据描述能够直接辨认的造型和画面布局，不虚构坐标或不存在的部件。
{"score":0,"riding":{"saddle":{"status":"ok|unclear|broken","evidence":"图中可见证据"},"handlebar":{"status":"ok|unclear|broken","evidence":"图中可见证据"},"pedals":{"status":"ok|unclear|broken","evidence":"图中可见证据"},"bicycle":{"status":"ok|unclear|broken","evidence":"图中可见证据"}},"visual_evidence":{"composition_and_depth":"画面证据","finish":"画面证据"},"dimensions":{"riding_and_form":0,"composition_and_depth":0,"finish":0,"color_and_readability":0},"severe_presentation":false,"problems":[],"comment":"不超过40字的整体观感"}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("v1", "trial", "vision", "current"), required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--relay")
    parser.add_argument("--effort")
    parser.add_argument("--image-dir", type=Path)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("samples", nargs="+")
    args = parser.parse_args()
    db = ROOT / "data" / "history.db"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
        settings = dict(con.execute("SELECT key, value FROM settings"))
        columns = "name, label, protocol, baseUrl, apiKey, model"
        relay = dict(zip(columns.split(", "), con.execute(
            f"SELECT {columns} FROM relays WHERE name=?",
            (args.relay or settings["svg_review_relay"],),
        ).fetchone()))
        samples = {}
        for sample in args.samples:
            if sample.isdecimal():
                samples[sample] = con.execute(
                    "SELECT svg_content FROM svg_generations WHERE id=?", (int(sample),),
                ).fetchone()[0]
            else:
                samples[Path(sample).stem] = sanitize_svg(Path(sample).read_text(encoding="utf-8"))
    prompt = {
        "v1": V1,
        "vision": VISION_PROMPT,
        "current": None,
    }.get(args.variant)
    if args.variant == "trial":
        prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    model = settings["svg_review_model"] or relay["model"]
    effort = args.effort or settings["svg_review_reasoning_effort"]
    if args.image_dir:
        previews = {}
        for sample in samples:
            name = sample.removeprefix("pelican_ride_") if sample.isidentifier() else f"history-{sample}"
            previews[sample] = (args.image_dir / f"pelican-{name}.png").read_bytes()
    else:
        # 与服务端使用同一套渲染链路，所有提示词变体都只看成品图。
        previews = {sample: svg_review.render_svg_preview(svg) for sample, svg in samples.items()}

    def score(task):
        sample, iteration = task
        start = time.monotonic()
        try:
            result = svg_review.review_svg(
                relay, preview_image=previews[sample], prompt=prompt, model=model,
                api_timeout_minutes=5, reasoning_effort=effort,
            )
        except svg_review.SVGReviewError as exc:
            result = {"error": str(exc)}
        return {"variant": args.variant, "sample": sample, "iteration": iteration,
                "relay": relay["name"], "model": model, "effort": effort,
                "elapsed": round(time.monotonic() - start, 1),
                **result}

    tasks = [(sample, i + 1) for i in range(args.count) for sample in samples]
    with args.output.open("a", encoding="utf-8", buffering=1) as output:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for result in executor.map(score, tasks):
                line = json.dumps(result, ensure_ascii=False)
                print(line, flush=True)
                output.write(line + "\n")


if __name__ == "__main__":
    main()
