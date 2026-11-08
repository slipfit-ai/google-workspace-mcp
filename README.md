# google-workspace-mcp

A local MCP server that gives Claude Code guarded access to the Gmail, Calendar and Drive of
**several Google accounts at once**. Name the account in chat ("check my work inbox", "what's on
my personal calendar this week") and Claude picks the right one.

Video: _coming soon_

## What it can and can't do

| Service | Tools | Guards |
|---|---|---|
| Gmail | `search_threads`, `get_thread`, `list_drafts`, `create_draft`, `list_labels`, `modify_thread_labels`, `archive_thread`, `trash_thread`, `untrash_thread` | **No send or forward tool.** Claude writes drafts; you send them. Trash is recoverable. |
| Calendar | `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`, `delete_event` | No tool can add guests. Events that have guests, or that someone else organizes, can't be changed or deleted. No notifications are sent. |
| Drive | `search_files`, `get_file_metadata`, `read_file_content`, `move_file`, `trash_file`, `untrash_file` | No tool writes file content, uploads or shares. Move and trash only work on files the account owns, in My Drive. Shared drives are read-only. |

Plus `list_accounts`, which lists the configured accounts.

Everything read from an email, event or file comes back wrapped in `<untrusted_content>` markers,
and the server tells Claude never to follow instructions found inside them. HTML email has hidden
elements stripped first, so text a sender hid from you is also hidden from Claude.

**Secrets live only in 1Password.** The OAuth client and each account's refresh token are stored
in a 1Password vault and read through the `op` CLI. Nothing is written to disk, and the first tool
call in a session prompts Touch ID.

## Requirements

- macOS or Linux, [uv](https://docs.astral.sh/uv/), [Claude Code](https://claude.com/claude-code)
- [1Password](https://1password.com) desktop app plus the [`op` CLI](https://developer.1password.com/docs/cli/get-started/),
  with **Settings → Developer → Integrate with 1Password CLI** turned on
- A Google account that can create a Google Cloud project

## Setup from zero

### 1. Google Cloud: one OAuth client for all your accounts

You only do this once, signed in as any one of your accounts. In Chrome, check the avatar in the
top right before you create anything: the console opens as whichever Google account it likes.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project.
2. **APIs & Services → Library**: enable the **Gmail API**, **Google Calendar API** and
   **Google Drive API**.
3. **Google Auth Platform → Branding**: set up the app as **External**. Give it a name and
   your email.
4. **Audience**: add every account you'll connect as a **test user**.
5. **Data Access**: add these scopes:
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/calendar.events`
   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
   - `https://www.googleapis.com/auth/drive`
6. **Clients → Create client**: choose **Desktop app** as the type. Leave
   "Use this client for an AI-powered agent" unchecked, because that option is for Google's own
   MCP tools.
7. In the "OAuth client created" dialog, click **Download JSON before you click OK**. The secret
   can't be downloaded again after the dialog closes.

**Testing vs production.** While the app is in **Testing**, Google expires refresh tokens after
7 days, so you'll re-run `gw-auth` (step 3) about once a week. When a token expires the server
tells Claude the exact command to run, so the weekly fix takes 30 seconds. To stop the weekly
expiry, click **Publish app** on the Audience page. Google asks for a homepage URL and a
privacy-policy URL on a domain you control before it allows this. The app stays "unverified",
which is fine for personal use (100-user cap).

**Google Workspace accounts.** If your organization blocks unverified apps, an admin can trust
the client ID under **Admin console → Security → API controls**.

### 2. 1Password: store the client

```bash
op vault create MCP-Google
op document create ~/Downloads/client_secret_*.json --title google-oauth-client --vault MCP-Google
rm ~/Downloads/client_secret_*.json
op document get google-oauth-client --vault MCP-Google   # prompts Touch ID, prints the JSON
```

To use a different vault name, set `GWMCP_VAULT` in the server's environment.

### 3. Install, list your accounts, authorize each one

```bash
git clone <this repo> && cd google-workspace-mcp
uv sync
mkdir -p local && cp accounts.example.toml local/accounts.toml
```

Edit `local/accounts.toml`. Git ignores `local/`, so your addresses stay out of the repo. The
table key is the account's main name, and `aliases` are any other names you'll use for it in chat:

```toml
[accounts.personal]
email = "you@gmail.com"

[accounts.work]
email = "you@yourcompany.com"
aliases = ["office"]
```

Then authorize each account:

```bash
uv run gw-auth personal
uv run gw-auth work
```

Each run opens a browser consent screen. Pick the matching account and tick every box. The
script checks that you signed in as the configured address and granted every scope. Only then
does it store the refresh token in 1Password, as item `google-<account>`.

### 4. Register with Claude Code

```bash
claude mcp add --scope user google-workspace -- \
  uv run --directory "$(pwd)" google-workspace-mcp
```

Start a new Claude Code session, run `/mcp`, and check that `google-workspace` is connected. Then
ask: *"list my Google accounts"*.

### 5. Turn off the claude.ai Google connectors in Claude Code (optional)

If you also have the claude.ai Gmail, Calendar or Drive connectors on, Claude Code sees two sets
of Google tools. To block the claude.ai ones in every Claude Code session but leave them on in
claude.ai itself, add this to `~/.claude/settings.json`:

```json
{
  "deniedMcpServers": [
    { "serverName": "claude.ai Gmail" },
    { "serverName": "claude.ai Google Calendar" },
    { "serverName": "claude.ai Google Drive" }
  ]
}
```

Turning a connector off in `/mcp` only applies to the current project.

## Troubleshooting

- **"... run `uv run gw-auth <account>`"**: the refresh token expired or was revoked. Run the
  command shown.
- **"Is the 1Password app unlocked?"**: unlock 1Password, then retry. Check that CLI integration
  is turned on.
- **Changed the code?** Reconnect the server from `/mcp` so it restarts.

## Development

```bash
uv run ruff check && uv run pyright
uv run python tests/smoke_stdio.py   # starts the server over stdio and lists its tools
```
