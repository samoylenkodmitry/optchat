"""Claude Code hooks: record the supplied events. The hooks never start a model."""
import hashlib
import time
from pathlib import Path
from uuid import uuid4

from .util import atomic_json, json_text, sync_dir, valid_unicode


def ignored(event):
    if not isinstance(event, dict) or not isinstance(event.get("tool_name", ""), str):
        raise ValueError("Hook event must be an object with a textual tool name")
    if event.get("agent_id") or event.get("agent_type") == "optchat-compactor":
        return "subagent"
    tool = event.get("tool_name", "")
    if tool.startswith("mcp__optchat__") or (tool in ("Agent", "Task") and "optchat-compactor" in json_text(event.get("tool_input", {}))):
        return "memory/compaction traffic"
    return None


def enqueue(chat, event):
    """Write the event to disk before delivery. The next service call delivers any event that is still on disk."""
    if ignored(event):
        return None
    fields = {"hook_event_name", "session_id", "cwd", "agent_type", "agent_id", "event_id", "prompt_id", "user_message_id", "turn_id", "prompt", "tool_name", "tool_input", "tool_use_id", "tool_response", "error", "last_assistant_message"}
    event = {key: value for key, value in event.items() if key in fields}
    if not event.get("event_id"):
        if event.get("hook_event_name") == "Stop":
            # A blocking Stop hook can produce different replies for one turn.
            base = event.get("turn_id") or event.get("prompt_id")
            text = valid_unicode(event.get("last_assistant_message", ""))
            event["event_id"] = str(base) + ":" + hashlib.sha256(text.encode()).hexdigest() if base else str(uuid4())
        else:
            event["event_id"] = event.get("prompt_id") or event.get("user_message_id") or str(uuid4())
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


def ingest(service, event):
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
        service.ledger.session(session, "main")
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": "OptChat memory is available. Call mcp__optchat__view and read every page. Use zoom for exact details. A partial view marks the messages that have no summary yet, and their content is unknown until you read them. Start the optchat-compactor subagent only when OptChat asks for compaction. Do not record compaction work."}}
    # Only a SessionStart event enrolls a session. A child without agent_id must
    # never become a main session through a tool event.
    if service.ledger.session(session) != "main":
        return {"ignored": "session not enrolled by SessionStart"}
    keybase = session + ":" + name + ":"
    origin = {"session": session, "agent": "claude"}
    if event.get("cwd"):
        origin["project"] = project_root(event["cwd"])
    def append(kind, text, identity):
        return service.call("append", {"event_id": keybase + str(identity), "kind": kind, "text": text, "origin": origin})
    if name == "UserPromptSubmit":
        text = event.get("prompt", "")
        # Newer clients send a prompt or message id. Without it the caller must
        # supply event_id, so two identical user messages stay separate.
        identity = event.get("event_id") or event.get("prompt_id") or event.get("user_message_id")
        if not identity:
            raise ValueError("UserPromptSubmit needs an event_id. The hook client supplies one.")
        result = append("user", text, identity)
    elif name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        identity = event.get("tool_use_id")
        if not identity:
            raise ValueError("Tool hook requires tool_use_id")
        if name == "PreToolUse":
            kind, text = "tool", f"{tool} [call={identity}] " + json_text(inputs)
        else:
            kind = "echo"
            content = event.get("tool_response", event.get("error", "Tool failed without a result"))
            text = f"{tool} [call={identity}] " + (content if isinstance(content, str) else json_text(content))
        result = append(kind, text, identity)
    elif name == "Stop":
        text = event.get("last_assistant_message")
        if not isinstance(text, str) or not text:
            return {"ignored": "The event has no final message, and OptChat does not read transcripts."}
        identity = event.get("event_id") or event.get("turn_id")
        if not identity:
            raise ValueError("Stop needs a stable turn or event id. The hook client supplies one.")
        result = append("talk", text, identity)
    else:
        return {"ignored": "unsupported hook"}
    if result.get("compaction_needed") and name == "UserPromptSubmit":
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": "OptChat asks for compaction: many messages wait for summaries. Start one optchat-compactor subagent in the background when subagents are allowed, and continue your task. The OptChat service makes no model calls."}}
    return {}
