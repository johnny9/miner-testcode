from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True, slots=True)
class TestRecord:
    test_id: str
    device: str
    outcome: str
    elapsed_seconds: float
    detail: str | None = None
    artifact_dir: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome in {"passed", "expected_failure"}


@dataclass(frozen=True, slots=True)
class PublisherRecord:
    name: str
    success: bool
    required: bool
    url: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class RunSummary:
    run_id: str
    artifact_root: Path
    started_at: float
    finished_at: float
    devices: tuple[dict[str, str], ...]
    tests: tuple[TestRecord, ...]
    tests_run: int
    failures: int
    errors: int
    skipped: int
    expected_failures: int
    unexpected_successes: int
    successful: bool
    publishers: list[PublisherRecord] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def passed_count(self) -> int:
        return sum(record.passed for record in self.tests)

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.failures or self.unexpected_successes:
            return "failed"
        if self.tests_run and self.skipped == self.tests_run:
            return "skipped"
        return "passed" if self.successful else "failed"

    def to_dict(self, *, detail_limit: int | None = None) -> dict[str, Any]:
        tests: list[dict[str, Any]] = []
        for record in self.tests:
            item = asdict(record)
            if detail_limit is not None and item["detail"]:
                item["detail"] = item["detail"][:detail_limit]
            tests.append(item)
        return {
            "run_id": self.run_id,
            "status": self.status,
            "successful": self.successful,
            "started_at": iso_timestamp(self.started_at),
            "finished_at": iso_timestamp(self.finished_at),
            "duration_ms": round(self.duration_seconds * 1000),
            "counts": {
                "run": self.tests_run,
                "passed": self.passed_count,
                "failures": self.failures,
                "errors": self.errors,
                "skipped": self.skipped,
                "expected_failures": self.expected_failures,
                "unexpected_successes": self.unexpected_successes,
            },
            "devices": list(self.devices),
            "tests": tests,
            "publishers": [asdict(record) for record in self.publishers],
        }

    def write_json(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
