import contextlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watchers import msgraph_watch


SENTINEL_ACCESS = "SENTINEL-ACCESS-TOKEN-DO-NOT-LEAK-9f8e7d6c"
SENTINEL_REFRESH = "SENTINEL-REFRESH-TOKEN-DO-NOT-LEAK-1a2b3c4d"


class MsGraphWatchTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfgdir = Path(self._tmp.name) / "msgraph-watch"
        self._orig_env = os.environ.get("MSGRAPH_WATCH_DIR")
        os.environ["MSGRAPH_WATCH_DIR"] = str(self.cfgdir)
        self._orig_call = msgraph_watch.call

    def tearDown(self):
        msgraph_watch.call = self._orig_call
        if self._orig_env is None:
            os.environ.pop("MSGRAPH_WATCH_DIR", None)
        else:
            os.environ["MSGRAPH_WATCH_DIR"] = self._orig_env
        self._tmp.cleanup()

    # -- helpers --------------------------------------------------------

    def _write_env(self):
        msgraph_watch.ensure_config_dir()
        (self.cfgdir / "env").write_text(
            "MSGRAPH_CLIENT_ID=00001111-aaaa-2222-bbbb-3333cccc4444\n"
            "MSGRAPH_TENANT=civic.onmicrosoft.com\n"
        )

    def _write_valid_tokens(self, access=None, refresh=None):
        tokens = {
            "access_token": access or SENTINEL_ACCESS,
            "refresh_token": refresh or SENTINEL_REFRESH,
            "expires_at": int(time.time()) + 3600,
            "scope": "User.Read Mail.Read",
        }
        msgraph_watch.save_tokens(tokens)
        return tokens

    def _write_sources(self, sources):
        msgraph_watch.save_sources({"sources": sources})


class ConfigMissingTests(MsGraphWatchTestBase):
    def test_config_missing_exit_2(self):
        rc = msgraph_watch.main(["check"])
        self.assertEqual(rc, 2)

    def test_run_config_missing_exit_2(self):
        rc = msgraph_watch.main(["run"])
        self.assertEqual(rc, 2)


class AddSourceTests(MsGraphWatchTestBase):
    def test_add_writes_sources_json(self):
        rc = msgraph_watch.main(
            ["add", "--name", "civic-inbox", "--kind", "mail", "--folder", "inbox", "--label", "civic Inbox"]
        )
        self.assertEqual(rc, 0)
        doc = json.loads((self.cfgdir / "sources.json").read_text())
        names = [s["name"] for s in doc["sources"]]
        self.assertIn("civic-inbox", names)
        entry = [s for s in doc["sources"] if s["name"] == "civic-inbox"][0]
        self.assertEqual(entry["kind"], "mail")
        self.assertEqual(entry["folder"], "inbox")
        self.assertEqual(entry["label"], "civic Inbox")

    def test_add_channel_requires_team_and_channel_id(self):
        rc = msgraph_watch.main(["add", "--name", "civic-team-general", "--kind", "channel"])
        self.assertEqual(rc, 1)

    def test_add_chat_requires_chat_id(self):
        rc = msgraph_watch.main(["add", "--name", "mario-chat", "--kind", "chat"])
        self.assertEqual(rc, 1)

    def test_list_registered_sources(self):
        self._write_sources([{"name": "civic-inbox", "kind": "mail", "folder": "inbox", "label": "Inbox"}])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = msgraph_watch.main(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("civic-inbox", buf.getvalue())


class TokenRefreshTests(MsGraphWatchTestBase):
    def test_refresh_when_expired(self):
        self._write_env()
        msgraph_watch.save_tokens(
            {
                "access_token": "OLD-EXPIRED-TOKEN",
                "refresh_token": "refresh-abc",
                "expires_at": int(time.time()) - 10,
                "scope": "User.Read",
            }
        )

        seen = {}

        def fake_call(method, url, token=None, params=None, data=None, **kw):
            seen["method"] = method
            seen["url"] = url
            seen["token"] = token
            seen["data"] = data
            return {
                "access_token": "NEW-TOKEN-AFTER-REFRESH",
                "refresh_token": "refresh-def",
                "expires_in": 3600,
                "scope": "User.Read",
            }

        msgraph_watch.call = fake_call
        cfg = msgraph_watch.load_config()
        token = msgraph_watch.get_access_token(cfg)

        self.assertEqual(token, "NEW-TOKEN-AFTER-REFRESH")
        self.assertIsNone(seen["token"])  # token endpoint calls are unauthenticated
        self.assertEqual(seen["data"]["grant_type"], "refresh_token")
        self.assertEqual(seen["data"]["refresh_token"], "refresh-abc")
        self.assertTrue(seen["url"].endswith("/oauth2/v2.0/token"))

        saved = json.loads((self.cfgdir / "tokens.json").read_text())
        self.assertEqual(saved["access_token"], "NEW-TOKEN-AFTER-REFRESH")

    def test_invalid_grant_prints_relogin_message_and_exits_1(self):
        self._write_env()
        msgraph_watch.save_tokens(
            {
                "access_token": "OLD-EXPIRED-TOKEN",
                "refresh_token": "refresh-abc",
                "expires_at": int(time.time()) - 10,
                "scope": "User.Read",
            }
        )

        def fake_call(method, url, token=None, params=None, data=None, **kw):
            raise msgraph_watch.GraphError("bad refresh token", code="invalid_grant")

        msgraph_watch.call = fake_call
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = msgraph_watch.main(["check"])
        self.assertEqual(rc, 1)
        self.assertIn("login", stderr.getvalue())


class MailFetchTests(MsGraphWatchTestBase):
    def test_pagination_and_cursor_advancement(self):
        source = {"name": "civic-inbox", "kind": "mail", "folder": "inbox"}
        page1 = {
            "value": [
                {
                    "id": "1",
                    "receivedDateTime": "2026-09-09T10:00:00Z",
                    "from": {"emailAddress": {"name": "Alice", "address": "a@civic.com"}},
                    "subject": "Hi",
                    "bodyPreview": "Hello there",
                    "webLink": "https://outlook.office.com/mail/1",
                    "hasAttachments": False,
                }
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=abc",
        }
        page2 = {
            "value": [
                {
                    "id": "2",
                    "receivedDateTime": "2026-09-09T11:00:00Z",
                    "from": {"emailAddress": {"name": "Bob", "address": "b@civic.com"}},
                    "subject": "Second",
                    "bodyPreview": "Body2",
                    "webLink": "https://outlook.office.com/mail/2",
                    "hasAttachments": True,
                }
            ],
        }
        calls = {"n": 0, "urls": []}

        def fake_call(method, url, token=None, params=None, data=None, **kw):
            calls["n"] += 1
            calls["urls"].append(url)
            if calls["n"] == 1:
                self.assertTrue(url.startswith("/me/mailFolders/inbox/messages"))
                self.assertIsNotNone(params)
                self.assertIn("receivedDateTime ge", params["$filter"])
                return page1
            return page2

        msgraph_watch.call = fake_call
        since = datetime(2026, 9, 9, 0, 0, 0, tzinfo=timezone.utc)
        items, new_cursor = msgraph_watch.fetch_mail("TOKEN", source, since)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["author"], "Alice")
        self.assertEqual(items[1]["author"], "Bob")
        self.assertEqual(items[1]["attachments"], ["(attachment present; see webLink)"])
        self.assertEqual(new_cursor, msgraph_watch.parse_iso("2026-09-09T11:00:00Z"))


class ChatFetchTests(MsGraphWatchTestBase):
    def test_stops_paging_once_older_than_cursor(self):
        source = {"name": "mario-chat", "kind": "chat", "chat_id": "19:abc@thread.v2"}
        page1 = {
            "value": [
                {
                    "id": "m3",
                    "createdDateTime": "2026-09-10T09:00:00Z",
                    "from": {"user": {"displayName": "Mario"}},
                    "body": {"contentType": "text", "content": "newest"},
                    "webUrl": None,
                    "attachments": [],
                },
                {
                    "id": "m2",
                    "createdDateTime": "2026-09-09T09:00:00Z",
                    "from": {"user": {"displayName": "Mario"}},
                    "body": {"contentType": "text", "content": "still new"},
                    "webUrl": None,
                    "attachments": [],
                },
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/chats/19:abc@thread.v2/messages?$skiptoken=x",
        }
        page2 = {
            "value": [
                {
                    "id": "m1",
                    "createdDateTime": "2026-09-08T09:00:00Z",
                    "from": {"user": {"displayName": "Mario"}},
                    "body": {"contentType": "text", "content": "old"},
                    "webUrl": None,
                    "attachments": [],
                }
            ],
        }
        calls = {"n": 0}

        def fake_call(method, url, token=None, params=None, data=None, **kw):
            calls["n"] += 1
            return page1 if calls["n"] == 1 else page2

        msgraph_watch.call = fake_call
        since = msgraph_watch.parse_iso("2026-09-08T12:00:00Z")
        items, new_cursor = msgraph_watch.fetch_chat("TOKEN", source, since)

        self.assertEqual(calls["n"], 2)  # paged into page2, hit the old message, stopped
        self.assertEqual(len(items), 2)
        self.assertEqual([it["text"] for it in items], ["still new", "newest"])
        self.assertEqual(new_cursor, msgraph_watch.parse_iso("2026-09-10T09:00:00Z"))


class ChannelFetchTests(MsGraphWatchTestBase):
    def test_replies_indented_under_parent(self):
        source = {"name": "civic-team-general", "kind": "channel", "team_id": "team1", "channel_id": "chan1"}
        since = msgraph_watch.parse_iso("2026-09-08T00:00:00Z")
        messages_page = {
            "value": [
                {
                    "id": "p1",
                    "createdDateTime": "2026-09-09T10:00:00Z",
                    "lastModifiedDateTime": "2026-09-09T10:05:00Z",
                    "from": {"user": {"displayName": "George"}},
                    "body": {"contentType": "text", "content": "Parent message"},
                    "webUrl": "https://teams.microsoft.com/l/message/p1",
                    "attachments": [],
                }
            ]
        }
        replies_page = {
            "value": [
                {
                    "id": "r1",
                    "replyToId": "p1",
                    "createdDateTime": "2026-09-09T10:05:00Z",
                    "from": {"user": {"displayName": "Morgan"}},
                    "body": {"contentType": "text", "content": "A reply"},
                    "webUrl": "https://teams.microsoft.com/l/message/r1",
                    "attachments": [],
                }
            ]
        }

        def fake_call(method, url, token=None, params=None, data=None, **kw):
            if url.endswith("/replies"):
                return replies_page
            return messages_page

        msgraph_watch.call = fake_call
        items, new_cursor = msgraph_watch.fetch_channel("TOKEN", source, since)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["_indent"], 0)
        self.assertEqual(items[0]["author"], "George")
        self.assertEqual(items[1]["_indent"], 1)
        self.assertEqual(items[1]["author"], "Morgan")

        text = msgraph_watch.render_text(items)
        lines = [l for l in text.splitlines() if l.strip()]
        reply_line = [l for l in lines if "Morgan" in l][0]
        parent_line = [l for l in lines if "George" in l][0]
        self.assertGreater(len(reply_line) - len(reply_line.lstrip()), len(parent_line) - len(parent_line.lstrip()))


class PeekAndDaysTests(MsGraphWatchTestBase):
    def _stub_mail_page(self):
        return {
            "value": [
                {
                    "id": "1",
                    "receivedDateTime": "2026-09-10T10:00:00Z",
                    "from": {"emailAddress": {"name": "Alice"}},
                    "subject": "Hi",
                    "bodyPreview": "Body",
                    "webLink": "https://outlook.office.com/mail/1",
                    "hasAttachments": False,
                }
            ]
        }

    def setUp(self):
        super().setUp()
        self._write_env()
        self._write_valid_tokens()
        self._write_sources([{"name": "civic-inbox", "kind": "mail", "folder": "inbox", "label": "Inbox"}])
        page = self._stub_mail_page()
        msgraph_watch.call = lambda method, url, token=None, params=None, data=None, **kw: page

    def test_peek_does_not_advance_cursor(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = msgraph_watch.main(["run", "--peek"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.cfgdir / "state.json").exists())

    def test_days_does_not_advance_cursor(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = msgraph_watch.main(["run", "--days", "3"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.cfgdir / "state.json").exists())

    def test_default_run_advances_cursor(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = msgraph_watch.main(["run"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.cfgdir / "state.json").exists())
        state = json.loads((self.cfgdir / "state.json").read_text())
        self.assertIn("default", state["consumers"])
        self.assertIn("civic-inbox", state["consumers"]["default"])


class ConsumerIsolationTests(MsGraphWatchTestBase):
    def test_separate_consumers_keep_separate_cursors(self):
        self._write_env()
        self._write_valid_tokens()
        self._write_sources([{"name": "civic-inbox", "kind": "mail", "folder": "inbox", "label": "Inbox"}])

        page = {
            "value": [
                {
                    "id": "1",
                    "receivedDateTime": "2026-09-10T10:00:00Z",
                    "from": {"emailAddress": {"name": "Alice"}},
                    "subject": "Hi",
                    "bodyPreview": "Body",
                    "webLink": "https://outlook.office.com/mail/1",
                    "hasAttachments": False,
                }
            ]
        }
        msgraph_watch.call = lambda method, url, token=None, params=None, data=None, **kw: page

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            msgraph_watch.main(["run", "--consumer", "board"])
        with contextlib.redirect_stdout(buf):
            msgraph_watch.main(["run", "--consumer", "default"])

        state = json.loads((self.cfgdir / "state.json").read_text())
        self.assertIn("board", state["consumers"])
        self.assertIn("default", state["consumers"])
        self.assertEqual(
            state["consumers"]["board"]["civic-inbox"],
            state["consumers"]["default"]["civic-inbox"],
        )
        # But advancing "board" again independently must not touch "default".
        later_page = {
            "value": [
                {
                    "id": "2",
                    "receivedDateTime": "2026-09-10T12:00:00Z",
                    "from": {"emailAddress": {"name": "Bob"}},
                    "subject": "Later",
                    "bodyPreview": "Body2",
                    "webLink": "https://outlook.office.com/mail/2",
                    "hasAttachments": False,
                }
            ]
        }
        msgraph_watch.call = lambda method, url, token=None, params=None, data=None, **kw: later_page
        with contextlib.redirect_stdout(buf):
            msgraph_watch.main(["run", "--consumer", "board"])
        state2 = json.loads((self.cfgdir / "state.json").read_text())
        self.assertNotEqual(
            state2["consumers"]["board"]["civic-inbox"],
            state2["consumers"]["default"]["civic-inbox"],
        )


class JsonOutputTests(MsGraphWatchTestBase):
    def test_json_output_has_documented_keys(self):
        self._write_env()
        self._write_valid_tokens()
        self._write_sources([{"name": "civic-inbox", "kind": "mail", "folder": "inbox", "label": "Inbox"}])
        page = {
            "value": [
                {
                    "id": "1",
                    "receivedDateTime": "2026-09-10T10:00:00Z",
                    "from": {"emailAddress": {"name": "Alice"}},
                    "subject": "Hi",
                    "bodyPreview": "Body",
                    "webLink": "https://outlook.office.com/mail/1",
                    "hasAttachments": False,
                }
            ]
        }
        msgraph_watch.call = lambda method, url, token=None, params=None, data=None, **kw: page

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = msgraph_watch.main(["run", "--json", "--peek"])
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(
            set(parsed[0].keys()),
            {"source", "kind", "ts", "author", "subject", "text", "url", "attachments"},
        )
        self.assertEqual(parsed[0]["kind"], "mail")
        self.assertEqual(parsed[0]["author"], "Alice")


class HtmlStrippingTests(unittest.TestCase):
    def test_strip_html_basic(self):
        html_body = {"contentType": "html", "content": "<div>Hello <b>world</b><br>Line2</div>"}
        text = msgraph_watch.body_to_text(html_body)
        self.assertNotIn("<", text)
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertIn("Line2", text)

    def test_strip_html_entities(self):
        html_body = {"contentType": "html", "content": "<p>Tom &amp; Jerry &gt; cat &amp; mouse</p>"}
        text = msgraph_watch.body_to_text(html_body)
        self.assertIn("Tom & Jerry > cat & mouse", text)

    def test_plain_text_passthrough(self):
        plain_body = {"contentType": "text", "content": "  already plain  "}
        self.assertEqual(msgraph_watch.body_to_text(plain_body), "already plain")


class NoTokenLeakTests(MsGraphWatchTestBase):
    def test_check_never_prints_the_token(self):
        self._write_env()
        self._write_valid_tokens(access=SENTINEL_ACCESS, refresh=SENTINEL_REFRESH)

        def fake_call(method, url, token=None, params=None, data=None, **kw):
            return {"displayName": "Jordan Example", "userPrincipalName": "jordan@civic.onmicrosoft.com"}

        msgraph_watch.call = fake_call
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = msgraph_watch.main(["check"])
        self.assertEqual(rc, 0)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(SENTINEL_ACCESS, combined)
        self.assertNotIn(SENTINEL_REFRESH, combined)

    def test_run_never_prints_the_token(self):
        self._write_env()
        self._write_valid_tokens(access=SENTINEL_ACCESS, refresh=SENTINEL_REFRESH)
        self._write_sources([{"name": "civic-inbox", "kind": "mail", "folder": "inbox", "label": "Inbox"}])
        page = {"value": []}
        msgraph_watch.call = lambda method, url, token=None, params=None, data=None, **kw: page

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = msgraph_watch.main(["run", "--peek"])
        self.assertEqual(rc, 0)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(SENTINEL_ACCESS, combined)
        self.assertNotIn(SENTINEL_REFRESH, combined)

    def test_tokens_file_is_mode_600(self):
        self._write_env()
        self._write_valid_tokens()
        mode = stat.S_IMODE(os.stat(self.cfgdir / "tokens.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_config_dir_is_mode_700(self):
        self._write_env()
        mode = stat.S_IMODE(os.stat(self.cfgdir).st_mode)
        self.assertEqual(mode, 0o700)


if __name__ == "__main__":
    unittest.main()
