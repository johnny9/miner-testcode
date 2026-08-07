from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from miner_testcode.artifacts import TestArtifacts
from miner_testcode.config import DeviceConfig
from miner_testcode.devices.base import PoolSettings
from miner_testcode.devices.bitaxe_bonanza import BitaxeBonanzaDevice


class FakeApi:
    base_url = "http://fake"

    def __init__(self) -> None:
        self.info: dict[str, Any] = {
            "boardVersion": "1002",
            "ASICModel": "BZM",
            "stratumURL": "old.pool",
            "stratumPort": 3333,
            "stratumUser": "old.worker",
            "stratumSuggestedDifficulty": 1000,
            "stratumProtocol": "SV1",
            "stratumTLS": 0,
            "stratumExtranonceSubscribe": False,
            "stratumDecodeCoinbase": True,
            "miningPaused": False,
            "uptimeSeconds": 100,
            "asicHealth": {"lifecycle": "MINING", "lastFaultCode": 0},
        }
        self.patches: list[dict[str, Any]] = []

    async def get_json(self, path: str) -> dict[str, Any]:
        result = dict(self.info)
        self.info["uptimeSeconds"] += 1
        return result

    async def patch_json(self, path: str, value: Mapping[str, Any]) -> bytes:
        patch = dict(value)
        self.patches.append(patch)
        self.info.update({key: item for key, item in patch.items() if key != "stratumPassword"})
        return b""

    async def post_json(self, path: str, value=None) -> bytes:
        if path == "/api/system/restart":
            self.info["uptimeSeconds"] = 0
        return b"{}"


class BonanzaLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_restores_write_only_password_from_environment_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            os.environ["TEST_BASELINE_POOL_PASSWORD"] = "original-secret"
            self.addCleanup(os.environ.pop, "TEST_BASELINE_POOL_PASSWORD", None)
            config = DeviceConfig(
                name="fake-bonanza",
                type="bitaxe_bonanza",
                interfaces={"api": {"base_url": "http://127.0.0.1", "online_timeout": 2}},
                options={
                    "baseline_stratum_password_env": "TEST_BASELINE_POOL_PASSWORD"
                },
            )
            artifacts = TestArtifacts.create(Path(directory) / "case")
            device = BitaxeBonanzaDevice(
                config,
                project_dir=Path(directory),
                artifacts=artifacts,
                logger=logging.getLogger("test-bonanza-lifecycle"),
            )
            fake_api = FakeApi()
            device.api = fake_api  # type: ignore[assignment]

            baseline = await device.snapshot_clean_state()
            await device.configure_pool(
                PoolSettings(
                    host="new.pool",
                    port=4444,
                    username="new.worker",
                    password="test-secret",
                )
            )
            await device.restore_clean_state(baseline)

            self.assertEqual(fake_api.patches[0]["stratumPassword"], "test-secret")
            self.assertEqual(fake_api.patches[-1]["stratumPassword"], "original-secret")
            self.assertEqual(fake_api.info["stratumURL"], "old.pool")
            baseline_artifact = (artifacts.path / "baseline.json").read_text()
            self.assertNotIn("original-secret", baseline_artifact)
            self.assertNotIn("old.worker", baseline_artifact)
