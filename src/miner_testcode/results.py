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
    source_path: str | None = None
    source_line: int | None = None
    source_url: str | None = None
    telemetry: dict[str, Any] | None = None

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


@dataclass(frozen=True, slots=True)
class TestCodeRecord:
    repository: str
    commit_sha: str
    url: str
    dirty: bool = False
    published: bool = False


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
    test_code: TestCodeRecord | None = None
    orchestration: dict[str, Any] | None = None
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

    def telemetry_series(self) -> tuple[dict[str, Any], ...]:
        """Return the richest telemetry snapshot for each device and test module."""

        selected: dict[
            tuple[str, str], tuple[tuple[float, int, int], dict[str, Any]]
        ] = {}
        for record in self.tests:
            telemetry = record.telemetry
            if not isinstance(telemetry, dict):
                continue
            parts = record.test_id.rsplit(".", 2)
            module_id = parts[0] if len(parts) == 3 else record.test_id
            samples = telemetry.get("samples")
            markers = telemetry.get("markers")
            score = (
                float(telemetry.get("duration_seconds") or 0.0),
                len(samples) if isinstance(samples, list) else 0,
                len(markers) if isinstance(markers, list) else 0,
            )
            series = {
                "test_id": module_id,
                "device": record.device,
                **telemetry,
            }
            key = (record.device, module_id)
            current = selected.get(key)
            if current is None or score >= current[0]:
                selected[key] = (score, series)
        return tuple(series for _, series in selected.values())

    def to_dict(
        self,
        *,
        detail_limit: int | None = None,
        include_telemetry: bool = True,
    ) -> dict[str, Any]:
        tests: list[dict[str, Any]] = []
        for record in self.tests:
            item = asdict(record)
            if detail_limit is not None and item["detail"]:
                item["detail"] = item["detail"][:detail_limit]
            if not include_telemetry:
                item.pop("telemetry", None)
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
            "test_code": asdict(self.test_code) if self.test_code else None,
            "orchestration": self.orchestration,
            "tests": tests,
            "publishers": [asdict(record) for record in self.publishers],
        }

    def write_json(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
