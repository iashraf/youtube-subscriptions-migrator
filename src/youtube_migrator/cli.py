"""Docker-friendly CLI. No secrets are included in error output."""

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .authentication import music_client, youtube_credentials
from .backup import Backup
from .clients import Music, YouTube
from .errors import Caller, InputError, UserError
from .events import event
from .migration import Conflict, Migrator, plan, route
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
StateOption = Annotated[Path, typer.Option("--state", help="Persistent journal file")]
SubscribeVia = Annotated[
    str,
    typer.Option(
        "--subscribe-via",
        help="youtube: official API, 50 quota units each. music: ytmusicapi, no Data API "
        "quota, experimental. Both are confirmed by one Data API listing.",
    ),
]
MaxSubscriptions = Annotated[
    int | None,
    typer.Option("--max-subscriptions", help="Cap new subscription attempts in this run"),
]
ContinueOnConflict = Annotated[
    bool,
    typer.Option(
        "--continue-on-conflict",
        help="Skip a playlist or subscription batch that needs inspection and carry on",
    ),
]


def heading(operation: str):
    console.print(f"YouTube Migrator — {operation}", style="bold")
    event(operation)
    for role in ("source", "destination"):
        console.print(
            f"{role}: {os.getenv(role.upper() + '_ALIAS', role)} (configured alias)", markup=False
        )


def check_choice(value, choices):
    if value not in choices:
        raise typer.BadParameter(f"Expected one of {', '.join(choices)}")


def yt(account, write=False):
    return YouTube(
        youtube_credentials(CONFIG, account, write),
        caller=Caller(float(os.getenv("API_INTERVAL", "0.2"))),
    )


def music(account):
    return Music(
        music_client(CONFIG, account),
        progress=progress,
        caller=Caller(float(os.getenv("API_INTERVAL", "0.2"))),
    )


def fail(exc):
    # Third-party exception messages can contain OAuth secrets and HTTP request URLs.
    from .errors import APIError

    if isinstance(exc, (APIError, Conflict, FileNotFoundError, InputError, UserError)):
        console.print(str(exc), style="red", markup=False)
    else:
        console.print(
            f"{type(exc).__name__}: operation failed. Check authentication, permissions "
            "and connectivity; sensitive exception details suppressed.",
            style="red",
        )
    event("failure", error_type=type(exc).__name__)
    raise typer.Exit(1) from None


@app.command()
def setup(account: str = "source", service: str = "youtube"):
    """Authenticate one account and one service. Browser interaction is required."""
    check_choice(account, ("source", "destination"))
    check_choice(service, ("youtube", "music"))
    heading(f"SETUP {account} / {service}")
    try:
        with locked(Path("data/state/process.lock")):
            if service == "youtube":
                credentials = youtube_credentials(CONFIG, account, account == "destination", True)
                console.print(f"Authenticated channel: {YouTube(credentials).identity()}")
            else:
                client = Music(music_client(CONFIG, account, setup=True))
                console.print_json(data=client.identity())
                console.print("Music credentials saved for the account selected in your browser.")
    except Exception as exc:
        fail(exc)


@app.command("watch-later")
def watch_later(path: Path):
    """Extract CSV/JSON/Takeout Watch Later into a portable JSON playlist."""
    try:
        with locked(Path("data/state/process.lock")):
            p = parse(path)
            atomic(Path("data/imports/watch_later.json"), p.dump())
            console.print(f"Parsed {len(p.entries)} entries. Include using export --watch-later.")
    except Exception as exc:
        fail(exc)


@app.command("export")
def export_command(service: str = "all", watch_later: Path | None = None):
    """Create a new portable backup generation. Never writes to the source account."""
    check_choice(service, ("all", "youtube", "music"))
    heading("EXPORT")
    try:
        with locked(Path("data/state/process.lock")):
            watch = None
            if watch_later:
                value = read(watch_later) if watch_later.suffix.lower() == ".json" else None
                watch = (
                    Playlist.load(value)
                    if isinstance(value, dict) and value.get("id") == "watch-later"
                    else parse(watch_later)
                )
            result = Backup(EXPORTS).export(
                yt("source") if service in {"all", "youtube"} else None,
                music("source") if service in {"all", "music"} else None,
                watch,
                {"source": os.getenv("SOURCE_ALIAS", "source")},
                progress=progress,
            )
            console.print_json(data=result)
    except Exception as exc:
        fail(exc)


def destination(backup, write, subscribe_via="youtube"):
    services = backup["manifest"]["services"]
    youtube = (
        yt("destination", write=write)
        if any(s in services for s in ("youtube", "watch_later"))
        else None
    )
    need_music = "music" in services or (subscribe_via == "music" and backup["subscriptions"])
    music_api = music("destination") if need_music else None
    identity = {}
    if youtube:
        identity["youtube_channel"] = youtube.identity()
        if identity["youtube_channel"] == backup["manifest"]["source"].get("youtube_channel"):
            raise UserError("Source and destination channels are identical; select the new account")
        console.print(f"Destination verified channel: {identity['youtube_channel']}")
    if music_api:
        music_identity = music_api.identity()
        console.print("Destination Music account:")
        console.print_json(data=music_identity)
        source_handle = backup["manifest"]["source"].get("music", {}).get("channel_handle")
        if source_handle and source_handle == music_identity.get("channel_handle"):
            raise UserError(
                "Source and destination Music handles are identical; select new account"
            )
        info = read(CONFIG / "destination_music_auth.json")
        if "music" in services:
            # Bind to the Music handle where returned; it survives re-authentication. The
            # credential fingerprint (never the secret) is decisive only without a handle.
            stable = info.get("refresh_token") or json.dumps(info, sort_keys=True)
            fingerprint = hashlib.sha256(stable.encode()).hexdigest()
            identity["music_credential_fingerprint"] = fingerprint
            if music_identity.get("channel_handle"):
                identity["music_channel_handle"] = music_identity["channel_handle"]
            else:
                console.print(
                    "Music returned no handle: this journal is bound to the current Music "
                    "credential, and re-running Music setup will require a new --state.",
                    style="yellow",
                )
        source_file = CONFIG / "source_music_auth.json"
        if source_file.exists():
            source_info = read(source_file)
            if source_info.get("refresh_token") == info.get("refresh_token"):
                raise UserError("Source and destination Music credentials are identical")
    return youtube, music_api, identity


def progress(message: str) -> None:
    console.print(message, markup=False)
    event("progress", message=message)


def execute_import(
    state_path,
    dry_run=False,
    subscribe_via="youtube",
    max_subscriptions=None,
    continue_on_conflict=False,
    daily_quota=10_000,
):
    heading("DRY RUN" if dry_run else "IMPORT / RESUME")
    state = None
    try:
        with locked(Path("data/state/process.lock")):
            backup = Backup(EXPORTS).load()
            if dry_run:
                # Offline planning deliberately needs no destination write credentials.
                console.print_json(data=plan(backup, subscribe_via, daily_quota))
                return
            youtube, music_api, identity = destination(backup, True, subscribe_via)
            state = State(state_path)
            state.bind(backup["manifest"]["generation"], identity)
            engine = Migrator(
                backup,
                state,
                youtube,
                music_api,
                progress,
                subscribe_via=subscribe_via,
                max_subscriptions=max_subscriptions,
                continue_on_conflict=continue_on_conflict,
            )
            try:
                engine.run()
            finally:
                if youtube:
                    state.set("estimated_youtube_units_last_run", youtube.caller.units)
            console.print(state.get("status"))
    except Exception as exc:
        fail(exc)
    finally:
        if state:
            state.close()


@app.command("import")
def import_command(
    state: StateOption = DEFAULT_STATE,
    dry_run: bool = False,
    subscribe_via: SubscribeVia = "youtube",
    max_subscriptions: MaxSubscriptions = None,
    continue_on_conflict: ContinueOnConflict = False,
    daily_quota: Annotated[
        int, typer.Option(help="Daily Data API allocation assumed by --dry-run")
    ] = 10_000,
):
    """Restore current export into destination, persisting every operation."""
    check_choice(subscribe_via, ("youtube", "music"))
    execute_import(
        state, dry_run, subscribe_via, max_subscriptions, continue_on_conflict, daily_quota
    )


@app.command()
def resume(
    state: StateOption = DEFAULT_STATE,
    subscribe_via: SubscribeVia = "youtube",
    max_subscriptions: MaxSubscriptions = None,
    continue_on_conflict: ContinueOnConflict = False,
):
    """Resume the current export using the same destination and journal."""
    check_choice(subscribe_via, ("youtube", "music"))
    execute_import(state, False, subscribe_via, max_subscriptions, continue_on_conflict)


@app.command()
def verify(state: StateOption = DEFAULT_STATE):
    """Read remote data and report discrepancies. Exit 2 if verification is incomplete."""
    heading("VERIFY")
    db = None
    try:
        with locked(Path("data/state/process.lock")):
            backup = Backup(EXPORTS).load()
            # Destination's write-scoped token also permits reads; never asks for new consent here.
            youtube, music_api, identity = destination(backup, True)
            db = State(state)
            db.bind(backup["manifest"]["generation"], identity)
            report = Migrator(backup, db, youtube, music_api).verify()
            atomic(Path("data/reports/verification.json"), report)
            console.print("Migration Verification")
            console.print_json(data=report["summary"])
            event("verify_complete", ok=bool(report["ok"]))
            console.print("Full report: data/reports/verification.json")
    except Exception as exc:
        fail(exc)
    finally:
        if db:
            db.close()
    if not report["ok"]:
        raise typer.Exit(2)


@app.command()
def status(state: StateOption = DEFAULT_STATE):
    """Show local progress; does not authenticate or contact Google."""
    if not state.exists():
        console.print("No migration state yet")
        return
    db = State(state)
    try:
        console.print_json(data=db.summary())
    finally:
        db.close()


@app.command("map-playlist")
def map_playlist(source_id: str, destination_id: str, state: StateOption = DEFAULT_STATE):
    """Explicitly map an inspected destination after an ambiguous create.

    Only an empty playlist can be adopted. Existing journalled entries are never reset.
    """
    db = None
    try:
        with locked(Path("data/state/process.lock")):
            backup = Backup(EXPORTS).load()
            youtube, music_api, identity = destination(backup, True)
            db = State(state)
            db.bind(backup["manifest"]["generation"], identity)
            p = next((Playlist.load(p) for p in backup["playlists"] if p["id"] == source_id), None)
            if not p:
                raise InputError("Source playlist ID not in current export")
            key = "playlist:" + source_id
            old = db.get(key)
            if old and (old.get("destination") or old.get("done")):
                raise Conflict("Mapping already exists; refusing to replace its migration history")
            via = route(p, music_api is not None, youtube is not None)
            remote = (
                Migrator(backup, db, youtube, music_api).client(p, via).playlist(destination_id)
            )
            if remote.entries:
                raise Conflict("Only an inspected empty destination can be explicitly mapped")
            db.set(
                key,
                {
                    "destination": destination_id,
                    "via": via,
                    "done": [],
                    "failed": {},
                    "explicit_mapping": True,
                    "status": "mapped",
                },
            )
            console.print("Mapping saved. Run resume.")
    except Exception as exc:
        fail(exc)
    finally:
        if db:
            db.close()


@app.command("resolve-pending")
def resolve_pending(source_id: str, outcome: str, state: StateOption = DEFAULT_STATE):
    """After manual inspection, resolve an uncertain append as applied or absent.

    For absent, wait for remote consistency and confirm no write can still commit.
    """
    check_choice(outcome, ("applied", "absent"))
    db = None
    try:
        with locked(Path("data/state/process.lock")):
            backup = Backup(EXPORTS).load()
            youtube, music_api, identity = destination(backup, True)
            db = State(state)
            db.bind(backup["manifest"]["generation"], identity)
            key = "playlist:" + source_id
            record = db.get(key, {})
            if "pending" not in record:
                raise InputError("No pending insertion for this source ID")
            p = next(Playlist.load(p) for p in backup["playlists"] if p["id"] == source_id)
            remote = (
                Migrator(backup, db, youtube, music_api).client(p).playlist(record["destination"])
            )
            expected = [p.entries[i].video_id for i in record["done"]]
            if outcome == "applied":
                expected.append(p.entries[record["pending"]].video_id)
            if [e.video_id for e in remote.entries] != expected:
                raise Conflict("Remote sequence does not match the selected outcome")
            if outcome == "applied":
                record["done"].append(record["pending"])
            record.pop("pending")
            db.set(key, record)
            console.print("Pending operation resolved. Run resume.")
    except Exception as exc:
        fail(exc)
    finally:
        if db:
            db.close()


@app.command("skip-subscription")
def skip_subscription(channel_id: str, state: StateOption = DEFAULT_STATE):
    """Deliberately skip a subscription the destination keeps rejecting.

    Local only. Verification reports it as skipped, so the migration is not counted as complete.
    """
    db = None
    try:
        with locked(Path("data/state/process.lock")):
            backup = Backup(EXPORTS).load()
            if channel_id not in backup["subscriptions"]:
                raise InputError("Channel ID not in current export's subscriptions")
            db = State(state)
            binding = db.get("binding")
            if not binding or binding["backup"] != backup["manifest"]["generation"]:
                raise UserError("No journal for the current export; run import first")
            key = "subscription:" + channel_id
            old = db.get(key, {})
            if old.get("status") in {"migrated", "already_present"}:
                raise Conflict("Subscription already confirmed; nothing to skip")
            db.set(key, {"status": "skipped", "reason": old.get("reason", "manual")})
            console.print("Subscription will be skipped. Run resume.")
    except Exception as exc:
        fail(exc)
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    app()
