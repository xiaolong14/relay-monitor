# -*- coding: utf-8 -*-
"""严格规范 SVG 题的本地判分器。

设计目标：
1. 题卡（resources/svg_specs/*.json）同时保存提示词和判分参数，保证题面与判分同源；
2. 纯标准库解析 SVG 几何属性，不渲染、不依赖浏览器，结果可复现；
3. 采用 100 分扣分制，每个扣分项给出可读原因，方便人工对照 SVG 判断问题。
"""

import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
ANIMATION_TAGS = {"animate", "animateTransform", "set"}

SECTION_LABELS = {
    "structure": "结构",
    "elements": "元素与数量",
    "geometry": "几何构图",
    "palette": "配色",
    "text": "文字",
    "animation": "动画",
    "detail": "画面细节",
}

# 各检查项在章节内的相对权重。章节总分从题卡读取，改分数时只需改题卡。
STRUCTURE_WEIGHTS = {
    "view_box": 3,
    "layers_present": 6,
    "layers_order": 3,
    "layers_direct": 3,
}
ELEMENT_WEIGHTS = {
    "sun": 1.5,
    "clouds": 2,
    "sailboat": 1.5,
    "lighthouse": 1,
    "lighthouse_stripes": 2,
    "wheels": 1.5,
    "spokes_front": 2,
    "spokes_rear": 2,
    "hud_text": 1.5,
    "grass": 3,
}
# 几何检查逐项权重（同一检查内部还会再拆分，例如太阳 cx/cy/r 各占 1）。
GEOMETRY_WEIGHTS = {
    "sun": 2,
    "clouds": 2,
    "sea": 1.5,
    "road": 1.5,
    "wheels_center": 1.5,
    "wheels_radius": 0.75,
    "wheels_gap": 1.25,
    "spokes_radius": 2,
    "body": 3.5,
    "beak": 2.5,
    "scarf": 1.5,
}
DETAIL_WEIGHTS = {
    "hubs": 1,
    "inner_rims": 1,
    "drivetrain": 2,
    "controls": 1,
    "neck": 2,
    "face_details": 1.5,
    "scarf_knot": 0.5,
    "lighthouse_details": 1,
    "cloud_shadows": 1,
    "hud_panel": 1,
    "drawing_elements": 4,
    "path_nodes": 3,
}
DRAWING_TAGS = ("path", "line", "circle", "rect", "ellipse", "polygon", "polyline")
TEXT_WEIGHTS = {"content": 4, "font_size": 2, "position": 2}
ANIMATION_ITEM_WEIGHTS = {"exists": 1.0, "kind": 0.5, "timing": 0.5}

_NUMBER_PATTERN = re.compile(r"^\s*(-?(?:\d+(?:\.\d*)?|\.\d+))")
_PATH_COMMAND = re.compile(r"([A-Za-z])([^A-Za-z]*)")
_PATH_NUMBER = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)")
_COMMAND_PARAM_COUNTS = {
    "M": 2, "L": 2, "T": 2, "Q": 4, "S": 4, "C": 6,
    "Z": 0, "H": 1, "V": 1, "A": 7,
}


def load_spec(path):
    """读取题卡 JSON，并把提示词数组拼接成可直接发送的字符串。"""
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ("id", "version", "viewBox", "layers", "weights", "geometry",
                "palette", "text", "animations", "prompt")
    missing = [key for key in required if key not in spec]
    if missing:
        raise ValueError("题卡缺少字段：" + "、".join(missing))
    if isinstance(spec["prompt"], list):
        spec["prompt"] = "\n".join(str(line) for line in spec["prompt"])
    return spec


def evaluate_svg(svg_text, spec):
    """对一份 SVG 源码按题卡打分。

    无法拿到完整 SVG（空内容、根标签截断、XML 非法）时返回 score=None，
    这类失败与“画得不合格”分开统计，避免把上游断流当成 0 分作品。
    """
    if not isinstance(svg_text, str) or not svg_text.strip():
        return _invalid_result(spec, "SVG 内容为空")
    match = re.search(r"<svg\b[^>]*>.*?</svg\s*>", svg_text, re.S | re.I)
    if not match:
        return _invalid_result(spec, "没有找到完整的 <svg>...</svg> 根元素，输出可能被截断")
    try:
        root = ET.fromstring(match.group(0))
    except ET.ParseError as exc:
        return _invalid_result(spec, f"SVG XML 解析失败：{exc}")
    if _local_name(root.tag) != "svg":
        return _invalid_result(spec, "根元素不是 svg")

    nodes = _walk(root)
    id_map = {}
    duplicate_ids = set()
    for element, _parent in nodes:
        element_id = element.get("id")
        if not element_id:
            continue
        if element_id in id_map:
            duplicate_ids.add(element_id)
        else:
            id_map[element_id] = element

    scorer = _Scorer(spec["weights"])
    _check_structure(root, nodes, id_map, spec, scorer)
    _check_elements(nodes, id_map, spec, scorer)
    _check_geometry(id_map, spec, scorer)
    _check_palette(id_map, spec, scorer)
    _check_text(id_map, spec, scorer)
    _check_animations(id_map, spec, scorer)
    _check_details(id_map, spec, scorer)
    _check_richness(root, spec, scorer)

    notes = []
    if duplicate_ids:
        notes.append("存在重复 id：" + "、".join(sorted(duplicate_ids)))
    return scorer.result(spec, notes)


class _Scorer:
    """累计每个章节的扣分，并在输出时裁剪到章节满分以内。"""

    def __init__(self, weights):
        self.weights = {key: float(value) for key, value in weights.items()}
        self.penalties = {key: 0.0 for key in self.weights}
        self.deductions = []

    def deduct(self, section, code, points, message):
        """记录一个扣分项；points<=0 时不产生噪声记录。"""
        if points <= 0:
            return
        self.penalties[section] += points
        self.deductions.append({
            "section": section,
            "code": code,
            "points": round(points, 2),
            "message": message,
        })

    def deduct_ratio(self, section, code, weight, total_weight, ratio, message):
        """按完成比例扣分；ratio=1 表示该项完全合格。"""
        if ratio >= 1:
            return
        points = self.weights[section] * weight / total_weight * (1 - max(0.0, ratio))
        self.deduct(section, code, points, message)

    def deduct_all(self, section, code, message):
        """整项未完成时扣掉该项全部权重。"""
        self.deduct(section, code, self.weights[section], message)

    def result(self, spec, notes):
        """生成带章节明细和扁平扣分列表的结果字典。"""
        sections = []
        total = 0.0
        for key, section_total in self.weights.items():
            penalty = min(section_total, self.penalties[key])
            score = round(section_total - penalty, 2)
            total += score
            sections.append({
                "key": key,
                "label": SECTION_LABELS.get(key, key),
                "score": score,
                "max": round(section_total, 2),
                "deductions": [item for item in self.deductions if item["section"] == key],
            })
        return {
            "status": "ok",
            "spec": f"{spec['id']}@{spec['version']}",
            "score": round(min(total, sum(self.weights.values())), 2),
            "max_score": round(sum(self.weights.values()), 2),
            "sections": sections,
            "deductions": self.deductions,
            "notes": notes,
        }


def _invalid_result(spec, error):
    """构造无法判分的结果；score 为 None，调用方应把它计入失败率。"""
    return {
        "status": "invalid",
        "spec": f"{spec['id']}@{spec['version']}",
        "score": None,
        "max_score": round(sum(float(value) for value in spec["weights"].values()), 2),
        "error": error,
        "sections": [],
        "deductions": [],
        "notes": [],
    }


def _local_name(tag):
    """去掉命名空间前缀，统一比较 SVG 元素名。"""
    return str(tag).rsplit("}", 1)[-1]


def _walk(root):
    """返回 (元素, 父元素) 列表，便于检查图层归属关系。"""
    pairs = []

    def visit(element, parent):
        pairs.append((element, parent))
        for child in list(element):
            visit(child, element)

    visit(root, None)
    return pairs


def _number(element, attribute):
    """读取数值属性；缺失、带单位或非法时安全返回 None。"""
    if element is None:
        return None
    raw = element.get(attribute)
    if raw is None:
        return None
    match = _NUMBER_PATTERN.match(str(raw))
    return float(match.group(1)) if match else None


def _close(actual, expected, tolerance):
    """比较数值是否落在容差范围内。"""
    return actual is not None and abs(actual - expected) <= tolerance


def _fmt(value):
    """把题卡数值格式化成便于阅读的文本。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _has_color(element, color):
    """元素是否用指定颜色填充或描边。"""
    if element is None:
        return False
    expected = color.upper()
    return any(
        str(element.get(name, "")).strip().upper() == expected
        for name in ("fill", "stroke")
    )


def _absolute_path_x_range(element):
    """只解析 path 中大写绝对指令的 x 坐标，返回 (最小值, 最大值)。

    题面限定长嘴与围巾使用 M/L/Q/C/Z 绝对指令；相对指令的偏移量
    不能当成画布坐标，因此这里直接跳过小写指令，避免误判。
    """
    raw = (element.get("d") if element is not None else "") or ""
    xs = []
    for match in _PATH_COMMAND.finditer(raw):
        command = match.group(1)
        if not command.isupper():
            continue
        numbers = [float(value) for value in _PATH_NUMBER.findall(match.group(2))]
        count = _COMMAND_PARAM_COUNTS.get(command.upper(), 0)
        if count <= 0:
            continue
        for index in range(0, len(numbers) - count + 1, count):
            group = numbers[index:index + count]
            if command.upper() == "H":
                xs.append(group[0])
            elif command.upper() == "V":
                continue
            elif command.upper() == "A":
                xs.extend(group[-2:])
            else:
                xs.extend(group[0::2])
    if not xs:
        return None
    return min(xs), max(xs)


def _unique_ids(nodes, pattern):
    """统计匹配正则的 id 数量，重复 id 只算一次。"""
    ids = {
        element.get("id")
        for element, _parent in nodes
        if element.get("id") and re.fullmatch(pattern, element.get("id"))
    }
    return len(ids)


def _line_end_on_circle(line, cx, cy, radius, tolerance):
    """判断 line 的任一端点是否落在以 (cx, cy) 为圆心、radius 为半径的圆周上。"""
    if line is None:
        return False
    for x_name, y_name in (("x1", "y1"), ("x2", "y2")):
        x = _number(line, x_name)
        y = _number(line, y_name)
        if x is None or y is None:
            continue
        if abs(math.hypot(x - cx, y - cy) - radius) <= tolerance:
            return True
    return False


def _absolute_path_points(element):
    """提取 path 中大写绝对指令的坐标点，用于估算包围盒。

    只处理 M/L/T/Q/S/C 这类成对坐标；H/V/A 不含完整点对，直接跳过。
    曲线控制点会参与包围盒计算，作为连接关系的近似判断已经足够。
    """
    raw = (element.get("d") if element is not None else "") or ""
    points = []
    for match in _PATH_COMMAND.finditer(raw):
        if not match.group(1).isupper():
            continue
        command = match.group(1)
        if command not in "MLTQSC":
            continue
        numbers = [float(value) for value in _PATH_NUMBER.findall(match.group(2))]
        count = _COMMAND_PARAM_COUNTS[command]
        for index in range(0, len(numbers) - count + 1, count):
            group = numbers[index:index + count]
            for offset in range(0, count, 2):
                points.append((group[offset], group[offset + 1]))
    return points


def _element_bounds(element):
    """估算元素包围盒 (min_x, min_y, max_x, max_y)，无法判断时返回 None。"""
    if element is None:
        return None
    tag = _local_name(element.tag)
    if tag == "circle":
        cx, cy, radius = _number(element, "cx"), _number(element, "cy"), _number(element, "r")
        if None in (cx, cy, radius):
            return None
        return cx - radius, cy - radius, cx + radius, cy + radius
    if tag == "ellipse":
        cx, cy = _number(element, "cx"), _number(element, "cy")
        rx, ry = _number(element, "rx"), _number(element, "ry")
        if None in (cx, cy, rx, ry):
            return None
        return cx - rx, cy - ry, cx + rx, cy + ry
    if tag == "rect":
        x = _number(element, "x") or 0.0
        y = _number(element, "y") or 0.0
        width, height = _number(element, "width"), _number(element, "height")
        if width is None or height is None:
            return None
        return x, y, x + width, y + height
    if tag == "path":
        points = _absolute_path_points(element)
        if not points:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        return min(xs), min(ys), max(xs), max(ys)
    return None


def _boxes_intersect(first, second):
    """判断两个包围盒是否相交（含边缘接触）。"""
    if first is None or second is None:
        return False
    return (first[0] <= second[2] and second[0] <= first[2]
            and first[1] <= second[3] and second[1] <= first[3])


def _relative_luminance(color):
    """把 #RRGGBB 转成 WCAG 相对亮度，用于判断文字与底框的对比度。"""
    text = str(color or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return None
    channels = [int(text[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.03928
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _check_structure(root, nodes, id_map, spec, scorer):
    """检查画布、图层齐全度、绘制顺序和图层是否直接挂在根元素下。"""
    total = sum(STRUCTURE_WEIGHTS.values())

    expected_box = spec["viewBox"]
    parts = re.split(r"[\s,]+", str(root.get("viewBox", "")).strip())
    view_box_ok = len(parts) == 4
    if view_box_ok:
        try:
            view_box_ok = all(
                abs(float(parts[index]) - float(expected_box[index])) <= 0.5
                for index in range(4)
            )
        except ValueError:
            view_box_ok = False
    if not view_box_ok:
        scorer.deduct_ratio(
            "structure", "view_box", STRUCTURE_WEIGHTS["view_box"], total, 0,
            "viewBox 必须精确为 0 0 1200 800，实际为 "
            + (str(root.get("viewBox")) if root.get("viewBox") else "缺失"),
        )

    expected_layers = spec["layers"]
    found_layers = [layer for layer in expected_layers if layer in id_map]
    missing_layers = [layer for layer in expected_layers if layer not in id_map]
    if missing_layers:
        scorer.deduct_ratio(
            "structure", "layers_present", STRUCTURE_WEIGHTS["layers_present"], total,
            len(found_layers) / len(expected_layers),
            "缺少图层：" + "、".join(missing_layers),
        )

    direct_ids = {
        element.get("id")
        for element in list(root)
        if _local_name(element.tag) == "g"
    }
    actual_order = [
        element.get("id")
        for element in list(root)
        if _local_name(element.tag) == "g" and element.get("id") in expected_layers
    ]
    order_positions = [expected_layers.index(layer) for layer in actual_order]
    if order_positions:
        kept = _longest_increasing_subsequence(order_positions)
        if kept < len(order_positions):
            scorer.deduct_ratio(
                "structure", "layers_order", STRUCTURE_WEIGHTS["layers_order"], total,
                kept / len(order_positions),
                "图层顺序错误，实际顺序为：" + " → ".join(actual_order),
            )
    elif not missing_layers:
        scorer.deduct_ratio(
            "structure", "layers_order", STRUCTURE_WEIGHTS["layers_order"], total, 0,
            "没有找到按规范顺序排列的图层",
        )

    nested_layers = [
        layer for layer in expected_layers
        if layer in id_map and layer not in direct_ids
    ]
    if nested_layers:
        scorer.deduct_ratio(
            "structure", "layers_direct", STRUCTURE_WEIGHTS["layers_direct"], total,
            1 - len(nested_layers) / len(expected_layers),
            "以下图层不是根元素的直接子元素：" + "、".join(nested_layers),
        )


def _check_elements(nodes, id_map, spec, scorer):
    """按题卡要求核对关键元素和数量，缺失按比例扣分。"""
    total = sum(ELEMENT_WEIGHTS.values())

    def expect_count(key, expected, found, label, pattern=None):
        """数量类检查：found/expected 作为完成比例。"""
        if found >= expected:
            return
        scorer.deduct_ratio(
            "elements", key, ELEMENT_WEIGHTS[key], total,
            min(1.0, found / expected),
            f"{label} 需要 {expected} 个，实际 {found} 个",
        )

    expect_count("sun", 1, 1 if "sun" in id_map else 0, "太阳 sun")
    expect_count("clouds", 3, _unique_ids(nodes, r"cloud-\d+"), "云朵 cloud-N")
    expect_count("sailboat", 1, 1 if "sailboat" in id_map else 0, "帆船 sailboat")
    expect_count(
        "lighthouse", 1, 1 if "lighthouse" in id_map else 0, "灯塔 lighthouse",
    )
    expect_count(
        "lighthouse_stripes", 3,
        _unique_ids(nodes, r"lighthouse-stripe-\d+"), "灯塔条纹 lighthouse-stripe-N",
    )
    expect_count(
        "wheels", 2,
        sum(1 for wheel in ("wheel-front", "wheel-rear") if wheel in id_map),
        "车轮 wheel-front / wheel-rear",
    )
    expect_count(
        "spokes_front", 12,
        _unique_ids(nodes, r"spoke-front-\d+"), "前轮辐条 spoke-front-N",
    )
    expect_count(
        "spokes_rear", 12,
        _unique_ids(nodes, r"spoke-rear-\d+"), "后轮辐条 spoke-rear-N",
    )
    expect_count("hud_text", 1, 1 if "hud-text" in id_map else 0, "文字 hud-text")
    expect_count("grass", 5, _unique_ids(nodes, r"grass-\d+"), "草丛 grass-N")


def _check_geometry(id_map, spec, scorer):
    """核对题面给出的精确坐标、尺寸和构图关系。"""
    geometry = spec["geometry"]
    tolerance = float(geometry.get("tolerance", 2))
    total = sum(GEOMETRY_WEIGHTS.values())

    def numeric(section_key, code, element, attribute, expected, weight, label):
        """单项数值比对，失败时按该项权重扣分。"""
        actual = _number(element, attribute)
        if _close(actual, expected, tolerance):
            return
        scorer.deduct_ratio(
            "geometry", f"{section_key}_{code}", weight, total, 0,
            f"{label}应为 {_fmt(expected)}，实际为 "
            + (f"{_fmt(actual)}" if actual is not None else "缺失或无法解析"),
        )

    sun = id_map.get("sun")
    sun_config = geometry["sun"]
    numeric("sun", "cx", sun, "cx", sun_config["cx"], 1, "太阳 sun 的 cx ")
    numeric("sun", "cy", sun, "cy", sun_config["cy"], 1, "太阳 sun 的 cy ")
    numeric("sun", "r", sun, "r", sun_config["r"], 1, "太阳 sun 的半径 r ")

    cloud_weight = GEOMETRY_WEIGHTS["clouds"] / len(geometry["clouds"])
    for cloud_id, bounds in geometry["clouds"].items():
        cloud = id_map.get(cloud_id)
        ellipses = [
            element for element in cloud.iter()
            if _local_name(element.tag) == "ellipse"
        ] if cloud is not None else []
        centers = [
            (_number(element, "cx"), _number(element, "cy"))
            for element in ellipses
        ]
        centers = [item for item in centers if item[0] is not None and item[1] is not None]
        if centers:
            average_x = sum(item[0] for item in centers) / len(centers)
            average_y = sum(item[1] for item in centers) / len(centers)
            if (bounds["min_cx"] <= average_x <= bounds["max_cx"]
                    and bounds["min_cy"] <= average_y <= bounds["max_cy"]):
                continue
            detail = f"实际平均中心 ({_fmt(round(average_x, 1))}, {_fmt(round(average_y, 1))})"
        else:
            detail = "缺少 ellipse 或坐标无法解析"
        scorer.deduct_ratio(
            "geometry", f"cloud_{cloud_id}", cloud_weight, total, 0,
            f"{cloud_id} 应位于 x {bounds['min_cx']}–{bounds['max_cx']}、"
            f"y {bounds['min_cy']}–{bounds['max_cy']}，{detail}",
        )

    numeric("sea", "y", id_map.get("sea"), "y", geometry["sea"]["y"], 1, "海面 sea 的 y ")
    numeric("sea", "height", id_map.get("sea"), "height", geometry["sea"]["height"], 1,
            "海面 sea 的 height ")
    numeric("road", "y", id_map.get("road"), "y", geometry["road"]["y"], 1, "公路 road 的 y ")
    numeric("road", "height", id_map.get("road"), "height", geometry["road"]["height"], 1,
            "公路 road 的 height ")

    wheels = geometry["wheels"]
    front = id_map.get("wheel-front")
    rear = id_map.get("wheel-rear")
    numeric("wheel_front", "cy", front, "cy", wheels["front"]["cy"], 1, "前轮 wheel-front 的 cy ")
    numeric("wheel_rear", "cy", rear, "cy", wheels["rear"]["cy"], 1, "后轮 wheel-rear 的 cy ")
    numeric("wheel_front", "r", front, "r", wheels["front"]["r"], 0.5, "前轮 wheel-front 的 r ")
    numeric("wheel_rear", "r", rear, "r", wheels["rear"]["r"], 0.5, "后轮 wheel-rear 的 r ")
    front_cx = _number(front, "cx")
    rear_cx = _number(rear, "cx")
    if front_cx is None or rear_cx is None:
        scorer.deduct_ratio(
            "geometry", "wheel_gap", GEOMETRY_WEIGHTS["wheels_gap"], total, 0,
            "两轮 cx 缺失，无法核对 300 的轮距",
        )
    elif not _close(front_cx - rear_cx, wheels["gap"], tolerance):
        scorer.deduct_ratio(
            "geometry", "wheel_gap", GEOMETRY_WEIGHTS["wheels_gap"], total, 0,
            f"前后轮距应为 {wheels['gap']}，实际为 {_fmt(round(front_cx - rear_cx, 1))}",
        )

    body = id_map.get("pelican-body")
    body_config = geometry["body"]
    numeric("body", "cx", body, "cx", body_config["cx"], 1, "鹈鹕身体 pelican-body 的 cx ")
    numeric("body", "cy", body, "cy", body_config["cy"], 1, "鹈鹕身体 pelican-body 的 cy ")
    numeric("body", "rx", body, "rx", body_config["rx"], 1, "鹈鹕身体 pelican-body 的 rx ")
    numeric("body", "ry", body, "ry", body_config["ry"], 1, "鹈鹕身体 pelican-body 的 ry ")

    beak = id_map.get("pelican-beak")
    beak_range = _absolute_path_x_range(beak)
    beak_config = geometry["beak"]
    if beak_range is None:
        scorer.deduct_ratio(
            "geometry", "beak_range", GEOMETRY_WEIGHTS["beak"], total, 0,
            "长嘴 pelican-beak 缺少可解析的绝对坐标路径",
        )
    else:
        min_x, max_x = beak_range
        if max_x < beak_config["tip_min_x"]:
            scorer.deduct_ratio(
                "geometry", "beak_tip", 2, total, 0,
                f"嘴尖最右端应达到 x ≥ {beak_config['tip_min_x']}，实际为 {_fmt(round(max_x, 1))}",
            )
        if min_x > beak_config["base_max_x"]:
            scorer.deduct_ratio(
                "geometry", "beak_base", 2, total, 0,
                f"长嘴起点应靠近头部 x ≤ {beak_config['base_max_x']}，实际最左端为 {_fmt(round(min_x, 1))}",
            )

    scarf = id_map.get("scarf")
    scarf_range = _absolute_path_x_range(scarf)
    if scarf_range is None:
        scorer.deduct_ratio(
            "geometry", "scarf_range", GEOMETRY_WEIGHTS["scarf"], total, 0,
            "围巾 scarf 缺少可解析的绝对坐标路径",
        )
    elif scarf_range[0] > geometry["scarf"]["tail_max_x"]:
        scorer.deduct_ratio(
            "geometry", "scarf_tail", GEOMETRY_WEIGHTS["scarf"], total, 0,
            f"围巾尾部最左端应达到 x ≤ {geometry['scarf']['tail_max_x']}，"
            f"实际为 {_fmt(round(scarf_range[0], 1))}",
        )

    # 辐条远端必须落在轮缘圆周上，需要模型自己换算坐标，而不是只数数量。
    spokes_config = geometry["spokes"]
    for prefix, center_id, label in (
        ("spoke-front", "wheel-front", "前轮"),
        ("spoke-rear", "wheel-rear", "后轮"),
    ):
        center = id_map.get(center_id)
        cx = _number(center, "cx")
        cy = _number(center, "cy")
        if cx is None or cy is None:
            scorer.deduct_ratio(
                "geometry", f"{prefix}_radius", 1, total, 0,
                f"{label}轮心坐标缺失，无法核对辐条是否落在轮缘上",
            )
            continue
        valid = sum(
            1
            for index in range(1, 13)
            if _line_end_on_circle(
                id_map.get(f"{prefix}-{index}"), cx, cy,
                float(spokes_config["radius"]), float(spokes_config["tolerance"]),
            )
        )
        if valid < 12:
            scorer.deduct_ratio(
                "geometry", f"{prefix}_radius", 1, total, valid / 12,
                f"{label}辐条中有 {12 - valid} 根端点未落在半径 "
                f"{_fmt(spokes_config['radius'])} 的圆周上"
                f"（容差 ±{_fmt(spokes_config['tolerance'])}）",
            )


def _check_palette(id_map, spec, scorer):
    """逐个核对 12 个指定颜色是否用在指定元素上。"""
    palette = spec["palette"]
    if not palette:
        return
    points_per_color = scorer.weights["palette"] / len(palette)
    for item in palette:
        element = id_map.get(item["element"])
        if element is None:
            scorer.deduct(
                "palette", f"palette_{item['element']}", points_per_color,
                f"{item['label']} {item['color']} 的绑定元素 {item['element']} 不存在",
            )
            continue
        if _has_color(element, item["color"]):
            continue
        actual = element.get("fill") or element.get("stroke") or "未设置"
        scorer.deduct(
            "palette", f"palette_{item['element']}", points_per_color,
            f"{item['element']} 应使用 {item['color']}（{item['label']}），实际为 {actual}",
        )


def _check_text(id_map, spec, scorer):
    """核对 HUD 文字的内容、字号和左上角坐标。"""
    config = spec["text"]
    text = id_map.get(config["element"])
    total = sum(TEXT_WEIGHTS.values())
    if text is None:
        scorer.deduct_ratio(
            "text", "content", TEXT_WEIGHTS["content"], total, 0,
            f"找不到文字元素 {config['element']}",
        )
        scorer.deduct_ratio(
            "text", "font_size", TEXT_WEIGHTS["font_size"], total, 0,
            "文字元素缺失，字号无法核对",
        )
        scorer.deduct_ratio(
            "text", "position", TEXT_WEIGHTS["position"], total, 0,
            "文字元素缺失，坐标无法核对",
        )
        return

    content = re.sub(r"\s+", " ", "".join(text.itertext())).strip()
    if content != config["content"]:
        scorer.deduct_ratio(
            "text", "content", TEXT_WEIGHTS["content"], total, 0,
            f"文字内容应为 {config['content']!r}，实际为 {content!r}",
        )

    font_size = _number(text, "font-size")
    if not _close(font_size, config["font_size"], 2):
        scorer.deduct_ratio(
            "text", "font_size", TEXT_WEIGHTS["font_size"], total, 0,
            f"font-size 应为 {config['font_size']}，实际为 "
            + (f"{_fmt(font_size)}" if font_size is not None else "缺失"),
        )

    x_ok = _close(_number(text, "x"), config["x"], 2)
    y_ok = _close(_number(text, "y"), config["y"], 2)
    if not (x_ok and y_ok):
        scorer.deduct_ratio(
            "text", "position", TEXT_WEIGHTS["position"], total, 0,
            f"文字坐标应为 x={config['x']}, y={config['y']}，实际为 "
            f"x={_fmt(_number(text, 'x'))}, y={_fmt(_number(text, 'y'))}",
        )


def _check_animations(id_map, spec, scorer):
    """核对 5 个 SMIL 动画的 id、标签类型、目标属性和时长。"""
    animations = spec["animations"]
    if not animations:
        return
    item_total = sum(ANIMATION_ITEM_WEIGHTS.values())
    for item in animations:
        element = id_map.get(item["id"])
        if element is None:
            scorer.deduct(
                "animation", f"anim_{item['id']}", item_total,
                f"缺少动画 {item['id']}",
            )
            continue

        kind_ok = _local_name(element.tag) == item["kind"]
        attribute_ok = (element.get("attributeName") or "").strip() == item["attributeName"]
        if not (kind_ok and attribute_ok):
            scorer.deduct(
                "animation", f"anim_{item['id']}_kind", ANIMATION_ITEM_WEIGHTS["kind"],
                f"{item['id']} 应为 <{item['kind']} attributeName=\"{item['attributeName']}\">，"
                f"实际为 <{_local_name(element.tag)} attributeName=\"{element.get('attributeName', '')}\">",
            )

        duration_ok = _normalize_duration(element.get("dur")) == item["dur"]
        type_ok = not item.get("type") or (element.get("type") or "").strip() == item["type"]
        if not (duration_ok and type_ok):
            detail = []
            if not duration_ok:
                detail.append(f"dur 应为 {item['dur']}，实际为 {element.get('dur', '缺失')}")
            if not type_ok:
                detail.append(f"type 应为 {item['type']}，实际为 {element.get('type', '缺失')}")
            scorer.deduct(
                "animation", f"anim_{item['id']}_timing",
                ANIMATION_ITEM_WEIGHTS["timing"],
                f"{item['id']} 动画参数错误：" + "；".join(detail),
            )


def _normalize_duration(value):
    """去掉时长中的空格并统一小写，兼容模型写成 6 s 的情况。"""
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).lower()


def _check_details(id_map, spec, scorer):
    """核对提升画面精美程度的细节元素与连接关系。

    除了检查元素是否存在，还验证脖子是否真的接住头和身体、
    云朵是否带阴影、HUD 底框是否覆盖文字并与文字形成足够对比。
    """
    config = spec.get("detail", {})
    total = sum(DETAIL_WEIGHTS.values())

    def require(key, element_id, weight, label):
        """要求指定 id 存在；缺失时按明细权重扣分。"""
        if element_id in id_map:
            return True
        scorer.deduct_ratio(
            "detail", key, weight, total, 0,
            f"缺少{label}（id={element_id}）",
        )
        return False

    require("hub_front", "hub-front", 0.5, "前轮轮毂")
    require("hub_rear", "hub-rear", 0.5, "后轮轮毂")
    require("rim_front", "wheel-front-inner", 0.5, "前轮内圈")
    require("rim_rear", "wheel-rear-inner", 0.5, "后轮内圈")
    require("chainring", "chainring", 0.75, "牙盘")
    require("chain", "chain", 0.75, "链条")
    require("pedal_front", "pedal-front", 0.25, "前踏板")
    require("pedal_rear", "pedal-rear", 0.25, "后踏板")
    require("handlebar", "handlebar", 0.5, "车把")
    require("lamp", "lamp", 0.5, "车灯")
    require("scarf_knot", "scarf-knot", 0.5, "围巾结")
    require("lighthouse_roof", "lighthouse-roof", 0.5, "灯塔屋顶")
    require("lighthouse_window", "lighthouse-window", 0.5, "灯塔窗户")
    require("eye_ring", "pelican-eye-ring", 0.5, "眼周")
    require("feather_1", "feather-1", 0.5, "第一片羽毛纹理")
    require("feather_2", "feather-2", 0.5, "第二片羽毛纹理")

    # 脖子必须同时接触头部和身体，防止“有脖子但飘在空中”。
    if require("neck", "pelican-neck", 1, "脖子"):
        neck_bounds = _element_bounds(id_map.get("pelican-neck"))
        head_bounds = _element_bounds(id_map.get("pelican-head"))
        body_bounds = _element_bounds(id_map.get("pelican-body"))
        if not _boxes_intersect(neck_bounds, head_bounds):
            scorer.deduct_ratio(
                "detail", "neck_head", 0.5, total, 0,
                "脖子 pelican-neck 没有连接到头部 pelican-head",
            )
        if not _boxes_intersect(neck_bounds, body_bounds):
            scorer.deduct_ratio(
                "detail", "neck_body", 0.5, total, 0,
                "脖子 pelican-neck 没有连接到身体 pelican-body",
            )

    # 每朵云至少有一个阴影色椭圆，避免云朵只是三个纯白色块。
    shadow_color = config.get("cloud_shadow_color", "#E8E0CC")
    cloud_weight = DETAIL_WEIGHTS["cloud_shadows"] / 3
    for cloud_id in ("cloud-1", "cloud-2", "cloud-3"):
        cloud = id_map.get(cloud_id)
        has_shadow = bool(cloud is not None) and any(
            _local_name(element.tag) == "ellipse" and _has_color(element, shadow_color)
            for element in cloud.iter()
        )
        if not has_shadow:
            scorer.deduct_ratio(
                "detail", f"{cloud_id}_shadow", cloud_weight, total, 0,
                f"{cloud_id} 缺少 {shadow_color} 阴影椭圆",
            )

    # HUD 底框要覆盖两行文字，并与文字形成足够亮度差。
    if require("hud_panel", "hud-panel", 0.5, "HUD 深色底框"):
        panel = id_map["hud-panel"]
        panel_bounds = _element_bounds(panel)
        covered = (
            panel_bounds is not None
            and panel_bounds[0] <= 60 and panel_bounds[1] <= 44
            and panel_bounds[2] >= 240 and panel_bounds[3] >= 104
        )
        text = id_map.get("hud-text")
        text_color = str(text.get("fill", "")).strip() if text is not None else ""
        panel_color = str(panel.get("fill", "")).strip()
        text_luminance = _relative_luminance(text_color)
        panel_luminance = _relative_luminance(panel_color)
        contrast_ok = (
            text_luminance is not None and panel_luminance is not None
            and abs(text_luminance - panel_luminance)
            >= float(config.get("hud_min_contrast", 0.4))
        )
        if not (covered and contrast_ok):
            reasons = []
            if not covered:
                reasons.append("底框没有覆盖两行文字区域")
            if not contrast_ok:
                reasons.append(
                    f"文字颜色 {text_color or '缺失'} 与底框颜色 {panel_color or '缺失'} 的亮度差不足"
                )
            scorer.deduct_ratio(
                "detail", "hud_panel_contrast", 0.5, total, 0,
                "；".join(reasons),
            )


def _check_richness(root, spec, scorer):
    """用元素密度衡量画面丰富度，抑制只按清单交作业的简版构图。

    绘图元素和路径节点越多，通常意味着轮廓、纹理和装饰越完整。
    阈值写进题卡，参考 SVG 需要明显高于阈值，避免正常模型偶尔波动被误伤。
    """
    config = spec.get("richness", {})
    total = sum(DETAIL_WEIGHTS.values())
    drawing_count = sum(
        1 for element in root.iter() if _local_name(element.tag) in DRAWING_TAGS
    )
    path_nodes = sum(
        len(re.findall(r"[MLQCS]", element.get("d") or ""))
        for element in root.iter()
        if _local_name(element.tag) == "path"
    )
    minimum_elements = int(config.get("min_drawing_elements", 170))
    if drawing_count < minimum_elements:
        scorer.deduct_ratio(
            "detail", "drawing_elements", DETAIL_WEIGHTS["drawing_elements"], total,
            drawing_count / minimum_elements,
            f"绘图元素只有 {drawing_count} 个，少于要求的 {minimum_elements} 个，画面偏简",
        )
    minimum_nodes = int(config.get("min_path_nodes", 380))
    if path_nodes < minimum_nodes:
        scorer.deduct_ratio(
            "detail", "path_nodes", DETAIL_WEIGHTS["path_nodes"], total,
            path_nodes / minimum_nodes,
            f"路径节点只有 {path_nodes} 个，少于要求的 {minimum_nodes} 个，轮廓细节不足",
        )


def _longest_increasing_subsequence(values):
    """返回最长递增子序列长度，用于计算图层顺序的保留比例。"""
    if not values:
        return 0
    best = [1] * len(values)
    for index, value in enumerate(values):
        for previous in range(index):
            if values[previous] < value:
                best[index] = max(best[index], best[previous] + 1)
    return max(best)
