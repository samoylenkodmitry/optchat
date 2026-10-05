"""Bounded synthetic benchmark; no models, network, or persistent user data."""
import json
import tempfile
import time
from pathlib import Path

from optchat.jobs import JobBoard
from optchat.memory import Memory
from optchat.prompts import COMPACT
from optchat.storage import Store


def backlog(count, text):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        start = time.perf_counter()
        with Store(path) as store:
            board = JobBoard(Memory(store))
            for _ in range(count):
                board.append('user', text)
        append_time = time.perf_counter() - start
        start = time.perf_counter()
        with Store(path) as store:
            board = JobBoard(Memory(store))
            board.advance()
        return {'messages': count, 'append_seconds': round(append_time, 3), 'restart_seconds': round(time.perf_counter() - start, 3)}


def compaction():
    with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)) as store:
        board = JobBoard(Memory(store))
        for i in range(1200):
            board.append('user', f'project-{i % 3} decision {i}: ' + 'x' * 1000)
        chars, repeat_baseline, workers, jobs = 0, 0, 1, 0
        worker = None
        start = time.perf_counter()
        while True:
            response = board.next(worker)
            if response['status'] == 'done':
                break
            if response['status'] == 'rotate':
                worker = None
                workers += 1
                continue
            if response['status'] != 'claimed':
                raise RuntimeError(response)
            worker = response['worker']
            job = board.leases[response['job']]
            repeat_baseline += len(COMPACT) + sum(len(t) for t in job.context.values()) + len(board.source(job.part))
            offset = 0
            while offset is not None:
                page = board.read(job.token, offset)
                chars += len(page['text'])
                offset = page['next_offset']
            # Fixed synthetic stand-in measures transport, not semantic quality.
            board.submit(job.token, 'user: synthetic benchmark summary ' + 's' * 300)
            jobs += 1
        return {'messages': 1200, 'jobs': jobs, 'workers': workers, 'prompt_characters': chars,
                'repeated_context_baseline_characters': repeat_baseline,
                'reduction_percent': round(100 * (1 - chars / repeat_baseline), 1), 'seconds': round(time.perf_counter() - start, 3)}


if __name__ == '__main__':
    print(json.dumps({'unsummarized_backlog': backlog(8000, 'x' * 1000),
                      'short_message_backlog': backlog(12000, 'small note'), 'compaction_transport': compaction()}, indent=2))
