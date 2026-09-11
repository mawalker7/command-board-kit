#!/usr/bin/env python3
"""slack_watch.py: read-only watcher for registered Slack conversations (any workspace).

Generalizes the single-DM watcher: a registry of named sources (workspace + conversation id),
one user token per workspace, and independent cursors per consumer so the twice-daily
command-board job and interactive checks never steal each other's messages. Never writes to Slack.

Config dir (default ~/.config/slack-watch, override with SLACK_WATCH_DIR):
  env            KEY=VALUE lines. One token per workspace: SLACK_TOKEN_<WORKSPACE>=xoxp-...
                 (workspace key uppercased, e.g. SLACK_TOKEN_ACME, SLACK_TOKEN_CIVIC)
  sources.json   {"sources": [{"name": "priya-dm", "workspace": "acme", "channel": "D0...",
                               "kind": "im", "label": "Acme DM with Priya"}]}
  state.json     {"<consumer>": {"<workspace>/<channel>": "<slack ts cursor>"}}

Usage:
  slack_watch.py --source priya-dm            new since last check (first run: last 3 days); advances cursor
  slack_watch.py --all [--consumer board]     every registered source, cursor kept per consumer
  slack_watch.py --source X --peek            do not advance the cursor
  slack_watch.py --source X --days 7          last N days; never advances the cursor
  slack_watch.py --all --json                 machine-readable output for the hub job
  slack_watch.py add --name N --workspace W --channel C --kind im|mpim|channel|group [--label L]
  slack_watch.py list                         registered sources (no tokens)
  slack_watch.py check                        auth.test per workspace: team + user + scopes (never the token)

Exit codes: 0 ok, 1 Slack/network error (stderr), 2 missing config.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
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


CONFIG_DIR = Path(os.environ.get("SLACK_WATCH_DIR", Path.home() / ".config" / "slack-watch"))
ENV_FILE = CONFIG_DIR / "env"
SOURCES_FILE = CONFIG_DIR / "sources.json"
STATE_FILE = CONFIG_DIR / "state.json"
API = "https://slack.com/api/"
FIRST_RUN_DAYS = 3
THREAD_LOOKBACK_DAYS = 14  # thread parents older than this are not checked for new replies
DAY = Decimal(86400)
KINDS = ("im", "mpim", "channel", "group")
SCOPE_FOR_KIND = {"im": "im:history", "mpim": "mpim:history", "channel": "channels:history", "group": "groups:history"}


class SlackError(Exception):
    pass


# ---------- config ----------

def _read_env_file():
    cfg = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


def token_for(workspace, env=None):
    key = "SLACK_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", workspace).upper()
    env = env if env is not None else _read_env_file()
    return os.environ.get(key) or env.get(key)


def load_sources():
    if not SOURCES_FILE.exists():
        return []
    data = json.loads(SOURCES_FILE.read_text() or "{}")
    return data.get("sources", [])


def save_sources(sources):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(SOURCES_FILE, json.dumps({"sources": sources}, indent=2) + "\n")


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(STATE_FILE, json.dumps(state))


def _atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ---------- slack api ----------

def call(method, token, **params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{API}{method}?{query}", headers={"Authorization": f"Bearer {token}"})
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_context()) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "5")))
                continue
            raise SlackError(f"{method}: HTTP {e.code}")
        except urllib.error.URLError as e:
            raise SlackError(f"{method}: network error ({e.reason})")
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown_error')}")
        return data
    raise SlackError(f"{method}: rate limited repeatedly")


def paginate(method, token, key, **params):
    cursor = None
    while True:
        data = call(method, token, cursor=cursor, limit=200, **params)
        yield from data.get(key, [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


_names = {}


def user_name(uid, token):
    if (token, uid) not in _names:
        try:
            u = call("users.info", token, user=uid)["user"]
            p = u.get("profile") or {}
            _names[(token, uid)] = p.get("display_name") or p.get("real_name") or u.get("name") or uid
        except SlackError:
            _names[(token, uid)] = uid
    return _names[(token, uid)]


def author(m, token):
    if m.get("user"):
        return user_name(m["user"], token)
    return m.get("username") or (m.get("bot_profile") or {}).get("name") or "bot"


def clean(text, token):
    text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda mo: "@" + user_name(mo.group(1), token), text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return html.unescape(text)


def when(ts):
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


# ---------- core ----------

def fetch_source(src, token, since, now):
    """Return (blocks, count, newest) for one source.

    blocks: list of (first_ts, text, [normalized items]) sorted later by first_ts.
    """
    channel = src["channel"]
    window_start = min(since, now - THREAD_LOOKBACK_DAYS * DAY)
    history = list(paginate("conversations.history", token, "messages", channel=channel, oldest=str(window_start)))

    threads, reply_ts = {}, set()
    for m in history:
        if m.get("reply_count") and Decimal(m.get("latest_reply", "0")) > since:
            replies = [r for r in paginate("conversations.replies", token, "messages", channel=channel, ts=m["ts"], oldest=str(since))
                       if r["ts"] != m["ts"] and Decimal(r["ts"]) > since]
            if replies:
                threads[m["ts"]] = sorted(replies, key=lambda r: Decimal(r["ts"]))
                reply_ts.update(r["ts"] for r in replies)

    blocks, count, newest = [], 0, since
    for m in history:
        ts = Decimal(m["ts"])
        # "Also send to channel" replies show up in history too; render them only inside their thread.
        is_new = ts > since and not (m.get("subtype") == "thread_broadcast" and m["ts"] in reply_ts)
        replies = threads.get(m["ts"], [])
        if not is_new and not replies:
            continue
        lines, new_ts, items = [], [], []
        if is_new:
            lines.append(render(m, token))
            new_ts.append(ts)
            items.append(normalize(src, m, token, parent_ts=None))
        else:
            snippet = clean(m.get("text", ""), token).replace("\n", " ")[:80]
            lines.append(f'[new replies in thread on {when(m["ts"])} message from {author(m, token)}: "{snippet}"]')
        for r in replies:
            lines.append(render(r, token, prefix="    ↳ "))
            new_ts.append(Decimal(r["ts"]))
            items.append(normalize(src, r, token, parent_ts=m["ts"]))
        count += len(new_ts)
        newest = max(newest, *new_ts)
        blocks.append((min(new_ts), "\n".join(lines), items))
    return blocks, count, newest


def render(m, token, prefix=""):
    out = [f"{prefix}[{when(m['ts'])}] {author(m, token)}:"]
    body = clean(m.get("text", ""), token).splitlines() or ["(no text)"]
    pad = " " * len(prefix)
    out += [f"{pad}  {line}" for line in body]
    files = [f.get("name") or f.get("title") or "file" for f in m.get("files") or []]
    if files:
        out.append(f"{pad}  (attachments: {', '.join(files)})")
    return "\n".join(out)


def normalize(src, m, token, parent_ts):
    return {
        "source": src["name"],
        "label": src.get("label") or src["name"],
        "workspace": src["workspace"],
        "channel": src["channel"],
        "ts": m["ts"],
        "when": when(m["ts"]),
        "author": author(m, token),
        "text": clean(m.get("text", ""), token),
        "thread_parent": parent_ts,
        "attachments": [f.get("name") or f.get("title") or "file" for f in m.get("files") or []],
    }


def select_sources(args, sources):
    if args.all:
        return sources
    if args.source:
        chosen = [s for s in sources if s["name"] == args.source]
        if not chosen:
            raise SystemExit(f"ERROR: no source named {args.source!r}; run `slack_watch.py list`.")
        return chosen
    return []


# ---------- subcommands ----------

def cmd_add(args):
    if args.kind not in KINDS:
        print(f"ERROR: --kind must be one of {', '.join(KINDS)}", file=sys.stderr)
        return 2
    sources = load_sources()
    if any(s["name"] == args.name for s in sources):
        print(f"ERROR: a source named {args.name!r} already exists.", file=sys.stderr)
        return 2
    sources.append({"name": args.name, "workspace": args.workspace, "channel": args.channel,
                    "kind": args.kind, "label": args.label or args.name})
    save_sources(sources)
    print(f"Added {args.name} ({args.workspace}/{args.channel}, {args.kind}). "
          f"Token expected as SLACK_TOKEN_{args.workspace.upper()} in {ENV_FILE}; scope needed: {SCOPE_FOR_KIND[args.kind]} (+ users:read).")
    return 0


def cmd_list(_args):
    sources = load_sources()
    if not sources:
        print("No sources registered.")
        return 0
    env = _read_env_file()
    for s in sources:
        has = "token ok" if token_for(s["workspace"], env) else "TOKEN MISSING"
        print(f"{s['name']:<20} {s['workspace']}/{s['channel']:<14} {s['kind']:<8} {s.get('label','')}  [{has}]")
    return 0


def cmd_check(_args):
    env = _read_env_file()
    workspaces = sorted({s["workspace"] for s in load_sources()})
    if not workspaces:
        print("No sources registered; nothing to check.")
        return 0
    rc = 0
    for ws in workspaces:
        token = token_for(ws, env)
        if not token:
            print(f"{ws}: TOKEN MISSING (SLACK_TOKEN_{ws.upper()})")
            rc = 1
            continue
        try:
            data = call("auth.test", token)
            print(f"{ws}: team={data.get('team')} user={data.get('user')} url={data.get('url')}")
        except SlackError as e:
            print(f"{ws}: ERROR {e}")
            rc = 1
    return rc


def cmd_watch(args):
    sources = select_sources(args, load_sources())
    if not sources:
        print("ERROR: pass --source NAME or --all (register sources with `slack_watch.py add ...`).", file=sys.stderr)
        return 2
    env = _read_env_file()
    state = load_state()
    consumer_state = state.setdefault(args.consumer, {})
    now = Decimal(f"{time.time():.6f}")
    all_blocks, all_items, errors = [], [], []

    for src in sources:
        token = token_for(src["workspace"], env)
        key = f"{src['workspace']}/{src['channel']}"
        if not token:
            errors.append(f"{src['name']}: no token for workspace {src['workspace']!r} (SLACK_TOKEN_{src['workspace'].upper()} in {ENV_FILE})")
            continue
        if args.days is not None:
            since, advance = now - Decimal(str(args.days)) * DAY, False
        else:
            since = Decimal(consumer_state.get(key, str(now - FIRST_RUN_DAYS * DAY)))
            advance = not args.peek
        try:
            blocks, count, newest = fetch_source(src, token, since, now)
        except SlackError as e:
            errors.append(f"{src['name']}: {e}")
            continue
        label = src.get("label") or src["name"]
        if not args.json:
            if not blocks:
                all_blocks.append((since, f"== {label}: no new messages since {when(since)}.", []))
            else:
                all_blocks.append((min(b[0] for b in blocks), f"== {label}: {count} new message(s) since {when(since)}:", []))
                all_blocks.extend(blocks)
        for b in blocks:
            all_items.extend(b[2])
        if advance:
            consumer_state[key] = str(newest)

    if args.json:
        print(json.dumps({"items": sorted(all_items, key=lambda i: Decimal(i["ts"])), "errors": errors}, indent=1))
    else:
        for _, text, _ in all_blocks:
            print(text)
            print()
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)

    if args.days is None and not args.peek:
        save_state(state)  # only sources that succeeded had their cursor moved
    if errors and not all_items:
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only watcher for registered Slack conversations.")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add", help="register a conversation")
    a.add_argument("--name", required=True)
    a.add_argument("--workspace", required=True, help="short key, e.g. acme or civic")
    a.add_argument("--channel", required=True, help="conversation id (D…, G…, C…)")
    a.add_argument("--kind", required=True, help="im | mpim | channel | group")
    a.add_argument("--label")
    sub.add_parser("list", help="show registered sources")
    sub.add_parser("check", help="auth.test each workspace token")
    ap.add_argument("--source", help="source name to read")
    ap.add_argument("--all", action="store_true", help="read every registered source")
    ap.add_argument("--peek", action="store_true", help="do not advance the cursor")
    ap.add_argument("--days", type=float, help="show the last N days; does not advance the cursor")
    ap.add_argument("--consumer", default="default", help="cursor namespace (e.g. board)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "check":
        return cmd_check(args)
    if not ENV_FILE.exists() and not any(k.startswith("SLACK_TOKEN_") for k in os.environ):
        print(f"ERROR: no {ENV_FILE} and no SLACK_TOKEN_* in the environment. See docs/slack-source-setup.md.", file=sys.stderr)
        return 2
    return cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
