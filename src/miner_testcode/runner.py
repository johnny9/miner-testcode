from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import unittest
from functools import partial
from pathlib import Path
from typing import Iterator

from .artifacts import RunArtifacts
from .config import ConfigError, DeviceConfig, ProjectConfig, load_config
from .publishers import PublisherManager
from .redaction import redact_text
from .results import RunSummary, TestRecord
from .testcase import MinerTestCase, TestContext


def _iter_tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def _load_device_suite(
    project: ProjectConfig,
    device: DeviceConfig,
    artifacts: RunArtifacts,
    *,
    pattern: str,
) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    discovered = loader.discover(str(project.runner.tests_dir), pattern=pattern)
    suite = unittest.TestSuite()
    context = TestContext(project=project, device_config=device, run_artifacts=artifacts)
    count = 0
    for test in _iter_tests(discovered):
        module = sys.modules.get(type(test).__module__)
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        try:
            Path(module_file).resolve().relative_to(project.runner.tests_dir.resolve())
        except ValueError:
            # unittest also sees imported TestCase base classes. Only cases
            # defined by files in the configured test tree belong in the run.
            continue
        if not isinstance(test, MinerTestCase):
            raise ConfigError(
                f"end-to-end test {test.id()} must inherit MinerTestCase"
            )
        MinerTestCase.bind_context(test, context)
        suite.addTest(test)
        count += 1
    if count == 0:
        raise ConfigError(
            f"no tests matching {pattern!r} found in {project.runner.tests_dir}"
        )
    return suite


class MiningTestResult(unittest.TextTestResult):
    def __init__(self, *args, artifacts: RunArtifacts, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.artifacts = artifacts
        self._started: dict[str, float] = {}
        self.records: list[TestRecord] = []

    @staticmethod
    def _identity(test: unittest.TestCase) -> dict[str, str]:
        context = getattr(test, "_context", None)
        return {
            "test": test.id(),
            "device": context.device_config.name if context is not None else "unknown",
        }

    def startTest(self, test: unittest.TestCase) -> None:
        identity = self._identity(test)
        key = f"{identity['device']}::{identity['test']}"
        self._started[key] = time.monotonic()
        self.artifacts.append_event(
            {"at": time.time(), "event": "test_started", **identity}
        )
        super().startTest(test)

    def _outcome(self, test: unittest.TestCase, outcome: str, detail: str | None = None) -> None:
        if detail:
            detail = redact_text(detail)
        identity = self._identity(test)
        key = f"{identity['device']}::{identity['test']}"
        started = self._started.pop(key, time.monotonic())
        event = {
            "at": time.time(),
            "event": "test_finished",
            "outcome": outcome,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            **identity,
        }
        if detail:
            event["detail"] = detail
        self.artifacts.append_event(event)
        test_artifacts = getattr(test, "artifacts", None)
        artifact_dir = None
        if test_artifacts is not None:
            try:
                artifact_dir = test_artifacts.path.relative_to(self.artifacts.path).as_posix()
            except ValueError:
                artifact_dir = None
        self.records.append(
            TestRecord(
                test_id=identity["test"],
                device=identity["device"],
                outcome=outcome,
                elapsed_seconds=event["elapsed_seconds"],
                detail=detail,
                artifact_dir=artifact_dir,
            )
        )

    def addSuccess(self, test: unittest.TestCase) -> None:
        self._outcome(test, "passed")
        super().addSuccess(test)

    def addFailure(self, test: unittest.TestCase, err) -> None:
        self._outcome(test, "failed", self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test: unittest.TestCase, err) -> None:
        self._outcome(test, "error", self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self._outcome(test, "skipped", reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.TestCase, err) -> None:
        self._outcome(test, "expected_failure", self._exc_info_to_string(err, test))
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self._outcome(test, "unexpected_success")
        super().addUnexpectedSuccess(test)


def _configure_logging(project: ProjectConfig, artifacts: RunArtifacts) -> None:
    level = getattr(logging, project.runner.log_level, None)
    if not isinstance(level, int):
        raise ConfigError(f"unknown runner.log_level: {project.runner.log_level}")
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(artifacts.runner_log, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="miner-test",
        description="Run generic unittest suites against configured mining devices.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("MINER_TEST_CONFIG", "config.toml"),
        help="TOML configuration file (default: config.toml or MINER_TEST_CONFIG)",
    )
    parser.add_argument(
        "--device",
        action="append",
        dest="devices",
        help="configured device name to run; repeatable (default: every enabled device)",
    )
    parser.add_argument(
        "--pattern",
        help="override unittest discovery pattern",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def run(argv: list[str] | None = None) -> bool:
    args = build_parser().parse_args(argv)
    project = load_config(args.config)
    devices = project.selected_devices(set(args.devices) if args.devices else None)
    if not devices:
        raise ConfigError("no enabled devices are configured")
    artifacts = RunArtifacts.create(project.runner.artifacts_dir)
    started_at = time.time()
    _configure_logging(project, artifacts)
    logger = logging.getLogger(__name__)
    publisher_manager = PublisherManager(project.publishers, logger=logger)
    logger.info("run artifacts: %s", artifacts.path)

    metadata = {
        "started_at": started_at,
        "config": str(project.source),
        "devices": [device.name for device in devices],
        "tests_dir": str(project.runner.tests_dir),
        "pattern": args.pattern or project.runner.pattern,
        "python": sys.version,
    }
    (artifacts.path / "run.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    combined = unittest.TestSuite()
    pattern = args.pattern or project.runner.pattern
    for device in devices:
        combined.addTests(
            _load_device_suite(project, device, artifacts, pattern=pattern)
        )

    verbosity = project.runner.verbosity + args.verbose
    test_runner = unittest.TextTestRunner(
        verbosity=verbosity,
        resultclass=partial(MiningTestResult, artifacts=artifacts),
    )
    result = test_runner.run(combined)
    finished_at = time.time()
    logger.info(
        "tests complete: tests=%d failures=%d errors=%d skipped=%d",
        result.testsRun,
        len(result.failures),
        len(result.errors),
        len(result.skipped),
    )
    summary = RunSummary(
        run_id=artifacts.run_id,
        artifact_root=artifacts.path,
        started_at=started_at,
        finished_at=finished_at,
        devices=tuple({"name": device.name, "type": device.type} for device in devices),
        tests=tuple(result.records),
        tests_run=result.testsRun,
        failures=len(result.failures),
        errors=len(result.errors),
        skipped=len(result.skipped),
        expected_failures=len(result.expectedFailures),
        unexpected_successes=len(result.unexpectedSuccesses),
        successful=result.wasSuccessful(),
    )
    publishers_ok = publisher_manager.publish(summary)
    logger.info(
        "run complete: status=%s publishers_ok=%s artifacts=%s",
        summary.status,
        publishers_ok,
        artifacts.path,
    )
    return result.wasSuccessful() and publishers_ok


def main(argv: list[str] | None = None) -> int:
    try:
        return 0 if run(argv) else 1
    except (ConfigError, OSError) as exc:
        print(f"miner-test: {exc}", file=sys.stderr)
        return 2
