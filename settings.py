from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)

MAX_ANT_ID = 0xFFFF  # 65535 (16-bit unsigned integer)


@dataclass(frozen=True)
class TeethSettings:
    front: Optional[list[float]] = None
    rear: Optional[list[float]] = None

    def __post_init__(self) -> None:
        if self.front is not None:
            object.__setattr__(self, "front", sorted(float(x) for x in self.front))
        if self.rear is not None:
            object.__setattr__(self, "rear", sorted((float(x) for x in self.rear), reverse=True))


@dataclass(frozen=True)
class AppSettings:
    ant_id: int
    teeth: Optional[TeethSettings] = None

    def calculate_gear_ratio(self, gear_front: Optional[int], gear_rear: Optional[int]) -> Optional[float]:
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

        try:
            if front_index < 0 or rear_index < 0:
                raise IndexError("Gear number must be >= 1")
            front_teeth = self.teeth.front[front_index]
            rear_teeth = self.teeth.rear[rear_index]
            if rear_teeth <= 0:
                return None
            return float(front_teeth) / float(rear_teeth)
        except IndexError:
            return None


def _is_valid_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_and_parse_settings(raw: Any) -> AppSettings:
    """Validate settings dict against settings.schema.json rules and return AppSettings."""
    if not isinstance(raw, dict):
        raise ValueError("settings.json must be a JSON object")

    if "antID" not in raw:
        raise ValueError("Missing required property 'antID' in settings.json")

    ant_id_raw = raw["antID"]
    if not _is_valid_number(ant_id_raw) or int(ant_id_raw) != ant_id_raw or not (0 <= int(ant_id_raw) <= MAX_ANT_ID):
        raise ValueError(f"'antID' must be an integer between 0 and {MAX_ANT_ID}")

    teeth_settings: Optional[TeethSettings] = None
    if "teeth" in raw and raw["teeth"] is not None:
        teeth_raw = raw["teeth"]
        if not isinstance(teeth_raw, dict):
            raise ValueError("'teeth' must be an object")

        front_list: Optional[list[float]] = None
        rear_list: Optional[list[float]] = None

        if "front" in teeth_raw and teeth_raw["front"] is not None:
            if not isinstance(teeth_raw["front"], list) or not all(
                _is_valid_number(x) and float(x) > 0 for x in teeth_raw["front"]
            ):
                raise ValueError("'teeth.front' must be an array of positive numbers")
            front_list = [float(x) for x in teeth_raw["front"]]

        if "rear" in teeth_raw and teeth_raw["rear"] is not None:
            if not isinstance(teeth_raw["rear"], list) or not all(
                _is_valid_number(x) and float(x) > 0 for x in teeth_raw["rear"]
            ):
                raise ValueError("'teeth.rear' must be an array of positive numbers")
            rear_list = [float(x) for x in teeth_raw["rear"]]

        teeth_settings = TeethSettings(front=front_list, rear=rear_list)

    return AppSettings(ant_id=int(ant_id_raw), teeth=teeth_settings)


def resolve_settings_path(path: Optional[Path] = None) -> Path:
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


def load_settings(path: Optional[Path] = None) -> AppSettings:
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
