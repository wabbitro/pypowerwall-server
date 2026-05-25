"""Helpers for Tesla-style battery percentage scaling."""

from typing import Optional


TESLA_BATTERY_RESERVE_FLOOR = 5.0
TESLA_BATTERY_USABLE_RANGE = 100.0 - TESLA_BATTERY_RESERVE_FLOOR


def raw_to_tesla_battery_percent(raw_percent: Optional[float]) -> Optional[float]:
    """Convert raw 5-100% SOE to the Tesla app's 0-100% display scale."""
    if raw_percent is None:
        return None
    scaled = (
        (raw_percent - TESLA_BATTERY_RESERVE_FLOOR)
        / TESLA_BATTERY_USABLE_RANGE
        * 100.0
    )
    return max(0.0, min(100.0, scaled))


def tesla_to_raw_battery_percent(display_percent: Optional[float]) -> Optional[float]:
    """Convert a Tesla app 0-100% display value back to raw SOE."""
    if display_percent is None:
        return None
    raw = (
        display_percent / 100.0 * TESLA_BATTERY_USABLE_RANGE
        + TESLA_BATTERY_RESERVE_FLOOR
    )
    return max(TESLA_BATTERY_RESERVE_FLOOR, min(100.0, raw))
