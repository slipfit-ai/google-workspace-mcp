"""Create or tear down the cross-account fixtures that tests/test_live_guards.py runs against.

Needs three configured accounts: a gmail.com account and two Workspace accounts in different
organizations, each one an admin of its domain. Organization B also needs a second user (the
"teammate") to own a file.

    uv run python tests/live_fixtures.py create --gmail personal --org-a work \\
        --org-b partner --teammate teammate@partner.example
    GWMCP_FIXTURES=local/live-fixtures.json uv run pytest -m live
    uv run python tests/live_fixtures.py teardown

Each fixture is a gwmcp-test-* folder holding one Doc and one Sheet:
  F1 org A My Drive, owned by org A           -> org A may write
  F2 org B My Drive, owned by the teammate    -> org B may write
  F3 org B shared drive                       -> org B may write
  F4 gmail My Drive, shared to org A and B    -> org A and B read-only
  F5 org A My Drive, shared to org B and gmail -> org B and gmail read-only
  F6 org A shared drive, org B a member       -> org A may write; org B read-only
  F7 gmail My Drive, owned by gmail           -> gmail may write

Side effects: the teammate gets one email for each item transferred to them (3). Teardown
trashes everything the three accounts own and deletes both shared drives, but it can't trash
F2: the teammate owns it and has to trash the gwmcp-test-F2 folder themselves.
"""

import argparse
import json
import uuid
from typing import Any

from loguru import logger

from google_workspace_mcp.config import PROJECT_ROOT
from google_workspace_mcp.google import account, service

OUT = PROJECT_ROOT / "local" / "live-fixtures.json"
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"


def drive(name: str) -> Any:
    """Drive v3 client for an account."""
    return service(account(name), "drive", "v3")


def _create(svc: Any, name: str, mime: str, parent: str) -> str:
    body = {"name": name, "mimeType": mime, "parents": [parent]}
    return svc.files().create(body=body, fields="id", supportsAllDrives=True).execute()["id"]


def bundle(svc: Any, tag: str, parent: str = "root") -> dict[str, str]:
    """A folder holding one Doc and one Sheet."""
    folder = _create(svc, f"gwmcp-test-{tag}", FOLDER, parent)
    return {
        "folder": folder,
        "doc": _create(svc, f"gwmcp-test-{tag}-doc", DOC, folder),
        "sheet": _create(svc, f"gwmcp-test-{tag}-sheet", SHEET, folder),
    }


def share(svc: Any, file_id: str, email: str) -> None:
    """Grant writer access without a notification email."""
    svc.permissions().create(
        fileId=file_id,
        body={"type": "user", "role": "writer", "emailAddress": email},
        sendNotificationEmail=False,
        supportsAllDrives=True,
    ).execute()


def shared_drive(svc: Any, name: str) -> str:
    """Create a shared drive and return its id."""
    return svc.drives().create(requestId=str(uuid.uuid4()), body={"name": name}).execute()["id"]


def create(gmail: str, org_a: str, org_b: str, teammate: str) -> None:
    """Build F1-F7 and write their ids to local/live-fixtures.json."""
    pe, sa, sb = drive(gmail), drive(org_a), drive(org_b)
    email = {k: account(k).email for k in (gmail, org_a, org_b)}
    fx: dict[str, Any] = {"accounts": {"gmail": gmail, "org_a": org_a, "org_b": org_b}}

    fx["F1"] = bundle(sa, "F1")
    fx["F2"] = bundle(sb, "F2")
    for fid in fx["F2"].values():
        sb.permissions().create(
            fileId=fid,
            body={"type": "user", "role": "owner", "emailAddress": teammate},
            transferOwnership=True,
        ).execute()
    d3 = shared_drive(sb, "gwmcp-test-F3")
    fx["F3"] = {"drive": d3, **bundle(sb, "F3", d3)}
    fx["F4"] = bundle(pe, "F4")
    share(pe, fx["F4"]["folder"], email[org_a])
    share(pe, fx["F4"]["folder"], email[org_b])
    fx["F5"] = bundle(sa, "F5")
    share(sa, fx["F5"]["folder"], email[org_b])
    share(sa, fx["F5"]["folder"], email[gmail])
    d6 = shared_drive(sa, "gwmcp-test-F6")
    fx["F6"] = {"drive": d6, **bundle(sa, "F6", d6)}
    share(sa, d6, email[org_b])
    fx["F7"] = bundle(pe, "F7")

    OUT.write_text(json.dumps(fx, indent=2))
    logger.success(f"Fixtures written to {OUT}")


def teardown() -> None:
    """Trash every owned fixture folder and delete both shared drives."""
    fx = json.loads(OUT.read_text())
    acc = fx["accounts"]
    for key, owner in (("F1", "org_a"), ("F4", "gmail"), ("F5", "org_a"), ("F7", "gmail")):
        drive(acc[owner]).files().update(fileId=fx[key]["folder"], body={"trashed": True}).execute()
        logger.info(f"{key}: trashed")
    for key, owner in (("F3", "org_b"), ("F6", "org_a")):
        drive(acc[owner]).drives().delete(
            driveId=fx[key]["drive"], useDomainAdminAccess=True, allowItemDeletion=True
        ).execute()
        logger.info(f"{key}: shared drive deleted")
    logger.warning(f"F2 is owned by the teammate: they must trash folder {fx['F2']['folder']}.")
    OUT.unlink()


def main() -> None:
    """CLI: create or teardown."""
    parser = argparse.ArgumentParser(description="Live-test fixtures for the write guard.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    for flag in ("--gmail", "--org-a", "--org-b", "--teammate"):
        c.add_argument(flag, required=True)
    sub.add_parser("teardown")
    args = parser.parse_args()
    if args.cmd == "create":
        create(args.gmail, args.org_a, args.org_b, args.teammate)
    else:
        teardown()


if __name__ == "__main__":
    main()
