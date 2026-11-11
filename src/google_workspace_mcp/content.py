"""Docs and Sheets tools: create them with content, and write into internal ones.

Every target passes `drive.require_internal` first. Two more guards close exfiltration paths
that would work even inside the organization:
- Sheet values are written RAW, so `=IMPORTDATA("https://...")` stays text: a formula could
  fetch an outside URL carrying sheet data.
- Markdown with images is refused: Google fetches image URLs when it converts the file.
"""

import re
from typing import Any

from googleapiclient.http import MediaInMemoryUpload
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from google_workspace_mcp.app import app
from google_workspace_mcp.config import Account
from google_workspace_mcp.drive import (
    CREATE,
    FIELDS,
    drive_client,
    internal_folder,
    require_internal,
)
from google_workspace_mcp.google import google_errors, service

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
IMAGE = re.compile(r"!\[|<img", re.IGNORECASE)

Cell = str | int | float | bool
OVERWRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def _target(account: str, file_id: str, mime: str, verb: str) -> tuple[Account, dict[str, Any]]:
    """The resolved account and the file, refused unless internal and of the expected type."""
    svc, acct = drive_client(account)
    f = svc.files().get(fileId=file_id, fields=FIELDS, supportsAllDrives=True).execute()
    if f["mimeType"] != mime:
        kind = "Google Doc" if mime == DOC else "Google Sheet"
        raise ToolError(f"{f['name']!r} is not a {kind} (type {f['mimeType']}).")
    return acct, require_internal(svc, acct, f, verb)


def _write_rows(acct: Account, sheet_id: str, a1_range: str, rows: list[list[Cell]]) -> int:
    """Write rows RAW (never evaluated as formulas); return the number of cells updated."""
    result = (
        service(acct, "sheets", "v4")
        .spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range=a1_range,
            valueInputOption="RAW",
            body={"values": rows},
        )
        .execute()
    )
    return int(result.get("updatedCells", 0))


@app.tool(annotations=CREATE)
@google_errors
def create_doc(account: str, name: str, parent_id: str = "root", markdown: str = "") -> str:
    """Create a Google Doc, optionally filled from Markdown (headings, bold, lists and links
    convert; images are refused). parent_id is a folder id, a shared drive id, or
    "root". The parent must belong to this account's organization."""
    if IMAGE.search(markdown):
        raise ToolError("Markdown images aren't allowed: Google fetches their URLs on import.")
    svc, acct = drive_client(account)
    parent = internal_folder(svc, acct, parent_id, "create a Doc in")
    media = MediaInMemoryUpload(markdown.encode(), mimetype="text/markdown") if markdown else None
    f = (
        svc.files()
        .create(
            body={"name": name, "mimeType": DOC, "parents": [parent["id"]]},
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    return f"Created Doc {f['name']!r} (id {f['id']}) in {parent['name']!r}: {f['webViewLink']}"


@app.tool(annotations=CREATE)
@google_errors
def create_sheet(
    account: str, name: str, parent_id: str = "root", rows: list[list[Cell]] | None = None
) -> str:
    """Create a Google Sheet, optionally filled with rows starting at A1 (stored as typed
    values, never as formulas). parent_id is a folder id, a shared drive id, or "root". The
    parent must belong to this account's organization."""
    svc, acct = drive_client(account)
    parent = internal_folder(svc, acct, parent_id, "create a Sheet in")
    f = (
        svc.files()
        .create(
            body={"name": name, "mimeType": SHEET, "parents": [parent["id"]]},
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    filled = f", {_write_rows(acct, f['id'], 'A1', rows)} cells filled" if rows else ""
    return (
        f"Created Sheet {f['name']!r} (id {f['id']}) in {parent['name']!r}{filled}: "
        f"{f['webViewLink']}"
    )


@app.tool(annotations=CREATE)
@google_errors
def append_doc_text(account: str, file_id: str, text: str) -> str:
    """Append plain text to the end of a Google Doc that belongs to this account's
    organization. Include a leading newline to start a new paragraph."""
    acct, f = _target(account, file_id, DOC, "write to")
    request = {"insertText": {"endOfSegmentLocation": {}, "text": text}}
    service(acct, "docs", "v1").documents().batchUpdate(
        documentId=file_id, body={"requests": [request]}
    ).execute()
    return f"Appended {len(text):,} characters to {f['name']!r} in {acct.key}."


@app.tool(annotations=OVERWRITE)
@google_errors
def update_sheet_range(account: str, file_id: str, a1_range: str, rows: list[list[Cell]]) -> str:
    """Overwrite cells in a Google Sheet that belongs to this account's organization.
    a1_range is A1 notation, e.g. "Sheet1!A1" or "Budget!B2:D10". Values are stored as typed
    text and numbers, never evaluated as formulas. Earlier values stay in version history."""
    acct, f = _target(account, file_id, SHEET, "write to")
    cells = _write_rows(acct, file_id, a1_range, rows)
    return f"Updated {cells} cells in {f['name']!r} ({a1_range}) in {acct.key}."
