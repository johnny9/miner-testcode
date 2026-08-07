from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..state import DeviceState, DeviceStateStore
from ..telemetry import TelemetryCapture


@dataclass(frozen=True, slots=True)
class CleanState:
    settings: Mapping[str, Any]
    mining_paused: bool


@dataclass(frozen=True, slots=True)
class PoolSettings:
    host: str
    port: int
    username: str
    password: str | None = None
    suggested_difficulty: int | None = None
    tls: bool = False


class MiningDevice(abc.ABC):
    """Capability-oriented device contract used by generic test cases."""

    name: str
    capabilities: frozenset[str]
    state: DeviceStateStore
    telemetry: TelemetryCapture

    @abc.abstractmethod
    async def start(self) -> None:
        """Open interfaces and start independent background state maintenance."""

    @abc.abstractmethod
    async def ensure_target_firmware(self) -> None:
        """Apply a configured target firmware if it is not already running."""

    @abc.abstractmethod
    async def snapshot_clean_state(self) -> CleanState:
        """Capture the mutable state that tests are allowed to change."""

    @abc.abstractmethod
    async def restore_clean_state(self, baseline: CleanState) -> None:
        """Return all test-mutated settings to the captured baseline."""

    @abc.abstractmethod
    async def configure_pool(self, pool: PoolSettings) -> None:
        """Apply pool settings and make them active."""

    @abc.abstractmethod
    async def current_info(self) -> Mapping[str, Any]:
        """Read and publish the latest API information."""

    @abc.abstractmethod
    async def wait_for_stable_state(
        self,
        predicate,
        *,
        samples: int,
        timeout: float,
        description: str,
    ) -> list[DeviceState]:
        """Require consecutive independently observed samples."""

    @abc.abstractmethod
    async def save_device_logs(self) -> None:
        """Persist device-owned logs into the current test artifact directory."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Stop monitors and close all interfaces."""

    def resolve_project_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.project_dir / path).resolve()
