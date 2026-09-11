import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bin import board_export as be

BOARD = {"schema": 1, "generated_at": "2026-09-11T15:30:00-04:00", "run": "pm", "hub": "hub-mac", "handled_applied": ["acme-old"],
         "lines": [
             {"id": "acme", "name": "Acme Consulting", "headline": "SOW in flight.", "counts": {}, "items": [
                 {"id": "acme-sow", "state": "needs_you", "title": "Sign the SOW", "synopsis": "Priya sent it.", "source_url": "https://x/1", "source_kind": "email", "owner": "you", "due": "2026-09-12", "updated": "", "sessions": [{"name": "orchard-tools-1", "machine": "hub", "first_prompt": "secret other project"}]},
                 {"id": "acme-wait", "state": "waiting", "title": "Legal review", "synopsis": "With Sam.", "source_url": None, "source_kind": "email", "owner": "them:Sam", "due": None, "updated": "", "sessions": []}]},
             {"id": "orchard", "name": "Orchard Rentals", "headline": "Guest question.", "counts": {}, "items": [
                 {"id": "orchard-parking", "state": "needs_you", "title": "Answer parking question", "synopsis": "Dana suggested the side lot.", "source_url": None, "source_kind": "email", "owner": "you", "due": None, "updated": "", "sessions": []}]}],
         "run_health": [{"input": "gmail", "status": "ok", "detail": "Orchard guest thread and Acme SOW reviewed"}]}


class TestExport(unittest.TestCase):
    def test_only_the_requested_line_leaves(self):
        md = be.export_line(BOARD, "acme")
        self.assertIn("# Acme Consulting", md)
        self.assertIn("Needs me: Sign the SOW", md)
        self.assertIn("with Sam", md)
        for leak in ("Orchard", "Dana", "parking", "orchard-tools-1", "secret other project", "acme-old", "gmail", "run health", "hub-mac"):
            self.assertNotIn(leak, md, leak)

    def test_unknown_line_and_cli(self):
        self.assertIsNone(be.export_line(BOARD, "nope"))
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / "b.json"; b.write_text(json.dumps(BOARD)); out = Path(d) / "share.md"
            o, e = io.StringIO(), io.StringIO()
            with redirect_stdout(o), redirect_stderr(e):
                rc = be.main(["--line", "orchard", "--board", str(b), "--out", str(out)])
            self.assertEqual(rc, 0, e.getvalue())
            self.assertIn("Orchard Rentals", out.read_text()); self.assertNotIn("Acme", out.read_text())
            with redirect_stdout(o), redirect_stderr(e):
                rc = be.main(["--line", "nope", "--board", str(b), "--out", str(out)])
            self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
