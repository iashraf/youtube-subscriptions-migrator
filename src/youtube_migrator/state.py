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

    @staticmethod
    def peek(path: Path, key: str, default=None):
        """Read a saved option without creating a journal during preflight/dry-run."""
        if not path.exists():
            return default
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        finally:
            db.close()

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
        if old and old != binding:
            raise UserError(
                "State belongs to another export or destination. Use a new --state file"
            )
        # Persist the current, validated destination binding.
        self.set("binding", binding)

    def summary(self):
        return {
            key: json.loads(value) for key, value in self.db.execute("SELECT key,value FROM kv")
        }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.db.close()
