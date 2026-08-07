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
    )


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
        self.assertNotIn(str(summary.artifact_root), report)
        self.assertNotIn("file://", report)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["test_code"]["repository"], "owner/miner-testcode")

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
        self.assertNotIn(str(summary.artifact_root), json.dumps(result_payload))
        self.assertEqual(len(reservations), 2)
        self.assertEqual(len(transport.uploads), 2)
        self.assertEqual(len(completions), 2)
        self.assertEqual(result.url, "https://qa.example/results/11111111-1111-1111-1111-111111111111")
