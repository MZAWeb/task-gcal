"""The Google Calendar client: request bodies, paging, and busy-time semantics.

Auth and the discovery build are stubbed; what's under test is the shape of
what we send and how we read what comes back.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from googleapiclient.errors import HttpError

from task_gcal import gcal as gcal_mod
from task_gcal.config import SCHEDULER_TAG, Settings
from task_gcal.gcal import GCal

UTC = timezone.utc


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 7, hour, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# A minimal stand-in for the discovery-built service object
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, result, error: HttpError | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self, num_retries=0):
        if self._error is not None:
            raise self._error
        return self._result


class FakeEvents:
    def __init__(self) -> None:
        self.list_pages: list[dict] = []
        self.list_kwargs: list[dict] = []
        self.insert_kwargs: list[dict] = []
        self.patch_kwargs: list[dict] = []
        self.delete_kwargs: list[dict] = []
        self.patch_error: HttpError | None = None
        self.delete_error: HttpError | None = None

    def list(self, **kwargs):
        self.list_kwargs.append(kwargs)
        page = self.list_pages[len(self.list_kwargs) - 1]
        return FakeRequest(page)

    def insert(self, **kwargs):
        self.insert_kwargs.append(kwargs)
        return FakeRequest({"id": "new-id"})

    def patch(self, **kwargs):
        self.patch_kwargs.append(kwargs)
        return FakeRequest({}, self.patch_error)

    def delete(self, **kwargs):
        self.delete_kwargs.append(kwargs)
        return FakeRequest({}, self.delete_error)


class FakeService:
    def __init__(self, events: FakeEvents) -> None:
        self._events = events

    def events(self):
        return self._events


def http_error(status: int) -> HttpError:
    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "nope"

    return HttpError(_Resp(status), b"{}")


@pytest.fixture
def client(monkeypatch):
    events = FakeEvents()
    monkeypatch.setattr(
        gcal_mod, "_ensure_credentials", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        gcal_mod, "build", lambda *a, **kw: FakeService(events)
    )
    api = GCal(Settings(calendar_id="cal-1"))
    api.events = events  # type: ignore[attr-defined]
    return api


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------

def test_created_events_carry_the_scheduler_tag_and_task_uuid(client):
    client.create_event(
        task_uuid="u1",
        summary="s",
        description="d",
        start=dt(9),
        end=dt(10),
        color_id="9",
    )
    body = client.events.insert_kwargs[0]["body"]
    private = body["extendedProperties"]["private"]
    assert private == {"scheduler": SCHEDULER_TAG, "taskUuid": "u1"}


def test_created_events_are_private_and_timed(client):
    client.create_event(
        task_uuid="u1",
        summary="s",
        description="d",
        start=dt(9),
        end=dt(10),
        color_id="9",
    )
    body = client.events.insert_kwargs[0]["body"]
    assert body["visibility"] == "private"
    assert body["start"] == {"dateTime": "2026-09-07T09:00:00+00:00"}
    assert "date" not in body["start"]


def test_create_goes_to_the_configured_calendar(client):
    client.create_event(
        task_uuid="u1", summary="s", description="d", start=dt(9), end=dt(10),
        color_id="9",
    )
    assert client.events.insert_kwargs[0]["calendarId"] == "cal-1"


def test_no_attendees_means_no_notification_emails(client):
    client.create_event(
        task_uuid="u1", summary="s", description="d", start=dt(9), end=dt(10),
        color_id="9",
    )
    assert "sendUpdates" not in client.events.insert_kwargs[0]
    assert "attendees" not in client.events.insert_kwargs[0]["body"]


def test_attendees_are_emailed_when_present(client):
    client.create_event(
        task_uuid="u1", summary="s", description="d", start=dt(9), end=dt(10),
        color_id="9", attendees=("a@b.com",),
    )
    call = client.events.insert_kwargs[0]
    assert call["body"]["attendees"] == [{"email": "a@b.com"}]
    assert call["sendUpdates"] == "all"


# ---------------------------------------------------------------------------
# patch_event
# ---------------------------------------------------------------------------

def test_patch_sends_only_the_given_fields(client):
    client.patch_event("ev1", summary="new")
    assert client.events.patch_kwargs[0]["body"] == {"summary": "new"}


def test_patch_with_nothing_to_change_makes_no_call(client):
    assert client.patch_event("ev1") is True
    assert client.events.patch_kwargs == []


@pytest.mark.parametrize("status", [404, 410])
def test_patch_reports_a_vanished_event_rather_than_raising(client, status):
    client.events.patch_error = http_error(status)
    assert client.patch_event("ev1", summary="new") is False


def test_patch_reraises_other_errors(client):
    client.events.patch_error = http_error(500)
    with pytest.raises(HttpError):
        client.patch_event("ev1", summary="new")


def test_patching_attendees_emails_them(client):
    client.patch_event("ev1", attendees=[{"email": "a@b.com"}])
    assert client.events.patch_kwargs[0]["sendUpdates"] == "all"


# ---------------------------------------------------------------------------
# delete_event
# ---------------------------------------------------------------------------

def test_delete_targets_the_right_event(client):
    client.delete_event("ev1")
    assert client.events.delete_kwargs[0]["eventId"] == "ev1"


@pytest.mark.parametrize("status", [404, 410])
def test_deleting_something_already_gone_is_success(client, status):
    client.events.delete_error = http_error(status)
    assert client.delete_event("ev1") is True


def test_delete_reraises_other_errors(client):
    client.events.delete_error = http_error(403)
    with pytest.raises(HttpError):
        client.delete_event("ev1")


# ---------------------------------------------------------------------------
# list_scheduler_events
# ---------------------------------------------------------------------------

def _our_event(**over):
    raw = {
        "id": "ev1",
        "summary": "a task",
        "start": {"dateTime": "2026-09-07T09:00:00Z"},
        "end": {"dateTime": "2026-09-07T10:00:00Z"},
        "extendedProperties": {"private": {"scheduler": SCHEDULER_TAG, "taskUuid": "u1"}},
    }
    raw.update(over)
    return raw


def test_our_events_are_filtered_by_the_scheduler_tag(client):
    client.events.list_pages = [{"items": []}]
    client.list_scheduler_events(dt(0), dt(23))
    assert (
        client.events.list_kwargs[0]["privateExtendedProperty"]
        == f"scheduler={SCHEDULER_TAG}"
    )


def test_recurring_events_are_expanded(client):
    client.events.list_pages = [{"items": []}]
    client.list_scheduler_events(dt(0), dt(23))
    assert client.events.list_kwargs[0]["singleEvents"] is True


def test_the_task_uuid_is_read_off_the_event(client):
    client.events.list_pages = [{"items": [_our_event()]}]
    (ev,) = client.list_scheduler_events(dt(0), dt(23))
    assert ev.task_uuid == "u1"
    assert ev.start == dt(9)
    assert ev.end == dt(10)
    assert ev.summary == "a task"


def test_all_day_managed_events_are_skipped(client):
    client.events.list_pages = [
        {"items": [_our_event(start={"date": "2026-09-07"}, end={"date": "2026-09-08"})]}
    ]
    assert client.list_scheduler_events(dt(0), dt(23)) == []


def test_paging_follows_next_page_token(client):
    client.events.list_pages = [
        {"items": [_our_event(id="ev1")], "nextPageToken": "tok"},
        {"items": [_our_event(id="ev2")]},
    ]
    got = client.list_scheduler_events(dt(0), dt(23))
    assert [e.id for e in got] == ["ev1", "ev2"]
    assert client.events.list_kwargs[1]["pageToken"] == "tok"


# ---------------------------------------------------------------------------
# list_busy_events — Google's own free/busy semantics
# ---------------------------------------------------------------------------

def _busy_event(**over):
    raw = {
        "id": "m1",
        "start": {"dateTime": "2026-09-07T11:00:00Z"},
        "end": {"dateTime": "2026-09-07T12:00:00Z"},
    }
    raw.update(over)
    return raw


def test_a_normal_meeting_is_busy(client):
    client.events.list_pages = [{"items": [_busy_event()]}]
    assert client.list_busy_events(dt(0), dt(23), set()) == [(dt(11), dt(12))]


def test_our_own_events_are_excluded_by_id(client):
    client.events.list_pages = [{"items": [_busy_event(id="ours")]}]
    assert client.list_busy_events(dt(0), dt(23), {"ours"}) == []


def test_cancelled_events_are_not_busy(client):
    client.events.list_pages = [{"items": [_busy_event(status="cancelled")]}]
    assert client.list_busy_events(dt(0), dt(23), set()) == []


def test_free_transparency_events_are_not_busy(client):
    client.events.list_pages = [{"items": [_busy_event(transparency="transparent")]}]
    assert client.list_busy_events(dt(0), dt(23), set()) == []


def test_all_day_events_are_not_busy(client):
    client.events.list_pages = [
        {"items": [_busy_event(start={"date": "2026-09-07"}, end={"date": "2026-09-08"})]}
    ]
    assert client.list_busy_events(dt(0), dt(23), set()) == []


def test_meetings_i_declined_are_not_busy(client):
    client.events.list_pages = [
        {
            "items": [
                _busy_event(
                    attendees=[{"self": True, "responseStatus": "declined"}]
                )
            ]
        }
    ]
    assert client.list_busy_events(dt(0), dt(23), set()) == []


def test_a_meeting_someone_else_declined_is_still_busy(client):
    client.events.list_pages = [
        {
            "items": [
                _busy_event(
                    attendees=[
                        {"self": False, "responseStatus": "declined"},
                        {"self": True, "responseStatus": "accepted"},
                    ]
                )
            ]
        }
    ]
    assert client.list_busy_events(dt(0), dt(23), set()) == [(dt(11), dt(12))]


def test_busy_intervals_come_back_sorted(client):
    client.events.list_pages = [
        {
            "items": [
                _busy_event(id="late", start={"dateTime": "2026-09-07T15:00:00Z"},
                            end={"dateTime": "2026-09-07T16:00:00Z"}),
                _busy_event(id="early"),
            ]
        }
    ]
    got = client.list_busy_events(dt(0), dt(23), set())
    assert got == sorted(got)
