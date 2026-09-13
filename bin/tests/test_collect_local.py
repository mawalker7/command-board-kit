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
            self.assertEqual(cl.project_name("/x/-Users-me-Documents-GitHub-acme-tools"), "acme-tools")
            self.assertEqual(cl.project_name("/x/-Users-me-local-share-command-board-context"), "command-board-context")
            self.assertEqual(cl.project_name("/x/-tmp-something-else"), "else")

    def test_pid_alive_handles_garbage(self):
        self.assertFalse(cl.pid_alive("not-a-pid"))
        self.assertFalse(cl.pid_alive(2 ** 22 + 12345))


if __name__ == "__main__":
    unittest.main()


class TestMemoryMirror(unittest.TestCase):
    def test_mirror_reads_sparse_clone_and_covers_project(self):
        import subprocess, tempfile, time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); src = d / "src"; src.mkdir()
            subprocess.run(["git", "init", "-q", src], check=True)
            (src / ".claude" / "memory").mkdir(parents=True)
            (src / ".claude" / "memory" / "MEMORY.md").write_text("- [x](x.md) — hook\n")
            (src / ".claude" / "memory" / "x.md").write_text("fact\n")
            subprocess.run(["git", "-C", src, "add", "-A"], check=True)
            subprocess.run(["git", "-C", src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "m"], check=True)
            repo = d / "repo"; repo.mkdir()
            parts = []
            covered = cl.mirror_memory(repo, {"memory_mirrors": [{"project": "proj", "repo": str(src), "path": ".claude/memory"}]}, time.time(), parts)
            self.assertEqual(covered, {"proj"})
            text = "\n".join(parts)
            self.assertIn("Memory index: proj (git mirror)", text)
            self.assertIn("hook", text)
            self.assertIn("Recently updated memory: x.md", text)
            self.assertTrue((repo / "mirrors" / "proj" / ".git").exists())
            # second call pulls instead of cloning and still covers
            parts2 = []
            self.assertEqual(cl.mirror_memory(repo, {"memory_mirrors": [{"project": "proj", "repo": str(src)}]}, time.time(), parts2), {"proj"})

    def test_mirror_failure_is_reported_not_raised(self):
        import tempfile, time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            parts = []
            covered = cl.mirror_memory(Path(d), {"memory_mirrors": [{"project": "ghost", "repo": str(Path(d) / "missing")}]}, time.time(), parts)
            self.assertEqual(covered, set())
            self.assertIn("mirror clone failed", "\n".join(parts))


class TestWatchersOutputShapes(unittest.TestCase):
    def test_watchers_accept_bare_array(self):
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d); (repo / "watchers").mkdir()
            (repo / "watchers" / "slack_watch.py").write_text("import json; print(json.dumps({'items': [{'source': 's'}], 'errors': []}))")
            (repo / "watchers" / "msgraph_watch.py").write_text("import json; print(json.dumps([{'source': 'm', 'kind': 'mail'}]))")
            with unittest.mock.patch.object(cl, "HOME", repo):
                cfg = repo / ".config"; (cfg / "slack-watch").mkdir(parents=True); (cfg / "msgraph-watch").mkdir(parents=True)
                (cfg / "slack-watch" / "sources.json").write_text("{}"); (cfg / "msgraph-watch" / "sources.json").write_text("{}")
                out = cl.collect_watchers(repo)
            self.assertEqual(out["slack"]["status"], "ok"); self.assertEqual(out["slack"]["items"][0]["source"], "s")
            self.assertEqual(out["msgraph"]["status"], "ok"); self.assertEqual(out["msgraph"]["items"][0]["source"], "m")
            self.assertEqual(out["msgraph"]["errors"], [])
