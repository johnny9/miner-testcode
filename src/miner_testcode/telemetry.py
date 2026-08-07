from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

from .artifacts import append_jsonl

CHART_LEVEL = 25
CHART_LEVEL_NAME = "CHART"


@dataclass(frozen=True, slots=True)
class TelemetryMetric:
    key: str
    label: str
    unit: str


STANDARD_MINING_METRICS = (
    TelemetryMetric("hashrate_ghs", "Hashrate", "GH/s"),
    TelemetryMetric("temperature_c", "Temperature", "°C"),
    TelemetryMetric("frequency_mhz", "Frequency", "MHz"),
    TelemetryMetric("fan_rpm", "Fan speed", "RPM"),
)


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class TelemetryCapture:
    """Thread-safe, device-neutral telemetry samples and chart annotations."""

    def __init__(
        self,
        metrics: tuple[TelemetryMetric, ...],
        *,
        event_path: Path,
        started_at: float | None = None,
        max_samples: int = 50_000,
    ) -> None:
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        keys = [metric.key for metric in metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("telemetry metric keys must be unique")
        self.metrics = metrics
        self.event_path = event_path
        self.started_at = started_at if started_at is not None else time.time()
        self.max_samples = max_samples
        self._metric_keys = frozenset(keys)
        self._samples: list[dict[str, object]] = []
        self._markers: list[dict[str, object]] = []
        self._dropped_samples = 0
        self._lock = threading.RLock()

    def record_sample(
        self,
        values: Mapping[str, float | int],
        *,
        source: str,
        observed_at: float | None = None,
    ) -> bool:
        clean: dict[str, float] = {}
        for key, value in values.items():
            if key not in self._metric_keys or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                clean[key] = number
        if not clean:
            return False

        at = observed_at if observed_at is not None else time.time()
        sample: dict[str, object] = {
            "at": at,
            "source": str(source)[:32],
            "values": clean,
        }
        with self._lock:
            if len(self._samples) < self.max_samples:
                self._samples.append(sample)
            else:
                # Preserve the beginning of a long test and its latest state.
                self._samples[-1] = sample
                self._dropped_samples += 1
        append_jsonl(
            self.event_path,
            {"event": "telemetry_sample", **sample},
            lock=self._lock,
        )
        return True

    def add_marker(
        self,
        label: str,
        *,
        observed_at: float | None = None,
        level: str = CHART_LEVEL_NAME,
    ) -> bool:
        clean_label = " ".join(str(label).split())[:240]
        if not clean_label:
            return False
        marker: dict[str, object] = {
            "at": observed_at if observed_at is not None else time.time(),
            "label": clean_label,
            "level": level[:32],
        }
        with self._lock:
            self._markers.append(marker)
        append_jsonl(
            self.event_path,
            {"event": "chart_marker", **marker},
            lock=self._lock,
        )
        return True

    def to_dict(self, *, max_samples: int = 2_000) -> dict[str, object]:
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        with self._lock:
            samples = [dict(sample) for sample in self._samples]
            markers = [dict(marker) for marker in self._markers]
            dropped_samples = self._dropped_samples

        if len(samples) > max_samples:
            if max_samples == 1:
                published_samples = [samples[-1]]
            else:
                indices = [
                    round(index * (len(samples) - 1) / (max_samples - 1))
                    for index in range(max_samples)
                ]
                published_samples = [samples[index] for index in indices]
            dropped_samples += len(samples) - len(published_samples)
            samples = published_samples

        def elapsed(at: object) -> float:
            return round(max(0.0, float(at) - self.started_at), 3)

        public_samples = [
            {
                "elapsed_seconds": elapsed(sample["at"]),
                "source": sample["source"],
                "values": sample["values"],
            }
            for sample in samples
        ]
        public_markers = [
            {
                "elapsed_seconds": elapsed(marker["at"]),
                "label": marker["label"],
                "level": marker["level"],
            }
            for marker in markers
        ]
        end_times = [
            *(float(sample["at"]) for sample in samples),
            *(float(marker["at"]) for marker in markers),
        ]
        finished_at = max(end_times, default=self.started_at)
        return {
            "version": 1,
            "started_at": _iso_timestamp(self.started_at),
            "duration_seconds": round(max(0.0, finished_at - self.started_at), 3),
            "metrics": [asdict(metric) for metric in self.metrics],
            "samples": public_samples,
            "markers": public_markers,
            "dropped_samples": dropped_samples,
        }


class ChartMarkerHandler(logging.Handler):
    """Turns only CHART-level records into published vertical annotations."""

    def __init__(
        self,
        capture: TelemetryCapture,
        *,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(level=CHART_LEVEL)
        self.capture = capture
        self.sanitize = sanitize or (lambda value: value)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno != CHART_LEVEL:
            return
        try:
            self.capture.add_marker(
                self.sanitize(record.getMessage()),
                observed_at=record.created,
                level=record.levelname,
            )
        except Exception:
            self.handleError(record)


def log_chart(logger: logging.Logger, message: str, *args, **kwargs) -> None:
    logger.log(CHART_LEVEL, message, *args, **kwargs)


logging.addLevelName(CHART_LEVEL, CHART_LEVEL_NAME)
