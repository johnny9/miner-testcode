from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .errors import ConfigError

_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _frozen_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _required_string(table: Mapping[str, Any], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value


def resolve_value(value: Any) -> Any:
    """Resolve an exact ``${NAME}`` value without interpolating secrets into logs."""
    if isinstance(value, str):
        match = _ENV_PATTERN.fullmatch(value)
        if match:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"required environment variable {name} is not set")
            return os.environ[name]
        return value
    if isinstance(value, list):
        return [resolve_value(item) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    artifacts_dir: Path = Path("artifacts")
    tests_dir: Path = Path("tests/e2e")
    pattern: str = "test*.py"
    verbosity: int = 2
    log_level: str = "INFO"
    cleanup_timeout: float = 120.0
    validation_prs: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    name: str
    type: str
    enabled: bool = True
    interfaces: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    def interface(self, name: str, *, required: bool = False) -> Mapping[str, Any]:
        value = self.interfaces.get(name)
        if value is None:
            if required:
                raise ConfigError(
                    f"device {self.name!r} requires an interfaces.{name} table"
                )
            return MappingProxyType({})
        return value

    @property
    def publication_name(self) -> str:
        value = self.options.get("publication_name", self.type)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"device {self.name!r} options.publication_name must be a non-empty string"
            )
        return value.strip()


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    source: Path
    runner: RunnerConfig
    devices: tuple[DeviceConfig, ...]
    tests: Mapping[str, Mapping[str, Any]]
    publishers: Mapping[str, Mapping[str, Any]]

    def selected_devices(self, names: set[str] | None = None) -> tuple[DeviceConfig, ...]:
        enabled = tuple(device for device in self.devices if device.enabled)
        if names is None:
            return enabled
        selected = tuple(device for device in enabled if device.name in names)
        missing = names.difference(device.name for device in selected)
        if missing:
            raise ConfigError(f"unknown or disabled device(s): {', '.join(sorted(missing))}")
        return selected

    def test_settings(self, name: str) -> Mapping[str, Any]:
        return self.tests.get(name, MappingProxyType({}))

    def publisher_settings(self, name: str) -> Mapping[str, Any]:
        return self.publishers.get(name, MappingProxyType({}))


def _path_from(base: Path, value: Any, default: str, context: str) -> Path:
    raw = default if value is None else value
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{context} must be a non-empty path string")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _validation_prs(value: Any) -> frozenset[int]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ConfigError("runner.validation_prs must be an array of PR numbers")
    prs: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ConfigError(
                "runner.validation_prs must contain positive integer PR numbers"
            )
        prs.add(item)
    return frozenset(prs)


def load_config(path: str | os.PathLike[str]) -> ProjectConfig:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {source}: {exc}") from exc

    raw = resolve_value(raw)
    base = source.parent

    runner_raw = raw.get("runner", {})
    if not isinstance(runner_raw, dict):
        raise ConfigError("runner must be a table")
    runner = RunnerConfig(
        artifacts_dir=_path_from(
            base, runner_raw.get("artifacts_dir"), "artifacts", "runner.artifacts_dir"
        ),
        tests_dir=_path_from(
            base, runner_raw.get("tests_dir"), "tests/e2e", "runner.tests_dir"
        ),
        pattern=str(runner_raw.get("pattern", "test*.py")),
        verbosity=int(runner_raw.get("verbosity", 2)),
        log_level=str(runner_raw.get("log_level", "INFO")).upper(),
        cleanup_timeout=float(runner_raw.get("cleanup_timeout", 120.0)),
        validation_prs=_validation_prs(runner_raw.get("validation_prs")),
    )
    if runner.verbosity < 0:
        raise ConfigError("runner.verbosity must be non-negative")
    if runner.cleanup_timeout <= 0:
        raise ConfigError("runner.cleanup_timeout must be positive")

    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, list) or not devices_raw:
        raise ConfigError("at least one [[devices]] table is required")

    devices: list[DeviceConfig] = []
    names: set[str] = set()
    for index, item in enumerate(devices_raw):
        context = f"devices[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{context} must be a table")
        name = _required_string(item, "name", context)
        device_type = _required_string(item, "type", context)
        if name in names:
            raise ConfigError(f"duplicate device name: {name}")
        names.add(name)

        interfaces_raw = item.get("interfaces", {})
        if not isinstance(interfaces_raw, dict):
            raise ConfigError(f"{context}.interfaces must be a table")
        interfaces: dict[str, Mapping[str, Any]] = {}
        for interface_name, interface_config in interfaces_raw.items():
            if not isinstance(interface_config, dict):
                raise ConfigError(
                    f"{context}.interfaces.{interface_name} must be a table"
                )
            interfaces[interface_name] = _frozen_mapping(interface_config)

        options_raw = item.get("options", {})
        if not isinstance(options_raw, dict):
            raise ConfigError(f"{context}.options must be a table")
        devices.append(
            DeviceConfig(
                name=name,
                type=device_type,
                enabled=bool(item.get("enabled", True)),
                interfaces=MappingProxyType(interfaces),
                options=_frozen_mapping(options_raw),
            )
        )

    tests_raw = raw.get("tests", {})
    if not isinstance(tests_raw, dict):
        raise ConfigError("tests must be a table")
    tests: dict[str, Mapping[str, Any]] = {}
    for name, settings in tests_raw.items():
        if not isinstance(settings, dict):
            raise ConfigError(f"tests.{name} must be a table")
        tests[name] = _frozen_mapping(settings)

    publishers_raw = raw.get("publishers", {})
    if not isinstance(publishers_raw, dict):
        raise ConfigError("publishers must be a table")
    publishers: dict[str, Mapping[str, Any]] = {}
    for name, settings in publishers_raw.items():
        if not isinstance(settings, dict):
            raise ConfigError(f"publishers.{name} must be a table")
        publishers[name] = _frozen_mapping(settings)

    return ProjectConfig(
        source=source,
        runner=runner,
        devices=tuple(devices),
        tests=MappingProxyType(tests),
        publishers=MappingProxyType(publishers),
    )
