from __future__ import annotations

import logging
from pathlib import Path

from ..artifacts import TestArtifacts
from ..config import DeviceConfig
from ..errors import ConfigError
from .base import MiningDevice
from .bitaxe_bonanza import BitaxeBonanzaDevice

_DEVICE_TYPES: dict[str, type[MiningDevice]] = {
    "bitaxe_bonanza": BitaxeBonanzaDevice,
}


def create_device(
    config: DeviceConfig,
    *,
    project_dir: Path,
    artifacts: TestArtifacts,
    logger: logging.Logger,
) -> MiningDevice:
    device_class = _DEVICE_TYPES.get(config.type)
    if device_class is None:
        supported = ", ".join(sorted(_DEVICE_TYPES))
        raise ConfigError(
            f"unsupported device type {config.type!r} for {config.name!r}; "
            f"supported types: {supported}"
        )
    return device_class(
        config,
        project_dir=project_dir,
        artifacts=artifacts,
        logger=logger,
    )


__all__ = ["BitaxeBonanzaDevice", "MiningDevice", "create_device"]
