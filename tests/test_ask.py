import tempfile
import unittest
from pathlib import Path

from optchat.jobs import ASK_AGAIN, ASK_MESSAGES
from optchat.service import Service


class AskBeforeCompaction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.temp.name))
        self.service.call("hook", {"hook_event_name": "SessionStart", "session_id": "s"})
        self.n = 0
        self.use_tools()

    def use_tools(self):
        self.service.call("hook", {"hook_event_name": "PostToolUse", "session_id": "s", "tool_use_id": "v", "tool_name": "mcp__optchat__view", "tool_input": {}, "tool_response": "view"})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def note(self, count):
        for _ in range(count):
            self.n += 1
            self.service.call("append", {"event_id": f"n{self.n}", "kind": "note", "text": f"note {self.n} " + "x" * 700})

    def prompt(self):
        self.n += 1
        result = self.service.call("hook", {"hook_event_name": "UserPromptSubmit", "session_id": "s", "event_id": f"p{self.n}", "prompt": "next question"})
        return result.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_agent_asks_once_then_again_after_more_messages(self):
        self.note(ASK_MESSAGES)
        first = self.prompt()
        self.assertIn(f"OptChat has {ASK_MESSAGES} messages from 1 recorded chat that wait for summaries", first)
        self.assertIn("only after the user agrees", first)
        self.assertEqual(self.prompt(), "", "follow-up questions in the same chat bring no new request")
        self.note(ASK_AGAIN)
        self.assertIn("ask the user", self.prompt())

    def test_no_question_in_a_chat_without_the_tools(self):
        self.service.call("hook", {"hook_event_name": "SessionStart", "session_id": "old"})
        self.note(ASK_MESSAGES)
        result = self.service.call("hook", {"hook_event_name": "UserPromptSubmit", "session_id": "old", "event_id": "x", "prompt": "hi"})
        self.assertNotIn("hookSpecificOutput", result)
        self.assertEqual([m.text for m in self.service.store.root if m.kind == "user"], ["hi"], "the chat is still recorded")

    def test_no_question_while_a_compactor_works(self):
        self.note(ASK_MESSAGES)
        self.service.call("compact_next", {})
        self.assertEqual(self.prompt(), "")


if __name__ == "__main__":
    unittest.main()
