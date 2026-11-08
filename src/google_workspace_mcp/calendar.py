"""Calendar tools. Guests are out of reach by design: no tool sets attendees, and events
that already have guests, or that another calendar organizes, can't be changed or deleted."""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from google_workspace_mcp.app import app
from google_workspace_mcp.google import account, google_errors, service
from google_workspace_mcp.untrusted import fence

MAX_RESULTS = 250

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _calendar(name: str) -> tuple[Any, str]:
    """The Calendar client and resolved account key for a chat-style account name."""
    acct = account(name)
    return service(acct, "calendar", "v3"), acct.key


def _parse(value: str) -> datetime:
    """Parse an ISO 8601 date or datetime, as a ToolError Claude can read if malformed."""
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            f"Can't read {value!r} as a date or time. Use ISO 8601, e.g. 2026-10-27 for an "
            "all-day event or 2026-10-27T16:30:00 for a timed one."
        ) from exc


def _calendar_zone(svc: Any, calendar_id: str) -> str:
    """The calendar's own time zone, used for times given without an offset."""
    return svc.calendarList().get(calendarId=calendar_id).execute()["timeZone"]


def _when(svc: Any, calendar_id: str, value: str, time_zone: str | None) -> dict[str, str]:
    """An event start/end: a bare date is all-day; a naive time uses the given or calendar zone."""
    parsed = _parse(value)
    if len(value) == 10:
        return {"date": parsed.date().isoformat()}
    if parsed.tzinfo is not None:
        return {"dateTime": parsed.isoformat()}
    return {
        "dateTime": parsed.isoformat(),
        "timeZone": time_zone or _calendar_zone(svc, calendar_id),
    }


def _bound(svc: Any, calendar_id: str, value: str) -> str:
    """An RFC 3339 instant for list filters; naive values are read in the calendar's zone."""
    parsed = _parse(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(_calendar_zone(svc, calendar_id)))
    return parsed.isoformat()


def _time(edge: dict[str, str]) -> str:
    """Render an event start or end."""
    return edge.get("dateTime") or f"{edge.get('date')} (all day)"


def _describe(event: dict[str, Any], full: bool = False) -> str:
    """Event details as text. `full` adds description and the guest list."""
    attendees = event.get("attendees", [])
    lines = [
        f"Event {event['id']}: {event.get('summary', '(no title)')}",
        f"  When: {_time(event['start'])} → {_time(event['end'])}",
        *([f"  Where: {event['location']}"] if event.get("location") else []),
        f"  Organizer: {event.get('organizer', {}).get('email', '?')}"
        + (" (this calendar)" if event.get("organizer", {}).get("self") else ""),
        f"  Guests: {len(attendees)}",
        *(
            [f"  Recurring: instance of {event['recurringEventId']}"]
            if "recurringEventId" in event
            else []
        ),
    ]
    if full:
        lines += [f"    - {a['email']} ({a.get('responseStatus', '?')})" for a in attendees]
        lines += [f"  Link: {event.get('htmlLink', '')}"]
        if event.get("description"):
            lines += ["  Description:", event["description"]]
    return "\n".join(lines)


def _editable(svc: Any, calendar_id: str, event_id: str, verb: str) -> dict[str, Any]:
    """Fetch an event and refuse if it has guests or another calendar organizes it."""
    event = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    if event.get("attendees"):
        raise ToolError(
            f"Refused to {verb} {event.get('summary', event_id)!r}: it has "
            f"{len(event['attendees'])} guests, and this server never changes events other "
            "people are invited to. Josh can do it in Google Calendar."
        )
    if not event.get("organizer", {}).get("self"):
        raise ToolError(
            f"Refused to {verb} {event.get('summary', event_id)!r}: it is organized by "
            f"{event.get('organizer', {}).get('email', 'another calendar')}, not this calendar."
        )
    return event


@app.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@google_errors
def list_calendars(account: str) -> list[dict[str, Any]]:
    """List the calendars an account can see, with ids, access role and time zone."""
    svc, _ = _calendar(account)
    items = svc.calendarList().list().execute().get("items", [])
    return [
        {
            "id": c["id"],
            "name": c.get("summaryOverride") or c.get("summary", ""),
            "access_role": c["accessRole"],
            "primary": c.get("primary", False),
            "time_zone": c.get("timeZone", ""),
        }
        for c in items
    ]


@app.tool(annotations=READ)
@google_errors
def list_events(
    account: str,
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
    max_results: int = 50,
) -> str:
    """List events in time order, recurring events expanded. time_min/time_max are ISO 8601
    (default: now to 7 days out); times without an offset use the calendar's zone. `query`
    is free text matched against title, description, location and guests."""
    svc, key = _calendar(account)
    start = _bound(svc, calendar_id, time_min) if time_min else datetime.now(UTC).isoformat()
    end = (
        _bound(svc, calendar_id, time_max)
        if time_max
        else (datetime.fromisoformat(start) + timedelta(days=7)).isoformat()
    )
    events = (
        svc.events()
        .list(
            calendarId=calendar_id,
            timeMin=start,
            timeMax=end,
            q=query,
            singleEvents=True,
            orderBy="startTime",
            maxResults=min(max_results, MAX_RESULTS),
        )
        .execute()
        .get("items", [])
    )
    if not events:
        return f"No events on {calendar_id} in {key} between {start} and {end}."
    return fence(f"calendar:{key}:{calendar_id}", "\n\n".join(_describe(e) for e in events))


@app.tool(annotations=READ)
@google_errors
def get_event(account: str, event_id: str, calendar_id: str = "primary") -> str:
    """Read one event in full: time, place, organizer, guests and their responses, description."""
    svc, key = _calendar(account)
    event = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    return fence(f"calendar:{key}:{calendar_id}:{event_id}", _describe(event, full=True))


@app.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
@google_errors
def create_event(
    account: str,
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str | None = None,
    location: str | None = None,
    time_zone: str | None = None,
) -> str:
    """Create an event with no guests (inviting people isn't possible through this server).
    start/end are ISO 8601: a date (2026-10-27) makes an all-day event, end exclusive; a
    time without an offset uses time_zone (IANA, e.g. America/Phoenix) or the calendar's."""
    svc, key = _calendar(account)
    body: dict[str, Any] = {
        "summary": summary,
        "start": _when(svc, calendar_id, start, time_zone),
        "end": _when(svc, calendar_id, end, time_zone),
    }
    body.update({k: v for k, v in [("description", description), ("location", location)] if v})
    event = svc.events().insert(calendarId=calendar_id, body=body, sendUpdates="none").execute()
    return f"Created in {key}:\n{_describe(event)}\n  Link: {event.get('htmlLink', '')}"


@app.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
@google_errors
def update_event(
    account: str,
    event_id: str,
    calendar_id: str = "primary",
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    time_zone: str | None = None,
) -> str:
    """Change an event's title, time, description or location; omitted fields are kept.
    Refused for events with guests or organized by another calendar. For a recurring event,
    an instance id changes that occurrence only."""
    svc, key = _calendar(account)
    _editable(svc, calendar_id, event_id, "update")
    patch: dict[str, Any] = {
        k: v
        for k, v in [("summary", summary), ("description", description), ("location", location)]
        if v is not None
    }
    if start:
        patch["start"] = _when(svc, calendar_id, start, time_zone)
    if end:
        patch["end"] = _when(svc, calendar_id, end, time_zone)
    if not patch:
        raise ToolError("Nothing to update: pass at least one field to change.")
    event = (
        svc.events()
        .patch(calendarId=calendar_id, eventId=event_id, body=patch, sendUpdates="none")
        .execute()
    )
    return f"Updated in {key}:\n{_describe(event)}"


@app.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
@google_errors
def delete_event(account: str, event_id: str, calendar_id: str = "primary") -> str:
    """Delete an event. It goes to Google Calendar's Trash (restorable there for 30 days).
    Refused for events with guests or organized by another calendar."""
    svc, key = _calendar(account)
    event = _editable(svc, calendar_id, event_id, "delete")
    svc.events().delete(calendarId=calendar_id, eventId=event_id, sendUpdates="none").execute()
    return (
        f"Deleted {event.get('summary', event_id)!r} from {key} (restorable from Calendar Trash)."
    )
