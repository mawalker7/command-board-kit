"""Tests for slack_watch.py against mocked Slack responses. No network."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

import watchers.slack_watch as sw

TOKEN = "xoxp-SENTINEL-DO-NOT-PRINT"
NOW = 1_800_000_000.0


class FakeSlack:
    """Minimal in-memory Slack: one channel with history + threads."""

    def __init__(self, history, replies=None, users=None):
        self.history = history
        self.replies = replies or {}
        self.users = users or {"U1": "Priya", "U2": "Jordan"}
        self.calls = []

    def __call__(self, method, token, **params):
        assert token == TOKEN
        self.calls.append((method, params))
        if method == "auth.test":
            return {"ok": True, "team": "Acme", "user": "jordan", "url": "https://acme.slack.com/"}
        if method == "users.info":
            return {"ok": True, "user": {"profile": {"display_name": self.users.get(params["user"], "")}, "name": params["user"]}}
        if method == "conversations.history":
            oldest = Decimal(params["oldest"])
            msgs = [m for m in self.history if Decimal(m["ts"]) > oldest]
            return {"ok": True, "messages": sorted(msgs, key=lambda m: -Decimal(m["ts"]))}
        if method == "conversations.replies":
            oldest = Decimal(params["oldest"])
            msgs = [m for m in self.replies.get(params["ts"], []) if Decimal(m["ts"]) > oldest]
            return {"ok": True, "messages": msgs}
        raise AssertionError(method)


def ts(offset_seconds):
    return f"{NOW - offset_seconds:.6f}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(sw, "CONFIG_DIR", d),
            mock.patch.object(sw, "ENV_FILE", d / "env"),
            mock.patch.object(sw, "SOURCES_FILE", d / "sources.json"),
            mock.patch.object(sw, "STATE_FILE", d / "state.json"),
            mock.patch.object(sw.time, "time", lambda: NOW),
        ]
        for p in self.patches:
            p.start()
        sw._names.clear()
        (d / "env").write_text(f"SLACK_TOKEN_FTT={TOKEN}\n")
        sw.save_sources([{"name": "priya-dm", "workspace": "acme", "channel": "D123", "kind": "im", "label": "Acme DM with Priya"}])

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = sw.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class TestConfig(Base):
    def test_missing_config_exits_2(self):
        sw.ENV_FILE.unlink()
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in [k for k in os.environ if k.startswith("SLACK_TOKEN_")]:
                del os.environ[k]
            rc, out, err = self.run_cli("--source", "priya-dm")
        self.assertEqual(rc, 2)
        self.assertIn("no", err)

    def test_add_and_list(self):
        rc, out, _ = self.run_cli("add", "--name", "civic-general", "--workspace", "civic", "--channel", "C9", "--kind", "channel")
        self.assertEqual(rc, 0)
        self.assertIn("channels:history", out)
        self.assertEqual(len(sw.load_sources()), 2)
        rc, out, _ = self.run_cli("list")
        self.assertIn("civic-general", out)
        self.assertIn("TOKEN MISSING", out)   # no civic token in env
        self.assertNotIn(TOKEN, out)

    def test_add_rejects_bad_kind_and_duplicate(self):
        rc, _, err = self.run_cli("add", "--name", "x", "--workspace", "acme", "--channel", "C1", "--kind", "dm")
        self.assertEqual(rc, 2)
        rc, _, err = self.run_cli("add", "--name", "priya-dm", "--workspace", "acme", "--channel", "C1", "--kind", "im")
        self.assertEqual(rc, 2)

    def test_check_never_prints_token(self):
        fake = FakeSlack([])
        with mock.patch.object(sw, "call", fake):
            rc, out, err = self.run_cli("check")
        self.assertEqual(rc, 0)
        self.assertIn("team=Acme", out)
        self.assertNotIn(TOKEN, out + err)


class TestWatch(Base):
    def history(self):
        return [
            {"ts": ts(5 * 86400), "user": "U1", "text": "old parent", "reply_count": 1, "latest_reply": ts(100)},
            {"ts": ts(3000), "user": "U1", "text": "Hey <@U2>, see <https://x.io/a|the doc> &amp; <https://y.io>", "files": [{"name": "deck.pdf"}]},
            {"ts": ts(200), "user": "U2", "text": "broadcast reply", "subtype": "thread_broadcast", "thread_ts": ts(5 * 86400)},
        ]

    def replies(self):
        return {ts(5 * 86400): [
            {"ts": ts(5 * 86400), "user": "U1", "text": "old parent"},
            {"ts": ts(200), "user": "U2", "text": "broadcast reply", "subtype": "thread_broadcast"},
            {"ts": ts(100), "user": "U1", "text": "thanks"},
        ]}

    def test_first_run_renders_and_advances(self):
        fake = FakeSlack(self.history(), self.replies())
        with mock.patch.object(sw, "call", fake):
            rc, out, err = self.run_cli("--source", "priya-dm")
        self.assertEqual(rc, 0, err)
        self.assertIn("Priya:", out)
        self.assertIn("@Jordan", out)                       # mention resolved
        self.assertIn("the doc (https://x.io/a) & https://y.io", out)  # link + entity cleanup
        self.assertIn("attachments: deck.pdf", out)
        self.assertIn("[new replies in thread on", out)   # reply on old parent gets a header
        self.assertIn("↳", out)
        self.assertEqual(out.count("broadcast reply"), 1) # broadcast deduped
        state = sw.load_state()
        self.assertEqual(state["default"]["acme/D123"], ts(100))
        self.assertNotIn(TOKEN, out + err)

    def test_second_run_is_quiet(self):
        fake = FakeSlack(self.history(), self.replies())
        with mock.patch.object(sw, "call", fake):
            self.run_cli("--source", "priya-dm")
            rc, out, _ = self.run_cli("--source", "priya-dm")
        self.assertEqual(rc, 0)
        self.assertIn("no new messages", out)

    def test_peek_and_days_do_not_advance(self):
        fake = FakeSlack(self.history(), self.replies())
        with mock.patch.object(sw, "call", fake):
            self.run_cli("--source", "priya-dm", "--peek")
            self.assertEqual(sw.load_state(), {})
            rc, out, _ = self.run_cli("--source", "priya-dm", "--days", "7")
            self.assertIn("old parent", out)              # 7-day window includes the 5-day-old parent
            self.assertEqual(sw.load_state(), {})

    def test_consumers_have_independent_cursors(self):
        fake = FakeSlack(self.history(), self.replies())
        with mock.patch.object(sw, "call", fake):
            self.run_cli("--all", "--consumer", "board")
            rc, out, _ = self.run_cli("--source", "priya-dm")   # default consumer still sees everything
        self.assertIn("Priya:", out)
        state = sw.load_state()
        self.assertIn("board", state)
        self.assertIn("default", state)

    def test_json_output_shape(self):
        fake = FakeSlack(self.history(), self.replies())
        with mock.patch.object(sw, "call", fake):
            rc, out, _ = self.run_cli("--all", "--json")
        data = json.loads(out)
        self.assertEqual(data["errors"], [])
        keys = {"source", "label", "workspace", "channel", "ts", "when", "author", "text", "thread_parent", "attachments"}
        self.assertTrue(all(keys <= set(i) for i in data["items"]))
        self.assertEqual([i["thread_parent"] for i in data["items"] if i["text"] == "thanks"], [ts(5 * 86400)])

    def test_missing_workspace_token_reports_error(self):
        sw.save_sources(sw.load_sources() + [{"name": "nc", "workspace": "civic", "channel": "C1", "kind": "channel"}])
        fake = FakeSlack(self.history(), self.replies())
        with mock.patch.object(sw, "call", fake):
            rc, out, err = self.run_cli("--all")
        self.assertEqual(rc, 0)                            # partial success
        self.assertIn("no token for workspace 'civic'", err)
        self.assertNotIn("civic/C1", sw.load_state()["default"])

    def test_slack_error_exit_1_when_nothing_read(self):
        def boom(method, token, **p):
            raise sw.SlackError(f"{method}: invalid_auth")
        with mock.patch.object(sw, "call", boom):
            rc, out, err = self.run_cli("--source", "priya-dm")
        self.assertEqual(rc, 1)
        self.assertIn("invalid_auth", err)
        self.assertEqual(sw.load_state().get("default", {}), {})


if __name__ == "__main__":
    unittest.main()


class TestSslContext(unittest.TestCase):
    def test_ssl_context_has_a_ca_source(self):
        ctx = sw.ssl_context()
        import ssl
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
