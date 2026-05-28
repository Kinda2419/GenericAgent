import json
import os
import sys
import unittest
import types

class _Builder:
    def __getattr__(self, name):
        def method(*args, **kwargs):
            return self
        return method
    def build(self):
        return self

class _Req:
    @classmethod
    def builder(cls):
        return _Builder()

class _Client:
    @classmethod
    def builder(cls):
        return _Builder()

lark = types.ModuleType("lark_oapi")
lark.Client = _Client
lark.LogLevel = types.SimpleNamespace(INFO="INFO")
sys.modules["lark_oapi"] = lark
for module_name in ("lark_oapi.api.im.v1", "lark_oapi.api.drive.v1"):
    module = types.ModuleType(module_name)
    for class_name in (
        "CreateMessageRequest", "CreateMessageRequestBody", "PatchMessageRequest", "PatchMessageRequestBody",
        "ReplyMessageRequest", "ReplyMessageRequestBody", "CreateMessageReactionRequest", "CreateMessageReactionRequestBody",
        "Emoji", "CreateImageRequest", "CreateImageRequestBody", "CreateFileRequest", "CreateFileRequestBody",
        "GetMessageResourceRequest",
    ):
        setattr(module, class_name, _Req)
    sys.modules[module_name] = module

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontends")))

import frontends.fsapp as fsapp


class FeishuCardSectionTests(unittest.TestCase):
    def test_short_final_output_is_visible_without_collapsible_sections(self):
        card = fsapp._TaskCard("chat-1", "chat_id", reply_to_message_id="root-1")
        card.final = "one line result"

        payload = json.loads(card._build())
        elements = payload["body"]["elements"]
        panels = [element for element in elements if element.get("tag") == "collapsible_panel"]
        markdown = [element.get("content", "") for element in elements if element.get("tag") == "markdown"]

        self.assertEqual(panels, [])
        self.assertIn("one line result", markdown)

    def test_long_final_output_uses_collapsible_sections(self):
        card = fsapp._TaskCard("chat-1", "chat_id", reply_to_message_id="root-1")
        card.final = "# First\n" + ("a" * 700) + "\n\n# Second\n" + ("b" * 700)

        payload = json.loads(card._build())
        elements = payload["body"]["elements"]
        panels = [element for element in elements if element.get("tag") == "collapsible_panel"]

        self.assertEqual(len(panels), 2)
        self.assertEqual([panel["expanded"] for panel in panels], [True, False])
        self.assertEqual(payload["schema"], "2.0")
        self.assertEqual(payload["header"]["title"]["content"], "GA Long Answer")
        self.assertEqual(payload["header"]["icon"]["token"], "bot_outlined")
        self.assertEqual(panels[0]["header"]["title"]["content"], "✅ First")
        self.assertEqual(panels[0]["header"]["background_color"], "blue-50")
        self.assertEqual(panels[1]["header"]["title"]["content"], "📌 Second")
        self.assertNotIn("# First", panels[0]["elements"][0]["content"])
        self.assertEqual(len([e for e in elements if e.get("tag") == "note"]), 0)
        self.assertTrue(any(e.get("tag") == "markdown" and "Feishu 7.20+" in e.get("content", "") for e in elements))

    def test_tool_turns_are_grouped_without_raw_details(self):
        card = fsapp._TaskCard("chat-1", "chat_id", reply_to_message_id="root-1")
        card.steps = [("read file", "raw detail 1"), ("run check", "raw detail 2")]
        card.turn_no = 2

        running_payload = json.loads(card._build())
        running_panels = [element for element in running_payload["body"]["elements"] if element.get("tag") == "collapsible_panel"]
        self.assertEqual(len(running_panels), 1)
        self.assertTrue(running_panels[0]["expanded"])
        self.assertIn("2 turns", running_panels[0]["header"]["title"]["content"])
        panel_content = running_panels[0]["elements"][0]["content"]
        self.assertIn("Turn 1: read file", panel_content)
        self.assertIn("Turn 2: run check", panel_content)
        self.assertNotIn("raw detail", panel_content)
        self.assertNotIn("Tool Calls", panel_content)
        self.assertNotIn("Output", panel_content)

        card.final = "final answer"
        done_payload = json.loads(card._build())
        done_panels = [element for element in done_payload["body"]["elements"] if element.get("tag") == "collapsible_panel"]
        self.assertEqual(len(done_panels), 1)
        self.assertFalse(done_panels[0]["expanded"])

    def test_long_plain_final_output_is_chunked(self):
        sections = fsapp._split_card_sections("aaa\n\n" + ("x" * 30) + "\n\nbbb", limit=20)

        self.assertGreaterEqual(len(sections), 2)
        self.assertTrue(all(len(body) <= 20 for _, body in sections))

    def test_tool_call_details_are_hidden_by_default(self):
        class Resp:
            thinking = "internal thinking"
            content = "internal output"

        detail = fsapp._build_step_detail(Resp(), [{"tool_name": "file_read", "args": {"path": "x"}}])

        self.assertEqual(detail, "")


if __name__ == "__main__":
    unittest.main()

