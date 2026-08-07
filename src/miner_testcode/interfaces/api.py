from __future__ import annotations

import asyncio
import http.client
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..artifacts import append_jsonl
from ..errors import InterfaceError


class _TransientInterfaceError(InterfaceError):
    """Transport failure for an operation that may be safe to retry."""


class HttpApiInterface:
    """Small async HTTP client with bounded responses and artifact tracing."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        retries: int = 2,
        retry_backoff: float = 0.5,
        read_only: bool = False,
        trace_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InterfaceError(f"invalid API base URL: {base_url!r}")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._base_path = parsed.path.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        if self.retries < 0:
            raise InterfaceError("API retries must not be negative")
        if self.retry_backoff < 0:
            raise InterfaceError("API retry backoff must not be negative")
        self.read_only = read_only
        self.trace_path = trace_path
        self.logger = logger or logging.getLogger(__name__)
        # ESP-Miner's embedded HTTP server can become unreliable when the
        # state monitor and a test issue requests at the same time.  Keep all
        # operations on this interface serialized while allowing the runner to
        # remain asynchronous across independent device interfaces.
        self._operation_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        default = 443 if self._scheme == "https" else 80
        port = "" if self._port == default else f":{self._port}"
        return f"{self._scheme}://{self._host}{port}{self._base_path}"

    def _connection(self, timeout: float | None = None) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=timeout or self.timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(
            self._host, self._port, timeout=timeout or self.timeout
        )

    def _trace(self, event: dict[str, Any]) -> None:
        if self.trace_path is not None:
            append_jsonl(self.trace_path, event)

    def _request_sync(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = 8 * 1024 * 1024,
        timeout: float | None = None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        if self.read_only and method.upper() not in {"GET", "HEAD"}:
            raise InterfaceError(f"HTTP {method.upper()} is blocked by read-only mode")
        request_path = f"{self._base_path}/{path.lstrip('/')}"
        if not request_path.startswith("/"):
            request_path = "/" + request_path
        started = time.monotonic()
        connection = self._connection(timeout)
        try:
            connection.request(method, request_path, body=body, headers=dict(headers or {}))
            response = connection.getresponse()
            payload = response.read(max_bytes + 1)
            truncated = len(payload) > max_bytes
            if truncated:
                payload = payload[:max_bytes]
            elapsed = time.monotonic() - started
            event = {
                "at": time.time(),
                "method": method,
                "path": request_path,
                "status": response.status,
                "elapsed_seconds": round(elapsed, 6),
                "response_bytes": len(payload),
                "truncated": truncated,
            }
            self._trace(event)
            self.logger.debug(
                "%s %s -> %d in %.3fs", method, request_path, response.status, elapsed
            )
            if truncated:
                raise InterfaceError(
                    f"{method} {request_path} exceeded the {max_bytes}-byte response limit"
                )
            if response.status < 200 or response.status >= 300:
                detail = payload[:300].decode("utf-8", errors="replace")
                raise InterfaceError(
                    f"{method} {request_path} returned HTTP {response.status}: {detail}"
                )
            return response.status, dict(response.getheaders()), payload
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            elapsed = time.monotonic() - started
            self._trace(
                {
                    "at": time.time(),
                    "method": method,
                    "path": request_path,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": round(elapsed, 6),
                }
            )
            raise _TransientInterfaceError(
                f"{method} {request_path} failed: {exc}"
            ) from exc
        finally:
            connection.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = 8 * 1024 * 1024,
        timeout: float | None = None,
    ) -> bytes:
        async with self._operation_lock:
            attempts = self.retries + 1 if method.upper() in {"GET", "HEAD"} else 1
            for attempt in range(attempts):
                try:
                    _, _, payload = await asyncio.to_thread(
                        self._request_sync,
                        method,
                        path,
                        body=body,
                        headers=headers,
                        max_bytes=max_bytes,
                        timeout=timeout,
                    )
                    break
                except _TransientInterfaceError as exc:
                    if attempt + 1 >= attempts:
                        raise InterfaceError(str(exc)) from exc
                    delay = self.retry_backoff * (2**attempt)
                    self.logger.warning(
                        "%s %s transient failure (%s); retrying in %.2fs (%d/%d)",
                        method.upper(),
                        path,
                        exc,
                        delay,
                        attempt + 1,
                        attempts - 1,
                    )
                    await asyncio.sleep(delay)
        return payload

    async def get_json(self, path: str) -> dict[str, Any]:
        payload = await self.request("GET", path, headers={"Accept": "application/json"})
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InterfaceError(f"GET {path} returned invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise InterfaceError(f"GET {path} returned {type(value).__name__}, expected object")
        return value

    async def patch_json(self, path: str, value: Mapping[str, Any]) -> bytes:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return await self.request(
            "PATCH",
            path,
            body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    async def post_json(self, path: str, value: Mapping[str, Any] | None = None) -> bytes:
        body = json.dumps(value or {}, separators=(",", ":")).encode("utf-8")
        return await self.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    def _upload_sync(
        self,
        path: str,
        source: Path,
        *,
        chunk_size: int,
        pace_seconds: float,
        timeout: float,
    ) -> bytes:
        if self.read_only:
            raise InterfaceError("firmware upload is blocked by read-only mode")
        if not source.is_file():
            raise InterfaceError(f"firmware artifact does not exist: {source}")
        request_path = f"{self._base_path}/{path.lstrip('/')}"
        if not request_path.startswith("/"):
            request_path = "/" + request_path
        size = source.stat().st_size
        started = time.monotonic()
        connection = self._connection(timeout)
        try:
            connection.putrequest("POST", request_path)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            with source.open("rb") as handle:
                while chunk := handle.read(chunk_size):
                    connection.send(chunk)
                    if pace_seconds > 0:
                        time.sleep(pace_seconds)
            response = connection.getresponse()
            payload = response.read(1024 * 1024)
            elapsed = time.monotonic() - started
            self._trace(
                {
                    "at": time.time(),
                    "method": "POST",
                    "path": request_path,
                    "status": response.status,
                    "elapsed_seconds": round(elapsed, 6),
                    "request_bytes": size,
                    "artifact": source.name,
                }
            )
            if response.status < 200 or response.status >= 300:
                detail = payload[:300].decode("utf-8", errors="replace")
                raise InterfaceError(
                    f"firmware upload to {request_path} returned HTTP "
                    f"{response.status}: {detail}"
                )
            return payload
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise InterfaceError(f"firmware upload to {request_path} failed: {exc}") from exc
        finally:
            connection.close()

    async def upload_file(
        self,
        path: str,
        source: Path,
        *,
        chunk_size: int = 4096,
        pace_seconds: float = 0.0,
        timeout: float = 180.0,
    ) -> bytes:
        async with self._operation_lock:
            return await asyncio.to_thread(
                self._upload_sync,
                path,
                source,
                chunk_size=chunk_size,
                pace_seconds=pace_seconds,
                timeout=timeout,
            )

    def _download_sync(self, path: str, destination: Path, max_bytes: int) -> bool:
        request_path = f"{self._base_path}/{path.lstrip('/')}"
        if not request_path.startswith("/"):
            request_path = "/" + request_path
        connection = self._connection()
        written = 0
        truncated = False
        started = time.monotonic()
        try:
            connection.request("GET", request_path)
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                detail = response.read(300).decode("utf-8", errors="replace")
                raise InterfaceError(
                    f"GET {request_path} returned HTTP {response.status}: {detail}"
                )
            with destination.open("wb") as handle:
                while chunk := response.read(min(65536, max_bytes - written + 1)):
                    remaining = max_bytes - written
                    if len(chunk) > remaining:
                        handle.write(chunk[:remaining])
                        written += remaining
                        truncated = True
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    if written == max_bytes:
                        extra = response.read(1)
                        truncated = bool(extra)
                        break
            self._trace(
                {
                    "at": time.time(),
                    "method": "GET",
                    "path": request_path,
                    "status": response.status,
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "response_bytes": written,
                    "truncated": truncated,
                    "destination": destination.name,
                }
            )
            return truncated
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise _TransientInterfaceError(f"GET {request_path} failed: {exc}") from exc
        finally:
            connection.close()

    async def download_to(self, path: str, destination: Path, *, max_bytes: int) -> bool:
        """Download to an artifact, returning whether the configured cap truncated it."""
        async with self._operation_lock:
            for attempt in range(self.retries + 1):
                try:
                    return await asyncio.to_thread(
                        self._download_sync, path, destination, max_bytes
                    )
                except _TransientInterfaceError as exc:
                    if attempt >= self.retries:
                        raise InterfaceError(str(exc)) from exc
                    delay = self.retry_backoff * (2**attempt)
                    self.logger.warning(
                        "GET %s transient download failure (%s); retrying in %.2fs (%d/%d)",
                        path,
                        exc,
                        delay,
                        attempt + 1,
                        self.retries,
                    )
                    await asyncio.sleep(delay)
        raise AssertionError("unreachable")
