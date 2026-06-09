"""Photo-to-event matching logic.

Matching rule
-------------
A photo with capture date P is assigned to event E when:

    E.start <= P <= E.end

where ``E.end`` is the **inclusive** end date extracted from the calendar
event's DTEND (or DURATION).  For all-day events this covers every day of the
event; for timed events it spans from the start date to the date of the end
time in the configured timezone.

If multiple events satisfy the condition the one with the **latest** start date
wins (the photo "belongs" to the most recent preceding event).

If no event satisfies the condition the photo is left unassigned (returned in
the ``unmatched`` list).

All dates are compared as plain ``datetime.date`` objects (timezone
normalisation is handled upstream).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from immich_calendar_album.calendar import CalendarEvent
    from immich_calendar_album.immich import ImmichAsset


@dataclass
class MatchResult:
    # album_id -> list of asset ids to add
    assignments: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    unmatched: list[str] = field(default_factory=list)


def match_assets(
    assets: list["ImmichAsset"],
    events: list["CalendarEvent"],
    event_album_map: dict[str, str],  # event.uid -> album_id
) -> MatchResult:
    """Assign each asset to the best-matching album.

    Parameters
    ----------
    assets:
        All unassigned Immich assets to consider.
    events:
        Calendar events (already filtered to the lookback window).
    event_album_map:
        Maps each event UID to its Immich album ID.

    Returns
    -------
    MatchResult
        ``assignments`` maps album_id → [asset_id, …].
        ``unmatched`` contains asset IDs that could not be matched.
    """
    # Sort events by start date descending so the first qualifying match is
    # always the most recent event whose window contains the photo date.
    sorted_events = sorted(events, key=lambda e: e.start, reverse=True)

    result = MatchResult()

    for asset in assets:
        p: date = asset.local_date
        best_event = None

        for event in sorted_events:
            if event.start <= p <= event.end:
                # sorted desc by start → first match is the most recent event
                best_event = event
                break

        if best_event is None:
            result.unmatched.append(asset.id)
            continue

        album_id = event_album_map.get(best_event.uid)
        if album_id is None:
            # Should not happen if orchestration built the map correctly.
            result.unmatched.append(asset.id)
            continue

        result.assignments[album_id].append(asset.id)

    return result
