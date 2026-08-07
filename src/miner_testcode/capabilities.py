from __future__ import annotations

from collections.abc import Iterable

API = "api"
DEVICE_LOGS = "device_logs"
MINING_STATE = "mining_state"
OTA_UPGRADE = "ota_upgrade"
POOL_CONFIG = "pool_config"
SERIAL_LOG = "serial_log"
STRATUM_V1 = "stratum_v1"
USB_FLASH = "usb_flash"


def missing(required: Iterable[str], available: Iterable[str]) -> frozenset[str]:
    return frozenset(required).difference(available)
