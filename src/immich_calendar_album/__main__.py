"""Entry point and scheduler loop.

Run once:
    python -m immich_calendar_album

The process loops forever, sleeping ``SCHEDULE_INTERVAL`` seconds between
runs.  Send SIGTERM or SIGINT to exit cleanly.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

from immich_calendar_album.calendar import CalendarClient
from immich_calendar_album.config import Config
from immich_calendar_album.immich import ImmichClient, ImmichPermissionError
from immich_calendar_album.matcher import match_assets

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Quieten noisy third-party loggers.
    for noisy in ("caldav", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Core run logic
# ---------------------------------------------------------------------------

def run_once(cfg: Config) -> None:
    """Execute one full sync cycle.

    New flow
    --------
    1. Fetch unassigned assets first — exit early when there is nothing to do.
    2. Fetch calendar events only when there are assets to place.
    3. Match assets to events using the event duration as the window.
    4. For every event that received at least one asset:
       a. Create (or reuse) an Immich album.
       b. Optionally write a share link back to the calendar event.
       c. Add the matched assets to the album.

    Empty albums are never created.
    """
    log.info(
        "Starting sync  (lookback=%dd, tz=%s)",
        cfg.lookback_days,
        cfg.tz,
    )

    with ImmichClient(cfg) as immich:

        # --- Step 1: fetch unassigned assets ---------------------------------
        # Nothing can happen without this, so bail early on permission error.
        try:
            assets = list(immich.iter_unassigned_assets())
        except ImmichPermissionError as exc:
            log.warning(
                "Sync aborted — API key is missing the 'asset.read' scope.\n%s",
                exc,
            )
            return

        log.info("Found %d unassigned asset(s)", len(assets))

        if not assets:
            log.info("No unassigned assets – nothing to do this cycle")
            return

        # --- Step 2: fetch calendar events -----------------------------------
        cal_client = CalendarClient(cfg)
        events = cal_client.fetch_events()
        log.info("Fetched %d calendar event(s)", len(events))

        if not events:
            log.info("No calendar events found – nothing to assign this cycle")
            return

        # --- Step 3: match assets to events ----------------------------------
        # Use each event's UID as a stand-in album key so that match_assets
        # can group asset IDs by event without needing real album IDs yet.
        uid_as_album = {e.uid: e.uid for e in events}
        result = match_assets(
            assets=assets,
            events=events,
            event_album_map=uid_as_album,
        )

        # Keep only events that actually have at least one matched asset.
        matched: dict[str, list[str]] = {
            uid: ids for uid, ids in result.assignments.items() if ids
        }

        log.info(
            "Matched %d asset(s) across %d event(s); %d asset(s) left unmatched",
            sum(len(v) for v in matched.values()),
            len(matched),
            len(result.unmatched),
        )

        if not matched:
            log.info("No assets matched any event – nothing to create or assign")
            return

        # --- Step 4: for each matched event, create album + share + assign ---
        uid_to_event = {e.uid: e for e in events}
        existing_albums = {a.name: a for a in immich.list_albums()}
        shared_links = immich.list_shared_links() if cfg.caldav_write_enabled else []

        for uid, asset_ids in matched.items():
            event = uid_to_event[uid]

            # Create or reuse album only for events that have matching photos.
            album = immich.ensure_album(event.summary, existing_albums)

            # Optionally write a share link back to the calendar event.
            if cfg.caldav_write_enabled:
                share_url = immich.get_or_create_share_link(album, shared_links)
                wrote = cal_client.write_back_share_link(event, share_url)
                if wrote:
                    log.info(
                        "Updated event %r with share URL %s",
                        event.summary,
                        share_url,
                    )

            # Add matched assets to the album.
            immich.add_assets_to_album(album.id, asset_ids)

    log.info("Sync cycle complete")


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(signum: int, _frame: object) -> None:
    global _running
    log.info("Received signal %d – shutting down after current cycle", signum)
    _running = False


def main() -> None:
    _configure_logging()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        cfg = Config()  # type: ignore[call-arg]  # fields come from env
    except Exception as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    log.info(
        "immich-calendar-album starting  (interval=%ds, immich=%s)",
        cfg.schedule_interval,
        cfg.immich_base_url,
    )

    while _running:
        try:
            run_once(cfg)
        except Exception as exc:
            log.exception("Unhandled error during sync cycle: %s", exc)

        if not _running:
            break

        log.info("Sleeping %d seconds until next cycle …", cfg.schedule_interval)
        # Sleep in small increments so SIGTERM is handled promptly.
        deadline = time.monotonic() + cfg.schedule_interval
        while _running and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    log.info("Goodbye")


if __name__ == "__main__":
    main()
