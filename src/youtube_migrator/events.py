"""Small structured local event log. Callers supply only non-secret progress fields."""

import json
from datetime import UTC, datetime
from pathlib import Path


def event(operation: str, **fields: str | int | bool) -> None:
    path = Path("logs/events.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"time": datetime.now(UTC).isoformat(), "operation": operation, **fields},
                ensure_ascii=False,
            )
            + "\n"
        )
