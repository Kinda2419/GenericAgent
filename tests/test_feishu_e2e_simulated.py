import json
import os
import io
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontends")))

import frontends.fsapp as fsapp
from agentmain import GenericAgentRuntime


class _Obj:
    pass


def _event(message_id, text, root_id=None, parent_id=None, chat_id="chat-e2e", open_id="user-e2e"):
    data = _Obj()
    data.event = _Obj()
    data.event.message = _Obj()
    data.event.sender = _Obj()
    data.event.sender.sender_id = _Obj()
    data.event.sender.sender_id.open_id = open_id
    msg = data.event.message
    msg.chat_id = chat_id
    msg.message_id = message_id
    msg.message_type = "text"
    msg.content = json.dumps({"text": text})
    msg.root_id = root_id
    msg.parent_id = parent_id
    msg.thread_id = root_id
    return data


class _E2EFakeAgent:
    def __init__(self, records, label):
        self._turn_end_hooks = {}
        self.records = records
        self.label = label
        self.history = []
        self.is_running = False

    def put_task(self, query, source="feishu", images=None):
        import queue

        self.records.append(("put_task", self.label, query, source, tuple(images or [])))
        self.history.append(query)
        display_queue = queue.Queue()
        if "retry-visible" in query:
            display_queue.put({"next": "[LLM Retry] retryable provider error detected", "summary": "LLM retry 1/2"})

        def complete():
            time.sleep(0.02)
            for hook in list(self._turn_end_hooks.values()):
                response = type("Resp", (), {"content": f"done:{self.label}:{query}"})()
                hook({"exit_reason": "done", "response": response})

        threading.Thread(target=complete, daemon=True).start()
        return display_queue

    def abort(self):
        self.records.append(("abort", self.label))

    def get_llm_name(self):
        return "fake"


class _E2ESession:
    def __init__(self, session_id, records, metadata=None):
        self.session_id = session_id
        self.metadata = metadata or {}
        self.agent = _E2EFakeAgent(records, session_id)
        self.updated_at = time.time()

    def put_task(self, query, source="feishu", images=None):
        self.updated_at = time.time()
        return self.agent.put_task(query, source=source, images=images)


class _E2ERuntime:
    def __init__(self, records):
        self.records = records
        self.sessions = {}

    def get_or_create(self, session_id, metadata=None):
        sess = self.sessions.get(session_id)
        if sess is None:
            sess = _E2ESession(session_id, self.records, metadata=metadata)
            self.sessions[session_id] = sess
        elif metadata:
            sess.metadata.update(metadata)
        return sess

    def active_sessions(self):
        return list(self.sessions.values())


class _FakeCard:
    counter = 0
    records = []

    def __init__(self, receive_id, rid_type, reply_to_message_id=None):
        type(self).counter += 1
        self.msg_id = f"om_card_{type(self).counter}"
        self.reply_to_message_id = reply_to_message_id
        self.rid = receive_id
        self.rtype = rid_type
        self.records.append(("card_init", self.msg_id, receive_id, rid_type, reply_to_message_id))

    def start(self):
        self.records.append(("card_start", self.msg_id, self.reply_to_message_id))

    def step(self, summary, detail=""):
        self.records.append(("card_step", self.msg_id, summary))

    def done(self, text):
        self.records.append(("card_done", self.msg_id, text))

    def fail(self, msg):
        self.records.append(("card_fail", self.msg_id, msg))


class FeishuSimulatedE2ETests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {
            "PUBLIC_ACCESS": fsapp.PUBLIC_ACCESS,
            "runtime": fsapp.runtime,
            "session_aliases": fsapp.session_aliases,
            "chat_active_sessions": fsapp.chat_active_sessions,
            "_persisted_sessions": fsapp._persisted_sessions,
            "_persist_feishu_sessions": fsapp._persist_feishu_sessions,
            "_TaskCard": fsapp._TaskCard,
            "_reply_text": fsapp._reply_text,
            "_add_done_reaction": fsapp._add_done_reaction,
            "_send_generated_files": fsapp._send_generated_files,
            "HEALTH_FILE": fsapp.HEALTH_FILE,
            "HEALTH_INTERVAL_SEC": fsapp.HEALTH_INTERVAL_SEC,
        }
        fsapp.HEALTH_FILE = os.path.join(self.tmp.name, "health.json")
        fsapp.HEALTH_INTERVAL_SEC = 60
        fsapp.PUBLIC_ACCESS = True
        fsapp.runtime = _E2ERuntime(self.records)
        fsapp.session_aliases = {}
        fsapp.chat_active_sessions = {}
        fsapp._persisted_sessions = {}
        fsapp._persist_feishu_sessions = lambda: self.records.append(("persist", len(fsapp.runtime.sessions)))
        fsapp._reply_text = lambda message_id, content: self.records.append(("reply_text", message_id, content))
        fsapp._add_done_reaction = lambda message_id: self.records.append(("reaction", message_id))
        fsapp._send_generated_files = lambda *args, **kwargs: self.records.append(("files", args[0]))
        _FakeCard.counter = 0
        _FakeCard.records = self.records
        fsapp._TaskCard = _FakeCard
        fsapp.user_tasks.clear()

    def tearDown(self):
        for name, value in self.old.items():
            setattr(fsapp, name, value)
        fsapp.user_tasks.clear()
        self.tmp.cleanup()

    def test_two_roots_run_as_independent_sessions_and_reply_to_own_threads(self):
        fsapp.handle_message(_event("om_root_a", "调研 Codex", root_id=None))
        fsapp.handle_message(_event("om_root_b", "写小红书选题", root_id=None))
        time.sleep(0.2)

        self.assertIn("chat-e2e:om_root_a", fsapp.runtime.sessions)
        self.assertIn("chat-e2e:om_root_b", fsapp.runtime.sessions)
        self.assertEqual(fsapp.runtime.sessions["chat-e2e:om_root_a"].agent.history, ["调研 Codex"])
        self.assertEqual(fsapp.runtime.sessions["chat-e2e:om_root_b"].agent.history, ["写小红书选题"])
        self.assertIn(("card_start", "om_card_1", "om_root_a"), self.records)
        self.assertIn(("card_start", "om_card_2", "om_root_b"), self.records)
        self.assertIn(("card_done", "om_card_1", "done:chat-e2e:om_root_a:调研 Codex"), self.records)
        self.assertIn(("card_done", "om_card_2", "done:chat-e2e:om_root_b:写小红书选题"), self.records)

    def test_thread_reply_reuses_original_session_and_card_alias(self):
        fsapp.handle_message(_event("om_root_a", "第一轮任务", root_id=None))
        time.sleep(0.1)
        fsapp.handle_message(_event("om_reply_a", "继续补充", root_id="om_root_a", parent_id="om_card_1"))
        time.sleep(0.2)

        self.assertEqual(list(fsapp.runtime.sessions), ["chat-e2e:om_root_a"])
        session = fsapp.runtime.sessions["chat-e2e:om_root_a"]
        self.assertEqual(session.agent.history, ["第一轮任务", "继续补充"])
        self.assertIn(("card_start", "om_card_2", "om_card_1"), self.records)
        self.assertIn(("card_done", "om_card_2", "done:chat-e2e:om_root_a:继续补充"), self.records)

    def test_retry_notice_is_visible_on_card_before_done(self):
        fsapp.handle_message(_event("om_retry_visible", "retry-visible", root_id=None))
        time.sleep(0.2)

        self.assertIn(("card_step", "om_card_1", "LLM retry 1/2"), self.records)
        self.assertIn(("card_done", "om_card_1", "done:chat-e2e:om_retry_visible:retry-visible"), self.records)

    def test_sensitive_text_is_redacted_before_logging_or_display(self):
        raw = "token=abc123 sk-liveSECRETVALUE access_key: xyz789"
        redacted = fsapp._redact_sensitive(raw)

        self.assertNotIn("sk-liveSECRETVALUE", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("xyz789", redacted)
        self.assertIn("sk-***", redacted)

    def test_redacting_stream_sanitizes_generic_stdout_writes(self):
        buf = io.StringIO()
        stream = fsapp._RedactingStream(buf)

        stream.write("run --api-key abc123 with sk-liveSECRETVALUE")

        written = buf.getvalue()
        self.assertNotIn("abc123", written)
        self.assertNotIn("sk-liveSECRETVALUE", written)
        self.assertIn("--api-key ***", written)

    def test_provider_failure_marks_card_failed_not_done(self):
        done_event = threading.Event()
        card = _FakeCard("chat-e2e", "chat_id", reply_to_message_id="om_root")
        hook = fsapp._make_task_hook(
            card,
            done_event,
            lambda raw: self.records.append(("files_from_final", raw)),
            lambda: self.records.append(("reaction_from_done",)),
        )

        response = type("Resp", (), {"content": "!!!Error: SSLError"})()
        hook({"exit_reason": "done", "response": response})

        self.assertTrue(done_event.is_set())
        self.assertIn(("card_fail", card.msg_id, "!!!Error: SSLError"), self.records)
        self.assertNotIn(("card_done", card.msg_id, "!!!Error: SSLError"), self.records)
        self.assertNotIn(("files_from_final", "!!!Error: SSLError"), self.records)
        self.assertNotIn(("reaction_from_done",), self.records)

    def test_normal_answer_mentioning_http_status_is_not_provider_failure(self):
        done_event = threading.Event()
        card = _FakeCard("chat-e2e", "chat_id", reply_to_message_id="om_root")
        hook = fsapp._make_task_hook(
            card,
            done_event,
            lambda raw: self.records.append(("files_from_final", raw)),
            lambda: self.records.append(("reaction_from_done",)),
        )

        response = type("Resp", (), {"content": "HTTP 429 means rate limiting, not a task failure."})()
        hook({"exit_reason": "done", "response": response})

        self.assertTrue(done_event.is_set())
        self.assertIn(("card_done", card.msg_id, "HTTP 429 means rate limiting, not a task failure."), self.records)
        self.assertIn(("files_from_final", "HTTP 429 means rate limiting, not a task failure."), self.records)
        self.assertIn(("reaction_from_done",), self.records)
        self.assertNotIn(("card_fail", card.msg_id, "HTTP 429 means rate limiting, not a task failure."), self.records)

    def test_hook_exception_releases_done_event(self):
        class BrokenCard(_FakeCard):
            def done(self, text):
                raise RuntimeError("card update failed")

            def fail(self, msg):
                self.records.append(("card_fail", self.msg_id, msg))

        done_event = threading.Event()
        card = BrokenCard("chat-e2e", "chat_id", reply_to_message_id="om_root")
        hook = fsapp._make_task_hook(card, done_event, lambda raw: None, None)

        response = type("Resp", (), {"content": "normal answer"})()
        hook({"exit_reason": "done", "response": response})

        self.assertTrue(done_event.is_set())
        self.assertIn(("card_fail", card.msg_id, "错误: card update failed"), self.records)

    def test_health_file_write_is_atomic_json(self):
        fsapp._write_health("waiting", session_id="s1")
        with open(fsapp.HEALTH_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["status"], "waiting")
        self.assertEqual(data["session_id"], "s1")
        self.assertIn("pid", data)

    def test_health_heartbeat_refreshes_last_state(self):
        fsapp.HEALTH_INTERVAL_SEC = 0.05
        fsapp._write_health("waiting", session_id="s1")
        first_mtime = os.path.getmtime(fsapp.HEALTH_FILE)
        try:
            fsapp._start_health_heartbeat()
            deadline = time.time() + 2
            while time.time() < deadline and os.path.getmtime(fsapp.HEALTH_FILE) <= first_mtime:
                time.sleep(0.05)
        finally:
            fsapp._stop_health_heartbeat()

        with open(fsapp.HEALTH_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertGreater(os.path.getmtime(fsapp.HEALTH_FILE), first_mtime)
        self.assertEqual(data["status"], "waiting")
        self.assertEqual(data["session_id"], "s1")


if __name__ == "__main__":
    unittest.main()
