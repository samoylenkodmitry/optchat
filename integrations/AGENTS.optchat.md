## Shared memory (OptChat)

The MCP server `optchat` holds the chats of the user from Claude Code, Codex and Antigravity on all machines. Hooks record them. Use it only when the task needs earlier decisions or work: `search(query)` first, then `zoom(id, 1)` for a whole message. A decision in one project is a rule for another project only when the user said so.
