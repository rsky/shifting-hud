from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from openant.devices.common import BatteryData, BatteryStatus
from openant.devices.shift import ShiftData, ShiftingSystemID
from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

import ant_worker
import hud_window
import settings as settings_mod


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app
    return QApplication([])


def test_settings_sorting_and_ratio() -> None:
    cfg = settings_mod.validate_and_parse_settings(
        {
            "antID": 0,
            "teeth": {
                "front": [50, 34],
                "rear": [11, 12, 13, 14, 15, 17, 19, 21, 24, 27, 30, 34],
            },
        }
    )
    assert cfg.ant_id == 0
    assert cfg.teeth is not None
    assert cfg.teeth.front == [34.0, 50.0]
    assert cfg.teeth.rear == [34.0, 30.0, 27.0, 24.0, 21.0, 19.0, 17.0, 15.0, 14.0, 13.0, 12.0, 11.0]

    # Inner front (1 -> 34T), inner rear (1 -> 34T) => 1.00
    assert cfg.calculate_gear_ratio(1, 1) == pytest.approx(1.0)
    # Outer front (2 -> 50T), outer rear (12 -> 11T) => 50 / 11
    assert cfg.calculate_gear_ratio(2, 12) == pytest.approx(50.0 / 11.0)

    # Out of range / invalid gear numbers => None
    assert cfg.calculate_gear_ratio(3, 1) is None
    assert cfg.calculate_gear_ratio(1, 13) is None
    assert cfg.calculate_gear_ratio(0, 1) is None
    assert cfg.calculate_gear_ratio(1, 0) is None
    assert cfg.calculate_gear_ratio(None, 1) is None
    assert cfg.calculate_gear_ratio(1, None) is None


def test_settings_front_single_and_no_teeth() -> None:
    single_cfg = settings_mod.validate_and_parse_settings({"antID": 12345, "teeth": {"front": [46], "rear": [11, 34]}})
    assert single_cfg.calculate_gear_ratio(1, 1) == pytest.approx(46.0 / 34.0)
    assert single_cfg.calculate_gear_ratio(1, 2) == pytest.approx(46.0 / 11.0)
    assert single_cfg.calculate_gear_ratio(2, 1) is None

    no_teeth_cfg = settings_mod.validate_and_parse_settings({"antID": 999})
    assert no_teeth_cfg.calculate_gear_ratio(1, 1) is None


@pytest.mark.parametrize(
    "invalid_payload",
    [
        "not_a_dict",
        {},
        {"antID": -1},
        {"antID": 70000},
        {"antID": True},
        {"antID": 1.5},
        {"antID": 0, "teeth": "invalid"},
        {"antID": 0, "teeth": {"front": [0, 50]}},
        {"antID": 0, "teeth": {"rear": [-11, 34]}},
        {"antID": 0, "teeth": {"front": [True]}},
    ],
)
def test_settings_validation_errors(invalid_payload: Any) -> None:
    with pytest.raises(ValueError):
        settings_mod.validate_and_parse_settings(invalid_payload)


def test_resolve_settings_path_pyinstaller() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir).resolve()
        exe_dir = tmp_path / "dist"
        exe_dir.mkdir()
        fake_exe = exe_dir / "shifting-hud.exe"
        fake_exe.touch()
        fake_settings = exe_dir / "settings.json"
        fake_settings.write_text('{"antID": 42}', encoding="utf-8")

        other_cwd = tmp_path / "other_cwd"
        other_cwd.mkdir()
        old_cwd = Path.cwd()
        try:
            os.chdir(other_cwd)
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(fake_exe)):
                resolved = settings_mod.resolve_settings_path()
                assert resolved == fake_settings
                loaded = settings_mod.load_settings()
                assert loaded.ant_id == 42
        finally:
            os.chdir(old_cwd)


def test_hud_shifting_device_packets() -> None:
    mock_node = MagicMock()
    mock_channel = MagicMock()
    mock_node.new_channel.return_value = mock_channel

    received_shifts: list[ShiftData] = []
    received_batteries: list[BatteryData] = []

    dev = ant_worker.HudShiftingDevice(
        mock_node,
        device_id=1234,
        on_shift_callback=received_shifts.append,
        on_battery_callback=received_batteries.append,
    )

    # Page 1 (0x01) with event_count = 0: gear_front=2, gear_rear=5, total_front=2, total_rear=12
    dev._on_data([0x01, 0x00, 0xFF, 0x45, 0x4C, 0x00, 0x00, 0x00])
    assert len(received_shifts) == 1
    shift = received_shifts[-1]
    assert shift.gear_front == 2
    assert shift.gear_rear == 5
    assert shift.total_front == 2
    assert shift.total_rear == 12
    assert shift is not dev.data["shift"]

    # Page 4 (0x04) must preserve gear_front / gear_rear
    dev._on_data([0x04, 0x01, 10, 10, 5, 5, 0x00, 0x00])
    assert dev.data["shift"].gear_front == 2
    assert dev.data["shift"].gear_rear == 5

    # Common Page 82 (0x52) for LeftShifter (id=3), status=Good (2)
    dev._on_data([82, 0xFF, 0x34, 0x00, 0x00, 0x00, 0x00, 0x23])
    assert len(received_batteries) == 1
    assert received_batteries[-1].battery_id == ShiftingSystemID.LeftShifter.value
    assert received_batteries[-1].status == BatteryStatus.Good
    assert received_batteries[-1] is not dev.data["common"].last_battery_data

    # Common Page 82 with battery_id = 15 (0xF1) must not raise IndexError
    dev._on_data([82, 0xFF, 0xF1, 0x00, 0x00, 0x00, 0x00, 0x13])
    assert len(received_batteries) == 2


def test_hud_window_ui_and_rendering(qapp: QApplication) -> None:
    cfg = settings_mod.validate_and_parse_settings(
        {
            "antID": 0,
            "teeth": {
                "front": [50, 34],
                "rear": [11, 12, 13, 14, 15, 17, 19, 21, 24, 27, 30, 34],
            },
        }
    )
    win = hud_window.ShiftingHudWindow(cfg)
    win.show()
    qapp.processEvents()

    assert win.front_value_label.text() == "-"
    assert win.rear_value_label.text() == "-"
    assert not win.ratio_block.isVisible()
    for key in ("L", "R", "FD", "RD"):
        assert win.battery_value_labels[key].text() == "-"

    win.update_shift_data(ShiftData(gear_front=2, total_front=2, gear_rear=12, total_rear=12))
    qapp.processEvents()
    assert win.front_value_label.text() == "2/2"
    assert win.rear_value_label.text() == "12/12"
    assert win.ratio_block.isVisible()
    assert win.ratio_value_label.text() == "4.55"

    win.update_battery_data(BatteryData(battery_id=ShiftingSystemID.LeftShifter.value, status=BatteryStatus.New))
    qapp.processEvents()
    assert win.battery_value_labels["L"].text() == "New"

    win.reset_display()
    qapp.processEvents()
    assert win.front_value_label.text() == "-"
    assert win.rear_value_label.text() == "-"
    assert not win.ratio_block.isVisible()
    assert win.battery_value_labels["L"].text() == "-"

    image = QImage(win.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    win.render(image, QPoint(0, 0))
    assert image.pixelColor(0, 0).alpha() == 0
    assert 0 < image.pixelColor(10, 10).alpha() < 255

    win.close()
