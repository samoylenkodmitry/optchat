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

    def test_chats_get_only_the_reading_tools_and_the_compactor_only_the_job_tools(self):
        import json
        from optchat.mcp import JOB_TOOLS, MEMORY_TOOLS, TOOLS, dispatch
        names = [t["name"] for t in dispatch({"method": "tools/list"}, None, MEMORY_TOOLS)["tools"]]
        self.assertEqual(names, ["view", "search", "thread", "zoom", "date", "read_message"])
        self.assertEqual([t["name"] for t in JOB_TOOLS], ["compact_next", "compact_read", "compact_submit", "compact_release"])
        with self.assertRaises(ValueError):
            dispatch({"method": "tools/call", "params": {"name": "compact_next", "arguments": {}}}, None, MEMORY_TOOLS)
        self.assertNotIn("instructions", dispatch({"method": "initialize"}, None, MEMORY_TOOLS), "the global instructions cover it")
        seen = lambda tools: len(json.dumps([{k: t[k] for k in ("name", "description", "inputSchema")} for t in tools]))
        self.assertLess(seen(MEMORY_TOOLS), 2000, "the tool text that a model reads")
        self.assertLess(seen(TOOLS), 4700)

    def test_append_reply_holds_no_hint_to_summarize(self):
        self.note(STATUS_AT * 2)
        reply = self.service.call("append", {"event_id": "last", "kind": "user", "text": "hello"})
        self.assertEqual(set(reply), {"id", "gid", "status", "ordered"})

    def test_search_returns_ten_lines_by_default(self):
        for k in range(15):
            self.service.call("append", {"event_id": f"d{k}", "kind": "user", "text": f"flavor {k}"})
        self.assertEqual(len(self.service.call("search", {"query": "flavor"})["text"].splitlines()), 11)

    def test_search_finds_and_limits(self):
        for k, text in enumerate(["use tabs in flutter", "the default flavor is internal", "unrelated"] + [f"flavor note {k}" for k in range(30)]):
            self.service.call("append", {"event_id": f"s{k}", "kind": "user", "text": text})
        result = self.service.call("search", {"query": "default flavor", "limit": 5})["text"]
        lines = result.splitlines()
        self.assertTrue(lines[0].startswith("31 messages match; the best 5 follow."))
        self.assertIn("the default flavor is internal", lines[1], "the line with both words ranks first")
        self.assertEqual(len(lines), 6)
        self.assertNotIn("unrelated", result)

    def add(self, text, session, project="/work/app", kind="user", agent="claude", machine="mac"):
        self.n += 1
        origin = {"agent": agent, "machine": machine, **({"project": project} if project else {}), **({"session": session} if session else {})}
        self.service.call("append", {"event_id": f"t{self.n}", "kind": kind, "text": text, "origin": origin})

    def test_search_lines_follow_the_documented_format(self):
        import hashlib
        from optchat.mcp import MEMORY_TOOLS
        self.add("send the email to Anna", "session-a")
        line = self.service.call("search", {"query": "email"})["text"].splitlines()[1]
        tag = hashlib.sha256(b"session-a").hexdigest()[:6]
        self.assertRegex(line, r"^0\+0\|user@app~[0-9a-f]{8}/claude@mac#" + tag + r" \d{4}-\d\d-\d\dT\d\d:\d\d: send the email to Anna$")
        description = next(t["description"] for t in MEMORY_TOOLS if t["name"] == "search")
        self.assertIn("id+0|kind@folder~hash/agent@machine#session date: text", description)
        self.assertIn("folder changes when the chat changes directory", description)

    def test_thread_reads_the_rest_of_one_session_in_order(self):
        import hashlib
        from optchat.mcp import MEMORY_TOOLS, dispatch
        self.add("question one", "a")                              # 0
        self.add("other chat 1", "b", project="/work/other")      # 1
        self.add("reply one", "a", kind="talk")                    # 2
        self.add("other chat 2", "b", project="/work/other")      # 3
        self.add("moved to another folder", "a", project="/work/lib")  # 4
        self.add("question two", "a")                              # 5
        self.add("question three", "a")                            # 6
        notice = '<task-notification>\n<tool-use-id>t</tool-use-id>\n<summary>x</summary>\n</task-notification>'
        self.service.call("append", {"event_id": "old-notice", "kind": "user", "text": notice, "origin": {"session": "a", "agent": "claude"}})  # 7, an old record
        tag = "#" + hashlib.sha256(b"a").hexdigest()[:6]
        first = self.service.call("thread", {"id": 0, "n": 2})["text"].splitlines()
        self.assertTrue(first[0].startswith(f"Later messages in {tag}: 4; the next 2 follow. thread(4, n) continues."), first[0])
        self.assertEqual([l.split("|")[0] for l in first[1:]], ["2+0", "4+0"])
        self.assertTrue(all(tag in l for l in first[1:]), "the session tag stays when the folder changes")
        self.assertIn("@lib~", first[2])
        rest = self.service.call("thread", {"id": 4})["text"].splitlines()
        self.assertTrue(rest[0].startswith(f"Later messages in {tag}: 2; the next 2 follow."), rest[0])
        self.assertNotIn("continues", rest[0])
        self.assertEqual([l.split("|")[0] for l in rest[1:]], ["5+0", "6+0"], "the old notice stays out")
        self.assertEqual(self.service.call("thread", {"id": 6})["text"], f"Later messages in {tag}: 0; the next 0 follow.")
        reply = dispatch({"method": "tools/call", "params": {"name": "thread", "arguments": {"id": 1}}}, self.service, MEMORY_TOOLS)
        self.assertFalse(reply["isError"])
        self.assertEqual([l.split("|")[0] for l in reply["content"][0]["text"].splitlines()[1:]], ["3+0"])

    def test_thread_needs_a_message_with_a_session_and_a_valid_count(self):
        self.add("no session", None)
        self.assertEqual(self.service.call("thread", {"id": 0})["text"], "Message 0 has no session.")
        for args in ({"id": 5}, {"id": -1}, {"id": 0, "n": 0}, {"id": 0, "n": 31}):
            with self.assertRaises(ValueError):
                self.service.call("thread", args)


if __name__ == "__main__":
    unittest.main()
