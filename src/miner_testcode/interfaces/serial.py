from __future__ import annotations

import asyncio
import glob
import logging
import os
import termios
import time
from pathlib import Path
from typing import Any, Mapping

from ..artifacts import append_jsonl
from ..errors import InterfaceError
from ..redaction import redact_file

_BAUD_CONSTANTS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: getattr(termios, "B230400", termios.B115200),
}


class EspSerialInterface:
    """Non-blocking ESP serial capture plus a no-shell USB flashing hook."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        log_path: Path,
        event_path: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        port = config.get("port")
        if not isinstance(port, str) or not port:
            raise InterfaceError("serial.port must be a non-empty path or glob")
        self.port_spec = port
        self.baudrate = int(config.get("baudrate", 115200))
        self.capture_enabled = bool(config.get("capture", True))
        self.required = bool(config.get("required", False))
        command = config.get("flash_command", [])
        if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
            raise InterfaceError("serial.flash_command must be an array of strings")
        self.flash_command: tuple[str, ...] = tuple(command)
        self.flash_timeout = float(config.get("flash_timeout", 300.0))
        self.log_path = log_path
        self.event_path = event_path
        self.logger = logger or logging.getLogger(__name__)
        self._task: asyncio.Task[None] | None = None
        self._fd: int | None = None

    def resolve_port(self) -> Path:
        matches = sorted(glob.glob(self.port_spec))
        if not matches and Path(self.port_spec).exists():
            matches = [self.port_spec]
        if not matches:
            raise InterfaceError(f"serial port did not match any device: {self.port_spec}")
        if len(matches) != 1:
            raise InterfaceError(
                f"serial port must resolve to exactly one device, got {len(matches)}: {matches}"
            )
        return Path(matches[0]).resolve()

    def _open(self) -> tuple[int, Path]:
        port = self.resolve_port()
        fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            baud = _BAUD_CONSTANTS.get(self.baudrate)
            if baud is None:
                raise InterfaceError(f"unsupported serial capture baudrate: {self.baudrate}")
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = (attrs[2] & ~termios.CSIZE) | termios.CS8 | termios.CLOCAL | termios.CREAD
            attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.HUPCL)
            attrs[3] = 0
            attrs[4] = baud
            attrs[5] = baud
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            return fd, port
        except BaseException:
            os.close(fd)
            raise

    async def start_capture(self) -> None:
        if not self.capture_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._capture_loop(), name="esp-serial-capture")
        await asyncio.sleep(0)
        if self._task.done():
            exception = self._task.exception()
            self._task = None
            if exception:
                raise exception

    async def _capture_loop(self) -> None:
        fd, port = self._open()
        self._fd = fd
        append_jsonl(
            self.event_path,
            {"at": time.time(), "event": "serial_capture_started", "port": str(port)},
        )
        self.logger.info("capturing ESP serial logs from %s", port)
        try:
            with self.log_path.open("ab", buffering=0) as handle:
                while True:
                    try:
                        data = os.read(fd, 65536)
                    except BlockingIOError:
                        data = b""
                    if data:
                        handle.write(data)
                    else:
                        await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            append_jsonl(
                self.event_path,
                {"at": time.time(), "event": "serial_capture_error", "error": str(exc)},
            )
            if self.required:
                raise InterfaceError(f"serial capture failed: {exc}") from exc
            self.logger.warning("serial capture stopped: %s", exc)
        finally:
            os.close(fd)
            self._fd = None
            append_jsonl(
                self.event_path,
                {"at": time.time(), "event": "serial_capture_stopped"},
            )

    async def stop_capture(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        redact_file(self.log_path)

    async def flash(
        self,
        artifacts: Mapping[str, Path],
        *,
        output_path: Path,
    ) -> None:
        if not self.flash_command:
            raise InterfaceError("USB flashing requested without serial.flash_command")
        await self.stop_capture()
        port = self.resolve_port()
        substitutions = {"port": str(port), **{key: str(value) for key, value in artifacts.items()}}
        command: list[str] = []
        try:
            for argument in self.flash_command:
                command.append(argument.format_map(substitutions))
        except KeyError as exc:
            raise InterfaceError(
                f"serial.flash_command references unknown artifact {exc.args[0]!r}"
            ) from exc

        self.logger.info("flashing ESP firmware through %s", port)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=self.flash_timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise InterfaceError(
                f"USB flash command timed out after {self.flash_timeout:.1f}s"
            ) from exc
        output_path.write_bytes(output)
        if process.returncode != 0:
            raise InterfaceError(
                f"USB flash command failed with exit code {process.returncode}; "
                f"see {output_path}"
            )
        await self.start_capture()
