"""Google Calendar client + OAuth bootstrap."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dtparser
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import (
    CONFIG_DIR,
    CREDENTIALS_PATH,
    GOOGLE_SCOPES,
    SCHEDULER_TAG,
    Settings,
    TOKEN_PATH,
)


@dataclass(frozen=True)
class Expectation:
    """What we last left an event looking like.

    Stamped onto the event itself rather than kept on our side, which is what
    lets a later run notice that something else moved or renamed a block
    without reading any history at all. It therefore survives a deleted
    journal and works from a second machine.
    """

    start: datetime
    end: datetime
    # None for a stamp written before summaries were included, which reads as
    # "we don't know what we called it" rather than "it was renamed".
    summary: Optional[str] = None

    def as_private(self) -> dict[str, str]:
        out = {
            EXPECTED_START: self.start.astimezone(timezone.utc).isoformat(),
            EXPECTED_END: self.end.astimezone(timezone.utc).isoformat(),
        }
        if self.summary is not None:
            out[EXPECTED_SUMMARY] = self.summary
        return out


@dataclass
class CalEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    task_uuid: Optional[str]
    raw: dict

    @property
    def expectation(self) -> Optional[Expectation]:
        """Where we last left this block, or None if we never stamped it.

        Unstamped means "written by an older version": treated as unknown
        rather than as drift, so upgrading doesn't report a wave of hand-moves
        that never happened.
        """
        priv = (self.raw.get("extendedProperties") or {}).get("private") or {}
        start = _parse_stamp(priv.get(EXPECTED_START))
        end = _parse_stamp(priv.get(EXPECTED_END))
        if start is None or end is None:
            return None
        summary = priv.get(EXPECTED_SUMMARY)
        return Expectation(
            start=start,
            end=end,
            summary=summary if isinstance(summary, str) else None,
        )


# Field masks: keep responses small and avoid downloading attendee
# details, descriptions, conferenceData, etc. that we never read.
_BUSY_FIELDS = (
    "nextPageToken,"
    "items(id,status,transparency,start,end,"
    "attendees(self,responseStatus))"
)
_OUR_FIELDS = (
    "nextPageToken,"
    "items(id,summary,description,colorId,visibility,start,end,"
    "attendees,extendedProperties)"
)

# Private property names for the expectation stamp. Private properties are
# invisible to anyone we invite, and Google merges them on patch — but we
# re-send `scheduler` with every stamp anyway, because a merge that turned out
# to be a replace would make our own events unfindable.
EXPECTED_START = "expectedStart"
EXPECTED_END = "expectedEnd"
EXPECTED_SUMMARY = "expectedSummary"


def _write_secure(path, content: str) -> None:
    """Write `content` to `path` with mode 0600 (private)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
    except Exception:
        os.close(fd)
        raise


class AuthUnavailable(Exception):
    """Authorizing would need a browser, and the caller can't have one.

    Raised instead of opening an OAuth flow on a read-only path. A review run
    from cron must degrade to "the calendar could not be read" rather than
    block forever waiting for a browser nobody is looking at.
    """


def _ensure_credentials(*, allow_interactive: bool = True) -> Credentials:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass

    creds: Optional[Credentials] = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(
            str(TOKEN_PATH), GOOGLE_SCOPES
        )
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Refresh token revoked / expired (e.g. Desktop OAuth client
            # left in "Testing" expires tokens after 7 days). Discard
            # and fall through to the interactive flow.
            try:
                TOKEN_PATH.unlink()
            except OSError:
                pass
            creds = None
        else:
            _write_secure(TOKEN_PATH, creds.to_json())
            return creds

    # Everything from here needs a browser. A read-only path says so and lets
    # its caller carry on without the calendar.
    if not allow_interactive:
        raise AuthUnavailable(
            "not authorized, and authorizing needs a browser — "
            "run `task-gcal --setup` once"
        )

    if not CREDENTIALS_PATH.exists():
        raise SystemExit(
            f"Missing OAuth client secrets at {CREDENTIALS_PATH}.\n"
            "Create a Google Cloud OAuth client (type: Desktop), download "
            "the JSON, save it there, then re-run with `--setup`."
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH), GOOGLE_SCOPES
        )
    except (ValueError, json.JSONDecodeError) as e:
        raise SystemExit(
            f"{CREDENTIALS_PATH} is not a valid OAuth client secrets file: {e}\n"
            "Download the JSON for a Desktop OAuth client from\n"
            "https://console.cloud.google.com/apis/credentials and save\n"
            "the whole file (it should start with `{\"installed\": ...}`)."
        ) from None
    creds = flow.run_local_server(port=0)
    _write_secure(TOKEN_PATH, creds.to_json())
    return creds


def _parse_stamp(raw) -> Optional[datetime]:
    """Parse one of our own expectation stamps; None if absent or unreadable."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _parse_when(node: dict) -> Optional[datetime]:
    if "dateTime" not in node:
        return None
    return dtparser.isoparse(node["dateTime"]).astimezone(timezone.utc)


def _private_props(
    *, task_uuid: Optional[str], expect: Optional[Expectation]
) -> dict[str, str]:
    """Our private properties: the tags that make an event ours, plus a stamp."""
    props = {"scheduler": SCHEDULER_TAG}
    if task_uuid:
        props["taskUuid"] = task_uuid
    if expect is not None:
        props.update(expect.as_private())
    return props


def _is_declined_by_self(raw: dict) -> bool:
    """True if this event is one I declined (matches Google FB semantics)."""
    for att in raw.get("attendees", []) or []:
        if att.get("self") and att.get("responseStatus") == "declined":
            return True
    return False


class GCal:
    def __init__(self, settings: Settings, *, allow_interactive: bool = True) -> None:
        creds = _ensure_credentials(allow_interactive=allow_interactive)
        self._svc = build(
            "calendar", "v3", credentials=creds, cache_discovery=False
        )
        self._calendar_id = settings.calendar_id

    # ---------------------------- queries ------------------------------

    def list_scheduler_events(
        self, time_min: datetime, time_max: datetime
    ) -> list[CalEvent]:
        events: list[CalEvent] = []
        page_token = None
        while True:
            resp = (
                self._svc.events()
                .list(
                    calendarId=self._calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    showDeleted=False,
                    privateExtendedProperty=f"scheduler={SCHEDULER_TAG}",
                    maxResults=2500,
                    pageToken=page_token,
                    fields=_OUR_FIELDS,
                )
                .execute(num_retries=3)
            )
            for raw in resp.get("items", []):
                start = _parse_when(raw.get("start", {}))
                end = _parse_when(raw.get("end", {}))
                if not start or not end:
                    continue
                priv = (raw.get("extendedProperties") or {}).get("private") or {}
                events.append(
                    CalEvent(
                        id=raw["id"],
                        summary=raw.get("summary", ""),
                        start=start,
                        end=end,
                        task_uuid=priv.get("taskUuid"),
                        raw=raw,
                    )
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return events

    def list_busy_events(
        self,
        time_min: datetime,
        time_max: datetime,
        exclude_event_ids: set[str],
    ) -> list[tuple[datetime, datetime]]:
        busy: list[tuple[datetime, datetime]] = []
        page_token = None
        while True:
            resp = (
                self._svc.events()
                .list(
                    calendarId=self._calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    showDeleted=False,
                    maxResults=2500,
                    pageToken=page_token,
                    fields=_BUSY_FIELDS,
                )
                .execute(num_retries=3)
            )
            for raw in resp.get("items", []):
                if raw.get("id") in exclude_event_ids:
                    continue
                if raw.get("status") == "cancelled":
                    continue
                if raw.get("transparency") == "transparent":
                    continue
                if _is_declined_by_self(raw):
                    continue
                start = _parse_when(raw.get("start", {}))
                end = _parse_when(raw.get("end", {}))
                if not start or not end:
                    continue
                busy.append((start, end))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        busy.sort()
        return busy

    # ---------------------------- mutations ----------------------------

    def create_event(
        self,
        *,
        task_uuid: str,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        color_id: str,
        visibility: str = "private",
        attendees: tuple[str, ...] = (),
    ) -> str:
        body = {
            "summary": summary,
            "description": description,
            "colorId": color_id,
            "visibility": visibility,
            "start": {"dateTime": start.astimezone(timezone.utc).isoformat()},
            "end": {"dateTime": end.astimezone(timezone.utc).isoformat()},
            "extendedProperties": {
                "private": _private_props(
                    task_uuid=task_uuid,
                    expect=Expectation(start=start, end=end, summary=summary),
                )
            },
        }
        kwargs: dict = {}
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]
            # Email the invitees; without this the API adds them silently.
            kwargs["sendUpdates"] = "all"
        created = (
            self._svc.events()
            .insert(calendarId=self._calendar_id, body=body, **kwargs)
            .execute(num_retries=3)
        )
        return created["id"]

    def patch_event(
        self,
        event_id: str,
        *,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        color_id: Optional[str] = None,
        visibility: Optional[str] = None,
        attendees: Optional[list[dict]] = None,
        expect: Optional[Expectation] = None,
        task_uuid: Optional[str] = None,
    ) -> bool:
        """Patch an event. Returns False if the event was already gone.

        `attendees`, when given, is the full desired attendee list (the
        API replaces the array wholesale); pass existing + new to add
        people without dropping anyone. Supplying it emails the invitees.

        `expect` re-stamps what we're leaving behind, and must be given
        whenever the time or summary is written — otherwise the stamp would
        still describe the old position and the next run would read our own
        edit as someone else's.
        """
        body: dict = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if start is not None:
            body["start"] = {"dateTime": start.astimezone(timezone.utc).isoformat()}
        if end is not None:
            body["end"] = {"dateTime": end.astimezone(timezone.utc).isoformat()}
        if expect is not None:
            body["extendedProperties"] = {
                "private": _private_props(task_uuid=task_uuid, expect=expect)
            }
        if color_id is not None:
            body["colorId"] = color_id
        if visibility is not None:
            body["visibility"] = visibility
        kwargs: dict = {}
        if attendees is not None:
            body["attendees"] = attendees
            kwargs["sendUpdates"] = "all"
        if not body:
            return True
        try:
            self._svc.events().patch(
                calendarId=self._calendar_id,
                eventId=event_id,
                body=body,
                **kwargs,
            ).execute(num_retries=3)
            return True
        except HttpError as e:
            if e.resp.status in (404, 410):
                return False
            raise

    def adopt_position(self, event: CalEvent, *, summary: str) -> bool:
        """Re-stamp our expectation to where the event now is.

        Called after we notice someone moved a block and decide to let it
        stay. The stamp is what makes drift a one-off observation rather than
        a permanent one: without this, the same hand-move would be reported
        again on every run for as long as the block existed.
        """
        return self.patch_event(
            event.id,
            expect=Expectation(
                start=event.start, end=event.end, summary=summary
            ),
            task_uuid=event.task_uuid,
        )

    def delete_event(self, event_id: str) -> bool:
        """Delete; treat 404/410 as success (already gone)."""
        try:
            self._svc.events().delete(
                calendarId=self._calendar_id, eventId=event_id
            ).execute(num_retries=3)
            return True
        except HttpError as e:
            if e.resp.status in (404, 410):
                return True
            raise
