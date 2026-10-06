---
description: Summarize the OptChat history now with one compactor subagent
---

Start one `optchat-compactor` subagent in the background to summarize the OptChat history, and continue with the current task. When it finishes, tell me in one line how many lines it wrote and whether work remains.

The OptChat tools load only in sessions that started after OptChat was installed. If this session has no `mcp__optchat__` tools, do not start the subagent. Tell me that a new session is needed for the summarization.
