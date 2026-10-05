import copy
import unittest

from optchat.install import BEGIN, END, hook_command, managed_block, merged_hooks


class InstallEdits(unittest.TestCase):
    def test_instruction_block_is_appended_replaced_and_removed_without_touching_user_text(self):
        user = "# Global rules\n\n- keep commits clean\n"
        once = managed_block(user, "OptChat v1")
        self.assertTrue(once.startswith(user.rstrip("\n")))
        self.assertIn(f"{BEGIN}\nOptChat v1\n{END}", once)
        self.assertEqual(managed_block(once, "OptChat v1"), once, "idempotent")
        twice = managed_block(once, "OptChat v2")
        self.assertNotIn("v1", twice)
        self.assertEqual(twice.count(BEGIN), 1)
        self.assertEqual(managed_block(twice, None).strip(), user.strip())
        self.assertEqual(managed_block("", "only"), f"{BEGIN}\nonly\n{END}\n")

    def test_hooks_merge_keeps_other_hooks_and_uninstall_restores(self):
        settings = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
                                               "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]}}
        original = copy.deepcopy(settings)
        merged = merged_hooks(copy.deepcopy(settings), True)
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(len(merged["hooks"]["Stop"]), 2)
        self.assertIn({"type": "command", "command": "guard"}, merged["hooks"]["PreToolUse"][0]["hooks"])
        commands = [h["command"] for entries in merged["hooks"].values() for e in entries for h in e["hooks"]]
        self.assertEqual(commands.count(hook_command()), 5)
        self.assertEqual(merged_hooks(copy.deepcopy(merged), True), merged, "idempotent")
        self.assertEqual(merged_hooks(merged, False), original)
        self.assertEqual(merged_hooks({"hooks": {}}, False), {})

    def test_hooks_of_events_that_install_no_longer_uses_are_removed(self):
        old = {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]}]}}
        merged = merged_hooks(old, True)
        self.assertNotIn("PreToolUse", merged["hooks"])
        self.assertIn("PostToolUse", merged["hooks"])


if __name__ == "__main__":
    unittest.main()
