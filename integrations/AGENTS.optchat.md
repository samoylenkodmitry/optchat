## Shared memory (OptChat)

OptChat is one memory shared by every Claude and Codex agent on all of the user's machines and projects. It is an MCP server (`optchat`) that stores the history and its summaries; it never runs a model.

- At the start of a session, before substantive work, call `view` and read every page (follow `next_offset` with the same `snapshot`). Summaries name the project, agent and machine; a decision in one project is not a rule for another unless the user said so.
- When a summary only mentions something you need (a decision, correction, earlier attempt, file location), call `zoom(id, n)` before you act, guess or ask; `zoom(id, 1)` gives the original message, `read_message` pages a long one, `date(id)` gives its time.
- `pending` and `unordered` ranges in the view are not yet summarized or placed: missing knowledge, not evidence that nothing happened. Read the originals with `zoom` if they matter.
- Claude sessions are recorded automatically by hooks; do not append them again. In Codex, record each user message and your final reply with `append(event_id, kind, text, origin)` (kinds `user`/`talk`, origin = project root, session, `codex`).
- Summaries keep little of tool output, so state in your replies what you learned that will matter later.
- When OptChat reports pending compaction, start the `optchat-compactor` subagent in the background if subagents are available, and continue your task. Never compact through a headless model or an API call, and never record reasoning or compaction traffic.
