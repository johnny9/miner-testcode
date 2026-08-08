from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .. import capabilities as caps
from ..artifacts import TestArtifacts, append_jsonl
from ..config import DeviceConfig
from ..errors import ConfigError, DeviceError, InterfaceError, UpgradeError
from ..interfaces.api import HttpApiInterface
from ..interfaces.serial import EspSerialInterface
from ..interfaces.websocket import JsonWebSocketInterface
from ..redaction import redact_file
from ..state import DeviceState, DeviceStateStore
from ..telemetry import STANDARD_MINING_METRICS, TelemetryCapture
from .base import CleanState, MiningDevice, PoolSettings

_RESTORABLE_POOL_FIELDS = (
    "stratumURL",
    "stratumPort",
    "stratumUser",
    "stratumSuggestedDifficulty",
    "stratumProtocol",
    "stratumTLS",
    "stratumExtranonceSubscribe",
    "stratumDecodeCoinbase",
)

_POOL_SELECTION_FIELDS = (
    "primaryPoolIndex",
    "secondaryPoolIndex",
    "useFallbackStratum",
)

_MASKED_STRATUM_PASSWORD = "*****"


class BitaxeDevice(MiningDevice):
    """Common ESP-Miner/AxeOS adapter for model-specific Bitaxe profiles."""

    device_label = "ESP-Miner device"
    board_prefix = ""
    asic_model = ""

    def __init__(
        self,
        config: DeviceConfig,
        *,
        project_dir: Path,
        artifacts: TestArtifacts,
        logger: logging.Logger,
    ) -> None:
        self.name = config.name
        self.config = config
        self.project_dir = project_dir
        self.artifacts = artifacts
        self.logger = logger
        self.state = DeviceStateStore()
        self._monitor_task: asyncio.Task[None] | None = None
        self._telemetry_task: asyncio.Task[None] | None = None
        self._telemetry_ready = asyncio.Event()
        self._telemetry_ws_connected = False
        self._telemetry_cache: dict[str, Any] = {}
        self._closed = False
        self.read_only = bool(config.options.get("read_only", False))
        self._known_baseline_password: str | None = None
        self._mutated_settings: set[str] = set()

        api_config = config.interface("api", required=True)
        base_url = api_config.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ConfigError(f"device {self.name!r} api.base_url is required")
        self.poll_interval = float(api_config.get("poll_interval", 0.5))
        if self.poll_interval <= 0:
            raise ConfigError(f"device {self.name!r} api.poll_interval must be positive")
        self.online_timeout = float(api_config.get("online_timeout", 120.0))
        self.log_max_bytes = int(api_config.get("log_max_bytes", 64 * 1024 * 1024))
        self.api = HttpApiInterface(
            base_url,
            timeout=float(api_config.get("timeout", 5.0)),
            retries=int(api_config.get("retries", 2)),
            retry_backoff=float(api_config.get("retry_backoff", 0.5)),
            read_only=self.read_only,
            trace_path=artifacts.api_trace_path,
            logger=logger,
        )

        websocket_config = config.interface("websocket")
        websocket_enabled = bool(websocket_config.get("enabled", True))
        websocket_url = websocket_config.get("url")
        if websocket_url is not None and not isinstance(websocket_url, str):
            raise ConfigError(f"device {self.name!r} websocket.url must be a string")
        if not websocket_url:
            parsed_api_url = urlsplit(base_url)
            websocket_url = urlunsplit(
                (
                    "wss" if parsed_api_url.scheme == "https" else "ws",
                    parsed_api_url.netloc,
                    "/api/ws/live",
                    "",
                    "",
                )
            )
        parsed_websocket_url = urlsplit(websocket_url)
        if (
            parsed_websocket_url.scheme not in {"ws", "wss"}
            or not parsed_websocket_url.netloc
        ):
            raise ConfigError(
                f"device {self.name!r} websocket.url must be an absolute ws:// or wss:// URL"
            )
        self.telemetry_reconnect_delay = float(
            websocket_config.get("reconnect_delay", 2.0)
        )
        if self.telemetry_reconnect_delay <= 0:
            raise ConfigError("websocket.reconnect_delay must be positive")
        self.telemetry_required = bool(websocket_config.get("required", False))
        self.telemetry_connect_timeout = float(
            websocket_config.get("connect_timeout", 5.0)
        )
        if self.telemetry_connect_timeout <= 0:
            raise ConfigError("websocket.connect_timeout must be positive")
        telemetry_ping_interval = float(websocket_config.get("ping_interval", 20.0))
        if telemetry_ping_interval <= 0:
            raise ConfigError("websocket.ping_interval must be positive")
        telemetry_max_message_bytes = int(
            websocket_config.get("max_message_bytes", 2 * 1024 * 1024)
        )
        if telemetry_max_message_bytes < 1024:
            raise ConfigError("websocket.max_message_bytes must be at least 1024")
        self.websocket = (
            JsonWebSocketInterface(
                websocket_url,
                open_timeout=self.telemetry_connect_timeout,
                ping_interval=telemetry_ping_interval,
                max_message_bytes=telemetry_max_message_bytes,
            )
            if websocket_enabled
            else None
        )
        self.telemetry = TelemetryCapture(
            STANDARD_MINING_METRICS,
            event_path=artifacts.telemetry_path,
            max_samples=int(websocket_config.get("max_samples", 50_000)),
        )

        serial_config = config.interface("serial")
        self.serial: EspSerialInterface | None = None
        if serial_config:
            self.serial = EspSerialInterface(
                serial_config,
                log_path=artifacts.serial_path,
                event_path=artifacts.events_path,
                logger=logger,
            )

        available = {
            caps.API,
            caps.DEVICE_LOGS,
            caps.MINING_STATE,
            caps.OTA_UPGRADE,
            caps.POOL_CONFIG,
            caps.STRATUM_V1,
            caps.TELEMETRY,
        }
        if self.serial is not None:
            available.add(caps.SERIAL_LOG)
            if self.serial.flash_command:
                available.add(caps.USB_FLASH)
        self.capabilities = frozenset(available)

    @classmethod
    def identity_matches(cls, info: Mapping[str, Any]) -> bool:
        board = str(info.get("boardVersion", ""))
        asic = str(info.get("ASICModel", ""))
        return board.startswith(cls.board_prefix) and asic == cls.asic_model

    @classmethod
    def state_from_info(cls, info: Mapping[str, Any]) -> DeviceState:
        health = info.get("asicHealth")
        if not isinstance(health, dict):
            health = {}
        lifecycle = health.get("lifecycle")
        hashrate_ghs = float(info.get("hashRate") or 0.0)
        fault_code = int(health.get("lastFaultCode") or 0)
        system_fault = bool(
            info.get("hardware_fault")
            or info.get("power_fault")
            or info.get("overheat_mode")
        )
        if fault_code == 0 and system_fault:
            fault_code = 1
        lifecycle_name = str(lifecycle).upper() if lifecycle is not None else None
        lifecycle_blocks_mining = lifecycle_name in {
            "FAULT",
            "MAINTENANCE",
            "PAUSED",
            "SAFE_OFF",
            "STOPPED",
        }
        mining_active = bool(
            hashrate_ghs > 0.0
            and not info.get("miningPaused")
            and fault_code == 0
            and not lifecycle_blocks_mining
        )
        return DeviceState(
            observed_at=time.time(),
            online=True,
            identity_ok=cls.identity_matches(info),
            lifecycle=str(lifecycle) if lifecycle is not None else None,
            mining_active=mining_active,
            hashrate_ghs=hashrate_ghs,
            shares_accepted=int(info.get("sharesAccepted") or 0),
            shares_rejected=int(info.get("sharesRejected") or 0),
            active_engines=(
                int(health["activeEngineCount"])
                if health.get("activeEngineCount") is not None
                else None
            ),
            expected_engines=(
                int(health["expectedEngineCount"])
                if health.get("expectedEngineCount") is not None
                else None
            ),
            pool_host=str(info.get("stratumURL")) if info.get("stratumURL") else None,
            pool_port=(int(info["stratumPort"]) if info.get("stratumPort") else None),
            current_work_age_seconds=(
                float(info["currentWorkAgeSeconds"])
                if info.get("currentWorkAgeSeconds") is not None
                else None
            ),
            uptime_seconds=(
                int(info["uptimeSeconds"]) if info.get("uptimeSeconds") is not None else None
            ),
            fault_code=fault_code,
            raw=dict(info),
        )

    @staticmethod
    def telemetry_from_info(info: Mapping[str, Any]) -> dict[str, float]:
        health = info.get("asicHealth")
        if not isinstance(health, Mapping):
            health = {}

        def first_number(*values: Any) -> float | None:
            for value in values:
                if value is None or isinstance(value, bool):
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
            return None

        values = {
            "hashrate_ghs": first_number(info.get("hashRate")),
            "temperature_c": first_number(
                health.get("boardTemperatureC"), info.get("temp")
            ),
            "frequency_mhz": first_number(
                info.get("actualFrequency"),
                health.get("fixedFrequencyMHz"),
                info.get("frequency"),
            ),
            "fan_rpm": first_number(health.get("fanRPM"), info.get("fanrpm")),
        }
        return {key: value for key, value in values.items() if value is not None}

    async def _publish_info(
        self, info: Mapping[str, Any], *, source: str = "api"
    ) -> DeviceState:
        state = self.state_from_info(info)
        await self.state.update(state)
        append_jsonl(self.artifacts.state_path, state.as_event())
        if source == "websocket" or not self._telemetry_ws_connected:
            self.telemetry.record_sample(
                self.telemetry_from_info(info),
                source=source,
                observed_at=state.observed_at,
            )
        return state

    async def current_info(self) -> Mapping[str, Any]:
        info = await self.api.get_json("/api/system/info")
        await self._publish_info(info)
        return info

    async def start(self) -> None:
        info = await self.current_info()
        state = self.state.latest
        if not state.identity_ok:
            raise DeviceError(
                f"{self.name} is not a {self.device_label}: "
                f"board={info.get('boardVersion')!r}, ASIC={info.get('ASICModel')!r}"
            )
        self.logger.info(
            "identified %s at %s: board=%s ASIC=%s firmware=%s",
            self.name,
            self.api.base_url,
            info.get("boardVersion"),
            info.get("ASICModel"),
            info.get("version"),
        )
        if self.serial is not None:
            try:
                await self.serial.start_capture()
            except InterfaceError:
                if self.serial.required:
                    raise
                self.logger.warning("serial capture is unavailable", exc_info=True)
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name=f"{self.name}-state-monitor"
        )
        if self.websocket is not None:
            self._telemetry_task = asyncio.create_task(
                self._telemetry_loop(), name=f"{self.name}-telemetry-monitor"
            )
            if self.telemetry_required:
                try:
                    await asyncio.wait_for(
                        self._telemetry_ready.wait(), self.telemetry_connect_timeout
                    )
                except TimeoutError as exc:
                    raise DeviceError(
                        f"required WebSocket telemetry did not start for {self.name}"
                    ) from exc

    async def _monitor_loop(self) -> None:
        previously_online = True
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.current_info()
                if not previously_online:
                    self.logger.info("%s API recovered", self.name)
                previously_online = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state = DeviceState.offline(f"{type(exc).__name__}: {exc}")
                await self.state.update(state)
                append_jsonl(self.artifacts.state_path, state.as_event())
                if previously_online:
                    self.telemetry.record_gap(source="api", observed_at=state.observed_at)
                    self.logger.warning("%s API became unavailable: %s", self.name, exc)
                previously_online = False

    @staticmethod
    def _merge_json_diff(target: dict[str, Any], diff: Mapping[str, Any]) -> None:
        for key, value in diff.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                BitaxeDevice._merge_json_diff(target[key], value)
            elif isinstance(value, Mapping):
                nested: dict[str, Any] = {}
                BitaxeDevice._merge_json_diff(nested, value)
                target[key] = nested
            else:
                target[key] = value

    async def _telemetry_loop(self) -> None:
        assert self.websocket is not None
        logged_unavailable = False
        while True:
            try:
                async for message in self.websocket.messages():
                    data = message.get("data")
                    if message.get("event") != "update" or not isinstance(data, Mapping):
                        continue
                    self._merge_json_diff(self._telemetry_cache, data)
                    self._telemetry_ws_connected = True
                    await self._publish_info(
                        self._telemetry_cache, source="websocket"
                    )
                    if not self._telemetry_ready.is_set():
                        self._telemetry_ready.set()
                        self.logger.info(
                            "WebSocket telemetry started for %s", self.name
                        )
                    logged_unavailable = False
                raise InterfaceError("WebSocket telemetry stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._telemetry_ws_connected = False
                if not logged_unavailable:
                    self.logger.warning(
                        "WebSocket telemetry unavailable for %s; using API polling (%s)",
                        self.name,
                        type(exc).__name__,
                    )
                    logged_unavailable = True
                await asyncio.sleep(self.telemetry_reconnect_delay)

    async def snapshot_clean_state(self) -> CleanState:
        info = await self.current_info()
        pools = self._pool_entries(info)
        if pools is not None:
            settings: dict[str, Any] = {"pools": pools}
            settings.update(
                {
                    key: info[key]
                    for key in _POOL_SELECTION_FIELDS
                    if key in info
                }
            )
        else:
            settings = {
                key: info[key] for key in _RESTORABLE_POOL_FIELDS if key in info
            }
        baseline_password_env = self.config.options.get("baseline_stratum_password_env")
        if baseline_password_env is not None:
            if not isinstance(baseline_password_env, str) or not baseline_password_env:
                raise ConfigError("options.baseline_stratum_password_env must be a variable name")
            baseline_password = os.environ.get(baseline_password_env)
            if baseline_password is None:
                raise ConfigError(
                    f"required baseline password variable {baseline_password_env} is not set"
                )
            if pools is not None:
                _, primary_pool = self._primary_pool(
                    pools, info.get("primaryPoolIndex", 0)
                )
                primary_pool["stratumPassword"] = baseline_password
            else:
                settings["stratumPassword"] = baseline_password
            self._known_baseline_password = baseline_password
        baseline = CleanState(
            settings=settings,
            mining_paused=bool(info.get("miningPaused", False)),
        )
        (self.artifacts.path / "baseline.json").write_text(
            json.dumps(
                {
                    "settings": self._redacted_pool_settings(settings),
                    "mining_paused": baseline.mining_paused,
                    "version": info.get("version"),
                    "boardVersion": info.get("boardVersion"),
                    "ASICModel": info.get("ASICModel"),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return baseline

    @staticmethod
    def _pool_entries(info: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        value = info.get("pools")
        if not isinstance(value, list) or not value:
            return None
        if not all(isinstance(pool, Mapping) for pool in value):
            return None
        return [dict(pool) for pool in value]

    @staticmethod
    def _primary_pool(
        pools: list[dict[str, Any]], primary_pool_index: Any
    ) -> tuple[int, dict[str, Any]]:
        try:
            selected = int(primary_pool_index)
        except (TypeError, ValueError):
            selected = 0
        for list_index, pool in enumerate(pools):
            try:
                pool_id = int(pool.get("id"))
            except (TypeError, ValueError):
                continue
            if pool_id == selected:
                return list_index, pool
        if 0 <= selected < len(pools):
            return selected, pools[selected]
        return 0, pools[0]

    @staticmethod
    def _redacted_pool_settings(value: Any, *, key: str | None = None) -> Any:
        if key in {"stratumUser", "stratumPassword"}:
            return "<redacted>"
        if isinstance(value, Mapping):
            return {
                item_key: BitaxeDevice._redacted_pool_settings(
                    item_value, key=str(item_key)
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [BitaxeDevice._redacted_pool_settings(item) for item in value]
        return value

    @staticmethod
    def _comparable_pools(pools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        comparable: list[dict[str, Any]] = []
        for pool in pools:
            item = dict(pool)
            if "stratumPassword" in item:
                item["stratumPassword"] = _MASKED_STRATUM_PASSWORD
            comparable.append(item)
        return comparable

    @classmethod
    def _pool_expected_settings(
        cls, pools: list[dict[str, Any]], primary_pool_index: Any
    ) -> dict[str, Any]:
        _, primary_pool = cls._primary_pool(pools, primary_pool_index)
        return {
            key: primary_pool[key]
            for key in _RESTORABLE_POOL_FIELDS
            if key in primary_pool
        }

    async def restore_clean_state(self, baseline: CleanState) -> None:
        self.logger.info("restoring clean state for %s", self.name)
        info = await self.current_info()
        baseline_pools = self._pool_entries(baseline.settings)
        if baseline_pools is not None:
            current_pools = self._pool_entries(info)
            pools_changed = current_pools is None or self._comparable_pools(
                current_pools
            ) != self._comparable_pools(baseline_pools)
            password_changed = "stratumPassword" in self._mutated_settings
            update: dict[str, Any] = {}
            if pools_changed or password_changed:
                update["pools"] = baseline_pools
            update.update(
                {
                    key: value
                    for key, value in baseline.settings.items()
                    if key in _POOL_SELECTION_FIELDS and info.get(key) != value
                }
            )
            primary_pool_index = baseline.settings.get("primaryPoolIndex", 0)
            expected = self._pool_expected_settings(
                baseline_pools, primary_pool_index
            )
            expected.update(
                {
                    key: value
                    for key, value in baseline.settings.items()
                    if key in _POOL_SELECTION_FIELDS
                }
            )
        else:
            update = {
                key: value
                for key, value in baseline.settings.items()
                if (
                    key == "stratumPassword" and key in self._mutated_settings
                )
                or (key != "stratumPassword" and info.get(key) != value)
            }
            expected = {
                key: value
                for key, value in baseline.settings.items()
                if key != "stratumPassword"
            }
        pause_changed = bool(info.get("miningPaused", False)) != baseline.mining_paused
        if self.read_only and (update or pause_changed):
            raise DeviceError(
                f"{self.name} changed during a read-only test; "
                f"settings={sorted(update)}, "
                f"miningPaused={info.get('miningPaused')}"
            )
        if update:
            await self.api.patch_json("/api/system", update)
            await self._restart_and_wait(expected=expected)

        info = await self.current_info()
        paused = bool(info.get("miningPaused", False))
        if paused != baseline.mining_paused:
            endpoint = "/api/system/pause" if baseline.mining_paused else "/api/system/resume"
            await self.api.post_json(endpoint)
            await self.current_info()

        final = await self.current_info()
        if baseline_pools is not None:
            final_pools = self._pool_entries(final)
            mismatches: dict[str, Any] = {}
            if final_pools is None or self._comparable_pools(
                final_pools
            ) != self._comparable_pools(baseline_pools):
                mismatches["pools"] = "configuration differs"
            mismatches.update(
                {
                    key: (final.get(key), value)
                    for key, value in baseline.settings.items()
                    if key in _POOL_SELECTION_FIELDS and final.get(key) != value
                }
            )
        else:
            mismatches = {
                key: (final.get(key), value)
                for key, value in baseline.settings.items()
                if key != "stratumPassword" and final.get(key) != value
            }
        if mismatches or bool(final.get("miningPaused", False)) != baseline.mining_paused:
            raise DeviceError(
                f"failed to restore clean state for {self.name}: "
                f"settings={mismatches}, miningPaused={final.get('miningPaused')}"
            )

    async def configure_pool(self, pool: PoolSettings) -> None:
        info = await self.current_info()
        desired: dict[str, Any] = {
            "stratumURL": pool.host,
            "stratumPort": pool.port,
            "stratumUser": pool.username,
            "stratumProtocol": "SV1",
            "stratumTLS": 1 if pool.tls else 0,
        }
        if pool.password is not None:
            if self._known_baseline_password is None:
                raise DeviceError(
                    "refusing to change write-only stratumPassword without "
                    "devices.options.baseline_stratum_password_env for cleanup"
                )
            desired["stratumPassword"] = pool.password
        if pool.suggested_difficulty is not None:
            desired["stratumSuggestedDifficulty"] = pool.suggested_difficulty
        pools = self._pool_entries(info)
        comparison = info
        if pools is not None:
            _, primary_pool = self._primary_pool(
                pools, info.get("primaryPoolIndex", 0)
            )
            comparison = primary_pool
        changed = {
            key: value
            for key, value in desired.items()
            if key == "stratumPassword" or comparison.get(key) != value
        }
        if self.read_only and changed:
            visible_changes = sorted(key for key in changed if key != "stratumPassword")
            raise DeviceError(
                f"read-only device {self.name} does not already match the requested pool; "
                f"differing fields: {', '.join(visible_changes) or 'write-only password'}"
            )
        if not changed:
            self.logger.info("pool settings already match %s:%d", pool.host, pool.port)
            return
        self.logger.info("applying test pool %s:%d", pool.host, pool.port)
        if pools is not None:
            primary_index, primary_pool = self._primary_pool(
                pools, info.get("primaryPoolIndex", 0)
            )
            updated_pool = dict(primary_pool)
            updated_pool.update(changed)
            pools[primary_index] = updated_pool
            await self.api.patch_json("/api/system", {"pools": [updated_pool]})
        else:
            await self.api.patch_json("/api/system", changed)
        self._mutated_settings.update(changed)
        expected = {key: value for key, value in desired.items() if key != "stratumPassword"}
        await self._restart_and_wait(expected=expected)

    async def _restart_and_wait(self, *, expected: Mapping[str, Any]) -> Mapping[str, Any]:
        old_uptime = self.state.latest.uptime_seconds
        try:
            await self.api.post_json("/api/system/restart")
        except InterfaceError as exc:
            # Some firmware closes the socket while honoring the restart.
            self.logger.warning("restart response was interrupted: %s", exc)

        deadline = asyncio.get_running_loop().time() + self.online_timeout
        saw_offline = False
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            try:
                info = await self.current_info()
            except Exception as exc:
                saw_offline = True
                last_error = exc
                continue
            uptime = int(info.get("uptimeSeconds") or 0)
            restarted = saw_offline or old_uptime is None or uptime < old_uptime
            matches = all(info.get(key) == value for key, value in expected.items())
            if restarted and matches:
                return info
        raise DeviceError(
            f"{self.name} did not return with expected settings after restart; "
            f"last_error={last_error}, latest={self.state.latest}"
        )

    @staticmethod
    def _version_matches(current: Any, expected: str) -> bool:
        value = str(current or "")
        return value == expected or value.startswith(expected) or expected.startswith(value)

    async def ensure_target_firmware(self) -> None:
        upgrade = self.config.interface("upgrade")
        if not upgrade or not bool(upgrade.get("enabled", False)):
            return
        if self.read_only:
            raise UpgradeError(f"firmware upgrade is blocked for read-only device {self.name}")
        method = str(upgrade.get("method", "ota"))
        expected = upgrade.get("expected_version")
        if expected is not None and not isinstance(expected, str):
            raise ConfigError("upgrade.expected_version must be a string")
        info = await self.current_info()
        if expected and self._version_matches(info.get("version"), expected):
            self.logger.info("target firmware %s is already running", expected)
            return

        artifacts: dict[str, Path] = {}
        for key in ("application", "web", "factory"):
            value = upgrade.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ConfigError(f"upgrade.{key} must be a path string")
                path = self.resolve_project_path(value)
                if not path.is_file():
                    raise UpgradeError(f"upgrade artifact does not exist: {path}")
                artifacts[key] = path
        append_jsonl(
            self.artifacts.events_path,
            {
                "at": time.time(),
                "event": "upgrade_started",
                "method": method,
                "expected_version": expected,
                "artifacts": {key: value.name for key, value in artifacts.items()},
            },
        )

        if method == "ota":
            application = artifacts.get("application")
            if application is None:
                raise UpgradeError("OTA upgrade requires upgrade.application")
            chunk_size = int(upgrade.get("chunk_size", 4096))
            pace_seconds = float(upgrade.get("pace_seconds", 0.0))
            timeout = float(upgrade.get("timeout", 240.0))
            if web := artifacts.get("web"):
                await self.api.upload_file(
                    "/api/system/OTAWWW",
                    web,
                    chunk_size=chunk_size,
                    pace_seconds=pace_seconds,
                    timeout=timeout,
                )
            old_uptime = self.state.latest.uptime_seconds
            await self.api.upload_file(
                "/api/system/OTA",
                application,
                chunk_size=chunk_size,
                pace_seconds=pace_seconds,
                timeout=timeout,
            )
            await self._wait_after_upgrade(old_uptime)
        elif method == "usb":
            if self.serial is None:
                raise UpgradeError("USB upgrade requires an interfaces.serial table")
            await self.serial.flash(artifacts, output_path=self.artifacts.path / "flash.log")
            await self._wait_after_upgrade(self.state.latest.uptime_seconds)
        else:
            raise ConfigError(f"unsupported upgrade method: {method!r}")

        final = await self.current_info()
        if expected and not self._version_matches(final.get("version"), expected):
            raise UpgradeError(
                f"firmware verification failed: expected {expected!r}, "
                f"device reports {final.get('version')!r}"
            )
        append_jsonl(
            self.artifacts.events_path,
            {
                "at": time.time(),
                "event": "upgrade_verified",
                "version": final.get("version"),
            },
        )

    async def _wait_after_upgrade(self, old_uptime: int | None) -> Mapping[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.online_timeout
        saw_offline = False
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1.0)
            try:
                info = await self.current_info()
            except Exception:
                saw_offline = True
                continue
            uptime = int(info.get("uptimeSeconds") or 0)
            if saw_offline or old_uptime is None or uptime < old_uptime:
                return info
        raise UpgradeError(f"{self.name} did not return after firmware upgrade")

    async def wait_for_stable_state(
        self,
        predicate: Callable[[DeviceState], bool],
        *,
        samples: int,
        timeout: float,
        description: str,
    ) -> list[DeviceState]:
        if samples < 1:
            raise ValueError("samples must be at least one")
        deadline = asyncio.get_running_loop().time() + timeout
        consecutive: list[DeviceState] = []
        generation = self.state.generation
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                observed = await self.state.wait_for(
                    lambda state: True,
                    timeout=remaining,
                    description=description,
                    after_generation=generation,
                )
            except TimeoutError:
                break
            generation = self.state.generation
            if predicate(observed):
                consecutive.append(observed)
                if len(consecutive) >= samples:
                    return consecutive
            else:
                consecutive.clear()
        raise TimeoutError(
            f"did not observe {samples} consecutive samples for {description} "
            f"within {timeout:.1f}s; latest state={self.state.latest}"
        )

    async def save_device_logs(self) -> None:
        destination = self.artifacts.path / "device-api.log"
        try:
            truncated = await self.api.download_to(
                "/api/system/logs", destination, max_bytes=self.log_max_bytes
            )
            redact_file(destination)
            if truncated:
                (self.artifacts.path / "device-api.log.TRUNCATED").write_text(
                    f"Log was capped at {self.log_max_bytes} bytes.\n", encoding="utf-8"
                )
                self.logger.warning("device API log was truncated at %d bytes", self.log_max_bytes)
        except Exception as exc:
            (self.artifacts.path / "device-api-log-error.txt").write_text(
                f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
            )
            self.logger.warning("could not save device API log: %s", exc)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [
            task
            for task in (self._monitor_task, self._telemetry_task)
            if task is not None
        ]
        self._monitor_task = None
        self._telemetry_task = None
        for task in tasks:
            task.cancel()
        errors: list[BaseException] = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors.extend(
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            )
        if self.serial is not None:
            try:
                await self.serial.stop_capture()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("device interface shutdown failed", errors)


class BitaxeGammaDevice(BitaxeDevice):
    """ESP-Miner/AxeOS profile for board 602 Bitaxe Gamma devices."""

    device_label = "Bitaxe Gamma 602"
    board_prefix = "602"
    asic_model = "BM1370"


# Compatibility for existing callers; Gamma is the canonical product name.
Bitaxe602Device = BitaxeGammaDevice


__all__ = ["BitaxeDevice", "BitaxeGammaDevice", "Bitaxe602Device"]
