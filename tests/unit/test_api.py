from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miner_testcode.errors import InterfaceError
from miner_testcode.interfaces.api import HttpApiInterface


class ReadOnlyApiTest(unittest.TestCase):
    def test_blocks_write_before_opening_a_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = HttpApiInterface(
                "http://127.0.0.1:1",
                read_only=True,
                trace_path=Path(directory) / "api.jsonl",
            )
            with self.assertRaisesRegex(InterfaceError, "blocked by read-only mode"):
                api._request_sync(  # noqa: SLF001 - verifies the transport boundary
                    "PATCH",
                    "/api/system",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
