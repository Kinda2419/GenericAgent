import json
import os
import re
import threading
import time

from agentmain import GenericAgentRuntime


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
SESSION_STATE_FILE = os.environ.get("GA_FEISHU_SESSION_STATE_FILE") or os.path.join(TEMP_DIR, "feishu_sessions.json")
SESSION_STATE_SCHEMA_VERSION = 1
SESSION_STATE_CORRUPT_SUFFIX = ".corrupt"
CONTEXT_FOLLOWUP_RE = re.compile(r"^\s*(重试|继续|接着|继续执行|通过进入下一阶段|下一阶段|go on|continue|retry)\b", re.I)


runtime = GenericAgentRuntime()
session_lock = threading.RLock()


def _empty_feishu_state():
    return {
        "schema_version": SESSION_STATE_SCHEMA_VERSION,
        "session_aliases": {},
        "chat_active_sessions": {},
        "sessions": {},
    }


def _normalize_feishu_state(raw):
    state = _empty_feishu_state()
    if not isinstance(raw, dict):
        return state
    state["schema_version"] = int(raw.get("schema_version") or 0) or SESSION_STATE_SCHEMA_VERSION
    for key in ("session_aliases", "chat_active_sessions", "sessions"):
        value = raw.get(key)
        if isinstance(value, dict):
            state[key] = dict(value)
    if state["schema_version"] < SESSION_STATE_SCHEMA_VERSION:
        state["schema_version"] = SESSION_STATE_SCHEMA_VERSION
    return state


def _backup_corrupt_session_state(path, error):
    if not os.path.exists(path):
        return None
    backup = f"{path}{SESSION_STATE_CORRUPT_SUFFIX}-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, backup)
        with open(backup + ".error.txt", "w", encoding="utf-8") as f:
            f.write(str(error))
        return backup
    except Exception as backup_error:
        print(f"[WARN] failed to back up corrupt Feishu session state: {backup_error}")
        return None


def _load_feishu_session_state(path=None):
    path = path or SESSION_STATE_FILE
    if not os.path.exists(path):
        return _empty_feishu_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _normalize_feishu_state(json.load(f))
    except Exception as e:
        backup = _backup_corrupt_session_state(path, e)
        print(f"[WARN] Feishu session state ignored after load failure: {e}" + (f"; backup={backup}" if backup else ""))
        return _empty_feishu_state()


def _apply_feishu_session_state(state):
    state = _normalize_feishu_state(state)
    session_aliases.clear()
    session_aliases.update(state["session_aliases"])
    chat_active_sessions.clear()
    chat_active_sessions.update(state["chat_active_sessions"])
    _persisted_sessions.clear()
    _persisted_sessions.update(state["sessions"])
    return state


def reload_feishu_session_state(path=None):
    with session_lock:
        return _apply_feishu_session_state(_load_feishu_session_state(path))


_state = _load_feishu_session_state()
session_aliases = dict(_state.get("session_aliases") or {})
chat_active_sessions = dict(_state.get("chat_active_sessions") or {})
_persisted_sessions = dict(_state.get("sessions") or {})


def _json_safe_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return None


def _get_llm_history(agent):
    try:
        return list(getattr(agent.llmclient.backend, "history", []) or [])
    except Exception:
        return []


def _set_llm_history(agent, history):
    if not isinstance(history, list) or not history:
        return False
    try:
        agent.llmclient.backend.history = list(history)
        return True
    except Exception as e:
        print(f"[WARN] failed to restore Feishu LLM history: {e}")
        return False


def _has_real_agent_history(agent):
    history = getattr(agent, "history", None)
    return isinstance(history, list) and bool(history)


def _persist_feishu_sessions():
    with session_lock:
        data = _empty_feishu_state()
        data["session_aliases"] = dict(session_aliases)
        data["chat_active_sessions"] = dict(chat_active_sessions)
        data["sessions"] = dict(_persisted_sessions)
        for sess in runtime.active_sessions():
            data["sessions"][sess.session_id] = {
                "metadata": dict(sess.metadata),
                "history": list(getattr(sess.agent, "history", [])),
                "llm_history": _json_safe_copy(_get_llm_history(sess.agent)),
                "updated_at": getattr(sess, "updated_at", time.time()),
            }
        parent = os.path.dirname(os.path.abspath(SESSION_STATE_FILE))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{SESSION_STATE_FILE}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SESSION_STATE_FILE)
        _persisted_sessions.clear()
        _persisted_sessions.update(data["sessions"])
        return data


def _restore_feishu_session(session):
    data = _persisted_sessions.get(session.session_id) or {}
    metadata = data.get("metadata") or {}
    if isinstance(metadata, dict):
        session.metadata.update(metadata)
    history = data.get("history")
    if isinstance(history, list) and history and not _has_real_agent_history(session.agent):
        session.agent.history = list(history)
    llm_history = data.get("llm_history")
    if isinstance(llm_history, list) and llm_history and not _get_llm_history(session.agent):
        if _set_llm_history(session.agent, llm_history):
            print(f"[Info] restored Feishu LLM history for {session.session_id}: {len(llm_history)} messages")
    return session


def _msg_attr(message, name, default=None):
    value = getattr(message, name, default)
    return value if value not in (None, "") else default


def _message_thread_ids(message):
    ids = []
    for name in ("root_id", "parent_id", "thread_id", "message_id"):
        value = _msg_attr(message, name)
        if value and value not in ids:
            ids.append(value)
    return ids


def _session_key(chat_id, message_id):
    return f"{chat_id or 'direct'}:{message_id}"


def _is_feishu_message_id(value):
    return isinstance(value, str) and value.startswith("om_")


def _message_root_id(message):
    return _msg_attr(message, "root_id") or _msg_attr(message, "parent_id") or _msg_attr(message, "thread_id")


def _is_context_followup(text):
    return bool(CONTEXT_FOLLOWUP_RE.search(text or ""))


def resolve_feishu_session(open_id, chat_id, message, user_input=None):
    with session_lock:
        scope_key = chat_id or open_id or "direct"
        for mid in _message_thread_ids(message):
            sid = session_aliases.get(_session_key(chat_id, mid))
            if sid:
                chat_active_sessions[scope_key] = sid
                sess = runtime.get_or_create(sid, metadata={"open_id": open_id, "chat_id": chat_id})
                _restore_feishu_session(sess)
                _persist_feishu_sessions()
                return sess, False
        if _is_context_followup(user_input):
            sid = chat_active_sessions.get(scope_key)
            if sid:
                sess = runtime.get_or_create(sid, metadata={"open_id": open_id, "chat_id": chat_id})
                _restore_feishu_session(sess)
                message_id = _msg_attr(message, "message_id")
                if message_id:
                    session_aliases[_session_key(chat_id, message_id)] = sid
                _persist_feishu_sessions()
                print(f"[Info] reused active Feishu session for follow-up: {sid}")
                return sess, False
        root = _message_root_id(message) or message.message_id
        sid = root if str(root).startswith(f"{chat_id or 'direct'}:") else _session_key(chat_id, root)
        sess = runtime.get_or_create(sid, metadata={"open_id": open_id, "chat_id": chat_id, "root_message_id": root})
        _restore_feishu_session(sess)
        chat_active_sessions[scope_key] = sid
        for mid in _message_thread_ids(message):
            session_aliases[_session_key(chat_id, mid)] = sid
        session_aliases[_session_key(chat_id, root)] = sid
        _persist_feishu_sessions()
        return sess, True


def _session_reply_anchor(session, fallback=None):
    for value in (session.metadata.get("card_message_id"), session.metadata.get("root_message_id"), fallback):
        if _is_feishu_message_id(value):
            return value
    return fallback


def bind_feishu_session_message(session, message_id):
    if not message_id:
        return
    chat_id = session.metadata.get("chat_id")
    open_id = session.metadata.get("open_id")
    scope_key = chat_id or open_id or "direct"
    with session_lock:
        session_aliases[_session_key(chat_id, message_id)] = session.session_id
        chat_active_sessions[scope_key] = session.session_id
        session.metadata["card_message_id"] = message_id
        _persist_feishu_sessions()
