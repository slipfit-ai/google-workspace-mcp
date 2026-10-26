"""One-time browser consent per account: `uv run gw-auth <account>`.

Runs Google's installed-app flow, checks the signed-in mailbox is the configured one, checks
every scope was granted, then stores the refresh token in 1Password.
"""

import sys

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from loguru import logger

from google_workspace_mcp import vault
from google_workspace_mcp.config import SCOPES, load_accounts, resolve


def authorise(name: str) -> None:
    """Run consent for one account and store its refresh token."""
    account = resolve(name, load_accounts())
    flow = InstalledAppFlow.from_client_config(vault.read_client_config(), SCOPES)
    logger.info(f"Opening browser consent for {account.key} ({account.email})")
    # prompt=consent guarantees a refresh token even if this client was approved before.
    credentials = flow.run_local_server(
        port=0, login_hint=account.email, prompt="consent", access_type="offline"
    )
    if not isinstance(credentials, Credentials):
        raise TypeError(f"Expected user credentials, got {type(credentials).__name__}")

    granted = set(credentials.granted_scopes or [])
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise SystemExit(
            f"Consent for {account.key} skipped {len(missing)} permission(s): {missing}. "
            f"Re-run `uv run gw-auth {account.key}` and tick every box on the consent screen."
        )

    gmail = build("gmail", "v1", credentials=credentials)
    signed_in = gmail.users().getProfile(userId="me").execute()["emailAddress"]
    if signed_in.lower() != account.email.lower():
        raise SystemExit(
            f"Signed in as {signed_in}, but '{account.key}' is configured as {account.email}. "
            f"Nothing was stored. Re-run and pick {account.email} in the account chooser."
        )

    if not credentials.refresh_token:
        raise SystemExit("Google returned no refresh token; nothing was stored. Re-run.")
    vault.write_refresh_token(account.key, account.email, credentials.refresh_token)
    logger.success(
        f"{account.key}: verified {signed_in}; refresh token stored in 1Password "
        f"({vault.VAULT}/{vault.token_item(account.key)})"
    )


def main() -> None:
    """CLI entry point: authorise each account named on the command line."""
    names = sys.argv[1:]
    if not names:
        accounts = ", ".join(load_accounts())
        raise SystemExit(f"Usage: gw-auth <account> [<account> ...]  (accounts: {accounts})")
    for name in names:
        authorise(name)
