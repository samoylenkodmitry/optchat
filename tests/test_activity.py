import tempfile
import time
import unittest
from pathlib import Path

from optchat.activity import DIGEST_LIMIT, REPORT_LIMIT, bash_is_read_only, compact, digest
from optchat.hooks import STALE_TURN
from optchat.service import Service


def post(tool, data=None, response=None, n="1", **extra):
    return {"hook_event_name": "PostToolUse", "session_id": "s", "tool_use_id": n, "tool_name": tool,
            "tool_input": data or {}, "tool_response": response, **extra}


class Classify(unittest.TestCase):
    def test_read_only_commands(self):
        for command in ("ls -la && git status", "cat a.txt | grep foo | head -5", "rg pattern src", "git log --oneline -5",
                        "cd app && find . -name '*.kt' | wc -l", "LANG=C sort x | uniq -c", "sed -n '1,20p' file",
                        "grep -rn 'selectedBuildVariant\\|isExplicitFlavor' app/build.gradle", "ls 2>/dev/null", "git branch -a", "git stash list"):
            self.assertTrue(bash_is_read_only(command), command)
        for command in ("./gradlew assembleDebug", "git commit -m x", "sed -i 's/a/b/' f", "npm test", "rm -rf build",
                        "ls && make", "git push origin main", "cat a > b", "find . -name x -delete", "ls\nrm x",
                        "git stash", "git branch feature", "echo $(rm -rf x)", "echo 'unbalanced"):
            self.assertFalse(bash_is_read_only(command), command)

    def test_items(self):
        root = "/repo"
        self.assertEqual(compact(post("Read", {"file_path": "/repo/app/build.gradle"}, "x" * 9000), root), {"group": "read", "text": "app/build.gradle"})
        edit = compact(post("Edit", {"file_path": "/repo/a.py", "old_string": "x\ny", "new_string": "x\ny\nz"}), root)
        self.assertEqual(edit["text"], "a.py (+3 -2)")
        self.assertEqual(compact(post("Write", {"file_path": "/repo/n.md", "content": "a\nb"}), root)["text"], "n.md (written, 2 lines)")
        ran = compact(post("Bash", {"command": "./gradlew build"}, {"stdout": "step\nBUILD SUCCESSFUL\n", "stderr": ""}), root)
        self.assertEqual(ran["group"], "ran")
        self.assertIn("last line: BUILD SUCCESSFUL", ran["text"])
        self.assertIsNone(compact(post("TodoWrite", {"todos": []})))
        failed = compact({**post("Bash", {"command": "npm run lint"}), "hook_event_name": "PostToolUseFailure", "error": "exit 1\nmore"})
        self.assertEqual(failed, {"group": "failed", "text": "Bash npm run lint: exit 1"})

    def test_user_answers_and_reports_are_kept_on_their_own(self):
        answers = compact(post("AskUserQuestion", {}, {"answers": {"Which flavor?": "internal"}}))
        self.assertEqual(answers["message"], ("user", "Answers to agent questions:\nWhich flavor? internal"))
        report = compact(post("Agent", {"subagent_type": "Explore", "description": "find flavors"}, {"content": [{"type": "text", "text": "r" * 9000}]}))
        kind, text = report["message"]
        self.assertEqual(kind, "echo")
        self.assertLess(len(text), REPORT_LIMIT + 100)

    def test_digest(self):
        items = [{"group": "read", "text": f"src/f{k}.py"} for k in range(7)] + [
            {"group": "search", "text": "foo"}, {"group": "looked", "text": "ls"},
            {"group": "changed", "text": "a.py (+1 -1)", "key": "/r/a.py"}, {"group": "changed", "text": "a.py (+5 -1)", "key": "/r/a.py"}]
        text = digest(items)
        self.assertIn("Changed: a.py (+5 -1)", text)
        self.assertNotIn("+1 -1", text)
        self.assertIn("read 7 files (f0.py, f1.py, f2.py and 4 more), 1 search, 1 read-only command", text)
        self.assertIsNone(digest([]))
        many = [{"group": "ran", "text": "make target" + str(k) + " (ok, 3 lines of output)"} for k in range(100)]
        self.assertLessEqual(len(digest(many).encode()), DIGEST_LIMIT)
        busy = [{"group": "changed", "text": f"src/f{k}.kt (+1 -1)", "key": str(k)} for k in range(12)] + [
            {"group": "ran", "text": f"./gradlew task{k} (ok, 9 lines of output)"} for k in range(4)] + [{"group": "read", "text": "a.kt"}]
        record = digest(busy)
        self.assertIn("and 7 more files", record)
        self.assertIn("and 2 more commands", record)
        self.assertIn("Looked at: read 1 file", record, "every section fits")


class TurnRecord(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.temp.name))
        self.call({"hook_event_name": "SessionStart", "session_id": "s"})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def call(self, event):
        return self.service.call("hook", event)

    def test_one_turn_becomes_a_few_short_messages(self):
        self.call({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt_id": "p", "prompt": "which flavor is the default?"})
        for k in range(20):
            self.call(post("Read", {"file_path": f"/repo/src/f{k}.kt"}, "SECRET FILE TEXT " * 500, n=f"r{k}", cwd="/repo"))
            self.call(post("Grep", {"pattern": f"flavor{k}"}, "MATCH LINES " * 100, n=f"g{k}", cwd="/repo"))
        self.call(post("Bash", {"command": "ls -la && git log -5"}, {"stdout": "LISTING " * 300}, n="b1", cwd="/repo"))
        self.call(post("AskUserQuestion", {}, {"answers": {"Use internal?": "yes"}}, n="q1", cwd="/repo"))
        self.call(post("Edit", {"file_path": "/repo/app/build.gradle", "old_string": "a", "new_string": "b\nc"}, "ok", n="e1", cwd="/repo"))
        self.call({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t", "last_assistant_message": "The default is internal."})
        root = self.service.store.root
        self.assertEqual([m.kind for m in root], ["user", "user", "tool", "talk"])
        self.assertIn("Use internal? yes", root[1].text)
        self.assertIn("build.gradle (+2 -1)", root[2].text)
        self.assertIn("read 20 files", root[2].text)
        joined = " ".join(m.text for m in root)
        for noise in ("SECRET FILE TEXT", "MATCH LINES", "LISTING"):
            self.assertNotIn(noise, joined)

    def test_replayed_stop_writes_one_record(self):
        self.call(post("Edit", {"file_path": "/repo/a.py", "old_string": "a", "new_string": "b"}, "ok"))
        stop = {"hook_event_name": "Stop", "session_id": "s", "turn_id": "t", "last_assistant_message": "done"}
        self.call(stop)
        self.call(post("Edit", {"file_path": "/repo/a.py", "old_string": "a", "new_string": "b"}, "ok"))  # the same call delivered again
        self.call(stop)
        self.assertEqual([m.kind for m in self.service.store.root], ["tool", "talk"])

    def test_a_turn_without_stop_is_written_later(self):
        self.call(post("Write", {"file_path": "/repo/x.md", "content": "a"}, "ok"))
        self.service.ledger.db.execute("UPDATE turn SET at = ?", (time.time() - STALE_TURN - 1,))
        self.call({"hook_event_name": "SessionStart", "session_id": "other"})
        self.assertIn("x.md (written, 1 line)", self.service.store.root[0].text)


if __name__ == "__main__":
    unittest.main()
