import json
import os
import tempfile
import threading
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontends"))

from agentmain import GenericAgentRuntime


class MultiSessionRuntimeTests(unittest.TestCase):
    def test_runtime_isolates_session_state(self):
        runtime = GenericAgentRuntime()
        a = runtime.get_or_create("chat:root-a", metadata={"chat_id": "chat", "root_message_id": "root-a"})
        b = runtime.get_or_create("chat:root-b", metadata={"chat_id": "chat", "root_message_id": "root-b"})

        self.assertIs(a, runtime.get_or_create("chat:root-a"))
        self.assertIsNot(a, b)

        a.agent.history.append("A only")
        b.agent.history.append("B only")
        a.add_document(__file__)

        self.assertEqual(a.agent.history, ["A only"])
        self.assertEqual(b.agent.history, ["B only"])
        self.assertEqual(len(a.documents), 1)
        self.assertEqual(b.documents, [])

    def test_runtime_uses_same_ga_installation_assets(self):
        runtime = GenericAgentRuntime()
        session = runtime.get_or_create("chat:root")
        root = os.path.dirname(os.path.dirname(__file__))
        self.assertTrue(os.path.isdir(os.path.join(root, "memory")))
        self.assertIsNotNone(session.agent.task_queue)


class FeishuTopicSessionTests(unittest.TestCase):
    def setUp(self):
        import frontends.fsapp as fsapp

        self.fsapp = fsapp
        self.old_runtime = fsapp.runtime
        self.old_aliases = fsapp.session_aliases
        self.old_active = fsapp.chat_active_sessions
        self.old_persisted = fsapp._persisted_sessions
        self.old_persist = fsapp._persist_feishu_sessions
        self.old_restore = fsapp._restore_feishu_session
        fsapp.runtime = GenericAgentRuntime()
        fsapp.session_aliases = {}
        fsapp.chat_active_sessions = {}
        fsapp._persisted_sessions = {}
        fsapp._persist_feishu_sessions = lambda: None
        fsapp._restore_feishu_session = lambda session: session

    def tearDown(self):
        fsapp = self.fsapp
        fsapp.runtime = self.old_runtime
        fsapp.session_aliases = self.old_aliases
        fsapp.chat_active_sessions = self.old_active
        fsapp._persisted_sessions = self.old_persisted
        fsapp._persist_feishu_sessions = self.old_persist
        fsapp._restore_feishu_session = self.old_restore

    def _message(self, message_id, root_id=None, parent_id=None, thread_id=None):
        return SimpleNamespace(
            message_id=message_id,
            root_id=root_id,
            parent_id=parent_id,
            thread_id=thread_id,
        )

    def test_direct_messages_create_independent_topic_sessions(self):
        first, first_is_new = self.fsapp.resolve_feishu_session("ou", "chat", self._message("m1"))
        second, second_is_new = self.fsapp.resolve_feishu_session("ou", "chat", self._message("m2"))

        self.assertTrue(first_is_new)
        self.assertTrue(second_is_new)
        self.assertEqual(first.session_id, "chat:m1")
        self.assertEqual(second.session_id, "chat:m2")
        self.assertIsNot(first, second)

    def test_thread_replies_continue_original_topic_session(self):
        original, _ = self.fsapp.resolve_feishu_session("ou", "chat", self._message("m1"))
        self.fsapp.bind_feishu_session_message(original, "card-1")

        by_root, by_root_is_new = self.fsapp.resolve_feishu_session(
            "ou", "chat", self._message("reply-1", root_id="m1", parent_id="card-1")
        )
        by_card, by_card_is_new = self.fsapp.resolve_feishu_session(
            "ou", "chat", self._message("reply-2", root_id="card-1", parent_id="card-1")
        )

        self.assertFalse(by_root_is_new)
        self.assertFalse(by_card_is_new)
        self.assertIs(original, by_root)
        self.assertIs(original, by_card)

    def test_task_card_creates_thread_reply(self):
        calls = []
        old_reply_raw = self.fsapp._reply_raw
        old_send_raw = self.fsapp._send_raw
        self.fsapp._reply_raw = lambda mid, payload, msg_type, reply_in_thread=True: calls.append(
            (mid, msg_type, reply_in_thread)
        ) or "card-1"
        self.fsapp._send_raw = lambda *args, **kwargs: self.fail("task card should reply under the user message")
        try:
            card = self.fsapp._TaskCard("chat", "chat_id", reply_to_message_id="m1")
            card.start()
        finally:
            self.fsapp._reply_raw = old_reply_raw
            self.fsapp._send_raw = old_send_raw

        self.assertEqual(card.msg_id, "card-1")
        self.assertEqual(calls, [("m1", "interactive", True)])
class FeishuSessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        import frontends.fsapp as fsapp

        self.fsapp = fsapp
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "feishu_sessions.json")
        self.old_state_file = fsapp.SESSION_STATE_FILE
        self.old_runtime = fsapp.runtime
        self.old_aliases = fsapp.session_aliases
        self.old_active = fsapp.chat_active_sessions
        self.old_persisted = fsapp._persisted_sessions
        fsapp.SESSION_STATE_FILE = self.state_file
        fsapp.runtime = GenericAgentRuntime()
        fsapp.session_aliases = {}
        fsapp.chat_active_sessions = {}
        fsapp._persisted_sessions = {}

    def tearDown(self):
        fsapp = self.fsapp
        fsapp.SESSION_STATE_FILE = self.old_state_file
        fsapp.runtime = self.old_runtime
        fsapp.session_aliases = self.old_aliases
        fsapp.chat_active_sessions = self.old_active
        fsapp._persisted_sessions = self.old_persisted
        self.tmp.cleanup()

    def _message(self, message_id, root_id=None, parent_id=None, thread_id=None):
        return SimpleNamespace(
            message_id=message_id,
            root_id=root_id,
            parent_id=parent_id,
            thread_id=thread_id,
        )

    def test_persist_writes_schema_version_and_survives_reload(self):
        fsapp = self.fsapp
        session, _ = fsapp.resolve_feishu_session("ou-1", "chat-1", self._message("m1"))
        session.agent.history.append({"role": "assistant", "content": "saved"})
        fsapp.bind_feishu_session_message(session, "om_card_1")
        state = fsapp._persist_feishu_sessions()

        self.assertEqual(state["schema_version"], fsapp.SESSION_STATE_SCHEMA_VERSION)
        with open(self.state_file, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["sessions"]["chat-1:m1"]["metadata"]["card_message_id"], "om_card_1")

        fsapp.runtime = GenericAgentRuntime()
        fsapp.session_aliases = {}
        fsapp.chat_active_sessions = {}
        fsapp._persisted_sessions = {}
        fsapp.reload_feishu_session_state()
        restored, is_new = fsapp.resolve_feishu_session("ou-1", "chat-1", self._message("reply", root_id="m1", parent_id="om_card_1"))

        self.assertFalse(is_new)
        self.assertEqual(restored.session_id, "chat-1:m1")
        self.assertEqual(restored.agent.history, [{"role": "assistant", "content": "saved"}])

    def test_corrupt_state_is_backed_up_and_does_not_crash(self):
        fsapp = self.fsapp
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write("{not json")
        state = fsapp._load_feishu_session_state(self.state_file)

        self.assertEqual(state["sessions"], {})
        self.assertFalse(os.path.exists(self.state_file))
        backups = [name for name in os.listdir(self.tmp.name) if name.startswith("feishu_sessions.json.corrupt-")]
        self.assertTrue(backups)

    def test_legacy_state_migrates_to_current_schema(self):
        fsapp = self.fsapp
        legacy = {
            "session_aliases": {"chat-1:m1": "chat-1:m1"},
            "chat_active_sessions": {"chat-1": "chat-1:m1"},
            "sessions": {"chat-1:m1": {"metadata": {"chat_id": "chat-1"}, "history": [], "updated_at": 1}},
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(legacy, f)

        state = fsapp.reload_feishu_session_state()

        self.assertEqual(state["schema_version"], fsapp.SESSION_STATE_SCHEMA_VERSION)
        self.assertEqual(fsapp.session_aliases["chat-1:m1"], "chat-1:m1")

    def test_parallel_persist_keeps_valid_json_and_all_sessions(self):
        fsapp = self.fsapp
        for idx in range(8):
            session = fsapp.runtime.get_or_create(f"chat:root-{idx}", metadata={"chat_id": "chat", "root_message_id": f"root-{idx}"})
            session.agent.history.append(f"history-{idx}")

        threads = [threading.Thread(target=fsapp._persist_feishu_sessions) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with open(self.state_file, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(len(state["sessions"]), 8)
        self.assertEqual(state["schema_version"], fsapp.SESSION_STATE_SCHEMA_VERSION)

    def test_same_chat_different_roots_and_users_do_not_cross(self):
        fsapp = self.fsapp
        first, _ = fsapp.resolve_feishu_session("ou-1", "chat-1", self._message("m1"))
        second, _ = fsapp.resolve_feishu_session("ou-2", "chat-1", self._message("m2"))
        direct, _ = fsapp.resolve_feishu_session("ou-1", None, self._message("dm1"))

        self.assertEqual(first.session_id, "chat-1:m1")
        self.assertEqual(second.session_id, "chat-1:m2")
        self.assertEqual(direct.session_id, "direct:dm1")
        self.assertIsNot(first, second)
        self.assertIsNot(first, direct)


if __name__ == "__main__":
    unittest.main()
