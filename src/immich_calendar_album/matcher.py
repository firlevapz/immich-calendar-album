"""Photo-to-event matching logic.

Matching rule
-------------
A photo with capture date P is assigned to event E when:

    E.start <= P  AND  P <= E.start + max_days

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
from datetime import date, timedelta
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
    max_days: int,
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
    max_days:
        Maximum number of days *after* an event start that a photo may still
        be assigned to that event.

    Returns
    -------
    MatchResult
        ``assignments`` maps album_id → [asset_id, …].
        ``unmatched`` contains asset IDs that could not be matched.
    """
    # Sort events by start date descending so we can iterate and find the
    # most recent qualifying event efficiently.
    sorted_events = sorted(events, key=lambda e: e.start, reverse=True)
    delta = timedelta(days=max_days)

    result = MatchResult()

    for asset in assets:
        p: date = asset.local_date
        best_event = None

        for event in sorted_events:
            e_start: date = event.start
            if e_start <= p <= e_start + delta:
                # Because events are sorted desc by start, the first match is
                # the most recent qualifying event.
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
