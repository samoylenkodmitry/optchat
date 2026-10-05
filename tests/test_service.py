import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat.mcp import dispatch, render_result
from optchat.service import Client, Service
from optchat.util import json_text


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.service = Service(self.path)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def append(self, key, text, kind="user"):
        return self.service.call("append", {"event_id": key, "kind": kind, "text": text})

    def test_idempotency_and_conflicts(self):
        a = self.append("same", "same")
        b = self.append("same", "same")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.service.memory.total, 1)
        with self.assertRaises(ValueError): self.append("same", "different")

    def test_pending_view_never_exposes_unsummarized_text(self):
        self.append("a", "whole long " * 100)
        result = self.service.call("view", {})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pending"], {"start": 0, "count": 1})
        self.assertNotIn("whole long", result["text"])

    def test_view_snapshot_stays_stable_across_appends(self):
        for i in range(100): self.append(str(i), str(i) + "x" * 300)
        first = self.service.call("view", {})
        self.assertEqual(first["status"], "ready")
        self.append("late", "new later input")
        text = first["text"]
        page = first
        while page["next_offset"] is not None:
            page = self.service.call("view", {"snapshot": first["snapshot"], "offset": page["next_offset"]})
            text += page["text"]
        self.assertNotIn("new later input", text)
        self.assertEqual(text.count("+1|"), 100)

    def test_surrogates_normalized_and_echo_cap(self):
        self.append("unicode", "broken \ud83d emoji")
        self.assertEqual(self.service.store.root[0].text, "broken � emoji")
        self.append("echo", "h" * 40000, "echo")
        self.assertEqual(len(self.service.store.root[1].text), 30000)

    def test_hook_skips_subagents_memory_and_compactor_delegation(self):
        base = {"hook_event_name": "PreToolUse", "session_id": "main", "tool_use_id": "t", "tool_name": "Bash", "tool_input": {"command": "pwd"}}
        for event in ({**base, "agent_id": "child"}, {**base, "tool_name": "mcp__optchat__compact_submit"}, {**base, "tool_name": "Agent", "tool_input": {"subagent_type": "optchat-compactor"}}):
            self.assertIn("ignored", self.service.call("hook", event))
        self.assertEqual(self.service.memory.total, 0)

    def test_hook_logs_main_chat_and_skips_reasoning(self):
        self.service.call("hook", {"hook_event_name": "SessionStart", "session_id": "s"})
        self.service.call("hook", {"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt_id": "p", "prompt": "hello"})
        self.service.call("hook", {"hook_event_name": "PreToolUse", "session_id": "s", "tool_use_id": "t", "tool_name": "Read", "tool_input": {"file_path": "README.md"}})
        self.service.call("hook", {"hook_event_name": "PostToolUse", "session_id": "s", "tool_use_id": "t", "tool_name": "Read", "tool_response": "contents"})
        self.service.call("hook", {"hook_event_name": "Stop", "session_id": "s", "turn_id": "turn", "last_assistant_message": "done", "thinking": "NEVER LOG"})
        self.assertEqual([m.kind for m in self.service.store.root], ["user", "tool", "echo", "talk"])
        self.assertNotIn("NEVER LOG", json_text([m.text for m in self.service.store.root]))

    def test_recovery_finishes_pending_ledger_write(self):
        ledger = self.service.ledger
        ledger.db.execute("INSERT INTO events (key,gid,kind,text,date,digest,done) VALUES ('crash',?,'user','saved','2026-01-01T00:00:00+00:00','digest',0)", (self.service.replica.next_gid(),))
        ledger.db.commit()
        self.service.close()
        self.service = Service(self.path)
        self.assertEqual(self.service.memory.total, 1)
        self.assertEqual(self.service.store.root[0].text, "saved")
        self.service.close()
        self.service = Service(self.path)
        self.assertEqual(self.service.memory.total, 1)

    def test_mcp_read_format_does_not_double_source_text(self):
        result = render_result({"offset": 0, "text": "unique source", "next_offset": None})
        self.assertEqual(result.count("unique source"), 1)
        self.assertEqual(dispatch({"method": "initialize"}, None)["serverInfo"]["name"], "optchat")


class DaemonTests(unittest.TestCase):
    def test_two_mcp_clients_share_one_writer_and_restart_preserves_history(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = Path(temp) / "chat"
            a, b = Client(chat), Client(chat)
            try:
                a.call("append", {"event_id": "one", "kind": "user", "text": "shared history"})
                self.assertEqual(b.call("status")["messages"], 1)
                proc = subprocess.run([sys.executable, "-m", "optchat", "--chat", str(chat), "mcp"],
                    input=json_text({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "zoom", "arguments": {"id": 0, "n": 1}}}) + "\n", text=True, capture_output=True, timeout=10)
                response = json.loads(proc.stdout)
                self.assertEqual(response["result"]["content"][0]["text"], "0+0|user: shared history")
                self.assertEqual(a.call("status")["model_processes"], 0)
            finally:
                a.call("shutdown")
                import time
                deadline = time.monotonic() + 3
                while a.path.exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertFalse(a.path.exists())
