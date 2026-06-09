# immich-calendar-album

[![Release Please](https://github.com/youruser/immich-calendar-album/actions/workflows/release-please.yml/badge.svg)](https://github.com/youruser/immich-calendar-album/actions/workflows/release-please.yml)
[![Docker](https://github.com/youruser/immich-calendar-album/actions/workflows/docker.yml/badge.svg)](https://github.com/youruser/immich-calendar-album/actions/workflows/docker.yml)
[![GitHub release](https://img.shields.io/github/v/release/youruser/immich-calendar-album)](https://github.com/youruser/immich-calendar-album/releases)

Automatically creates albums in [Immich](https://immich.app) based on events fetched from a CalDAV calendar, then assigns your recent unorganised photos to the matching album.

## How it works

On each run the service performs five steps:

1. **Fetch calendar events** — queries the CalDAV server (or a plain `.ics` URL) for all events within the configured lookback window (default: 7 days back from today).
2. **Ensure albums exist** — for every event with a title, creates an Immich album named after that title. If the album already exists it is reused.
3. **Write share links back** *(optional)* — if CalDAV write credentials are provided, creates a public upload-enabled Immich share link for each album and appends it to the corresponding calendar event's description. The operation is idempotent: it is skipped if the link is already present.
4. **Fetch unassigned assets** — retrieves all Immich photos/videos that are not yet part of any album.
5. **Match and assign** — assigns each unassigned asset to the album whose event start date is the closest earlier date relative to the photo's capture date, within a configurable window (default: 5 days). Photos outside every event window are left unassigned.

### Matching rule

```
event.start  ≤  photo.date  ≤  event.start + PHOTO_ASSIGN_MAX_DAYS
```

When multiple events qualify (overlapping windows), the most recent event wins. Photos that do not fall within any event window are never forcibly assigned.

### Calendar compatibility

| Server type | Behaviour |
|---|---|
| CalDAV (Nextcloud, Radicale, …) | REPORT query with date range — efficient, handles recurring events |
| Plain `.ics` URL | HTTP GET of the whole file, filtered in Python via `recurring_ical_events` |
| CalDAV fails at runtime | Automatic fallback to plain ICS fetch |

---

## Quick start

```bash
# 1. Clone and enter the project
git clone https://github.com/youruser/immich-calendar-album.git
cd immich-calendar-album

# 2. Create your env file
cp .env.example .env
$EDITOR .env          # fill in Immich URL/key and CalDAV URL/credentials

# 3. Build and run
docker compose up --build -d

# 4. Follow logs
docker compose logs -f
```

---

## Configuration

All settings are passed as environment variables (or via `.env`).

### Required

| Variable | Description |
|---|---|
| `IMMICH_URL` | Base URL of your Immich instance, e.g. `https://immich.example.com` |
| `IMMICH_API_KEY` | Immich API key (Profile → API Keys) |
| `CALDAV_URL` | CalDAV calendar collection URL **or** a direct `.ics` file URL |

### CalDAV authentication

At least one of the following pairs must be set to enable write-back:

| Variable | Description |
|---|---|
| `CALDAV_USERNAME` | Basic-auth username |
| `CALDAV_PASSWORD` | Basic-auth password |
| `CALDAV_API_KEY` | Bearer-token API key (alternative to username/password) |

Leave all three empty for unauthenticated (read-only) access. Write-back is automatically disabled in that case.

### Behaviour

| Variable | Default | Description |
|---|---|---|
| `LOOKBACK_DAYS` | `7` | How many days back to search for calendar events |
| `PHOTO_ASSIGN_MAX_DAYS` | `5` | Max days after an event's start date a photo may still be assigned to it |
| `SCHEDULE_INTERVAL` | `3600` | Seconds between sync runs |
| `TZ` | `UTC` | IANA timezone used for all date comparisons (e.g. `Europe/Berlin`) |

---

## CalDAV URL examples

```
# Nextcloud calendar collection
https://cloud.example.com/remote.php/dav/calendars/USERNAME/CALENDAR-SLUG/

# Radicale
https://cal.example.com/USERNAME/CALENDAR-SLUG/

# Plain ICS export (Google Calendar, iCloud public link, …)
https://calendar.google.com/calendar/ical/USER%40gmail.com/public/basic.ics
```

---

## Share link write-back

When CalDAV write credentials are configured the service will, for each calendar event:

1. Look up whether the album already has a public shared link in Immich.
2. Create one if it does not exist (upload + download enabled, no expiry).
3. Append the following text to the event's `DESCRIPTION` field (skipped if already present):

```
Immich Album: https://immich.example.com/share/<key>
```

This gives anyone with access to the calendar a direct link to view or upload photos for that event.

---

## Project structure

```
immich-calendar-album/
├── Dockerfile                        # two-stage Python 3.14 / Alpine / uv build
├── docker-compose.yml
├── .env.example                      # documented variable reference
├── pyproject.toml                    # uv-managed project + dependencies
├── uv.lock                           # pinned dependency lockfile
└── src/
    └── immich_calendar_album/
        ├── config.py                 # pydantic-settings env config
        ├── calendar.py               # CalDAV + ICS fetch, write-back
        ├── immich.py                 # Immich REST API client
        ├── matcher.py                # photo-to-event assignment logic
        └── __main__.py               # scheduler loop + orchestration
```

### Dependencies

| Library | Purpose |
|---|---|
| [`caldav`](https://github.com/python-caldav/caldav) | CalDAV REPORT queries |
| [`icalendar`](https://github.com/collective/icalendar) | iCal parsing |
| [`recurring-ical-events`](https://github.com/niccokunzmann/python-recurring-ical-events) | RRULE/EXDATE expansion |
| [`httpx`](https://www.python-httpx.org) | HTTP client (Immich API + ICS fallback) |
| [`pydantic-settings`](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Typed env-var config with validation |
| [`vobject`](https://eventable.github.io/vobject/) | Mutating VEVENT objects for write-back |
| [`python-dateutil`](https://dateutil.readthedocs.io) | Timezone-aware date arithmetic |

---

## Local development

```bash
# Install dependencies
uv sync

# Run against a real or mock environment (needs a populated .env)
uv run python -m immich_calendar_album
```

---

## Releases and versioning

Versioning follows [Semantic Versioning](https://semver.org). Releases are fully automated via [Release Please](https://github.com/googleapis/release-please):

1. Commit to `main` using [Conventional Commits](https://www.conventionalcommits.org):

   | Prefix | Effect |
   |---|---|
   | `fix: …` | Patch release (`0.1.0` → `0.1.1`) |
   | `feat: …` | Minor release (`0.1.0` → `0.2.0`) |
   | `feat!: …` or `BREAKING CHANGE:` footer | Major release (`0.1.0` → `1.0.0`) |
   | `chore:`, `docs:`, `refactor:`, … | No release |

2. Release Please opens a **Release PR** that bumps `pyproject.toml`, writes `CHANGELOG.md`, and updates `.release-please-manifest.json`.

3. Merging the Release PR creates a **GitHub Release** with a git tag (`v1.2.3`).

4. The Docker workflow immediately builds and pushes a versioned image to GHCR.

### Docker image

```bash
# Latest build from main
docker pull ghcr.io/youruser/immich-calendar-album:latest

# Specific release
docker pull ghcr.io/youruser/immich-calendar-album:1.2.3
```

Images are built for `linux/amd64` and `linux/arm64`.
