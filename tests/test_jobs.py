import unittest

from optchat.jobs import PAGE, JobBoard
from optchat.memory import Memory
from optchat.util import PLACEHOLDER, size
from .test_memory import Fixture


class JobTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.memory = Memory(self.store)
        self.now = 100.0
        self.board = JobBoard(self.memory, clock=lambda: self.now)

    def read_all(self, job):
        offset, pages = 0, []
        while offset is not None:
            part = self.board.read(job, offset)
            pages.append(part["text"])
            offset = part["next_offset"]
        return "".join(pages)

    def test_free_nodes_and_binary_parent_need_no_worker(self):
        for text in ("a", "b", "c", "d"):
            self.board.append("user", text)
        self.assertEqual(self.board.next("w")["status"], "done")
        self.assertEqual(self.store.tree[(2, 0)].text, "user: a\nuser: b\nuser: c\nuser: d")

    def test_ordered_leaves_context_and_no_placeholder(self):
        for i in range(3):
            self.board.append("user", f"marker{i}:" + "x" * 1000)
        first = self.board.next("one")
        self.assertEqual(self.board.next("two")["status"], "waiting")
        prompt = self.read_all(first["job"])
        self.assertIn("marker0:" + "x" * 1000, prompt)
        self.assertNotIn(PLACEHOLDER, prompt)
        self.assertNotRegex(prompt, r"\d+\+\d+\|")
        self.board.submit(first["job"], "user: marker0 was supplied")
        second = self.board.next("two")
        prompt = self.read_all(second["job"])
        self.assertIn("user: marker0 was supplied", prompt)
        self.assertIn("marker1:" + "x" * 1000, prompt)

    def test_full_input_must_be_read(self):
        self.board.append("user", "😀" * 40000)
        job = self.board.next("w")["job"]
        with self.assertRaisesRegex(ValueError, "complete prompt"):
            self.board.submit(job, "user: emoji paste")
        with self.assertRaises(ValueError):
            self.board.read(job, PAGE + 1)  # The claim delivered the first page only.
        text = self.read_all(job)
        self.assertIn("😀" * 40000, text)
        self.assertEqual(self.board.submit(job, "user: emoji paste")["status"], "saved")

    def test_shortest_of_five_and_idempotent_final_submit(self):
        self.board.append("user", "x" * 1000)
        job = self.board.next("w")["job"]
        self.read_all(job)
        for n in (600, 550, 560, 580):
            response = self.board.submit(job, "s" * n)
            self.assertEqual(response["status"], "retry")
            self.assertIn("[LIMIT]", response["feedback"])
        result = self.board.submit(job, "s" * 590)
        self.assertEqual(result["bytes"], 550)
        self.assertEqual(self.board.submit(job, "s" * 590), result)
        self.assertEqual(len(self.store.tree), 1)

    def test_expired_job_cannot_commit_and_becomes_available(self):
        self.board.append("user", "x" * 1000)
        job = self.board.next("w")["job"]
        self.read_all(job)
        self.now += 301
        with self.assertRaisesRegex(ValueError, "expired"):
            self.board.submit(job, "user: x")
        self.now += 10
        self.assertEqual(self.board.next("different")["status"], "claimed")

    def test_claim_idempotent_and_release_has_fixed_delay(self):
        self.board.append("user", "x" * 1000)
        first = self.board.next("w")
        self.assertEqual(self.board.next("w"), first)
        self.board.release(first["job"], "worker refused")
        self.assertEqual(self.board.next("other")["status"], "waiting")
        self.now += 10
        self.assertEqual(self.board.next("other")["status"], "claimed")

    def test_refusal_empty_and_invalid_unicode_rejected(self):
        self.board.append("user", "x" * 1000)
        job = self.board.next("w")["job"]
        self.read_all(job)
        for line in ("", "I can't help with that", "i cannot summarize", "user: \ud83d"):
            with self.assertRaises(ValueError): self.board.submit(job, line)
        self.assertEqual(self.store.tree, {})

    def test_resume_reconstructs_unfinished_jobs(self):
        self.board.append("user", "x" * 1000)
        self.board.next("lost worker")
        restarted = JobBoard(Memory(self.store))
        self.assertEqual(restarted.next("new")["status"], "claimed")

    def test_only_ready_parents_are_offered(self):
        for _ in range(8):
            self.board.append("user", "x" * 1000)
        count = 0
        while True:
            job = self.board.next("w")
            if job["status"] == "done": break
            self.assertEqual(job["status"], "claimed")
            self.read_all(job["job"])
            self.board.submit(job["job"], "user: " + "s" * 400)
            count += 1
        self.assertEqual(count, 15)
        self.assertEqual(len(self.store.tree), 15)
