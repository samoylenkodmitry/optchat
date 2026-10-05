## Shared memory (OptChat)

OptChat is one memory for all Claude Code and Codex agents of the user, on all machines and in all projects. It is an MCP server named `optchat` that stores the history and its summaries. It never runs a model.

- At the start of a session, before real work, call `view` and read every page. Follow `next_offset` with the same `snapshot`. Each summary names the origin of its items in the form project/agent@machine. A decision in one project is a rule for another project only when the user said so.
- When a summary only mentions something that you need, call `zoom(id, n)` before you act or ask the user. Examples are a decision, a correction, an earlier attempt or the place of a file. `zoom(id, 1)` returns the original message. `read_message` returns a long message in pages. `date(id)` returns the time of a message.
- The `pending` and `unordered` ranges of the view have no summary or no place in the order yet. Treat their content as unknown, and read the originals with `zoom` when they matter.
- Hooks record Claude sessions, so Claude agents do not call `append`. In Codex, record each message of the user and your final reply with `append(event_id, kind, text, origin)`. Use the kinds `user` and `talk`. Set `origin.project` and `origin.session`, and set `origin.agent` to `codex`.
- Summaries keep little of the tool output, so write in your replies what you learned that will matter later.
- When a hook message from OptChat asks for compaction, start one `optchat-compactor` subagent in the background if subagents are available, and continue your task. Do not start it for every pending message, because each new worker reads the whole view. Never compact through a headless model or an API call. Never record reasoning or compaction work.
