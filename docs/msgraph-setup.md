# Microsoft Graph watcher setup (Microsoft 365 Outlook + Teams)

This is the manual, one-time setup for `watchers/msgraph_watch.py`, the
read-only watcher that lets the command board see your organization email
(Outlook) and Teams chats/channels. Nothing in this file is automated —
every step here is something you (or the your M365 admin) has to click
or type by hand.

## 1. Why Graph, not IMAP or mail forwarding

Outlook on a Microsoft 365 tenant does not expose IMAP by default the way
a personal inbox does, and turning it on tenant-wide is a change most
admins won't make for one read-only script. Forwarding mail to a personal
address would leak your organization client correspondence outside the tenant, which
defeats the point of keeping ventures separated. Microsoft Graph is the
one path that reads mail and Teams messages in place, scoped to exactly
the permissions granted, without moving anything or requiring a server
setting change.

## 2. Register the app in Entra ID

1. Go to `entra.microsoft.com` (or `portal.azure.com` → Microsoft Entra ID)
   → **App registrations** → **New registration**.
2. Name it something recognizable: `command board reader`.
3. **Supported account types**:
   - If *you* are registering this app in a tenant *you* control (e.g. a
     personal/consulting Entra tenant), choose **"Accounts in any
     organizational directory"** (multi-tenant). This lets the same app
     registration later request access into the your tenant.
   - If the **your organization admin** is doing the registering directly inside the
     your tenant, single-tenant is fine and simpler.
4. **Redirect URI**: leave blank — the device code flow this script uses
   doesn't need one. Instead, go to **Authentication** (left nav) →
   scroll to **Advanced settings** → set **"Allow public client flows"**
   to **Yes**, then Save.
5. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated permissions** → add:
   - `User.Read`
   - `Mail.Read`
   - `Chat.Read`
   - `ChannelMessage.Read.All`
   - `Team.ReadBasic.All`
   - `Channel.ReadBasic.All`
   - `offline_access`
6. Copy the **Application (client) ID** from the app's Overview page —
   this is `MSGRAPH_CLIENT_ID` in step 4 below.

## 3. The consent reality — read this before asking anyone for anything

`User.Read` and `Mail.Read` are the two permissions a regular user can
usually consent to for themselves, with no admin involved, the first time
they run `login`.

`Chat.Read`, `ChannelMessage.Read.All`, `Team.ReadBasic.All`, and
`Channel.ReadBasic.All` are different: in most business tenants these
require **admin consent**, because they touch Teams data broadly rather
than just the signed-in user's own mailbox. On top of that, many
organizations (your organization's may be one of them) have a tenant-wide policy that
blocks *any* user consent for apps from publishers the tenant hasn't
verified — in which case even `Mail.Read` alone will fail with a
"needs admin approval" error, not just the Teams scopes.

Practically, this means one of two things has to happen before Teams
data (and possibly mail) will work:

- The your M365 admin registers the app **inside the your tenant**
  themselves (repeat step 2, single-tenant, in their tenant) and grants
  admin consent to the six delegated permissions above, **or**
- The your organization admin grants admin consent to *your* multi-tenant app's
  Application ID from their own Entra portal (**Enterprise applications**
  → find the app by its Application ID → **Permissions** → **Grant admin
  consent**).

Either way, someone with admin rights in the your tenant has to act.
There's no way around that for `ChannelMessage.Read.All` / `Chat.Read` in
a business tenant — treat that as a fact, not a blocker to route around.

**Paste-ready message for your M365 admin:**

> Hi — I'd like to run a small read-only script against our Microsoft 365
> tenant so I can see my your organization inbox and Teams messages in one place
> alongside my other consulting work, instead of checking multiple apps.
> It's an app registration named "command board reader" that uses
> the OAuth device-code sign-in flow (no password stored, no browser
> redirect needed). It requests six Microsoft Graph delegated
> permissions, all read-only: User.Read, Mail.Read, Calendars.Read, Chat.Read,
> ChannelMessage.Read.All, Team.ReadBasic.All, Channel.ReadBasic.All, and
> offline_access (so it doesn't need me to sign in every hour). It has no
> write, send, or delete permissions of any kind — it cannot send email,
> post messages, or change anything in Teams or Outlook. Could you either
> register the app in our tenant and grant admin consent, or grant admin
> consent to my existing app's Application ID? Happy to walk through it
> together if that's easier.

## 4. Set up the local config and run it

Create the config directory and env file with tight permissions:

```bash
mkdir -p ~/.config/msgraph-watch
chmod 700 ~/.config/msgraph-watch
cat > ~/.config/msgraph-watch/env <<'EOF'
MSGRAPH_CLIENT_ID=<application-client-id-from-step-2>
MSGRAPH_TENANT=<tenant-id-or-domain-or-"organizations">
EOF
chmod 600 ~/.config/msgraph-watch/env
```

`MSGRAPH_TENANT` can be the your tenant's GUID, its verified domain
(e.g. `contoso.onmicrosoft.com` or a custom domain they've added), or the
literal word `organizations` if you want Microsoft to resolve it from
whichever work account signs in.

Sign in (this is the only step that opens a browser):

```bash
python3 watchers/msgraph_watch.py login
```

It prints a short code and a URL (`https://microsoft.com/devicelogin`).
Open that URL on any device, enter the code, sign in with the your organization
account, and approve consent if prompted. The script polls in the
background and stores tokens in `~/.config/msgraph-watch/tokens.json`
(mode 600) once you finish. It never prints the token itself.

Verify it worked:

```bash
python3 watchers/msgraph_watch.py check
```

This should print your display name, your `userPrincipalName`, and the
list of scopes (`scp`) the token actually carries — compare that against
the six scopes above. If Teams scopes are missing from that list, admin
consent hasn't landed yet even if mail scopes did.

Find IDs for the sources you want to register:

```bash
python3 watchers/msgraph_watch.py discover --chats
python3 watchers/msgraph_watch.py discover --teams
```

Register sources:

```bash
python3 watchers/msgraph_watch.py add --name work-inbox --kind mail --folder inbox --label "your organization Outlook inbox"
python3 watchers/msgraph_watch.py add --name team-general --kind channel --team-id <team-id> --channel-id <channel-id> --label "your organization team — General"
python3 watchers/msgraph_watch.py add --name mario-chat --kind chat --chat-id <chat-id> --label "Mario 1:1"
```

First smoke test — pull the last week without touching the cursor:

```bash
python3 watchers/msgraph_watch.py --all --days 7
```

**Acceptance criteria** (mirrors the Slack watcher's bar):

- Output matches what's actually in Outlook/Teams for that window —
  spot-check a couple of messages against the real inbox/Teams app.
- Running it again with no flags (`python3 watchers/msgraph_watch.py`)
  prints "Nothing new." — the cursor advanced and nothing is re-shown.
- `grep -r` for the access/refresh token string across the repo, your
  shell history, and `~/.config/msgraph-watch/` turns up nothing outside
  `tokens.json` itself (which is mode 600 and gitignored via the
  `context/` pattern not applying here — keep `~/.config/msgraph-watch/`
  itself outside any git-tracked directory, which it already is by
  living under `~/.config`).

## 5. Known gotchas

- **Shared mailboxes**: a shared your organization mailbox isn't reachable via
  `/me/mailFolders/...`. It needs `/users/{shared-mailbox-upn}/mailFolders/...`
  and the `Mail.Read.Shared` delegated permission (also admin-consent
  territory in most tenants). Not wired up in v1 — add it as a new
  `kind: "mail"` source shape later if needed.
- **Channel replies**: Graph doesn't push reply notifications standalone.
  The only signal that a channel message has new replies is its parent's
  `lastModifiedDateTime` moving forward, which is also what happens on a
  plain edit. This watcher re-checks any parent whose `lastModifiedDateTime`
  is newer than the cursor and re-fetches its replies — a message that was
  merely *edited* with no new reply will get its replies re-checked too,
  which is a harmless extra read, not a bug.
- **Edits and deletes are not re-reported as such.** If a message you've
  already seen gets edited, this watcher won't show you a "message
  edited" event — it only surfaces genuinely new messages (by
  `createdDateTime`) after the cursor. Deleted messages simply vanish
  from future fetches with no notice.
- **Throttling**: Graph returns HTTP 429 under load with a `Retry-After`
  header. The watcher honors it and retries up to 5 times before giving
  up and exiting 1 with the Graph error code on stderr.
- **Conditional access policies** (common in security-conscious business
  tenants) can block the device code flow outright, especially if the
  tenant requires a managed/compliant device. If `login` fails
  immediately with something like `AADSTS50076` or a conditional-access
  error, the fallback is the authorization-code flow with a localhost
  redirect listener — that's a real, separate implementation and is
  explicitly **out of scope for v1**. If you hit this, it needs a v2.
- **Revocation**: to cut this off entirely, either delete the app
  registration in Entra (kills every token issued to it, immediately,
  tenant-wide) or have your M365 admin remove the granted consent for it,
  and separately delete `~/.config/msgraph-watch/` on your machine to
  clear the locally cached tokens.

## Endpoint references used while building this

- Device code flow: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code
- List chat messages: https://learn.microsoft.com/en-us/graph/api/chat-list-messages
- List channel messages: https://learn.microsoft.com/en-us/graph/api/channel-list-messages
- List channel message replies: https://learn.microsoft.com/en-us/graph/api/chatmessage-list-replies
