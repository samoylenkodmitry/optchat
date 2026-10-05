"""Regression cases from the Codex/Claude implementation review."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from optchat.hooks import enqueue
from optchat.jobs import JobBoard, SOURCE_CHUNK
from optchat.memory import Memory
from optchat.service import Service, endpoint
from optchat.storage import Store
from optchat.util import size
from .test_memory import Fixture


def read_all(board, token):
    offset, pages = 0, []
    while offset is not None:
        page = board.read(token, offset)
        pages.append(page['text'])
        offset = page['next_offset']
    return ''.join(pages)


class ReviewJobs(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.board = JobBoard(Memory(self.store))

    def test_context_is_incremental_and_exact_after_merges(self):
        for n in range(20):
            self.board.append('user', f'message {n} ' + 'x' * 1000)
        worker, retained, context_chars = None, {}, 0
        for step in range(100):
            response = self.board.next(worker)
            worker = response['worker']
            if response['status'] == 'done':
                break
            self.assertEqual(response['status'], 'claimed')
            job = self.board.leases[response['job']]
            prompt = read_all(self.board, job.token)
            update_text = prompt.split('Context update:\n')[1].split('\n\n')[0]
            update = json.loads(update_text)
            for key in update['remove']:
                del retained[key]
            retained.update(update['add'])
            self.assertEqual(retained, job.context)
            if step:
                self.assertNotIn('For scale, this line is exactly', prompt)
            context_chars += len(update_text)
            self.board.submit(job.token, 'user: summary ' + 's' * 300)
        else:
            self.fail('compaction never drained')
        self.assertLess(context_chars, 30_000)
        self.assertEqual(len(self.store.tree), 38)

    def test_worker_rotates_before_next_prompt_exceeds_budget(self):
        for _ in range(4):
            self.board.append('user', 'x' * 2000)
        first = self.board.next()
        self.board.worker_budget = first['prompt_characters'] + 150
        read_all(self.board, first['job'])
        self.board.submit(first['job'], 'user: long text')
        second = self.board.next(first['worker'])
        self.assertEqual(second['status'], 'rotate')
        fresh = self.board.next()
        self.assertEqual(fresh['status'], 'claimed')
        self.assertNotEqual(first['worker'], fresh['worker'])
        self.assertIn('For scale', read_all(self.board, fresh['job']))

    def test_large_message_segments_resume_after_restart_without_original_loss(self):
        original = 'start:' + '😀x' * SOURCE_CHUNK + ':end'
        self.board.append('user', original)
        first = self.board.next()
        prompt = read_all(self.board, first['job'])
        self.assertLess(len(prompt), SOURCE_CHUNK + 6000)
        self.assertIn('start:', prompt)
        result = self.board.submit(first['job'], 'user: initial segment ' + 's' * 280)
        self.assertEqual(result['status'], 'progress_saved')
        self.assertNotIn((0, 0), self.store.tree)
        self.board = JobBoard(Memory(self.store))
        offset = self.board.state['0:0']['progress']['offset']
        self.assertEqual(offset, SOURCE_CHUNK)
        saw_end = False
        for _ in range(10):
            item = self.board.next()
            if item['status'] == 'done': break
            prompt = read_all(self.board, item['job'])
            saw_end |= ':end' in prompt
            self.board.submit(item['job'], 'user: segment summarized ' + 't' * 280)
        self.assertTrue(saw_end)
        self.assertIn((0, 0), self.store.tree)
        self.assertEqual(self.store.root[0].text, original)
        self.assertEqual(self.board.state, {})

    def test_failed_node_stays_blocked_across_restart_and_can_be_resumed(self):
        clock = [0.0]
        self.board.clock = lambda: clock[0]
        self.board.append('user', 'x' * 1000)
        for _ in range(3):
            job = self.board.next()
            self.board.release(job['job'], 'Cannot faithfully summarize')
            clock[0] += 11
        self.assertEqual(self.board.next()['status'], 'blocked')
        self.board = JobBoard(Memory(self.store))
        self.assertEqual(self.board.next()['status'], 'blocked')
        self.board.resume(0, 1)
        job = self.board.next()
        read_all(self.board, job['job'])
        self.board.submit(job['job'], 'user: actual reviewed summary')
        self.assertEqual(self.board.next()['status'], 'done')

    def test_known_nonfree_source_is_not_reencoded_on_each_append(self):
        self.board.append('user', 'x' * 30000)
        with patch('optchat.jobs.size', wraps=size) as encoding:
            for _ in range(50):
                self.board.append('user', 'x' * 30000)
        self.assertEqual(encoding.call_count, 0)  # Only frontier leaf is eligible.

    def test_backlog_rebuild_does_not_reencode_every_prior_placeholder(self):
        # Writing 5k small records is cheap; this counts algorithmic work rather
        # than enforcing a machine-dependent timing assertion.
        for _ in range(5000):
            self.store.append('user', 'pending')
        with patch('optchat.memory.size', wraps=size) as encoding:
            memory = Memory(self.store)
        self.assertLessEqual(encoding.call_count, 5001)
        self.assertEqual(memory.bytes, 5000 * size('(not summarized yet: zoom it)'))


class ReviewService(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.service = Service(self.path)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_origin_survives_delivery_restart_zoom_and_compaction(self):
        origin = {'project': '/projects/alpha', 'session': 'a1', 'agent': 'codex'}
        args = {'event_id': 'a', 'kind': 'user', 'text': 'x' * 1000, 'origin': origin}
        self.service.call('append', args)
        self.service.close()
        self.service = Service(self.path)
        self.assertEqual(self.service.call('append', args)['id'], 0)
        self.assertEqual(self.service.store.root[0].origin, origin)
        self.assertIn('/projects/alpha', self.service.call('zoom', {'id': 0, 'n': 1})['text'])
        claim = self.service.call('compact_next', {})
        self.assertIn('/projects/alpha', read_all(self.service.board, claim['job']))
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.service.call('append', {**args, 'origin': {**origin, 'project': '/projects/beta'}})

    def test_replacement_on_same_connection_gets_fresh_identity_and_pages(self):
        self.service.call('append', {'event_id': 'a', 'kind': 'user', 'text': 'x' * 1000})
        first = self.service.call('compact_next', {}, client='shared')
        read_all(self.service.board, first['job'])
        second = self.service.call('compact_next', {}, client='shared')
        self.assertNotEqual(first['worker'], second['worker'])
        self.assertEqual(second['status'], 'waiting')
        self.service.board.clock = lambda: self.service.board.leases[first['job']].expires + 1 if first['job'] in self.service.board.leases else 1e9
        second = self.service.call('compact_next', {}, client='shared')
        if second['status'] == 'waiting':
            self.service.board.clock = lambda: 1e10
            second = self.service.call('compact_next', {}, client='shared')
        with self.assertRaisesRegex(ValueError, 'complete prompt'):
            self.service.call('compact_submit', {'job': second['job'], 'line': 'user: text'})

    def test_partial_snapshot_preserves_completed_knowledge_and_pending_range(self):
        self.service.call('append', {'event_id': 'a', 'kind': 'user', 'text': 'durable decision'})
        self.service.call('append', {'event_id': 'b', 'kind': 'user', 'text': 'unread detail ' * 100})
        view = self.service.call('view', {})
        self.assertEqual(view['status'], 'partial')
        self.assertEqual(view['pending'], {'start': 1, 'count': 1})
        self.assertIn('durable decision', view['text'])
        self.assertNotIn('unread detail', view['text'])
        self.service.call('append', {'event_id': 'c', 'kind': 'user', 'text': 'new'})
        same = self.service.call('view', {'snapshot': view['snapshot'], 'offset': 0})
        self.assertEqual(same['pending'], view['pending'])

    def test_spool_replay_survives_restart_and_lost_ack_without_duplicates(self):
        enqueue(self.path, {'hook_event_name': 'SessionStart', 'session_id': 'main'})
        event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'main', 'prompt_id': 'p1', 'prompt': 'persist me', 'cwd': '/alpha', 'thinking': 'never serialize'}
        filename = enqueue(self.path, event)
        self.assertNotIn('never serialize', (self.path / 'spool' / filename).read_text())
        self.service.close()
        self.service = Service(self.path)
        self.assertEqual(self.service.call('status', {})['messages'], 1)
        enqueue(self.path, event)
        self.assertEqual(self.service.call('status', {})['messages'], 1)
        self.assertEqual(self.service.store.root[0].origin['project'], '/alpha')
        self.assertFalse(list((self.path / 'spool').glob('*.json')))

    def test_socket_path_does_not_depend_on_app_tmpdir(self):
        with patch.dict('os.environ', {'TMPDIR': '/different-app-environment'}):
            self.assertEqual(endpoint(self.path), self.service.path)

    def test_failed_delivery_is_never_acknowledged_by_retry(self):
        payload = {'event_id': 'a', 'kind': 'user', 'text': 'recover me', 'origin': {'project': '/alpha'}}
        with patch.object(self.service.replica, 'record', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError): self.service.call('append', payload)
        with self.assertRaisesRegex(RuntimeError, '(?i)restart'): self.service.call('append', payload)
        self.service.close()
        self.service = Service(self.path)
        self.assertEqual(self.service.call('append', payload)['id'], 0)
        self.assertEqual(self.service.store.root[0].origin, {'project': '/alpha'})

    def test_bad_spool_event_is_retained_without_blocking_new_events(self):
        enqueue(self.path, {'hook_event_name': 'SessionStart', 'session_id': 's'})
        base = {'hook_event_name': 'UserPromptSubmit', 'session_id': 's', 'event_id': 'same', 'prompt': 'first'}
        enqueue(self.path, base)
        enqueue(self.path, {**base, 'prompt': 'conflict'})
        enqueue(self.path, {**base, 'event_id': 'next', 'prompt': 'later'})
        with patch('sys.stderr'):
            status = self.service.call('status', {})
        self.assertEqual([m.text for m in self.service.store.root], ['first', 'later'])
        self.assertEqual(status['queued_hook_events'], 0)
        self.assertEqual(status['rejected_hook_events'], 1)

    def test_repeated_stop_for_one_prompt_preserves_each_distinct_reply(self):
        enqueue(self.path, {'hook_event_name': 'SessionStart', 'session_id': 's'})
        base = {'hook_event_name': 'Stop', 'session_id': 's', 'prompt_id': 'p'}
        for text in ['first reply', 'continued reply', 'continued reply']:
            enqueue(self.path, {**base, 'last_assistant_message': text})
        self.service.call('status', {})
        self.assertEqual([m.text for m in self.service.store.root], ['first reply', 'continued reply'])

    def test_snapshot_reused_and_active_old_version_not_evicted(self):
        self.service.call('append', {'event_id': 'first', 'kind': 'user', 'text': 'before'})
        original = self.service.call('view', {})
        same = self.service.call('view', {})
        self.assertEqual(original['snapshot'], same['snapshot'])
        for n in range(20):
            self.service.call('append', {'event_id': str(n), 'kind': 'user', 'text': 'later'})
            self.service.call('view', {})
        old = self.service.call('view', {'snapshot': original['snapshot'], 'offset': 0})
        self.assertEqual(old['text'], original['text'])

    def test_view_caps_completed_backlog_and_marks_missing_range(self):
        self.service.memory.budget = 800
        for n in range(10):
            self.service.call('append', {'event_id': str(n), 'kind': 'user', 'text': str(n) + 'x' * 300})
        view = self.service.call('view', {})
        self.assertLessEqual(size(view['text']), 800)
        self.assertEqual(view['status'], 'partial')
        self.assertEqual(view['pending'], {'start': 10, 'count': 0})
        self.assertEqual(view['omitted']['count'], 8)

    def test_hook_result_has_call_identity_and_project_root(self):
        root = self.path / 'repo'
        (root / '.git').mkdir(parents=True)
        (root / 'src').mkdir()
        self.service.call('hook', {'hook_event_name': 'SessionStart', 'session_id': 's'})
        for n in ('b', 'a'):
            self.service.call('hook', {'hook_event_name': 'PostToolUse', 'session_id': 's', 'tool_use_id': n,
                                      'tool_name': 'Read', 'tool_response': f'result {n}', 'cwd': str(root / 'src')})
        self.assertIn('Read [call=b]', self.service.store.root[0].text)
        self.assertEqual(self.service.store.root[0].origin['project'], str(root.resolve()))

    def test_fully_summarized_view_keeps_newest_lines_near_text_budget(self):
        self.service.memory.budget = 600
        for n in range(2):
            self.service.call('append', {'event_id': str(n), 'kind': 'user', 'text': str(n) + 'x' * 289})
        self.assertEqual(self.service.memory.bytes, 592)
        view = self.service.call('view', {})
        self.assertEqual(view['status'], 'ready')
        self.assertEqual(view['omitted']['count'], 0)
        self.assertIn('1+1|user: 1', view['text'])

    def test_interruptions_do_not_count_as_source_failures(self):
        self.service.call('append', {'event_id': 'one', 'kind': 'user', 'text': 'x' * 1000})
        clock = [0.0]
        self.service.board.clock = lambda: clock[0]
        for _ in range(5):
            response = self.service.call('compact_next', {})
            self.assertEqual(response['status'], 'claimed')
            clock[0] += 301
        self.assertEqual(self.service.call('compact_next', {})['status'], 'claimed')
        self.assertEqual(self.service.board.failures, {})

    def test_compact_tags_do_not_repeat_long_origin_fields(self):
        self.service.call('append', {'event_id': 'tag', 'kind': 'user', 'text': 'use tabs, not spaces',
            'origin': {'project': '/home/user/projects/optchat', 'agent': 'claude', 'session': '8b1e6c0e-long-session-uuid'}})
        message = self.service.store.root[0]
        self.assertLess(size(message.compact_source), 90)
        self.assertIn('user@optchat~', self.service.store.tree[(0, 0)].text)
        self.assertIn('/home/user/projects/optchat', message.source)

    def test_worker_prompt_describes_shared_context_map(self):
        self.service.call('append', {'event_id': 'prompt', 'kind': 'user', 'text': 'x' * 1000})
        job = self.service.call('compact_next', {})
        prompt = read_all(self.service.board, job['job'])
        self.assertIn('map from range labels', prompt)
        self.assertNotIn('<chat> is', prompt)
        self.assertNotIn('one endless chat', prompt)
        self.assertNotIn('subagent reports as', prompt)
