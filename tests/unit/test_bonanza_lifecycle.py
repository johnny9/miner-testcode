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


class FakePoolsApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.info.update(
            {
                "pools": [
                    {
                        "id": 0,
                        "stratumURL": "old.pool",
                        "stratumPort": 3333,
                        "stratumUser": "old.worker",
                        "stratumPassword": "*****",
                        "stratumSuggestedDifficulty": 1000,
                        "stratumProtocol": "SV1",
                        "stratumTLS": 0,
                        "stratumExtranonceSubscribe": False,
                        "stratumDecodeCoinbase": True,
                    },
                    {
                        "id": 1,
                        "stratumURL": "backup.pool",
                        "stratumPort": 4444,
                        "stratumUser": "backup.worker",
                        "stratumPassword": "*****",
                        "stratumSuggestedDifficulty": 512,
                        "stratumProtocol": "SV1",
                        "stratumTLS": 0,
                        "stratumExtranonceSubscribe": False,
                        "stratumDecodeCoinbase": True,
                    },
                ],
                "primaryPoolIndex": 0,
                "secondaryPoolIndex": 1,
                "useFallbackStratum": True,
            }
        )

    async def patch_json(self, path: str, value: Mapping[str, Any]) -> bytes:
        patch = dict(value)
        self.patches.append(patch)
        if "pools" not in patch:
            self.info.update(patch)
            return b""
        by_id = {int(pool["id"]): dict(pool) for pool in self.info["pools"]}
        for incoming in patch["pools"]:
            pool = dict(incoming)
            pool_id = int(pool["id"])
            if pool.get("stratumPassword") == "*****":
                pool["stratumPassword"] = by_id[pool_id]["stratumPassword"]
            by_id[pool_id].update(pool)
        self.info["pools"] = [by_id[key] for key in sorted(by_id)]
        primary = by_id[int(self.info["primaryPoolIndex"])]
        self.info.update(
            {
                key: value
                for key, value in primary.items()
                if key.startswith("stratum") and key != "stratumPassword"
            }
        )
        return b""


class BonanzaLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_pool_schema_is_configured_and_fully_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = DeviceConfig(
                name="fake-bonanza",
                type="bitaxe_bonanza",
                interfaces={
                    "api": {"base_url": "http://127.0.0.1", "online_timeout": 2}
                },
            )
            artifacts = TestArtifacts.create(Path(directory) / "case")
            device = BitaxeBonanzaDevice(
                config,
                project_dir=Path(directory),
                artifacts=artifacts,
                logger=logging.getLogger("test-bonanza-pool-schema"),
            )
            fake_api = FakePoolsApi()
            device.api = fake_api  # type: ignore[assignment]

            baseline = await device.snapshot_clean_state()
            await device.configure_pool(
                PoolSettings(
                    host="new.pool",
                    port=5555,
                    username="new.worker",
                )
            )

            self.assertEqual(set(fake_api.patches[0]), {"pools"})
            configured = fake_api.patches[0]["pools"][0]
            self.assertEqual(configured["id"], 0)
            self.assertEqual(configured["stratumURL"], "new.pool")
            self.assertEqual(configured["stratumPassword"], "*****")
            self.assertEqual(fake_api.info["stratumURL"], "new.pool")

            await device.restore_clean_state(baseline)

            restored = fake_api.patches[-1]["pools"]
            self.assertEqual(len(restored), 2)
            self.assertEqual(restored[0]["stratumURL"], "old.pool")
            self.assertEqual(restored[1]["stratumURL"], "backup.pool")
            self.assertEqual(fake_api.info["stratumURL"], "old.pool")
            self.assertEqual(fake_api.info["primaryPoolIndex"], 0)
            self.assertEqual(fake_api.info["secondaryPoolIndex"], 1)
            self.assertTrue(fake_api.info["useFallbackStratum"])

            baseline_artifact = (artifacts.path / "baseline.json").read_text()
            self.assertNotIn("old.worker", baseline_artifact)
            self.assertNotIn("backup.worker", baseline_artifact)

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
