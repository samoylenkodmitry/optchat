import tempfile
import unittest
from pathlib import Path

from optchat.jobs import STATUS_AT
from optchat.service import Service


class QuietMemory(unittest.TestCase):
    """OptChat keeps out of the context of agents unless they ask it something."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.temp.name))
        self.n = 0

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def note(self, count):
        for _ in range(count):
            self.n += 1
            self.service.call("append", {"event_id": f"n{self.n}", "kind": "note", "text": f"note {self.n} " + "x" * 700})

    def test_hooks_add_nothing_to_the_context(self):
        self.assertNotIn("hookSpecificOutput", self.service.call("hook", {"hook_event_name": "SessionStart", "session_id": "s"}))
        self.note(STATUS_AT * 3)
        result = self.service.call("hook", {"hook_event_name": "UserPromptSubmit", "session_id": "s", "event_id": "p", "prompt": "next"})
        self.assertNotIn("hookSpecificOutput", result)

    def test_status_line_shows_the_backlog_only_when_it_is_large(self):
        self.note(STATUS_AT - 1)
        self.assertEqual(self.service.call("status_line", {})["text"], "")
        self.note(1)
        self.assertEqual(self.service.call("status_line", {})["text"], f"OptChat: {STATUS_AT} to summarize")

    def test_a_worker_stops_at_the_messages_that_existed_when_it_started(self):
        self.note(3)
        claim = self.service.call("compact_next", {})
        self.assertEqual(claim["tasks"], 3)
        self.note(5)  # other agents keep writing during the run
        reply = self.service.call("compact_submit", {"job": claim["job"], "lines": ["note: a", "note: b", "note: c"]})
        nxt = reply["next"]
        while nxt["status"] == "claimed":
            nxt = self.service.call("compact_submit", {"job": nxt["job"], "lines": ["note: merged"] * nxt["tasks"]})["next"]
        self.assertEqual(nxt["status"], "done")
        self.assertEqual(nxt["arrived_later"], 5)
        self.assertNotIn((0, 3), self.service.store.tree, "later messages wait for the next run")

    def test_search_finds_and_limits(self):
        for k, text in enumerate(["use tabs in flutter", "the default flavor is internal", "unrelated"] + [f"flavor note {k}" for k in range(30)]):
            self.service.call("append", {"event_id": f"s{k}", "kind": "user", "text": text})
        result = self.service.call("search", {"query": "default flavor", "limit": 5})["text"]
        lines = result.splitlines()
        self.assertTrue(lines[0].startswith("31 messages match; the best 5 follow."))
        self.assertIn("the default flavor is internal", lines[1], "the line with both words ranks first")
        self.assertEqual(len(lines), 6)
        self.assertNotIn("unrelated", result)


if __name__ == "__main__":
    unittest.main()
