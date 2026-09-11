#!/usr/bin/env python3
"""Read-only Microsoft Graph watcher for the your Microsoft 365 tenant M365 tenant.

Mirrors the interface of watchers/slack_watch.py: same config-dir layout
philosophy, same default-run/--peek/--days/--consumer/--json semantics.

Reads Outlook mail (via /me/mailFolders/{folder}/messages), Teams 1:1/group
chats (via /chats/{id}/messages), and Teams channel messages + replies
(via /teams/{team}/channels/{channel}/messages[/{id}/replies]).

Hard rules:
  - Read-only. No write scopes are requested, no message is ever sent,
    reacted to, or modified.
  - Tokens are never printed or logged. Only docs/msgraph-setup.md and the
    operator's own terminal history should ever see them, and even then
    only transiently during `login`.
  - Only registered sources (sources.json) are ever polled. There is no
    "discover everything" default run.

Module structure (stable, so tests can target it directly):
  call()            -- the ONE function that touches the network. Tests
                        monkeypatch this to avoid ever hitting the wire.
  load_config()      -- reads ~/.config/msgraph-watch/env
  load_sources()      / save_sources()
  load_state()        / save_state()
  fetch_mail() / fetch_chat() / fetch_channel() -- each returns
        (items, new_cursor_datetime)
  render_text(items) -- readable text rendering
  main(argv)          -- CLI entrypoint, returns process exit code
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

def ssl_context():
    """HTTPS context that works on the python.org framework build (no CA bundle by default).

    Order: certifi if installed, else macOS/Linux system bundle, else Python's default.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"

DEVICE_CODE_SCOPES = (
    "offline_access User.Read Mail.Read Calendars.Read Chat.Read "
    "ChannelMessage.Read.All Team.ReadBasic.All Channel.ReadBasic.All"
)

DEFAULT_LOOKBACK_DAYS = 3
VALID_KINDS = {"mail", "chat", "channel"}
PUBLIC_KEYS = ["source", "kind", "ts", "author", "subject", "text", "url", "attachments"]
KNOWN_COMMANDS = {"login", "check", "list", "add", "discover", "run"}


class ConfigError(Exception):
    """Raised when the config dir / env file / tokens are missing or incomplete."""


class GraphError(Exception):
    """Raised for any Graph or OAuth HTTP-level error."""

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status

    def __str__(self):
        base = super().__str__()
        if self.code:
            return f"{base} (code={self.code})"
        return base


# ---------------------------------------------------------------------------
# Config dir / files
# ---------------------------------------------------------------------------

def config_dir() -> Path:
    return Path(os.environ.get("MSGRAPH_WATCH_DIR") or "~/.config/msgraph-watch").expanduser()


def ensure_config_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def env_path() -> Path:
    return config_dir() / "env"


def tokens_path() -> Path:
    return config_dir() / "tokens.json"


def sources_path() -> Path:
    return config_dir() / "sources.json"


def state_path() -> Path:
    return config_dir() / "state.json"


def load_config() -> dict:
    p = env_path()
    if not p.exists():
        raise ConfigError(
            f"Config not found: {p}\n"
            "Create it first (see docs/msgraph-setup.md), then run "
            "`msgraph_watch.py login`."
        )
    cfg = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip()
    client_id = cfg.get("MSGRAPH_CLIENT_ID")
    tenant = cfg.get("MSGRAPH_TENANT")
    if not client_id or not tenant:
        raise ConfigError(
            f"Config incomplete in {p}: need MSGRAPH_CLIENT_ID and MSGRAPH_TENANT."
        )
    return {"client_id": client_id, "tenant": tenant}


def load_tokens() -> dict | None:
    p = tokens_path()
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_tokens(tokens: dict) -> None:
    ensure_config_dir()
    p = tokens_path()
    p.write_text(json.dumps(tokens))
    os.chmod(p, 0o600)


def load_sources() -> dict:
    p = sources_path()
    if not p.exists():
        return {"sources": []}
    return json.loads(p.read_text())


def save_sources(doc: dict) -> None:
    ensure_config_dir()
    sources_path().write_text(json.dumps(doc, indent=2) + "\n")


def load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {"consumers": {}}
    return json.loads(p.read_text())


def save_state(state: dict) -> None:
    ensure_config_dir()
    state_path().write_text(json.dumps(state, indent=2) + "\n")


def get_cursor(state: dict, consumer: str, source_name: str):
    return state.get("consumers", {}).get(consumer, {}).get(source_name)


def set_cursor(state: dict, consumer: str, source_name: str, iso_ts: str) -> None:
    state.setdefault("consumers", {}).setdefault(consumer, {})[source_name] = iso_ts


# ---------------------------------------------------------------------------
# The single network entrypoint
# ---------------------------------------------------------------------------

def call(method, path_or_url, token=None, params=None, data=None, max_retries=5):
    """Perform one HTTP request against Graph or an OAuth endpoint.

    This is the ONLY function in this module that touches the network.
    Tests monkeypatch `msgraph_watch.call` so nothing here ever hits the wire
    during `python3 -m unittest`.

    - path_or_url: either a full URL (used verbatim, e.g. an @odata.nextLink
      or a login.microsoftonline.com URL) or a path starting with "/" that
      is resolved against GRAPH_BASE.
    - token: bearer token, or None for unauthenticated OAuth calls
      (devicecode / token endpoints).
    - params: dict of query-string params (GET).
    - data: dict of form fields (POST, x-www-form-urlencoded) -- used for
      the OAuth device-code / token / refresh calls.

    Returns the parsed JSON body as a dict. Raises GraphError on any
    non-2xx response, with .code set to the Graph/OAuth error code
    (e.g. "InvalidAuthenticationToken", "Forbidden", "invalid_grant",
    "authorization_pending") when the error body provides one.
    """
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = GRAPH_BASE + path_or_url

    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)

    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                pass
            if e.code == 429 and attempt < max_retries:
                retry_after = 1
                try:
                    retry_after = int(e.headers.get("Retry-After", "1")) if e.headers else 1
                except (TypeError, ValueError):
                    retry_after = 1
                time.sleep(retry_after)
                attempt += 1
                continue
            payload = {}
            if raw:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {}
            err = payload.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message") or raw or str(e)
            elif isinstance(err, str):
                code = err
                message = payload.get("error_description") or raw or str(e)
            else:
                code = None
                message = raw or str(e)
            raise GraphError(message, code=code, status=e.code)
        except urllib.error.URLError as e:
            raise GraphError(str(e), code="NetworkError", status=None)


# ---------------------------------------------------------------------------
# Auth: device code login, refresh, JWT scope introspection
# ---------------------------------------------------------------------------

def now_ts() -> int:
    return int(time.time())


def device_login(cfg: dict) -> dict:
    devicecode_url = f"{LOGIN_BASE}/{cfg['tenant']}/oauth2/v2.0/devicecode"
    resp = call(
        "POST",
        devicecode_url,
        token=None,
        data={"client_id": cfg["client_id"], "scope": DEVICE_CODE_SCOPES},
    )
    message = resp.get("message") or (
        f"To sign in, visit {resp.get('verification_uri')} and enter the "
        f"code {resp.get('user_code')}"
    )
    print(message)

    device_code = resp["device_code"]
    interval = int(resp.get("interval", 5))
    expires_in = int(resp.get("expires_in", 900))
    deadline = now_ts() + expires_in
    token_url = f"{LOGIN_BASE}/{cfg['tenant']}/oauth2/v2.0/token"

    while now_ts() < deadline:
        time.sleep(interval)
        try:
            tok = call(
                "POST",
                token_url,
                token=None,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": cfg["client_id"],
                    "device_code": device_code,
                },
            )
        except GraphError as e:
            if e.code == "authorization_pending":
                continue
            if e.code == "slow_down":
                interval += 5
                continue
            # authorization_declined, bad_verification_code, expired_token, etc.
            raise
        tokens = {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": now_ts() + int(tok.get("expires_in", 3600)),
            "scope": tok.get("scope", ""),
        }
        save_tokens(tokens)
        return tokens

    raise GraphError("Device code expired before sign-in completed.", code="expired_token")


def refresh_access_token(cfg: dict, tokens: dict) -> dict:
    token_url = f"{LOGIN_BASE}/{cfg['tenant']}/oauth2/v2.0/token"
    tok = call(
        "POST",
        token_url,
        token=None,
        data={
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "refresh_token": tokens["refresh_token"],
            "scope": DEVICE_CODE_SCOPES,
        },
    )
    new_tokens = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", tokens.get("refresh_token")),
        "expires_at": now_ts() + int(tok.get("expires_in", 3600)),
        "scope": tok.get("scope", tokens.get("scope", "")),
    }
    save_tokens(new_tokens)
    return new_tokens


def get_access_token(cfg: dict) -> str:
    tokens = load_tokens()
    if not tokens or not tokens.get("access_token"):
        raise ConfigError("No stored tokens. Run `msgraph_watch.py login` first.")
    if tokens.get("expires_at", 0) - now_ts() < 300:
        try:
            tokens = refresh_access_token(cfg, tokens)
        except GraphError as e:
            if e.code == "invalid_grant":
                print(
                    "Refresh token invalid or expired. Run "
                    "`msgraph_watch.py login` again.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            raise
    return tokens["access_token"]


def decode_jwt_payload(token: str) -> dict:
    """Decode (never verify) the payload segment of a JWT, for scope display only."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1]
    padded = segment + ("=" * (-len(segment) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded)
        return json.loads(raw)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_local_str(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_P_CLOSE_RE = re.compile(r"(?i)</p>")


def strip_html(content: str) -> str:
    text = _BR_RE.sub("\n", content)
    text = _P_CLOSE_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def body_to_text(body: dict | None) -> str:
    if not body:
        return ""
    content = body.get("content") or ""
    if body.get("contentType") == "html":
        return strip_html(content)
    return content.strip()


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _mail_author(m: dict) -> str:
    from_field = m.get("from") or {}
    addr = from_field.get("emailAddress") or {}
    return addr.get("name") or addr.get("address") or "Unknown"


def _chat_or_channel_author(m: dict) -> str:
    from_field = m.get("from")
    if not from_field:
        return "(system)"
    user = from_field.get("user")
    if user:
        return user.get("displayName") or "Unknown"
    application = from_field.get("application")
    if application:
        return application.get("displayName") or "(app)"
    return "Unknown"


def _attachment_names(m: dict) -> list:
    return [a.get("name") for a in (m.get("attachments") or []) if a.get("name")]


def _normalize_mail(m: dict, source: dict) -> dict:
    attachments = []
    if m.get("hasAttachments"):
        attachments = ["(attachment present; see webLink)"]
    return {
        "source": source["name"],
        "kind": "mail",
        "ts": m.get("receivedDateTime"),
        "author": _mail_author(m),
        "subject": m.get("subject") or "(no subject)",
        "text": (m.get("bodyPreview") or "")[:600],
        "url": m.get("webLink"),
        "attachments": attachments,
        "_indent": 0,
    }


def _normalize_chat_message(m: dict, source: dict) -> dict:
    return {
        "source": source["name"],
        "kind": "chat",
        "ts": m.get("createdDateTime"),
        "author": _chat_or_channel_author(m),
        "subject": None,
        "text": body_to_text(m.get("body")),
        "url": m.get("webUrl"),
        "attachments": _attachment_names(m),
        "_indent": 0,
    }


def _normalize_channel_message(m: dict, source: dict, indent: int) -> dict:
    return {
        "source": source["name"],
        "kind": "channel",
        "ts": m.get("createdDateTime"),
        "author": _chat_or_channel_author(m),
        "subject": None,
        "text": body_to_text(m.get("body")),
        "url": m.get("webUrl"),
        "attachments": _attachment_names(m),
        "_indent": indent,
    }


def to_public(item: dict) -> dict:
    return {k: item.get(k) for k in PUBLIC_KEYS}


# ---------------------------------------------------------------------------
# Fetchers -- each returns (items, new_cursor_datetime)
# ---------------------------------------------------------------------------

def fetch_mail(token, source, since_dt):
    folder = source.get("folder", "inbox")
    items = []
    max_seen = since_dt
    params = {
        "$filter": f"receivedDateTime ge {iso_utc(since_dt)}",
        "$orderby": "receivedDateTime asc",
        "$top": "50",
        "$select": "id,receivedDateTime,from,subject,bodyPreview,webLink,hasAttachments",
    }
    url = f"/me/mailFolders/{folder}/messages"
    while url:
        resp = call("GET", url, token=token, params=params)
        params = None  # nextLink already carries the query string
        for m in resp.get("value", []):
            received = parse_iso(m["receivedDateTime"])
            if received > max_seen:
                max_seen = received
            if received > since_dt:
                items.append(_normalize_mail(m, source))
        url = resp.get("@odata.nextLink")
    return items, max_seen


def fetch_chat(token, source, since_dt):
    chat_id = source["chat_id"]
    items = []
    max_seen = since_dt
    url = f"/chats/{chat_id}/messages"
    params = {"$top": "50"}
    stop = False
    while url and not stop:
        resp = call("GET", url, token=token, params=params)
        params = None
        for m in resp.get("value", []):
            created = parse_iso(m["createdDateTime"])
            if created <= since_dt:
                stop = True
                break
            if created > max_seen:
                max_seen = created
            items.append(_normalize_chat_message(m, source))
        if stop:
            break
        url = resp.get("@odata.nextLink")
    items.sort(key=lambda it: it["ts"])
    return items, max_seen


def _fetch_channel_replies(token, team_id, channel_id, message_id):
    replies = []
    url = f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies"
    params = {"$top": "50"}
    while url:
        resp = call("GET", url, token=token, params=params)
        params = None
        replies.extend(resp.get("value", []))
        url = resp.get("@odata.nextLink")
    return replies


def fetch_channel(token, source, since_dt):
    team_id = source["team_id"]
    channel_id = source["channel_id"]
    url = f"/teams/{team_id}/channels/{channel_id}/messages"
    params = {"$top": "50"}
    groups = []  # (sort_key_iso, parent_item_or_None, [reply_items])
    max_seen = since_dt
    first_seen = True
    stop = False
    while url and not stop:
        resp = call("GET", url, token=token, params=params)
        params = None
        for m in resp.get("value", []):
            last_mod = parse_iso(m["lastModifiedDateTime"])
            if last_mod <= since_dt:
                stop = True
                break
            if first_seen:
                # Pages are returned newest-modified-first, so the very
                # first message we inspect carries the new high-water mark.
                max_seen = last_mod
                first_seen = False
            created = parse_iso(m["createdDateTime"])
            parent_item = _normalize_channel_message(m, source, indent=0) if created > since_dt else None
            reply_items = []
            for r in _fetch_channel_replies(token, team_id, channel_id, m["id"]):
                r_created = parse_iso(r["createdDateTime"])
                if r_created > since_dt:
                    reply_items.append(_normalize_channel_message(r, source, indent=1))
            reply_items.sort(key=lambda it: it["ts"])
            if parent_item or reply_items:
                anchor = parent_item["ts"] if parent_item else reply_items[0]["ts"]
                groups.append((anchor, parent_item, reply_items))
        if not stop:
            url = resp.get("@odata.nextLink")

    groups.sort(key=lambda g: g[0])
    items = []
    for _, parent_item, reply_items in groups:
        if parent_item:
            items.append(parent_item)
        items.extend(reply_items)
    return items, max_seen


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_text(items: list) -> str:
    lines = []
    for item in items:
        try:
            ts_local = to_local_str(parse_iso(item["ts"])) if item.get("ts") else "?"
        except Exception:
            ts_local = item.get("ts") or "?"
        indent = "    " if item.get("_indent") else ""
        if item["kind"] == "mail":
            lines.append(
                f"[{ts_local}] {item['source']} — From: {item['author']}  "
                f"Subject: {item.get('subject') or '(no subject)'}"
            )
            lines.append(item.get("text") or "")
            if item.get("attachments"):
                lines.append(f"  Attachments: {', '.join(item['attachments'])}")
            lines.append("")
        else:
            prefix = "↳ " if item.get("_indent") else ""
            lines.append(
                f"{indent}[{ts_local}] {item['source']} {prefix}{item['author']}: "
                f"{item.get('text') or ''}"
            )
            if item.get("attachments"):
                lines.append(f"{indent}  Attachments: {', '.join(item['attachments'])}")
    return "\n".join(lines).rstrip("\n")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_add(args) -> int:
    if args.kind not in VALID_KINDS:
        print(f"Unknown kind: {args.kind}", file=sys.stderr)
        return 1
    entry = {"name": args.name, "kind": args.kind}
    if args.kind == "mail":
        entry["folder"] = args.folder or "inbox"
    elif args.kind == "chat":
        if not args.chat_id:
            print("--chat-id is required for kind=chat", file=sys.stderr)
            return 1
        entry["chat_id"] = args.chat_id
    elif args.kind == "channel":
        if not args.team_id or not args.channel_id:
            print("--team-id and --channel-id are required for kind=channel", file=sys.stderr)
            return 1
        entry["team_id"] = args.team_id
        entry["channel_id"] = args.channel_id
    if args.label:
        entry["label"] = args.label

    doc = load_sources()
    doc.setdefault("sources", [])
    doc["sources"] = [s for s in doc["sources"] if s.get("name") != args.name]
    doc["sources"].append(entry)
    save_sources(doc)
    print(f"Added source '{args.name}' ({args.kind}).")
    return 0


def cmd_list(args) -> int:
    doc = load_sources()
    sources = doc.get("sources", [])
    if not sources:
        print("No sources registered. Use `add` to register one.")
        return 0
    for s in sources:
        label = s.get("label", s["name"])
        print(f"{s['name']}\t{s['kind']}\t{label}")
    return 0


def cmd_check(token) -> int:
    me = call("GET", "/me", token=token)
    print(f"Signed in as {me.get('displayName')} <{me.get('userPrincipalName')}>")
    payload = decode_jwt_payload(token)
    print(f"Token scopes: {payload.get('scp', '')}")
    return 0


def cmd_discover(args, token) -> int:
    both = not (args.chats or args.teams)
    if args.chats or both:
        resp = call("GET", "/me/chats", token=token, params={"$expand": "members"})
        print("Chats:")
        for chat in resp.get("value", []):
            topic = chat.get("topic")
            if not topic:
                names = [m.get("displayName") for m in (chat.get("members") or []) if m.get("displayName")]
                topic = ", ".join(names) if names else "(no topic)"
            print(f"  {chat.get('id')}\t{topic}")
    if args.teams or both:
        resp = call("GET", "/me/joinedTeams", token=token)
        print("Teams:")
        for team in resp.get("value", []):
            print(f"  team {team.get('id')}\t{team.get('displayName')}")
            ch_resp = call("GET", f"/teams/{team['id']}/channels", token=token)
            for ch in ch_resp.get("value", []):
                print(f"    channel {ch.get('id')}\t{ch.get('displayName')}")
    return 0


def cmd_run(args, token) -> int:
    doc = load_sources()
    all_sources = doc.get("sources", [])
    if args.source:
        sources = [s for s in all_sources if s["name"] == args.source]
        if not sources:
            print(f"Unknown source: {args.source}", file=sys.stderr)
            return 1
    else:
        sources = all_sources

    if not sources:
        print("No sources registered. Use `add` to register one.")
        return 0

    state = load_state()
    consumer = args.consumer
    now = datetime.now(timezone.utc)
    all_items = []
    advances = {}

    for source in sources:
        if args.days is not None:
            since_dt = now - timedelta(days=args.days)
        else:
            cursor = get_cursor(state, consumer, source["name"])
            since_dt = parse_iso(cursor) if cursor else now - timedelta(days=DEFAULT_LOOKBACK_DAYS)

        kind = source.get("kind")
        if kind == "mail":
            items, new_cursor = fetch_mail(token, source, since_dt)
        elif kind == "chat":
            items, new_cursor = fetch_chat(token, source, since_dt)
        elif kind == "channel":
            items, new_cursor = fetch_channel(token, source, since_dt)
        else:
            print(f"Skipping source '{source.get('name')}': unknown kind '{kind}'", file=sys.stderr)
            continue

        all_items.extend(items)
        advances[source["name"]] = new_cursor

    all_items.sort(key=lambda it: it["ts"] or "")

    if args.as_json:
        print(json.dumps([to_public(it) for it in all_items], indent=2))
    else:
        text = render_text(all_items)
        print(text if text else "Nothing new.")

    if not args.peek and args.days is None:
        for name, new_cursor in advances.items():
            set_cursor(state, consumer, name, iso_utc(new_cursor))
        save_state(state)

    return 0


# ---------------------------------------------------------------------------
# Argument parsing / main
# ---------------------------------------------------------------------------

def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default=None)
    p.add_argument("--all", action="store_true")
    p.add_argument("--peek", action="store_true")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--consumer", default="default")
    p.add_argument("--json", action="store_true", dest="as_json")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msgraph_watch.py")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("login")
    sub.add_parser("check")
    sub.add_parser("list")

    add_p = sub.add_parser("add")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--kind", required=True, choices=sorted(VALID_KINDS))
    add_p.add_argument("--folder", default=None)
    add_p.add_argument("--chat-id", default=None)
    add_p.add_argument("--team-id", default=None)
    add_p.add_argument("--channel-id", default=None)
    add_p.add_argument("--label", default=None)

    discover_p = sub.add_parser("discover")
    discover_p.add_argument("--chats", action="store_true")
    discover_p.add_argument("--teams", action="store_true")

    run_p = sub.add_parser("run")
    _add_run_args(run_p)

    return parser


def main(argv) -> int:
    argv = list(argv)
    if not argv or argv[0].startswith("-") or argv[0] not in KNOWN_COMMANDS:
        argv = ["run"] + argv

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        if command == "add":
            return cmd_add(args)
        if command == "list":
            return cmd_list(args)

        cfg = load_config()

        if command == "login":
            device_login(cfg)
            print("Login complete. Tokens stored (not printed).")
            return 0

        token = get_access_token(cfg)

        if command == "check":
            return cmd_check(token)
        if command == "discover":
            return cmd_discover(args, token)
        if command == "run":
            return cmd_run(args, token)

    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    except GraphError as e:
        print(f"Graph error: {e}", file=sys.stderr)
        return 1
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        return code

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
