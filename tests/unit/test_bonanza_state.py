from __future__ import annotations

import unittest

from miner_testcode.devices.bitaxe_bonanza import BitaxeBonanzaDevice


class BonanzaStateTest(unittest.TestCase):
    def test_normalizes_live_api_contract(self) -> None:
        state = BitaxeBonanzaDevice.state_from_info(
            {
                "boardVersion": "1002",
                "ASICModel": "BZM",
                "hashRate": 1234.5,
                "sharesAccepted": 3,
                "sharesRejected": 1,
                "stratumURL": "public-pool.io",
                "stratumPort": 3333,
                "currentWorkAgeSeconds": 4.5,
                "uptimeSeconds": 90,
                "asicHealth": {
                    "lifecycle": "MINING",
                    "activeEngineCount": 944,
                    "expectedEngineCount": 944,
                    "lastFaultCode": 0,
                },
            }
        )
        self.assertTrue(state.identity_ok)
        self.assertEqual(state.lifecycle, "MINING")
        self.assertEqual(state.hashrate_ghs, 1234.5)
        self.assertEqual(state.active_engines, state.expected_engines)
        self.assertEqual(state.pool_host, "public-pool.io")

    def test_rejects_non_bonanza_identity_in_normalized_state(self) -> None:
        state = BitaxeBonanzaDevice.state_from_info(
            {"boardVersion": "204", "ASICModel": "BM1368"}
        )
        self.assertFalse(state.identity_ok)
