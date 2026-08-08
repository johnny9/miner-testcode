from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import dataclass
from typing import Any

from ..errors import InterfaceError


@dataclass(frozen=True, slots=True)
class StratumProbeResult:
    connected: bool
    subscribed: bool
    authorized: bool
    job_received: bool
    job_notifications_received: int
    difficulty: float | None
    messages_received: int
    elapsed_seconds: float


class StratumV1Probe:
    """Independent Stratum V1 handshake used alongside device observation."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        *,
        password: str = "x",
        tls: bool = False,
        user_agent: str = "miner-testcode/0.1.0",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.tls = tls
        self.user_agent = user_agent

    async def run(
        self,
        *,
        timeout: float = 20.0,
        minimum_job_notifications: int = 1,
    ) -> StratumProbeResult:
        if minimum_job_notifications < 1:
            raise ValueError("minimum_job_notifications must be positive")
        started = time.monotonic()
        ssl_context = ssl.create_default_context() if self.tls else None
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(
                    self.host,
                    self.port,
                    ssl=ssl_context,
                    server_hostname=self.host if self.tls else None,
                    limit=1024 * 1024,
                )
                try:
                    requests = (
                        {"id": 1, "method": "mining.subscribe", "params": [self.user_agent]},
                        {
                            "id": 2,
                            "method": "mining.authorize",
                            "params": [self.username, self.password],
                        },
                    )
                    for request in requests:
                        writer.write(
                            json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
                        )
                    await writer.drain()

                    subscribed = False
                    authorized = False
                    job_received = False
                    job_notifications = 0
                    difficulty: float | None = None
                    messages = 0
                    while not (
                        subscribed
                        and authorized
                        and job_notifications >= minimum_job_notifications
                    ):
                        line = await reader.readline()
                        if not line:
                            raise InterfaceError(
                                "Stratum server closed before subscribe, authorize, and job completed"
                            )
                        if len(line) > 1024 * 1024:
                            raise InterfaceError("Stratum message exceeded 1 MiB")
                        try:
                            message: Any = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise InterfaceError(f"Stratum server sent invalid JSON: {exc}") from exc
                        if not isinstance(message, dict):
                            continue
                        messages += 1
                        message_id = message.get("id")
                        if message_id == 1:
                            if message.get("error") is not None or not message.get("result"):
                                raise InterfaceError(
                                    f"Stratum subscription rejected: {message.get('error')!r}"
                                )
                            subscribed = True
                        elif message_id == 2:
                            if message.get("error") is not None or message.get("result") is not True:
                                raise InterfaceError(
                                    f"Stratum authorization rejected: {message.get('error')!r}"
                                )
                            authorized = True

                        method = message.get("method")
                        params = message.get("params")
                        if method == "mining.notify" and isinstance(params, list):
                            job_notifications += 1
                            job_received = True
                        elif method == "mining.set_difficulty" and isinstance(params, list) and params:
                            try:
                                difficulty = float(params[0])
                            except (TypeError, ValueError):
                                pass
                finally:
                    writer.close()
                    await writer.wait_closed()
        except TimeoutError as exc:
            raise InterfaceError(
                f"Stratum probe timed out after {timeout:.1f}s waiting for handshake and job"
            ) from exc
        except OSError as exc:
            raise InterfaceError(
                f"could not connect to Stratum server {self.host}:{self.port}: {exc}"
            ) from exc

        return StratumProbeResult(
            connected=True,
            subscribed=subscribed,
            authorized=authorized,
            job_received=job_received,
            job_notifications_received=job_notifications,
            difficulty=difficulty,
            messages_received=messages,
            elapsed_seconds=time.monotonic() - started,
        )
