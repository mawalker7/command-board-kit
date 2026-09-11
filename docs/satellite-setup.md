# Satellite setup: give a second machine's context to the hub

**Give this file to Claude Code (or any agent) on the non-hub machine.** The hub runs the board at 07:00 and 17:00 and reads a bundle this machine pushes twenty minutes earlier. No model call is involved; the satellite job is a plain script.

## What the satellite sends
A markdown bundle plus a JSON list of sessions, committed to `data/satellite/<this hostname>/` in your private board repo:

- Claude Code sessions live on this machine and sessions active in the last 24 hours: name, project, last activity, first prompt.
- Every project memory index under `~/.claude/projects/*/memory/MEMORY.md`, plus memory files updated in the last 3 days.
- Markdown deliverables in `~/Downloads` touched in the last 3 days (first 2 KB of each).
- Unpushed work per repo listed in `config/repos.json`: branch, dirty file count, unpushed commit count.

No secrets, no tokens, no `.env` content, no transcripts beyond the first prompt. Bundle capped at 60 KB.

## Steps on this machine

1. Confirm `gh auth status` shows the GitHub user that owns the private board repo.
2. Clone it to `~/.local/share/command-board` (not under `~/Documents`: macOS blocks launchd agents from Documents, Desktop, and Downloads), then `git pull --ff-only`.
3. Dry-run and read the result: `python3 bin/satellite_bundle.py <repo>` then `sed -n 1,40p data/satellite/$(hostname)/bundle.md`. Check the sessions list matches the windows open on this machine and no line contains a token, key, or email address.
4. Push once by hand, then install the schedule: `bin/satellite-sync` and `bin/install.sh satellite` (06:40 and 16:40 daily; launchd runs a missed slot on wake).
5. Verify: `launchctl print gui/$(id -u)/com.commandboard.command-board-satellite | grep -E "state|last exit"` and `git log --oneline -3`.

## Rules
- The only repo this job commits to is the board repo, and only under `data/satellite/`.
- A failed push notifies via macOS notification and exits non-zero; rerun after `git pull --ff-only`.
- Do not install the hub job on this machine. One hub only.
- To stop: `launchctl bootout gui/$(id -u)/com.commandboard.command-board-satellite` and delete the plist.
