# -*- coding: utf-8 -*-
"""规范 SVG 判分器测试：参考作品满分，降级作品按预期扣分。"""

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from relay_monitor import svg_artwork, svg_eval

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "resources" / "svg_specs" / "pelican_ride_v2.json"
REFERENCE_PATH = ROOT / "tests" / "fixtures" / "svg" / "reference_pelican_ride.svg"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NAMESPACE)


def _reference_text():
    """读取参考 SVG 源码。"""
    return REFERENCE_PATH.read_text(encoding="utf-8")


def _reference_root():
    """把参考 SVG 解析成可修改的元素树。"""
    return ET.fromstring(_reference_text())


def _serialize(root):
    """把元素树序列化回 SVG 文本。"""
    return ET.tostring(root, encoding="unicode")


def _find(root, element_id):
    """按 id 查找元素，找不到返回 None。"""
    return next(
        (element for element in root.iter() if element.get("id") == element_id),
        None,
    )


def _parents(root):
    """建立子元素到父元素的映射，便于删除节点。"""
    return {child: parent for parent in root.iter() for child in parent}


def _drop_elements(root, ids):
    """删除指定 id 的元素，模拟模型漏画。"""
    parents = _parents(root)
    for element in list(root.iter()):
        if element.get("id") in ids and element in parents:
            parents[element].remove(element)


class SvgEvalTest(unittest.TestCase):
    """验证判分器对合格与降级 SVG 的区分能力。"""

    @classmethod
    def setUpClass(cls):
        cls.spec = svg_eval.load_spec(SPEC_PATH)
        cls.reference_text = _reference_text()

    def evaluate(self, svg_text):
        """按统一题卡判分。"""
        return svg_eval.evaluate_svg(svg_text, self.spec)

    def test_reference_scores_full_marks(self):
        """参考 SVG 必须拿满分，保证题面与判分参数一致。"""
        result = self.evaluate(self.reference_text)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["deductions"], [])

    def test_reference_stays_full_marks_after_sanitize(self):
        """真实链路会先净化 SVG，净化后仍应保持满分。"""
        sanitized = svg_artwork.sanitize_svg(self.reference_text)
        result = self.evaluate(sanitized)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["score"], 100.0)

    def test_prompt_is_a_single_string(self):
        """题卡提示词数组应拼接成可直接发送的字符串。"""
        self.assertIn("viewBox", self.spec["prompt"])
        self.assertIn("SLOW RIDE", self.spec["prompt"])
        self.assertIn("\n", self.spec["prompt"])

    def test_missing_layers_lose_structure_and_text_points(self):
        """漏画三个图层会同时影响结构、元素、几何、配色和文字分。"""
        root = _reference_root()
        _drop_elements(root, {"layer-sea", "layer-foreground", "layer-hud"})
        result = self.evaluate(_serialize(root))
        self.assertEqual(result["status"], "ok")
        self.assertLess(result["score"], 90)
        self.assertGreater(result["score"], 75)
        section_scores = {item["key"]: item["score"] for item in result["sections"]}
        self.assertLess(section_scores["structure"], 20)
        self.assertEqual(section_scores["text"], 0)

    def test_wrong_layer_order_is_penalized(self):
        """图层顺序颠倒应扣顺序分，但不影响其它章节。"""
        root = _reference_root()
        bicycle = _find(root, "layer-bicycle")
        pelican = _find(root, "layer-pelican")
        pelican_index = list(root).index(pelican)
        # 先把自行车图层移到鹈鹕之后，形成相邻图层顺序颠倒。
        root.remove(bicycle)
        root.insert(pelican_index, bicycle)
        result = self.evaluate(_serialize(root))
        self.assertLess(result["score"], 100)
        self.assertGreater(result["score"], 97)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("图层顺序", messages)

    def test_missing_spokes_are_counted_proportionally(self):
        """辐条缺失按比例扣分，缺 10 根时只扣对应比例。"""
        root = _reference_root()
        _drop_elements(root, {
            *(f"spoke-front-{index}" for index in range(3, 13)),
            *(f"spoke-rear-{index}" for index in range(3, 13)),
        })
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 93)
        self.assertLess(result["score"], 98)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("前轮辐条", messages)
        self.assertIn("后轮辐条", messages)

    def test_short_beak_is_penalized(self):
        """长嘴画得不够长会被几何检查识别。"""
        root = _reference_root()
        _find(root, "pelican-beak").set(
            "d", "M735 368 L800 380 L795 402 L740 396 Z",
        )
        result = self.evaluate(_serialize(root))
        self.assertLess(result["score"], 100)
        self.assertGreater(result["score"], 96)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("嘴尖", messages)

    def test_wrong_colors_are_penalized(self):
        """指定元素用错颜色会扣配色分。"""
        root = _reference_root()
        _find(root, "sun").set("fill", "#FF0000")
        _find(root, "sky").set("fill", "#FFFFFF")
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 95)
        self.assertLess(result["score"], 99)
        palette = next(item for item in result["sections"] if item["key"] == "palette")
        self.assertEqual(len(palette["deductions"]), 2)

    def test_broken_animations_are_penalized(self):
        """动画缺失或时长错误会扣动画分。"""
        root = _reference_root()
        _drop_elements(root, {"anim-scarf", "anim-body-bob"})
        _find(root, "anim-clouds").set("dur", "10s")
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 91)
        self.assertLess(result["score"], 97)
        animation = next(item for item in result["sections"] if item["key"] == "animation")
        self.assertEqual(len(animation["deductions"]), 3)

    def test_wrong_text_is_penalized(self):
        """文字内容错误会扣掉文字内容分。"""
        root = _reference_root()
        hud = _find(root, "hud-text")
        hud.text = "FAST RIDE"
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 93)
        self.assertLess(result["score"], 99)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("文字内容", messages)

    def test_shifted_geometry_is_penalized(self):
        """关键元素坐标漂移会扣几何分。"""
        root = _reference_root()
        _find(root, "sun").set("cx", "700")
        _find(root, "wheel-front").set("cy", "700")
        _find(root, "pelican-body").set("cx", "420")
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 92)
        self.assertLess(result["score"], 99)
        geometry = next(item for item in result["sections"] if item["key"] == "geometry")
        self.assertGreaterEqual(len(geometry["deductions"]), 3)

    def test_missing_details_are_penalized(self):
        """漏画轮毂、牙盘、脖子等精美细节会集中扣画面细节分。"""
        root = _reference_root()
        _drop_elements(root, {
            "hub-front", "hub-rear", "chainring", "chain", "pedal-front", "pedal-rear",
            "handlebar", "lamp", "pelican-neck", "scarf-knot",
            "lighthouse-roof", "lighthouse-window", "pelican-eye-ring",
        })
        result = self.evaluate(_serialize(root))
        detail = next(item for item in result["sections"] if item["key"] == "detail")
        self.assertLessEqual(detail["score"], 12)
        self.assertGreater(result["score"], 85)
        self.assertLess(result["score"], 95)

    def test_cloud_without_shadow_is_penalized(self):
        """云朵没有阴影椭圆会扣该云朵的细节分。"""
        root = _reference_root()
        cloud = _find(root, "cloud-1")
        for ellipse in cloud.iter():
            if ellipse.get("fill") == "#E8E0CC":
                ellipse.set("fill", "#FFFFFF")
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 96)
        self.assertLess(result["score"], 100)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("阴影", messages)

    def test_hud_panel_without_contrast_is_penalized(self):
        """HUD 底框与文字颜色接近时扣对比度分。"""
        root = _reference_root()
        _find(root, "hud-panel").set("fill", "#FDF6E3")
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 98)
        self.assertLess(result["score"], 100)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("亮度差", messages)

    def test_off_circle_spokes_are_penalized(self):
        """辐条端点没有落在轮缘圆周上会被识别。"""
        root = _reference_root()
        _find(root, "spoke-front-1").set("x2", "760")
        _find(root, "spoke-front-1").set("y2", "530")
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 98)
        self.assertLess(result["score"], 100)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("圆周", messages)

    def test_detached_neck_is_penalized(self):
        """脖子没有接住头和身体会被连接关系检查识别。"""
        root = _reference_root()
        _find(root, "pelican-neck").set("d", "M300 300 Q320 280 340 300 L330 330 Z")
        result = self.evaluate(_serialize(root))
        self.assertGreater(result["score"], 96)
        self.assertLess(result["score"], 100)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("没有连接到头部", messages)
        self.assertIn("没有连接到身体", messages)

    def test_low_density_is_penalized(self):
        """元素密度低于题卡阈值时按比例扣画面丰富度分。"""
        spec = svg_eval.load_spec(SPEC_PATH)
        spec["richness"] = {"min_drawing_elements": 400, "min_path_nodes": 900}
        result = svg_eval.evaluate_svg(self.reference_text, spec)
        messages = " ".join(item["message"] for item in result["deductions"])
        self.assertIn("绘图元素只有", messages)
        self.assertIn("路径节点只有", messages)
        self.assertGreater(result["score"], 85)
        self.assertLess(result["score"], 100)

    def test_truncated_svg_returns_no_score(self):
        """被截断的 SVG 不能当作 0 分作品，应返回 score=None。"""
        result = self.evaluate('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 800"><g id="layer-sky">')
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["score"])
        self.assertIn("截断", result["error"])

    def test_empty_svg_returns_no_score(self):
        """空内容同样属于无法判分。"""
        result = self.evaluate("")
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["score"])


if __name__ == "__main__":
    unittest.main()
