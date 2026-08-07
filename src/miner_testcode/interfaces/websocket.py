from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Mapping

from ..errors import InterfaceError


class JsonWebSocketInterface:
    """Small async JSON WebSocket transport with bounded messages."""

    def __init__(
        self,
        url: str,
        *,
        open_timeout: float = 5.0,
        ping_interval: float = 20.0,
        max_message_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.url = url
        self.open_timeout = open_timeout
        self.ping_interval = ping_interval
        self.max_message_bytes = max_message_bytes

    async def messages(self) -> AsyncIterator[Mapping[str, Any]]:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - installation failure path
            raise InterfaceError(
                "WebSocket telemetry requires the project websockets dependency"
            ) from exc

        try:
            async with connect(
                self.url,
                open_timeout=self.open_timeout,
                ping_interval=self.ping_interval,
                max_size=self.max_message_bytes,
            ) as socket:
                async for raw in socket:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        yield value
        except InterfaceError:
            raise
        except Exception as exc:
            raise InterfaceError(f"WebSocket telemetry failed: {exc}") from exc
