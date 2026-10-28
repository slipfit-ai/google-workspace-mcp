"""Drive tools: find and read anything the account can see; move and trash only what it owns.

No tool writes file content or shares. Shared-drive items are read-only in v1.
"""

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from google_workspace_mcp.app import app
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


def _drive(name: str) -> tuple[Any, str]:
    """The Drive client and resolved account key for a chat-style account name."""
    acct = account(name)
    return service(acct, "drive", "v3"), acct.key


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


def _owned(f: dict[str, Any], verb: str) -> dict[str, Any]:
    """Refuse unless this account owns the item and it's in My Drive, not a shared drive."""
    if f.get("driveId"):
        raise ToolError(
            f"Refused to {verb} {f['name']!r}: it's in a shared drive (read-only in v1)."
        )
    if not f.get("ownedByMe"):
        owner = ", ".join(o["emailAddress"] for o in f.get("owners", [])) or "someone else"
        raise ToolError(
            f"Refused to {verb} {f['name']!r}: it's owned by {owner}, not this account."
        )
    return f


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
    svc, key = _drive(account)
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
    svc, key = _drive(account)
    return fence(f"drive:{key}:{file_id}", _describe(_get(svc, file_id)))


@app.tool(annotations=READ)
@google_errors
def read_file_content(account: str, file_id: str) -> str:
    """Read a file as text: Google Docs and Slides as plain text, Sheets as CSV (first sheet
    only), and plain-text files (txt, md, csv, json, code). Other types (PDF, images, Office
    files) are refused; their metadata is still available."""
    svc, key = _drive(account)
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


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def move_file(account: str, file_id: str, folder_id: str) -> str:
    """Move a file or folder into another folder. Both must be owned by this account and in
    My Drive; anything else is refused. Use "root" as folder_id for the top of My Drive."""
    svc, key = _drive(account)
    f = _owned(_get(svc, file_id), "move")
    folder = _get(svc, folder_id)
    if folder["mimeType"] != FOLDER:
        raise ToolError(f"{folder['name']!r} is not a folder.")
    if folder_id != "root":
        _owned(folder, "move files into")
    moved = (
        svc.files()
        .update(
            fileId=file_id,
            addParents=folder["id"],
            removeParents=",".join(f.get("parents", [])),
            fields=FIELDS,
        )
        .execute()
    )
    return f"Moved {moved['name']!r} into {folder['name']!r} in {key}."


@app.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@google_errors
def trash_file(account: str, file_id: str) -> str:
    """Move a file or folder this account owns to Drive's Trash (restorable with
    untrash_file for 30 days). Nothing is permanently deleted by this server."""
    svc, key = _drive(account)
    f = _owned(_get(svc, file_id), "trash")
    svc.files().update(fileId=file_id, body={"trashed": True}).execute()
    return f"Moved {f['name']!r} to Trash in {key} (restorable for 30 days)."


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def untrash_file(account: str, file_id: str) -> str:
    """Restore a file or folder this account owns from Drive's Trash to its original folder."""
    svc, key = _drive(account)
    f = _owned(_get(svc, file_id), "restore")
    svc.files().update(fileId=file_id, body={"trashed": False}).execute()
    return f"Restored {f['name']!r} from Trash in {key}."
