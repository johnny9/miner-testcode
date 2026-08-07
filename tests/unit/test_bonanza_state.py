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

    def test_normalizes_authoritative_bonanza_telemetry(self) -> None:
        telemetry = BitaxeBonanzaDevice.telemetry_from_info(
            {
                "hashRate": 1491.8,
                "temp": 0,
                "actualFrequency": 1200,
                "fanrpm": 0,
                "asicHealth": {
                    "boardTemperatureC": 59.97,
                    "fixedFrequencyMHz": 1200,
                    "fanRPM": 3360,
                },
            }
        )

        self.assertEqual(
            telemetry,
            {
                "hashrate_ghs": 1491.8,
                "temperature_c": 59.97,
                "frequency_mhz": 1200.0,
                "fan_rpm": 3360.0,
            },
        )

    def test_merges_nested_websocket_diffs(self) -> None:
        state = {
            "hashRate": 1000,
            "asicHealth": {"boardTemperatureC": 50, "fanRPM": 3000},
        }
        BitaxeBonanzaDevice._merge_json_diff(
            state,
            {"hashRate": 1200, "asicHealth": {"fanRPM": 3200}},
        )

        self.assertEqual(state["hashRate"], 1200)
        self.assertEqual(state["asicHealth"]["boardTemperatureC"], 50)
        self.assertEqual(state["asicHealth"]["fanRPM"], 3200)
