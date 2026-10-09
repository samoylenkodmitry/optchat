---
description: Summarize the OptChat history now with one compactor subagent
---

Start one `optchat-compactor` subagent in the background to summarize the OptChat history, and continue with the current task. If it reports rotate, start one new compactor subagent for the rest. When a subagent reports done, waiting or blocked, tell me the result in one line. Messages that arrived during the run wait for a later run, so do not suggest another run for them.

The OptChat tools load only in sessions that started after OptChat was installed. If this session has no `mcp__optchat__` tools, do not start the subagent. Tell me that a new session is needed for the summarization.
