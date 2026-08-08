from __future__ import annotations

import argparse
import inspect
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
from .provenance import ResolvedTestCode, resolve_test_code
from .redaction import PrivacyFormatter, redact_text, sanitize_artifacts
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
    project_root: Path,
    validation_prs: frozenset[int] = frozenset(),
) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    discovered = loader.discover(str(project.runner.tests_dir), pattern=pattern)
    suite = unittest.TestSuite()
    context = TestContext(
        project=project,
        device_config=device,
        run_artifacts=artifacts,
        project_root=project_root,
        validation_prs=validation_prs,
    )
    count = 0
    class_scoped_types: dict[type[MinerTestCase], type[MinerTestCase]] = {}
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
        original_type = type(test)
        if getattr(original_type, "class_scoped_lifecycle", False):
            bound_type = class_scoped_types.get(original_type)
            if bound_type is None:
                bound_type = type(
                    original_type.__name__,
                    (original_type,),
                    {
                        "__module__": original_type.__module__,
                        "__qualname__": original_type.__qualname__,
                        "_class_context": context,
                    },
                )
                class_scoped_types[original_type] = bound_type
            test = bound_type(test._testMethodName)
        MinerTestCase.bind_context(test, context)
        suite.addTest(test)
        count += 1
    if count == 0:
        raise ConfigError(
            f"no tests matching {pattern!r} found in {project.runner.tests_dir}"
        )
    return suite


class MiningTestResult(unittest.TextTestResult):
    def __init__(
        self,
        *args,
        artifacts: RunArtifacts,
        test_code: ResolvedTestCode,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.artifacts = artifacts
        self.test_code = test_code
        self._started: dict[str, float] = {}
        self.records: list[TestRecord] = []

    @staticmethod
    def _identity(test: unittest.TestCase) -> dict[str, str]:
        context = getattr(test, "_context", None)
        return {
            "test": test.id(),
            "device": (
                context.device_config.publication_name
                if context is not None
                else "unknown"
            ),
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
            detail = redact_text(
                detail,
                project_root=self.test_code.root,
                artifact_root=self.artifacts.path,
            )
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
        source_path: str | None = None
        source_line: int | None = None
        source_url: str | None = None
        telemetry: dict[str, object] | None = None
        try:
            method = getattr(type(test), test._testMethodName)
            source = Path(inspect.getsourcefile(method) or "").resolve()
            source_path = source.relative_to(self.test_code.root).as_posix()
            source_line = inspect.getsourcelines(method)[1]
            source_url = self.test_code.file_url(source, source_line)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        device = getattr(test, "device", None)
        capture = getattr(device, "telemetry", None)
        if capture is not None:
            if outcome in {"passed", "expected_failure"}:
                marker_status = "good"
            elif outcome == "skipped":
                marker_status = "info"
            else:
                marker_status = "bad"
            capture.add_marker(
                f"Test {outcome.replace('_', ' ')}",
                status=marker_status,
            )
            telemetry = capture.to_dict()
        self.records.append(
            TestRecord(
                test_id=identity["test"],
                device=identity["device"],
                outcome=outcome,
                elapsed_seconds=event["elapsed_seconds"],
                detail=detail,
                artifact_dir=artifact_dir,
                source_path=source_path,
                source_line=source_line,
                source_url=source_url,
                telemetry=telemetry,
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


def _configure_logging(
    project: ProjectConfig,
    artifacts: RunArtifacts,
    devices: tuple[DeviceConfig, ...],
    project_root: Path,
) -> None:
    level = getattr(logging, project.runner.log_level, None)
    if not isinstance(level, int):
        raise ConfigError(f"unknown runner.log_level: {project.runner.log_level}")
    root = logging.getLogger()
    root.setLevel(level)
    formatter = PrivacyFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        project_root=project_root,
        artifact_root=artifacts.path,
        replacements={device.name: device.publication_name for device in devices},
    )
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
    parser.add_argument(
        "--validation-pr",
        action="append",
        type=int,
        default=[],
        metavar="PR",
        help=(
            "enable opt-in validation tests associated with a PR number; "
            "repeat for multiple PRs"
        ),
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def run(argv: list[str] | None = None) -> bool:
    args = build_parser().parse_args(argv)
    project = load_config(args.config)
    invalid_cli_prs = [number for number in args.validation_pr if number <= 0]
    if invalid_cli_prs:
        raise ConfigError("--validation-pr must be a positive integer")
    validation_prs = frozenset(
        {*project.runner.validation_prs, *args.validation_pr}
    )
    devices = project.selected_devices(set(args.devices) if args.devices else None)
    if not devices:
        raise ConfigError("no enabled devices are configured")
    remote_publication = any(
        bool(project.publisher_settings(name).get("enabled", False))
        for name in ("github", "mining_qa_status")
    )
    test_code = resolve_test_code(
        project.runner.tests_dir, require_published=remote_publication
    )
    artifacts = RunArtifacts.create(project.runner.artifacts_dir)
    started_at = time.time()
    _configure_logging(project, artifacts, devices, test_code.root)
    logger = logging.getLogger(__name__)
    publisher_manager = PublisherManager(project.publishers, logger=logger)
    logger.info("run artifacts: %s", artifacts.run_id)

    def relative_path(path: Path) -> str:
        try:
            return path.resolve().relative_to(test_code.root).as_posix()
        except ValueError:
            return path.name

    metadata = {
        "started_at": started_at,
        "config": relative_path(project.source),
        "devices": [device.publication_name for device in devices],
        "tests_dir": relative_path(project.runner.tests_dir),
        "pattern": args.pattern or project.runner.pattern,
        "validation_prs": sorted(validation_prs),
        "python": sys.version,
        "test_code": {
            "repository": test_code.record.repository,
            "commit_sha": test_code.record.commit_sha,
            "url": test_code.record.url,
        },
    }
    (artifacts.path / "run.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    combined = unittest.TestSuite()
    pattern = args.pattern or project.runner.pattern
    for device in devices:
        combined.addTests(
            _load_device_suite(
                project,
                device,
                artifacts,
                pattern=pattern,
                project_root=test_code.root,
                validation_prs=validation_prs,
            )
        )

    verbosity = project.runner.verbosity + args.verbose
    test_runner = unittest.TextTestRunner(
        verbosity=verbosity,
        resultclass=partial(
            MiningTestResult,
            artifacts=artifacts,
            test_code=test_code,
        ),
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
        devices=tuple(
            {"name": device.publication_name, "type": device.type}
            for device in devices
        ),
        tests=tuple(result.records),
        tests_run=result.testsRun,
        failures=len(result.failures),
        errors=len(result.errors),
        skipped=len(result.skipped),
        expected_failures=len(result.expectedFailures),
        unexpected_successes=len(result.unexpectedSuccesses),
        successful=result.wasSuccessful(),
        test_code=test_code.record,
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    sanitize_artifacts(
        artifacts.path,
        project_root=test_code.root,
        replacements={device.name: device.publication_name for device in devices},
    )
    publishers_ok = publisher_manager.publish(summary)
    logger.info(
        "run complete: status=%s publishers_ok=%s artifacts=%s",
        summary.status,
        publishers_ok,
        artifacts.run_id,
    )
    return result.wasSuccessful() and publishers_ok


def main(argv: list[str] | None = None) -> int:
    try:
        return 0 if run(argv) else 1
    except (ConfigError, OSError) as exc:
        print(f"miner-test: {exc}", file=sys.stderr)
        return 2
