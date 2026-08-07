class MinerTestError(Exception):
    """Base error for runner, interface, and device failures."""


class ConfigError(MinerTestError):
    """The test configuration is invalid."""


class InterfaceError(MinerTestError):
    """A configured device interface failed."""


class DeviceError(MinerTestError):
    """A device failed its lifecycle or identity contract."""


class UpgradeError(DeviceError):
    """A target firmware upgrade or its verification failed."""
