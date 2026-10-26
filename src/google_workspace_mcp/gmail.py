"""Gmail tools: search, read, draft, label, archive, trash. There is deliberately no send."""

import base64
import html
from email.message import EmailMessage
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from google_workspace_mcp.app import app
from google_workspace_mcp.google import account, google_errors, service
from google_workspace_mcp.untrusted import fence, html_to_text

MAX_RESULTS = 50  # one batch request; larger batches get rate-limited
MAX_BODY_CHARS = 20_000
LIST_HEADERS = ["From", "To", "Subject", "Date"]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


def _gmail(name: str) -> tuple[Any, str]:
    """The Gmail client and resolved account key for a chat-style account name."""
    acct = account(name)
    return service(acct, "gmail", "v1"), acct.key


def _header(payload: dict[str, Any], name: str) -> str:
    """A message header's value, or '' if absent."""
    return next(
        (h["value"] for h in payload.get("headers", []) if h["name"].lower() == name.lower()),
        "",
    )


def _batch_get(svc: Any, requests: list[Any]) -> list[dict[str, Any]]:
    """Execute GET requests in one HTTP batch, returning responses in request order."""
    results: dict[str, dict[str, Any]] = {}

    def collect(request_id: str, response: dict[str, Any], exception: Exception | None) -> None:
        if exception is not None:
            raise exception
        results[request_id] = response

    batch = svc.new_batch_http_request(callback=collect)
    for i, request in enumerate(requests):
        batch.add(request, request_id=str(i))
    if requests:
        batch.execute()
    return [results[str(i)] for i in range(len(requests))]


def _decode(part: dict[str, Any]) -> str:
    """Decode a MIME part's base64url body using its declared charset."""
    data = part.get("body", {}).get("data", "")
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    content_type = _header(part, "Content-Type")
    charset = next(
        (p.split("=", 1)[1].strip("\"' ") for p in content_type.split(";") if "charset=" in p),
        "utf-8",
    )
    try:
        return raw.decode(charset, errors="replace").replace("\r\n", "\n")
    except LookupError:  # unknown charset name
        return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a MIME tree into its leaf parts."""
    children = part.get("parts")
    return [leaf for child in children for leaf in _walk(child)] if children else [part]


def _body_and_attachments(payload: dict[str, Any]) -> tuple[str, list[str]]:
    """A message's readable text (text/plain preferred, else visible HTML) and attachment names."""
    leaves = _walk(payload)
    attachments = [p["filename"] for p in leaves if p.get("filename")]
    inline = [p for p in leaves if not p.get("filename") and p.get("body", {}).get("data")]
    plain = [p for p in inline if p.get("mimeType") == "text/plain"]
    rich = [p for p in inline if p.get("mimeType") == "text/html"]
    if plain:
        text = "\n".join(_decode(p) for p in plain).strip()
    elif rich:
        text = "\n".join(html_to_text(_decode(p)) for p in rich)
    else:
        text = "(no text body)"
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"\n[... truncated at {MAX_BODY_CHARS} characters]"
    return text, attachments


def _summary(payload: dict[str, Any], snippet: str) -> str:
    """One listing entry: who, what, when, and the snippet."""
    lines = [f"  {h}: {_header(payload, h)}" for h in LIST_HEADERS if _header(payload, h)]
    return "\n".join([*lines, f"  Snippet: {html.unescape(snippet)}"])


def _label_ids(svc: Any, names: list[str]) -> list[str]:
    """Map label names (user labels or system ids like INBOX, STARRED) to label ids."""
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    by_name = {lab["name"].lower(): lab["id"] for lab in labels}
    by_name.update({lab["id"].lower(): lab["id"] for lab in labels})
    missing = [n for n in names if n.lower() not in by_name]
    if missing:
        raise ToolError(
            f"No label named {', '.join(missing)}. Use list_labels to see what exists; "
            "this server doesn't create labels."
        )
    return [by_name[n.lower()] for n in names]


@app.tool(annotations=READ)
@google_errors
def search_threads(
    account: str, query: str = "in:inbox", max_results: int = 20, page_token: str | None = None
) -> str:
    """Search an account's Gmail threads with Gmail search syntax (e.g. "from:bob is:unread",
    "newer_than:7d", "label:receipts"). Returns thread ids with sender, subject, date and
    snippet of each thread's latest message, plus a page token when more results exist."""
    svc, key = _gmail(account)
    listing = (
        svc.users()
        .threads()
        .list(userId="me", q=query, maxResults=min(max_results, MAX_RESULTS), pageToken=page_token)
        .execute()
    )
    ids = [t["id"] for t in listing.get("threads", [])]
    threads = _batch_get(
        svc,
        [
            svc.users()
            .threads()
            .get(userId="me", id=i, format="metadata", metadataHeaders=LIST_HEADERS)
            for i in ids
        ],
    )
    entries = [
        f"Thread {t['id']} ({len(t['messages'])} messages, labels: "
        f"{', '.join(t['messages'][-1].get('labelIds', []))})\n"
        + _summary(t["messages"][-1]["payload"], t["messages"][-1].get("snippet", ""))
        for t in threads
    ]
    more = listing.get("nextPageToken")
    footer = f"\n\nMore results: page_token={more}" if more else ""
    if not entries:
        return f"No threads in {key} match {query!r}."
    return fence(f"gmail:{key}:search", "\n\n".join(entries)) + footer


@app.tool(annotations=READ)
@google_errors
def get_thread(account: str, thread_id: str) -> str:
    """Read every message in a Gmail thread: headers, Gmail message id, labels, plain-text
    body and attachment names. Use a message id from here to draft a reply."""
    svc, key = _gmail(account)
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = []
    for m in thread["messages"]:
        payload = m["payload"]
        body, attachments = _body_and_attachments(payload)
        headers = [
            f"{h}: {_header(payload, h)}"
            for h in ["From", "To", "Cc", "Date", "Subject"]
            if _header(payload, h)
        ]
        messages.append(
            "\n".join(
                [
                    f"--- Message {m['id']} (labels: {', '.join(m.get('labelIds', []))})",
                    *headers,
                    *([f"Attachments: {', '.join(attachments)}"] if attachments else []),
                    "",
                    body,
                ]
            )
        )
    return fence(f"gmail:{key}:thread:{thread_id}", "\n\n".join(messages))


@app.tool(annotations=READ)
@google_errors
def list_drafts(account: str, max_results: int = 20) -> str:
    """List an account's Gmail drafts with recipients, subject and snippet."""
    svc, key = _gmail(account)
    listing = (
        svc.users().drafts().list(userId="me", maxResults=min(max_results, MAX_RESULTS)).execute()
    )
    drafts = _batch_get(
        svc,
        [
            svc.users().drafts().get(userId="me", id=d["id"], format="metadata")
            for d in listing.get("drafts", [])
        ],
    )
    if not drafts:
        return f"No drafts in {key}."
    entries = [
        f"Draft {d['id']} (thread {d['message']['threadId']})\n"
        + _summary(d["message"]["payload"], d["message"].get("snippet", ""))
        for d in drafts
    ]
    return fence(f"gmail:{key}:drafts", "\n\n".join(entries))


@app.tool(annotations=WRITE)
@google_errors
def create_draft(
    account: str,
    body: str,
    to: list[str] | None = None,
    subject: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to_message_id: str | None = None,
) -> str:
    """Save a plain-text draft in an account's Gmail. It is never sent: Josh reviews and
    sends it himself. To reply, pass reply_to_message_id (a message id from get_thread);
    the draft joins that thread, and `to` and `subject` default to the reply's."""
    svc, key = _gmail(account)
    msg = EmailMessage()
    thread_id = None
    if reply_to_message_id:
        original = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=reply_to_message_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "Reply-To", "Message-ID", "References"],
            )
            .execute()
        )
        payload = original["payload"]
        thread_id = original["threadId"]
        to = to or [_header(payload, "Reply-To") or _header(payload, "From")]
        original_subject = _header(payload, "Subject")
        subject = subject or (
            original_subject
            if original_subject.lower().startswith("re:")
            else f"Re: {original_subject}"
        )
        message_id = _header(payload, "Message-ID")
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"] = f"{_header(payload, 'References')} {message_id}".strip()
    for field, value in [("To", to), ("Cc", cc), ("Bcc", bcc)]:
        if value:
            msg[field] = ", ".join(value)
    msg["Subject"] = subject or ""
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    message: dict[str, str] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    draft = svc.users().drafts().create(userId="me", body={"message": message}).execute()
    return (
        f"Draft {draft['id']} saved in {key} (thread {draft['message']['threadId']}), "
        f"to: {msg['To'] or '(none)'}, subject: {msg['Subject']!r}. Not sent."
    )


@app.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@google_errors
def list_labels(account: str) -> list[dict[str, str]]:
    """List an account's Gmail labels (system and user) by name and id."""
    svc, _ = _gmail(account)
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    return sorted(
        ({"name": lab["name"], "id": lab["id"], "type": lab["type"]} for lab in labels),
        key=lambda lab: (lab["type"], lab["name"].lower()),
    )


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def modify_thread_labels(
    account: str,
    thread_id: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> str:
    """Add or remove labels on every message in a thread. Takes label names or system ids
    (STARRED, UNREAD, IMPORTANT, INBOX). Removing UNREAD marks the thread read."""
    svc, key = _gmail(account)
    body = {
        "addLabelIds": _label_ids(svc, add_labels or []),
        "removeLabelIds": _label_ids(svc, remove_labels or []),
    }
    svc.users().threads().modify(userId="me", id=thread_id, body=body).execute()
    return f"Thread {thread_id} in {key}: added {add_labels or []}, removed {remove_labels or []}."


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def archive_thread(account: str, thread_id: str) -> str:
    """Archive a thread: remove it from the inbox. It stays searchable under All Mail."""
    svc, key = _gmail(account)
    svc.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()
    return f"Thread {thread_id} archived in {key}."


@app.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@google_errors
def trash_thread(account: str, thread_id: str) -> str:
    """Move a thread to Trash. Recoverable with untrash_thread for 30 days; nothing is
    permanently deleted by this server."""
    svc, key = _gmail(account)
    svc.users().threads().trash(userId="me", id=thread_id).execute()
    return f"Thread {thread_id} moved to Trash in {key} (recoverable for 30 days)."


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def untrash_thread(account: str, thread_id: str, to_inbox: bool = True) -> str:
    """Restore a thread from Trash. Trashing strips the INBOX label and Gmail's untrash
    doesn't put it back, so this re-adds it unless to_inbox is false (then the thread
    lands in All Mail only, e.g. when it had been archived before it was trashed)."""
    svc, key = _gmail(account)
    svc.users().threads().untrash(userId="me", id=thread_id).execute()
    if to_inbox:
        svc.users().threads().modify(
            userId="me", id=thread_id, body={"addLabelIds": ["INBOX"]}
        ).execute()
    return (
        f"Thread {thread_id} restored from Trash in {key} to {'Inbox' if to_inbox else 'All Mail'}."
    )
