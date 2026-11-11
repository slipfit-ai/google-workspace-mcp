"""Drive tools: find and read anything the account can see; change only internal items.

Internal means owned by the account's organization (see `require_internal`). Content shared in
from outside stays read-only, so an injected instruction can't use it as a drop box. No tool
shares or changes permissions: every outward share must stay a human's decision.
"""

from typing import Any

from googleapiclient.errors import HttpError
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from google_workspace_mcp.app import app
from google_workspace_mcp.config import Account
from google_workspace_mcp.google import account, google_errors, service
from google_workspace_mcp.untrusted import fence

MAX_RESULTS = 100
MAX_CONTENT_CHARS = 100_000
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
FOLDER = "application/vnd.google-apps.folder"
EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",  # first sheet only
    "application/vnd.google-apps.presentation": "text/plain",
}
TEXT_TYPES = {"application/json", "application/xml", "application/javascript", "application/x-yaml"}
FIELDS = (
    "id, name, mimeType, modifiedTime, size, owners(emailAddress), ownedByMe, parents, "
    "driveId, trashed, webViewLink"
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


def drive_client(name: str) -> tuple[Any, Account]:
    """The Drive client and resolved account for a chat-style account name."""
    acct = account(name)
    return service(acct, "drive", "v3"), acct


def _get(svc: Any, file_id: str) -> dict[str, Any]:
    """A file's metadata, including items in shared drives."""
    return svc.files().get(fileId=file_id, fields=FIELDS, supportsAllDrives=True).execute()


def _describe(f: dict[str, Any]) -> str:
    """One file as text."""
    owners = ", ".join(o["emailAddress"] for o in f.get("owners", [])) or "shared drive"
    return "\n".join(
        [
            f"{f['name']} (id {f['id']})",
            f"  Type: {f['mimeType']}" + (f", {int(f['size']):,} bytes" if f.get("size") else ""),
            f"  Owner: {owners}" + (" (this account)" if f.get("ownedByMe") else ""),
            f"  Modified: {f.get('modifiedTime', '?')}",
            f"  Parents: {', '.join(f.get('parents', [])) or '(none)'}",
            *([f"  Shared drive: {f['driveId']}"] if f.get("driveId") else []),
            *(["  In Trash"] if f.get("trashed") else []),
            f"  Link: {f.get('webViewLink', '')}",
        ]
    )


def require_internal(svc: Any, acct: Account, f: dict[str, Any], verb: str) -> dict[str, Any]:
    """Refuse unless the item belongs to this account's organization.

    Shared-drive items have no owner, so the drive itself is checked: an admin-access lookup
    only succeeds when the drive belongs to a domain this account administers. In My Drive,
    every owner address must be internal (`Account.is_internal`); a hidden address fails.
    """
    if drive_id := f.get("driveId"):
        try:
            svc.drives().get(driveId=drive_id, useDomainAdminAccess=True, fields="id").execute()
        except HttpError as exc:
            if exc.status_code not in (403, 404):
                raise
            raise ToolError(
                f"Refused to {verb} {f['name']!r}: its shared drive belongs to another "
                "organization (or this account isn't an admin of the drive's domain). "
                "It's read-only here."
            ) from exc
        return f
    owners = [o.get("emailAddress", "") for o in f.get("owners", [])]
    if owners and all(acct.is_internal(o) for o in owners):
        return f
    shown = ", ".join(o or "(hidden address)" for o in owners) or "an unknown owner"
    raise ToolError(
        f"Refused to {verb} {f['name']!r}: it's owned by {shown}, outside {acct.key}. "
        "Content shared in from outside is read-only here."
    )


def internal_folder(svc: Any, acct: Account, folder_id: str, verb: str) -> dict[str, Any]:
    """A folder (or shared-drive root) the account may add items to."""
    folder = _get(svc, folder_id)
    if folder["mimeType"] != FOLDER:
        raise ToolError(f"{folder['name']!r} is not a folder.")
    return require_internal(svc, acct, folder, verb)


@app.tool(annotations=READ)
@google_errors
def search_files(
    account: str,
    text: str | None = None,
    drive_query: str | None = None,
    max_results: int = 25,
    page_token: str | None = None,
) -> str:
    """Find Drive files the account can see (owned, shared with it, shared drives), newest
    first, Trash excluded. `text` searches names and contents; `drive_query` takes Drive
    query syntax (e.g. "mimeType = 'application/vnd.google-apps.folder'",
    "'<folder id>' in parents", "sharedWithMe"). Both may be combined."""
    svc, acct = drive_client(account)
    key = acct.key
    clauses = ["trashed = false"]
    if text:
        escaped = text.replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"fullText contains '{escaped}'")
    if drive_query:
        clauses.append(f"({drive_query})")
    q = " and ".join(clauses)
    listing = (
        svc.files()
        .list(
            q=q,
            pageSize=min(max_results, MAX_RESULTS),
            pageToken=page_token,
            orderBy=None if text else "modifiedTime desc",  # fullText can't be sorted
            fields=f"nextPageToken, files({FIELDS})",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = listing.get("files", [])
    if not files:
        return f"No files in {key} match {q!r}."
    more = listing.get("nextPageToken")
    footer = f"\n\nMore results: page_token={more}" if more else ""
    return fence(f"drive:{key}:search", "\n\n".join(_describe(f) for f in files)) + footer


@app.tool(annotations=READ)
@google_errors
def get_file_metadata(account: str, file_id: str) -> str:
    """A file's or folder's name, type, size, owner, parents, location and link."""
    svc, acct = drive_client(account)
    return fence(f"drive:{acct.key}:{file_id}", _describe(_get(svc, file_id)))


@app.tool(annotations=READ)
@google_errors
def read_file_content(account: str, file_id: str) -> str:
    """Read a file as text: Google Docs and Slides as plain text, Sheets as CSV (first sheet
    only), and plain-text files (txt, md, csv, json, code). Other types (PDF, images, Office
    files) are refused; their metadata is still available."""
    svc, acct = drive_client(account)
    key = acct.key
    f = _get(svc, file_id)
    mime = f["mimeType"]
    if mime in EXPORTS:
        raw: bytes = svc.files().export(fileId=file_id, mimeType=EXPORTS[mime]).execute()
    elif mime.startswith("text/") or mime in TEXT_TYPES:
        if int(f.get("size", 0)) > MAX_DOWNLOAD_BYTES:
            raise ToolError(f"{f['name']!r} is over {MAX_DOWNLOAD_BYTES // 2**20} MB; not read.")
        raw = svc.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    else:
        raise ToolError(f"Can't read {f['name']!r} as text: type {mime} isn't supported.")
    text = raw.decode("utf-8", errors="replace").lstrip("﻿")
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + f"\n[... truncated at {MAX_CONTENT_CHARS} characters]"
    note = " (first sheet only)" if mime.endswith("spreadsheet") else ""
    return fence(f"drive:{key}:{f['name']}{note}", text)


@app.tool(annotations=CREATE)
@google_errors
def create_folder(account: str, name: str, parent_id: str = "root") -> str:
    """Create a folder. parent_id is a folder id, a shared drive id, or "root" (top of My
    Drive). The parent must belong to this account's organization; see search_files."""
    svc, acct = drive_client(account)
    parent = internal_folder(svc, acct, parent_id, "create a folder in")
    body = {"name": name, "mimeType": FOLDER, "parents": [parent["id"]]}
    f = svc.files().create(body=body, fields=FIELDS, supportsAllDrives=True).execute()
    return f"Created folder {f['name']!r} in {parent['name']!r} ({acct.key}).\n{_describe(f)}"


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def move_file(account: str, file_id: str, folder_id: str) -> str:
    """Move a file or folder into another folder. Both must belong to this account's
    organization; anything shared in from outside is refused. Use "root" for the top of My
    Drive, or a shared drive id for the top of that drive."""
    svc, acct = drive_client(account)
    f = require_internal(svc, acct, _get(svc, file_id), "move")
    folder = internal_folder(svc, acct, folder_id, "move files into")
    moved = (
        svc.files()
        .update(
            fileId=file_id,
            addParents=folder["id"],
            removeParents=",".join(f.get("parents", [])),
            fields=FIELDS,
            supportsAllDrives=True,
        )
        .execute()
    )
    return f"Moved {moved['name']!r} into {folder['name']!r} in {acct.key}."


@app.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@google_errors
def trash_file(account: str, file_id: str) -> str:
    """Move a file or folder that belongs to this account's organization to Trash
    (restorable with untrash_file for 30 days). Nothing is permanently deleted."""
    svc, acct = drive_client(account)
    f = require_internal(svc, acct, _get(svc, file_id), "trash")
    svc.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
    return f"Moved {f['name']!r} to Trash in {acct.key} (restorable for 30 days)."


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def untrash_file(account: str, file_id: str) -> str:
    """Restore a file or folder that belongs to this account's organization from Trash."""
    svc, acct = drive_client(account)
    f = require_internal(svc, acct, _get(svc, file_id), "restore")
    svc.files().update(fileId=file_id, body={"trashed": False}, supportsAllDrives=True).execute()
    return f"Restored {f['name']!r} from Trash in {acct.key}."
