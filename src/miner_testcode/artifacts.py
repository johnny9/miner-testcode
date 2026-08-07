from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(value: str) -> str:
    cleaned = _SAFE_NAME.sub("_", value).strip("._")
    return cleaned or "unnamed"


@dataclass(frozen=True, slots=True)
class TestArtifacts:
    path: Path
    events_path: Path
    state_path: Path
    serial_path: Path
    api_trace_path: Path

    @classmethod
    def create(cls, path: Path) -> "TestArtifacts":
        path.mkdir(parents=True, exist_ok=False)
        return cls(
            path=path,
            events_path=path / "events.jsonl",
            state_path=path / "device-state.jsonl",
            serial_path=path / "serial.log",
            api_trace_path=path / "api.jsonl",
        )


class RunArtifacts:
    def __init__(self, root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.path = root / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.runner_log = self.path / "runner.log"
        self.events_path = self.path / "events.jsonl"
        self._lock = threading.Lock()
        self._ordinal = 0

    @classmethod
    def create(cls, root: Path) -> "RunArtifacts":
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        return cls(root, timestamp)

    def for_test(self, device_name: str, test_id: str) -> TestArtifacts:
        with self._lock:
            self._ordinal += 1
            ordinal = self._ordinal
        dirname = f"{ordinal:03d}-{safe_name(device_name)}-{safe_name(test_id)}"
        return TestArtifacts.create(self.path / dirname)

    def append_event(self, event: dict[str, Any]) -> None:
        append_jsonl(self.events_path, event, lock=self._lock)


def append_jsonl(
    path: Path, event: dict[str, Any], *, lock: threading.Lock | None = None
) -> None:
    line = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    if lock is None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
