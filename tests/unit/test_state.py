from __future__ import annotations

import asyncio
import unittest

from miner_testcode.state import DeviceState, DeviceStateStore


class DeviceStateStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_a_new_matching_generation(self) -> None:
        store = DeviceStateStore()
        generation = store.generation

        waiter = asyncio.create_task(
            store.wait_for(
                lambda state: state.online and state.hashrate_ghs > 100,
                timeout=1,
                description="hashrate",
                after_generation=generation,
            )
        )
        await store.update(
            DeviceState(observed_at=1, online=True, identity_ok=True, hashrate_ghs=250)
        )
        observed = await waiter
        self.assertEqual(observed.hashrate_ghs, 250)

    async def test_timeout_reports_latest_state(self) -> None:
        store = DeviceStateStore()
        with self.assertRaisesRegex(TimeoutError, "latest state"):
            await store.wait_for(
                lambda state: state.online,
                timeout=0.01,
                description="online",
            )
