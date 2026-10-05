import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat.memory import Part
from optchat.replica import RcloneExchange, iso
from optchat.service import Service


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def read_all(board, job):
    offset = 0
    while offset is not None:
        offset = board.read(job, offset)["next_offset"]


class TwoMachines(unittest.TestCase):
    """Two machines sharing one folder, no owner. Dates follow the test clock."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.machines = {}
        self.a = self.start("alpha")
        self.b = self.start("beta")
        self.sync(self.a, self.b, self.a)

    def tearDown(self):
        for service in self.machines.values():
            service.close()
        self.temp.cleanup()

    def start(self, name, offline_after=600, join_grace=0):
        config = {"machine": name, "exchange": {"type": "dir", "path": str(self.root / "shared")}, "offline_after": offline_after, "join_grace": join_grace}
        service = Service(self.root / name, config=config, clock=self.clock)
        self.machines[name] = service
        return service

    def restart(self, name):
        self.machines.pop(name).close()
        return self.start(name)

    def sync(self, *services):
        for service in services:
            service.replica.sync_once()

    def say(self, service, event, text, at):
        return service.call("append", {"event_id": event, "kind": "user", "text": text, "date": iso(self.clock.t + at)})

    def order(self, service):
        return [m.text for m in service.store.root]

    def test_online_machines_agree_on_one_order(self):
        for at, (service, text) in enumerate([(self.a, "a1"), (self.b, "b1"), (self.a, "a2"), (self.b, "b2")], 1):
            result = self.say(service, text, text, at)
            self.assertFalse(result["ordered"], "must wait for the other machine's check-in")
        self.clock.t += 10
        self.sync(self.a, self.b, self.a)
        self.assertEqual(self.order(self.a), ["a1", "b1", "a2", "b2"])
        self.assertEqual(self.order(self.b), self.order(self.a))
        self.assertEqual([m.gid for m in self.a.store.root], [m.gid for m in self.b.store.root])

    def test_summary_written_on_one_machine_is_reused_on_the_other(self):
        for at, service in enumerate([self.a, self.b, self.a, self.b], 1):
            self.say(service, f"m{at}", f"message {at} " + "x" * 700, at)
        self.clock.t += 10
        self.sync(self.a, self.b, self.a)
        jobs = 0
        while (claim := self.a.board.next())["status"] == "claimed":
            read_all(self.a.board, claim["job"])
            self.a.board.submit(claim["job"], f"user: summary {jobs} " + "s" * 300)
            jobs += 1
        self.assertGreater(jobs, 4)
        self.sync(self.a, self.b)
        self.assertEqual(self.b.board.next()["status"], "done", "beta reuses alpha's summaries instead of new jobs")
        self.assertEqual({k: n.text for k, n in self.b.store.tree.items()}, {k: n.text for k, n in self.a.store.tree.items()})
        self.assertEqual(self.b.call("view", {})["status"], "ready")

    def test_offline_machine_is_skipped_then_rejoins(self):
        self.say(self.a, "a1", "a1", 1)
        self.say(self.b, "b1", "b1", 300)  # beta is cut off from the shared folder
        self.assertEqual(self.order(self.a), [])
        self.clock.t += 700  # past offline_after for both
        self.sync(self.a)
        self.assertEqual(self.order(self.a), ["a1"], "alpha stops waiting for silent beta")
        self.b.replica.seal()
        self.assertEqual(self.order(self.b), ["b1"], "beta keeps working offline")
        self.sync(self.b, self.a)  # beta reconnects
        self.assertEqual(self.order(self.a), ["a1", "b1"])
        self.assertEqual(self.order(self.b), ["b1", "a1"], "what arrives late is appended")
        self.say(self.a, "a2", "a2", 10)
        self.say(self.b, "b2", "b2", 20)
        self.clock.t += 30
        self.sync(self.a, self.b, self.a)
        self.assertEqual(self.order(self.a)[2:], ["a2", "b2"])
        self.assertEqual(self.order(self.b)[2:], ["a2", "b2"])
        self.assertEqual(self.a.replica.key(Part(1, 1)), self.b.replica.key(Part(1, 1)), "orders realign after the late stretch")
        self.assertNotEqual(self.a.replica.key(Part(1, 0)), self.b.replica.key(Part(1, 0)))

    def test_unordered_and_received_messages_survive_restart(self):
        self.say(self.a, "a1", "a1", 1)
        self.say(self.b, "b1", "b1", 2)
        self.sync(self.b)
        self.a = self.restart("alpha")
        self.assertEqual(len(self.a.replica.unordered()), 1)
        self.sync(self.a)  # receives b1; beta's watermark has not passed a1 yet
        self.a = self.restart("alpha")
        self.assertEqual(len(self.a.replica.unordered()), 2)
        self.clock.t += 5
        self.sync(self.b, self.a)
        self.assertEqual(self.order(self.a), ["a1", "b1"])
        self.assertEqual(self.a.replica.machine, self.machines["alpha"].replica.machine)

    def test_damaged_batch_blocks_only_until_offline_timeout(self):
        beta_dir = self.root / "shared" / "machines" / self.b.replica.machine / "messages"
        beta_dir.mkdir(parents=True, exist_ok=True)
        (beta_dir / "000000000000-000000000000.jsonl.gz").write_bytes(b"not gzip")
        self.say(self.b, "b1", "b1", 1)
        self.say(self.a, "a1", "a1", 2)
        self.clock.t += 5
        # Beta's heartbeat claims a message alpha cannot read: alpha must not skip it.
        self.b.replica.state["uploaded_seq"] = 1
        self.b.replica.exchange.write(f"machines/{self.b.replica.machine}/heartbeat.json",
                                      ('{"updated":"%s","watermark":"%s","seq":1,"sum":0}' % (iso(self.clock.t), iso(self.clock.t))).encode())
        self.sync(self.a)
        self.assertIn("error", self.a.call("status", {})["replication"]["machines"][self.b.replica.machine])
        self.assertEqual(self.order(self.a), [])
        self.clock.t += 700
        self.b.replica.exchange.write(f"machines/{self.b.replica.machine}/heartbeat.json",
                                      ('{"updated":"%s","watermark":"%s","seq":1,"sum":0}' % (iso(self.clock.t), iso(self.clock.t))).encode())
        self.sync(self.a)
        self.assertEqual(self.order(self.a), ["a1"], "a stuck peer stops blocking after offline_after")

    def test_new_machines_agree_even_when_they_record_before_first_sync(self):
        gamma, delta = self.start("gamma"), self.start("delta")
        self.say(gamma, "g1", "g1", 1)
        self.say(delta, "d1", "d1", 2)
        self.say(gamma, "g2", "g2", 3)
        for service in (gamma, delta, self.a, self.b, gamma, delta, self.a, self.b, gamma):
            self.clock.t += 1
            self.sync(service)
        self.assertEqual(sorted(self.order(gamma)), ["d1", "g1", "g2"])
        for service in (delta, self.a, self.b):
            self.assertEqual(self.order(service), self.order(gamma))

    def test_machine_noticed_late_cannot_reorder_what_others_placed(self):
        self.clock.t += 700  # alpha and beta go silent and stop counting
        gamma, delta = self.start("gamma", join_grace=5), self.start("delta", join_grace=5)
        self.sync(gamma)
        self.say(gamma, "g1", "g1", 1)
        self.clock.t += 2
        self.sync(delta)  # delta appears; gamma has not noticed it yet
        self.say(delta, "d1", "d1", 0.5)
        self.clock.t += 1
        self.say(gamma, "g2", "g2", 0)
        self.assertEqual(self.order(gamma), ["g1", "g2"], "gamma placed both before it knew delta")
        for _ in range(6):
            self.clock.t += 1
            self.sync(gamma, delta)
        self.assertEqual(self.order(delta), ["g1", "g2", "d1"])
        self.assertEqual(self.order(gamma), self.order(delta))

    def test_origin_names_the_machine(self):
        self.say(self.a, "a1", "a1", 1)
        self.clock.t += 5
        self.sync(self.a, self.b, self.a)
        self.assertEqual(self.b.store.root[0].origin["machine"], "alpha")
        self.assertIn("@alpha", self.b.store.root[0].compact_source)


if __name__ == "__main__":
    unittest.main()


class RcloneWrites(unittest.TestCase):
    def test_overwrite_is_forced_even_when_sizes_match(self):
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            class Done: returncode, stdout, stderr = 0, b"", b""
            return Done()
        with patch("optchat.replica.subprocess.run", fake_run):
            RcloneExchange("remote:optchat", rclone="rclone").write("machines/m-000000/heartbeat.json", b"{}")
        self.assertEqual(calls[0][:2], ["rclone", "rcat"])
        self.assertEqual(calls[1][:3], ["rclone", "moveto", "--ignore-times"])
        self.assertTrue(calls[1][-1].endswith("heartbeat.json"))
