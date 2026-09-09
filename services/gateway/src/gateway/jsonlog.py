"""One JSON line per event on stdout (spec §9.3): metadata only, never text."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Protocol


class Log(Protocol):
    def __call__(self, evt: str, /, **fields: object) -> None: ...


def stdout_log(evt: str, /, **fields: object) -> None:
    line: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "evt": evt,
        **fields,
    }
    sys.stdout.write(json.dumps(line, default=str, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def null_log(evt: str, /, **fields: object) -> None:
    return None
