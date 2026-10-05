# OptChat

OptChat keeps one memory for the Claude Code and Codex agents of one user, on all of the user's machines. Each message of a session is stored word for word. This includes the tool calls of the agent and their results.

The messages are folded into a binary tree of one-line summaries. At the start of a session, an agent reads a view of about 128 KB that covers the whole history. Recent messages have one line each, and older lines cover more messages. When a line is too vague, the agent opens it into the two lines from which it was made, down to the original message.

OptChat is an MCP server in Python 3.11 or newer, with no dependencies. It never runs a model and needs no API key. The agents write the summaries themselves. When messages wait for summaries, an agent starts a small subagent, which takes jobs through MCP tools and submits the summary lines.

The idea of a memory that consists of its own compressed history comes from the OptChat design, which grew out of [OptMem](https://github.com/VictorTaelin/OptMem). This repository does not include the original specification.

## How it works

- Log: every message is appended to `~/.local/share/optchat/chat`, with one write and one fsync per record. Nothing is edited or deleted.
- Tree: each message gets a summary line of at most 512 bytes. Two neighboring lines merge into one line, two of those merge again, and so on. A short message or a short pair of lines needs no model, because it is its own line.
- View: a list of tree lines that covers the whole history in about 128 KB. New messages are added at the end. When the view grows too large, the pair that is oldest for its size merges, so detail fades with age.
- Compaction jobs: a job holds up to 10 tasks, for example consecutive messages or merges of two lines. The first job of a worker brings the view as context, and later jobs bring only the changes. A worker subagent calls `compact_next`, which returns the job text, and answers with `compact_submit`. The reply to a submit holds the next job, so each job costs one request. A worker sees at most 8,000 characters of a tool call or result, and the log keeps the original.
- Cost: the `optchat-compactor` subagent runs on Claude Haiku. When at least 20 messages or 40 lines wait, the agent asks the user once per chat whether to summarize now, and it starts the subagent only after the user agrees. The question comes again after 50 more messages, and never while a compactor works. The command `/optchat-compact` starts a run at any time.
- Recording: Claude Code hooks record your prompts and the final replies of the agents word for word. The tool calls of one turn become one short record of what changed and what failed. Exploration is kept only as counts and a few file names, because its content stays on disk. Your answers to agent questions are kept in full. Subagent reports and approved plans are kept up to 4,000 characters. Codex agents record your messages and their final replies with `append`.

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
- It registers the `optchat` MCP server for Claude Code and Codex.
- It adds the recording hooks, the `optchat-compactor` subagent and the `/optchat-compact` command to Claude Code.
- It appends a short OptChat section to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`.

Every changed file keeps a backup with a time stamp. `./run uninstall --apply` removes all of this and keeps the memory.

Without a config file, OptChat keeps a memory for one machine only.

## MCP tools

- `view`: read the current view in pages. Follow `next_offset` with the same `snapshot`.
- `zoom(id, n)`: open line `id+n` into its two halves. With `n` set to 1 it returns the original message.
- `read_message(id, offset)`: read a long original message in pages.
- `date(id)`: the time of message `id`.
- `append(event_id, kind, text, origin)`: record a message. The kinds are `user`, `talk`, `tool`, `echo` and `note`.
- `status`: the backlog, failed jobs, the hook queue and the replication state.
- `compact_next`, `compact_read`, `compact_submit`, `compact_release`, `compact_resume`: the job protocol of the worker subagent.

## Commands

```sh
./run status
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
  spool/                hook events, written to disk before delivery
```

A `flock` makes one service the only writer of a chat directory. The operating system releases it when the process ends. After a crash, torn lines are skipped and reported. A gap or a conflict stops the start, and the service does not guess. Hook events are written to disk before delivery and are delivered again after a restart. Invalid events stay in `spool/failed/`. Job leases expire after five minutes. After three explicit failures a line pauses until `compact_resume`.

## Limits

- The Claude Code and Codex sessions own their context and their cache. Clear a session to start fresh. The agent then reads the memory again.
- The worker subagent decides the quality of a summary. The server checks order and size, and it checks that the worker read the whole input.
- The memory keeps no file contents and no tool output. Agents should write what they learned into their replies.
- The log is permanent. Do not paste secrets into recorded sessions.

## Tests

```sh
python3 -m unittest
```

## License

MIT
