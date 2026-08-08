from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


STRATUM_V1_MAX_JSON_LINE_SIZE = 16_384

# Captured, structurally valid coinbase split used by ESP-Miner's own Stratum
# and coinbase-decoder tests.  The split reserves 15 bytes for extranonce1 and
# extranonce2, matching the defaults below (7 + 8 bytes).
STANDARD_COINBASE_1 = (
    "0100000001000000000000000000000000000000000000000000000000000000"
    "0000000000ffffffff4b03a5020cfabe6d6d379ae882651f6469f2ed6b8b40a4"
    "f9a4b41fd838a3ad6de8cba775f4e8f1d3080100000000000000"
)
STANDARD_COINBASE_2 = (
    "41903d4c1b2f736c7573682f0000000003ca890d27000000001976a9147c154e"
    "d1dc59609e3d26abb2df2ea3d587cd8c4188ac00000000000000002c6a4c2952"
    "534b424c4f434b3a4cb4cb2ddfc37c41baf5ef6b6b4899e3253a8f1dfc7e5dd"
    "68a5b5b27005014ef0000000000000000266a24aa21a9ed5caa249f1af9fbf71"
    "c986fea8e076ca34ae3514fb2f86400561b28c7b15949bf00000000"
)


@dataclass(frozen=True, slots=True)
class MiningJob:
    """A Stratum V1 mining.notify template that can be copied and mutated."""

    job_id: str
    prev_hash: str = "00" * 32
    coinbase_1: str = STANDARD_COINBASE_1
    coinbase_2: str = STANDARD_COINBASE_2
    merkle_branches: tuple[str, ...] = ()
    version: str = "20000000"
    nbits: str = "1d00ffff"
    ntime: str = "00000000"
    clean_jobs: bool = True

    @classmethod
    def standard(cls, job_id: str, *, clean_jobs: bool = True) -> MiningJob:
        return cls(
            job_id=job_id,
            ntime=f"{int(time.time()) & 0xFFFFFFFF:08x}",
            clean_jobs=clean_jobs,
        )

    def with_changes(self, **changes: Any) -> MiningJob:
        return replace(self, **changes)

    def notification(self) -> dict[str, Any]:
        return {
            "id": None,
            "method": "mining.notify",
            "params": [
                self.job_id,
                self.prev_hash,
                self.coinbase_1,
                self.coinbase_2,
                list(self.merkle_branches),
                self.version,
                self.nbits,
                self.ntime,
                self.clean_jobs,
            ],
        }


@dataclass(frozen=True, slots=True)
class StratumRequest:
    sequence: int
    connection_id: int
    received_at: float
    raw: bytes
    payload: Any | None
    parse_error: str | None = None

    @property
    def method(self) -> str | None:
        if isinstance(self.payload, Mapping):
            value = self.payload.get("method")
            return value if isinstance(value, str) else None
        return None

    @property
    def message_id(self) -> int | str | None:
        if isinstance(self.payload, Mapping):
            value = self.payload.get("id")
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                return value
        return None

    @property
    def params(self) -> list[Any] | None:
        if isinstance(self.payload, Mapping):
            value = self.payload.get("params")
            return value if isinstance(value, list) else None
        return None


@dataclass(frozen=True, slots=True)
class ShareSubmission:
    sequence: int
    connection_id: int
    request_id: int | str | None
    username: str
    job_id: str
    extranonce2: str
    ntime: str
    nonce: str
    version_bits: str | None

    @classmethod
    def from_request(cls, request: StratumRequest) -> ShareSubmission:
        params = request.params
        if params is None or len(params) not in {5, 6}:
            raise ValueError("mining.submit must contain five or six params")
        if not all(isinstance(value, str) for value in params):
            raise ValueError("mining.submit params must all be strings")
        return cls(
            sequence=request.sequence,
            connection_id=request.connection_id,
            request_id=request.message_id,
            username=params[0],
            job_id=params[1],
            extranonce2=params[2],
            ntime=params[3],
            nonce=params[4],
            version_bits=params[5] if len(params) == 6 else None,
        )


@dataclass(frozen=True, slots=True)
class StratumHandshake:
    connection_id: int
    subscribe: StratumRequest
    authorize: StratumRequest
    configure: StratumRequest | None


@dataclass(slots=True)
class StratumSession:
    connection_id: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: Any
    connected_at: float
    closed_at: float | None = None

    @property
    def connected(self) -> bool:
        return self.closed_at is None and not self.writer.is_closing()


SubmissionPolicy = Callable[[ShareSubmission], bool | Awaitable[bool]]
RequestPredicate = Callable[[StratumRequest], bool]


class FakeStratumV1Server:
    """Scriptable asynchronous Stratum V1 server for device regressions.

    The server automatically handles setup requests, records every client
    request, and leaves pool-to-client traffic under test-script control.
    Raw and fragmented writes intentionally remain available for parser and
    transport hardening tests.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        extranonce1: str = "01020304050607",
        extranonce2_size: int = 8,
        version_mask: str = "1fffe000",
        accept_submissions: bool = True,
        submission_policy: SubmissionPolicy | None = None,
        client_line_limit: int = 1 << 20,
    ) -> None:
        if not 0 <= extranonce2_size <= 32:
            raise ValueError("extranonce2_size must be between 0 and 32")
        if len(extranonce1) % 2 or len(extranonce1) > 64:
            raise ValueError("extranonce1 must be even-length hex up to 32 bytes")
        if len(version_mask) != 8:
            raise ValueError("version_mask must be exactly four bytes of hex")
        try:
            bytes.fromhex(extranonce1)
            bytes.fromhex(version_mask)
        except ValueError as exc:
            raise ValueError("extranonce1 and version_mask must be hex strings") from exc

        self.host = host
        self.requested_port = port
        self.extranonce1 = extranonce1
        self.extranonce2_size = extranonce2_size
        self.version_mask = version_mask
        self.accept_submissions = accept_submissions
        self.submission_policy = submission_policy
        self.client_line_limit = client_line_limit

        self._server: asyncio.AbstractServer | None = None
        self._sessions: dict[int, StratumSession] = {}
        self._requests: list[StratumRequest] = []
        self._submissions: list[ShareSubmission] = []
        self._events: list[dict[str, Any]] = []
        self._condition = asyncio.Condition()
        self._next_connection_id = 1
        self._next_sequence = 1
        self._client_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> FakeStratumV1Server:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("fake Stratum server is not running")
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def requests(self) -> tuple[StratumRequest, ...]:
        return tuple(self._requests)

    @property
    def submissions(self) -> tuple[ShareSubmission, ...]:
        return tuple(self._submissions)

    @property
    def sessions(self) -> tuple[StratumSession, ...]:
        return tuple(self._sessions.values())

    @property
    def latest_connection_id(self) -> int | None:
        return max(self._sessions, default=None)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._client_connected,
            self.host,
            self.requested_port,
            limit=self.client_line_limit,
        )
        await self._record_event(
            "server_started", host=self.host, port=self.port
        )

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()

        sessions = list(self._sessions.values())
        for session in sessions:
            session.writer.close()

        tasks = list(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(
            self._serve_client(reader, writer), name="fake-stratum-client"
        )
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection_id = self._next_connection_id
        self._next_connection_id += 1
        session = StratumSession(
            connection_id=connection_id,
            reader=reader,
            writer=writer,
            peer=writer.get_extra_info("peername"),
            connected_at=time.time(),
        )
        self._sessions[connection_id] = session
        await self._record_event(
            "client_connected", connection_id=connection_id, peer=str(session.peer)
        )

        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    await self._record_event(
                        "client_line_error",
                        connection_id=connection_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    break
                if not line:
                    break
                raw = line.rstrip(b"\r\n")
                parse_error: str | None = None
                payload: Any | None = None
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    parse_error = f"{type(exc).__name__}: {exc}"

                stored_raw = raw
                if (
                    isinstance(payload, Mapping)
                    and payload.get("method") == "mining.authorize"
                ):
                    # The server does not authenticate clients. Drop the secret
                    # immediately instead of merely redacting it at artifact time.
                    payload = self._redact_payload(payload)
                    stored_raw = b"<redacted mining.authorize>"

                request = StratumRequest(
                    sequence=self._take_sequence(),
                    connection_id=connection_id,
                    received_at=time.time(),
                    raw=stored_raw,
                    payload=payload,
                    parse_error=parse_error,
                )
                async with self._condition:
                    self._requests.append(request)
                    self._events.append(self._request_event(request))
                    self._condition.notify_all()
                if parse_error is None and isinstance(payload, Mapping):
                    await self._handle_request(session, request)
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            if session.closed_at is None:
                session.closed_at = time.time()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (ConnectionError, TimeoutError):
                pass
            await self._record_event(
                "client_disconnected", connection_id=connection_id
            )

    async def _handle_request(
        self, session: StratumSession, request: StratumRequest
    ) -> None:
        method = request.method
        message_id = request.message_id
        if method == "mining.configure":
            await self.send_response(
                message_id,
                {
                    "version-rolling": True,
                    "version-rolling.mask": self.version_mask,
                },
                session=session,
            )
        elif method == "mining.subscribe":
            subscription_id = f"fake-{session.connection_id:08x}"
            await self.send_response(
                message_id,
                [
                    [["mining.notify", subscription_id]],
                    self.extranonce1,
                    self.extranonce2_size,
                ],
                session=session,
            )
        elif method == "mining.authorize":
            await self.send_response(message_id, True, session=session)
        elif method in {"mining.suggest_difficulty", "mining.extranonce.subscribe"}:
            await self.send_response(message_id, True, session=session)
        elif method == "mining.submit":
            accepted = False
            try:
                submission = ShareSubmission.from_request(request)
                async with self._condition:
                    self._submissions.append(submission)
                    self._condition.notify_all()
                accepted = self.accept_submissions
                if self.submission_policy is not None:
                    decision = self.submission_policy(submission)
                    accepted = bool(
                        await decision if inspect.isawaitable(decision) else decision
                    )
            except ValueError as exc:
                await self._record_event(
                    "invalid_submission",
                    connection_id=session.connection_id,
                    error=str(exc),
                )
            await self.send_response(
                message_id,
                accepted,
                error=None if accepted else [23, "low difficulty share", None],
                session=session,
            )

    async def send_response(
        self,
        message_id: int | str | None,
        result: Any,
        *,
        error: Any = None,
        session: StratumSession | int | None = None,
        fragment_sizes: Sequence[int] | None = None,
    ) -> None:
        await self.send_json(
            {"id": message_id, "result": result, "error": error},
            session=session,
            fragment_sizes=fragment_sizes,
            label="response",
        )

    async def send_notification(
        self,
        method: str,
        params: Sequence[Any],
        *,
        session: StratumSession | int | None = None,
        fragment_sizes: Sequence[int] | None = None,
        fragment_delay: float = 0.0,
    ) -> None:
        await self.send_json(
            {"id": None, "method": method, "params": list(params)},
            session=session,
            fragment_sizes=fragment_sizes,
            fragment_delay=fragment_delay,
            label=method,
        )

    async def send_difficulty(
        self,
        difficulty: float,
        *,
        session: StratumSession | int | None = None,
    ) -> None:
        await self.send_notification(
            "mining.set_difficulty", [difficulty], session=session
        )

    async def send_job(
        self,
        job: MiningJob,
        *,
        difficulty: float | None = None,
        session: StratumSession | int | None = None,
        fragment_sizes: Sequence[int] | None = None,
        fragment_delay: float = 0.0,
    ) -> None:
        if difficulty is not None:
            await self.send_difficulty(difficulty, session=session)
        await self.send_json(
            job.notification(),
            session=session,
            fragment_sizes=fragment_sizes,
            fragment_delay=fragment_delay,
            label=f"mining.notify:{job.job_id}",
        )

    async def send_json(
        self,
        payload: Any,
        *,
        session: StratumSession | int | None = None,
        fragment_sizes: Sequence[int] | None = None,
        fragment_delay: float = 0.0,
        label: str = "json",
    ) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        await self.send_raw(
            data,
            session=session,
            fragment_sizes=fragment_sizes,
            fragment_delay=fragment_delay,
            label=label,
            payload=payload,
        )

    async def send_batch(
        self,
        payloads: Sequence[Any],
        *,
        session: StratumSession | int | None = None,
        fragment_sizes: Sequence[int] | None = None,
        fragment_delay: float = 0.0,
        label: str = "json_batch",
    ) -> None:
        data = b"".join(
            json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
            for payload in payloads
        )
        await self.send_raw(
            data,
            session=session,
            fragment_sizes=fragment_sizes,
            fragment_delay=fragment_delay,
            label=label,
            payload=list(payloads),
        )

    async def send_raw(
        self,
        data: bytes,
        *,
        session: StratumSession | int | None = None,
        fragment_sizes: Sequence[int] | None = None,
        fragment_delay: float = 0.0,
        label: str = "raw",
        payload: Any | None = None,
    ) -> None:
        target = self._resolve_session(session)
        await self._record_event(
            "server_sent",
            connection_id=target.connection_id,
            label=label,
            size=len(data),
            payload=self._redact_payload(payload),
        )
        if not fragment_sizes:
            target.writer.write(data)
            await target.writer.drain()
            return

        offset = 0
        for requested_size in fragment_sizes:
            if requested_size <= 0:
                raise ValueError("fragment sizes must be positive")
            if offset >= len(data):
                break
            end = min(offset + requested_size, len(data))
            target.writer.write(data[offset:end])
            await target.writer.drain()
            offset = end
            if fragment_delay:
                await asyncio.sleep(fragment_delay)
        if offset < len(data):
            target.writer.write(data[offset:])
            await target.writer.drain()

    async def wait_for_connection(
        self, *, after_connection_id: int = 0, timeout: float = 10.0
    ) -> StratumSession:
        async with asyncio.timeout(timeout):
            async with self._condition:
                while True:
                    candidates = [
                        session
                        for session in self._sessions.values()
                        if session.connection_id > after_connection_id
                    ]
                    if candidates:
                        return min(candidates, key=lambda item: item.connection_id)
                    await self._condition.wait()

    async def wait_for_request(
        self,
        method: str | None = None,
        *,
        connection_id: int | None = None,
        after_sequence: int = 0,
        predicate: RequestPredicate | None = None,
        timeout: float = 10.0,
    ) -> StratumRequest:
        async with asyncio.timeout(timeout):
            async with self._condition:
                while True:
                    for request in self._requests:
                        if request.sequence <= after_sequence:
                            continue
                        if connection_id is not None and request.connection_id != connection_id:
                            continue
                        if method is not None and request.method != method:
                            continue
                        if predicate is not None and not predicate(request):
                            continue
                        return request
                    await self._condition.wait()

    async def wait_for_handshake(
        self,
        *,
        connection_id: int | None = None,
        after_connection_id: int = 0,
        require_configure: bool = False,
        timeout: float = 15.0,
    ) -> StratumHandshake:
        async with asyncio.timeout(timeout):
            if connection_id is None:
                session = await self.wait_for_connection(
                    after_connection_id=after_connection_id, timeout=timeout
                )
                connection_id = session.connection_id
            subscribe = await self.wait_for_request(
                "mining.subscribe", connection_id=connection_id, timeout=timeout
            )
            authorize = await self.wait_for_request(
                "mining.authorize", connection_id=connection_id, timeout=timeout
            )
            configure = next(
                (
                    request
                    for request in self._requests
                    if request.connection_id == connection_id
                    and request.method == "mining.configure"
                ),
                None,
            )
            if require_configure and configure is None:
                configure = await self.wait_for_request(
                    "mining.configure", connection_id=connection_id, timeout=timeout
                )
            return StratumHandshake(
                connection_id=connection_id,
                subscribe=subscribe,
                authorize=authorize,
                configure=configure,
            )

    async def wait_for_submission(
        self,
        *,
        job_id: str | None = None,
        connection_id: int | None = None,
        after_sequence: int = 0,
        timeout: float = 30.0,
    ) -> ShareSubmission:
        async with asyncio.timeout(timeout):
            async with self._condition:
                while True:
                    for submission in self._submissions:
                        if submission.sequence <= after_sequence:
                            continue
                        if job_id is not None and submission.job_id != job_id:
                            continue
                        if (
                            connection_id is not None
                            and submission.connection_id != connection_id
                        ):
                            continue
                        return submission
                    await self._condition.wait()

    async def assert_no_submission(
        self,
        *,
        job_id: str,
        after_sequence: int = 0,
        duration: float = 1.0,
    ) -> None:
        try:
            submission = await self.wait_for_submission(
                job_id=job_id,
                after_sequence=after_sequence,
                timeout=duration,
            )
        except TimeoutError:
            return
        raise AssertionError(
            f"unexpected mining.submit for rejected job {submission.job_id!r}"
        )

    def write_transcript(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "".join(
                json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
                for event in self._events
            ),
            encoding="utf-8",
        )

    def _resolve_session(
        self, session: StratumSession | int | None
    ) -> StratumSession:
        if isinstance(session, StratumSession):
            target = session
        elif isinstance(session, int):
            target = self._sessions.get(session)
            if target is None:
                raise LookupError(f"unknown Stratum connection {session}")
        else:
            connected = [item for item in self._sessions.values() if item.connected]
            if not connected:
                raise RuntimeError("no connected Stratum client")
            target = max(connected, key=lambda item: item.connection_id)
        if not target.connected:
            raise ConnectionError(
                f"Stratum connection {target.connection_id} is closed"
            )
        return target

    def _take_sequence(self) -> int:
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    async def _record_event(self, event: str, **fields: Any) -> None:
        async with self._condition:
            self._events.append({"at": time.time(), "event": event, **fields})
            self._condition.notify_all()

    def _request_event(self, request: StratumRequest) -> dict[str, Any]:
        return {
            "at": request.received_at,
            "event": "client_request",
            "sequence": request.sequence,
            "connection_id": request.connection_id,
            "size": len(request.raw),
            "payload": self._redact_payload(request.payload),
            "parse_error": request.parse_error,
        }

    @classmethod
    def _redact_payload(cls, payload: Any) -> Any:
        if not isinstance(payload, Mapping):
            return payload
        redacted = dict(payload)
        if redacted.get("method") == "mining.authorize":
            params = redacted.get("params")
            if isinstance(params, list) and len(params) >= 2:
                redacted["params"] = [params[0], "<redacted>", *params[2:]]
        return redacted
