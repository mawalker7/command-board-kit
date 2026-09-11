import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from bin import collect_local as cl


class TestPromptExtraction(unittest.TestCase):
    def test_strip_tags_removes_ide_and_reminder_blocks(self):
        raw = "<ide_opened_file>The user opened x</ide_opened_file><system-reminder>stuff</system-reminder>  build me a board  "
        self.assertEqual(cl.strip_tags(raw), "build me a board")

    def test_first_prompt_skips_empty_and_slash_commands(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            rows = [
                {"type": "summary", "sessionId": "x"},
                {"type": "user", "message": {"content": [{"type": "text", "text": "<ide_selection>foo</ide_selection>"}]}},
                {"type": "user", "message": {"content": "/memory"}},
                {"type": "user", "message": {"content": [{"type": "text", "text": "<ide_opened_file>a</ide_opened_file>Validate the plan and build it"}]}},
                {"type": "user", "message": {"content": "later prompt"}},
            ]
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            self.assertEqual(cl.first_prompt(p), "Validate the plan and build it")
            self.assertEqual(cl.first_prompt(Path(d) / "missing.jsonl"), "")

    def test_project_name_never_touches_filesystem(self):
        self.assertEqual(cl.project_name("/x/-Users-me-Documents-GitHub-proforma-ai", "/Users/me/Documents/GitHub/proforma-ai"), "proforma-ai")
        with unittest.mock.patch.object(cl.glob, "glob", side_effect=AssertionError("must not list directories")):
            self.assertEqual(cl.project_name("/x/-Users-me-Documents-GitHub-proforma-ai"), "proforma-ai")
            self.assertEqual(cl.project_name("/x/-Users-me-Documents-GitHub-nexus"), "nexus")
            self.assertEqual(cl.project_name("/x/-Users-me-local-share-command-board-context"), "command-board-context")
            self.assertEqual(cl.project_name("/x/-tmp-something-else"), "else")

    def test_pid_alive_handles_garbage(self):
        self.assertFalse(cl.pid_alive("not-a-pid"))
        self.assertFalse(cl.pid_alive(2 ** 22 + 12345))


if __name__ == "__main__":
    unittest.main()
