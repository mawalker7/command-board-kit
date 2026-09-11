import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bin import board_sanitize as bs

LINES = {"lines": [{"id": "acme", "name": "Acme Consulting"}, {"id": "orchard", "name": "Orchard Rentals"}]}


def board(**over):
    b = {"schema": 1, "generated_at": "2026-09-11T07:00:00-04:00", "run": "am", "hub": "hub-mac",
         "lines": [{"id": "orchard", "headline": "Turnover season starts", "items": [
             {"id": "orchard-guest-debrief", "state": "needs_you", "title": 'Book the neighbor "debrief"',
              "synopsis": "Line one.\nLine two with a back\\slash.", "source_url": "https://x.io/1", "source_kind": "email",
              "owner": "you", "due": "2026-09-12", "updated": "2026-09-11T06:50:00", "sessions": ["proforma-ai-d8", {"name": "acme-tools-a1", "machine": "hub-mac", "first_prompt": "fix the thing"}]},
             {"id": "orchard-parked-thing", "state": "parked", "title": "Old", "synopsis": "s"}]}],
         "run_health": [{"input": "gmail", "status": "ok", "detail": ""}]}
    b.update(over)
    return b


class TestSanitize(unittest.TestCase):
    def test_happy_path_canonical_order_and_cleaning(self):
        out, text = bs.sanitize(board(), LINES)
        self.assertEqual([l["id"] for l in out["lines"]], ["acme", "orchard"])   # canonical order, missing line added empty
        self.assertEqual(out["lines"][0]["items"], [])
        item = out["lines"][1]["items"][0]
        self.assertEqual(item["title"], "Book the neighbor 'debrief'")
        self.assertEqual(item["synopsis"], "Line one. Line two with a back/slash.")
        self.assertEqual(item["sessions"][0], {"name": "proforma-ai-d8", "machine": "", "first_prompt": ""})
        self.assertEqual(item["sessions"][1]["first_prompt"], "fix the thing")
        self.assertEqual(out["lines"][1]["counts"], {"needs_you": 1, "waiting": 0, "moving": 0, "parked": 1})
        self.assertNotIn("\n", text)
        self.assertNotIn('\\"', text)
        json.loads(text)

    def test_rejects_bad_schema_run_and_ids(self):
        with self.assertRaises(bs.ValidationError):
            bs.sanitize(board(schema=2), LINES)
        with self.assertRaises(bs.ValidationError):
            bs.sanitize(board(run="noon"), LINES)
        b = board(); b["lines"][0]["items"][0]["id"] = "Bad Id"
        with self.assertRaises(bs.ValidationError):
            bs.sanitize(b, LINES)
        b = board(); b["lines"][0]["items"][1]["id"] = "orchard-guest-debrief"
        with self.assertRaises(bs.ValidationError):
            bs.sanitize(b, LINES)
        b = board(); b["lines"][0]["id"] = "nope"
        with self.assertRaises(bs.ValidationError):
            bs.sanitize(b, LINES)
        b = board(); b["lines"][0]["items"][0]["state"] = "urgent"
        with self.assertRaises(bs.ValidationError):
            bs.sanitize(b, LINES)
        b = board(); b["lines"][0]["items"][0]["due"] = "tomorrow"
        with self.assertRaises(bs.ValidationError):
            bs.sanitize(b, LINES)

    def test_caps_and_ordering(self):
        b = board()
        items = [{"id": f"orchard-p{i}", "state": "parked", "title": "p", "synopsis": "s"} for i in range(15)]
        items += [{"id": "orchard-w", "state": "waiting", "title": "w", "synopsis": "x" * 1000}]
        b["lines"][0]["items"] = items
        out, _ = bs.sanitize(b, LINES)
        got = out["lines"][1]["items"]
        self.assertEqual(len(got), bs.MAX_ITEMS_PER_LINE)
        self.assertEqual(got[0]["state"], "waiting")                       # non-parked first
        self.assertEqual(len(got[0]["synopsis"]), bs.MAX_SYNOPSIS)

    def test_markdown_and_cli(self):
        out, _ = bs.sanitize(board(), LINES)
        md = bs.to_markdown(out)
        self.assertIn("## Orchard Rentals", md)
        self.assertIn("[Needs you] Book the neighbor 'debrief'", md)
        self.assertIn("Sessions: proforma-ai-d8; acme-tools-a1 (hub-mac)", md)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "b.json").write_text(json.dumps(board()))
            (d / "lines.json").write_text(json.dumps(LINES))
            o, e = io.StringIO(), io.StringIO()
            with redirect_stdout(o), redirect_stderr(e):
                rc = bs.main([str(d / "b.json"), "--lines", str(d / "lines.json"), "--out-json", str(d / "o.json"), "--out-md", str(d / "o.md")])
            self.assertEqual(rc, 0, e.getvalue())
            self.assertTrue((d / "o.json").exists() and (d / "o.md").exists())
            (d / "b.json").write_text("{not json")
            with redirect_stdout(o), redirect_stderr(e):
                rc = bs.main([str(d / "b.json"), "--lines", str(d / "lines.json"), "--out-json", str(d / "o.json"), "--out-md", str(d / "o.md")])
            self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
