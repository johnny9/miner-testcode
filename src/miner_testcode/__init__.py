"""Reliable, hardware-facing tests for Bitcoin mining devices."""

from .config import ProjectConfig, load_config
from .telemetry import CHART_LEVEL, CHART_LEVEL_NAME, log_chart

__all__ = [
    "CHART_LEVEL",
    "CHART_LEVEL_NAME",
    "ProjectConfig",
    "load_config",
    "log_chart",
]
__version__ = "0.1.0"
