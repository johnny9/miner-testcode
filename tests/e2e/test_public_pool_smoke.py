from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict

from miner_testcode import capabilities as caps
from miner_testcode.devices.base import PoolSettings
from miner_testcode.interfaces.stratum import StratumV1Probe
from miner_testcode.state import DeviceState
from miner_testcode.testcase import MinerTestCase


class PublicPoolSmokeTest(MinerTestCase):
    """Generic mining test; device-specific behavior stays in the adapter."""

    required_capabilities = frozenset(
        {caps.API, caps.MINING_STATE, caps.POOL_CONFIG, caps.STRATUM_V1}
    )

    async def test_mines_against_public_pool(self) -> None:
        settings = self.settings_for("public_pool_smoke")
        initial_info = await self.device.current_info()

        host = str(settings.get("host", "public-pool.io"))
        port = int(settings.get("port", 3333))
        configure_device = bool(settings.get("configure_device", True))
        configured_probe_username = settings.get("probe_username")
        username_value = settings.get("username")
        username_env = str(settings.get("username_env", "MINER_TEST_POOL_USER"))
        if username_value is None:
            username_value = os.environ.get(username_env)
        if username_value is None and not configure_device:
            username_value = configured_probe_username
        username = str(username_value or "")
        if not username:
            self.fail(
                f"set {username_env} or tests.public_pool_smoke.username for the pool test"
            )
        password_value = settings.get("password")
        password_env = str(settings.get("password_env", "MINER_TEST_POOL_PASSWORD"))
        if password_value is None:
            password_value = os.environ.get(password_env)
        password = str(password_value) if password_value is not None else None
        device_password = (
            password if bool(settings.get("configure_device_password", False)) else None
        )
        tls = bool(settings.get("tls", False))
        suggested_difficulty = settings.get("suggested_difficulty")
        suggested = int(suggested_difficulty) if suggested_difficulty is not None else None
        min_hashrate = float(settings.get("min_hashrate_ghs", 100.0))
        max_work_age = float(settings.get("max_work_age_seconds", 60.0))
        readiness_timeout = float(settings.get("readiness_timeout", 120.0))
        stable_samples = int(settings.get("stable_samples", 3))
        minimum_job_notifications = int(settings.get("minimum_job_notifications", 1))

        probe_username = str(configured_probe_username or username)
        probe = StratumV1Probe(
            host,
            port,
            probe_username,
            password=password or "x",
            tls=tls,
        )
        self.chart("Public pool Stratum probe started")
        probe_task = asyncio.create_task(
            probe.run(
                timeout=float(settings.get("probe_timeout", 30.0)),
                minimum_job_notifications=minimum_job_notifications,
            ),
            name="public-pool-stratum-probe",
        )

        async def stop_probe() -> None:
            if not probe_task.done():
                probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)

        self.addAsyncCleanup(stop_probe)

        if configure_device:
            self.chart("Applying public pool configuration")
            await self.device.configure_pool(
                PoolSettings(
                    host=host,
                    port=port,
                    username=username,
                    password=device_password,
                    suggested_difficulty=suggested,
                    tls=tls,
                )
            )
        else:
            self.assertEqual(
                initial_info.get("stratumURL"),
                host,
                "observational test requires the device to already use the target pool",
            )
            self.assertEqual(
                int(initial_info.get("stratumPort") or 0),
                port,
                "observational test requires the device to already use the target port",
            )

        def healthy_mining(state: DeviceState) -> bool:
            engines_healthy = (
                state.active_engines is None
                or state.expected_engines is None
                or state.active_engines == state.expected_engines
            )
            work_fresh = (
                state.current_work_age_seconds is None
                or state.current_work_age_seconds <= max_work_age
            )
            return (
                state.online
                and state.identity_ok
                and state.mining_active
                and state.pool_host == host
                and state.pool_port == port
                and state.hashrate_ghs >= min_hashrate
                and engines_healthy
                and work_fresh
                and state.fault_code == 0
            )

        states, probe_result = await asyncio.gather(
            self.device.wait_for_stable_state(
                healthy_mining,
                samples=stable_samples,
                timeout=readiness_timeout,
                description=(
                    f"healthy mining at {host}:{port} with at least "
                    f"{min_hashrate:g} GH/s"
                ),
            ),
            probe_task,
        )
        self.chart(
            "Healthy mining and fresh pool work observed "
            f"({probe_result.job_notifications_received} mining.notify)",
            status="good",
        )

        self.assertTrue(probe_result.subscribed)
        self.assertTrue(probe_result.authorized)
        self.assertTrue(probe_result.job_received)
        self.assertGreaterEqual(
            probe_result.job_notifications_received,
            minimum_job_notifications,
        )
        self.assertEqual(len(states), stable_samples)
        self.assertTrue(all(state.fault_code == 0 for state in states))
        (self.artifacts.path / "stratum-probe.json").write_text(
            json.dumps(asdict(probe_result), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.logger.info(
            "Stratum probe completed: subscribed=%s authorized=%s job=%s "
            "notifications=%d messages=%d",
            probe_result.subscribed,
            probe_result.authorized,
            probe_result.job_received,
            probe_result.job_notifications_received,
            probe_result.messages_received,
        )

        if bool(settings.get("require_accepted_share", False)):
            baseline_shares = states[-1].shares_accepted
            share_timeout = float(settings.get("accepted_share_timeout", 300.0))
            await self.device.state.wait_for(
                lambda state: state.online and state.shares_accepted > baseline_shares,
                timeout=share_timeout,
                description="an accepted device share",
                after_generation=self.device.state.generation,
            )
            self.chart("Accepted device share observed", status="good")
