# OptChat

OptChat keeps one memory for the Claude Code and Codex agents of one user, on all of the user's machines. Your prompts and the final replies of the agents are stored word for word. The tool calls of one turn are kept as one short record, without their output.

The messages are folded into a binary tree of one-line summaries. The view, about 128 KB, covers the whole history: recent messages have one line each, and older lines cover more messages. When a line is too vague, an agent opens it into the two lines from which it was made, down to the original message. An agent uses the memory only when its task needs it, and starts with a search by words.

OptChat is an MCP server in Python 3.11 or newer, with no dependencies. Without an API key it runs no model, and the agents write the summaries themselves: when you type `/optchat-compact`, the agent starts a small subagent, which takes jobs through MCP tools and submits the summary lines. When you store an Anthropic API key, the service writes the summaries itself with Claude Haiku on your API credit, and the subagent takes over whenever the API fails.

The idea of a memory that consists of its own compressed history comes from the OptChat design, which grew out of [OptMem](https://github.com/VictorTaelin/OptMem). This repository does not include the original specification.

## How it works

- Log: every message is appended to `~/.local/share/optchat/chat`, with one write and one fsync per record. Nothing is edited or deleted.
- Tree: each message gets a summary line of at most 512 bytes. Two neighboring lines merge into one line, two of those merge again, and so on. A short message or a short pair of lines needs no model, because it is its own line.
- View: a list of tree lines that covers the whole history in about 128 KB. New messages are added at the end. When the view grows too large, the pair that is oldest for its size merges, so detail fades with age.
- Compaction jobs: a job holds up to 10 tasks, for example consecutive messages or merges of two lines. The first job of a worker brings the view as context, and later jobs bring only the changes. A worker subagent calls `compact_next`, which returns the job text, and answers with `compact_submit`. The reply to a submit holds the next job, so each job costs one request. A worker sees at most 8,000 characters of a tool call or result, and the log keeps the original.
- Cost: the `optchat-compactor` subagent runs on Claude Haiku, and only when you type `/optchat-compact`. A run covers the messages that existed when it started. The Claude Code status line shows how many messages wait, once at least 30 wait. Nothing about the backlog goes into the context of an agent. With an API key, the service does this work and the subagent is needed only when the API fails (see below).
- Recording: Claude Code hooks record your prompts and the final replies of the agents word for word. The tool calls of one turn become one short record of what changed and what failed. Exploration is kept only as counts and a few file names, because its content stays on disk. Your answers to agent questions are kept in full. Subagent reports, also those of background subagents, and approved plans are kept up to 4,000 characters. Messages from other Claude sessions are kept up to 4,000 characters too. Claude Code also submits notices of the app and of background tasks as prompts. OptChat does not keep them. Codex hooks record your prompts and the final replies of the agents. They run in the background, so they never delay a turn. The Codex app also sends prompts that ask for task suggestions. OptChat does not keep them, and search and thread skip those that older versions stored.

## Summaries on API credit

```sh
./run api-key
```

The command prints the link to the key page of the Claude Console, https://platform.claude.com/settings/keys, and opens it when the machine has a desktop. You paste the key, and the input stays hidden. One request of a few tokens checks the key, and the key goes to `~/.config/optchat/anthropic-api-key`, which only your user can read. `./run api-key --remove` deletes it.

Run the command on one machine. The other machines get the key through the shared folder within about a minute, and a removal reaches them the same way:

- The machine writes `{"key": ..., "date": ...}` to `machines/<its id>/api-key.json`. rclone gets the data on stdin, so the key never appears on a command line.
- Its heartbeat holds only the date of the change. A machine that sees a newer date reads the file and stores the key in its own `~/.config/optchat/anthropic-api-key`. A machine that had shared an older key deletes its file.
- The key has the same protection as the memory: an encrypted remote keeps it encrypted at rest, and whoever can read the shared folder can read the key. Give the key its own workspace with a spend limit in the Console, so that a leaked key can spend only that limit. Revoke it in the Console if a machine is lost.

With a key, the memory service sends the compaction jobs to Claude Haiku 5.5 (`claude-haiku-5-5`, low effort). It starts once 10 summaries wait, or once fewer have waited for 10 minutes without new messages. Each worker is one conversation in the prompt cache, so a job costs a small fraction of a cent. `./run status` shows the lines written and the estimated dollars under `api`.

When a request fails, the service pauses the API and gives the job back, and the job does not count as a failure of the messages:

| Failure | Next automatic try |
|---|---|
| no credit (HTTP 402) | after 1 hour |
| key rejected or access denied | after a new key |
| rate limited | after the time that the API asks for, at least 1 minute |
| API not reachable or overloaded | after 5 minutes |
| any other error | after 1 hour |

While the API is paused, the status line says why, for example `OptChat: 45 to summarize, API: no credit`. `/optchat-compact` first runs `./run compact`, which asks the service to try the API at once. When the API works, the service writes the summaries in the background. When it fails or no key is stored, the agent starts the compactor subagent as before.

Each heartbeat says whether the API works on that machine. Of the online machines whose API works, the first by machine id writes the summaries, and the other machines get the lines through the shared folder. When that machine goes offline or its API pauses, the next one takes over. `./run status` shows `runs_here` under `api`. `/optchat-compact` tries the API on its own machine in any case.

## Several machines without an owner

The machines share one memory through a folder that all of them can reach, for example an encrypted rclone remote. Each machine runs its own OptChat service and writes only its own files:

```text
<shared folder>/machines/<machine-id>/messages/<first>-<last>.jsonl.gz   messages of this machine
<shared folder>/machines/<machine-id>/summaries/<first>-<last>.jsonl.gz  summaries that its agents wrote
<shared folder>/machines/<machine-id>/heartbeat.json                     time up to which everything is uploaded
```

No file has two writers, so a sync of whole files cannot lose data.

- Order: each machine orders the messages by time after the heartbeats of all active machines have passed them. Machines that are online at the same time therefore compute the same order, and no machine acts as an owner.
- Offline machines: after 10 minutes without a heartbeat, the other machines stop waiting for a machine. When it comes back, the messages that it recorded while away are placed after the point at which the others notice it again. The machines may group that one stretch in different ways. The orders match again after it.
- Shared summaries: a summary is stored under the exact list of messages that it covers. When an agent on one machine writes it, the other machines reuse it and do not summarize those messages again.
- Access: the service reads the rclone remote directly, so the cache of a mount cannot delay the exchange. The folder holds the complete history in compressed form. An encrypted remote also keeps it encrypted at rest.

Keep the clocks of the machines synchronized with NTP. A large clock difference delays the order or groups a short stretch in different ways.

## Install

On each machine, in a clone of this repository:

```sh
./run install --machine laptop --remote my-crypt:optchat
./run install --machine laptop --remote my-crypt:optchat --apply
```

The first command prints every change. The second command makes the changes:

- It writes `~/.config/optchat/config.json`.
- It starts the service at login, with launchd on macOS or a systemd user unit on Linux.
- It registers the `optchat` MCP server for Claude Code and Codex, with only the six tools that read the memory. Hooks record the chats, so agents never call a tool to record them.
- It adds the recording hooks, the `optchat-compactor` subagent, the `/optchat-compact` command and a status line to Claude Code. The command tries the API first when a key is stored. An existing status line stays. The subagent starts its own server with the job tools, so other sessions never load them.
- It adds the recording hooks to `~/.codex/hooks.json`. Codex runs a new hook only after you trust it once with `/hooks`.
- It appends a short OptChat section to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`.

Every changed file keeps a backup with a time stamp. `./run uninstall --apply` removes all of this and keeps the memory.

Without a config file, OptChat keeps a memory for one machine only.

## MCP tools

Chats get the tools that read the memory (`run mcp --memory-only`):

- `search(query, limit)`: find earlier messages by words. It returns up to 10 short lines with ids by default, so an agent does not need the whole view. A line reads `id+0|kind@folder~hash/agent@machine#session date: text`. `#session` is the first 6 characters of the SHA-256 of the session id and is the same for one chat. The folder changes when the chat changes directory.
- `thread(id, n)`: the next `n` messages (default 5, at most 30) after message `id` in its session, as search lines. Continue from the last id.
- `view`: read the current view in pages. Follow `next_offset` with the same `snapshot`.
- `zoom(id, n)`: open line `id+n` into its two halves. With `n` set to 1 it returns the original message, up to 12,000 characters. `read_message` gives the rest.
- `read_message(id, offset)`: read a long original message in pages.
- `date(id)`: the time of message `id`.

The compactor subagent gets the job tools (`run mcp --jobs`): `compact_next`, `compact_read`, `compact_submit` and `compact_release`. `run mcp` without an option offers all tools, also `append(event_id, kind, text, origin)`, `status` and `compact_resume`.

## Commands

```sh
./run status
./run api-key                       # store an API key; --remove deletes it
./run compact                       # write summaries with the API now; exit status 3 if it fails
./run view
./run zoom 0 1
./run append note 'A decision to keep.'
./run import old-notes.txt          # or a .jsonl file of {kind, text, date, origin}
./run export memory.html            # every original message and every tree level
./run backup memory.tar.gz
./run stop                          # the history stays on disk
./run serve                         # run the service in the foreground
```

## Data and durability

```text
~/.local/share/optchat/chat/
  main/  tree/          local order of the messages and their summaries (append-only JSONL)
  own/  remote/         messages of this machine and messages from other machines
  pool/  outsum/        summaries from other machines and summaries written here
  delivery.sqlite3      event ids for hook delivery and crash recovery
  jobs.json  sync.json  job progress and replication state
  api.json              lines and estimated dollars of the API worker
  spool/                hook events, written to disk before delivery
```

A `flock` makes one service the only writer of a chat directory. The operating system releases it when the process ends. After a crash, torn lines are skipped and reported. A gap or a conflict stops the start, and the service does not guess. Hook events are written to disk before delivery and are delivered again after a restart. Invalid events stay in `spool/failed/`. Job leases expire after five minutes. After three explicit failures a line pauses until `compact_resume`.

## Limits

- The Claude Code and Codex sessions own their context and their cache. Clear a session to start fresh. The agent then reads the memory again.
- The worker subagent decides the quality of a summary. The server checks order and size, and it checks that the worker read the whole input.
- The memory keeps no file contents and no tool output. Agents should write what they learned into their replies.
- A chat that started before OptChat was installed is recorded, but it has no OptChat tools, because Claude Code loads MCP servers when a session starts. Start a new session to read the memory or to summarize.
- The log is permanent. Do not paste secrets into recorded sessions.

## Tests

```sh
python3 -m unittest
```

## License

MIT
