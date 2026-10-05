from __future__ import annotations

import html
import os
import tarfile
from pathlib import Path

from .util import sync_dir


def export_html(memory, path: Path):
    """A self-contained, script-free browser for every summary and original."""
    esc = html.escape
    store = memory.store
    with path.open("w", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        out.write('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OptChat memory</title>
<style>:root{color-scheme:light dark}body{font:16px/1.6 system-ui;max-width:1000px;margin:40px auto;padding:0 24px}h1{letter-spacing:-.04em}nav{position:sticky;top:0;background:Canvas;padding:12px 0;display:flex;gap:20px;flex-wrap:wrap}a{color:#5588dd}details{border-top:1px solid #8885;padding:12px 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}small{opacity:.7}.node{scroll-margin-top:90px}header p{opacity:.7}</style>
<header><h1>OptChat memory</h1><p>Every original, every summary, one continuous history.</p></header><nav><a href="#view">Current view</a><a href="#root">Original messages</a><a href="#tree">Summary tree</a></nav>''')
        out.write(f'<p>{memory.total:,} messages · {len(store.tree):,} summaries · {memory.bytes:,} view bytes</p><h2 id="view">Current view</h2>')
        for p in memory.view:
            target = f"node-{p.l}-{p.i}" if memory.built(p) else f"message-{p.start}"
            out.write(f'<p><a href="#{target}">{p.start}+{p.n}</a> {esc(memory.text(p))}</p>')
        out.write('<h2 id="root">Original messages</h2>')
        for m in store.root:
            out.write(f'<details class="node" id="message-{m.i}"><summary>{m.i}+0 · {esc(m.kind)} <small>{esc(m.date)} · {m.size:,} bytes</small></summary><pre>{esc(m.source)}</pre><a href="#node-0-{m.i}">Summary</a></details>')
        out.write('<h2 id="tree">Summary tree</h2>')
        level = None
        for (l, i), node in sorted(store.tree.items()):
            if level != l:
                level = l
                out.write(f'<h3>Level {l} · {2**l} messages per node</h3>')
            first, end = i * 2**l, (i + 1) * 2**l
            dates = f"{store.root[first].date} — {store.root[end-1].date}"
            out.write(f'<details class="node" id="node-{l}-{i}"><summary>{first}+{2**l} <small>{esc(dates)} · {node.size:,} bytes</small></summary><pre>{esc(node.text)}</pre>')
            if l:
                for j in (0, 1):
                    out.write(f'<a href="#node-{l-1}-{2*i+j}">Child {j+1}</a> ')
            else:
                out.write(f'<a href="#message-{i}">Original message</a>')
            out.write('</details>')
        out.write('</html>')
        out.flush()
        os.fsync(out.fileno())
    sync_dir(path.parent)


def backup(store, path: Path):
    # Only copy durable data. Lock, IPC sockets and temporary credentials stay out.
    with path.open("xb") as f:
        os.chmod(path, 0o600)
        with tarfile.open(fileobj=f, mode="w:gz") as archive:
            for name in ("main", "tree", "delivery.sqlite3", "jobs.json", "spool", "identity.json", "sync.json", "own", "remote", "pool", "outsum"):
                source = store.path / name
                if source.exists():
                    archive.add(source, arcname=f"chat/{name}", recursive=True)
        f.flush()
        os.fsync(f.fileno())
    sync_dir(path.parent)
