"""Guards for files crossing between Google and the local disk.

Any file type may move; what's refused is where it comes from and whether it carries a secret:
- Paths must sit under $HOME, outside every dot-folder and ~/Library (except iCloud Drive and
  ~/Library/CloudStorage). On macOS those hold nearly every credential: ~/.ssh, ~/.aws,
  ~/.config, Keychains, browser profiles.
- Uploads are refused by secret-looking name, and by content (a PEM private key, or JSON with a
  real-looking refresh_token or client_secret), so a renamed key is caught too.
- Downloads get a flat, cleaned-up name (Drive names are attacker-controlled), never overwrite,
  lose the execute bit, and carry macOS's quarantine flag like any browser download.
"""

import fnmatch
import mimetypes
import re
import subprocess
import time
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

HOME = Path.home()
DOWNLOADS = HOME / "Downloads"
LIBRARY_ALLOWED = (
    Path("Library/Mobile Documents/com~apple~CloudDocs"),
    Path("Library/CloudStorage"),
)
SECRET_NAMES = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.ppk", "*.ovpn",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "client_secret*.json", "credentials*.json", "*service-account*.json",
)  # fmt: skip
SECRET_CONTENT = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb'|"(?:refresh_token|client_secret|private_key)"\s*:\s*"[^"]{20,}"'
)
UNSAFE_NAME_CHARS = re.compile(r'[/\\:\x00-\x1f"<>|?*]')


def check_local_path(raw: str | Path, home: Path = HOME) -> Path:
    """The resolved path, refused unless it sits under home outside dot-folders and Library.

    Symlinks are resolved first, so a link can't point the check somewhere it would refuse.
    """
    path = Path(raw).expanduser().resolve()
    home = home.resolve()
    if not path.is_relative_to(home):
        raise ToolError(f"Refused {str(path)!r}: local files must be under {home}.")
    rel = path.relative_to(home)
    if any(part.startswith(".") for part in rel.parts):
        raise ToolError(f"Refused {str(path)!r}: dotfiles and dot-folders are off limits.")
    if rel.parts[:1] == ("Library",) and not any(rel.is_relative_to(p) for p in LIBRARY_ALLOWED):
        raise ToolError(
            f"Refused {str(path)!r}: ~/Library is off limits except iCloud Drive and "
            "~/Library/CloudStorage."
        )
    return path


def refuse_secrets(path: Path, data: bytes) -> None:
    """Refuse a file whose name or content looks like a credential."""
    name = path.name.lower()
    if pattern := next((p for p in SECRET_NAMES if fnmatch.fnmatch(name, p)), None):
        raise ToolError(f"Refused {path.name!r}: its name matches secret pattern {pattern!r}.")
    if SECRET_CONTENT.search(data):
        raise ToolError(
            f"Refused {path.name!r}: it contains what looks like a private key or OAuth secret."
        )


def safe_name(name: str, mime: str) -> str:
    """A flat file name: no separators, control characters or leading dots, extension matching
    the MIME type."""
    cleaned = UNSAFE_NAME_CHARS.sub("_", name).replace("..", "_").strip().lstrip(".").strip()
    cleaned = cleaned or "download"
    known = mimetypes.guess_all_extensions(mime)
    if known and Path(cleaned).suffix.lower() not in known:
        cleaned += mimetypes.guess_extension(mime) or known[0]
    return cleaned[:200]


def write_new(directory: Path, name: str, data: bytes) -> Path:
    """Write data to a new file in directory, adding " (n)" rather than overwriting.

    Opened with "xb" so a file that appears between the check and the write isn't clobbered.
    """
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(1000):
        target = directory / (name if n == 0 else f"{stem} ({n}){suffix}")
        try:
            with target.open("xb") as fh:
                fh.write(data)
        except FileExistsError:
            continue
        target.chmod(0o644)
        quarantine(target)
        return target
    raise ToolError(f"Too many files named {name!r} in {directory}.")


def quarantine(path: Path) -> None:
    """Flag the file as downloaded from the internet, so Gatekeeper checks it before it runs."""
    value = f"0081;{int(time.time()):x};google-workspace-mcp;"
    subprocess.run(["xattr", "-w", "com.apple.quarantine", value, str(path)], check=True)
