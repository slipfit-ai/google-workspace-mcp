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
| Drive | `search_files`, `get_file_metadata`, `read_file_content`, `create_folder`, `move_file`, `trash_file`, `untrash_file` | Writes only touch internal content (see [Write guard](#write-guard)). No tool shares or changes permissions. |
| Docs & Sheets | `create_doc`, `create_sheet`, `append_doc_text`, `update_sheet_range` | Same write guard. Sheet values are stored as text, never run as formulas. Markdown with images is refused. |
| File transfer | `upload_file`, `download_file` | Uploads go only into internal folders, as new unconverted files, and refuse secrets. Downloads land in `~/Downloads`, quarantined. See [File transfer](#file-transfer). |

Plus `list_accounts`, which lists the configured accounts.

Everything read from an email, event or file comes back wrapped in `<untrusted_content>` markers,
and the server tells Claude never to follow instructions found inside them. HTML email has hidden
elements stripped first, so text a sender hid from you is also hidden from Claude.

### Write guard

Anything the account can see can be read. Only **internal** content can be created in, written
to, moved or trashed:

- **My Drive items:** every owner's address is in the account's `internal_domains` (for an
  account without them, such as gmail.com, the owner must be the account itself). Files owned by
  a teammate count; files shared in from anyone else are read-only.
- **Shared-drive items:** the drive belongs to a domain this account administers, checked with
  an admin-access lookup. A shared drive from another organization is read-only even when the
  account is a member. **This needs a Workspace admin account**; for a non-admin account, every
  shared drive reads as external and is refused.
- **New items and moves:** the destination folder must pass the same check, since a new file
  inherits its folder's sharing.

Internal content that a person has shared outside the organization stays writable: that share
was a human decision. The guard exists so an instruction injected through outside content can't
use outside-controlled files as a drop box. That's also why the server has no sharing tool.

Two deliberate locks close exfiltration paths that work even inside the organization. Both stay
until they get in the way; then they're worth designing around, not removing:

- **Sheet values are plain text.** Writes are RAW, so `=IMPORTDATA("https://…")` is stored as
  text: a live formula could send sheet data to an outside URL. No tool can write a formula.
- **Markdown images are refused** in `create_doc`: Google fetches image URLs on import.

### File transfer

Any file type can move between Drive and the local disk. The guards are about where a file comes
from and whether it carries a secret, not its type:

- **Local paths** must be under your home folder, outside every dot-folder and `~/Library`
  (iCloud Drive and `~/Library/CloudStorage` are allowed). On macOS those hold nearly every
  credential: `~/.ssh`, `~/.aws`, `~/.config`, Keychains, browser profiles. Symlinks are resolved
  before the check.
- **Uploads refuse secrets**, by name (`*.pem`, `*.key`, `id_rsa*`, `client_secret*.json`, …) and
  by content (a PEM private key, or JSON with a real-looking `refresh_token` or `client_secret`),
  so a key renamed to `.pdf` is caught too. SVGs that reference outside URLs are refused for the
  same reason Markdown images are.
- **Uploads only create.** The destination passes the [write guard](#write-guard); the file is
  stored as-is (a PDF stays a PDF), and a name already in the folder is refused rather than
  duplicated.
- **Downloads** default to `~/Downloads`. Drive names are cleaned to a flat name (no `../`), an
  existing file is never overwritten, and the file is never executable and carries macOS's
  quarantine flag like a browser download. Google Docs, Sheets and Slides export as Office files.
- Both directions cap at 10 MB and log account, path, file id, size and sha256.

Keep `upload_file` on "ask" in Claude Code: the prompt shows the local path and folder, and it's
the human checkpoint for what leaves the machine.

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
2. **APIs & Services → Library**: enable the **Gmail API**, **Google Calendar API**,
   **Google Drive API**, **Google Docs API** and **Google Sheets API**. Docs and Sheets need no
   extra scopes; the Drive scope covers them.
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
internal_domains = ["yourcompany.com"]  # see "Write guard"; leave out for gmail.com
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

The write guard has live tests against real accounts. They need three accounts and a set of
cross-account fixtures; `tests/live_fixtures.py` builds and tears them down and says what's
required.

## License

[MIT](LICENSE). You use your own Google Cloud OAuth client, so your use of the Google APIs is
governed by [Google's API Terms of Service](https://developers.google.com/terms).
