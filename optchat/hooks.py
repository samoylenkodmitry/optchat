"""Agent hooks: record the supplied events. The hooks never start a model."""
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from . import activity
from .util import atomic_json, json_text, sync_dir, valid_unicode

STALE_TURN = 7200  # Seconds after which an unfinished turn is written out.


def antigravity_user_text(content):
    if not isinstance(content, str):
        return ""
    start = content.find("<USER_REQUEST>")
    if start != -1:
        body = content[start + len("<USER_REQUEST>"):]
        end = body.rfind("</USER_REQUEST>")
        if end != -1:
            return body[:end].strip()
    return content.strip()


def antigravity_transcript(path_str):
    """Return (has_user_input, entries) from an Antigravity transcript.

    Each entry is (step_index, kind, text) for explicit user prompts and final
    replies without tool calls. Reasoning (thinking) is never read.
    """
    path = Path(path_str).expanduser()
    full = path.with_name("transcript_full.jsonl") if path.name == "transcript.jsonl" else path
    source = full if full.is_file() else path
    if not source.is_file():
        return False, []
    has_user = False
    entries = []
    with source.open("rb") as f:
        for line_no, raw in enumerate(f):
            if b'"USER_INPUT"' in raw:
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if row.get("type") == "USER_INPUT" and row.get("source") == "USER_EXPLICIT":
                    has_user = True
                    text = antigravity_user_text(row.get("content", ""))
                    step = row.get("step_index")
                    if not isinstance(step, int):
                        step = line_no
                    if text:
                        entries.append((step, "user", text))
            elif b'"PLANNER_RESPONSE"' in raw and b'"content"' in raw:
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if row.get("type") == "PLANNER_RESPONSE" and not row.get("tool_calls"):
                    content = row.get("content")
                    if isinstance(content, str) and content.strip():
                        step = row.get("step_index")
                        if not isinstance(step, int):
                            step = line_no
                        entries.append((step, "talk", content.strip()))
    return has_user, entries


def ignored(event):
    if not isinstance(event, dict) or not isinstance(event.get("tool_name", ""), str):
        raise ValueError("Hook event must be an object with a textual tool name")
    if event.get("agent_id") or event.get("agent_type") == "optchat-compactor":
        return "subagent"
    tool = event.get("tool_name", "")
    if tool.startswith(("mcp__optchat__", "mcp__optchat-jobs__")) or (tool in ("Agent", "Task") and "optchat-compactor" in json_text(event.get("tool_input", {}))):
        return "memory or compaction traffic"
    return None


def text_key(event):
    """A stable id for a prompt or a final reply. Claude Code gives one prompt id to
    every prompt that arrives while a turn runs, and a blocking Stop hook can give
    different replies for one turn. The hash of the text keeps them apart."""
    if event.get("hook_event_name") == "Stop":
        base, text = event.get("turn_id") or event.get("prompt_id"), event.get("last_assistant_message")
    else:
        base, text = event.get("prompt_id") or event.get("user_message_id") or event.get("turn_id"), event.get("prompt")
    text = valid_unicode(text) if isinstance(text, str) else ""
    return f"{base}:{hashlib.sha256(text.encode()).hexdigest()}" if base else None


def enqueue(chat, event, agent="claude"):
    """Write the event to disk before delivery. The next service call delivers any event that is still on disk."""
    if ignored(event):
        return None
    if agent == "antigravity":
        hook_name = event.get("hookEventName") or event.get("hook_event_name") or ""
        if hook_name != "Stop":
            return None
        workspaces = event.get("workspacePaths")
        cwd = event.get("cwd") or (workspaces[0] if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], str) else "")
        event = {
            "hook_event_name": hook_name,
            "session_id": event.get("conversationId") or event.get("session_id") or "",
            "cwd": cwd,
            "transcript_path": event.get("transcriptPath") or event.get("transcript_path") or "",
            "client": "antigravity",
        }
    else:
        fields = {"hook_event_name", "session_id", "cwd", "agent_type", "agent_id", "event_id", "prompt_id", "user_message_id", "turn_id", "prompt", "tool_name", "tool_input", "tool_use_id", "tool_response", "error", "last_assistant_message"}
        event = {key: value for key, value in event.items() if key in fields}
        event["client"] = agent
        if not event.get("event_id"):
            key = text_key(event) if event.get("hook_event_name") in ("Stop", "UserPromptSubmit") else None
            event["event_id"] = key or event.get("prompt_id") or event.get("user_message_id") or str(uuid4())
    directory = chat / "spool"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sync_dir(chat)
    path = directory / f"{time.time_ns():020d}-{uuid4().hex}.json"
    # JSON escapes keep lone surrogates until intake replaces them.
    event = json.loads(valid_unicode(json_text(event)))
    atomic_json(path, event)
    return path.name


def project_root(cwd):
    path = Path(cwd).expanduser().resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(path)


def flush_turn(service, session):
    """Write the held tool calls of a session as one record, then forget them."""
    keys, origin, items = service.ledger.held(session)
    if not keys:
        return
    text = activity.digest(items)
    if text:
        event_id = f"{session}:turn:" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]
        service.call("append", {"event_id": event_id, "kind": "tool", "text": text, "origin": origin})
    service.ledger.release_turn(session)


def ingest(service, event):
    for stale in service.ledger.stale_turns(time.time() - STALE_TURN):
        flush_turn(service, stale)  # A turn that never reached Stop.
    exclusion = ignored(event)
    name = event.get("hook_event_name", "")
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        raise ValueError("Hook event lacks session_id")
    # Subagent activity stays out of the main memory. OptChat calls and the start
    # of the compactor are excluded too, so compaction never creates new messages.
    if exclusion:
        return {"ignored": exclusion}
    if event.get("client") == "antigravity":
        if name != "Stop":
            return {"ignored": "unsupported hook"}
        transcript = event.get("transcript_path")
        if not isinstance(transcript, str) or not transcript:
            return {"ignored": "no transcript"}
        has_user, entries = antigravity_transcript(transcript)
        if not has_user:
            return {"ignored": "subagent"}
        if not entries:
            return {"ignored": "no messages"}
        origin = {"session": session, "agent": "antigravity"}
        if event.get("cwd"):
            origin["project"] = project_root(event["cwd"])
        cursor = service.ledger.cursor(session)
        if cursor is None:
            # First Stop seen for this chat: record only the current turn so an
            # already-open multi-turn chat does not backfill older turns out of order.
            earlier = [step for step, kind, _ in (entries[:-1] if entries[-1][1] == "talk" else entries) if kind == "talk"]
            cutoff = earlier[-1] if earlier else -1
            pending = [entry for entry in entries if entry[0] > cutoff]
        else:
            pending = [entry for entry in entries if entry[0] > cursor]
        service.ledger.session(session, "main")
        for step, kind, text in pending:
            if kind == "user":
                item = activity.prompt(text)
                if item is None or "message" not in item:
                    continue
                kind, text = item["message"]
            key = f"{session}:antigravity:step:{step}"
            if not service.ledger.has(key):
                service.call("append", {"event_id": key, "kind": kind, "text": text, "origin": origin})
        service.ledger.cursor(session, max(cursor if cursor is not None else -1, entries[-1][0]))
        return {}
    tool = event.get("tool_name", "")
    inputs = event.get("tool_input", {})
    if name == "SessionStart":
        # Enroll the chat. Nothing goes into the context of the agent: the global
        # instructions already say when to use the memory.
        service.ledger.session(session, "main")
        return {"enrolled": session}
    # In Claude Code only a SessionStart event enrolls a session: a child without
    # agent_id must never become a main session through a tool event. Codex marks
    # every subagent event with agent_id, so its sessions enroll at the first prompt.
    if event.get("client") == "codex" and name == "UserPromptSubmit":
        service.ledger.session(session, "main")
    if service.ledger.session(session) != "main":
        return {"ignored": "session not enrolled by SessionStart"}
    keybase = session + ":" + name + ":"
    origin = {"session": session, "agent": event.get("client") or "claude"}
    if event.get("cwd"):
        origin["project"] = project_root(event["cwd"])
    def append(kind, text, identity):
        return service.call("append", {"event_id": keybase + str(identity), "kind": kind, "text": text, "origin": origin})
    if name == "UserPromptSubmit":
        identity = event.get("event_id") or text_key(event)
        if not identity:
            raise ValueError("UserPromptSubmit needs an event_id. The hook client supplies one.")
        item = activity.prompt(event.get("prompt", ""))
        if item is None:
            return {"ignored": "a notice or a suggestion prompt of an app, or a notice of a background task"}
        if "report" in item:
            # The end of a background subagent. Its launch was recorded as a wait
            # for this report. Background commands and the compactor were not.
            # A resumed subagent sends a new report with the same tool id.
            tool = item["report"]
            label = service.ledger.report_label(tool)
            if not label:
                return {"ignored": "a background task without a report to keep"}
            report = activity.clip(item["text"], activity.REPORT_LIMIT)
            append("echo", f"Report of the {label}:\n{report}", f"report:{tool}:" + hashlib.sha256(report.encode()).hexdigest()[:16])
            return {}
        kind, text = item["message"]
        result = append(kind, text, identity)
    elif name == "PreToolUse":
        return {"ignored": "A tool call is recorded once, after it finishes."}
    elif name in ("PostToolUse", "PostToolUseFailure"):
        identity = event.get("tool_use_id")
        if not identity:
            raise ValueError("Tool hook requires tool_use_id")
        item = activity.compact(event, origin.get("project"))
        if item is None:
            return {"ignored": "tool call without lasting value"}
        if "report_later" in item:
            # A background subagent. Its report arrives later as a task notification.
            service.ledger.expect_report(identity, session, item["report_later"], time.time())
            return {}
        if "message" not in item:
            # Held until the turn ends; Stop turns all calls of the turn into one record.
            service.ledger.hold(keybase + identity, session, origin, item, time.time())
            return {}
        kind, text = item["message"]
        result = append(kind, text, identity)
    elif name == "Stop":
        flush_turn(service, session)
        text = event.get("last_assistant_message")
        if not isinstance(text, str) or not text:
            return {"ignored": "The event has no final message, and OptChat does not read transcripts."}
        identity = event.get("event_id") or text_key(event)
        if not identity:
            raise ValueError("Stop needs a stable turn or event id. The hook client supplies one.")
        result = append("talk", text, identity)
    else:
        return {"ignored": "unsupported hook"}
    return {}
