"""Turn the tool calls of one agent turn into one short record.

The memory keeps what changed and what failed. Exploration (file reads,
searches, read-only commands, web lookups) is kept only as counts and a few
names: its content is still on disk or on the web. The answers of the user to
agent questions, subagent reports and approved plans are kept in full,
because they hold decisions and findings.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .util import cut_bytes

DIGEST_LIMIT = 420  # Bytes of one turn record. With its origin prefix it stays under 512 bytes, so it is its own summary line and needs no worker.
REPORT_LIMIT = 4_000  # Characters of a subagent report or a plan.
IGNORED_TOOLS = {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TaskStop", "ToolSearch",
                 "ScheduleWakeup", "Monitor", "ReadNotifications", "ListAgents"}
READ_ONLY = {"ls", "cat", "head", "tail", "grep", "rg", "ag", "ack", "find", "fd", "wc", "pwd", "echo", "printf", "which",
             "whereis", "type", "file", "stat", "du", "df", "tree", "less", "more", "sort", "uniq", "cut", "tr", "jq", "yq",
             "diff", "cmp", "date", "env", "printenv", "uname", "ps", "id", "whoami", "hostname", "readlink", "realpath",
             "basename", "dirname", "nl", "column", "xxd", "hexdump", "od", "strings", "test", "[", "true", "false", "cd",
             "eza", "exa", "bat", "lsof", "pgrep", "sw_vers", "otool", "nm", "awk", "sed", "md5", "md5sum", "shasum", "sha256sum"}
GIT_READ_ONLY = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "grep", "shortlog",
                 "reflog", "ls-remote", "cat-file", "merge-base", "whatchanged"}


def short_path(path, root=None):
    if not isinstance(path, str) or not path:
        return "?"
    try:
        if root and Path(path).is_relative_to(root):
            return str(Path(path).relative_to(root))
    except (TypeError, ValueError):
        pass
    return path.replace(str(Path.home()), "~", 1)


def clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def lines(text):
    return text.count("\n") + 1 if isinstance(text, str) and text else 0


def segments(command):
    """Simple commands of a shell line, split at pipes, lists and subshells, with quotes respected."""
    lexer = shlex.shlex((command or "").replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segment = []
    for token in lexer:
        if token and set(token) <= set("|&;()"):
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)
    if segment:
        yield segment


def git_reads(words):
    args = [w for w in words[1:] if not w.startswith("-")]
    sub, rest = (args[0], args[1:]) if args else ("", [])
    if sub in GIT_READ_ONLY:
        return True
    flags = set(words[1:])
    if sub == "branch":
        return not rest or bool(flags & {"-a", "-r", "--list", "-v", "-vv", "--show-current"})
    if sub == "tag":
        return not rest or bool(flags & {"-l", "--list"})
    if sub == "remote":
        return not rest or rest[0] in ("show", "get-url")
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if sub == "config":
        return bool(flags & {"--get", "--list", "-l", "--get-all"})
    return False


def bash_is_read_only(command):
    """True when every simple command of the line only reads."""
    try:
        parts = list(segments(command))
    except ValueError:
        return False  # Unbalanced quotes: treat the command as a change.
    for words in parts:
        for i, word in enumerate(words):
            if word in (">", ">>", ">|", "&>") and (i + 1 >= len(words) or words[i + 1] != "/dev/null"):
                return False  # Output goes into a file.
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]  # VAR=value before the command
        if not words:
            continue
        name = Path(words[0]).name
        if name == "git":
            if not git_reads(words):
                return False
        elif name == "sed" and any(w.startswith("-i") for w in words[1:]):
            return False
        elif name == "find" and any(w in ("-delete", "-exec", "-execdir", "-ok") for w in words[1:]):
            return False
        elif name not in READ_ONLY:
            return False
    return True


def result_text(response):
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        content = response.get("content")
        if isinstance(content, list):
            return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        if isinstance(content, str):
            return content
        for key in ("result", "output", "text", "stdout"):
            if isinstance(response.get(key), str):
                return response[key]
    if isinstance(response, list):
        return "\n".join(c.get("text", "") for c in response if isinstance(c, dict))
    return json.dumps(response, ensure_ascii=False) if response is not None else ""


def compact(event, root=None):
    """One finished tool call as a small item for the turn record.

    Returns None for a call without lasting value, {"message": (kind, text)}
    for a call that is recorded on its own, or {"group": ..., "text": ...}.
    """
    tool = event.get("tool_name", "")
    data = event.get("tool_input") or {}
    data = data if isinstance(data, dict) else {}
    response = event.get("tool_response")
    failed = event.get("hook_event_name") == "PostToolUseFailure"
    if tool in IGNORED_TOOLS:
        return None
    if failed:
        raw = (event.get("error") or result_text(response) or "").strip()
        error = clip(raw.splitlines()[0], 160) if raw else "no details"
        what = clip(data.get("command") or data.get("file_path") or data.get("pattern") or data.get("url") or "", 80)
        return {"group": "failed", "text": f"{tool} {what}: {error}".strip()}
    if tool == "AskUserQuestion":
        answers = (response or {}).get("answers") if isinstance(response, dict) else None
        if isinstance(answers, dict) and answers:
            body = "\n".join(f"{q} {a}" for q, a in answers.items())
            return {"message": ("user", "Answers to agent questions:\n" + body)}
        return None
    if tool in ("Agent", "Task"):
        report = clip(result_text(response), REPORT_LIMIT)
        who = data.get("subagent_type") or "general"
        about = clip(data.get("description") or "", 80)
        return {"message": ("echo", f"Report of the {who} subagent ({about}):\n{report}")} if report else None
    if tool == "ExitPlanMode":
        plan = data.get("plan")
        return {"message": ("talk", "Plan:\n" + clip(plan, REPORT_LIMIT))} if plan else None
    if tool in ("Edit", "MultiEdit"):
        edits = data.get("edits") if tool == "MultiEdit" else [data]
        added = sum(lines(e.get("new_string")) for e in edits or [] if isinstance(e, dict))
        removed = sum(lines(e.get("old_string")) for e in edits or [] if isinstance(e, dict))
        return {"group": "changed", "text": f"{short_path(data.get('file_path'), root)} (+{added} -{removed})", "key": data.get("file_path")}
    if tool == "Write":
        n = lines(data.get("content"))
        return {"group": "changed", "text": f"{short_path(data.get('file_path'), root)} (written, {n} line{'s' if n != 1 else ''})", "key": data.get("file_path")}
    if tool == "NotebookEdit":
        return {"group": "changed", "text": f"{short_path(data.get('notebook_path'), root)} (notebook)", "key": data.get("notebook_path")}
    if tool in ("Read", "LS", "NotebookRead"):
        return {"group": "read", "text": short_path(data.get("file_path") or data.get("path") or data.get("notebook_path"), root)}
    if tool in ("Grep", "Glob"):
        return {"group": "search", "text": clip(data.get("pattern"), 60)}
    if tool in ("WebSearch", "WebFetch"):
        return {"group": "web", "text": clip(data.get("query") or data.get("url"), 100)}
    if tool == "Bash":
        command = data.get("command", "")
        if bash_is_read_only(command):
            return {"group": "looked", "text": clip(command, 60)}
        output = result_text(response) or ""
        if isinstance(response, dict):
            output = (response.get("stdout") or "") + ("\n" + response["stderr"] if response.get("stderr") else "")
        last = next((l for l in reversed(output.splitlines()) if l.strip()), "")
        tail = f", last line: {clip(last, 100)}" if last else ""
        n = lines(output)
        return {"group": "ran", "text": f"{clip(command, 120)} (ok, {n} line{'s' if n != 1 else ''} of output{tail})"}
    if tool.startswith("mcp__optchat__"):
        return None
    brief = clip(json.dumps(data, ensure_ascii=False), 80) if data else ""
    return {"group": "used", "text": f"{tool} {brief}".strip()}


def digest(items):
    """One record for the tool calls of one turn, or None when nothing is worth keeping."""
    groups = {}
    for item in items:
        groups.setdefault(item["group"], []).append(item)
    changed = {}
    for item in groups.get("changed", []):
        changed[item.get("key") or item["text"]] = item["text"]  # The last edit of a file wins.
    def listed(texts, keep, noun):
        """The first `keep` entries, and a count of the others."""
        more = f"; and {len(texts) - keep} more {noun}" if len(texts) > keep else ""
        return "; ".join(texts[:keep]) + more

    out = []
    if changed:
        out.append("Changed: " + listed(list(changed.values()), 5, "files"))
    if groups.get("failed"):
        out.append("Failed: " + listed([clip(i["text"], 100) for i in groups["failed"]], 2, "failures"))
    if groups.get("ran"):
        out.append("Ran: " + listed([clip(i["text"], 90) for i in groups["ran"]], 2, "commands"))
    if groups.get("used"):
        out.append("Used: " + listed([clip(i["text"], 60) for i in groups["used"]], 2, "tool calls"))
    reads = list(dict.fromkeys(i["text"] for i in groups.get("read", [])))
    looked = []
    if reads:
        names = ", ".join(Path(r).name for r in reads[:3]) + (f" and {len(reads) - 3} more" if len(reads) > 3 else "")
        looked.append(f"read {len(reads)} file{'s' if len(reads) != 1 else ''} ({names})")
    if groups.get("search"):
        looked.append(f"{len(groups['search'])} search{'es' if len(groups['search']) != 1 else ''}")
    if groups.get("looked"):
        looked.append(f"{len(groups['looked'])} read-only command{'s' if len(groups['looked']) != 1 else ''}")
    if groups.get("web"):
        looked.append(f"{len(groups['web'])} web lookup{'s' if len(groups['web']) != 1 else ''}")
    if looked:
        out.append("Looked at: " + ", ".join(looked))
    if not out:
        return None
    text = "Turn activity: " + ". ".join(out)
    if len(text.encode()) > DIGEST_LIMIT:
        text = cut_bytes(text, DIGEST_LIMIT - 4).rsplit(" ", 1)[0] + " ..."
    return text
