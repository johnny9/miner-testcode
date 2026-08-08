from __future__ import annotations

import asyncio
import logging
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from .artifacts import RunArtifacts, TestArtifacts
from .capabilities import missing
from .config import DeviceConfig, ProjectConfig
from .devices import create_device
from .devices.base import CleanState, MiningDevice
from .redaction import PrivacyFormatter, redact_text
from .telemetry import ChartMarkerHandler, log_chart

_TestMethod = TypeVar("_TestMethod", bound=Callable[..., Any])


def validation_test(*pr_numbers: int) -> Callable[[_TestMethod], _TestMethod]:
    """Mark a test as opt-in coverage associated with one or more PRs."""

    if not pr_numbers or any(
        isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0
        for pr in pr_numbers
    ):
        raise ValueError("validation_test requires positive integer PR numbers")
    related_prs = frozenset(pr_numbers)

    def decorate(function: _TestMethod) -> _TestMethod:
        setattr(function, "validation_prs", related_prs)
        return function

    return decorate


@dataclass(frozen=True, slots=True)
class TestContext:
    project: ProjectConfig
    device_config: DeviceConfig
    run_artifacts: RunArtifacts
    project_root: Path
    validation_prs: frozenset[int] = frozenset()


class MinerTestCase(unittest.IsolatedAsyncioTestCase):
    """Base case that owns one complete, failure-safe device lifecycle."""

    required_capabilities: frozenset[str] = frozenset()
    _context: TestContext | None = None

    device: MiningDevice
    baseline: CleanState
    artifacts: TestArtifacts
    logger: logging.Logger

    @classmethod
    def bind_context(cls, test: "MinerTestCase", context: TestContext) -> None:
        test._context = context

    def setUp(self) -> None:
        if self._context is None:
            self.fail("MinerTestCase must be run by miner-test with a device context")
        method = getattr(type(self), self._testMethodName)
        related_prs = getattr(method, "validation_prs", frozenset())
        if related_prs and related_prs.isdisjoint(self._context.validation_prs):
            listed = ", ".join(f"#{number}" for number in sorted(related_prs))
            self.skipTest(
                f"validation test for {listed}; enable with runner.validation_prs "
                "or --validation-pr"
            )

    async def asyncSetUp(self) -> None:
        if self._context is None:
            self.fail("MinerTestCase must be run by miner-test with a device context")
        context = self._context
        self.artifacts = context.run_artifacts.for_test(
            context.device_config.publication_name, self.id()
        )
        self.logger = logging.getLogger(
            f"miner_testcode.test.{context.device_config.publication_name}.{self.id()}"
        )
        handler = logging.FileHandler(self.artifacts.path / "test.log", encoding="utf-8")
        handler.setFormatter(
            PrivacyFormatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                project_root=context.project_root,
                artifact_root=context.run_artifacts.path,
                replacements={
                    context.device_config.name: context.device_config.publication_name
                },
            )
        )
        self.logger.addHandler(handler)

        def remove_handler() -> None:
            self.logger.removeHandler(handler)
            handler.close()

        self.addCleanup(remove_handler)

        self.device = create_device(
            context.device_config,
            project_dir=context.project.source.parent,
            artifacts=self.artifacts,
            logger=self.logger,
        )
        chart_handler = ChartMarkerHandler(
            self.device.telemetry,
            sanitize=lambda value: redact_text(
                value,
                project_root=context.project_root,
                artifact_root=context.run_artifacts.path,
                replacements={
                    context.device_config.name: context.device_config.publication_name
                },
            ),
        )
        self.logger.addHandler(chart_handler)

        def remove_chart_handler() -> None:
            self.logger.removeHandler(chart_handler)
            chart_handler.close()

        self.addCleanup(remove_chart_handler)
        unavailable = missing(self.required_capabilities, self.device.capabilities)
        if unavailable:
            self.skipTest(
                f"device {self.device.name} lacks capabilities: {', '.join(sorted(unavailable))}"
            )

        self._baseline: CleanState | None = None
        self.addAsyncCleanup(self._cleanup_device)
        self.chart("Device lifecycle started")
        await self.device.start()
        await self.device.ensure_target_firmware()
        self.chart("Target firmware ready", status="good")
        self._baseline = await self.device.snapshot_clean_state()
        self.baseline = self._baseline
        self.chart("Test body started")

    async def _cleanup_device(self) -> None:
        errors: list[BaseException] = []
        context = self._context
        timeout = context.project.runner.cleanup_timeout if context else 120.0
        try:
            if self._baseline is not None:
                self.chart("Clean-state restore started")
                await asyncio.wait_for(
                    self.device.restore_clean_state(self._baseline), timeout=timeout
                )
                self.chart("Clean state restored", status="good")
        except BaseException as exc:
            errors.append(exc)
            self.logger.exception("device clean-state restoration failed")
        try:
            await self.device.save_device_logs()
        except BaseException as exc:
            errors.append(exc)
            self.logger.exception("device log collection failed")
        try:
            await self.device.close()
            self.chart("Device lifecycle finished", status="good")
        except BaseException as exc:
            errors.append(exc)
            self.logger.exception("device interface shutdown failed")
        if errors:
            raise ExceptionGroup("device cleanup failed", errors)

    @property
    def device_config(self) -> DeviceConfig:
        assert self._context is not None
        return self._context.device_config

    def settings_for(self, name: str) -> Mapping[str, Any]:
        assert self._context is not None
        return self._context.project.test_settings(name)

    def chart(self, message: str, *args, status: str = "info", **kwargs) -> None:
        """Log a CHART-level event rendered as a vertical telemetry marker."""

        log_chart(self.logger, message, *args, status=status, **kwargs)
