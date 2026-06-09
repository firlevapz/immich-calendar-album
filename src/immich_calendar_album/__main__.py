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
from typing import NoReturn

from immich_calendar_album.calendar import CalendarClient
from immich_calendar_album.config import Config
from immich_calendar_album.immich import ImmichAlbum, ImmichClient, ImmichPermissionError
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
    """Execute one full sync cycle."""
    log.info(
        "Starting sync  (lookback=%dd, photo_max=%dd, tz=%s)",
        cfg.lookback_days,
        cfg.photo_assign_max_days,
        cfg.tz,
    )

    # --- Step 1: fetch calendar events -----------------------------------
    cal_client = CalendarClient(cfg)
    events = cal_client.fetch_events()
    log.info("Fetched %d calendar event(s)", len(events))

    if not events:
        log.info("No events found – nothing to do this cycle")
        return

    with ImmichClient(cfg) as immich:

        # --- Step 2: ensure albums exist ---------------------------------
        existing_albums: dict[str, ImmichAlbum] = {
            a.name: a for a in immich.list_albums()
        }
        log.info("Immich has %d existing album(s)", len(existing_albums))

        # event.uid -> album_id mapping used by the matcher
        event_album_map: dict[str, str] = {}
        # event -> ImmichAlbum (needed for share-link write-back)
        event_to_album: dict[str, ImmichAlbum] = {}

        for event in events:
            album = immich.ensure_album(event.summary, existing_albums)
            event_album_map[event.uid] = album.id
            event_to_album[event.uid] = album

        # --- Step 2b: write share links back to calendar events ----------
        if cfg.caldav_write_enabled:
            shared_links = immich.list_shared_links()
            for event in events:
                album = event_to_album[event.uid]
                share_url = immich.get_or_create_share_link(album, shared_links)
                wrote = cal_client.write_back_share_link(event, share_url)
                if wrote:
                    log.info(
                        "Updated event %r with share URL %s",
                        event.summary,
                        share_url,
                    )

        # --- Steps 3-5: fetch, match, and assign unassigned assets -------
        try:
            assets = list(immich.iter_unassigned_assets())
        except ImmichPermissionError as exc:
            log.warning(
                "Asset assignment skipped — API key lacks 'asset.read' scope.\n%s",
                exc,
            )
            log.info("Sync cycle complete (asset steps skipped)")
            return

        log.info("Found %d unassigned asset(s)", len(assets))

        if not assets:
            log.info("No unassigned assets – nothing to assign this cycle")
            return

        # --- Step 4: match assets to events ------------------------------
        result = match_assets(
            assets=assets,
            events=events,
            event_album_map=event_album_map,
            max_days=cfg.photo_assign_max_days,
        )
        log.info(
            "Matched %d asset(s) across %d album(s); %d asset(s) left unassigned",
            sum(len(v) for v in result.assignments.values()),
            len(result.assignments),
            len(result.unmatched),
        )

        # --- Step 5: assign assets to albums -----------------------------
        for album_id, asset_ids in result.assignments.items():
            immich.add_assets_to_album(album_id, asset_ids)

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
