# Slack sources: add any channel or DM the connector cannot reach

The claude.ai Slack connector is bound to one workspace. Every other workspace is read through `watchers/slack_watch.py`: one read-only user token per workspace, a registry of named conversations, and independent cursors per consumer. Nothing is ever posted.

## Part 1: you, once per workspace (needs your Slack login)

1. **Check app-install policy.** If the workspace requires admin approval, warn its admin that a read-only app request is coming; Slack shows "Request to Install" instead of "Install" in that case.
2. **Create the app** at https://api.slack.com/apps → Create New App → From scratch. Name it something obvious, e.g. `command board reader`. Pick the workspace.
3. **User Token Scopes only** (OAuth & Permissions → *User* Token Scopes, not Bot): `users:read` always, plus one history scope per conversation kind you will register: `im:history` (1:1 DM), `mpim:history` (group DM), `channels:history` (public channel), `groups:history` (private channel). Add every scope you will need up front; adding one later means reinstalling and a new token. If you want the watcher to enumerate conversations for you, also add the matching `*:read` scopes.
4. **Install** (or request install). Copy the **User OAuth Token** (`xoxp-…`).
5. **Store it** outside all repos, one line per workspace; the key is `SLACK_TOKEN_` + the workspace short name in caps:
   ```bash
   mkdir -p ~/.config/slack-watch && chmod 700 ~/.config/slack-watch
   touch ~/.config/slack-watch/env && chmod 600 ~/.config/slack-watch/env
   echo 'SLACK_TOKEN_ACME=xoxp-PASTE' >> ~/.config/slack-watch/env
   ```
6. **Find the conversation id.** Open the DM or channel in a browser; the id is the last path segment (`D…` 1:1 DM, `G…` group DM or private channel, `C…` public channel). Or "Copy link" on any message: the id follows `/archives/`.

## Part 2: per conversation

```bash
python3 watchers/slack_watch.py add --name priya-dm --workspace acme --channel DXXXXXXXX --kind im --label "Acme DM with Priya"
python3 watchers/slack_watch.py check                         # auth.test per workspace; prints team + user, never the token
python3 watchers/slack_watch.py --source priya-dm --days 7    # smoke test, moves no cursor
python3 watchers/slack_watch.py --source priya-dm             # initialize the interactive cursor
python3 watchers/slack_watch.py --all --consumer board --peek # what the next hub run will see
```

Acceptance: `--days 7` matches what you see in Slack including thread replies and real names; a second no-flag run prints "no new messages"; the token appears in no repo, no `CLAUDE*.md`, and no terminal output.

The hub calls `--all --consumer board --json` automatically once `~/.config/slack-watch/sources.json` exists. Interactive checks use the default consumer, so the two never steal each other's messages.

## Interactive use inside a project
Put this in that project's local, gitignored agent instructions so sessions can check a conversation on demand:

```markdown
## Slack watch (read-only)
- Conversation X is readable via `python3 <kit>/watchers/slack_watch.py --source priya-dm` (`--peek` to look without marking seen; `--days N` for a window).
- Always surface the output: summarize what is new, then list action items with owner, ask, and any stated date. If nothing is new, say so in one line.
- Read-only. Never post, react, or attempt any Slack write. Never read or print `~/.config/slack-watch/env`.
- On `invalid_auth`, `missing_scope`, `channel_not_found`: report the error verbatim and stop.
```

## Gotchas
- `missing_scope` usually means the conversation kind does not match the installed scope. Fix the scope, reinstall, update the token line.
- Replies on threads whose parent is older than 14 days are not detected (`THREAD_LOOKBACK_DAYS`). Edits and deletions are not re-reported.
- A consumer's cursor only advances for sources read successfully; a failed workspace never loses messages.
- Revocation: uninstall the app or revoke the token, then delete the token line and that workspace's sources.
