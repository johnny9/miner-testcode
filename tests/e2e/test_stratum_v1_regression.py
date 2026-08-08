from __future__ import annotations

import asyncio
import inspect
import json
import os
import unittest
from collections.abc import Mapping
from typing import Any

from miner_testcode import capabilities as caps
from miner_testcode.devices.base import PoolSettings
from miner_testcode.interfaces.fake_stratum import (
    STRATUM_V1_MAX_JSON_LINE_SIZE,
    FakeStratumV1Server,
    MiningJob,
    ShareSubmission,
    StratumHandshake,
)
from miner_testcode.testcase import MinerTestCase


class StratumV1RegressionTest(MinerTestCase):
    """Exercise the device Stratum client against a scriptable local pool."""

    class_scoped_lifecycle = True

    required_capabilities = frozenset(
        {caps.API, caps.MINING_STATE, caps.POOL_CONFIG, caps.STRATUM_V1}
    )

    def _class_fixture(self) -> None:
        """Synthetic method name used for the shared artifact lifecycle."""

    @classmethod
    async def _drain_class_cleanups(cls) -> None:
        errors: list[BaseException] = []
        while cls._fixture_owner._cleanups:
            function, args, kwargs = cls._fixture_owner._cleanups.pop()
            try:
                result = function(*args, **kwargs)
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("class-scoped Stratum cleanup failed", errors)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._ordered_case_failed = False
        cls._class_runner = asyncio.Runner()
        cls._fixture_owner = cls("_class_fixture")
        cls._fixture_owner._context = cls._class_context
        try:
            cls._class_runner.run(
                MinerTestCase.asyncSetUp(cls._fixture_owner)
            )
            (
                cls._server,
                cls._settings,
                cls._username,
            ) = cls._class_runner.run(
                cls._fixture_owner._start_local_pool()
            )
        except BaseException:
            try:
                cls._class_runner.run(cls._drain_class_cleanups())
            finally:
                cls._class_runner.close()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._class_runner.run(cls._drain_class_cleanups())
        finally:
            cls._class_runner.close()
            super().tearDownClass()

    def setUp(self) -> None:
        if type(self)._ordered_case_failed:
            self.skipTest("an earlier ordered Stratum feature failed")

    async def asyncSetUp(self) -> None:
        owner = type(self)._fixture_owner
        self._context = owner._context
        self.artifacts = owner.artifacts
        self.logger = owner.logger
        self.device = owner.device
        self.baseline = owner.baseline

    def _run_ordered_case(self, awaitable: Any) -> None:
        try:
            type(self)._class_runner.run(awaitable)
        except BaseException:
            type(self)._ordered_case_failed = True
            raise

    async def _start_local_pool(
        self,
    ) -> tuple[FakeStratumV1Server, Mapping[str, Any], str]:
        settings = self.settings_for("stratum_v1_regression")
        advertised_host = str(settings.get("advertised_host", "")).strip()
        if not advertised_host:
            self.fail(
                "tests.stratum_v1_regression.advertised_host must be the test "
                "host address reachable by the device"
            )

        server = FakeStratumV1Server(
            host=str(settings.get("bind_host", "0.0.0.0")),
            port=int(settings.get("port", 0)),
            extranonce1=str(settings.get("extranonce1", "01020304050607")),
            extranonce2_size=int(settings.get("extranonce2_size", 8)),
            version_mask=str(settings.get("version_mask", "1fffe000")),
        )
        await server.start()
        # Close first, then serialize all final disconnect events.  The generic
        # device cleanup subsequently restores the real pool configuration.
        self.addCleanup(
            server.write_transcript, self.artifacts.path / "fake-stratum.jsonl"
        )
        self.addAsyncCleanup(server.close)
        self.chart(
            f"Local Stratum server listening on port {server.port}", status="good"
        )

        username = str(settings.get("username", "stratum-regression.worker"))
        password_env = settings.get("temporary_password_env")
        if password_env is not None:
            password = os.environ.get(str(password_env))
            if password is None:
                self.fail(f"temporary password variable {password_env} is not set")
        else:
            password = None
            if not bool(settings.get("allow_existing_device_password", False)):
                self.fail(
                    "set tests.stratum_v1_regression.allow_existing_device_password=true "
                    "to let the device send its current write-only pool password to "
                    "this local process, or configure temporary_password_env together "
                    "with devices.options.baseline_stratum_password_env"
                )
        await self.device.configure_pool(
            PoolSettings(
                host=advertised_host,
                port=server.port,
                username=username,
                password=password,
                tls=False,
            )
        )
        return server, settings, username

    async def _wait_for_handshake(
        self,
        server: FakeStratumV1Server,
        settings: Mapping[str, Any],
        username: str,
    ) -> StratumHandshake:
        handshake = await server.wait_for_handshake(
            require_configure=True,
            timeout=float(settings.get("handshake_timeout", 45.0)),
        )
        authorize_params = handshake.authorize.params
        self.assertIsNotNone(authorize_params)
        assert authorize_params is not None
        self.assertEqual(authorize_params[0], username)
        return handshake

    @staticmethod
    def _latest_sequence(server: FakeStratumV1Server) -> int:
        return server.requests[-1].sequence if server.requests else 0

    async def _processing_barrier(
        self,
        server: FakeStratumV1Server,
        connection_id: int,
        barrier_id: int,
        *,
        timeout: float = 10.0,
    ) -> None:
        after = self._latest_sequence(server)
        await server.send_json(
            {"id": barrier_id, "method": "mining.ping", "params": []},
            session=connection_id,
            label=f"barrier:{barrier_id}",
        )
        await server.wait_for_request(
            "pong",
            connection_id=connection_id,
            after_sequence=after,
            predicate=lambda request: request.message_id == barrier_id,
            timeout=timeout,
        )

    async def _mine_one_share(
        self,
        server: FakeStratumV1Server,
        job: MiningJob,
        *,
        difficulty: float | None,
        connection_id: int,
        timeout: float,
        fragment_sizes: tuple[int, ...] | None = None,
        fragment_delay: float = 0.0,
    ) -> ShareSubmission:
        after = self._latest_sequence(server)
        await server.send_job(
            job,
            difficulty=difficulty,
            session=connection_id,
            fragment_sizes=fragment_sizes,
            fragment_delay=fragment_delay,
        )
        submission = await server.wait_for_submission(
            job_id=job.job_id,
            connection_id=connection_id,
            after_sequence=after,
            timeout=timeout,
        )
        self.assertEqual(submission.ntime.lower(), job.ntime.lower())
        self.assertRegex(submission.extranonce2, r"^[0-9a-fA-F]+$")
        self.assertRegex(submission.nonce, r"^[0-9a-fA-F]{8}$")
        if submission.version_bits is not None:
            self.assertRegex(submission.version_bits, r"^[0-9a-fA-F]{8}$")
        return submission

    async def _park_work(
        self,
        server: FakeStratumV1Server,
        connection_id: int,
        sequence: int,
    ) -> None:
        await server.send_job(
            MiningJob.standard(f"park-{sequence}"),
            difficulty=1.0e12,
            session=connection_id,
        )
        await self._processing_barrier(
            server, connection_id, 80_000 + sequence
        )

    async def _wait_for_pool_difficulty(
        self, expected: float, *, timeout: float
    ) -> None:
        async with asyncio.timeout(timeout):
            while True:
                info = await self.device.current_info()
                value = info.get("poolDifficulty")
                if value is not None and float(value) == expected:
                    return
                await asyncio.sleep(0.25)

    async def _case_01_configure_extension_negotiation(
        self,
        server: FakeStratumV1Server,
        settings: Mapping[str, Any],
    ) -> None:
        request = await server.wait_for_request(
            "mining.configure",
            timeout=float(settings.get("handshake_timeout", 45.0)),
        )
        self.assertIsNotNone(request.params)
        assert request.params is not None
        self.assertGreaterEqual(len(request.params), 1)
        self.assertIsInstance(request.params[0], list)
        self.assertIn("version-rolling", request.params[0])
        self.chart("mining.configure negotiated version rolling", status="good")

    async def _case_02_subscribe_request(
        self,
        server: FakeStratumV1Server,
        settings: Mapping[str, Any],
    ) -> None:
        request = await server.wait_for_request(
            "mining.subscribe",
            timeout=float(settings.get("handshake_timeout", 45.0)),
        )
        self.assertIsNotNone(request.params)
        assert request.params is not None
        self.assertGreaterEqual(len(request.params), 1)
        self.assertIsInstance(request.params[0], str)
        self.assertTrue(request.params[0])
        self.chart("mining.subscribe completed", status="good")

    async def _case_03_authorize_request(
        self,
        server: FakeStratumV1Server,
        settings: Mapping[str, Any],
        username: str,
    ) -> StratumHandshake:
        request = await server.wait_for_request(
            "mining.authorize",
            timeout=float(settings.get("handshake_timeout", 45.0)),
        )
        self.assertIsNotNone(request.params)
        assert request.params is not None
        self.assertGreaterEqual(len(request.params), 2)
        self.assertEqual(request.params[0], username)
        self.assertEqual(request.params[1], "<redacted>")
        self.chart("mining.authorize completed", status="good")
        return await self._wait_for_handshake(server, settings, username)

    async def _case_04_mining_notify_and_accepted_share(
        self,
        server: FakeStratumV1Server,
        handshake: StratumHandshake,
        settings: Mapping[str, Any],
        username: str,
    ) -> None:
        difficulty = float(settings.get("share_difficulty", 256.0))
        share_timeout = float(settings.get("share_timeout", 45.0))
        accept_timeout = float(settings.get("accept_timeout", 20.0))
        expected_extranonce2_chars = server.extranonce2_size * 2
        self.assertGreater(difficulty, 0)
        accepted_before = self.device.state.latest.shares_accepted

        job = MiningJob.standard("basic-mining-notify")
        server.submission_policy = lambda submission: submission.job_id == job.job_id
        submission = await self._mine_one_share(
            server,
            job,
            difficulty=difficulty,
            connection_id=handshake.connection_id,
            timeout=share_timeout,
        )
        self.assertEqual(submission.username, username)
        self.assertEqual(
            len(submission.extranonce2), expected_extranonce2_chars
        )
        await self._wait_for_pool_difficulty(difficulty, timeout=accept_timeout)
        self.chart("mining.notify produced a valid share", status="good")

        generation = self.device.state.generation
        await self.device.state.wait_for(
            lambda state: state.online and state.shares_accepted > accepted_before,
            timeout=accept_timeout,
            description="the fake-pool accepted-share response",
            after_generation=generation,
        )
        self.chart("Accepted share reached device state", status="good")

    async def _case_05_difficulty_change_and_fresh_work(
        self,
        server: FakeStratumV1Server,
        handshake: StratumHandshake,
        settings: Mapping[str, Any],
        username: str,
    ) -> None:
        initial_difficulty = float(settings.get("share_difficulty", 256.0))
        changed_difficulty = float(
            settings.get("changed_difficulty", initial_difficulty * 2.0)
        )
        share_timeout = float(settings.get("share_timeout", 45.0))
        accept_timeout = float(settings.get("accept_timeout", 20.0))
        self.assertGreater(initial_difficulty, 0)
        self.assertGreater(changed_difficulty, 0)
        self.assertNotEqual(changed_difficulty, initial_difficulty)

        job = MiningJob.standard("fresh-work-after-difficulty-change")
        server.submission_policy = lambda submission: submission.job_id == job.job_id
        await server.send_difficulty(
            initial_difficulty, session=handshake.connection_id
        )
        await self._wait_for_pool_difficulty(
            initial_difficulty, timeout=accept_timeout
        )
        await server.send_difficulty(
            changed_difficulty, session=handshake.connection_id
        )
        await self._wait_for_pool_difficulty(
            changed_difficulty, timeout=accept_timeout
        )
        self.chart(
            "mining.set_difficulty changed the device pool target", status="good"
        )

        accepted_before = self.device.state.latest.shares_accepted
        submission = await self._mine_one_share(
            server,
            job,
            difficulty=None,
            connection_id=handshake.connection_id,
            timeout=share_timeout,
        )
        self.assertEqual(submission.username, username)
        self.chart(
            "Fresh mining.notify used the changed difficulty and produced a share",
            status="good",
        )

        generation = self.device.state.generation
        await self.device.state.wait_for(
            lambda state: state.online and state.shares_accepted > accepted_before,
            timeout=accept_timeout,
            description="the changed-difficulty accepted-share response",
            after_generation=generation,
        )
        self.chart("Changed-difficulty share reached device state", status="good")

    def test_01_configure_extension_negotiation(self) -> None:
        self._run_ordered_case(
            self._case_01_configure_extension_negotiation(
                type(self)._server, type(self)._settings
            )
        )

    def test_02_subscribe_request(self) -> None:
        self._run_ordered_case(
            self._case_02_subscribe_request(
                type(self)._server, type(self)._settings
            )
        )

    def test_03_authorize_request(self) -> None:
        async def run() -> None:
            type(self)._handshake = await self._case_03_authorize_request(
                type(self)._server,
                type(self)._settings,
                type(self)._username,
            )

        self._run_ordered_case(run())

    def test_04_mining_notify_and_accepted_share(self) -> None:
        self._run_ordered_case(
            self._case_04_mining_notify_and_accepted_share(
                type(self)._server,
                type(self)._handshake,
                type(self)._settings,
                type(self)._username,
            )
        )

    def test_05_difficulty_change_and_fresh_work(self) -> None:
        self._run_ordered_case(
            self._case_05_difficulty_change_and_fresh_work(
                type(self)._server,
                type(self)._handshake,
                type(self)._settings,
                type(self)._username,
            )
        )

    @unittest.skip("requires the disabled Stratum parser hardening regressions")
    async def test_90_fragmented_consecutive_and_boundary_messages(self) -> None:
        server, settings, username = await self._start_local_pool()
        handshake = await self._wait_for_handshake(server, settings, username)
        difficulty = float(settings.get("share_difficulty", 256.0))
        timeout = float(settings.get("share_timeout", 45.0))
        connection_id = handshake.connection_id

        fragmented = MiningJob.standard("fragmented-notify")
        await self._mine_one_share(
            server,
            fragmented,
            difficulty=difficulty,
            connection_id=connection_id,
            timeout=timeout,
            fragment_sizes=(1, 2, 3, 5, 8, 13, 21, 34),
            fragment_delay=0.01,
        )
        self.chart("Fragmented JSON-RPC line produced valid work", status="good")

        consecutive = MiningJob.standard("consecutive-notify")
        after = self._latest_sequence(server)
        await server.send_batch(
            [
                {
                    "id": None,
                    "method": "mining.set_difficulty",
                    "params": [difficulty],
                },
                consecutive.notification(),
            ],
            session=connection_id,
            label="consecutive-difficulty-and-notify",
        )
        await server.wait_for_submission(
            job_id=consecutive.job_id,
            connection_id=connection_id,
            after_sequence=after,
            timeout=timeout,
        )
        self.chart("Consecutive JSON-RPC lines were both processed", status="good")

        boundary = MiningJob.standard("boundary-successor")
        maximum_object = b"{}" + b" " * (STRATUM_V1_MAX_JSON_LINE_SIZE - 2)
        notify_line = json.dumps(
            boundary.notification(), separators=(",", ":")
        ).encode("utf-8")
        after = self._latest_sequence(server)
        await server.send_raw(
            maximum_object + b"\n" + notify_line + b"\n",
            session=connection_id,
            label="maximum-line-and-successor",
        )
        await server.wait_for_submission(
            job_id=boundary.job_id,
            connection_id=connection_id,
            after_sequence=after,
            timeout=timeout,
        )
        self.chart("16 KiB line preserved and processed its successor", status="good")

        for label, invalid_bytes in (
            ("embedded NUL", b"{}\x00\n"),
            ("oversized line", b" " * (STRATUM_V1_MAX_JSON_LINE_SIZE + 1) + b"\n"),
        ):
            previous_connection = connection_id
            await server.send_raw(
                invalid_bytes,
                session=previous_connection,
                label=label.lower().replace(" ", "-"),
            )
            recovered = await server.wait_for_handshake(
                after_connection_id=previous_connection,
                require_configure=True,
                timeout=float(settings.get("reconnect_timeout", 45.0)),
            )
            connection_id = recovered.connection_id
            recovery_job = MiningJob.standard(
                f"recovery-{label.lower().replace(' ', '-')}"
            )
            await self._mine_one_share(
                server,
                recovery_job,
                difficulty=difficulty,
                connection_id=connection_id,
                timeout=timeout,
            )
            self.chart(f"Recovered after {label}", status="good")

    @unittest.skip("requires the disabled Stratum parser hardening regressions")
    async def test_91_invalid_messages_do_not_create_work_or_corrupt_state(self) -> None:
        server, settings, username = await self._start_local_pool()
        handshake = await self._wait_for_handshake(server, settings, username)
        difficulty = float(settings.get("share_difficulty", 256.0))
        timeout = float(settings.get("share_timeout", 45.0))
        no_submit_window = float(settings.get("invalid_submit_window", 0.1))
        connection_id = handshake.connection_id
        expected_extranonce2_chars = server.extranonce2_size * 2
        barrier = 90_000

        valid = MiningJob.standard("invalid-template")
        initial_info = await self.device.current_info()
        self.assertIn(
            "workReceived",
            initial_info,
            "target firmware must expose workReceived for side-effect assertions",
        )
        notify_cases: list[tuple[str, dict[str, Any]]] = []

        def changed_notify(name: str, index: int, value: Any) -> None:
            payload = valid.with_changes(job_id=f"bad-{name}").notification()
            payload["params"][index] = value
            notify_cases.append((name, payload))

        changed_notify("prevhash-type", 1, 7)
        changed_notify("nonhex-prevhash", 1, "gg" * 32)
        changed_notify("short-prevhash", 1, "00" * 31)
        changed_notify("invalid-merkle", 4, ["xyz"])
        changed_notify("too-many-merkle", 4, ["00" * 32] * 33)
        changed_notify("short-version", 5, "20")
        changed_notify("short-nbits", 6, "1d00ff")
        changed_notify("short-ntime", 7, "000000")
        changed_notify("odd-coinbase", 2, valid.coinbase_1 + "f")
        changed_notify("empty-coinbase", 2, "")
        changed_notify("clean-jobs-type", 8, "true")
        changed_notify("missing-locktime", 3, valid.coinbase_2[:-8])
        changed_notify("extra-locktime", 3, valid.coinbase_2 + "00")

        for index, (name, payload) in enumerate(notify_cases, start=1):
            with self.subTest(case=name):
                before = await self.device.current_info()
                work_before = int(before.get("workReceived") or 0)
                after = self._latest_sequence(server)
                await server.send_json(
                    payload,
                    session=connection_id,
                    label=f"invalid-notify:{name}",
                )
                barrier += 1
                await self._processing_barrier(
                    server, connection_id, barrier
                )
                await server.assert_no_submission(
                    job_id=str(payload["params"][0]),
                    after_sequence=after,
                    duration=no_submit_window,
                )
                work_after = int(
                    (await self.device.current_info()).get("workReceived") or 0
                )
                self.assertEqual(
                    work_after,
                    work_before,
                    f"invalid {name} notification changed workReceived",
                )

                recovery = MiningJob.standard(f"valid-after-{name}")
                submission = await self._mine_one_share(
                    server,
                    recovery,
                    difficulty=difficulty,
                    connection_id=connection_id,
                    timeout=timeout,
                )
                self.assertEqual(
                    len(submission.extranonce2), expected_extranonce2_chars
                )
                await self._park_work(server, connection_id, index)
                self.chart(f"Rejected {name} and accepted successor", status="good")

        state_cases: list[tuple[str, bytes]] = [
            ("non-object", b"[]\n"),
            ("trailing-json", b'{"id":1,"result":true} trailing\n'),
            (
                "fractional-id",
                b'{"id":1.5,"method":"mining.set_difficulty","params":[1]}\n',
            ),
            (
                "zero-difficulty",
                b'{"id":null,"method":"mining.set_difficulty","params":[0]}\n',
            ),
            (
                "negative-extranonce2",
                b'{"id":1,"method":"mining.set_extranonce","params":["deadbeef",-1]}\n',
            ),
            (
                "oversized-extranonce2",
                b'{"id":1,"method":"mining.set_extranonce","params":["deadbeef",33]}\n',
            ),
            (
                "fractional-extranonce2",
                b'{"id":1,"method":"mining.set_extranonce","params":["deadbeef",1.5]}\n',
            ),
            (
                "malformed-subscribe-result",
                b'{"result":[[],"deadbeef",-1],"id":2,"error":null}\n',
            ),
        ]

        for offset, (name, payload) in enumerate(state_cases, start=1):
            with self.subTest(case=name):
                state_before = await self.device.current_info()
                work_before = int(state_before.get("workReceived") or 0)
                difficulty_before = float(state_before.get("poolDifficulty") or 0)
                await server.send_raw(
                    payload,
                    session=connection_id,
                    label=f"invalid-message:{name}",
                )
                barrier += 1
                await self._processing_barrier(server, connection_id, barrier)
                state_after = await self.device.current_info()
                work_after = int(state_after.get("workReceived") or 0)
                self.assertEqual(work_after, work_before)
                if name in {"fractional-id", "zero-difficulty"}:
                    self.assertEqual(
                        float(state_after.get("poolDifficulty") or 0),
                        difficulty_before,
                        f"invalid {name} changed pool difficulty",
                    )

                recovery = MiningJob.standard(f"valid-after-{name}")
                submission = await self._mine_one_share(
                    server,
                    recovery,
                    difficulty=difficulty,
                    connection_id=connection_id,
                    timeout=timeout,
                )
                self.assertEqual(
                    len(submission.extranonce2), expected_extranonce2_chars
                )
                await self._park_work(server, connection_id, 100 + offset)
                self.chart(f"State survived {name}", status="good")
