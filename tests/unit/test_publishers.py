from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miner_testcode.publishers import (
    GithubCheckPublisher,
    LocalHtmlPublisher,
    MiningQaStatusPublisher,
    PublisherManager,
    PublishError,
)
from miner_testcode.results import RunSummary, TestCodeRecord, TestRecord


class FakeTransport:
    def __init__(self) -> None:
        self.json_calls: list[dict] = []
        self.uploads: list[dict] = []

    def json_request(
        self, method, url, body, *, token=None, headers=None, timeout=20.0
    ):
        self.json_calls.append(
            {
                "method": method,
                "url": url,
                "body": body,
                "token": token,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if url.endswith("/check-runs"):
            return {"id": 42, "html_url": "https://github.example/checks/42"}
        if url.endswith("/api/v1/results"):
            return {
                "created": True,
                "result": {"id": "11111111-1111-1111-1111-111111111111"},
            }
        if url.endswith("/api/v1/artifacts/upload-url"):
            return {
                "artifact_id": "22222222-2222-2222-2222-222222222222",
                "signed_url": "https://storage.example/upload",
            }
        if url.endswith("/api/v1/artifacts/complete"):
            return {"uploaded_at": "2026-08-05T00:00:00Z"}
        raise AssertionError(f"unexpected URL: {url}")

    def put_file(self, url, path, *, content_type, timeout=120.0) -> None:
        self.uploads.append(
            {
                "url": url,
                "path": path,
                "content_type": content_type,
                "timeout": timeout,
            }
        )


class FailingTransport(FakeTransport):
    def json_request(self, *args, **kwargs):
        raise PublishError("simulated publication failure")


def make_summary(root: Path, *, successful: bool = True) -> RunSummary:
    case = root / "001-device-test"
    case.mkdir()
    (case / "test.log").write_text("test output\n", encoding="utf-8")
    (root / "runner.log").write_text("runner output\n", encoding="utf-8")
    return RunSummary(
        run_id="20260805T000000Z",
        artifact_root=root,
        started_at=100.0,
        finished_at=104.25,
        devices=({"name": "bonanza", "type": "bitaxe_bonanza"},),
        tests=(
            TestRecord(
                test_id="tests.PublicPoolSmoke.test_mines",
                device="bonanza",
                outcome="passed" if successful else "failed",
                elapsed_seconds=4.0,
                detail=None if successful else "assertion failed",
                artifact_dir=case.name,
                source_path="tests/e2e/test_public_pool_smoke.py",
                source_line=22,
                source_url=(
                    "https://github.com/owner/miner-testcode/blob/"
                    "abcdef0123456789abcdef0123456789abcdef01/"
                    "tests/e2e/test_public_pool_smoke.py#L22"
                ),
                telemetry={
                    "version": 1,
                    "started_at": "1970-01-01T00:01:40.000Z",
                    "duration_seconds": 4.0,
                    "metrics": [
                        {"key": "hashrate_ghs", "label": "Hashrate", "unit": "GH/s"},
                        {"key": "temperature_c", "label": "Temperature", "unit": "°C"},
                        {"key": "frequency_mhz", "label": "Frequency", "unit": "MHz"},
                        {"key": "fan_rpm", "label": "Fan speed", "unit": "RPM"},
                    ],
                    "samples": [
                        {
                            "elapsed_seconds": 0.0,
                            "source": "websocket",
                            "values": {
                                "hashrate_ghs": 1000.0,
                                "temperature_c": 50.0,
                                "frequency_mhz": 800.0,
                                "fan_rpm": 3000.0,
                            },
                        },
                        {
                            "elapsed_seconds": 2.0,
                            "source": "api",
                            "values": {},
                            "gap": True,
                        },
                        {
                            "elapsed_seconds": 4.0,
                            "source": "websocket",
                            "values": {
                                "hashrate_ghs": 1200.0,
                                "temperature_c": 52.0,
                                "frequency_mhz": 800.0,
                                "fan_rpm": 3200.0,
                            },
                        },
                    ],
                    "markers": [
                        {
                            "elapsed_seconds": 2.0,
                            "label": "Pool configured",
                            "level": "CHART",
                            "status": "good",
                        },
                        {
                            "elapsed_seconds": 2.01,
                            "label": "Restore started",
                            "level": "CHART",
                            "status": "info",
                        }
                    ],
                    "dropped_samples": 0,
                },
            ),
        ),
        tests_run=1,
        failures=0 if successful else 1,
        errors=0,
        skipped=0,
        expected_failures=0,
        unexpected_successes=0,
        successful=successful,
        test_code=TestCodeRecord(
            repository="owner/miner-testcode",
            commit_sha="abcdef0123456789abcdef0123456789abcdef01",
            url="https://github.com/owner/miner-testcode",
            published=True,
        ),
        orchestration={
            "gate_id": "firmware-smoke",
            "gate_run_id": "gate-run-1",
            "assignment_id": "assignment-1",
            "trigger": {"type": "pull_request"},
        },
    )


def add_cumulative_module_record(summary: RunSummary) -> None:
    first = summary.tests[0]
    telemetry = json.loads(json.dumps(first.telemetry))
    telemetry["duration_seconds"] = 5.0
    telemetry["samples"].append(
        {
            "elapsed_seconds": 5.0,
            "source": "websocket",
            "values": {"hashrate_ghs": 1250.0},
        }
    )
    telemetry["markers"].append(
        {
            "elapsed_seconds": 5.0,
            "label": "test_next passed",
            "level": "CHART",
            "status": "good",
        }
    )
    summary.tests = (
        first,
        TestRecord(
            test_id="tests.PublicPoolSmoke.test_next",
            device=first.device,
            outcome="passed",
            elapsed_seconds=1.0,
            telemetry=telemetry,
        ),
    )
    summary.tests_run = 2


def quiet_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


class LocalPublisherTest(unittest.TestCase):
    def test_writes_html_json_and_relative_artifact_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = make_summary(Path(directory))
            publisher = LocalHtmlPublisher(
                {"enabled": True, "filename": "report.html", "json_filename": "result.json"}
            )
            result = publisher.publish(summary)
            report = (summary.artifact_root / "report.html").read_text(encoding="utf-8")
            payload = json.loads((summary.artifact_root / "result.json").read_text())

        self.assertTrue(result.success)
        self.assertEqual(result.url, "report.html")
        self.assertIn("tests.PublicPoolSmoke.test_mines", report)
        self.assertIn("001-device-test/test.log", report)
        self.assertIn("tests/e2e/test_public_pool_smoke.py#L22", report)
        self.assertIn("owner/miner-testcode@abcdef012345", report)
        self.assertIn("Mining telemetry time series", report)
        self.assertIn("Pool configured", report)
        self.assertIn("marker--good", report)
        self.assertIn('cy="36.00"', report)
        self.assertIn("Hashrate", report)
        self.assertNotIn(str(summary.artifact_root), report)
        self.assertNotIn("file://", report)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["test_code"]["repository"], "owner/miner-testcode")

    def test_renders_one_telemetry_chart_per_test_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = make_summary(Path(directory))
            add_cumulative_module_record(summary)
            LocalHtmlPublisher({"enabled": True}).publish(summary)
            report = (summary.artifact_root / "report.html").read_text(encoding="utf-8")

        self.assertEqual(report.count('<section class="telemetry">'), 1)
        self.assertIn("test_next passed", report)
        self.assertIn("<code>tests</code>", report)

    def test_manager_refreshes_html_with_remote_publisher_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = make_summary(Path(directory))
            transport = FakeTransport()
            with patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "installation-token",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
                },
                clear=False,
            ):
                ok = PublisherManager(
                    {
                        "local": {"enabled": True},
                        "github": {"enabled": True},
                    },
                    logger=quiet_logger("publisher-test.remote-success"),
                    transport=transport,
                ).publish(summary)
            report = (summary.artifact_root / "report.html").read_text(encoding="utf-8")

        self.assertTrue(ok)
        self.assertIn("github", report)
        self.assertIn("https://github.example/checks/42", report)

    def test_best_effort_remote_failure_does_not_fail_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = make_summary(Path(directory))
            with patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "installation-token",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
                },
                clear=False,
            ):
                ok = PublisherManager(
                    {
                        "local": {"enabled": True},
                        "github": {"enabled": True, "required": False},
                    },
                    logger=quiet_logger("publisher-test.best-effort"),
                    transport=FailingTransport(),
                ).publish(summary)
            payload = json.loads((summary.artifact_root / "result.json").read_text())

        self.assertTrue(ok)
        github = next(item for item in payload["publishers"] if item["name"] == "github")
        self.assertFalse(github["success"])
        self.assertFalse(github["required"])


class RemotePublisherTest(unittest.TestCase):
    def test_creates_completed_github_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = make_summary(Path(directory), successful=False)
            transport = FakeTransport()
            with patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "installation-token",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
                },
                clear=False,
            ):
                result = GithubCheckPublisher(
                    {"enabled": True}, transport=transport
                ).publish(summary, details_url="https://qa.example/results/1")

        payload = transport.json_calls[0]["body"]
        self.assertTrue(result.success)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["conclusion"], "failure")
        self.assertEqual(payload["details_url"], "https://qa.example/results/1")
        self.assertIn(
            "tests/e2e/test_public_pool_smoke.py#L22",
            payload["output"]["summary"],
        )
        self.assertNotIn("installation-token", json.dumps(payload))

    def test_publishes_mining_qa_result_and_signed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = make_summary(Path(directory))
            LocalHtmlPublisher({"enabled": True}).publish(summary)
            transport = FakeTransport()
            with patch.dict(
                "os.environ",
                {
                    "MINING_QA_TOKEN": "mqa-secret",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
                },
                clear=False,
            ):
                result = MiningQaStatusPublisher(
                    {
                        "enabled": True,
                        "base_url": "https://qa.example",
                        "artifact_globs": ["result.json", "**/test.log"],
                    },
                    transport=transport,
                ).publish(summary)

        result_payload = transport.json_calls[0]["body"]
        reservations = [
            call for call in transport.json_calls if call["url"].endswith("upload-url")
        ]
        completions = [
            call for call in transport.json_calls if call["url"].endswith("complete")
        ]
        self.assertTrue(result.success)
        self.assertEqual(result_payload["status"], "passed")
        self.assertEqual(result_payload["details"]["checks"][0]["passed"], True)
        self.assertIn(
            "tests/e2e/test_public_pool_smoke.py#L22",
            result_payload["details"]["checks"][0]["url"],
        )
        self.assertEqual(
            result_payload["details"]["test_code"]["repository"],
            "owner/miner-testcode",
        )
        self.assertEqual(
            result_payload["details"]["telemetry"][0]["markers"][0]["label"],
            "Pool configured",
        )
        self.assertEqual(
            result_payload["details"]["orchestration"]["gate_run_id"],
            "gate-run-1",
        )
        self.assertNotIn(
            "telemetry", result_payload["details"]["result"]["tests"][0]
        )
        self.assertNotIn(str(summary.artifact_root), json.dumps(result_payload))
        self.assertEqual(len(reservations), 2)
        self.assertEqual(len(transport.uploads), 2)
        self.assertEqual(len(completions), 2)
        self.assertEqual(result.url, "https://qa.example/results/11111111-1111-1111-1111-111111111111")

    def test_publishes_one_telemetry_series_per_test_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = make_summary(Path(directory))
            add_cumulative_module_record(summary)
            transport = FakeTransport()
            with patch.dict(
                "os.environ",
                {
                    "MINING_QA_TOKEN": "mqa-secret",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
                },
                clear=False,
            ):
                MiningQaStatusPublisher(
                    {
                        "enabled": True,
                        "base_url": "https://qa.example",
                        "upload_artifacts": False,
                    },
                    transport=transport,
                ).publish(summary)

        telemetry = transport.json_calls[0]["body"]["details"]["telemetry"]
        self.assertEqual(len(telemetry), 1)
        self.assertEqual(telemetry[0]["test_id"], "tests")
        self.assertEqual(telemetry[0]["markers"][-1]["label"], "test_next passed")
