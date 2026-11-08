"""Account registry: which Google accounts exist and what they are called.

Personal data (emails, aliases) lives in `local/accounts.toml`, which is gitignored.
`accounts.example.toml` at the repo root shows the shape.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "local" / "accounts.toml"

# Gmail: read, label, archive, trash, drafts. The token could send; the server exposes no
# send tool. Calendar: events read/write plus the calendar list. Drive: full scope, needed
# to move files the app didn't create.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/drive",
]


@dataclass(frozen=True)
class Account:
    """One Google account and the names it answers to."""

    key: str
    email: str
    aliases: tuple[str, ...]


def config_path() -> Path:
    """Return the accounts config path (override with GWMCP_CONFIG)."""
    return Path(os.environ.get("GWMCP_CONFIG", DEFAULT_CONFIG))


def load_accounts(path: Path | None = None) -> dict[str, Account]:
    """Load accounts keyed by their canonical key; fail loudly if the config is missing."""
    path = path or config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No accounts config at {path}. Copy accounts.example.toml to local/accounts.toml "
            "and fill in your accounts."
        )
    raw = tomllib.loads(path.read_text())["accounts"]
    return {
        key: Account(key=key, email=entry["email"], aliases=tuple(entry.get("aliases", [])))
        for key, entry in raw.items()
    }


def normalize(name: str) -> str:
    """Lower-case and collapse spaces, hyphens and underscores so 'Side Project' == 'sideproject'."""
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch == "@" or ch == ".")


def resolve(name: str, accounts: dict[str, Account]) -> Account:
    """Find an account by key, alias or email; raise with the valid names if none match."""
    wanted = normalize(name)
    for account in accounts.values():
        names = (account.key, account.email, *account.aliases)
        if wanted in {normalize(n) for n in names}:
            return account
    valid = ", ".join(sorted(n for a in accounts.values() for n in (a.key, *a.aliases)))
    raise ValueError(f"Unknown account '{name}'. Use one of: {valid}.")
