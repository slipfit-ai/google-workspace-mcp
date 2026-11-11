"""Offline tests of the local-file guards behind upload_file and download_file."""

import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp import localfiles

PEM = b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"
TOKEN_JSON = b'{"refresh_token": "1//0gAbCdEfGhIjKlMnOpQrStUvWxYz"}'


@pytest.mark.parametrize(
    ("rel", "ok"),
    [
        ("Downloads/statement.pdf", True),
        ("Library/Mobile Documents/com~apple~CloudDocs/a.md", True),
        ("Library/CloudStorage/GoogleDrive-x/My Drive/a.csv", True),
        (".ssh/id_ed25519", False),
        ("code/app/.env", False),
        ("Library/Keychains/login.keychain-db", False),
    ],
)
def test_path_rules(tmp_path: Path, rel: str, ok: bool) -> None:
    target = tmp_path / rel
    if ok:
        assert localfiles.check_local_path(target, home=tmp_path) == target.resolve()
    else:
        with pytest.raises(ToolError, match="Refused"):
            localfiles.check_local_path(target, home=tmp_path)


def test_outside_home_and_symlink_escape_refused(tmp_path: Path) -> None:
    home, secret = tmp_path / "home", tmp_path / "home" / ".aws" / "credentials"
    secret.parent.mkdir(parents=True)
    secret.write_text("x")
    (home / "Downloads").mkdir()
    (home / "Downloads" / "harmless.pdf").symlink_to(secret)
    with pytest.raises(ToolError, match="Refused"):
        localfiles.check_local_path(home / "Downloads" / "harmless.pdf", home=home)
    with pytest.raises(ToolError, match="must be under"):
        localfiles.check_local_path("/etc/hosts", home=home)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("id_rsa", b"x"),
        ("client_secret_123.apps.googleusercontent.com.json", b"{}"),
        ("server.PEM", b"x"),
        ("statement.pdf", PEM),  # renamed key: caught by content
        ("notes.md", TOKEN_JSON),
    ],
)
def test_secrets_refused(name: str, data: bytes) -> None:
    with pytest.raises(ToolError, match="Refused"):
        localfiles.refuse_secrets(Path(name), data)


def test_ordinary_files_pass() -> None:
    localfiles.refuse_secrets(Path("README.md"), b'Set "refresh_token": "..." in the config.')
    localfiles.refuse_secrets(Path("statement.pdf"), b"%PDF-1.7 ...")


@pytest.mark.parametrize(
    ("name", "mime", "expected"),
    [
        ("../../.zshrc", "application/pdf", "____.zshrc.pdf"),
        ("statement_20261016.pdf", "application/pdf", "statement_20261016.pdf"),
        ("photo.jpeg", "image/jpeg", "photo.jpeg"),
        (
            "Budget",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Budget.xlsx",
        ),
        ("notes.md", "text/markdown", "notes.md"),
    ],
)
def test_safe_name(name: str, mime: str, expected: str) -> None:
    assert localfiles.safe_name(name, mime) == expected


def test_write_new_never_overwrites_and_quarantines(tmp_path: Path) -> None:
    first = localfiles.write_new(tmp_path, "a.pdf", b"one")
    second = localfiles.write_new(tmp_path, "a.pdf", b"two")
    assert (first.name, second.name) == ("a.pdf", "a (1).pdf")
    assert first.read_bytes() == b"one"
    assert second.stat().st_mode & 0o111 == 0
    attr = subprocess.run(
        ["xattr", "-p", "com.apple.quarantine", str(second)], capture_output=True, text=True
    )
    assert attr.stdout.startswith("0081;")
