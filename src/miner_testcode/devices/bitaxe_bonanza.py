from __future__ import annotations

from .bitaxe import BitaxeDevice


class BitaxeBonanzaDevice(BitaxeDevice):
    """ESP-Miner/AxeOS profile for board 1002 Bitaxe Bonanza devices."""

    device_label = "Bitaxe Bonanza"
    board_prefix = "1002"
    asic_model = "BZM"


__all__ = ["BitaxeBonanzaDevice"]
