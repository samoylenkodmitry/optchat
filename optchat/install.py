"""Set up OptChat for every Claude Code and Codex session on this machine, or remove it.

Without --apply it only prints each change. With --apply it writes the files and
keeps a backup with a time stamp next to every file that it changes. Uninstall
removes only what install added, and the memory stays on disk.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import DEFAULT_CHAT

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "run"
HOME = Path.home()
CONFIG = HOME / ".config/optchat/config.json"
LABEL = "optchat.memory"
BEGIN, END = "<!-- optchat:begin -->", "<!-- optchat:end -->"
HOOK_EVENTS = {"SessionStart": None, "UserPromptSubmit": None, "PreToolUse": ".*", "PostToolUse": ".*", "PostToolUseFailure": ".*", "Stop": None}


def hook_command():
    return f"{RUN} hook"


def python():
    # This path of the interpreter stays the same across minor Python upgrades.
    return shutil.which("python3") or sys.executable


class Plan:
    def __init__(self, apply):
        self.apply, self.stamp = apply, time.strftime("%Y%m%d-%H%M%S")

    def say(self, text):
        print(("  " if self.apply else "  would ") + text)

    def write(self, path: Path, content: str, why: str):
        path = path.expanduser()
        old = path.read_text() if path.exists() else None
        if old == content:
            print(f"  ok  {path} ({why})")
            return
        self.say(f"{'update' if old is not None else 'create'} {path} ({why})")
        if self.apply:
            if old is not None:
                shutil.copy2(path, path.with_name(path.name + f".optchat-backup-{self.stamp}"))
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".optchat-tmp")
            tmp.write_text(content)
            os.replace(tmp, path)

    def remove(self, path: Path, why: str):
        if path.exists():
            self.say(f"remove {path} ({why})")
            if self.apply:
                path.unlink()

    def run(self, argv, why, check=True):
        self.say(f"run: {' '.join(map(str, argv))} ({why})")
        if self.apply:
            proc = subprocess.run(list(map(str, argv)), capture_output=True, text=True)
            if check and proc.returncode:
                raise RuntimeError(f"{argv[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def managed_block(text: str, block: str | None) -> str:
    """Add or replace the OptChat section of an instruction file. With block=None, remove it."""
    if BEGIN in text and END in text:
        before, rest = text.split(BEGIN, 1)
        after = rest.split(END, 1)[1]
        text = before.rstrip("\n") + ("\n" if before.strip() else "") + after.lstrip("\n")
    if block is None:
        return text
    return text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"{BEGIN}\n{block.strip()}\n{END}\n"


def merged_hooks(settings: dict, install: bool) -> dict:
    hooks = settings.setdefault("hooks", {})
    command = hook_command()
    for event, matcher in HOOK_EVENTS.items():
        entries = [e for e in hooks.get(event, []) if not any(h.get("command") == command for h in e.get("hooks", []))]
        if install:
            entry = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
            if matcher:
                entry = {"matcher": matcher, **entry}
            entries.append(entry)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")
    return settings


def registered(cli, name="optchat"):
    exe = shutil.which(cli)
    return exe is not None and subprocess.run([exe, "mcp", "get", name], capture_output=True).returncode == 0


def install(args):
    plan = Plan(args.apply)
    chat = Path(args.chat or DEFAULT_CHAT).expanduser()
    exchange = {"type": "rclone", "remote": args.remote, "rclone": args.rclone or shutil.which("rclone") or "rclone"}
    if args.rclone_config:
        exchange["config"] = str(Path(args.rclone_config).expanduser())
    config = {"machine": args.machine, "chat": str(chat), "exchange": exchange, "interval": args.interval, "offline_after": args.offline_after}
    print(f"OptChat install for machine '{args.machine}' (repository {REPO}):")
    plan.write(CONFIG, json.dumps(config, indent=2) + "\n", "machine settings, outside Git")
    if args.apply:
        chat.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Start the memory service automatically. It runs no models.
    env = {"PYTHONPATH": str(REPO), "OPTCHAT_CONFIG": str(CONFIG)}
    if sys.platform == "darwin":
        plist = HOME / f"Library/LaunchAgents/{LABEL}.plist"
        body = plistlib.dumps({"Label": LABEL, "ProgramArguments": [python(), "-m", "optchat", "--chat", str(chat), "serve"],
                               "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
                               "StandardOutPath": str(chat / "service.log"), "StandardErrorPath": str(chat / "service.log")}).decode()
        plan.write(plist, body, "start at login and after an exit")
        plan.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], "reload", check=False)
        plan.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist], "start now")
    else:
        unit = HOME / ".config/systemd/user/optchat.service"
        body = "\n".join(["[Unit]", "Description=OptChat memory service (it runs no models)", "After=network-online.target", "",
                          "[Service]", *[f"Environment={k}={v}" for k, v in env.items()],
                          f"ExecStart={python()} -m optchat --chat {chat} serve", "Restart=always", "RestartSec=10", "",
                          "[Install]", "WantedBy=default.target", ""])
        plan.write(unit, body, "start at boot and after an exit")
        plan.run(["systemctl", "--user", "daemon-reload"], "load the unit")
        plan.run(["systemctl", "--user", "enable", "--now", "optchat.service"], "start now")

    # MCP for Claude Code and Codex.
    for cli, argv in (("claude", ["claude", "mcp", "add", "--scope", "user", "optchat", "--", RUN, "mcp"]),
                      ("codex", ["codex", "mcp", "add", "optchat", "--", RUN, "mcp"])):
        if not shutil.which(cli):
            print(f"  skip {cli}: not installed")
        elif registered(cli):
            print(f"  ok  {cli} MCP server 'optchat' already registered")
        else:
            plan.run(argv, f"make the memory tools available in every {cli} session")

    # Hooks record every Claude session. The compactor subagent writes summaries.
    claude_home = HOME / ".claude"
    if shutil.which("claude") or claude_home.exists():
        settings_path = claude_home / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        plan.write(settings_path, json.dumps(merged_hooks(settings, True), indent=2) + "\n", "hooks that record every Claude session")
        plan.write(claude_home / "agents/optchat-compactor.md", (REPO / "integrations/optchat-compactor.md").read_text(), "compaction subagent")

    # Instructions for every agent.
    block = (REPO / "integrations/AGENTS.optchat.md").read_text()
    targets = [claude_home / "CLAUDE.md"] + ([HOME / ".codex/AGENTS.md"] if (HOME / ".codex").exists() or shutil.which("codex") else [])
    for path in targets:
        old = path.read_text() if path.exists() else ""
        plan.write(path, managed_block(old, block), "adds the OptChat section; your own text stays")
    if not args.apply:
        print("Dry run only. Re-run with --apply to make these changes.")


def uninstall(args):
    plan = Plan(args.apply)
    print("OptChat uninstall. The memory stays on disk:")
    if sys.platform == "darwin":
        plan.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], "stop", check=False)
        plan.remove(HOME / f"Library/LaunchAgents/{LABEL}.plist", "autostart")
    else:
        plan.run(["systemctl", "--user", "disable", "--now", "optchat.service"], "stop", check=False)
        plan.remove(HOME / ".config/systemd/user/optchat.service", "autostart")
    for cli in ("claude", "codex"):
        if registered(cli):
            plan.run([cli, "mcp", "remove", "optchat"] + (["--scope", "user"] if cli == "claude" else []), "MCP server", check=False)
    settings_path = HOME / ".claude/settings.json"
    if settings_path.exists():
        plan.write(settings_path, json.dumps(merged_hooks(json.loads(settings_path.read_text()), False), indent=2) + "\n", "remove OptChat hooks")
    plan.remove(HOME / ".claude/agents/optchat-compactor.md", "compaction subagent")
    for path in (HOME / ".claude/CLAUDE.md", HOME / ".codex/AGENTS.md"):
        if path.exists() and BEGIN in path.read_text():
            plan.write(path, managed_block(path.read_text(), None), "remove the OptChat section")
    if not args.apply:
        print("Dry run only. Re-run with --apply to make these changes.")
