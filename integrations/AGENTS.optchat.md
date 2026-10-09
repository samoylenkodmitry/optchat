## Shared memory (OptChat)

The MCP server `optchat` holds the memory of all Claude Code and Codex chats of the user, on all machines. Use it only when the task needs earlier decisions, preferences or work from other chats. Start with `search(query)`, and call `zoom(id, 1)` for a whole message. Read the whole `view` only when you need broad context. A decision in one project is a rule for another project only when the user said so.

- Hooks record Claude chats. In Codex, record each message of the user and your final reply with `append`.
- The memory keeps your replies word for word and keeps no tool output, so write in your replies what you learned that will matter later.
- Summarize only when the user asks, for example with `/optchat-compact`. Never record reasoning.
