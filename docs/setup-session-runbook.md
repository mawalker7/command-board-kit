# Setup session runbook (for helping someone else build their board)

Use this when you sit with another person to stand up their own Command Board. Their board is theirs: their accounts, their machine, their connectors, their data. You facilitate; they click anything that involves a password, a consent screen, or an admin setting. Budget about 2.5 hours plus two short follow-ups they do alone.

## Before the session (send two days ahead)

1. A Mac that stays awake at the two run times. (Windows: the generic guide only, no kit scripts.)
2. A claude.ai account with Claude Code and connectors; `claude --version` works; artifacts available.
3. Google Drive, Gmail, and Google Calendar connectors attached for the Google account the board will live on. Slack connector for one workspace if wanted.
4. A GitHub account with `gh auth login` done (the board repo is private).
5. Python 3.11+; on the python.org build, run "Install Certificates.command" once.
6. Their list of business lines with two or three key people per line.

## Agenda

| Min | Step | Who clicks |
|---|---|---|
| 0–10 | Frame: what it is and is not, read-only, cost | you |
| 10–25 | Clone to `~/.local/share/command-board`, create the private repo | them |
| 25–55 | `lines.json`, `persona.md`, `repos.json`; calendars from `list_calendars` | both |
| 55–70 | `bin/command-board --collect-only`; read `context/` together | both |
| 70–90 | First full terminal run; read `out/board-latest.md` | both |
| 90–110 | Publish the page as an artifact from THEIR session (mcp: Google Drive search_files, read_file_content, create_file); open on phone; pin | them |
| 110–125 | `bin/install.sh hub`; prove with `launchctl kickstart -k gui/$(id -u)/com.commandboard.command-board`; watch the log | both |
| 125–150 | Walk the board: expand, Handled, per-line Share view; agree what they will share | both |

Follow-ups they own: `docs/slack-source-setup.md`, `docs/msgraph-setup.md`, `docs/satellite-setup.md`.

## Command sequence

```bash
git clone <kit-url> ~/.local/share/command-board && cd ~/.local/share/command-board
git remote rename origin kit
gh repo create <user>/command-board --private --source . --remote origin --push
cp config/lines.example.json config/lines.json; cp config/persona.example.md config/persona.md; cp config/repos.example.json config/repos.json
# edit the three files
python3 -m unittest discover -s . -p 'test_*.py'
claude mcp list
bin/command-board --collect-only
bin/command-board
bin/install.sh hub
launchctl kickstart -k gui/$(id -u)/com.commandboard.command-board
```

## Pitfalls to check off

1. Placement under Documents/Desktop/Downloads fails or hangs under launchd; symlinks that resolve there are skipped on scheduled runs. Prove with `launchctl kickstart`, never a terminal run.
2. The job passes `--effort low` itself; a high user-level effort setting does not affect it.
3. The publish must produce a Google Doc; a raw text file is unreadable by the page.
4. The page addresses the connector as "Google Drive"; the artifact publish reports any rename.
5. Never run the collector by hand without `BOARD_PEEK=1` (`--collect-only` sets it). Failed runs roll cursors back.
6. Free/busy calendars show untitled blocks until sharing is "See all event details".
7. Slack history scopes must match the conversation kind.
8. Microsoft app registrations need "Allow public client flows" = Yes; Teams read scopes usually need admin consent.
9. Cost is a few dollars a day on a Sonnet-class model; one run a day halves it.
10. Show the per-line Share view and `bin/board_export.py --line <id>` before anything is shared.

## Done means

A launchd-triggered run logged OK with a Drive file id; the board opens on their phone; a Handled tap survives a second run; they know where the logs are (`~/Library/Logs/command-board/`) and which follow-up docs they own.
