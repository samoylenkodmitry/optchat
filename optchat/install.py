"""Set up OptChat for every Claude Code, Codex and Antigravity session on this machine, or remove it.

Without --apply it only prints each change. With --apply it writes the files and
keeps a backup with a time stamp next to every shared file that it changes. Uninstall
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
HOOK_EVENTS = {"SessionStart": None, "UserPromptSubmit": None, "PostToolUse": ".*", "PostToolUseFailure": ".*", "Stop": None}
# Codex records the words of the chat. Its hooks run in the background, so they never
# delay a turn, and their output never reaches the context.
CODEX_HOOK_EVENTS = {"UserPromptSubmit": None, "Stop": None}
MCP_ARGS = ["mcp", "--memory-only"]


def hook_command(agent="claude"):
    return f"{RUN} hook" + (f" --agent {agent}" if agent != "claude" else "")


def antigravity_plugin_files(block: str) -> dict[str, str]:
    manifest = {
        "name": "optchat",
        "displayName": "OptChat",
        "version": "1.0.0",
        "description": "Shared memory across Claude Code, Codex and Antigravity sessions.",
    }
    mcp = {"mcpServers": {"optchat": {"command": str(RUN), "args": list(MCP_ARGS)}}}
    hooks = {"optchat": {"Stop": [{"type": "command", "command": hook_command("antigravity"), "timeout": 10}]}}
    return {
        "plugin.json": json.dumps(manifest, indent=2) + "\n",
        "mcp_config.json": json.dumps(mcp, indent=2) + "\n",
        "hooks.json": json.dumps(hooks, indent=2) + "\n",
        "rules/AGENTS.md": block.strip() + "\n",
    }


def python():
    # This path of the interpreter stays the same across minor Python upgrades.
    return shutil.which("python3") or sys.executable


class Plan:
    def __init__(self, apply):
        self.apply, self.stamp = apply, time.strftime("%Y%m%d-%H%M%S")

    def say(self, text):
        print(("  " if self.apply else "  would ") + text)

    def write(self, path: Path, content: str, why: str, *, backup: bool = True):
        path = path.expanduser()
        old = path.read_text() if path.exists() else None
        if old == content:
            print(f"  ok  {path} ({why})")
            return
        self.say(f"{'update' if old is not None else 'create'} {path} ({why})")
        if self.apply:
            if old is not None and backup:
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

    def rmdir(self, path: Path):
        if path.is_dir() and not any(path.iterdir()):
            self.say(f"remove empty directory {path}")
            if self.apply:
                path.rmdir()

    def run(self, argv, why, check=True):
        self.say(f"run: {' '.join(map(str, argv))} ({why})")
        if self.apply:
            proc = subprocess.run(list(map(str, argv)), capture_output=True, text=True)
            if check and proc.returncode:
                raise RuntimeError(f"{argv[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def launchd_reload(plan, plist):
    """Stop the running job, wait until launchd has unloaded it, then load the plist again."""
    target = f"gui/{os.getuid()}/{LABEL}"
    plan.run(["launchctl", "bootout", target], "stop the running copy", check=False)
    if not plan.apply:
        plan.say(f"run: launchctl bootstrap gui/{os.getuid()} {plist} (start now)")
        return
    for _ in range(50):
        if subprocess.run(["launchctl", "print", target], capture_output=True).returncode:
            break
        time.sleep(0.2)
    for attempt in range(3):
        proc = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
        if proc.returncode == 0:
            plan.say(f"run: launchctl bootstrap gui/{os.getuid()} {plist} (start now)")
            return
        time.sleep(1)
    raise RuntimeError(f"launchctl bootstrap failed: {proc.stderr.strip()}")


def managed_block(text: str, block: str | None) -> str:
    """Add or replace the OptChat section of an instruction file. With block=None, remove it."""
    if BEGIN in text and END in text:
        before, rest = text.split(BEGIN, 1)
        after = rest.split(END, 1)[1]
        text = before.rstrip("\n") + ("\n" if before.strip() else "") + after.lstrip("\n")
    if block is None:
        return text
    return text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"{BEGIN}\n{block.strip()}\n{END}\n"


def merged_hooks(settings: dict, install: bool, agent="claude") -> dict:
    events = CODEX_HOOK_EVENTS if agent == "codex" else HOOK_EVENTS
    hooks = settings.setdefault("hooks", {})
    command = hook_command(agent)
    # Remove the OptChat hook from every event, also from events that install
    # no longer uses, then add it back for the current events.
    for event in list(hooks) + [e for e in events if e not in hooks]:
        matcher = events.get(event)
        entries = [e for e in hooks.get(event, []) if not any(h.get("command") == command for h in e.get("hooks", []))]
        if install and event in events:
            handler = {"type": "command", "command": command, "timeout": 10}
            if agent == "codex":
                handler["async"] = True
            entry = {"hooks": [handler]}
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


def server_args(cli, name="optchat"):
    """The arguments with which Claude Code or Codex starts the server, or None."""
    exe = shutil.which(cli)
    found = exe and subprocess.run([exe, "mcp", "get", name] + (["--json"] if cli == "codex" else []), capture_output=True, text=True)
    if not found or found.returncode:
        return None
    try:
        if cli == "codex":
            return json.loads(found.stdout)["transport"]["args"]
        return next(line.split(":", 1)[1].split() for line in found.stdout.splitlines() if line.strip().startswith("Args:"))
    except (ValueError, KeyError, TypeError, StopIteration):
        return None


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

    # Start the memory service automatically. It runs no model, unless the user stores an API key.
    env = {"PYTHONPATH": str(REPO), "OPTCHAT_CONFIG": str(CONFIG)}
    # A hook or MCP client may have started a copy outside launchd or systemd.
    # Stop whichever copy runs, so the restarted service loads the new code.
    plan.run([RUN, "--chat", chat, "stop"], "stop the running memory service", check=False)
    if sys.platform == "darwin":
        plist = HOME / f"Library/LaunchAgents/{LABEL}.plist"
        body = plistlib.dumps({"Label": LABEL, "ProgramArguments": [python(), "-m", "optchat", "--chat", str(chat), "serve", "--wait"],
                               "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
                               "StandardOutPath": str(chat / "service.log"), "StandardErrorPath": str(chat / "service.log")}).decode()
        plan.write(plist, body, "start at login and after an exit")
        launchd_reload(plan, plist)
    else:
        unit = HOME / ".config/systemd/user/optchat.service"
        body = "\n".join(["[Unit]", "Description=OptChat memory service", "After=network-online.target", "",
                          "[Service]", *[f"Environment={k}={v}" for k, v in env.items()],
                          f"ExecStart={python()} -m optchat --chat {chat} serve --wait", "Restart=always", "RestartSec=10", "",
                          "[Install]", "WantedBy=default.target", ""])
        plan.write(unit, body, "start at boot and after an exit")
        plan.run(["systemctl", "--user", "daemon-reload"], "load the unit")
        plan.run(["systemctl", "--user", "enable", "optchat.service"], "start at boot")
        plan.run(["systemctl", "--user", "restart", "optchat.service"], "start now with the current code")

    # MCP for Claude Code and Codex: only the tools that read the memory. Hooks record
    # the chats, and the compactor subagent starts its own server with the job tools.
    for cli, add, remove in (("claude", ["claude", "mcp", "add", "--scope", "user"], ["claude", "mcp", "remove", "--scope", "user", "optchat"]),
                             ("codex", ["codex", "mcp", "add"], ["codex", "mcp", "remove", "optchat"])):
        args_now = server_args(cli) if shutil.which(cli) else None
        if not shutil.which(cli):
            print(f"  skip {cli}: not installed")
        elif args_now == MCP_ARGS:
            print(f"  ok  {cli} MCP server 'optchat' already registered")
        else:
            if args_now is not None:
                plan.run(remove, f"remove the old {cli} registration")
            plan.run(add + ["optchat", "--", RUN, *MCP_ARGS], f"offer the memory tools in every {cli} session")

    # Hooks record every Claude session. The compactor subagent writes summaries.
    claude_home = HOME / ".claude"
    if shutil.which("claude") or claude_home.exists():
        settings_path = claude_home / "settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        settings = merged_hooks(settings, True)
        if "statusLine" not in settings:
            # The backlog shows in the status line, outside the context of any agent.
            settings["statusLine"] = {"type": "command", "command": f"{RUN} status-line"}
        plan.write(settings_path, json.dumps(settings, indent=2) + "\n", "hooks that record every Claude session, and the status line")
        agent = (REPO / "integrations/optchat-compactor.md").read_text().replace("{RUN}", str(RUN))
        plan.write(claude_home / "agents/optchat-compactor.md", agent, "compaction subagent with its own job server")
        command = (REPO / "integrations/optchat-compact-command.md").read_text().replace("{RUN}", str(RUN))
        plan.write(claude_home / "commands/optchat-compact.md", command, "the /optchat-compact command")

    # Hooks record every Codex session.
    codex_home = HOME / ".codex"
    if codex_home.exists() or shutil.which("codex"):
        path = codex_home / "hooks.json"
        hooks = merged_hooks(json.loads(path.read_text()) if path.exists() else {}, True, "codex")
        plan.write(path, json.dumps(hooks, indent=2) + "\n", "hooks that record every Codex session; trust them once with /hooks in Codex")

    # Instructions for every agent.
    block = (REPO / "integrations/AGENTS.optchat.md").read_text()
    targets = [claude_home / "CLAUDE.md"] + ([HOME / ".codex/AGENTS.md"] if (HOME / ".codex").exists() or shutil.which("codex") else [])
    for path in targets:
        old = path.read_text() if path.exists() else ""
        plan.write(path, managed_block(old, block), "adds the OptChat section; your own text stays")

    # Plugin for Antigravity: MCP server, Stop hook and rules in one bundle.
    agy_config = HOME / ".gemini/config"
    if agy_config.exists() or (HOME / ".gemini/antigravity").exists() or shutil.which("agy"):
        plugin = agy_config / "plugins/optchat"
        for rel, content in antigravity_plugin_files(block).items():
            plan.write(plugin / rel, content, "Antigravity OptChat plugin", backup=False)
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
        settings = merged_hooks(json.loads(settings_path.read_text()), False)
        if settings.get("statusLine", {}).get("command") == f"{RUN} status-line":
            settings.pop("statusLine")
        plan.write(settings_path, json.dumps(settings, indent=2) + "\n", "remove OptChat hooks and status line")
    codex_hooks = HOME / ".codex/hooks.json"
    if codex_hooks.exists():
        plan.write(codex_hooks, json.dumps(merged_hooks(json.loads(codex_hooks.read_text()), False, "codex"), indent=2) + "\n", "remove OptChat hooks")
    plan.remove(HOME / ".claude/agents/optchat-compactor.md", "compaction subagent")
    plan.remove(HOME / ".claude/commands/optchat-compact.md", "the /optchat-compact command")
    for path in (HOME / ".claude/CLAUDE.md", HOME / ".codex/AGENTS.md"):
        if path.exists() and BEGIN in path.read_text():
            plan.write(path, managed_block(path.read_text(), None), "remove the OptChat section")
    agy_plugin = HOME / ".gemini/config/plugins/optchat"
    for rel in ("plugin.json", "mcp_config.json", "hooks.json", "rules/AGENTS.md"):
        plan.remove(agy_plugin / rel, "Antigravity OptChat plugin")
    plan.rmdir(agy_plugin / "rules")
    plan.rmdir(agy_plugin)
    if not args.apply:
        print("Dry run only. Re-run with --apply to make these changes.")
