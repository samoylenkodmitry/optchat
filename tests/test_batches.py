import unittest

from optchat.jobs import TOOL_CAP, JobBoard
from optchat.memory import Memory, Part
from .test_memory import Fixture


def read_all(board, claim):
    text, offset = claim["text"], claim["next_offset"]
    while offset is not None:
        page = board.read(claim["job"], offset)
        text, offset = text + page["text"], page["next_offset"]
    return text


class BatchTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.memory = Memory(self.store)
        self.board = JobBoard(self.memory, batch_tasks=10)

    def test_one_job_carries_consecutive_messages_and_short_ones_as_context(self):
        self.board.append("user", "first " + "x" * 700)
        self.board.append("user", "ok")
        self.board.append("user", "third " + "y" * 700)
        claim = self.board.next()
        self.assertEqual(claim["tasks"], 2)
        text = read_all(self.board, claim)
        self.assertIn("Task 1: compress message 0", text)
        self.assertIn("Context: message 1 is short and is its own line:\nuser: ok", text)
        self.assertIn("Task 2: compress message 2", text)
        result = self.board.submit(claim["job"], ["user: first long message", "user: third long message"])
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.store.tree[(0, 1)].text, "user: ok")
        self.assertEqual(self.memory.first(), 3)

    def test_only_an_overlong_line_comes_back(self):
        for k in range(3):
            self.board.append("user", f"m{k} " + "x" * 700)
        claim = self.board.next()
        read_all(self.board, claim)
        reply = self.board.submit(claim["job"], ["user: a", "user: " + "b" * 600, "user: c"])
        self.assertEqual(reply["status"], "retry")
        self.assertEqual(reply["tasks"], [2])
        self.assertIn((0, 0), self.store.tree)
        self.assertIn((0, 2), self.store.tree)
        self.assertNotIn((0, 1), self.store.tree)
        self.assertEqual(self.board.submit(claim["job"], ["user: b"])["status"], "saved")
        self.assertEqual(self.store.tree[(0, 1)].text, "user: b")

    def test_submit_hands_over_the_next_job(self):
        for k in range(4):
            self.board.append("user", f"m{k} " + "x" * 700)
        first = self.board.next()
        read_all(self.board, first)
        reply = self.board.submit(first["job"], [f"user: m{k} " + "s" * 300 for k in range(first["tasks"])], chain=True)
        nxt = reply["next"]
        self.assertEqual(nxt["status"], "claimed")
        self.assertEqual(nxt["worker"], first["worker"])
        self.assertIn("Task 1: merge these two lines", nxt["text"])
        self.assertNotIn("You write the memory of OptChat", nxt["text"], "the instructions come once per worker")

    def test_release_of_one_task_keeps_the_rest(self):
        for k in range(2):
            self.board.append("user", f"m{k} " + "x" * 700)
        claim = self.board.next()
        read_all(self.board, claim)
        released = self.board.release(claim["job"], "cannot summarize", task=1)
        self.assertEqual(released["open_tasks"], [2])
        self.assertEqual(self.board.submit(claim["job"], ["user: m1"])["status"], "saved")
        self.assertIn((0, 1), self.store.tree)
        self.assertEqual(self.board.failures, {(0, 0): "cannot summarize"})

    def test_worker_sees_only_the_ends_of_long_tool_output(self):
        self.board.append("echo", "start " + "z" * 50_000 + " end")
        text = read_all(self.board, self.board.next())
        self.assertIn("start", text)
        self.assertIn("end", text)
        self.assertIn("the middle part is not kept", text)
        self.assertLess(text.count("z"), TOOL_CAP)
        self.assertEqual(len(self.store.root[0].text), 50_010, "the log keeps the original")

    def test_worker_context_holds_only_the_newest_lines(self):
        import optchat.jobs as jobs
        for k in range(300):
            self.board.append("user", f"m{k} " + "x" * 300)  # short enough to be their own lines
        self.board.append("user", "last " + "y" * 700)
        context = self.board.context(self.board.frontier)
        self.assertLessEqual(sum(len(v) for v in context.values()), jobs.CONTEXT_CHARS)
        self.assertIn(f"{self.board.frontier - 1}+1", context, "the newest line before the task is kept")
        self.assertNotIn("0+1", context, "old lines are left out")

    def test_claimed_tasks_never_go_to_a_second_worker(self):
        for k in range(3):
            self.board.append("user", f"m{k} " + "x" * 700)
        first = self.board.next()
        self.assertEqual(first["tasks"], 3)
        self.assertEqual(self.board.next()["status"], "waiting")
        self.assertEqual(self.board.claimed, {Part(0, k).key for k in range(3)})


if __name__ == "__main__":
    unittest.main()
