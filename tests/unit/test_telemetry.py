from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from miner_testcode.telemetry import (
    CHART_LEVEL,
    STANDARD_MINING_METRICS,
    ChartMarkerHandler,
    TelemetryCapture,
)


class TelemetryCaptureTest(unittest.TestCase):
    def test_normalizes_samples_and_chart_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "telemetry.jsonl"
            capture = TelemetryCapture(
                STANDARD_MINING_METRICS,
                event_path=event_path,
                started_at=100.0,
            )
            capture.record_sample(
                {
                    "hashrate_ghs": 1234.5,
                    "temperature_c": 52,
                    "unknown": 99,
                    "fan_rpm": float("inf"),
                },
                source="websocket",
                observed_at=101.25,
            )
            capture.add_marker(
                "  Pool   configured  ", observed_at=101.5, status="good"
            )
            report = capture.to_dict()
            events = [json.loads(line) for line in event_path.read_text().splitlines()]

        self.assertEqual(report["version"], 1)
        self.assertEqual(report["samples"][0]["elapsed_seconds"], 1.25)
        self.assertEqual(
            report["samples"][0]["values"],
            {"hashrate_ghs": 1234.5, "temperature_c": 52.0},
        )
        self.assertEqual(report["markers"][0]["label"], "Pool configured")
        self.assertEqual(report["markers"][0]["elapsed_seconds"], 1.5)
        self.assertEqual(report["markers"][0]["status"], "good")
        self.assertEqual([event["event"] for event in events], [
            "telemetry_sample",
            "chart_marker",
        ])

    def test_chart_handler_only_captures_chart_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = TelemetryCapture(
                STANDARD_MINING_METRICS,
                event_path=Path(directory) / "telemetry.jsonl",
                started_at=100.0,
            )
            handler = ChartMarkerHandler(
                capture, sanitize=lambda value: value.replace("private", "public")
            )
            logger = logging.getLogger("telemetry-marker-test")
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            logger.info("ordinary log")
            logger.log(CHART_LEVEL, "private moment")
            logger.log(
                CHART_LEVEL,
                "private success",
                extra={"chart_status": "good"},
            )
            report = capture.to_dict()

        self.assertEqual(len(report["markers"]), 2)
        self.assertEqual(report["markers"][0]["label"], "public moment")
        self.assertEqual(report["markers"][0]["level"], "CHART")
        self.assertEqual(report["markers"][0]["status"], "info")
        self.assertEqual(report["markers"][1]["status"], "good")

    def test_downsamples_published_series_but_preserves_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = TelemetryCapture(
                STANDARD_MINING_METRICS,
                event_path=Path(directory) / "telemetry.jsonl",
                started_at=100.0,
            )
            for index in range(10):
                capture.record_sample(
                    {"hashrate_ghs": index},
                    source="websocket",
                    observed_at=100.0 + index,
                )
            report = capture.to_dict(max_samples=4)

        self.assertEqual(len(report["samples"]), 4)
        self.assertEqual(report["samples"][0]["values"]["hashrate_ghs"], 0.0)
        self.assertEqual(report["samples"][-1]["values"]["hashrate_ghs"], 9.0)
        self.assertEqual(report["dropped_samples"], 6)
