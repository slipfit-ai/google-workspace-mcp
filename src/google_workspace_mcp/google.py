"""Google credentials and API clients, one set per account, loaded lazily from 1Password."""

import functools
import threading
from collections.abc import Callable
from typing import Any

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger
from mcp.server.mcpserver.exceptions import ToolError

from google_workspace_mcp import vault
from google_workspace_mcp.config import PROJECT_ROOT, SCOPES, Account, load_accounts, resolve

_lock = threading.Lock()
_client: dict[str, Any] = {}
_credentials: dict[str, Credentials] = {}


def accounts() -> dict[str, Account]:
    """The configured accounts (re-read each call so config edits apply without a restart)."""
    return load_accounts()


def account(name: str) -> Account:
    """Resolve a chat-style account name, as a ToolError Claude can read if unknown."""
    try:
        return resolve(name, accounts())
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def credentials(acct: Account) -> Credentials:
    """Return an account's credentials, reading 1Password once per server process.

    The lock stops parallel first calls from each triggering a Touch ID prompt.
    """
    with _lock:
        if acct.key not in _credentials:
            if not _client:
                _client.update(vault.read_client_config()["installed"])
            logger.info(f"Loading refresh token for {acct.key} from 1Password")
            _credentials[acct.key] = Credentials(
                token=None,
                refresh_token=vault.read_refresh_token(acct.key),
                token_uri=_client["token_uri"],
                client_id=_client["client_id"],
                client_secret=_client["client_secret"],
                scopes=SCOPES,
            )
        return _credentials[acct.key]


def service(acct: Account, api: str, version: str) -> Any:
    """Build an API client. Built per call because httplib2 clients aren't thread-safe."""
    return build(api, version, credentials=credentials(acct), cache_discovery=False)


def reauth_hint(acct: Account) -> str:
    """The exact command that fixes an expired or revoked token."""
    return f"cd {PROJECT_ROOT} && uv run gw-auth {acct.key}"


def google_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Turn Google and 1Password failures into ToolErrors whose text reaches Claude.

    mcp 2.x hides the message of any exception that isn't a ToolError.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except RefreshError as exc:
            name = kwargs.get("account", "?")
            acct = account(str(name))
            logger.error(f"Refresh failed for {acct.key}: {exc}")
            raise ToolError(
                f"Google rejected the stored token for '{acct.key}' (expired or revoked). "
                f"Ask Josh to run: {reauth_hint(acct)}"
            ) from exc
        except HttpError as exc:
            logger.error(f"{fn.__name__}: Google API {exc.status_code}: {exc.reason}")
            raise ToolError(f"Google API error {exc.status_code}: {exc.reason}") from exc
        except RuntimeError as exc:  # raised by vault._op with op's own message
            logger.error(f"{fn.__name__}: {exc}")
            raise ToolError(f"{exc}. Is the 1Password app unlocked?") from exc

    return wrapper
