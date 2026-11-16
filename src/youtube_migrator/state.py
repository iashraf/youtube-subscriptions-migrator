"""Transactional journal. Pending writes survive process and container termination."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .errors import UserError


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
            self.db.execute(
                "INSERT OR REPLACE INTO kv VALUES ('last_operation',?)",
                (json.dumps({"key": key, "time": datetime.now(UTC).isoformat()}),),
            )

    def bind(self, backup_id: str, destination: dict):
        binding = {"backup": backup_id, "destination": destination}
        old = self.get("binding")
        if old and not same_binding(old, binding):
            raise UserError(
                "State belongs to another export or destination. Use a new --state file"
            )
        # Re-authentication changes the Music fingerprint; record the current one.
        self.set("binding", binding)

    def summary(self):
        return {
            key: json.loads(value) for key, value in self.db.execute("SELECT key,value FROM kv")
        }

    def close(self):
        self.db.close()


def same_binding(old: dict, new: dict) -> bool:
    """A matching Music handle outranks the credential fingerprint.

    Testing-mode refresh tokens expire after seven days, so re-running Music setup must not
    orphan a multi-day migration. The fingerprint is only decisive when no handle is returned.
    """
    old_dest, new_dest = dict(old["destination"]), dict(new["destination"])
    handle = old_dest.get("music_channel_handle")
    if handle and handle == new_dest.get("music_channel_handle"):
        old_dest.pop("music_credential_fingerprint", None)
        new_dest.pop("music_credential_fingerprint", None)
    return old["backup"] == new["backup"] and old_dest == new_dest
