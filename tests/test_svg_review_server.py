# -*- coding: utf-8 -*-
"""验证服务端只发送成品图，并在渲染失败时跳过评分。"""

import json
import unittest
from unittest.mock import MagicMock, patch

import server
from relay_monitor import svg_review


RELAY = {
    "name": "review", "label": "评分渠道", "protocol": "openai",
    "baseUrl": "https://example.com/v1", "apiKey": "test-key", "model": "review-model",
}
SETTINGS = {
    "svg_review_enabled": True, "svg_review_relay": "review",
    "svg_review_model": "", "svg_review_prompt": svg_review.DEFAULT_REVIEW_PROMPT,
    "svg_review_reasoning_effort": "low", "svg_review_retry_count": 0, "api_timeout_minutes": 1,
}


class SVGReviewServerTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.get_connection = patch.object(server, "get_con", return_value=self.connection)
        self.get_connection.start()
        self.addCleanup(self.get_connection.stop)
        load_relays = patch.object(server.monitor, "load_relays", return_value=[RELAY])
        self.load_relays = load_relays.start()
        self.addCleanup(load_relays.stop)

    def test_image_review_is_saved_with_current_rubric_metadata(self):
        result = {"score": 91.0, "review_basis": "rendered-image", "review_attempts": 1}
        with patch.object(server.svg_review, "render_svg_preview", return_value=b"png bytes"), \
                patch.object(server.svg_review, "review_svg", return_value=result) as review:
            score, detail = server._review_svg(55, "<svg/>", SETTINGS)

        self.assertEqual(score, 91.0)
        self.assertEqual(review.call_args.args, (RELAY,))
        self.assertEqual(review.call_args.kwargs["preview_image"], b"png bytes")
        self.assertEqual(review.call_args.kwargs["retry_count"], 0)
        saved = json.loads(detail)
        self.assertEqual(saved["rubric_version"], svg_review.REVIEW_RUBRIC_VERSION)
        self.assertEqual(saved["mechanism"], "ai-rendered-image-review")
        self.assertEqual(saved["review_attempts"], 1)
        self.assertFalse(server._score_metadata(detail)["score_is_legacy"])

    def test_render_failure_skips_review_and_records_unscored_state(self):
        with patch.object(server.svg_review, "render_svg_preview", side_effect=svg_review.SVGReviewError("渲染错误")), \
                patch.object(server.svg_review, "review_svg") as review:
            score, detail = server._review_svg(56, "<svg/>", SETTINGS)

        self.assertIsNone(score)
        review.assert_not_called()
        saved = json.loads(detail)
        self.assertEqual(saved["review_basis"], "render-failed")
        self.assertEqual(saved["error"], "成品图渲染失败，未进行评分")
        self.assertEqual(server._score_metadata(detail)["score_label"], "评分图渲染失败")
        self.assertEqual(server._score_comment(detail), "渲染错误")

    def test_configured_fallback_is_reported_as_the_actual_scoring_channel(self):
        backup = {**RELAY, "name": "backup", "label": "备用评分"}
        self.load_relays.return_value = [RELAY, backup]
        result = {"score": 78, "review_basis": "rendered-image", "review_attempts": 2,
                  "primary_relay": "review", "relay": "backup", "relay_label": "备用评分"}
        with patch.object(server.svg_review, "render_svg_preview", return_value=b"png bytes"), \
                patch.object(server.svg_review, "review_svg", return_value=result) as review:
            score, detail = server._review_svg(57, "<svg/>", {**SETTINGS, "svg_review_fallback_relay": "backup"})

        self.assertEqual(score, 78)
        self.assertEqual(review.call_args.args, (RELAY,))
        self.assertEqual(review.call_args.kwargs["fallback_relay"], backup)
        self.assertEqual(server._score_metadata(detail)["score_fallback_relay"], "备用评分")

    def test_previous_source_fallback_history_is_labeled_as_legacy(self):
        metadata = server._score_metadata('{"review_basis":"source-fallback"}')
        self.assertEqual(metadata["score_label"], "旧评分口径")
        self.assertTrue(metadata["score_is_legacy"])

    def test_unversioned_history_is_marked_as_legacy(self):
        self.assertTrue(server._score_metadata('{"score":82}')["score_is_legacy"])

    def test_previous_image_rubric_history_is_labeled_as_legacy(self):
        for version in ("4", "5", "6"):
            with self.subTest(version=version):
                metadata = server._score_metadata(json.dumps({
                    "review_basis": "rendered-image", "rubric_version": version,
                }))
                self.assertEqual(metadata["score_label"], "旧评分口径")
                self.assertTrue(metadata["score_is_legacy"])

    def test_custom_image_rubric_is_distinguished_from_default(self):
        metadata = server._score_metadata(
            '{"review_basis":"rendered-image","rubric_version":"custom"}'
        )
        self.assertEqual(metadata["score_label"], "自定义评分")
        self.assertTrue(metadata["score_is_legacy"])


if __name__ == "__main__":
    unittest.main()
