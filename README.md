# YouTube & YouTube Music Account Migrator

A local Python 3.12 application for copying subscriptions and owned playlists between separate Google accounts. Docker Compose runs the CLI; credentials, JSON backups, reports, logs, and a SQLite migration journal stay on your filesystem.

The source is only read. The application never deletes playlists, removes entries, or unsubscribes channels. It copies accessible entries by exact video ID, preserving order and duplicate occurrences. It does not perform fuzzy song matching.

**Validation:** 64 mocked tests pass without Google credentials; Docker Compose build, container CLI startup, Ruff linting, and formatting checks pass. Live migrations and actual browser consent have not been tested. YouTube Music uses the unofficial, pinned `ytmusicapi==1.12.3`; upstream changes and account-specific behavior can require adjustments.

## Quick start

After the one-time [Google Cloud setup](#google-cloud-setup):

```sh
cp .env.example .env && chmod 700 config && docker compose build
docker compose run --rm -p 127.0.0.1:8080:8080 migrator setup --account source --service youtube
docker compose run --rm -p 127.0.0.1:8080:8080 migrator setup --account destination --service youtube
docker compose run --rm migrator setup --account source --service music
docker compose run --rm migrator setup --account destination --service music

docker compose run --rm migrator export            # read-only snapshot of the source
docker compose run --rm migrator import --dry-run  # routes, quota units, estimated days
docker compose run --rm migrator import            # writes to the destination only
docker compose run --rm migrator resume            # next day, after a quota pause or any stop
docker compose run --rm migrator verify            # always finish with this
```

Repeat `resume` daily until it reports `finished`; progress is kept in `data/state/`. See [Running over several days](#running-over-several-days).

The quota cost is mostly subscriptions: each costs 50 of a typical 10,000 daily units, so about 200 a day. Playlists that YouTube Music shows identically are written through Music and use no Data API quota. To try quota-free subscriptions, see [Subscriptions](#subscriptions). Everything below is detail for setup, recovery, and edge cases.

## Prerequisites

- Docker Engine/Desktop running, with Docker Compose v2 or newer.
- Two Google accounts, each with a YouTube channel. Select the intended personal/Brand channel during consent.
- A Google Cloud project with YouTube Data API v3 enabled.
- A Desktop OAuth client for YouTube and a **separate TV / limited-input client** for Music.
- A browser on the machine running Docker for YouTube's loopback callback. No browser runs inside the container.
- Sufficient disk space for backups. Credentials and backups are private personal data; keep a separate secure backup of the journal and exports.

## Google Cloud setup

1. Open [Google Cloud Console](https://console.cloud.google.com/), create a project, and select it.
2. In **APIs & Services → Library**, find **YouTube Data API v3** and enable it.
3. Open **Google Auth Platform** (or **OAuth consent screen**). Configure app branding, support email, and developer contact. Choose External audience for consumer Google accounts.
4. If the app is in Testing, add **both accounts** under Audience → Test users. Configure the YouTube scopes under Data Access as needed. An organization may restrict consent.
5. Under Clients (or Credentials → Create credentials → OAuth client ID), create a **Desktop app** client. Download its JSON to `config/client_secret.json`.
6. Create another OAuth client with application type **TVs and Limited Input devices**. Download its JSON to `config/music_client_secret.json`. Music also accepts this private local file shape:

   ```json
   {"client_id": "YOUR_TV_CLIENT_ID", "client_secret": "YOUR_TV_CLIENT_SECRET"}
   ```

7. Copy `.env.example` to `.env` and set account aliases so terminal output helps identify the intended accounts. Aliases are labels, not verified email addresses.
8. Build and run the four setup commands below. In every browser consent flow, explicitly select the intended account/channel.

YouTube source consent requests `youtube.readonly`. Destination consent requests `youtube.force-ssl`, a write scope accepted by subscription and playlist insert endpoints. These scopes do not supply email addresses. The tool displays the authenticated YouTube channel ID and refuses to import into the source channel. There are no API keys for private data.

Google's Desktop flow uses PKCE and a loopback callback, **not** the deprecated out-of-band flow. The callback is `http://127.0.0.1:8080/` by default. Desktop clients support loopback redirects; you do not register a Web application redirect URL. The listener binds to `0.0.0.0` inside Docker, and the command below publishes it only on the host's loopback interface. Only run one setup process at a time. For a remote Docker host, use an SSH local port tunnel to the Docker host; the browser's `127.0.0.1` must reach that host.

External apps in Testing generally receive refresh tokens that expire after seven days when requesting these YouTube scopes. Revocation, inactivity, token limits, and account/organization policies can also invalidate tokens. Re-run setup for the affected account when refresh fails. Moving an app to Production changes testing restrictions and may introduce verification requirements; consult Google's console guidance for your configuration.

See [Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app), [refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration), and [YouTube authorization](https://developers.google.com/youtube/v3/guides/authentication).

## Build and authenticate

From this repository:

```sh
cp .env.example .env
# Download your two OAuth client JSON files into config/ as described above.
chmod 700 config
docker compose build

docker compose run --rm -p 127.0.0.1:8080:8080 migrator setup --account source --service youtube
docker compose run --rm -p 127.0.0.1:8080:8080 migrator setup --account destination --service youtube

docker compose run --rm migrator setup --account source --service music
docker compose run --rm migrator setup --account destination --service music
```

For YouTube, copy the printed consent URL into your host browser. Complete consent within five minutes; the callback completes authentication. Do not paste the URL or resulting credentials into chat.

For Music, the library prints a verification URL and device code. Open that URL in your browser, enter the code, select the correct account, grant consent, and follow the terminal prompt. This is Google's device authorization flow, distinct from deprecated out-of-band OAuth. Music uses its own refresh tokens and its own client credentials; the YouTube desktop token is never reused. Its OAuth scope is determined by ytmusicapi and is broader than a source-only read scope; this application still performs only reads on the source.

The setup wrapper calls the installed library's `setup_oauth(client_id, client_secret, filepath, open_browser=False)`. Runtime passes `OAuthCredentials` to `YTMusic`. This follows the pinned version's [OAuth setup documentation](https://ytmusicapi.readthedocs.io/en/1.12.3/setup/oauth.html). Browser-header authentication is not implemented; this avoids asking you to copy browser cookies into the tool.

Stored files:

```text
config/
  client_secret.json
  music_client_secret.json
  source_youtube_token.json
  destination_youtube_token.json
  source_music_auth.json
  destination_music_auth.json
```

Generated token files have restrictive permissions; credentials are mounted at runtime, excluded from Git and Docker build context, and never copied into the image. Do not share these files. On Linux, Docker's default root user may create root-owned bind-mount files; use `docker compose run --rm --user "$(id -u):$(id -g)" ...` consistently if needed (ensure mounted directories are writable).

To use another OAuth port, set `OAUTH_PORT=8090` in `.env` and publish `-p 127.0.0.1:8090:8090` for the two YouTube setup commands. Normal export/import/verify runs do not need a published port. Compose `run` does not automatically publish service ports, so the setup commands explicitly use `-p`.

## Watch Later

The official API does not expose native Watch Later contents. The destination is a regular private playlist named **Watch Later (Migrated)**, never the destination's native Watch Later collection.

1. Visit [Google Takeout](https://takeout.google.com/) in the **source** account.
2. Deselect other products; select YouTube and YouTube Music. Use the data selection controls to include playlists where offered.
3. Request and download an export. Copy the archive into `data/imports/`.
4. Parse it:

   ```sh
   docker compose run --rm migrator watch-later data/imports/takeout.zip
   ```

ZIP, TAR, and TAR.GZ are supported without extracting files onto disk. Inside archives, the parser recognizes English filenames containing `Watch Later`, `watch_later`, or `watch-later` with CSV/JSON extensions. It handles Takeout CSV video tables following playlist metadata. Individual matching members are limited to 64 MiB. Multiple matches require explicitly choosing one extracted input, so ordering is never guessed. HTML and localized filename variants are not parsed; convert them to the manual schema below.

**Takeout may omit Watch Later entirely.** A missing file produces an explicit error. The application cannot retrieve unavailable native contents through another official API. In that case, manually prepare an ordered list of exact video IDs/YouTube watch URLs and use one of the included examples:

```sh
cp examples/watch_later.csv data/imports/my_watch_later.csv
# Replace the sample videos with your own ordered IDs.
docker compose run --rm migrator watch-later data/imports/my_watch_later.csv
```

CSV requires a header `video_id`, `Video ID`, `videoId`, or `url`, followed by one entry per row. JSON accepts a list of ID strings, a list of objects with `video_id`/`videoId`/`url`, or an object containing that list under `videos` or `entries`. See `examples/watch_later.json`. Video IDs must contain exactly 11 allowed characters. Invalid/missing IDs fail validation rather than being silently dropped. File order and duplicates are preserved.

The extraction command atomically writes `data/imports/watch_later.json`. Include that file in your export:

```sh
docker compose run --rm migrator export --watch-later data/imports/watch_later.json
```

You may also pass a supported CSV/JSON/archive directly to `export --watch-later PATH`. An export without this option explicitly warns that Watch Later is omitted. The extracted input is never included automatically.

## Export, import, resume, verify

```sh
# Export both services, optionally with the Watch Later option described above.
docker compose run --rm migrator export

# Offline upper-bound plan; no authentication or remote/local migration writes.
docker compose run --rm migrator import --dry-run

# Writes only to the destination. Keep this export and journal for the entire migration.
docker compose run --rm migrator import

# Optional: carry on past a playlist/subscription that needs inspection; still exits 1 at the end.
docker compose run --rm migrator resume --continue-on-conflict

docker compose run --rm migrator status
docker compose run --rm migrator resume
docker compose run --rm migrator verify
```

Service-specific backups:

```sh
docker compose run --rm migrator export --service youtube
docker compose run --rm migrator export --service music
```

Each export creates a **new standalone backup**, not an incremental merge into the previous export. For a combined migration, use `--service all` (the default) so overlapping playlists can be detected. Do not run a new export during an unfinished migration. Resume reads the current backup and refuses a journal bound to a different generation or destination credential identity.

A Music-only export/import needs only Music credentials. YouTube or Watch Later exports require the corresponding YouTube credentials. No source credentials are required during import except an optional local check for accidentally identical Music files.

### Running over several days

A migration larger than one day's quota is expected to span several runs:

1. Run `export` once, then `import`. It works through playlists and then subscriptions until Google reports quota exhaustion.
2. A write rejected for quota definitely did not apply, so it is marked for retry, not as uncertain. The journal records `paused: quota exhausted`, and the command exits `1`.
3. After the quota resets, run `resume`. This is normally midnight Pacific time; check your project's quota page. `resume` re-reads the destination, skips everything already confirmed, and continues where it stopped. `import` with the same journal behaves identically.
4. Repeat until `resume` prints `finished with N failed/unavailable entries; run verify`, then run `verify`.

Progress lives in `data/state/migration.sqlite3`; the snapshot it works from is in `data/exports/`. Both are on the host via bind mounts, so they survive the container exiting, Ctrl-C, or a reboot. `status` shows progress at any time without contacting Google or using quota.

Between runs:

- **Do not run `export` again.** A new snapshot is a different generation, and `resume` refuses to use the existing journal with it. Channels subscribed or songs added to the source after the export are not picked up; add them manually afterwards.
- **Repeat `--subscribe-via music` on every `resume` if you chose that route.** The option is not saved; plain `resume` uses the official API and spends quota. Mixing routes is safe, only costlier.
- **Do not edit migrated playlists in either account.** Unexpected destination changes pause the run instead of being guessed at.
- **Authentication errors:** Testing-mode tokens expire after seven days. Re-run `setup` for the affected account and service, then `resume`. The journal survives re-authenticating YouTube, and Music too when Music returns a handle (see [Data and recovery design](#data-and-recovery-design)).
- **Other pauses** (throttled or unconfirmed subscriptions, playlist conflicts) print what to do. Most need only a wait and `resume`.

Each playlist's write route is chosen once and journalled as `via`, so resume never switches services:

| Playlist | Route | Data API quota |
| --- | --- | --- |
| Music-only | ytmusicapi | none |
| Shared, and the Music view equals the YouTube sequence | ytmusicapi | none |
| Shared, but the views differ (Music hides some entries) | official YouTube API | 50 per entry + read-backs |
| YouTube-only, or Watch Later | official YouTube API | 50 per entry + read-backs |

### Playlist order

Each destination playlist reproduces the source playlist's **top-to-bottom order exactly**, including repeated videos. The source order is the playlist's position order, as the YouTube API and Music report it. Watch Later uses the order of your input file. Entries are inserted one at a time in that order, each at the end of the already-confirmed entries. On the official API the position is explicit; Music appends. After every insert the playlist is read back, and the run pauses if the sequence is not exactly the expected prefix. A wrong order cannot be silently recorded as success, and `verify` checks the full sequence again.

The tool copies order; it does not sort by date. If the source playlist adds new videos at the bottom (YouTube's default), the newest videos are last in the destination too. If the source was set to add new videos to the top, that top-to-bottom order is copied as is. Original "date added" timestamps cannot be recreated: every destination entry is dated by the migration, in ascending order. A view sorted by **Date added** in the destination therefore matches the source's manual order, not the source's original dates. Skipped unavailable or failed entries leave the rest in their relative order without gaps.

Your own playlists normally appear in both services under the same ID, so a combined export usually routes them through Music. Divergent views stay on the official API because the Music read-back could not confirm the canonical sequence. `import --dry-run` lists each playlist's route.

### Subscriptions

Subscriptions are processed after playlists, so quota-free playlists finish even if subscriptions use up the day's quota. Each subscribe is sent once. A single Data API listing at the end confirms the whole batch (about 1 unit per 50 subscriptions), not one read per channel. Writes that are not yet visible are re-read after short waits. If they still are not visible, the run pauses; `resume` resends them, which is safe because YouTube treats subscribing as idempotent.

- `--subscribe-via youtube` (default): official `subscriptions.insert`, 50 units each.
- `--subscribe-via music` (**experimental**): ytmusicapi's `subscribe_artist`, the web client's subscribe endpoint, one channel per request and no Data API quota. It is meant for artist channels and has not been confirmed for ordinary YouTube channels. It needs destination Music credentials, and confirmation still uses the destination YouTube listing, so the two destination logins must be the same Google account. Try a few first:

  ```sh
  docker compose run --rm migrator import --subscribe-via music --max-subscriptions 5
  docker compose run --rm migrator status   # look for subscription:… "migrated"
  ```

  If they are confirmed, continue with `resume --subscribe-via music`. If not, run `resume` without the option to use the official API. Mixing routes is safe.

`--max-subscriptions N` caps new subscription attempts in one run; subscriptions already sent are still confirmed.

Error handling: `subscriptionDuplicate` counts as success. `publisherNotFound`/`channelNotFound` (and other 404s for the channel) are recorded as permanent failures. **Any other rejection pauses the run** and marks the channel `blocked`, rather than silently failing it. This covers throttling such as `subscriptionLimitExceeded`, which YouTube applies to bulk subscribing regardless of quota, and `subscriptionForbidden`. If YouTube is throttling, wait a few hours and `resume`. If one channel can never be subscribed, skip it explicitly. Verification then reports it as `skipped` and does not count the migration as complete:

```sh
docker compose run --rm migrator skip-subscription CHANNEL_ID
docker compose run --rm migrator resume
```

`status` reads local state without authentication. `verify` reads the destination and writes `data/reports/verification.json`, with a terminal summary grouped into subscriptions, YouTube, Music, and Watch Later. Verification checks mapped IDs, titles, descriptions (allowing the recovery marker), privacy, counts, exact ID sequence, and accessibility. Shared playlists are read through both services. Report statuses include migrated, already_present, missing, unavailable, failed, skipped, discrepancy, and not_verifiable. Unavailable content is not counted as fully verified success. Extra destination subscriptions are permitted; extra entries in mapped playlists are discrepancies.

Exit codes: `0` for a completed command, `1` for an error/quota pause requiring attention, `2` for verification discrepancies or incomplete verification. Import completion means its operation loop finished; read its failure count and **always run verify**. The dry run is an offline upper-bound plan. For each playlist it lists the route, entry count, and YouTube units. It totals Data API units (writes plus read-backs), the Music request count, and `estimated_days` against `--daily-quota` (default 10,000). It assumes destination credentials for every exported service and cannot know existing subscriptions or destination changes. It does not promise an exact quota figure.

## Data and recovery design

```text
data/
  exports/
    manifest.json                  # atomic pointer to current immutable generation
    <generation>/
      manifest.json
      backup.json                  # unified playlists + subscriptions
      subscriptions.json
      youtube_playlists.json
      youtube_playlist_items.json
      youtube_music_playlists.json
      watch_later.json
      unavailable.json
  imports/                         # your inputs, plus extracted watch_later.json
  state/
    migration.sqlite3              # mapping, intents, successes, failures, quota events
    migration.sqlite3-wal          # may exist while SQLite is open
    migration.sqlite3-shm
    process.lock
  reports/
    verification.json
logs/
  events.jsonl                     # local structured operation/progress log; no tokens
```

JSON is UTF-8, schema version 1. Each generation contains source channel identifiers where available, Music account display information/handle where returned, configured aliases, timestamp, service names, counts, warnings, completeness status, service-specific metadata, and all identifiable unavailable entries. The backup has a content hash checked before import. A failure while exporting leaves the previous manifest pointer unchanged. Exports are snapshots; avoid changing source playlists during an export because the APIs cannot give a cross-request transaction.

Playlist overlap is determined by **source playlist ID**, never title. A shared playlist retains both service snapshots and exposure flags and produces a single destination mapping. The YouTube sequence/metadata is canonical for overlapping playlists; differing Music snapshots are retained and generate a warning and verification discrepancy. This can occur because the services hide different entries; there is no universally correct combined sequence. The importer does not guess a union or substitute tracks. Saved playlists owned by other users and Music automatic collections are excluded. Unknown Music ownership is warned about.

SQLite commits intent before each write and progress after remote confirmation. Entries are identified by the immutable backup's playlist and position, with original item IDs retained. Repeated video IDs are not collapsed. Each created playlist has a short `[youtube-migrator:...]` suffix in its description for recovery after a lost creation response; original descriptions remain in the backup. Descriptions are never silently truncated. If the YouTube 5,000-character description limit would be exceeded, create an empty destination manually and map it as described below.

On resume, existing subscriptions are re-read and mapped playlist contents are reconciled in order. A pending append that appears exactly once in the expected position is acknowledged. Because list results can briefly lag a write, every read-back that does not yet match is retried after 1, 2, 4, and 8 seconds before the run pauses. Unexpected remote edits, missing mappings, changed privacy, missing recovery markers, and uncertain writes pause the operation. Do not edit mapped playlists during migration. Keep both exports and journal; starting with a fresh journal is a **new migration** and can intentionally create another copy.

Music account handles are displayed and compared with the source when available, but may be absent or change. They are not verified Google email addresses. The journal binds to the destination Music **handle** when Music returns one, so re-running Music setup (for example after a Testing-mode token expires after seven days) keeps the same journal. The SHA-256 fingerprint of the refresh credential is also recorded and updated, but it decides the binding only when no handle is returned. In that case the tool warns at import, and re-authenticating Music causes a binding refusal. Then preserve the journal/backup and reconcile the new binding manually after confirming the destination. Do not discard state or start a fresh import as a shortcut.

### Ambiguous remote operations

There is no cross-system transaction or idempotency key for playlist creation/appends. A network timeout can mean the operation succeeded. The tool favors an explicit pause over duplicate writes.

- After interrupted creation, `resume` searches owned destination descriptions for the persisted recovery marker. Exactly one match is reused. Zero or multiple matches pause; the tool does not create another playlist automatically.
- If creation definitely failed, or the description could not hold a marker, inspect the destination in your browser. After ensuring the original request cannot still create a playlist, create an empty playlist with the intended title/description/privacy. Explicitly map its stable ID:

  ```sh
  docker compose run --rm migrator map-playlist SOURCE_PLAYLIST_ID DESTINATION_PLAYLIST_ID
  docker compose run --rm migrator resume
  ```

  Only an empty playlist can be adopted. An existing completed mapping is never overwritten. This may also be done before import. Titles alone are never used to choose a playlist.

- For an uncertain insertion, first wait for remote state to settle and inspect the mapped playlist. `resume` automatically acknowledges an exact expected pending occurrence if visible. If the operation definitely did not apply and can no longer commit:

  ```sh
  docker compose run --rm migrator resolve-pending SOURCE_PLAYLIST_ID absent
  docker compose run --rm migrator resume
  ```

  The alternative outcome is `applied`. Both commands validate the entire remote sequence against the selected outcome. The positional arguments are `SOURCE_PLAYLIST_ID OUTCOME`; no `--outcome` option is needed. Do not choose `absent` solely because a just-timed-out operation is not yet visible.

Permanent failures and unavailable entries are journalled and skipped on later resumes. This prevents repeated permanent requests and preserves the order of the successfully migrated subset. No automatic retry or reorder of these entries is offered. Repair an identified failed entry manually in its original position after the migration and rerun verification, keeping the failure record for audit. A successful insertion response alone is insufficient: the remote sequence must confirm the insertion.

Use `--state data/state/OTHER.sqlite3` with `import`, `resume`, `verify`, `status`, `map-playlist`, `resolve-pending`, or `skip-subscription` only for an intentionally separate migration. The process lock prevents concurrent operations against this workspace. Back up SQLite after processes exit, or use SQLite's backup facilities; do not copy a live database without its WAL.

## Quotas and retries

YouTube list requests are estimated at 1 unit and subscription/playlist/item inserts at 50 units each. Many projects have a 10,000-unit daily allocation for these operations, but your actual allocation may differ. Check Cloud Console → YouTube Data API v3 → Quotas. The application does not assume a reset time; `--daily-quota` only affects the dry-run day estimate.

To minimise units:

1. Export with `--service all` (the default) and set up destination Music, so matching playlists route through Music at zero units.
2. Run `import --dry-run` and check each playlist's `via` and the `estimated_days`.
3. For subscriptions, either accept about 200 per day via the official API or test `--subscribe-via music` as described in [Subscriptions](#subscriptions).
4. If you need more, request a quota increase through the YouTube API audit/extension form linked from Cloud Console. Do not spread one migration across extra Cloud projects to multiply quota; YouTube's API terms forbid it.

Worked example: 500 subscriptions and 4 Music playlists with 150 songs total cost about 25,000 units (3 days) with the default route. Playlists cost nothing via Music. With `--subscribe-via music`, the whole run costs roughly 20 units if that route works for your channels.

Actual attempted YouTube units, including reads and retry attempts, are stored as `estimated_youtube_units_last_run` in state. Officially routed playlists are read back after each insertion (about 1 unit per 50 entries in the playlist). This improves recovery confidence but adds quota usage and makes very large playlists slower. Inserts are deliberately sequential to preserve ordering. Music quota usage is separate and undocumented.

Requests are spaced by `API_INTERVAL` seconds (default `0.2`). Transient reads (HTTP 429, selected 5xx errors, rate-limit reasons, network errors) use capped exponential backoff with jitter, up to five attempts. Writes are **not blindly retried** after transient/unknown outcomes: their pending intent is preserved for reconciliation. Quota exhaustion pauses promptly with state intact; run `resume` after your quota is available. Authentication/insufficient-scope failures stop for correction. Definite permanent entry failures are recorded without automatic retry. The tool never bypasses quota limits.

Costs and behavior were checked against the [official quota reference](https://developers.google.com/youtube/v3/determine_quota_cost), [playlist creation](https://developers.google.com/youtube/data/reference/rest/v3/playlists/insert), and [item insertion](https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/insert). The pinned Music signatures were checked in the installed library and its [playlist](https://ytmusicapi.readthedocs.io/en/1.12.3/reference/playlists.html) and [library](https://ytmusicapi.readthedocs.io/en/1.12.3/reference/library.html) references.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `access_denied`, unverified app, or app unavailable | Add both accounts as test users; check audience, organization restrictions, app publishing status, and consent settings. |
| Redirect mismatch / browser cannot connect | Use a Desktop client; publish the correct loopback port with `-p`; check `OAUTH_PORT`, port conflicts, firewall, and Docker host location. Do not use Web-client/OOB redirects. |
| Wrong account | Compare the printed YouTube channel ID/Music handle with the intended channel. Re-run setup with correct `--account` and service. The destination channel must differ from source. Never change account mid-migration. |
| Expired/revoked token or `invalid_grant` | Re-run setup for that account/service; see the Music fingerprint caveat above. Do not print token files to diagnose. |
| Missing scopes / API disabled | Enable YouTube Data API v3 in the client project and re-run destination setup to grant the write scope. |
| Quota exhausted | Wait for the project's allocation to become available, then `resume` with the same journal and backup. A new OAuth token does not reset quota. |
| Subscription `blocked` / `subscriptionLimitExceeded` | YouTube is throttling bulk subscribing. Wait a few hours, then `resume`; use `--max-subscriptions` to pace runs. For a channel that is permanently rejected, run `skip-subscription CHANNEL_ID`. |
| Sent subscriptions "not yet visible" | Wait a few minutes and `resume`; resending is safe. With `--subscribe-via music`, persistent invisibility means that route does not work for those channels or the Music and YouTube destination logins are different accounts. Resume without the option. |
| Music OAuth client rejected | Create a TV/limited-input client, place its JSON under the Music filename, and authenticate each account independently. Desktop credentials are not interchangeable. |
| Music parser/server error | Check whether YouTube Music works for the account in a browser, regional/account eligibility, and upstream ytmusicapi issues. A pinned dependency can still break after a server change. Resolve pending writes before changing versions. |
| Private/deleted videos or region-restricted songs | Inspect unavailable/failed entries and verification JSON. Exact identifiers are retained; no alternate recording is chosen. Availability can change between export and import. |
| No Watch Later in Takeout | Use the CSV/JSON fallback. Absence cannot be repaired through the official API. |
| Interrupted migration / uncertain write | Preserve exports and state; run `resume`. Use the explicit recovery commands only after inspecting the remote sequence. |
| Destination playlist already exists | The tool never assumes a title match is yours. Adopt an inspected empty playlist with `map-playlist`, or let the tool create a separate mapped copy. |
| Export generation / destination binding mismatch | Restore the original current export pointer/credential and journal from backup. Do not start with empty state unless you intend a separate copy. Re-running Music setup is fine when Music returns a handle (see above). |
| Destination sequence or privacy conflict | Inspect changes made outside the migrator. Restore expected metadata/sequence manually; the tool does not delete or reorder existing remote content to force a match. |

## Development and checks

The image includes pinned pytest and Ruff so tests can run without a host Python installation or Google credentials:

```sh
docker compose build
docker compose run --rm migrator --help
docker compose run --rm --entrypoint pytest migrator -q
docker compose run --rm --entrypoint ruff migrator check src tests
docker compose run --rm --entrypoint ruff migrator format --check src tests
```

The tests mock external services and exercise service routing, subscription batching/throttling/duplicates, listing-lag settling, handle-based Music binding, dry-run estimates, pagination across all YouTube collections, ordering, duplicated tracks, overlapping service IDs, ownership filtering, Takeout/manual parsing, missing Watch Later, OAuth token refresh/scope selection, transient/permanent/quota failures, interrupted creation/insertion/subscriptions, journal mappings, idempotent resume, destination edits, verification discrepancies, backup integrity, and offline dry runs. They do not prove live account behavior.

With Python 3.12+ installed locally, you may instead create a virtual environment, run `pip install -e '.[dev]'`, then `pytest` and `ruff check src tests`. Run the CLI from the repository root so relative config/data/log paths resolve consistently.

## Repository

```text
Dockerfile, compose.yaml, pyproject.toml
README.md, .env.example, .gitignore, .dockerignore
examples/                      Watch Later CSV and JSON samples
config/                        runtime credentials (ignored)
data/                          runtime inputs, exports, state, reports (ignored)
logs/                          runtime structured events (ignored)
src/youtube_migrator/
  cli.py                       commands and account selection
  authentication.py            independent OAuth/refresh and bounded Music transport
  clients.py                   YouTube and Music adapters/pagination
  models.py                    unified playlists and occurrence identities
  backup.py                    immutable generation snapshots and integrity checks
  watch_later.py               Takeout and manual parsing
  state.py                     SQLite operation journal
  migration.py                 routing, import, reconciliation, verification, dry-run plan
  errors.py                    classification, throttling, retry policy
  storage.py                   atomic JSON writes and process lock
  events.py                    structured local progress log
tests/                         credential-free unit/integration-style tests
```

Out of scope: history, comments, likes, Music liked songs/albums/artists, Premium, and Google account settings. Native Watch Later semantics, creation timestamps, thumbnails, collaborative roles, account ownership transfers, and inaccessible/hidden items cannot be faithfully recreated. Public/private/unlisted settings are copied where exposed; missing privacy information defaults to private and is retained in the service metadata for inspection. No live migration guarantee is made.
