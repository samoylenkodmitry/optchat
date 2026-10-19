"""Claude Code hooks: record the supplied events. The hooks never start a model."""
import hashlib
import time
from pathlib import Path
from uuid import uuid4

from . import activity
from .util import atomic_json, json_text, sync_dir, valid_unicode

STALE_TURN = 7200  # Seconds after which an unfinished turn is written out.


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
    import json
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
