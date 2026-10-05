# OptChat — one memory for all your coding agents

OptChat gives every Claude Code and Codex session, on every machine you use, the same long-term memory. Each message (your words, the agent's replies, its tool calls and results) is kept forever, word for word, and folded into a binary tree of one-line summaries. An agent starting a fresh session reads a fixed-size view of the whole history: recent messages one line each, older ones many per line. When a line is too vague, it zooms into the lines it was made from, down to the original message.

OptChat is an MCP server written in plain Python (3.11+, no dependencies). **It never runs a model and needs no API key.** Summaries are written by your own agents: when work is pending, the agent hands it to a small subagent that claims jobs through MCP tools, writes the summary lines and submits them.

The design follows the OptChat idea of a chat whose memory is its own compressed history, which grew out of [OptMem](https://github.com/VictorTaelin/OptMem). The original specification is not included here.

## How it works

- **Log.** Every message is appended, with one write and fsync per record, to `~/.local/share/optchat/chat`. Nothing is edited or deleted.
- **Tree.** Message `i` gets a one-line summary of at most 512 bytes. Two adjacent lines merge into one, two of those into one, and so on. Short messages and short pairs need no model; they are their own line.
- **View.** A list of tree lines that covers the whole history in about 128 KB. New messages append at the end, and the most overdue old pair merges, so detail fades with age.
- **Compaction jobs.** Lines are summarized strictly in order, and each job carries the view before it as context. A worker subagent calls `compact_next`, reads the job with `compact_read`, and submits with `compact_submit`. If a line is too long, the server says by how much.
- **Recording.** Claude Code hooks record every session automatically. Codex agents record user messages and their final replies with `append`.

## Several machines, no owner

Machines share one memory through a folder they can all reach, for example an encrypted rclone remote. Each machine runs its own OptChat service and writes only its own files:

```text
<shared folder>/machines/<machine-id>/messages/<first>-<last>.jsonl.gz   its messages, gzip
<shared folder>/machines/<machine-id>/summaries/<first>-<last>.jsonl.gz  summaries its agents wrote
<shared folder>/machines/<machine-id>/heartbeat.json                     "everything I wrote up to T is uploaded"
```

Because no file has two writers, a file-level sync can never lose data.

- **One order, no owner.** Every machine orders messages by time once the heartbeats of all active machines have passed them, so machines online together compute the same order on their own.
- **Offline is fine.** A machine silent for 10 minutes is skipped. What it records while away is placed after the others notice it again. Only that stretch may be grouped differently on each machine; the orders line up again after it.
- **Shared summaries.** A summary is filed under the exact messages it covers. When one machine's agent writes it, the others reuse it instead of summarizing again.
- **Reading the folder.** The rclone remote is read directly, not through a mount, so mount caches don't delay the exchange. The folder holds the complete history, compressed. With an encrypted remote it is encrypted at rest.

Keep the machines' clocks synchronized (NTP). A large clock skew only delays ordering, or groups a short stretch differently.

## Install

On each machine, from a clone of this repository:

```sh
./run install --machine laptop --remote my-crypt:optchat      # dry run: shows every change
./run install --machine laptop --remote my-crypt:optchat --apply
```

This writes `~/.config/optchat/config.json` and starts the service at login (launchd on macOS, a systemd user unit on Linux). It also registers the `optchat` MCP server with Claude Code and Codex, adds the recording hooks and the `optchat-compactor` subagent to Claude Code, and appends a short OptChat section to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. Every edited file keeps a timestamped backup. `./run uninstall --apply` removes all of it and keeps the memory.

Without `--remote` settings (no config file), OptChat is a single-machine memory.

## MCP tools

| Tool | Purpose |
|---|---|
| `view` | Read the current view in pages (follow `next_offset` with the same `snapshot`). |
| `zoom(id, n)` | Open line `id+n` into its two halves; `n = 1` returns the original message. |
| `read_message(id, offset)` | Page through a long original. |
| `date(id)` | When message `id` was written. |
| `append(event_id, kind, text, origin)` | Record a message (kinds `user`, `talk`, `tool`, `echo`, `note`). |
| `status` | Backlog, failures, hook queue and replication state. |
| `compact_next`, `compact_read`, `compact_submit`, `compact_release`, `compact_resume` | The compaction job protocol used by the worker subagent. |

## Commands

```sh
./run status
./run view
./run zoom 0 1
./run append note 'A durable decision.'
./run import old-notes.txt          # or a .jsonl of {kind, text, date}
./run export memory.html            # every original, summary and tree level
./run backup memory.tar.gz
./run stop                          # the history stays on disk
./run serve                         # foreground service
```

## Data and durability

```text
~/.local/share/optchat/chat/
  main/  tree/          the local order of messages and their summaries (append-only JSONL)
  own/  remote/         this machine's messages and those received from others
  pool/  outsum/        summaries received from others and written here
  delivery.sqlite3      event ids for idempotent hook delivery and crash recovery
  jobs.json  sync.json  job progress and replication state
  spool/                hook events, written to disk before delivery
```

A `flock` makes one service the only writer of a chat directory, and it is released automatically if the process dies. Torn lines from a crash are skipped and reported. Any gap or conflict stops startup instead of guessing. Hook events are written to disk before delivery and replayed after a restart; invalid ones are kept in `spool/failed/`. Job leases expire after five minutes. Explicit failures pause a line after three attempts, until `compact_resume`.

## Limits

- Your normal Claude and Codex sessions own their context and caching. Clear a session to start fresh; the memory is read again at the start.
- Summary quality is up to the worker subagent. The server enforces order, size and completeness, not meaning.
- Recording is as complete as the hooks or explicit appends. Tool results are capped at 30,000 characters (start and end kept).
- The log is permanent by design. Don't paste secrets into sessions that are recorded.

## Tests

```sh
python3 -m unittest
```

## License

MIT
