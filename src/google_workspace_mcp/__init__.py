"""Local MCP server: guarded Gmail, Calendar and Drive access across several Google accounts."""


def main() -> None:
    """Run the server over stdio. Logs go to stderr; stdout carries the protocol."""
    from google_workspace_mcp import calendar, content, drive, gmail  # noqa: F401  (registers the tools)
    from google_workspace_mcp.app import app

    app.run()
