# YouTube Migrator

Copy subscriptions and video playlists between Google accounts using Docker and the official YouTube API. The source is read-only. New playlists are **private by default**, optionally unlisted, **never public**. Video order and duplicates are preserved and checked after insertion. Watch Later becomes a regular private playlist.

## Continue your migration

```sh
docker compose run --rm migrator resume --max-subscriptions 100
docker compose run --rm migrator status
docker compose run --rm migrator verify
```

Resume remembers your subscription-only selection. If quota runs out, wait until it is available and resume again. Existing subscriptions are skipped. **Keep the same export and state; do not export again or clear state between batches.**

## First-time setup

You need Docker Compose, two Google accounts, and Google OAuth credentials:

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen. For an External app in Testing, add both Google accounts as test users.
3. Create a **Desktop app** OAuth client. Download its JSON to `config/client_secret.json`.
4. Build and authenticate each account, choosing the correct account/channel in your browser:

```sh
cp .env.example .env
chmod 700 config
docker compose build
docker compose run --rm -p 127.0.0.1:8080:8080 migrator setup --account source
docker compose run --rm -p 127.0.0.1:8080:8080 migrator setup --account destination
```

Open each printed URL on the Docker host. The callback is `http://127.0.0.1:8080/`; no browser runs in Docker. Source access is read-only; destination access permits writes. To choose another account or replace an expired token, repeat setup with `--reauth`. Testing-mode refresh tokens commonly expire after seven days. See [Google's OAuth setup](https://developers.google.com/identity/protocols/oauth2/native-app).

Account aliases in `.env` are labels, not verified email addresses. Keep credentials private; they are ignored by Git and excluded from the image.

## Export once

For subscriptions:

```sh
docker compose run --rm migrator export --subscriptions-only
```

To include accessible owned video playlists, use `export` without that flag. To include a particular accessible playlist, add `--playlist-id PL_ID` (repeatable). The ID is the value after `list=` in its URL. A public source playlist is still copied privately by default.

An export creates a new local backup generation. If Google blocks playlist discovery, it saves accessible subscriptions and marks playlist coverage partial rather than claiming everything was exported.

## Import and check

Preview without contacting the destination, then start subscriptions in batches:

```sh
docker compose run --rm migrator import --subscriptions-only --dry-run
docker compose run --rm migrator import --subscriptions-only --max-subscriptions 100
docker compose run --rm migrator resume --max-subscriptions 100
docker compose run --rm migrator verify
```

The subscription-only choice is saved for resume and verification. Omit the batch limit to continue until completion or a quota/rate-limit pause. Verification writes `data/reports/verification.json`; subscription-only reports do **not** claim that excluded playlists were migrated.

## Video playlists and Watch Later

Subscription migration works without a YouTube channel ID. Playlist creation retains a channel check; the tool never creates a channel for you.

For a backup containing playlists, `import --all-data` includes them. They are private unless you explicitly pass `--playlist-privacy unlisted`; the choice is saved for resume. Public creation and public playlist adoption are rejected. Unavailable videos are reported, and remaining videos keep their relative source order.

To supply Watch Later videos, create an ordered CSV with a `video_id` header or a JSON list of IDs. Examples are in `examples/`. Include the file in a **new** export:

```sh
docker compose run --rm migrator export --watch-later data/imports/watch_later.csv
```

The result is a regular private **Watch Later (Migrated)** playlist, not native Watch Later. Takeout ZIP/TAR inputs are also supported where they contain Watch Later CSV/JSON; Takeout may omit it entirely.

## If something stops

- **Quota or subscription rate limit:** wait, then resume the same migration.
- **Authentication error:** re-run setup with `--reauth`. If the destination has no channel ID, changing its token can require checking the journal binding.
- **Missing source playlists:** try specific accessible `--playlist-id` values, or migrate subscriptions only.
- **Wrong order, unexpected privacy, uncertain write, or state mismatch:** stop and inspect; do not reset the journal or repeatedly append. See [recovery commands](docs/recovery.md).

Credentials stay in `config/`; backups, state, and reports stay in `data/`; local events stay in `logs/`. These directories are ignored by Git. Back up the export and journal while no migration is running. The tool never deletes destination playlists or unsubscribes channels.

## Development

```sh
docker compose build
docker compose run --rm --entrypoint pytest migrator -q
docker compose run --rm --entrypoint ruff migrator check src tests
docker compose run --rm --entrypoint ruff migrator format --check src tests
```

Tests use mocked APIs and need no credentials. Subscription migration has been tested live; playlist creation has not. New backups contain `backup.json`, `unavailable.json`, and a manifest with an integrity hash. Existing backups and journals remain readable.
