import tempfile
import unittest
from pathlib import Path

from temp.ga_gstack_controller import ga_gstack_controller as controller


class GstackControllerLearningTests(unittest.TestCase):
    def test_related_learnings_boosts_matching_business_line_only(self):
        with tempfile.TemporaryDirectory() as td:
            old_dir = controller.LEARNINGS_DIR
            controller.LEARNINGS_DIR = Path(td)
            try:
                (Path(td) / "content.md").write_text(
                    "---\n"
                    "title: 内容经验\n"
                    "business_line: content_creation\n"
                    "---\n"
                    "AI 工具 场景\n",
                    encoding="utf-8",
                )
                (Path(td) / "market.md").write_text(
                    "---\n"
                    "title: 调研经验\n"
                    "business_line: market_research\n"
                    "---\n"
                    "AI 工具 场景\n",
                    encoding="utf-8",
                )

                results = controller.related_learnings("AI 工具 场景", ["content_creation"], limit=2)

                self.assertEqual(results[0]["business_line"], "content_creation")
                self.assertGreater(results[0]["score"], results[1]["score"])
            finally:
                controller.LEARNINGS_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
