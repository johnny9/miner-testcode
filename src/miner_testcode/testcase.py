from __future__ import annotations

import asyncio
import logging
import unittest
from dataclasses import dataclass
from typing import Any, Mapping

from .artifacts import RunArtifacts, TestArtifacts
from .capabilities import missing
from .config import DeviceConfig, ProjectConfig
from .devices import create_device
from .devices.base import CleanState, MiningDevice


@dataclass(frozen=True, slots=True)
class TestContext:
    project: ProjectConfig
    device_config: DeviceConfig
    run_artifacts: RunArtifacts


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

    async def asyncSetUp(self) -> None:
        if self._context is None:
            self.fail("MinerTestCase must be run by miner-test with a device context")
        context = self._context
        self.artifacts = context.run_artifacts.for_test(
            context.device_config.name, self.id()
        )
        self.logger = logging.getLogger(
            f"miner_testcode.test.{context.device_config.name}.{self.id()}"
        )
        handler = logging.FileHandler(self.artifacts.path / "test.log", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
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
        unavailable = missing(self.required_capabilities, self.device.capabilities)
        if unavailable:
            self.skipTest(
                f"device {self.device.name} lacks capabilities: {', '.join(sorted(unavailable))}"
            )

        self._baseline: CleanState | None = None
        self.addAsyncCleanup(self._cleanup_device)
        await self.device.start()
        await self.device.ensure_target_firmware()
        self._baseline = await self.device.snapshot_clean_state()
        self.baseline = self._baseline

    async def _cleanup_device(self) -> None:
        errors: list[BaseException] = []
        context = self._context
        timeout = context.project.runner.cleanup_timeout if context else 120.0
        try:
            if self._baseline is not None:
                await asyncio.wait_for(
                    self.device.restore_clean_state(self._baseline), timeout=timeout
                )
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
