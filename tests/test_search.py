import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from optchat.service import Service

HEAD = "Best matches first. zoom(id, 1) gives a whole message."


class Search(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.temp.name))
        self.n = 0

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def add(self, text, kind="user"):
        self.n += 1
        return self.service.call("append", {"event_id": f"m{self.n}", "kind": kind, "text": text})["id"]

    def ids(self, query, limit=10):
        lines = self.service.call("search", {"query": query, "limit": limit})["text"].splitlines()
        return [int(line.split("+")[0]) for line in lines[1:]]

    def test_words_match_whole_words(self):
        self.add("the shader programs compile")
        self.add("the link takes 18 ms")
        self.assertEqual(self.ids("ms"), [1])

    def test_a_long_message_does_not_win_by_its_length(self):
        self.add("Decision: remove the wgpu fork on every platform.")
        # A pasted conversation holds every word somewhere, as message 1735 of the real memory did.
        pasted = " ".join(f"word{k} filler{k % 37} text" for k in range(2000)) + " remove " + " wgpu " * 2 + " fork "
        self.add(pasted)
        for k in range(20):
            self.add(f"unrelated note {k} about the build")
        self.assertEqual(self.ids("remove wgpu fork")[0], 0)

    def test_a_long_query_word_also_finds_longer_words(self):
        self.add("we keep two forks of wgpu")
        self.add("the fork is gone")
        self.add("for the record")
        self.assertEqual(sorted(self.ids("fork")), [0, 1])
        self.assertEqual(self.ids("for"), [2], "a short word matches only itself")

    def test_an_identifier_written_as_in_the_query_ranks_first(self):
        self.add("log in with the account saved in ~/acc.txt")
        self.add("the acc field of the txt export holds the acc value and the txt name")
        self.assertEqual(self.ids("acc.txt")[0], 0)

    def test_no_match_and_new_messages_after_the_first_search(self):
        self.add("the first message")
        self.assertEqual(self.service.call("search", {"query": "pelican"})["text"], "No message holds these words.")
        self.add("a pelican flew by")
        self.assertEqual(self.service.call("search", {"query": "pelican"})["text"].splitlines(), [HEAD, self.line(1)])

    def line(self, i):
        m = self.service.store.root[i]
        return f"{i}+0|{m.label} {m.date[:16]}: {m.text}"

    def test_a_long_message_shows_its_summary_and_the_words_around_the_match(self):
        body = "We measured the first frame. " * 30 + "The tint compiler costs 18 ms on the Mate. " + "More notes follow. " * 30
        self.add(body, kind="talk")
        self.add("short message about tint")
        self.service.store.save_node(0, 0, "talk@app/claude@mac: first frame measured on the Mate; compile cost found.")
        from optchat.memory import Part
        self.service.memory.node_built(Part(0, 0))
        lines = self.service.call("search", {"query": "tint compiler"})["text"].splitlines()
        long_line = next(line for line in lines if line.startswith("0+0|"))
        self.assertIn(": first frame measured on the Mate; compile cost found. Match: ...", long_line, "the label is not repeated")
        self.assertIn("The tint compiler costs 18 ms", long_line)
        self.assertLess(len(long_line), 400)
        found = next(line for line in self.service.call("search", {"query": "compile frame"})["text"].splitlines() if line.startswith("0+0|"))
        self.assertTrue(found.endswith("compile cost found."), "no match part when the summary holds the word")
        self.assertEqual(lines[1 if lines[1].startswith("1+0|") else 2], self.line(1))

    def test_thread_lines_show_the_summary_of_a_long_message(self):
        from optchat.memory import Part
        origin = {"session": "s", "agent": "claude"}
        for k, text in enumerate(["start", "a long reply " * 40]):
            self.service.call("append", {"event_id": f"t{k}", "kind": "talk", "text": text, "origin": origin})
        self.service.store.save_node(0, 1, "talk: the reply in one line")
        self.service.memory.node_built(Part(0, 1))
        line = self.service.call("thread", {"id": 0})["text"].splitlines()[1]
        self.assertTrue(line.startswith("1+0|talk@shared/claude#"), line)
        self.assertTrue(line.endswith(": the reply in one line"), line)


class Tools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.temp.name))
        self.service.call("append", {"event_id": "a", "kind": "user", "text": "x" * 20})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def call(self, name, arguments):
        from optchat.mcp import MEMORY_TOOLS, dispatch
        return dispatch({"method": "tools/call", "params": {"name": name, "arguments": arguments}}, self.service, MEMORY_TOOLS)

    def test_wrong_arguments_get_an_error_that_names_them(self):
        reply = self.call("zoom", {"id": 0, "level": 1})
        self.assertTrue(reply["isError"])
        self.assertEqual(reply["content"][0]["text"], "zoom: unknown argument level; missing argument n. zoom takes id, n.")
        reply = self.call("search", {})
        self.assertEqual(reply["content"][0]["text"], "search: missing argument query. search takes query, limit (optional).")

    def test_read_message_starts_at_offset_zero_by_default(self):
        reply = self.call("read_message", {"id": 0})
        self.assertFalse(reply["isError"])
        self.assertTrue(reply["content"][0]["text"].endswith("\nuser: " + "x" * 20))


class Log(unittest.TestCase):
    def test_the_service_moves_a_large_log_aside(self):
        script = (
            "import os, sys\n"
            "import optchat.service as s\n"
            "chat = sys.argv[1]\n"
            "fd = os.open(chat + '/service.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)\n"
            "os.dup2(fd, 2)\n"
            "s.LOG_LIMIT = 100\n"
            "print('x' * 200, file=sys.stderr, flush=True)\n"
            "print(s.rotate_log(chat), file=sys.stdout, flush=True)\n"
            "s.log('after the move')\n"
            "print(s.rotate_log(chat), file=sys.stdout, flush=True)\n")
        with tempfile.TemporaryDirectory() as chat:
            out = subprocess.run([sys.executable, "-c", script, chat], capture_output=True, text=True, timeout=20,
                                 cwd=Path(__file__).resolve().parent.parent)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.split(), ["True", "False"])
            self.assertEqual((Path(chat) / "service.log.1").read_text(), "x" * 200 + "\n")
            self.assertRegex((Path(chat) / "service.log").read_text(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d after the move\n$")


if __name__ == "__main__":
    unittest.main()
