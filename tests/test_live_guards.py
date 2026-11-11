"""Live tests of the internal-only write guard against real cross-account fixtures.

Build the fixtures first (tests/live_fixtures.py describes them), then:
GWMCP_FIXTURES=local/live-fixtures.json uv run pytest -m live
"""

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp import content, drive
from google_workspace_mcp.google import account, service

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif("GWMCP_FIXTURES" not in os.environ, reason="GWMCP_FIXTURES not set"),
]


FIXTURES = os.environ.get("GWMCP_FIXTURES")
ACC: dict[str, str] = json.loads(Path(FIXTURES).read_text())["accounts"] if FIXTURES else {}


@pytest.fixture(scope="module")
def fx() -> dict[str, dict[str, str]]:
    """Fixture ids keyed F1-F7, plus "accounts": role (gmail, org_a, org_b) -> account."""
    return json.loads(Path(os.environ["GWMCP_FIXTURES"]).read_text())


@pytest.fixture(scope="module")
def made() -> Iterator[list[tuple[str, str]]]:
    """Items the tests create, trashed (by the owning account) afterwards."""
    items: list[tuple[str, str]] = []
    yield items
    for acct, file_id in items:
        drive.trash_file(acct, file_id)


def _id(result: str) -> str:
    """The file id from a create tool's reply."""
    return result.split("(id ", 1)[1].split(")", 1)[0]


def _refused(fn: Callable[..., Any], *args: Any) -> None:
    with pytest.raises(ToolError, match="Refused"):
        fn(*args)


def _doc_text(acct: str, doc_id: str) -> str:
    doc = service(account(acct), "docs", "v1").documents().get(documentId=doc_id).execute()
    return "".join(
        r["textRun"]["content"]
        for el in doc["body"]["content"]
        for r in el.get("paragraph", {}).get("elements", [])
        if "textRun" in r
    )


def _cells(acct: str, sheet_id: str, a1: str) -> list[list[str]]:
    values = service(account(acct), "sheets", "v4").spreadsheets().values()
    return values.get(spreadsheetId=sheet_id, range=a1).execute().get("values", [])


INTERNAL = [
    ("org_a", "F1"),  # own My Drive
    ("org_b", "F2"),  # teammate-owned
    ("org_b", "F3"),  # own org's shared drive
    ("org_a", "F6"),  # own org's shared drive, with an outside member
    ("gmail", "F7"),  # consumer account, owns it
]
EXTERNAL = [
    ("org_a", "F4"),  # gmail.com owner
    ("org_b", "F4"),
    ("org_b", "F5"),  # another Workspace owns it
    ("gmail", "F5"),
    ("org_b", "F6"),  # member of another org's shared drive
]


@pytest.mark.parametrize(("role", "key"), INTERNAL)
def test_internal_writes(
    role: str, key: str, fx: dict[str, dict[str, str]], made: list[tuple[str, str]]
) -> None:
    acct = ACC[role]
    f = fx[key]
    marker = f"\ngwmcp {acct} {key} append"
    content.append_doc_text(acct, f["doc"], marker)
    assert marker.strip() in _doc_text(acct, f["doc"])

    content.update_sheet_range(acct, f["sheet"], "A1", [["gwmcp", key, 1]])
    assert _cells(acct, f["sheet"], "A1:C1") == [["gwmcp", key, "1"]]

    doc = _id(content.create_doc(acct, f"gwmcp-test-{key}-new", f["folder"], "# Hi\n\n**x**"))
    made.append((acct, doc))
    assert "Hi" in _doc_text(acct, doc)
    made.append((acct, _id(content.create_sheet(acct, "gwmcp-t", f["folder"], [["a", 2]]))))
    made.append((acct, _id(drive.create_folder(acct, f"gwmcp-test-{key}-sub", f["folder"]))))


@pytest.mark.parametrize(("role", "key"), EXTERNAL)
def test_external_read_only(role: str, key: str, fx: dict[str, dict[str, str]]) -> None:
    acct = ACC[role]
    f = fx[key]
    assert "gwmcp-test" in drive.get_file_metadata(acct, f["doc"])  # still readable
    _refused(content.append_doc_text, acct, f["doc"], "x")
    _refused(content.update_sheet_range, acct, f["sheet"], "A1", [["x"]])
    _refused(content.create_doc, acct, "x", f["folder"])
    _refused(content.create_sheet, acct, "x", f["folder"])
    _refused(drive.create_folder, acct, "x", f["folder"])
    _refused(drive.trash_file, acct, f["doc"])
    _refused(drive.move_file, acct, f["doc"], "root")


def test_external_shared_drive_root_refused(fx: dict[str, dict[str, str]]) -> None:
    _refused(drive.create_folder, ACC["org_b"], "x", fx["F6"]["drive"])


def test_move_internal_file_out_refused(
    fx: dict[str, dict[str, str]], made: list[tuple[str, str]]
) -> None:
    doc = _id(content.create_doc(ACC["org_a"], "gwmcp-test-move", fx["F1"]["folder"]))
    made.append((ACC["org_a"], doc))
    _refused(drive.move_file, ACC["org_a"], doc, fx["F4"]["folder"])  # into a gmail.com folder
    drive.move_file(ACC["org_a"], doc, "root")  # internal destination is fine


def test_sheet_formulas_stored_as_text(fx: dict[str, dict[str, str]]) -> None:
    formula = '=IMPORTDATA("https://x.test")'
    content.update_sheet_range(ACC["org_a"], fx["F1"]["sheet"], "D1", [[formula]])
    assert _cells(ACC["org_a"], fx["F1"]["sheet"], "D1") == [[formula]]  # text, not a result


def test_markdown_images_refused(fx: dict[str, dict[str, str]]) -> None:
    with pytest.raises(ToolError, match="images"):
        content.create_doc(ACC["org_a"], "x", fx["F1"]["folder"], "![a](https://x.test/p.png)")
