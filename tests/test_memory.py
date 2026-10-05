import asyncio
import json
import os
import random
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from optchat.jobs import SCALE
from optchat.memory import Memory, Part
from optchat.storage import StorageError, Store
from optchat.util import NODE, PLACEHOLDER, cap, chunks, cut_bytes, size


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.reports = []
        self.store = Store(self.path, self.reports.append)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class StorageTests(Fixture, unittest.TestCase):
    def test_fsync_one_write_and_unicode(self):
        real_write, real_sync = os.write, os.fsync
        with patch("optchat.storage.os.write", wraps=real_write) as write, patch("optchat.storage.os.fsync", wraps=real_sync) as sync:
            message = self.store.append("user", "Здравствуйте 🌲\nnext")
            self.assertEqual(write.call_count, 1)
            self.assertGreaterEqual(sync.call_count, 1)
        self.assertEqual(message.size, size(message.source))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.root[0], message)

    def test_second_writer_is_rejected_and_lock_inode_survives(self):
        inode = (self.path / "lock").stat().st_ino
        with self.assertRaises(StorageError):
            Store(self.path)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual((self.path / "lock").stat().st_ino, inode)

    def test_torn_tail_skipped_and_terminated_before_append(self):
        self.store.append("user", "one")
        file = next((self.path / "main").glob("*.jsonl"))
        with file.open("ab") as f:
            f.write(b'{"i":1,"kind":"user","text":"torn')
        self.store.close()
        self.store = Store(self.path, self.reports.append)
        self.assertEqual(len(self.store.root), 1)
        self.assertTrue(file.read_bytes().endswith(b"\n"))
        self.store.append("user", "two")
        self.store.close()
        self.store = Store(self.path, self.reports.append)
        self.assertEqual([m.text for m in self.store.root], ["one", "two"])
        self.assertTrue(self.reports)

    def test_valid_line_without_newline_is_kept(self):
        m = self.store.append("user", "kept")
        file = next((self.path / "main").glob("*.jsonl"))
        file.write_text(json.dumps(asdict(m)))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.root, [m])

    def test_ids_sorted_independently_of_filename(self):
        a = self.store.append("user", "a")
        b = self.store.append("user", "b")
        file = next((self.path / "main").glob("*.jsonl"))
        file.write_text(json.dumps(asdict(b)) + "\n")
        (file.parent / "9999-01-01.jsonl").write_text(json.dumps(asdict(a)) + "\n")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual([m.i for m in self.store.root], [0, 1])

    def test_gap_never_renumbers(self):
        self.store.append("user", "a")
        m = self.store.append("user", "b")
        file = next((self.path / "main").glob("*.jsonl"))
        file.write_text('broken\n' + json.dumps(asdict(m)) + '\n')
        self.store.close()
        with self.assertRaisesRegex(StorageError, "Missing message 0"):
            Store(self.path, self.reports.append)

    def test_short_write_poisoning_prevents_further_appends(self):
        with patch("optchat.storage.os.write", return_value=1):
            with self.assertRaises(StorageError):
                self.store.append("user", "a")
        self.assertEqual(self.store.root, [])
        with self.assertRaises(StorageError):
            self.store.append("user", "b")

    def test_reasoning_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.store.append("thought", "private")

    def test_parent_needs_children_and_nodes_are_immutable(self):
        self.store.append("user", "a")
        self.store.append("user", "b")
        with self.assertRaises(ValueError):
            self.store.save_node(1, 0, "parent")
        self.store.save_node(0, 0, "a")
        self.store.save_node(0, 1, "b")
        self.store.save_node(1, 0, "parent")
        with self.assertRaises(StorageError):
            self.store.save_node(1, 0, "changed")


class ViewTests(Fixture, unittest.TestCase):
    def make_complete(self, n):
        for i in range(n):
            self.store.append("user", f"Message {i}")
            self.store.save_node(0, i, str(i).zfill(3) + ":" + "x" * 20)
        for l in range(1, n.bit_length()):
            for i in range(n // 2**l):
                self.store.save_node(l, i, f"summary {l}/{i}".ljust(24, "x"))

    def test_replay_tiles_entire_history_with_bounded_view(self):
        self.make_complete(127)
        memory = Memory(self.store, budget=300)
        self.assertLessEqual(memory.bytes, 300)
        self.assertEqual(memory.view[0].start, 0)
        self.assertEqual(memory.view[-1].end, 127)
        for a, b in zip(memory.view, memory.view[1:]):
            self.assertEqual(a.end, b.start)
        self.assertEqual(Memory(self.store, 300).view, memory.view)

    def test_largest_due_pair_wins(self):
        self.make_complete(8)
        m = Memory(self.store, budget=192)
        self.assertEqual(len(m.view), 8)
        m.budget = 168
        m.fit()
        self.assertEqual(m.view[0], Part(1, 0))
        self.assertEqual(len(m.view), 7)

    def test_big_unbuilt_message_never_enters_view(self):
        m = Memory(self.store, 1000)
        m.append("user", "SECRET" * 100000)
        self.assertEqual(m.bytes, size(PLACEHOLDER))
        self.assertNotIn("SECRET", m.render())
        with self.assertRaises(RuntimeError):
            m.render(strict=True)
        self.assertIn("SECRET" * 100000, m.zoom(0, 1))

    def test_no_split_when_budget_increases(self):
        self.make_complete(32)
        m = Memory(self.store, 100)
        before = m.view.copy()
        m.budget = 100000
        m.fit()
        self.assertEqual(m.view, before)

    def test_no_merge_until_parent_built(self):
        m = Memory(self.store, 1)
        m.append("user", "a")
        m.append("user", "b")
        for i in (0, 1):
            self.store.save_node(0, i, "summary")
            m.node_built(Part(0, i))
        self.assertEqual(len(m.view), 2)
        self.store.save_node(1, 0, "p")
        m.node_built(Part(1, 0))
        self.assertEqual(m.view, [Part(1, 0)])

    def test_zoom_address_validation_and_date(self):
        self.make_complete(8)
        m = Memory(self.store)
        self.assertIn("0+2|", m.zoom(0, 4))
        self.assertEqual(m.zoom(0, 1), "0+0|user: Message 0")
        for id, n in [(-1, 1), (1, 2), (0, 3), (0, 0), (0, 16), (True, 1)]:
            self.assertTrue(m.zoom(id, n).startswith("No line"))
        self.assertIn("20", m.date(0))

    def test_page_reconstruction_preserves_all_unicode(self):
        m = Memory(self.store)
        text = "😀\nquote\" " * 8000
        m.append("user", text)
        offset, result = 0, []
        while offset is not None:
            page = m.read_message(0, offset)
            self.assertLess(len(page), 30000)
            header, part = page.split("\n", 1)
            result.append(part)
            offset = json.loads(header)["next_offset"]
        self.assertEqual("".join(result), "user: " + text)


class UtilityTests(unittest.TestCase):
    def test_utf8_cut_never_splits_character(self):
        for i in range(20):
            cut = cut_bytes("😀aé�😀", i)
            self.assertLessEqual(size(cut), i)
            self.assertTrue("😀aé�😀".startswith(cut))

    def test_scale_and_cap(self):
        self.assertEqual(size(SCALE), NODE)
        self.assertEqual(cap("short"), "short")
        result = cap("head" + "😀" * 40000 + "tail")
        self.assertEqual(len(result), 30000)
        self.assertTrue(result.startswith("head"))
        self.assertTrue(result.endswith("tail"))
        self.assertIn("omitted", result)

    def test_cache_chunks_are_lossless_and_stable(self):
        text = "line\n" * 30000
        pieces = chunks(text)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(len(pieces), 4)
        self.assertEqual(pieces[:3], chunks(text + "new\n")[:3])
