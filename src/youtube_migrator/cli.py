"""Docker CLI for YouTube subscriptions and ordered, non-public playlists."""

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .authentication import youtube_credentials
from .backup import Backup
from .clients import YouTube
from .errors import APIError, InputError, UserError
from .events import event
from .migration import Conflict, Migrator, allowed_privacy, plan
from .models import Playlist
from .state import State
from .storage import atomic, locked, read
from .watch_later import parse

os.umask(0o077)
app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
CONFIG = Path("config")
EXPORTS = Path("data/exports")
DEFAULT_STATE = Path("data/state/migration.sqlite3")
StateOption = Annotated[Path, typer.Option("--state", help="Migration journal")]
PlaylistPrivacy = Annotated[str | None, typer.Option(help="private (default) or unlisted")]
SubscriptionScope = Annotated[
    bool | None,
    typer.Option("--subscriptions-only/--all-data", help="Scope saved for resume and verify"),
]
MaxSubscriptions = Annotated[int | None, typer.Option(min=1, help="Maximum new attempts this run")]
ContinueOnConflict = Annotated[bool, typer.Option(help="Continue unrelated work after a conflict")]


def check_choice(value, choices):
    if value not in choices:
        raise typer.BadParameter(f"Expected one of {', '.join(choices)}")


def yt(account, write=False):
    from .errors import Caller

    return YouTube(
        youtube_credentials(CONFIG, account, write),
        caller=Caller(float(os.getenv("API_INTERVAL", "0.2"))),
    )


@contextmanager
def operation(name=None):
    """Serialize commands and report errors without leaking third-party response details."""
    try:
        if name:
            console.print(f"YouTube Migrator — {name}", style="bold")
            for role in ("source", "destination"):
                alias = os.getenv(role.upper() + "_ALIAS", role)
                console.print(f"{role}: {alias} (configured alias)", markup=False)
            event(name)
        with locked(Path("data/state/process.lock")):
            yield
    except Exception as exc:
        safe = isinstance(exc, (APIError, Conflict, FileNotFoundError, InputError, UserError))
        message = (
            str(exc)
            if safe
            else (
                f"{type(exc).__name__}: check credentials, permissions, and connectivity. "
                "Sensitive error details suppressed."
            )
        )
        console.print(message, style="red", markup=False)
        event("failure", error_type=type(exc).__name__)
        raise typer.Exit(1) from None


def destination(backup, write, allow_missing_channel=False):
    youtube = yt("destination", write=write)
    channel = (
        youtube.identity(allow_missing=True)
        if (allow_missing_channel or not backup.get("playlists"))
        else youtube.identity()
    )
    identity = {"youtube_channel": channel}
    if channel:
        if channel == backup["manifest"]["source"].get("youtube_channel"):
            raise UserError("Source and destination channels are identical")
        console.print(f"Destination channel: {channel}")
    else:
        token = read(CONFIG / "destination_youtube_token.json").get("refresh_token")
        if not token:
            raise UserError("Re-run destination setup: no refresh token was saved")
        source_token = CONFIG / "source_youtube_token.json"
        if source_token.exists() and read(source_token).get("refresh_token") == token:
            raise UserError("Source and destination YouTube credentials are identical")
        identity["youtube_credential_fingerprint"] = hashlib.sha256(token.encode()).hexdigest()
        console.print(
            "No channel ID; subscription identity is bound to the saved OAuth credential."
        )
    return youtube, identity


def progress(message):
    console.print(message, markup=False)
    event("progress", message=message)


def scoped_backup(backup, state_path, subscriptions_only):
    only = subscriptions_only
    if only is None:
        only = State.peek(state_path, "migration_scope", "all") == "subscriptions"
    if not only:
        return backup
    count = len(backup["playlists"])
    console.print(f"Scope: subscriptions only; {count} playlist(s) excluded.")
    return {
        **backup,
        "playlists": [],
        "migration_scope": "subscriptions",
        "excluded_playlists": count,
    }


@contextmanager
def journaled_migration(backup, path, privacy=None):
    youtube, identity = destination(backup, True)
    with State(path) as state:
        state.bind(backup["manifest"]["generation"], identity)
        yield state, Migrator(backup, state, youtube, progress=progress, playlist_privacy=privacy)


def source_playlist(backup, source_id):
    for raw in backup["playlists"]:
        if raw["id"] == source_id:
            return Playlist.load(raw)
    raise InputError("Source playlist ID not in current export")


@app.command()
def setup(account: str = "source", reauth: bool = False):
    """Authenticate one account. --reauth opens consent again."""
    check_choice(account, ("source", "destination"))
    with operation(f"SETUP {account}"):
        credentials = youtube_credentials(
            CONFIG, account, account == "destination", True, force_reauth=reauth
        )
        client = YouTube(credentials)
        channel = client.identity(allow_missing=True)
        if channel:
            console.print(f"Authenticated channel: {channel}")
        else:
            count = len(client.subscriptions())
            console.print(
                f"Read access verified: {count} subscriptions; account aliases are unverified."
            )
            console.print(
                "Subscriptions work without a channel ID; playlist access may be limited."
            )


@app.command("export")
def export_command(
    subscriptions_only: bool = False,
    playlist_id: Annotated[
        list[str] | None, typer.Option(help="Additional playlist ID; repeatable")
    ] = None,
    watch_later: Path | None = None,
):
    """Read the source and save a new backup. Do not re-export during a migration."""
    with operation("EXPORT"):
        watch = None
        if watch_later:
            value = read(watch_later) if watch_later.suffix.lower() == ".json" else None
            watch = (
                Playlist.load(value)
                if isinstance(value, dict) and value.get("id") == "watch-later"
                else parse(watch_later)
            )
        result = Backup(EXPORTS).export(
            youtube=yt("source"),
            watch=watch,
            progress=progress,
            aliases={"source": os.getenv("SOURCE_ALIAS", "source")},
            playlist_ids=playlist_id,
            subscriptions_only=subscriptions_only,
        )
        console.print_json(data=result)


@app.command("watch-later")
def watch_later(path: Path):
    """Convert Takeout/CSV/JSON into data/imports/watch_later.json."""
    with operation():
        playlist = parse(path)
        atomic(Path("data/imports/watch_later.json"), playlist.dump())
        console.print(f"Parsed {len(playlist.entries)} entries. Include with export --watch-later.")


def execute_import(
    state_path,
    dry_run=False,
    max_subscriptions=None,
    continue_on_conflict=False,
    daily_quota=10_000,
    playlist_privacy=None,
    subscriptions_only=None,
):
    with operation("DRY RUN" if dry_run else "IMPORT / RESUME"):
        if playlist_privacy is not None:
            allowed_privacy(playlist_privacy)
        backup = scoped_backup(Backup(EXPORTS).load(), state_path, subscriptions_only)
        if backup["manifest"].get("completeness") == "partial":
            console.print("Source export is partial; see its manifest warnings.", style="yellow")
        if dry_run:
            privacy = playlist_privacy or State.peek(state_path, "playlist_privacy", "private")
            console.print_json(data=plan(backup, daily_quota, privacy))
            return
        with journaled_migration(backup, state_path, playlist_privacy) as (state, engine):
            state.set("migration_scope", backup.get("migration_scope", "all"))
            engine.max_subscriptions = max_subscriptions
            engine.continue_on_conflict = continue_on_conflict
            try:
                engine.run()
            finally:
                state.set("estimated_youtube_units_last_run", engine.youtube.caller.units)
            console.print(state.get("status"))


@app.command("import")
def import_command(
    state: StateOption = DEFAULT_STATE,
    dry_run: bool = False,
    max_subscriptions: MaxSubscriptions = None,
    continue_on_conflict: ContinueOnConflict = False,
    daily_quota: Annotated[int, typer.Option(min=1, help="Allocation assumed by dry-run")] = 10_000,
    playlist_privacy: PlaylistPrivacy = None,
    subscriptions_only: SubscriptionScope = None,
):
    """Import subscriptions and ordered playlists. New playlists are never public."""
    execute_import(
        state,
        dry_run,
        max_subscriptions,
        continue_on_conflict,
        daily_quota,
        playlist_privacy,
        subscriptions_only,
    )


@app.command()
def resume(
    state: StateOption = DEFAULT_STATE,
    max_subscriptions: MaxSubscriptions = None,
    continue_on_conflict: ContinueOnConflict = False,
    playlist_privacy: PlaylistPrivacy = None,
    subscriptions_only: SubscriptionScope = None,
):
    """Continue the same backup and journal after a pause."""
    execute_import(
        state,
        max_subscriptions=max_subscriptions,
        continue_on_conflict=continue_on_conflict,
        playlist_privacy=playlist_privacy,
        subscriptions_only=subscriptions_only,
    )


@app.command()
def verify(state: StateOption = DEFAULT_STATE, subscriptions_only: SubscriptionScope = None):
    """Check destination contents. Exit 2 for missing or unverifiable data."""
    with operation("VERIFY"):
        backup = scoped_backup(Backup(EXPORTS).load(), state, subscriptions_only)
        with journaled_migration(backup, state) as (_, engine):
            report = engine.verify()
        atomic(Path("data/reports/verification.json"), report)
        console.print(f"Verification scope: {report['scope']}")
        if report["source_export_completeness"] == "partial":
            console.print("Source coverage is partial; no full migration claim is possible.")
        console.print_json(data=report["summary"])
        console.print("Report: data/reports/verification.json")
        event("verify_complete", ok=bool(report["ok"]))
    if not report["ok"]:
        raise typer.Exit(2)


@app.command()
def status(state: StateOption = DEFAULT_STATE):
    """Show local state without contacting Google."""
    if not state.exists():
        console.print("No migration state yet")
        return
    with State(state) as journal:
        console.print_json(data=journal.summary())


@app.command("check-subscription")
def check_subscription(
    apply: Annotated[bool, typer.Option(help="Attempt one missing subscription")] = False,
    state: StateOption = DEFAULT_STATE,
):
    """Preview one missing source subscription. --apply attempts and verifies it."""
    with operation("SUBSCRIPTION CHECK" + (" / ONE WRITE" if apply else " / PREVIEW ONLY")):
        if not (EXPORTS / "manifest.json").exists():
            raise UserError("Run export first")
        backup = Backup(EXPORTS).load()
        if not backup["subscriptions"]:
            raise UserError("Current export contains no subscriptions")
        youtube, identity = destination(backup, True, allow_missing_channel=True)
        existing = youtube.subscriptions()
        channel = next((c for c in backup["subscriptions"] if c not in existing), None)
        console.print(f"Destination API-visible subscriptions: {len(existing)}")
        if channel is None:
            console.print("All exported subscriptions are already present; no write needed.")
            return
        console.print(f"Selected source channel: https://www.youtube.com/channel/{channel}")
        if not apply:
            console.print(
                "Preview only. Use check-subscription --apply to attempt one subscription."
            )
            return
        with State(state) as journal:
            journal.bind(backup["manifest"]["generation"], identity)
            selected = {**backup, "subscriptions": [channel], "playlists": []}
            engine = Migrator(selected, journal, youtube, progress=progress, max_subscriptions=1)
            try:
                engine.subscriptions()
            finally:
                journal.set("estimated_youtube_units_last_run", youtube.caller.units)
            if journal.get("subscription:" + channel, {}).get("status") not in {
                "migrated",
                "already_present",
            }:
                raise UserError("Subscription not confirmed; inspect the journal before continuing")
        console.print("API subscription write confirmed by destination read-back.")


@app.command("map-playlist")
def map_playlist(
    source_id: str,
    destination_id: str,
    state: StateOption = DEFAULT_STATE,
    playlist_privacy: PlaylistPrivacy = None,
):
    """Adopt an inspected empty private/unlisted playlist after an uncertain create."""
    with operation():
        backup = Backup(EXPORTS).load()
        with journaled_migration(backup, state, playlist_privacy) as (journal, engine):
            playlist = source_playlist(backup, source_id)
            old = journal.get("playlist:" + source_id, {})
            if old.get("destination") or old.get("done"):
                raise Conflict("Mapping already exists; refusing to replace its history")
            privacy = engine.target_privacy(playlist)
            remote = engine.youtube.playlist(destination_id)
            if remote.privacy != privacy:
                raise Conflict("Mapped playlist must have the selected non-public privacy")
            if remote.entries:
                raise Conflict("Only an inspected empty destination can be explicitly mapped")
            journal.set("playlist_privacy", engine.playlist_privacy)
            journal.set(
                "playlist:" + source_id,
                {
                    "destination": destination_id,
                    "privacy": privacy,
                    "done": [],
                    "failed": {},
                    "explicit_mapping": True,
                    "status": "mapped",
                },
            )
        console.print("Mapping saved. Run resume.")


@app.command("resolve-pending")
def resolve_pending(source_id: str, outcome: str, state: StateOption = DEFAULT_STATE):
    """After remote inspection, resolve an uncertain append as applied or absent."""
    check_choice(outcome, ("applied", "absent"))
    with operation():
        backup = Backup(EXPORTS).load()
        with journaled_migration(backup, state) as (journal, engine):
            record = journal.get("playlist:" + source_id, {})
            if "pending" not in record:
                raise InputError("No pending insertion for this source ID")
            playlist = source_playlist(backup, source_id)
            remote = engine.client().playlist(record["destination"])
            if remote.privacy != engine.target_privacy(playlist, record):
                raise Conflict("Destination privacy differs from the saved non-public policy")
            expected = [playlist.entries[i].video_id for i in record["done"]]
            if outcome == "applied":
                expected.append(playlist.entries[record["pending"]].video_id)
            if [entry.video_id for entry in remote.entries] != expected:
                raise Conflict("Remote sequence does not match the selected outcome")
            if outcome == "applied":
                record["done"].append(record["pending"])
            record.pop("pending")
            journal.set("playlist:" + source_id, record)
        console.print("Pending operation resolved. Run resume.")


@app.command("skip-subscription")
def skip_subscription(channel_id: str, state: StateOption = DEFAULT_STATE):
    """Skip a rejected channel locally; verification reports the omission."""
    with operation():
        backup = Backup(EXPORTS).load()
        if channel_id not in backup["subscriptions"]:
            raise InputError("Channel ID not in current export")
        with State(state) as journal:
            binding = journal.get("binding")
            if not binding or binding["backup"] != backup["manifest"]["generation"]:
                raise UserError("No journal for this export; run import first")
            old = journal.get("subscription:" + channel_id, {})
            if old.get("status") in {"migrated", "already_present"}:
                raise Conflict("Subscription already confirmed; nothing to skip")
            journal.set(
                "subscription:" + channel_id,
                {"status": "skipped", "reason": old.get("reason", "manual")},
            )
        console.print("Subscription will be skipped. Run resume.")


if __name__ == "__main__":
    app()
