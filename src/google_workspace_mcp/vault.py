"""1Password access through the `op` CLI.

Secrets never touch disk: values go to `op` on stdin and come back on stdout. With the
1Password desktop app integration on, the first call in a session prompts Touch ID.
"""

import json
import os
import subprocess

VAULT = os.environ.get("GWMCP_VAULT", "MCP-Google")
CLIENT_DOCUMENT = "google-oauth-client"


def token_item(account_key: str) -> str:
    """1Password item title holding an account's refresh token."""
    return f"google-{account_key}"


def _op(*args: str, stdin: str | None = None) -> str:
    """Run `op` and return stdout; raise with op's own message on failure."""
    result = subprocess.run(["op", *args], input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"`op {args[0]} {args[1]}` failed: {result.stderr.strip()}")
    return result.stdout


def read_client_config() -> dict:
    """Return the OAuth client JSON (the file downloaded from Google Cloud)."""
    return json.loads(_op("document", "get", CLIENT_DOCUMENT, "--vault", VAULT))


def read_refresh_token(account_key: str) -> str:
    """Return an account's refresh token."""
    return _op("read", f"op://{VAULT}/{token_item(account_key)}/credential").strip()


def item_exists(title: str) -> bool:
    """True if an item with this title is in the vault."""
    result = subprocess.run(
        ["op", "item", "get", title, "--vault", VAULT, "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def write_refresh_token(account_key: str, email: str, refresh_token: str) -> None:
    """Create or replace an account's refresh-token item, passing the secret on stdin."""
    title = token_item(account_key)
    template = json.dumps(
        {
            "title": title,
            "category": "API_CREDENTIAL",
            "fields": [
                {"id": "username", "type": "STRING", "label": "username", "value": email},
                {
                    "id": "credential",
                    "type": "CONCEALED",
                    "label": "credential",
                    "value": refresh_token,
                },
            ],
        }
    )
    if item_exists(title):
        _op("item", "edit", title, "--vault", VAULT, "-", stdin=template)
    else:
        _op("item", "create", "--vault", VAULT, "-", stdin=template)
