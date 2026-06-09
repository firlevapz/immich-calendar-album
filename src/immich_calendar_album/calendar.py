"""CalDAV calendar client with plain-ICS fallback and event description write-back.

Strategy
--------
1. Attempt a CalDAV REPORT query (caldav library) for the requested date range.
2. If the server does not speak CalDAV (or the URL ends with ".ics"), fall back
   to a plain HTTP GET of the URL and parse the whole file with *icalendar*,
   then filter events by date in Python.

Write-back
----------
When CalDAV write credentials are available the client can append an Immich
share URL to a calendar event's DESCRIPTION field and PUT the modified VEVENT
back to the server.  The operation is idempotent: if the share URL is already
present in the description the PUT is skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import caldav
import httpx
import vobject
from icalendar import Calendar, Event
from icalendar import vDatetime as iCalDatetime
from recurring_ical_events import of as ical_events_of

if TYPE_CHECKING:
    from immich_calendar_album.config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalendarEvent:
    """A single calendar event relevant for album creation."""

    uid: str
    summary: str
    start: date  # always a plain date (time stripped); tz-normalised
    href: str | None  # CalDAV object URL, None when obtained via plain ICS fetch


class CalendarClient:
    """Fetch events and optionally write back Immich share links."""

    def __init__(self, cfg: "Config") -> None:
        self._cfg = cfg
        self._tz = ZoneInfo(cfg.tz)
        self._caldav_client: caldav.DAVClient | None = None
        self._calendar: caldav.Calendar | None = None
        self._use_fallback = str(cfg.caldav_base_url).lower().endswith(".ics")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_events(self) -> list[CalendarEvent]:
        """Return all events in [today - lookback_days, today] with a non-empty SUMMARY."""
        now_local = datetime.now(tz=self._tz)
        start_dt = now_local - timedelta(days=self._cfg.lookback_days)
        end_dt = now_local

        if not self._use_fallback:
            events = self._fetch_via_caldav(start_dt, end_dt)
            if events is None:
                log.warning(
                    "CalDAV REPORT failed – falling back to plain ICS fetch"
                )
                self._use_fallback = True

        if self._use_fallback:
            events = self._fetch_via_ics(start_dt, end_dt)

        return events or []

    def write_back_share_link(self, event: CalendarEvent, share_url: str) -> bool:
        """Append *share_url* to the event's DESCRIPTION and PUT it back.

        Returns True when the event was actually updated, False when the link
        was already present or the write could not be performed.
        """
        if not self._cfg.caldav_write_enabled:
            return False

        if event.href is None:
            log.debug(
                "Event %r has no CalDAV href – cannot write back share link",
                event.summary,
            )
            return False

        try:
            raw = self._get_event_raw(event.href)
        except Exception as exc:
            log.warning("Failed to fetch event %r for write-back: %s", event.href, exc)
            return False

        if share_url in raw:
            log.debug("Share URL already present in event %r – skipping", event.summary)
            return False

        updated = self._append_to_description(raw, share_url)
        if updated is None:
            return False

        try:
            self._put_event_raw(event.href, updated)
        except Exception as exc:
            log.warning(
                "Failed to write back share link to event %r: %s", event.summary, exc
            )
            return False

        log.info("Wrote Immich share link back to calendar event %r", event.summary)
        return True

    # ------------------------------------------------------------------
    # CalDAV path
    # ------------------------------------------------------------------

    def _ensure_caldav_client(self) -> bool:
        """Initialise the caldav client / calendar. Returns False on failure."""
        if self._calendar is not None:
            return True
        try:
            kwargs: dict = {"url": self._cfg.caldav_base_url}
            if self._cfg.caldav_api_key:
                kwargs["headers"] = {"Authorization": f"Bearer {self._cfg.caldav_api_key}"}
            elif self._cfg.caldav_username:
                kwargs["username"] = self._cfg.caldav_username
                kwargs["password"] = self._cfg.caldav_password

            client = caldav.DAVClient(**kwargs)
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                log.warning("CalDAV principal has no calendars")
                return False

            # If the URL points directly to a calendar use that; otherwise pick first.
            url_str = self._cfg.caldav_base_url
            for cal in calendars:
                if str(cal.url).rstrip("/") == url_str.rstrip("/"):
                    self._calendar = cal
                    break
            else:
                self._calendar = calendars[0]

            self._caldav_client = client
            return True
        except Exception as exc:
            log.debug("CalDAV init error: %s", exc)
            return False

    def _fetch_via_caldav(
        self,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent] | None:
        if not self._ensure_caldav_client():
            return None
        try:
            assert self._calendar is not None
            results = self._calendar.date_search(
                start=start, end=end, expand=True
            )
        except Exception as exc:
            log.debug("CalDAV date_search error: %s", exc)
            return None

        events: list[CalendarEvent] = []
        for obj in results:
            try:
                obj.load()
                cal = Calendar.from_ical(obj.data)
                for component in cal.walk("VEVENT"):
                    ev = self._component_to_event(component, href=str(obj.url))
                    if ev is not None:
                        events.append(ev)
            except Exception as exc:
                log.debug("Error parsing CalDAV event: %s", exc)
        return events

    # ------------------------------------------------------------------
    # Plain ICS path
    # ------------------------------------------------------------------

    def _fetch_via_ics(
        self,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        url = self._cfg.caldav_base_url
        headers: dict[str, str] = {}
        if self._cfg.caldav_api_key:
            headers["Authorization"] = f"Bearer {self._cfg.caldav_api_key}"

        auth = None
        if self._cfg.caldav_username:
            auth = (self._cfg.caldav_username, self._cfg.caldav_password)

        try:
            with httpx.Client(follow_redirects=True) as client:
                resp = client.get(url, headers=headers, auth=auth, timeout=30)
                resp.raise_for_status()
                raw = resp.text
        except Exception as exc:
            log.error("Failed to fetch ICS from %s: %s", url, exc)
            return []

        try:
            cal = Calendar.from_ical(raw)
        except Exception as exc:
            log.error("Failed to parse ICS data: %s", exc)
            return []

        # recurring_ical_events handles RRULE/EXDATE expansion and returns
        # plain Event objects filtered by the given window.
        try:
            occurrences = ical_events_of(cal).between(
                start.date(), (end + timedelta(days=1)).date()
            )
        except Exception as exc:
            log.warning("recurring_ical_events error, falling back to walk: %s", exc)
            occurrences = list(cal.walk("VEVENT"))

        events: list[CalendarEvent] = []
        for component in occurrences:
            ev = self._component_to_event(component, href=None)
            if ev is not None:
                events.append(ev)
        return events

    # ------------------------------------------------------------------
    # Write-back helpers
    # ------------------------------------------------------------------

    def _http_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "text/calendar; charset=utf-8"}
        if self._cfg.caldav_api_key:
            headers["Authorization"] = f"Bearer {self._cfg.caldav_api_key}"
        return headers

    def _http_auth(self):
        if self._cfg.caldav_username:
            return (self._cfg.caldav_username, self._cfg.caldav_password)
        return None

    def _get_event_raw(self, href: str) -> str:
        url = href if href.startswith("http") else urljoin(self._cfg.caldav_base_url + "/", href.lstrip("/"))
        with httpx.Client(follow_redirects=True) as client:
            resp = client.get(url, headers=self._http_headers(), auth=self._http_auth(), timeout=15)
            resp.raise_for_status()
            return resp.text

    def _put_event_raw(self, href: str, data: str) -> None:
        url = href if href.startswith("http") else urljoin(self._cfg.caldav_base_url + "/", href.lstrip("/"))
        with httpx.Client(follow_redirects=True) as client:
            resp = client.put(url, content=data.encode(), headers=self._http_headers(), auth=self._http_auth(), timeout=15)
            resp.raise_for_status()

    @staticmethod
    def _append_to_description(raw_ical: str, share_url: str) -> str | None:
        """Return a modified iCal string with *share_url* appended to DESCRIPTION."""
        try:
            cal = vobject.readOne(raw_ical)
        except Exception as exc:
            log.warning("vobject could not parse event for write-back: %s", exc)
            return None

        vevent = cal.vevent
        append_text = f"\n\nImmich Album: {share_url}"

        if hasattr(vevent, "description"):
            existing = vevent.description.value or ""
            vevent.description.value = existing + append_text
        else:
            vevent.add("description").value = append_text

        return cal.serialize()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _component_to_event(
        self,
        component: Event,
        href: str | None,
    ) -> CalendarEvent | None:
        """Convert an icalendar VEVENT component to a CalendarEvent or None."""
        summary = str(component.get("SUMMARY", "")).strip()
        if not summary:
            log.debug("Skipping event with empty SUMMARY")
            return None

        uid_raw = component.get("UID")
        uid = str(uid_raw) if uid_raw else summary  # fall back to summary as uid

        dtstart = component.get("DTSTART")
        if dtstart is None:
            log.debug("Skipping event %r: no DTSTART", summary)
            return None

        start_val = dtstart.dt
        if isinstance(start_val, datetime):
            # Normalise to the configured timezone, then extract the date.
            if start_val.tzinfo is None:
                start_val = start_val.replace(tzinfo=self._tz)
            start_date = start_val.astimezone(self._tz).date()
        elif isinstance(start_val, date):
            start_date = start_val
        else:
            log.debug("Skipping event %r: unrecognised DTSTART type %s", summary, type(start_val))
            return None

        return CalendarEvent(uid=uid, summary=summary, start=start_date, href=href)
