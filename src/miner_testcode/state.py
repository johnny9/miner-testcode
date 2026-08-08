from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping


@dataclass(frozen=True, slots=True)
class DeviceState:
    observed_at: float
    online: bool
    identity_ok: bool = False
    lifecycle: str | None = None
    mining_active: bool = False
    hashrate_ghs: float = 0.0
    shares_accepted: int = 0
    shares_rejected: int = 0
    active_engines: int | None = None
    expected_engines: int | None = None
    pool_host: str | None = None
    pool_port: int | None = None
    current_work_age_seconds: float | None = None
    uptime_seconds: int | None = None
    fault_code: int = 0
    error: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def offline(cls, error: str | None = None) -> "DeviceState":
        return cls(observed_at=time.time(), online=False, error=error)

    def as_event(self) -> dict[str, Any]:
        event = asdict(self)
        event.pop("raw", None)
        return event


class DeviceStateStore:
    """Latest state plus an async condition used by tests and lifecycle code."""

    def __init__(self, initial: DeviceState | None = None) -> None:
        self._state = initial or DeviceState.offline("not observed yet")
        self._condition = asyncio.Condition()
        self._generation = 0

    @property
    def latest(self) -> DeviceState:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    async def update(self, state: DeviceState) -> None:
        async with self._condition:
            self._state = state
            self._generation += 1
            self._condition.notify_all()

    async def wait_for(
        self,
        predicate: Callable[[DeviceState], bool],
        *,
        timeout: float,
        description: str,
        after_generation: int | None = None,
    ) -> DeviceState:
        async def _wait() -> DeviceState:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: (
                        (after_generation is None or self._generation > after_generation)
                        and predicate(self._state)
                    )
                )
                return self._state

        try:
            return await asyncio.wait_for(_wait(), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(
                f"timed out after {timeout:.1f}s waiting for {description}; "
                f"latest state={self._state}"
            ) from exc
