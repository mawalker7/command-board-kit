# Command Board Kit

A twice-daily, cross-venture status board for one person who runs several things at once. One page, opened on a phone or laptop, with a collapsed row per business line; each row expands into cards that say what needs you, what is waiting on someone else, what is quietly moving, and which AI coding sessions are already on it. Every card cites its source. Nothing is ever posted or sent on your behalf.

Built for a solo operator on macOS with Claude Code and the claude.ai connectors (Gmail, Google Calendar, Google Drive, one Slack workspace). Other Slack workspaces and Microsoft 365 are read through small stdlib-only watchers with minimal, read-only scopes.

```
satellite Mac ──(06:40/16:40 bundle → git push)──┐
                                                 ▼
hub Mac 06:45/16:45  collect_local.py → context/ → claude -p (phase A, summarize) → board.json
                     → board_sanitize.py → board.doc.json + board.md → snapshots (git) + ~/Downloads
                     → claude -p (phase B, publish) → Google Doc "command-board-data" (Drive connector)
                                                 ▼
claude.ai artifact page ── viewer's own Drive connector (mcp capability) ──► newest data doc → render
        └── "Handled" → tiny "command-board-handled" doc → the next run applies it
```

## Design decisions worth reading

- **The collector is not an AI.** A deterministic script gathers sessions, notes, PRs, recent deliverables, the previous board, and watcher output into bounded files. The model reads; it does not explore. Cheap, fast, debuggable.
- **The summarizer may synthesize but not invent.** Every item cites a source URL or kind; anything not checked is recorded in `run_health`, never silently dropped. A validator enforces the schema before anything is published.
- **No server, no secrets on the page.** The board page is static HTML. At open time it calls the viewer's own Google Drive connector to read the newest data document, so the page never needs republishing and holds nothing sensitive. The Docs text export escapes markdown punctuation; the page reverses that with one regex.
- **Read-only integrations with per-consumer cursors.** The Slack and Microsoft Graph watchers hold no write scopes, keep tokens in `~/.config/*/env` (mode 600, outside the repo), and keep an independent cursor per consumer so the board run and an interactive check never steal each other's messages.
- **A feedback loop that closes.** Tapping "Handled" writes a marker document; the next run applies it and lists it in `handled_applied`. Item ids are carried forward run to run, so marks stick.
- **Two machines, one hub.** The satellite machine pushes a bundle of its own sessions, notes, and unpushed work into the repo five minutes before each hub run.

## Layout

| Path | What |
|---|---|
| `bin/command-board` | hub wrapper (launchd entry); `--collect-only`, `--skip-publish`, `--dry-run`; 15-minute phase cap with one retry |
| `bin/collect_local.py` | deterministic context collector |
| `bin/board_sanitize.py` | schema validation, doc-safe single-line JSON, markdown twin |
| `bin/board_export.py` | scoped single-line markdown export for anything that leaves your screen |
| `bin/ics_events.py` | reads published Outlook ICS feeds, expands recurrences, 48-hour window |
| `bin/satellite_bundle.py`, `bin/satellite-sync` | the second machine's bundle and push |
| `bin/install.sh hub\|satellite` | installs the launchd job for this machine's role |
| `config/lines.example.json`, `config/persona.example.md`, `config/repos.example.json` | copy to the non-example names and edit; the real ones are gitignored |
| `config/prompt-hub.md`, `config/prompt-publish.md` | the two headless prompts |
| `watchers/slack_watch.py` | read-only Slack watcher for workspaces the connector cannot reach |
| `watchers/msgraph_watch.py` | read-only Microsoft Graph watcher (Outlook mail, calendar, Teams) |
| `page/command-board.html` | the board page; open with `?demo` for a fictional board without Claude |
| `docs/build-your-own-command-board.md` | a tool-agnostic guide any assistant can follow to build an equivalent |
| `docs/slack-source-setup.md`, `docs/msgraph-setup.md`, `docs/satellite-setup.md` | adding sources and the second machine |
| `docs/setup-session-runbook.md` | facilitating someone else's setup in one sitting |

## Quick start

**macOS placement rule.** Clone to `~/.local/share/command-board`, not under `~/Documents`, `~/Desktop`, or `~/Downloads`: launchd agents cannot read those folders (Apple's privacy protections), so a scheduled run fails with "Operation not permitted" while a manual run from your terminal works. Keep a symlink in your projects folder if you like. The installer refuses a protected path. For the same reason a scheduled run cannot scan `~/Downloads`; the launchd jobs set `BOARD_SCHEDULED=1` and the collector skips that scan (a blocked read can hang on a consent prompt that never appears, not just fail); terminal runs still read it.

1. Copy `config/lines.example.json` to `config/lines.json`, `config/persona.example.md` to `config/persona.md`, and `config/repos.example.json` to `config/repos.json`; edit for your businesses.
2. Attach Gmail, Google Calendar, and Google Drive (and optionally Slack) as connectors on your claude.ai account; confirm `claude mcp list` shows them.
3. `bin/command-board --collect-only`, read `context/`, then a full `bin/command-board`.
4. Publish `page/command-board.html` as a claude.ai artifact with the `mcp` capability granting Google Drive `search_files`, `read_file_content`, and `create_file`; pin it.
5. `bin/install.sh hub` for the 06:45 / 16:45 schedule. On the second machine, follow `docs/satellite-setup.md`.

Tests: `python3 -m unittest discover -s . -p 'test_*.py'`. Python 3.11+ (uses `zoneinfo`), no third-party packages; `certifi` is picked up if present for the python.org build.

## What it costs and what it is not

Measured on a busy inbox with Sonnet 4.6: roughly 2 to 6 US dollars per cycle before the turn and thinking caps, so budget a few dollars a day and check the numbers in your own transcripts; a single daily run, a smaller memory window, or Haiku for the publish step all cut it. It is not a task manager and not real time: it runs on a schedule and tells you what is going on; you act in the original tools.

MIT licensed. Built by Matt Walker as personal tooling; the private instance runs with real configuration in a separate repo.
