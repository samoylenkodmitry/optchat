import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from .mcp import render_result, serve_stdio
from .service import Client, DEFAULT_CHAT, Service


def parser():
    p = argparse.ArgumentParser(description="OptChat: one memory for Claude Code, Codex and Antigravity agents, served over MCP. It runs no model, unless you store an API key with api-key.")
    p.add_argument("--chat", type=Path, default=DEFAULT_CHAT)
    sub = p.add_subparsers(dest="command", required=True)
    mcp = sub.add_parser("mcp", help="Serve MCP over stdio, and start the memory service if it is not running")
    tools = mcp.add_mutually_exclusive_group()
    tools.add_argument("--memory-only", action="store_true", help="Offer only the tools that read the memory")
    tools.add_argument("--jobs", action="store_true", help="Offer only the job tools of the compactor subagent")
    srv = sub.add_parser("serve", help="Run the memory service in the foreground")
    srv.add_argument("--wait", action="store_true", help="If another copy holds the lock, wait for it and take over when it stops")
    sub.add_parser("status")
    sub.add_parser("stop", help="Stop the memory service. The history stays on disk.")
    sub.add_parser("view")
    z = sub.add_parser("zoom"); z.add_argument("id", type=int); z.add_argument("n", type=int, nargs="?", default=1)
    d = sub.add_parser("date"); d.add_argument("id", type=int)
    a = sub.add_parser("append"); a.add_argument("kind", choices=["user", "talk", "tool", "echo", "note"]); a.add_argument("text", nargs="?"); a.add_argument("--event-id")
    for name in ("export", "backup"):
        cmd = sub.add_parser(name); cmd.add_argument("output", type=Path)
    imp = sub.add_parser("import"); imp.add_argument("source", type=Path)
    hook = sub.add_parser("hook", help="Record a Claude Code, Codex or Antigravity hook event (JSON on stdin)")
    hook.add_argument("--agent", choices=("claude", "codex", "antigravity"), default="claude", help="The program that runs the hook")
    sub.add_parser("status-line", help="One short line for the Claude Code status line; empty when little waits")
    key = sub.add_parser("api-key", help="Store an Anthropic API key, so that the memory service writes summaries on your API credit")
    key.add_argument("--remove", action="store_true", help="Delete the stored key")
    key.add_argument("--no-browser", action="store_true", help="Only print the link to the key page")
    sub.add_parser("compact", help="Let the memory service write summaries with the API now. Exit status 3: the API is not available, so use the subagent.")
    call = sub.add_parser("call", help="Call a memory method with JSON arguments")
    call.add_argument("method"); call.add_argument("arguments", nargs="?", default="{}")
    ins = sub.add_parser("install", help="Set up OptChat for every agent session on this machine. Without --apply it only prints the changes.")
    ins.add_argument("--machine", required=True, help="Short name of this machine, e.g. laptop or desktop")
    ins.add_argument("--remote", required=True, help="rclone remote path of the shared folder, e.g. my-crypt:optchat")
    ins.add_argument("--rclone", help="Path of the rclone binary. The default comes from PATH.")
    ins.add_argument("--rclone-config", help="rclone config file, if it is not the default one")
    ins.add_argument("--interval", type=int, default=15, help="Seconds between exchanges with the shared folder")
    ins.add_argument("--offline-after", type=int, default=600, help="Seconds of silence before another machine is skipped")
    ins.add_argument("--apply", action="store_true")
    un = sub.add_parser("uninstall", help="Remove what install added. The memory stays. Without --apply it only prints the changes.")
    un.add_argument("--apply", action="store_true")
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command in ("install", "uninstall"):
            from . import install
            if args.command == "install":
                args.chat = args.chat if args.chat != DEFAULT_CHAT else None
                install.install(args)
            else:
                install.uninstall(args)
            return
        if args.command == "api-key":
            from .api import key_command
            raise SystemExit(key_command(remove=args.remove, browser=not args.no_browser, notify=lambda: Client(args.chat).call("api_key_changed")))
        if args.command == "serve":
            from .config import load_config
            asyncio.run(Service(args.chat, config=load_config(args.chat), wait=args.wait).serve()); return
        if args.command == "status-line":
            sys.stdin.read()  # Claude Code sends session data; the line does not need it.
            try:
                print(Client(args.chat, autostart=False).call("status_line")["text"])
            except Exception:
                pass  # Never start a service or show an error in the status line.
            return
        client = Client(args.chat, autostart=args.command != "stop")
        if args.command == "mcp":
            from .mcp import JOB_TOOLS, MEMORY_TOOLS, TOOLS
            serve_stdio(client, MEMORY_TOOLS if args.memory_only else JOB_TOOLS if args.jobs else TOOLS); return
        if args.command == "compact":
            from .api import compact_command
            from .install import RUN
            raise SystemExit(compact_command(client, RUN))
        if args.command == "hook":
            event = json.load(sys.stdin)
            from .hooks import enqueue
            current = enqueue(client.chat, event, args.agent)
            # Codex adds plain output of a prompt hook to the context, and it reads
            # the output of a Stop hook as JSON. Antigravity reads JSON for every hook.
            if args.agent == "codex" and event.get("hook_event_name") == "Stop":
                done = "{}"
            elif args.agent == "antigravity":
                done = '{"decision": "stop"}' if event.get("hookEventName") == "Stop" else "{}"
            else:
                done = ""
            if current is None:
                print(done, end="")
                return
            try:
                result = client.call("flush_hooks", {"current": current})
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"OptChat: the hook event is saved on disk and will be delivered later: {exc}", file=sys.stderr)
            print(done, end="")
            return
        if args.command == "append":
            result = client.call("append", {"kind": args.kind, "text": args.text if args.text is not None else sys.stdin.read(), "event_id": args.event_id or str(uuid4())})
        elif args.command == "call":
            result = client.call(args.method, json.loads(args.arguments))
        elif args.command == "import":
            source = args.source.read_text()
            records = [json.loads(line) for line in source.splitlines() if line.strip()] if args.source.suffix == ".jsonl" else [{"kind": "note", "text": source}]
            import hashlib
            identity = hashlib.sha256(source.encode()).hexdigest()
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("text"), str) or record.get("kind", "note") not in ("user", "talk", "tool", "echo", "note"):
                    raise ValueError("Invalid import record")
                if "date" in record:
                    from datetime import datetime
                    if datetime.fromisoformat(record["date"]).tzinfo is None:
                        raise ValueError("Imported dates must include a timezone")
            for index, record in enumerate(records):
                payload = {"event_id": f"import:{identity}:{index}", "kind": record.get("kind", "note"), "text": record["text"]}
                if "date" in record: payload["date"] = record["date"]
                if record.get("origin"): payload["origin"] = record["origin"]
                client.call("append", payload)
            result = {"imported": len(records), "instruction": "Agents summarize the new messages through the MCP compaction tools."}
        elif args.command in ("export", "backup"):
            result = client.call(args.command, {"path": str(args.output.resolve())})
        elif args.command == "zoom":
            result = client.call("zoom", {"id": args.id, "n": args.n})
        elif args.command == "date":
            result = client.call("date", {"id": args.id})
        elif args.command == "view":
            result = client.call("view")
            if result.get("status") in ("ready", "partial"):
                if result["status"] == "partial":
                    print(render_result({k: v for k, v in result.items() if k not in ("text", "snapshot", "offset", "next_offset", "total")}))
                parts = [result["text"]]
                while result["next_offset"] is not None:
                    result = client.call("view", {"snapshot": result["snapshot"], "offset": result["next_offset"]})
                    parts.append(result["text"])
                print("".join(parts)); return
        else:
            result = client.call("shutdown" if args.command == "stop" else args.command)
        print(render_result(result))
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"OptChat: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
