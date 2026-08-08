from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..errors import ConfigError

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEYS = {"token", "password", "private_key", "secret", "api_key"}
_SECTIONS = {"repositories", "test_modules", "gates"}
_LAB_SECTIONS = {"hosts", "devices", "setups"}


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be an object")
    return value


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ConfigError(
            f"{context} must start with a lowercase letter or digit and contain "
            "only lowercase letters, digits, underscores, and hyphens"
        )
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, context: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value):
        raise ConfigError(f"{context} must be a non-empty array of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, f"{context}[{index}]"))
    if len(result) != len(set(result)):
        raise ConfigError(f"{context} must not contain duplicates")
    return result


def _reject_plaintext_secrets(value: Any, context: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SECRET_KEYS and item not in (None, ""):
                raise ConfigError(
                    f"{context}.{key} must use an *_env or *_file reference; "
                    "plaintext secrets are not accepted"
                )
            _reject_plaintext_secrets(item, f"{context}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_plaintext_secrets(item, f"{context}[{index}]")


def _validate_host(host_id: str, raw: Any) -> None:
    host = _mapping(raw, f"lab.hosts.{host_id}")
    transport = host.get("transport", "local")
    if transport not in {"local", "ssh"}:
        raise ConfigError(f"lab.hosts.{host_id}.transport must be local or ssh")
    if transport == "ssh":
        _string(host.get("ssh_target"), f"lab.hosts.{host_id}.ssh_target")
    if "max_parallel" in host and (
        isinstance(host["max_parallel"], bool)
        or not isinstance(host["max_parallel"], int)
        or host["max_parallel"] < 1
    ):
        raise ConfigError(f"lab.hosts.{host_id}.max_parallel must be positive")


def _validate_device(device_id: str, raw: Any, hosts: Mapping[str, Any]) -> None:
    device = _mapping(raw, f"lab.devices.{device_id}")
    _string(device.get("name"), f"lab.devices.{device_id}.name")
    _string(device.get("type"), f"lab.devices.{device_id}.type")
    host_id = _identifier(device.get("host"), f"lab.devices.{device_id}.host")
    if host_id not in hosts:
        raise ConfigError(f"lab.devices.{device_id}.host references unknown host {host_id!r}")
    addresses = _mapping(device.get("addresses", {}), f"lab.devices.{device_id}.addresses")
    if "api" in addresses:
        api = _string(addresses["api"], f"lab.devices.{device_id}.addresses.api")
        if not api.startswith(("http://", "https://")):
            raise ConfigError(f"lab.devices.{device_id}.addresses.api must be HTTP(S)")
    usb = _mapping(device.get("usb", {}), f"lab.devices.{device_id}.usb")
    if usb and "serial_path" not in usb:
        raise ConfigError(f"lab.devices.{device_id}.usb.serial_path is required")
    if "serial_path" in usb:
        _string(usb["serial_path"], f"lab.devices.{device_id}.usb.serial_path")
    _string_list(device.get("tags", []), f"lab.devices.{device_id}.tags")


def validate_config(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized, deep-copied configuration or raise ConfigError."""

    raw = copy.deepcopy(dict(document))
    if raw.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    _reject_plaintext_secrets(raw)

    controller = _mapping(raw.setdefault("controller", {}), "controller")
    controller.setdefault("bind", "127.0.0.1")
    controller.setdefault("port", 8765)
    controller.setdefault("state_dir", ".miner-orchestrator")
    controller.setdefault("poll_seconds", 30)
    if not isinstance(controller["port"], int) or not 1 <= controller["port"] <= 65535:
        raise ConfigError("controller.port must be between 1 and 65535")
    if not isinstance(controller["poll_seconds"], (int, float)) or controller["poll_seconds"] <= 0:
        raise ConfigError("controller.poll_seconds must be positive")

    qa = _mapping(raw.setdefault("qa_status", {}), "qa_status")
    qa.setdefault("enabled", False)
    if qa["enabled"]:
        base_url = _string(qa.get("base_url"), "qa_status.base_url")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError("qa_status.base_url must be HTTP(S)")
        _string(qa.get("token_env", "MINING_QA_TOKEN"), "qa_status.token_env")

    repositories = _mapping(raw.setdefault("repositories", {}), "repositories")
    for repository_id, value in repositories.items():
        _identifier(repository_id, f"repositories.{repository_id}")
        repository = _mapping(value, f"repositories.{repository_id}")
        github = _string(repository.get("repository"), f"repositories.{repository_id}.repository")
        if not _REPOSITORY.fullmatch(github):
            raise ConfigError(f"repositories.{repository_id}.repository must be owner/name")
        pushes = _mapping(repository.setdefault("pushes", {}), f"repositories.{repository_id}.pushes")
        pushes.setdefault("branches", ["main", "master"])
        _string_list(pushes["branches"], f"repositories.{repository_id}.pushes.branches", required=True)
        pulls = _mapping(
            repository.setdefault("pull_requests", {}),
            f"repositories.{repository_id}.pull_requests",
        )
        pulls.setdefault("base_branches", ["main", "master"])
        pulls.setdefault("trusted_contributors", [])
        _string_list(
            pulls["base_branches"],
            f"repositories.{repository_id}.pull_requests.base_branches",
            required=True,
        )
        _string_list(
            pulls["trusted_contributors"],
            f"repositories.{repository_id}.pull_requests.trusted_contributors",
        )

    modules = _mapping(raw.setdefault("test_modules", {}), "test_modules")
    for module_id, value in modules.items():
        _identifier(module_id, f"test_modules.{module_id}")
        module = _mapping(value, f"test_modules.{module_id}")
        _string(module.get("pattern"), f"test_modules.{module_id}.pattern")
        _string_list(module.get("device_types", []), f"test_modules.{module_id}.device_types")
        _string_list(module.get("required_interfaces", []), f"test_modules.{module_id}.required_interfaces")

    lab = _mapping(raw.setdefault("lab", {}), "lab")
    hosts = _mapping(lab.setdefault("hosts", {}), "lab.hosts")
    devices = _mapping(lab.setdefault("devices", {}), "lab.devices")
    setups = _mapping(lab.setdefault("setups", {}), "lab.setups")
    for host_id, value in hosts.items():
        _identifier(host_id, f"lab.hosts.{host_id}")
        _validate_host(host_id, value)
    for device_id, value in devices.items():
        _identifier(device_id, f"lab.devices.{device_id}")
        _validate_device(device_id, value, hosts)
    for setup_id, value in setups.items():
        _identifier(setup_id, f"lab.setups.{setup_id}")
        setup = _mapping(value, f"lab.setups.{setup_id}")
        setup_devices = _mapping(setup.get("devices"), f"lab.setups.{setup_id}.devices")
        if not setup_devices:
            raise ConfigError(f"lab.setups.{setup_id}.devices must not be empty")
        device_hosts: set[str] = set()
        for role, device_id in setup_devices.items():
            _identifier(role, f"lab.setups.{setup_id}.devices role")
            device_id = _identifier(device_id, f"lab.setups.{setup_id}.devices.{role}")
            if device_id not in devices:
                raise ConfigError(f"lab.setups.{setup_id} references unknown device {device_id!r}")
            device_hosts.add(str(devices[device_id]["host"]))
        configured_host = setup.get("host")
        if configured_host is not None and configured_host not in hosts:
            raise ConfigError(f"lab.setups.{setup_id}.host references unknown host")
        if len(device_hosts) != 1:
            raise ConfigError(f"lab.setups.{setup_id} devices must belong to one host")
        if configured_host is not None and configured_host not in device_hosts:
            raise ConfigError(f"lab.setups.{setup_id}.host must own every setup device")
        _string(setup.get("runner_profile"), f"lab.setups.{setup_id}.runner_profile")

    gates = _mapping(raw.setdefault("gates", {}), "gates")
    for gate_id, value in gates.items():
        _identifier(gate_id, f"gates.{gate_id}")
        gate = _mapping(value, f"gates.{gate_id}")
        repository_id = _identifier(gate.get("repository"), f"gates.{gate_id}.repository")
        if repository_id not in repositories:
            raise ConfigError(f"gates.{gate_id} references unknown repository {repository_id!r}")
        gate_modules = _string_list(gate.get("test_modules"), f"gates.{gate_id}.test_modules", required=True)
        for module_id in gate_modules:
            if module_id not in modules:
                raise ConfigError(f"gates.{gate_id} references unknown test module {module_id!r}")
        targets = _mapping(gate.get("targets"), f"gates.{gate_id}.targets")
        target_setups = _string_list(targets.get("setups"), f"gates.{gate_id}.targets.setups", required=True)
        for setup_id in target_setups:
            if setup_id not in setups:
                raise ConfigError(f"gates.{gate_id} references unknown setup {setup_id!r}")
        triggers = _mapping(gate.setdefault("triggers", {}), f"gates.{gate_id}.triggers")
        triggers.setdefault("pushes", True)
        triggers.setdefault("pull_requests", True)
        schedules = triggers.setdefault("schedules", [])
        if not isinstance(schedules, list):
            raise ConfigError(f"gates.{gate_id}.triggers.schedules must be an array")
        for index, schedule in enumerate(schedules):
            schedule = _mapping(schedule, f"gates.{gate_id}.triggers.schedules[{index}]")
            _identifier(schedule.get("id"), f"gates.{gate_id}.triggers.schedules[{index}].id")
            _string(schedule.get("cron"), f"gates.{gate_id}.triggers.schedules[{index}].cron")
        policy = gate.setdefault("required", "all")
        if policy not in {"all", "any"}:
            raise ConfigError(f"gates.{gate_id}.required must be all or any")

        for setup_id in target_setups:
            setup_device_ids = setups[setup_id]["devices"].values()
            setup_types = {devices[item]["type"] for item in setup_device_ids}
            for module_id in gate_modules:
                allowed = set(modules[module_id].get("device_types", []))
                if allowed and setup_types.isdisjoint(allowed):
                    raise ConfigError(
                        f"gates.{gate_id}: setup {setup_id!r} has no device compatible "
                        f"with test module {module_id!r}"
                    )
                required_interfaces = set(
                    modules[module_id].get("required_interfaces", [])
                )
                available_interfaces: set[str] = set()
                for item in setup_device_ids:
                    device = devices[item]
                    if device.get("addresses", {}).get("api"):
                        available_interfaces.add("api")
                    if device.get("addresses", {}).get("websocket"):
                        available_interfaces.add("websocket")
                    if device.get("usb", {}).get("serial_path"):
                        available_interfaces.update({"serial", "usb"})
                missing = required_interfaces - available_interfaces
                if missing:
                    raise ConfigError(
                        f"gates.{gate_id}: setup {setup_id!r} lacks interfaces "
                        f"required by {module_id!r}: {', '.join(sorted(missing))}"
                    )

    return raw


def config_digest(document: Mapping[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    document: dict[str, Any]
    revision: str
    loaded_at: str
    source: Path

    @property
    def etag(self) -> str:
        return f'"config-{self.revision}"'


class ConfigStore:
    """Atomic, revisioned YAML configuration shared by the API and scheduler."""

    def __init__(self, source: str | os.PathLike[str]) -> None:
        self.source = Path(source).expanduser().resolve()
        self._lock = threading.RLock()
        self._snapshot = self._read()

    def _read(self) -> ConfigSnapshot:
        try:
            parsed = yaml.safe_load(self.source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"orchestrator configuration not found: {self.source}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {self.source}: {exc}") from exc
        normalized = validate_config(_mapping(parsed, "config"))
        return ConfigSnapshot(
            document=normalized,
            revision=config_digest(normalized),
            loaded_at=datetime.now(UTC).isoformat(),
            source=self.source,
        )

    @property
    def snapshot(self) -> ConfigSnapshot:
        with self._lock:
            return self._snapshot

    def reload(self) -> ConfigSnapshot:
        with self._lock:
            candidate = self._read()
            self._snapshot = candidate
            return candidate

    def validate(self, document: Mapping[str, Any]) -> ConfigSnapshot:
        normalized = validate_config(document)
        return ConfigSnapshot(
            document=normalized,
            revision=config_digest(normalized),
            loaded_at=datetime.now(UTC).isoformat(),
            source=self.source,
        )

    def replace(
        self,
        document: Mapping[str, Any],
        *,
        expected_revision: str | None,
    ) -> ConfigSnapshot:
        with self._lock:
            if expected_revision and expected_revision.removeprefix("config-") != self._snapshot.revision:
                raise ConfigError("configuration revision does not match the active revision")
            candidate = self.validate(document)
            self.source.parent.mkdir(parents=True, exist_ok=True)
            backup_dir = self.source.parent / ".orchestrator-backups"
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            if self.source.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                shutil.copy2(self.source, backup_dir / f"{self.source.name}.{stamp}.bak")
            rendered = yaml.safe_dump(candidate.document, sort_keys=False, allow_unicode=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.source.parent,
                prefix=f".{self.source.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.source)
                directory_fd = os.open(self.source.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
            self._snapshot = candidate
            return candidate

    def mutate_resource(
        self,
        section: str,
        resource_id: str,
        value: Mapping[str, Any] | None,
        *,
        expected_revision: str | None,
        create_only: bool = False,
    ) -> ConfigSnapshot:
        _identifier(resource_id, "resource id")
        document = copy.deepcopy(self.snapshot.document)
        if section in _SECTIONS:
            resources = document[section]
        elif section in _LAB_SECTIONS:
            resources = document["lab"][section]
        else:
            raise ConfigError(f"unsupported configuration section: {section}")
        if create_only and resource_id in resources:
            raise ConfigError(f"{section}.{resource_id} already exists")
        if value is None:
            if resource_id not in resources:
                raise ConfigError(f"{section}.{resource_id} does not exist")
            del resources[resource_id]
        else:
            resources[resource_id] = copy.deepcopy(dict(value))
        return self.replace(document, expected_revision=expected_revision)


def write_example(path: str | os.PathLike[str]) -> Path:
    destination = Path(path).expanduser().resolve()
    if destination.exists():
        raise ConfigError(f"refusing to overwrite existing file: {destination}")
    example = Path(__file__).resolve().parents[3] / "configs" / "orchestrator.example.yaml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, destination)
    os.chmod(destination, 0o600)
    return destination
