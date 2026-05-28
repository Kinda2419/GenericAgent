
import json
import os
import queue
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontends")))

import agentmain
import agent_loop
import frontends.fsapp as fsapp


class FakeBackend:
    extra_sys_prompt = ""


class FakeLLMClient:
    backend = FakeBackend()


class FakeHandler:
    def __init__(self, parent, history, temp_dir):
        self.parent = parent
        self.history_info = history
        self.working = {}
        self.code_stop_signal = []


def failing_runner(*args, **kwargs):
    yield "partial output before failure"
    raise ValueError("simulated backend failure")


class FeishuCardSectionTests(unittest.TestCase):
    def test_unknown_tool_raises_backend_error(self):
        handler = agent_loop.BaseHandler()

        with self.assertRaisesRegex(RuntimeError, "未知工具: __force_backend_error_for_e2e__"):
            list(handler.dispatch("__force_backend_error_for_e2e__", {}, response=None))

    def test_agent_backend_exception_emits_error_not_done(self):
        agent = agentmain.GeneraticAgent.__new__(agentmain.GeneraticAgent)
        agent.task_queue = queue.Queue()
        agent.task_dir = None
        agent.history = []
        agent.handler = None
        agent.is_running = False
        agent.stop_sig = False
        agent.inc_out = False
        agent.verbose = True
        agent.peer_hint = False
        agent.llmclient = FakeLLMClient()

        with mock.patch.object(agentmain, "get_system_prompt", return_value="system"), \
             mock.patch.object(agentmain, "GenericAgentHandler", FakeHandler), \
             mock.patch.object(agentmain, "agent_runner_loop", failing_runner):
            worker = threading.Thread(target=agent.run, daemon=True)
            worker.start()

            result_queue = agent.put_task("trigger failure", source="test")
            seen = []
            for _ in range(5):
                item = result_queue.get(timeout=2)
                seen.append(item)
                if "error" in item or "done" in item:
                    break

        self.assertIn("error", seen[-1])
        self.assertNotIn("done", seen[-1])
        self.assertIn("ValueError: simulated backend failure", seen[-1]["error_info"])

    def test_progress_panels_remain_visible_and_final_output_is_direct_markdown(self):
        card = fsapp._TaskCard("chat-1", "chat_id", reply_to_message_id="root-1")
        card.steps.append(("调用工具file_read, args: {'path': 'x'}", "<search_web>"))
        card.final = "# 卖法建议\n先给结论，再给执行清单。\n\n# 下一步\n改标题、改主图、改套餐。"

        payload = json.loads(card._build())
        elements = payload["body"]["elements"]
        panels = [element for element in elements if element.get("tag") == "collapsible_panel"]
        markdown = "\n".join(element.get("content", "") for element in elements if element.get("tag") == "markdown")

        self.assertEqual(len(panels), 1)
        self.assertIn("file_read", panels[0]["header"]["title"]["content"])
        self.assertIn("<search_web>", panels[0]["elements"][0]["content"])
        self.assertIn("# 卖法建议", markdown)
        self.assertIn("先给结论", markdown)
        self.assertNotIn("file_read", markdown)
        self.assertNotIn("<search_web>", markdown)

    def test_empty_model_output_does_not_mark_done(self):
        card = fsapp._TaskCard("chat-1", "chat_id", reply_to_message_id="root-1")
        card._push = lambda: (True, False)

        card.done("⚠️ 模型输出被截断或为空")

        payload = json.loads(card._build())
        markdown = "\n".join(element.get("content", "") for element in payload["body"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("模型没有返回可发送给用户的最终答案", markdown)
        self.assertNotIn("✅ 已完成", markdown)

    def test_backend_error_output_does_not_mark_done(self):
        card = fsapp._TaskCard("chat-1", "chat_id", reply_to_message_id="root-1")
        card._push = lambda: (True, False)

        card.done("```\nValueError: broken @ agentmain.py:166, run -> `raise e`\n```")

        payload = json.loads(card._build())
        markdown = "\n".join(element.get("content", "") for element in payload["body"]["elements"] if element.get("tag") == "markdown")
        self.assertIn("❌ 执行失败", markdown)
        self.assertIn("**出了什么问题**", markdown)
        self.assertIn("**技术细节**", markdown)
        self.assertIn("ValueError: broken", markdown)
        self.assertNotIn("✅ 已完成", markdown)

    def test_failure_status_keeps_error_details_out_of_header(self):
        card = fsapp._TaskCard("chat-1", "chat_id", reply_to_message_id="root-1")
        card._push = lambda: (True, False)

        card.fail("**出了什么问题**\n\n```plain_text\nRuntimeError: 未知工具\n```\n\n---\n**技术细节**\n\n```plain_text\nRuntimeError: 未知工具: x @ agent_loop.py:28\n```")

        payload = json.loads(card._build())
        elements = payload["body"]["elements"]
        header = elements[0]["content"]
        markdown = "\n".join(element.get("content", "") for element in elements if element.get("tag") == "markdown")

        self.assertEqual(header, "**❌ 执行失败**")
        self.assertNotIn("RuntimeError", header)
        self.assertIn("RuntimeError: 未知工具", markdown)

    def test_task_hook_marks_backend_error_as_failed(self):
        class FakeCard:
            def __init__(self):
                self.done_text = None
                self.failed = None

            def done(self, text):
                self.done_text = text

            def fail(self, msg):
                self.failed = msg

        class FakeResponse:
            content = "Backend Error: RuntimeError: failed @ agentmain.py:166"

        card = FakeCard()
        called = []
        hook = fsapp._make_task_hook(card, done_event=type("Evt", (), {"set": lambda self: None})(), on_final=called.append)

        hook({"exit_reason": {"result": "CURRENT_TASK_DONE"}, "response": FakeResponse()})

        self.assertIn("RuntimeError: failed", card.failed)
        self.assertIsNone(card.done_text)
        self.assertEqual(called, [])

    def test_long_plain_final_output_is_chunked(self):
        sections = fsapp._split_card_sections("???\n\n" + ("x" * 30) + "\n\n???", limit=20)

        self.assertGreaterEqual(len(sections), 2)
        self.assertTrue(all(len(body) <= 20 for _, body in sections))


if __name__ == "__main__":
    unittest.main()
