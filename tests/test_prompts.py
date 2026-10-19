import tempfile
import unittest
from pathlib import Path

from optchat.hooks import enqueue
from optchat.service import Service

NOTICE = """<task-notification>
<task-id>b1</task-id>
<tool-use-id>{tool}</tool-use-id>
<output-file>/tmp/tasks/b1.output</output-file>
<status>completed</status>
<summary>{summary}</summary>
{result}
</task-notification>"""


class Prompts(unittest.TestCase):
    """Claude Code submits more than the words of the user through UserPromptSubmit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(Path(self.temp.name))
        self.hook({"hook_event_name": "SessionStart"})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def hook(self, event):
        # The path of a real hook: the client spools the event, then the service delivers it.
        name = enqueue(self.service.store.path, {"session_id": "s", "cwd": self.temp.name, **event})
        return self.service.call("flush_hooks", {"current": name})

    def prompt(self, text, prompt_id="p1"):
        return self.hook({"hook_event_name": "UserPromptSubmit", "prompt_id": prompt_id, "prompt": text})

    def records(self):
        return [(m.kind, m.text) for m in self.service.store.root]

    def test_prompts_sent_during_one_turn_are_all_kept(self):
        self.prompt("fix the build")
        self.assertNotIn("rejected", self.prompt("then merge it"), "Claude Code gives both prompts one prompt id")
        self.prompt("then merge it")  # the same prompt delivered twice is kept once
        self.assertEqual(self.records(), [("user", "fix the build"), ("user", "then merge it")])
        self.assertEqual(list((self.service.store.path / "spool").glob("failed/*")), [])

    def test_notices_stay_out(self):
        self.prompt(NOTICE.format(tool="toolu_bash", summary='Background command "make" completed', result=""))
        self.prompt(NOTICE.format(tool="toolu_x", summary="Monitor event", result="<event>line</event>"))
        self.prompt('<ci-monitor-event>"Auto-fix pull requests" was just enabled.</ci-monitor-event>')
        self.prompt("<system-reminder>The user started a task.</system-reminder>")
        self.assertEqual(self.records(), [])

    def test_app_context_is_cut_from_the_words_of_the_user(self):
        self.prompt("<system-reminder>\nYou are in a worktree.\n</system-reminder>\nkeep the old API")
        self.prompt('<artifact-view-context artifact="a1">page</artifact-view-context>\ndo not backport', "p2")
        self.assertEqual(self.records(), [("user", "keep the old API"), ("user", "do not backport")])

    def test_background_subagent_report_is_kept_once_and_its_launch_receipt_never(self):
        launch = {"hook_event_name": "PostToolUse", "tool_use_id": "toolu_a", "tool_name": "Agent",
                  "tool_input": {"subagent_type": "Explore", "description": "Find the cache", "run_in_background": True},
                  "tool_response": {"isAsync": True, "status": "async_launched", "agentId": "a1", "prompt": "LONG PROMPT"}}
        self.hook(launch)
        self.hook({"hook_event_name": "Stop", "turn_id": "t1", "last_assistant_message": "started"})
        report = NOTICE.format(tool="toolu_a", summary='Agent "Find the cache" finished', result="<result>The cache is in src/cache.rs.</result>")
        self.prompt(report)
        self.prompt(report, "p2")  # the same notice delivered again
        self.prompt(report.replace("src/cache.rs.", "src/cache.rs, and it leaks."), "p3")  # the agent was resumed
        self.assertEqual(self.records(), [("talk", "started"),
                                          ("echo", "Report of the Explore subagent (Find the cache):\nThe cache is in src/cache.rs."),
                                          ("echo", "Report of the Explore subagent (Find the cache):\nThe cache is in src/cache.rs, and it leaks.")])
        self.assertNotIn("LONG PROMPT", str(self.records()))

    def test_compactor_report_stays_out(self):
        self.hook({"hook_event_name": "PostToolUse", "tool_use_id": "toolu_c", "tool_name": "Agent",
                   "tool_input": {"subagent_type": "optchat-compactor", "run_in_background": True},
                   "tool_response": {"isAsync": True, "status": "async_launched"}})
        self.prompt(NOTICE.format(tool="toolu_c", summary='Agent "Summarize" finished', result="<result>Status: rotate.</result>"))
        self.assertEqual(self.records(), [])

    def test_codex_suggestion_prompts_stay_out_and_old_ones_stay_out_of_search(self):
        suggestions = ("# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with Codex in this local project: /Users/s\n\n"
                       "# Rules\n\nYour suggestions must be based on recent events; e.g. recent Slack messages, unread emails, newly created issues, etc.")
        mention = 'Prompts of the Codex app contain "suggestions must be based on recent events; e.g. recent Slack messages, unread emails".'
        self.assertEqual(self.prompt(suggestions), {"ignored": "a notice or a suggestion prompt of an app, or a notice of a background task"})
        self.prompt(mention, "p2")
        self.assertEqual(self.records(), [("user", mention)], "a user message that quotes the prompt is kept")
        # An older version stored the prompt as a message of the user. The log keeps it, and search skips it.
        self.service.call("append", {"event_id": "old", "kind": "user", "text": suggestions})
        self.assertEqual(len(self.service.store.root), 2)
        lines = self.service.call("search", {"query": "unread emails"})["text"].splitlines()
        self.assertEqual(lines[0], "1 messages match; the best 1 follow. zoom(id, 1) gives a whole message.")
        self.assertEqual([l.split("+")[0] for l in lines[1:]], ["0"])

    def test_message_from_another_session_and_terminal_command(self):
        self.prompt('<cross-session-message from="uds:/tmp/x.sock" from-session="local_1" from-name="Perf runs" from-mode="bypass">\n'
                    "Regression found: 22 to 8 fps.\n</cross-session-message>")
        self.prompt("<bash-input>git pull</bash-input><bash-stdout>Already up to date.</bash-stdout><bash-stderr></bash-stderr>", "p2")
        self.assertEqual(self.records(), [("echo", "Message from the session Perf runs:\nRegression found: 22 to 8 fps."),
                                          ("user", "The user ran in the terminal: git pull")])

    def test_codex_chat_is_recorded_from_its_first_prompt_and_subagents_stay_out(self):
        codex = lambda event: self.service.call("flush_hooks", {"current": enqueue(self.service.store.path, {"session_id": "c", **event}, "codex")})
        codex({"hook_event_name": "UserPromptSubmit", "turn_id": "t1", "agent_id": "child", "prompt": "subagent task"})
        codex({"hook_event_name": "UserPromptSubmit", "turn_id": "t1", "prompt": "rename the flag"})
        codex({"hook_event_name": "UserPromptSubmit", "turn_id": "t1", "prompt": "and update the docs"})
        codex({"hook_event_name": "Stop", "turn_id": "t1", "last_assistant_message": "Renamed and updated.", "stop_hook_active": False})
        self.assertEqual(self.records(), [("user", "rename the flag"), ("user", "and update the docs"), ("talk", "Renamed and updated.")])
        self.assertEqual({m.origin["agent"] for m in self.service.store.root}, {"codex"})

    def test_search_corrects_records_that_older_versions_stored(self):
        for k, (kind, text) in enumerate([
                ("user", "where is the cache"),
                ("user", NOTICE.format(tool="t", summary='Agent "cache" finished', result="<result>The cache is in src.</result>")),
                ("user", NOTICE.format(tool="b", summary='Background command "clear cache" completed', result="")),
                ("echo", 'Report of the Explore subagent (cache):\n{"isAsync": true, "status": "async_launched", "prompt": "cache"}'),
                ("talk", "I moved the cache")]):
            self.service.call("append", {"event_id": f"old{k}", "kind": kind, "text": text})
        lines = self.service.call("search", {"query": "cache"})["text"].splitlines()
        self.assertEqual(lines[0], "3 messages match; the best 3 follow. zoom(id, 1) gives a whole message.")
        self.assertEqual([line.split("+")[0] for line in lines[1:]], ["0", "4", "1"], "a report stored as a prompt ranks as a report")


class HookOutput(unittest.TestCase):
    def test_hook_prints_nothing_for_a_prompt_and_an_empty_decision_for_a_codex_stop(self):
        import json
        import subprocess
        import sys
        import time
        from optchat.service import Client
        with tempfile.TemporaryDirectory() as temp:
            chat = Path(temp) / "chat"
            run = lambda agent, event: subprocess.run([sys.executable, "-m", "optchat", "--chat", str(chat), "hook", "--agent", agent],
                                                      input=json.dumps({"session_id": "c", "turn_id": "t", **event}), text=True, capture_output=True, timeout=20)
            try:
                prompt = run("codex", {"hook_event_name": "UserPromptSubmit", "prompt": "hello"})
                self.assertEqual((prompt.returncode, prompt.stdout), (0, ""), "plain output would enter the context")
                stop = run("codex", {"hook_event_name": "Stop", "last_assistant_message": "hi"})
                self.assertEqual((stop.returncode, stop.stdout), (0, "{}"))
                claude = run("claude", {"hook_event_name": "Stop", "last_assistant_message": "hi"})
                self.assertEqual(claude.stdout, "")
                self.assertEqual(Client(chat).call("status")["messages"], 2)
            finally:
                client = Client(chat, autostart=False)
                if client.path.exists():
                    client.call("shutdown")
                    deadline = time.monotonic() + 3
                    while client.path.exists() and time.monotonic() < deadline:
                        time.sleep(.01)


if __name__ == "__main__":
    unittest.main()
