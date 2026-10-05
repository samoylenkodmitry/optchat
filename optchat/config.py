"""Per-machine settings. They live outside the repository, e.g.

~/.config/optchat/config.json
{
  "machine": "mac",
  "chat": "~/.local/share/optchat/chat",
  "exchange": {"type": "rclone", "remote": "my-crypt:optchat", "rclone": "/usr/bin/rclone"},
  "interval": 15,
  "offline_after": 600
}

Replication only applies to the configured chat directory, so tests and
scratch memories never touch the shared folder.
"""
import json
import os
from pathlib import Path

DEFAULT_CHAT = Path.home() / ".local/share/optchat/chat"


def config_path():
    return Path(os.environ.get("OPTCHAT_CONFIG", "~/.config/optchat/config.json")).expanduser()


def load_config(chat=None):
    path = config_path()
    if not path.exists():
        return {}
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError(f"{path} must contain a JSON object")
    configured = Path(config.get("chat", DEFAULT_CHAT)).expanduser().resolve()
    if chat is not None and Path(chat).expanduser().resolve() != configured:
        return {}
    return config
