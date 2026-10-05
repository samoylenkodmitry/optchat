from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

NODE = 512
VIEW = 128_000
JOBS = 8
TRIES = 5
RETRY = 10.0
CAP = 30_000
MARKS = (50_000, 80_000, 100_000)
PLACEHOLDER = "(not summarized yet: zoom it)"


def size(text: str) -> int:
    return len(text.encode("utf-8"))


def valid_unicode(text: str) -> str:
    """Preserve valid Unicode; replace unpaired surrogate code units visibly."""
    return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def cut_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def flat(text: str) -> str:
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def cap(text: str, limit: int = CAP) -> str:
    """Character cap including the omission marker; retain both ends."""
    if len(text) <= limit:
        return text
    marker = f"\n[... {len(text)} original characters; middle omitted ...]\n"
    keep = max(0, limit - len(marker))
    head = (keep + 1) // 2
    tail = keep // 2
    return (text[:head] + marker + (text[-tail:] if tail else ""))[:limit]


def chunks(text: str, marks: tuple[int, ...] = MARKS) -> list[str]:
    """Stable pieces ending at the last newline before each character mark."""
    result, start = [], 0
    for mark in marks:
        if mark >= len(text):
            continue
        end = text.rfind("\n", 0, mark) + 1
        if end > start:
            result.append(text[start:end])
            start = end
    if start < len(text):
        result.append(text[start:])
    return result


def now() -> str:
    return datetime.now().astimezone().isoformat()


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        f.write(json_text(value) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    sync_dir(path.parent)
