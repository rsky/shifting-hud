import json
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

MAX_ANT_ID = 0xFFFF  # 65535 (16-bit unsigned integer)


@dataclass(frozen=True)
class BatteryStatusSettings:
    shifters: bool = False
    front_derailleur: bool = True
    rear_derailleur: bool = True
    power_meter: bool = False
    power_meter_id: int = 0


@dataclass(frozen=True)
class TeethSettings:
    front: list[float] | None = None
    rear: list[float] | None = None

    def __post_init__(self) -> None:
        if self.front is not None:
            object.__setattr__(self, "front", sorted(float(x) for x in self.front))
        if self.rear is not None:
            object.__setattr__(self, "rear", sorted((float(x) for x in self.rear), reverse=True))


@dataclass(frozen=True)
class AppSettings:
    ant_id: int = 0
    teeth: TeethSettings | None = None
    battery_status: BatteryStatusSettings = field(default_factory=BatteryStatusSettings)

    def calculate_gear_ratio(self, gear_front: int | None, gear_rear: int | None) -> float | None:
        """
        Calculate gear ratio: teeth.front[current_front] / teeth.rear[current_rear].

        In the ANT+ Shifting profile, gear numbers are 1-based where 1 is the
        innermost (lightest) gear:
        - teeth.front is sorted ascending (inner/small -> outer/large)
        - teeth.rear is sorted descending (inner/large -> outer/small)
        - When gear_front is None (no front derailleur / 1x setup), teeth.front[0] is used.

        Returns None if teeth are not configured, rear gear is invalid, or IndexError occurs.
        """
        if self.teeth is None or not self.teeth.front or not self.teeth.rear:
            return None
        if gear_rear is None:
            return None

        front_index = 0 if gear_front is None else gear_front - 1
        rear_index = gear_rear - 1

        if not (0 <= front_index < len(self.teeth.front) and 0 <= rear_index < len(self.teeth.rear)):
            return None

        front_teeth = self.teeth.front[front_index]
        rear_teeth = self.teeth.rear[rear_index]
        if rear_teeth <= 0:
            return None
        return float(front_teeth) / float(rear_teeth)


def _is_valid_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _parse_ant_id(value: Any, name: str) -> int:
    if not _is_valid_number(value) or int(value) != value or not (0 <= int(value) <= MAX_ANT_ID):
        raise ValueError(f"'{name}' must be an integer between 0 and {MAX_ANT_ID}")
    return int(value)


def _parse_bool_field(raw_obj: dict[str, Any], key: str, default: bool, prefix: str = "batteryStatus") -> bool:
    if key not in raw_obj:
        return default
    value = raw_obj[key]
    if not isinstance(value, bool):
        raise ValueError(f"'{prefix}.{key}' must be a boolean")
    return value


def _parse_teeth_list(teeth_raw: dict[str, Any], key: str) -> list[float] | None:
    value = teeth_raw.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(_is_valid_number(x) and float(x) > 0 for x in value):
        raise ValueError(f"'teeth.{key}' must be an array of positive numbers")
    return [float(x) for x in value]


def validate_and_parse_settings(raw: Any) -> AppSettings:
    """Validate settings dict against settings.schema.json rules and return AppSettings."""
    if not isinstance(raw, dict):
        raise ValueError("settings.json must be a JSON object")

    ant_id = _parse_ant_id(raw["antID"], "antID") if "antID" in raw else 0

    battery_status = BatteryStatusSettings()
    if "batteryStatus" in raw and raw["batteryStatus"] is not None:
        bs_raw = raw["batteryStatus"]
        if not isinstance(bs_raw, dict):
            raise ValueError("'batteryStatus' must be an object")

        power_meter_id = (
            _parse_ant_id(bs_raw["powerMeterID"], "batteryStatus.powerMeterID") if "powerMeterID" in bs_raw else 0
        )
        battery_status = BatteryStatusSettings(
            shifters=_parse_bool_field(bs_raw, "shifters", False),
            front_derailleur=_parse_bool_field(bs_raw, "frontDerailleur", True),
            rear_derailleur=_parse_bool_field(bs_raw, "rearDerailleur", True),
            power_meter=_parse_bool_field(bs_raw, "powerMeter", False),
            power_meter_id=power_meter_id,
        )

    teeth_settings: TeethSettings | None = None
    if "teeth" in raw and raw["teeth"] is not None:
        teeth_raw = raw["teeth"]
        if not isinstance(teeth_raw, dict):
            raise ValueError("'teeth' must be an object")

        teeth_settings = TeethSettings(
            front=_parse_teeth_list(teeth_raw, "front"),
            rear=_parse_teeth_list(teeth_raw, "rear"),
        )

    return AppSettings(ant_id=ant_id, teeth=teeth_settings, battery_status=battery_status)


def resolve_settings_path(path: Path | None = None) -> Path:
    """
    Resolve path to settings.json, supporting both standard Python execution
    and PyInstaller frozen executables.
    """
    if path is not None:
        return path

    candidates: list[Path] = [Path.cwd() / "settings.json"]

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "settings.json")
        # Support macOS .app bundle (App.app/Contents/MacOS/executable)
        if exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
            candidates.append(exe_dir.parent.parent.parent / "settings.json")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass).resolve() / "settings.json")
    else:
        candidates.append(Path(__file__).resolve().parent / "settings.json")

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[1] if len(candidates) > 1 else candidates[0]


def load_settings(path: Path | None = None) -> AppSettings:
    """
    Load and validate settings.json.
    If path is not explicitly specified and settings.json does not exist,
    returns default AppSettings(ant_id=0, teeth=None).
    """
    resolved_path = resolve_settings_path(path)
    if path is None and not resolved_path.exists():
        _logger.info("settings.json not found; using default settings (antID=0)")
        return AppSettings(ant_id=0, teeth=None)

    with resolved_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return validate_and_parse_settings(data)
