import asyncio
import contextlib
import io
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat import api
from optchat.jobs import STATUS_AT
from optchat.service import Service


def answer(lines, stop="end_turn", numbers=None):
    import json
    entries = [{"task": n, "line": line} for n, line in zip(numbers or range(1, len(lines) + 1), lines)]
    return {"content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": json.dumps({"lines": entries})}],
            "stop_reason": stop, "usage": {"input_tokens": 1000, "output_tokens": 100}}


class FakeApi:
    """Answers each job with one short line per task, and keeps a copy of every request."""

    def __init__(self, empty_first=False, skip_once=None):
        self.requests, self.empty_first, self.skip_once, self.n = [], empty_first, skip_once, 0

    def __call__(self, key, messages):
        self.requests.append([dict(m) for m in messages])
        last = messages[-1]["content"]
        job = re.search(r"This job has (\d+) task", last)
        numbers = list(range(1, int(job.group(1)) + 1)) if job else [int(n) for n in re.search(r"each of these tasks: ([\d, ]+)\.", last).group(1).split(", ")]
        lines = []
        for _ in numbers:
            self.n += 1
            lines.append(f"note: summary {self.n}")
        if self.empty_first:
            self.empty_first = False
            lines[0] = ""
        if self.skip_once and len(numbers) > 1:
            self.skip_once = False
            return answer(lines[1:], numbers=numbers[1:])
        return answer(lines, numbers=numbers)


class ApiWorker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"OPTCHAT_CONFIG": str(Path(self.temp.name) / "config/config.json")})
        self.env.start()
        self.service = Service(Path(self.temp.name) / "chat")
        self.n = 0

    def tearDown(self):
        self.service.close()
        self.env.stop()
        self.temp.cleanup()

    def note(self, count):
        for _ in range(count):
            self.n += 1
            self.service.call("append", {"event_id": f"n{self.n}", "kind": "note", "text": f"note {self.n} " + "x" * 700})

    def compact(self, fake):
        self.service.api.ask = fake
        asyncio.run(self.service.api.compact(self.service.call, "sk-ant-test"))

    def test_the_api_worker_summarizes_the_backlog_in_one_append_only_conversation(self):
        self.note(25)
        fake = FakeApi()
        self.compact(fake)
        self.assertEqual(self.service.memory.first(), self.service.memory.total)
        self.assertEqual(self.service.api.last["result"], "done")
        self.assertEqual(self.service.api.last["lines"], 25)
        self.assertGreater(len(fake.requests), 1)
        for before, after in zip(fake.requests, fake.requests[1:]):
            self.assertEqual(after[:len(before)], before, "a worker only appends to its conversation")
        self.assertEqual(fake.requests[1][1]["content"][0]["type"], "thinking", "thinking blocks go back unchanged")
        self.assertEqual(self.service.api.total["requests"], len(fake.requests))
        self.assertAlmostEqual(self.service.api.total["usd"], len(fake.requests) * api.cost({"input_tokens": 1000, "output_tokens": 100}))

    def test_without_credit_the_api_pauses_and_the_subagent_gets_the_job(self):
        self.note(STATUS_AT)
        api.save_key("sk-ant-test")

        def no_credit(key, messages):
            raise api.ApiError("no credit", "HTTP 402: Your credit balance is too low to access the Anthropic API.")
        self.compact(no_credit)
        self.assertEqual(self.service.api.last["result"], "paused: no credit")
        self.assertEqual(self.service.board.leases, {})
        self.assertEqual(self.service.board.failures, {}, "a missing credit says nothing about the messages")
        self.assertFalse(self.service.api.ready("sk-ant-test"))
        self.assertTrue(self.service.api.ready("sk-ant-test", forced=True))
        self.assertTrue(self.service.api.ready("sk-ant-other"), "a new key is tried at once")
        self.assertEqual(self.service.call("api_status", {})["status"], "paused")
        self.service.api_enabled = True
        self.assertEqual(self.service.call("status_line", {})["text"], f"OptChat: {STATUS_AT} to summarize, API: no credit")
        self.assertEqual(self.service.call("compact_next", {})["status"], "claimed")

    def test_a_missing_task_is_asked_again(self):
        self.note(3)
        fake = FakeApi(skip_once=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.compact(fake)
        self.assertEqual(self.service.board.failures, {})
        self.assertEqual(self.service.api.last["lines"], 3)
        self.assertIn("no line for task 1", fake.requests[1][-1]["content"])

    def test_an_empty_line_releases_only_its_task(self):
        self.note(3)
        self.compact(FakeApi(empty_first=True))
        self.assertEqual(len(self.service.board.failures), 1)
        self.assertEqual(self.service.api.last["result"], "waiting", "the released task waits for a retry")
        self.assertEqual(self.service.api.last["lines"], 2)

    def test_a_refusal_releases_the_job(self):
        self.note(2)
        self.compact(lambda key, messages: {**answer([]), "stop_reason": "refusal", "stop_details": {"category": "cyber"}})
        self.assertEqual(self.service.board.failures.get((0, 0)), "The API declined the job (cyber).")

    def test_the_service_starts_only_when_enough_waits_or_after_a_quiet_time(self):
        now = [1000.0]
        worker = api.ApiCompactor(Path(self.temp.name), clock=lambda: now[0])
        self.assertTrue(worker.due(50, api.API_AT))
        self.assertFalse(worker.due(51, 1))
        now[0] += api.IDLE
        self.assertTrue(worker.due(51, 1))
        self.assertFalse(worker.due(52, 0))

    def test_compact_runs_the_api_now_when_the_command_asks(self):
        self.note(12)
        api.save_key("sk-ant-test")
        self.service.api_enabled = True
        self.service.api.ask = FakeApi()

        async def scenario():
            loop = self.service.summarize()
            task = asyncio.create_task(loop)
            started = self.service.call("api_compact", {})
            self.assertEqual(started["target"], 1)
            for _ in range(500):
                await asyncio.sleep(0.01)
                if self.service.api.run == 1 and not self.service.api.running:
                    break
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())
        self.assertEqual(self.service.api.last["result"], "done")
        self.assertEqual(self.service.memory.first(), self.service.memory.total)


class Errors(unittest.TestCase):
    def test_http_errors_map_to_reasons(self):
        self.assertEqual(api.classify(402, b'{"error":{"type":"billing_error","message":"x"}}').kind, "no credit")
        self.assertEqual(api.classify(400, b'{"error":{"type":"invalid_request_error","message":"Your credit balance is too low"}}').kind, "no credit")
        self.assertEqual(api.classify(401, b"{}").kind, "key rejected")
        self.assertEqual(api.classify(404, b"{}").kind, "access denied")
        limited = api.classify(429, b"{}", "30")
        self.assertEqual((limited.kind, limited.retry_after), ("rate limited", 30.0))
        self.assertEqual(api.classify(529, b"not json").kind, "unreachable")
        self.assertEqual(api.classify(400, b'{"error":{"message":"bad field"}}').kind, "error")

    def test_cost_uses_the_rates_of_haiku(self):
        self.assertAlmostEqual(api.cost({"input_tokens": 100_000, "output_tokens": 10_000}), 0.01 + 0.005)
        self.assertAlmostEqual(api.cost({"input_tokens": 0, "cache_read_input_tokens": 1_000_000, "output_tokens": 0}), 0.50 * 0.1, msg="a prompt over 100K tokens costs more")


class KeyCommand(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"OPTCHAT_CONFIG": str(Path(self.temp.name) / "config.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_command(self, key, checker=lambda key: None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = api.key_command(browser=False, read=lambda prompt: key, checker=checker)
        return status, out.getvalue()

    def test_a_working_key_is_saved_for_the_user_only(self):
        status, text = self.run_command("sk-ant-api03-good")
        self.assertEqual(status, 0)
        self.assertIn(api.KEY_URL, text)
        self.assertNotIn("sk-ant-api03-good", text)
        self.assertEqual(api.load_key(), "sk-ant-api03-good")
        self.assertEqual(stat.S_IMODE(api.key_path().stat().st_mode), 0o600)

    def test_a_rejected_or_foreign_key_changes_nothing(self):
        def rejected(key):
            raise api.ApiError("key rejected", "HTTP 401: invalid x-api-key")
        self.assertEqual(self.run_command("sk-ant-bad", rejected)[0], 1)
        self.assertEqual(self.run_command("not-a-key")[0], 1)
        self.assertIsNone(api.load_key())

    def test_a_key_without_credit_is_kept_for_later(self):
        def no_credit(key):
            raise api.ApiError("no credit", "HTTP 402: no credit")
        status, text = self.run_command("sk-ant-empty", no_credit)
        self.assertEqual(status, 0)
        self.assertIn(api.BILLING_URL, text)
        self.assertEqual(api.load_key(), "sk-ant-empty")


class CompactCommand(unittest.TestCase):
    class Client:
        def __init__(self, started, status):
            self.started, self.status = started, status

        def call(self, method, args=None):
            return self.started if method == "api_compact" else self.status

    def run_command(self, client):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = api.compact_command(client, "/repo/run", sleep=lambda s: None)
        return status, out.getvalue()

    def test_the_command_tells_the_agent_when_to_start_the_subagent(self):
        status, text = self.run_command(self.Client({"status": "no key"}, None))
        self.assertEqual(status, 3)
        self.assertIn("/repo/run api-key", text)
        paused = {"run": 1, "running": False, "kind": "no credit", "problem": "no credit: HTTP 402", "last_run": {"result": "paused: no credit", "lines": 0, "usd": 0.0}}
        status, text = self.run_command(self.Client({"status": "ready", "target": 1}, paused))
        self.assertEqual(status, 3)
        self.assertIn(api.BILLING_URL, text)
        done = {"run": 1, "running": False, "last_run": {"result": "done", "lines": 12, "usd": 0.0021}}
        self.assertEqual(self.run_command(self.Client({"status": "ready", "target": 1}, done)), (0, "OptChat API: done, 12 lines, about $0.0021.\n"))
        blocked = {**done, "last_run": {"result": "blocked", "lines": 0, "usd": 0.0}, "blocked": {"6579+1": "The API worker could not write a faithful line."}}
        status, text = self.run_command(self.Client({"status": "ready", "target": 1}, blocked))
        self.assertEqual(status, 0)
        self.assertIn("""/repo/run call compact_resume '{"id": 6579, "n": 1}'""", text)


if __name__ == "__main__":
    unittest.main()


class SharedKey(unittest.TestCase):
    """A key that the user stores on one machine reaches the others through the shared folder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"OPTCHAT_CONFIG": str(self.root / "unused/config.json")})
        self.env.start()
        self.now = [1_800_000_000.0]
        self.a, self.b = self.start("alpha"), self.start("beta")

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.env.stop()
        self.temp.cleanup()

    def start(self, name):
        config = {"machine": name, "exchange": {"type": "dir", "path": str(self.root / "shared")}, "join_grace": 0}
        service = Service(self.root / name, config=config, clock=lambda: self.now[0])
        service.api.key_file = self.root / name / "key"
        return service

    def rounds(self, *services):
        for service in services:
            asyncio.run(service.exchange())

    def shared(self, service):
        return self.root / "shared/machines" / service.replica.machine

    def test_a_key_stored_on_one_machine_reaches_the_other_and_a_removal_too(self):
        api.save_key("sk-ant-from-alpha", self.a.api.key_file)
        self.assertEqual(self.a.call("api_key_changed", {}), {"shared": True})
        self.rounds(self.a, self.a, self.b)
        self.assertEqual(self.b.api.load_key(), "sk-ant-from-alpha")
        self.assertEqual(stat.S_IMODE(self.b.api.key_file.stat().st_mode), 0o600)
        self.assertEqual(self.b.call("api_status", {})["key_from"], self.a.replica.machine)
        for service in (self.a, self.b):
            self.assertNotIn("sk-ant", (self.shared(service) / "heartbeat.json").read_text(), "a heartbeat never holds the key")
        self.now[0] += 60
        api.remove_key(self.b.api.key_file)
        self.b.call("api_key_changed", {})
        self.rounds(self.b, self.b, self.a)
        self.assertIsNone(self.a.api.load_key())
        self.assertFalse((self.shared(self.a) / api.SHARED).exists(), "the older shared key is deleted")
        self.rounds(self.a, self.b)
        self.assertIsNone(self.b.api.load_key(), "an older change never comes back")

    def test_only_the_first_ready_machine_runs_the_api(self):
        for service in (self.a, self.b):
            api.save_key("sk-ant-same", service.api.key_file)
        self.rounds(self.a, self.b, self.a)
        first, second = sorted((self.a, self.b), key=lambda s: s.replica.machine)
        self.assertTrue(first.api_turn())
        self.assertFalse(second.api_turn())
        first.api.pause(api.ApiError("unreachable", "no network"), "sk-ant-same")
        self.rounds(first, second)
        self.assertTrue(second.api_turn(), "the next machine takes over while the first one is paused")
