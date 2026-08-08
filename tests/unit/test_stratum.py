from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from miner_testcode.interfaces.stratum import StratumV1Probe


class StratumProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_subscribes_authorizes_and_receives_job(self) -> None:
        messages = [
            {"id": 1, "result": [[[]], "aa", 4], "error": None},
            {"id": 2, "result": True, "error": None},
            {"id": None, "method": "mining.set_difficulty", "params": [1024]},
            {"id": None, "method": "mining.notify", "params": ["job"]},
            {"id": None, "method": "mining.notify", "params": ["next-job"]},
        ]

        class FakeReader:
            def __init__(self) -> None:
                self.lines = [json.dumps(message).encode() + b"\n" for message in messages]

            async def readline(self) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        class FakeWriter:
            def __init__(self) -> None:
                self.writes: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.writes.append(data)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        reader = FakeReader()
        writer = FakeWriter()
        with patch(
            "miner_testcode.interfaces.stratum.asyncio.open_connection",
            new=AsyncMock(return_value=(reader, writer)),
        ):
            result = await StratumV1Probe(
                "pool.example", 3333, "bc1test.worker"
            ).run(timeout=1, minimum_job_notifications=2)

        subscribe = json.loads(writer.writes[0])
        authorize = json.loads(writer.writes[1])
        self.assertEqual(subscribe["method"], "mining.subscribe")
        self.assertEqual(authorize["method"], "mining.authorize")
        self.assertTrue(result.subscribed)
        self.assertTrue(result.authorized)
        self.assertTrue(result.job_received)
        self.assertEqual(result.job_notifications_received, 2)
        self.assertEqual(result.difficulty, 1024)
