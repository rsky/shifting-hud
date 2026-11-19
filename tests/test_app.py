import contextlib
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from openant.devices.common import BatteryData, BatteryStatus
from openant.devices.shift import ShiftData, ShiftingSystemID
from openant.easy.exception import TransferFailedException
from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

import ant_logger
import ant_worker
import capture
import hud_window
import main as main_mod
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
    assert cfg.calculate_gear_ratio(1, None) is None
    assert cfg.calculate_gear_ratio(None, None) is None

    # When gear_front is None (FD absent / 1x), teeth.front[0] (34.0) is used
    assert cfg.calculate_gear_ratio(None, 1) == pytest.approx(1.0)
    assert cfg.calculate_gear_ratio(None, 12) == pytest.approx(34.0 / 11.0)


def test_settings_front_single_and_no_teeth() -> None:
    single_cfg = settings_mod.validate_and_parse_settings({"antID": 12345, "teeth": {"front": [46], "rear": [11, 34]}})
    assert single_cfg.calculate_gear_ratio(1, 1) == pytest.approx(46.0 / 34.0)
    assert single_cfg.calculate_gear_ratio(1, 2) == pytest.approx(46.0 / 11.0)
    assert single_cfg.calculate_gear_ratio(2, 1) is None

    no_teeth_cfg = settings_mod.validate_and_parse_settings({"antID": 999})
    assert no_teeth_cfg.calculate_gear_ratio(1, 1) is None

    empty_cfg = settings_mod.validate_and_parse_settings({})
    assert empty_cfg.ant_id == 0
    assert empty_cfg.teeth is None
    assert empty_cfg.battery_status == settings_mod.BatteryStatusSettings(
        shifters=False,
        front_derailleur=True,
        rear_derailleur=True,
        power_meter=False,
        power_meter_id=0,
    )

    custom_bs_cfg = settings_mod.validate_and_parse_settings(
        {
            "batteryStatus": {
                "shifters": True,
                "frontDerailleur": False,
                "rearDerailleur": True,
                "powerMeter": True,
                "powerMeterID": 54321,
            }
        }
    )
    assert custom_bs_cfg.ant_id == 0
    assert custom_bs_cfg.battery_status.shifters is True
    assert custom_bs_cfg.battery_status.front_derailleur is False
    assert custom_bs_cfg.battery_status.rear_derailleur is True
    assert custom_bs_cfg.battery_status.power_meter is True
    assert custom_bs_cfg.battery_status.power_meter_id == 54321


@pytest.mark.parametrize(
    "invalid_payload",
    [
        "not_a_dict",
        {"antID": None},
        {"antID": -1},
        {"antID": 70000},
        {"antID": True},
        {"antID": 1.5},
        {"antID": 0, "teeth": "invalid"},
        {"antID": 0, "teeth": {"front": [0, 50]}},
        {"antID": 0, "teeth": {"rear": [-11, 34]}},
        {"antID": 0, "teeth": {"front": [True]}},
        {"antID": float("inf")},
        {"antID": float("nan")},
        {"antID": 0, "teeth": {"front": [float("inf")]}},
        {"antID": 0, "teeth": {"rear": [float("nan")]}},
        {"batteryStatus": "invalid"},
        {"batteryStatus": {"shifters": 1}},
        {"batteryStatus": {"frontDerailleur": "true"}},
        {"batteryStatus": {"rearDerailleur": None}},
        {"batteryStatus": {"powerMeter": 0}},
        {"batteryStatus": {"powerMeterID": -1}},
        {"batteryStatus": {"powerMeterID": 70000}},
        {"batteryStatus": {"powerMeterID": True}},
        {"batteryStatus": {"powerMeterID": 1.5}},
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
        with (
            contextlib.chdir(other_cwd),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(fake_exe)),
        ):
            resolved = settings_mod.resolve_settings_path()
            assert resolved == fake_settings
            loaded = settings_mod.load_settings()
            assert loaded.ant_id == 42


def test_load_settings_default_when_missing() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir).resolve()
        missing_file = tmp_path / "settings.json"

        with patch.object(settings_mod, "resolve_settings_path", return_value=missing_file):
            default_cfg = settings_mod.load_settings()
            assert default_cfg.ant_id == 0
            assert default_cfg.teeth is None

        # Explicit path (-s) that does not exist should still raise FileNotFoundError
        with pytest.raises(FileNotFoundError):
            settings_mod.load_settings(missing_file)


def test_resolve_icons_dir_pyinstaller() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir).resolve()
        meipass_dir = tmp_path / "_MEI12345"
        meipass_icons = meipass_dir / "icons"
        meipass_icons.mkdir(parents=True)

        exe_dir = tmp_path / "dist"
        exe_dir.mkdir()
        fake_exe = exe_dir / "shifting-hud.exe"
        fake_exe.touch()
        exe_icons = exe_dir / "icons"
        exe_icons.mkdir()

        # When both _MEIPASS/icons and exe_dir/icons exist, _MEIPASS/icons takes priority
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(meipass_dir), create=True),
            patch.object(sys, "executable", str(fake_exe)),
        ):
            assert hud_window.resolve_icons_dir() == meipass_icons

        # When _MEIPASS has no icons dir, falls back to exe_dir/icons
        meipass_icons.rmdir()
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(meipass_dir), create=True),
            patch.object(sys, "executable", str(fake_exe)),
        ):
            assert hud_window.resolve_icons_dir() == exe_icons


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

    # Page 1 (0x01) with raw data[3]=0x24 (0-based raw front=1, rear=4, total front=2, rear=12):
    # Converted to 1-based gear_front=2, gear_rear=5
    dev._on_data([0x01, 0x00, 0xFF, 0x24, 0x4C, 0x00, 0x00, 0x00])
    assert len(received_shifts) == 1
    shift = received_shifts[-1]
    assert shift.gear_front == 2
    assert shift.gear_rear == 5
    assert shift.total_front == 2
    assert shift.total_rear == 12
    assert shift is not dev.data["shift"]

    # Inner-lowest gear: raw front=0, raw rear=0 -> 1-based gear_front=1, gear_rear=1
    dev._on_data([0x01, 0x01, 0xFF, 0x00, 0x4C, 0x00, 0x00, 0x00])
    assert len(received_shifts) == 2
    inner_shift = received_shifts[-1]
    assert inner_shift.gear_front == 1
    assert inner_shift.gear_rear == 1

    # 1x setup without FD: raw front=7 (0xE0) preserved as INVALID_GEAR_FRONT (7), raw rear=11 -> 12
    dev._on_data([0x01, 0x02, 0xFF, 0xEB, 0x0C, 0x00, 0x00, 0x00])
    assert len(received_shifts) == 3
    single_shift = received_shifts[-1]
    assert single_shift.gear_front == ant_worker.INVALID_GEAR_FRONT
    assert single_shift.gear_rear == 12

    # Page 4 (0x04) must preserve gear_front / gear_rear
    dev._on_data([0x04, 0x01, 10, 10, 5, 5, 0x00, 0x00])
    assert dev.data["shift"].gear_front == ant_worker.INVALID_GEAR_FRONT
    assert dev.data["shift"].gear_rear == 12

    # Common Page 82 (0x52) for LeftShifter (id=3), status=Good (2)
    dev._on_data([82, 0xFF, 0x34, 0x00, 0x00, 0x00, 0x00, 0x23])
    assert len(received_batteries) == 1
    assert received_batteries[-1].battery_id == ShiftingSystemID.LeftShifter.value
    assert received_batteries[-1].status == BatteryStatus.Good
    assert received_batteries[-1] is not dev.data["common"].last_battery_data

    # Common Page 82 with battery_id = 15 (0xF1) must not raise IndexError
    dev._on_data([82, 0xFF, 0xF1, 0x00, 0x00, 0x00, 0x00, 0x13])
    assert len(received_batteries) == 2
    assert dev.last_rx_time is not None


def test_battery_debug_logging(caplog: pytest.LogCaptureFixture) -> None:
    mock_node = MagicMock()
    mock_channel = MagicMock()
    mock_node.new_channel.return_value = mock_channel

    dev = ant_worker.HudShiftingDevice(mock_node, device_id=1234)

    with caplog.at_level(logging.DEBUG):
        # Send Page 82 for LeftShifter (id=3, status=Good=2, 3.0V)
        dev._on_data([82, 0xFF, 0x34, 0x00, 0x00, 0x00, 0x00, 0x23])

    assert "Raw Page 82 (Battery) packet" in caplog.text
    assert "Battery update: ID=3 (LeftShifter)" in caplog.text
    assert "All active battery states" in caplog.text
    assert "Slot 3 (LeftShifter)" in caplog.text


def test_hud_shifting_device_opens_bidirectional_channel_and_sends_ack() -> None:
    mock_node = MagicMock()
    mock_channel = MagicMock()
    mock_channel.id = 0
    mock_node.new_channel.return_value = mock_channel

    dev = ant_worker.HudShiftingDevice(mock_node, device_id=1234)

    # Channel must be opened with ext_assign=None (not 0x01 Background Scanning)
    assert mock_node.new_channel.call_args.args[2] is None

    # Successful ACK send
    assert dev.request_battery_page(ShiftingSystemID.LeftShifter.value) is True
    expected_payload = [0x46, 0xFF, 0xFF, 0x03, 0xFF, 0x04, 0x52, 0x01]
    mock_channel._ant.send_acknowledged_data.assert_called_once_with(0, expected_payload)
    mock_channel.wait_for_event.assert_called_once()
    mock_channel.send_broadcast_data.assert_not_called()

    # Bounded retry on TransferFailedException then succeed
    mock_channel._ant.send_acknowledged_data.reset_mock()
    mock_channel.wait_for_event.reset_mock()
    mock_channel.wait_for_event.side_effect = [TransferFailedException(), None]
    with patch.object(ant_worker, "BATTERY_REQUEST_RETRY_DELAY", 0.01):
        assert dev.request_battery_page(ShiftingSystemID.RightShifter.value, max_retries=3) is True
    assert mock_channel._ant.send_acknowledged_data.call_count == 2


def test_request_shifter_battery_pages_on_first_rd_battery() -> None:
    mock_node = MagicMock()
    mock_channel = MagicMock()
    mock_node.new_channel.return_value = mock_channel

    dev = ant_worker.HudShiftingDevice(mock_node, device_id=1234)

    with patch.object(dev, "request_battery_page") as mock_req:
        # First RD battery packet (battery_id=2 = RearDerailleur)
        dev.on_battery(BatteryData(battery_id=ShiftingSystemID.RearDerailleur.value, status=BatteryStatus.New))

        # Wait briefly for the worker thread to spawn and execute
        time.sleep(0.4)

        # Both LeftShifter (3) and RightShifter (4) requested
        assert mock_req.call_count == 2
        mock_req.assert_any_call(ShiftingSystemID.LeftShifter.value)
        mock_req.assert_any_call(ShiftingSystemID.RightShifter.value)

        # Second RD battery packet does not re-request
        dev.on_battery(BatteryData(battery_id=ShiftingSystemID.RearDerailleur.value, status=BatteryStatus.New))
        time.sleep(0.1)
        assert mock_req.call_count == 2


def test_no_request_when_shifters_already_received() -> None:
    mock_node = MagicMock()
    mock_channel = MagicMock()
    mock_node.new_channel.return_value = mock_channel

    dev = ant_worker.HudShiftingDevice(mock_node, device_id=1234)

    with patch.object(dev, "request_battery_page") as mock_req:
        # Pre-receive Left and Right shifters
        dev.on_battery(BatteryData(battery_id=ShiftingSystemID.LeftShifter.value, status=BatteryStatus.Good))
        dev.on_battery(BatteryData(battery_id=ShiftingSystemID.RightShifter.value, status=BatteryStatus.Good))

        # Then RD battery arrives
        dev.on_battery(BatteryData(battery_id=ShiftingSystemID.RearDerailleur.value, status=BatteryStatus.New))

        time.sleep(0.1)
        assert mock_req.call_count == 0


def test_hud_shifting_device_wildcard_discovery() -> None:
    mock_node = MagicMock()
    mock_channel = MagicMock()
    mock_node.new_channel.return_value = mock_channel

    received_shifts: list[ShiftData] = []
    received_batteries: list[BatteryData] = []

    dev = ant_worker.HudShiftingDevice(
        mock_node,
        device_id=0,
        on_shift_callback=received_shifts.append,
        on_battery_callback=received_batteries.append,
    )
    assert dev.device_id == 0

    # Non-extended packet while device_id == 0 is ignored until a device ID is discovered
    dev._on_data([0x01, 0x00, 0xFF, 0x24, 0x4C, 0x00, 0x00, 0x00])
    assert dev.device_id == 0
    assert len(received_shifts) == 0

    # Extended packet with device_id = 0x3039 (12345), device_type = 0x22 (34), trans_type = 0x05, raw gear = 0x24
    ext_packet_12345 = [0x01, 0x01, 0xFF, 0x24, 0x4C, 0x00, 0x00, 0x00, 0x80, 0x39, 0x30, 0x22, 0x05]
    dev._on_data(ext_packet_12345)

    # Device ID is discovered and locked without calling channel.close() (avoiding CHANNEL_IN_WRONG_STATE)
    assert dev.device_id == 12345
    assert dev.trans_type == 0x05
    assert dev._attached is True
    assert mock_channel.close.call_count == 0
    assert len(received_shifts) == 1
    assert received_shifts[-1].gear_front == 2
    assert received_shifts[-1].gear_rear == 5

    # Packet from a different device_id (0x270F = 9999) is ignored
    ext_packet_9999 = [0x01, 0x02, 0xFF, 0x21, 0x4C, 0x00, 0x00, 0x00, 0x80, 0x0F, 0x27, 0x22, 0x05]
    dev._on_data(ext_packet_9999)
    assert dev.device_id == 12345
    assert len(received_shifts) == 1


def test_worker_discovery_polling() -> None:
    worker = ant_worker.AntShiftingWorker(
        ant_id=0,
        on_shift=lambda _: None,
        on_battery=lambda _: None,
        discovery_poll_interval=1.0,
    )
    assert worker.active_ant_id == 0
    assert worker.poll_discovery() is None

    mock_device = MagicMock()
    mock_device.device_id = 0
    worker._device = mock_device

    # Still undiscovered (device_id == 0)
    assert worker.poll_discovery() is None
    assert worker.active_ant_id == 0

    # Device discovered by listener (device_id == 12345)
    mock_device.device_id = 12345
    assert worker.poll_discovery() == 12345
    assert worker.active_ant_id == 12345

    # Subsequent polls return the locked active_ant_id
    assert worker.poll_discovery() == 12345


def test_worker_inactivity_watchdog() -> None:
    lost_count = 0

    def on_lost() -> None:
        nonlocal lost_count
        lost_count += 1

    worker = ant_worker.AntShiftingWorker(
        ant_id=0,
        on_shift=lambda _: None,
        on_battery=lambda _: None,
        on_connection_lost=on_lost,
        inactivity_timeout=10.0,
    )
    mock_device = MagicMock()
    mock_device.last_rx_time = 100.0
    mock_node = MagicMock()
    worker._device = mock_device
    worker._node = mock_node

    # Before timeout (elapsed 5s < 10s)
    assert not worker.check_inactivity(now=105.0)
    assert lost_count == 0
    assert mock_device.last_rx_time == 100.0
    assert mock_node.stop.call_count == 0

    # At/after timeout (elapsed 10s >= 10s)
    assert worker.check_inactivity(now=110.0)
    assert lost_count == 1
    assert mock_device.last_rx_time is None
    assert mock_node.stop.call_count == 1

    # Subsequent check without new packet does not re-trigger
    assert not worker.check_inactivity(now=120.0)
    assert lost_count == 1


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

    assert win.front_value_label.text() == "--"
    assert win.rear_value_label.text() == "--"
    assert not win.ratio_block.isVisible()
    for key in ("L", "R", "FD", "RD"):
        assert win.battery_value_labels[key].text() == "--"

    win.update_shift_data(ShiftData(gear_front=2, total_front=2, gear_rear=12, total_rear=12))
    qapp.processEvents()
    assert win.front_block.isVisible()
    assert win.front_value_label.text() == "2/2"
    assert win.rear_value_label.text() == "12/12"
    assert win.ratio_block.isVisible()
    assert win.ratio_value_label.text() == "4.55"

    # Differential update: repeating identical ShiftData should not trigger adjustSize()
    with patch.object(win, "adjustSize") as mock_adjust:
        win.update_shift_data(ShiftData(gear_front=2, total_front=2, gear_rear=12, total_rear=12))
        assert mock_adjust.call_count == 0

    # 1x setup without FD (gear_front=7 invalid, gear_rear=12 valid):
    # FRONT block hidden, RATIO calculated with teeth.front[0] (34 / 11 = 3.09)
    win.update_shift_data(ShiftData(gear_front=7, total_front=0, gear_rear=12, total_rear=12))
    qapp.processEvents()
    assert not win.front_block.isVisible()
    assert win.rear_value_label.text() == "12/12"
    assert win.ratio_block.isVisible()
    assert win.ratio_value_label.text() == "3.09"

    # 1x setup with total_front=1 (gear_front=1, gear_rear=12 valid):
    # FRONT block hidden, RATIO calculated with teeth.front[0] (34 / 11 = 3.09)
    win.update_shift_data(ShiftData(gear_front=1, total_front=1, gear_rear=12, total_rear=12))
    qapp.processEvents()
    assert not win.front_block.isVisible()
    assert win.rear_value_label.text() == "12/12"
    assert win.ratio_block.isVisible()
    assert win.ratio_value_label.text() == "3.09"

    # Both front and rear invalid (e.g. gear_front=7, gear_rear=31):
    # FRONT block visible showing "--", REAR showing "--", RATIO hidden
    win.update_shift_data(ShiftData(gear_front=7, total_front=0, gear_rear=31, total_rear=0))
    qapp.processEvents()
    assert win.front_block.isVisible()
    assert win.front_value_label.text() == "--"
    assert win.rear_value_label.text() == "--"
    assert not win.ratio_block.isVisible()

    win.update_battery_data(BatteryData(battery_id=ShiftingSystemID.LeftShifter.value, status=BatteryStatus.New))
    qapp.processEvents()
    assert win.battery_statuses["L"] == BatteryStatus.New
    assert not win.battery_value_labels["L"].pixmap().isNull()

    win.reset_display()
    qapp.processEvents()
    assert win.front_value_label.text() == "--"
    assert win.rear_value_label.text() == "--"
    assert not win.ratio_block.isVisible()
    assert win.battery_statuses["L"] is None
    assert win.battery_value_labels["L"].text() == "--"

    image = QImage(win.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    win.render(image, QPoint(0, 0))
    assert image.pixelColor(0, 0).alpha() == 0
    assert 0 < image.pixelColor(10, 10).alpha() < 255

    win.close()


def test_demo_mode_and_apply_demo_data(qapp: QApplication) -> None:
    # 2x setup
    cfg_2x = settings_mod.validate_and_parse_settings(
        {"antID": 0, "teeth": {"front": [50, 34], "rear": [11, 12, 13, 14, 15, 17, 19, 21, 24, 27, 30, 34]}}
    )
    win_2x = hud_window.ShiftingHudWindow(cfg_2x)
    main_mod.apply_demo_data(win_2x)
    win_2x.show()
    qapp.processEvents()
    assert win_2x.front_block.isVisible()
    assert win_2x.front_value_label.text() == "2/2"
    assert win_2x.rear_value_label.text() == "5/12"
    assert win_2x.ratio_block.isVisible()
    assert win_2x.battery_statuses["L"] == BatteryStatus.Good
    assert not win_2x.battery_value_labels["L"].pixmap().isNull()
    assert win_2x.battery_statuses["FD"] == BatteryStatus.New
    assert not win_2x.battery_value_labels["FD"].pixmap().isNull()
    assert win_2x.battery_statuses["PM"] == BatteryStatus.Low
    assert not win_2x.battery_value_labels["PM"].pixmap().isNull()
    win_2x.close()

    # 1x setup
    cfg_1x = settings_mod.validate_and_parse_settings({"antID": 0, "teeth": {"front": [46], "rear": [11, 34]}})
    win_1x = hud_window.ShiftingHudWindow(cfg_1x)
    main_mod.apply_demo_data(win_1x)
    win_1x.show()
    qapp.processEvents()
    assert not win_1x.front_block.isVisible()
    assert win_1x.rear_value_label.text() == "2/2"
    assert win_1x.ratio_block.isVisible()
    assert win_1x.battery_statuses["FD"] is None
    assert win_1x.battery_value_labels["FD"].text() == "--"
    win_1x.close()


def test_battery_icon_mapping(qapp: QApplication) -> None:
    cfg = settings_mod.validate_and_parse_settings({"antID": 0})
    win = hud_window.ShiftingHudWindow(cfg)
    win.show()
    qapp.processEvents()

    # Initial state
    assert win.battery_statuses["RD"] is None
    assert win.battery_value_labels["RD"].text() == "--"

    # New and Good share the same cached QPixmap instance; the 5 icon files produce distinct pixmaps
    assert win._battery_icons[BatteryStatus.New].cacheKey() == win._battery_icons[BatteryStatus.Good].cacheKey()
    distinct_statuses = (
        BatteryStatus.New,
        BatteryStatus.Ok,
        BatteryStatus.Low,
        BatteryStatus.Critical,
        BatteryStatus.Charging,
    )
    assert len({win._battery_icons[s].cacheKey() for s in distinct_statuses}) == len(distinct_statuses)

    # Statuses mapped to icons
    for status in hud_window.BATTERY_ICON_FILES:
        src_pix = win._battery_icons[status]
        assert src_pix.devicePixelRatio() == hud_window.BATTERY_ICON_DPR
        assert src_pix.size() == hud_window.BATTERY_ICON_CROP_RECT.size()

        win.update_battery_data(BatteryData(battery_id=ShiftingSystemID.RearDerailleur.value, status=status))
        qapp.processEvents()
        assert win.battery_statuses["RD"] == status
        pix = win.battery_value_labels["RD"].pixmap()
        assert not pix.isNull()
        assert pix.deviceIndependentSize().toSize() == hud_window.BATTERY_ICON_SIZE
        assert win.battery_value_labels["RD"].sizeHint() == hud_window.BATTERY_ICON_SIZE

    # Differential update: repeating identical BatteryData should not trigger adjustSize()
    with patch.object(win, "adjustSize") as mock_adjust:
        win.update_battery_data(
            BatteryData(battery_id=ShiftingSystemID.RearDerailleur.value, status=BatteryStatus.Charging)
        )
        assert mock_adjust.call_count == 0

    # Unknown and Invalid fall back to text "--"
    for status in (BatteryStatus.Unknown, BatteryStatus.Invalid):
        win.update_battery_data(BatteryData(battery_id=ShiftingSystemID.RearDerailleur.value, status=status))
        qapp.processEvents()
        assert win.battery_statuses["RD"] == status
        assert win.battery_value_labels["RD"].text() == "--"
        assert win.battery_value_labels["RD"].pixmap().isNull()

    win.close()


def test_capture_execution(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"antID": 100, "teeth": {"front": [50, 34], "rear": [11, 34]}}', encoding="utf-8")
    output_png = tmp_path / "out" / "sample.png"

    ret = capture.main(["-s", str(settings_file), "-o", str(output_png)])
    assert ret == 0
    assert output_png.exists()
    assert output_png.stat().st_size > 0


def test_main_demo_flag(qapp: QApplication) -> None:
    with patch.object(QApplication, "exec", return_value=0):
        ret = main_mod.main(["--demo"])
        assert ret == 0


def test_openant_timeout_warning_filter() -> None:
    filter_obj = ant_worker.OpenantTimeoutWarningFilter()
    test_logger = logging.getLogger("test.openant.base.ant")

    # 1. At non-DEBUG level (INFO): timeout warning should be dropped (filter returns False)
    test_logger.setLevel(logging.INFO)
    rec_timeout = test_logger.makeRecord(
        name=test_logger.name,
        level=logging.WARNING,
        fn="ant.py",
        lno=234,
        msg="%s, %r",
        args=("usb.core.USBError", (None, b"libusb0-dll:err [_usb_reap_async] timeout error\n")),
        exc_info=None,
    )
    assert filter_obj.filter(rec_timeout) is False

    # 2. At DEBUG level: timeout warning should be downgraded to DEBUG level (filter returns True)
    test_logger.setLevel(logging.DEBUG)
    rec_debug = test_logger.makeRecord(
        name=test_logger.name,
        level=logging.WARNING,
        fn="ant.py",
        lno=234,
        msg="%s, %r",
        args=("usb.core.USBError", (None, b"libusb0-dll:err [_usb_reap_async] timeout error\n")),
        exc_info=None,
    )
    assert filter_obj.filter(rec_debug) is True
    assert rec_debug.levelno == logging.DEBUG
    assert rec_debug.levelname == "DEBUG"

    # 3. Unrelated warnings should always pass through untouched
    test_logger.setLevel(logging.INFO)
    rec_other = test_logger.makeRecord(
        name=test_logger.name,
        level=logging.WARNING,
        fn="ant.py",
        lno=206,
        msg="Got unknown message, %r",
        args=(123,),
        exc_info=None,
    )
    assert filter_obj.filter(rec_other) is True
    assert rec_other.levelno == logging.WARNING


def test_patch_pyusb_libusb0_timeout() -> None:
    import usb.backend.libusb0
    import usb.core

    # Ensure patched
    assert ant_worker.patch_pyusb_libusb0_timeout() is True
    patched_check = usb.backend.libusb0._check

    class MockLib:
        def __init__(self, msg: bytes) -> None:
            self._msg = msg

        def usb_strerror(self) -> bytes:
            return self._msg

    # Test 1: libusb0 async reap timeout with ret = None
    usb.backend.libusb0._lib = MockLib(b"libusb0-dll:err [_usb_reap_async] timeout error\n")
    with pytest.raises(usb.core.USBTimeoutError) as exc_info:
        patched_check(None)
    assert "timeout" in str(exc_info.value).lower()
    assert isinstance(exc_info.value, usb.core.USBError)

    # Test 2: MinGW ETIMEDOUT code (-116)
    usb.backend.libusb0._lib = MockLib(b"No error")
    with pytest.raises(usb.core.USBTimeoutError):
        patched_check(-116)

    # Test 3: Standard POSIX ETIMEDOUT (-110)
    with pytest.raises(usb.core.USBTimeoutError):
        patched_check(-110)

    # Test 4: Genuine other error (e.g. -EIO or ret = -5)
    usb.backend.libusb0._lib = MockLib(b"libusb0-dll:err [control_msg] failed")
    with pytest.raises(usb.core.USBError) as exc_info2:
        patched_check(-5)
    assert not isinstance(exc_info2.value, usb.core.USBTimeoutError)

    # Test 5: Positive return (bytes transferred) returns value unchanged
    assert patched_check(128) == 128


def test_apply_usb_fixes_idempotent() -> None:
    ant_worker.apply_usb_fixes()
    ant_worker.apply_usb_fixes()
    openant_logger = logging.getLogger("openant.base.ant")
    matching_filters = [f for f in openant_logger.filters if isinstance(f, ant_worker.OpenantTimeoutWarningFilter)]
    assert len(matching_filters) == 1


def test_hud_power_meter_device_packets_and_discovery() -> None:
    mock_node = MagicMock()
    mock_channel = MagicMock()
    mock_node.new_channel.return_value = mock_channel

    received_batteries: list[BatteryData] = []
    pm = ant_worker.HudPowerMeterDevice(
        mock_node,
        device_id=0,
        on_battery_callback=received_batteries.append,
    )
    assert pm.device_id == 0

    # Non-extended packet ignored while device_id == 0
    pm._on_data([82, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x23])
    assert pm.device_id == 0
    assert len(received_batteries) == 0

    # Extended standard power packet (Page 0x10) discovers device_id = 0x5678 (22136)
    ext_power_pkt = [0x10, 0x01, 0xFF, 90, 0x00, 0x01, 200, 0x00, 0x80, 0x78, 0x56, 0x0B, 0x05]
    pm._on_data(ext_power_pkt)
    assert pm.device_id == 0x5678
    assert pm.trans_type == 0x05
    assert pm._attached is True
    assert pm.last_rx_time is not None

    # Common Page 82 with battery_id = 0xFF (single battery), status = Good (2)
    pm._on_data([82, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x80, 0x23])
    assert len(received_batteries) == 1
    assert received_batteries[-1].status == BatteryStatus.Good
    assert received_batteries[-1] is not pm.data["common"].last_battery_data

    # Packet from another power meter device_id (0x1111) is ignored
    other_ext_pkt = [82, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x80, 0x43, 0x80, 0x11, 0x11, 0x0B, 0x05]
    pm._on_data(other_ext_pkt)
    assert len(received_batteries) == 1


def test_worker_power_meter_discovery_and_watchdog() -> None:
    shift_lost = 0
    pm_lost = 0

    def on_shift_lost() -> None:
        nonlocal shift_lost
        shift_lost += 1

    def on_pm_lost() -> None:
        nonlocal pm_lost
        pm_lost += 1

    worker = ant_worker.AntShiftingWorker(
        ant_id=0,
        on_shift=lambda _: None,
        on_battery=lambda _: None,
        on_connection_lost=on_shift_lost,
        inactivity_timeout=10.0,
        power_meter_id=0,
        on_power_meter_battery=lambda _: None,
        on_power_meter_connection_lost=on_pm_lost,
    )
    assert worker.active_power_meter_id == 0
    assert worker.poll_power_meter_discovery() is None

    mock_shift_dev = MagicMock()
    mock_shift_dev.device_id = 0
    mock_shift_dev.last_rx_time = 100.0
    mock_pm_dev = MagicMock()
    mock_pm_dev.device_id = 22222
    mock_pm_dev.last_rx_time = 95.0
    mock_node = MagicMock()

    worker._device = mock_shift_dev
    worker._power_meter_device = mock_pm_dev
    worker._node = mock_node

    # PowerMeter discovery locks onto 22222
    assert worker.poll_power_meter_discovery() == 22222
    assert worker.active_power_meter_id == 22222

    # At t=106.0: PM timed out (106 - 95 = 11 >= 10), but Shifting is still active (106 - 100 = 6 < 10)
    assert worker.check_inactivity(now=106.0)
    assert pm_lost == 1
    assert shift_lost == 0
    assert mock_pm_dev.last_rx_time is None
    assert mock_node.stop.call_count == 0

    # At t=110.0: Shifting also times out -> node.stop() is called
    assert worker.check_inactivity(now=110.0)
    assert shift_lost == 1
    assert mock_node.stop.call_count == 1


def test_hud_window_battery_visibility_and_pm(qapp: QApplication) -> None:
    # 1. Default batteryStatus: shifters=False, frontDerailleur=True, rearDerailleur=True, powerMeter=False
    default_cfg = settings_mod.validate_and_parse_settings({})
    win_default = hud_window.ShiftingHudWindow(default_cfg)
    win_default.show()
    qapp.processEvents()

    assert not win_default.battery_blocks["L"].isVisible()
    assert not win_default.battery_blocks["R"].isVisible()
    assert win_default.battery_blocks["FD"].isVisible()
    assert win_default.battery_blocks["RD"].isVisible()
    assert not win_default.battery_blocks["PM"].isVisible()
    assert win_default.divider.isVisible()
    win_default.close()

    # 2. All battery components enabled (including PM)
    all_cfg = settings_mod.validate_and_parse_settings(
        {
            "batteryStatus": {
                "shifters": True,
                "frontDerailleur": True,
                "rearDerailleur": True,
                "powerMeter": True,
                "powerMeterID": 1234,
            }
        }
    )
    win_all = hud_window.ShiftingHudWindow(all_cfg)
    win_all.show()
    qapp.processEvents()

    for key in ("L", "R", "FD", "RD", "PM"):
        assert win_all.battery_blocks[key].isVisible()
        assert win_all.battery_value_labels[key].text() == "--"

    # Verify PM is positioned to the right of RD
    assert win_all.battery_blocks["PM"].x() > win_all.battery_blocks["RD"].x()

    # Update PM battery status
    win_all.update_power_meter_battery_data(BatteryData(battery_id=0xFF, status=BatteryStatus.Low))
    qapp.processEvents()
    assert win_all.battery_statuses["PM"] == BatteryStatus.Low
    assert not win_all.battery_value_labels["PM"].pixmap().isNull()

    # Reset PM display only
    win_all.reset_power_meter_display()
    qapp.processEvents()
    assert win_all.battery_statuses["PM"] is None
    assert win_all.battery_value_labels["PM"].text() == "--"
    win_all.close()

    # 3. All battery components disabled -> divider hidden
    none_cfg = settings_mod.validate_and_parse_settings(
        {
            "batteryStatus": {
                "shifters": False,
                "frontDerailleur": False,
                "rearDerailleur": False,
                "powerMeter": False,
            }
        }
    )
    win_none = hud_window.ShiftingHudWindow(none_cfg)
    win_none.show()
    qapp.processEvents()
    for key in ("L", "R", "FD", "RD", "PM"):
        assert not win_none.battery_blocks[key].isVisible()
    assert not win_none.divider.isVisible()
    win_none.close()


def test_ant_logger_parsing_and_formatting(tmp_path: Path) -> None:
    assert ant_logger.parse_device_type("shifting") == (0x22, 8192)
    assert ant_logger.parse_device_type("power") == (0x0B, 8182)
    assert ant_logger.parse_device_type("0x22") == (0x22, 8192)

    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"antID": 43210}', encoding="utf-8")
    assert ant_logger.resolve_ant_id(None, settings_file) == 43210
    assert ant_logger.resolve_ant_id(12345, settings_file) == 12345

    # Page 82 (LeftShifter, Good, 3.00V) with extended header for ID 12345 (0x3039)
    ext_page82 = [82, 0xFF, 0x34, 0x00, 0x00, 0x00, 0x00, 0x23, 0x80, 0x39, 0x30, 0x22, 0x05]
    line = ant_logger.format_packet_line(ext_page82, kind="BROADCAST", device_type=0x22)
    assert "id=12345" in line
    assert "page= 82 (0x52)" in line
    assert "LeftShifter" in line
    assert "Good(2)" in line
    assert "3.00V" in line

    # Page 1 (ShiftStatus)
    page1 = [0x01, 0x05, 0xFF, 0x24, 0x4C, 0x00, 0x00, 0x00]
    summary1 = ant_logger.decode_page_summary(page1, device_type=0x22)
    assert "ShiftStatus" in summary1
    assert "front=2/2" in summary1
    assert "rear=5/12" in summary1


def test_ant_broadcast_logger_filtering_and_file_output(tmp_path: Path) -> None:
    log_file = tmp_path / "packets.log"
    pkt_logger = ant_logger.configure_logging(log_file, debug=False)

    receiver = ant_logger.AntBroadcastLogger(
        ant_id=12345,
        device_type=0x22,
        ignore_page1=True,
        dedup=True,
        packet_logger=pkt_logger,
    )

    # Wrong device ID (9999 = 0x270F) -> ignored
    assert (
        receiver.handle_packet([82, 0xFF, 0x34, 0x00, 0x00, 0x00, 0x00, 0x23, 0x80, 0x0F, 0x27, 0x22, 0x05]) is False
    )
    assert receiver.total_packets == 0

    # Page 1 from matching ID (12345 = 0x3039) -> counted, but suppressed by ignore_page1
    assert (
        receiver.handle_packet([0x01, 0x01, 0xFF, 0x24, 0x4C, 0x00, 0x00, 0x00, 0x80, 0x39, 0x30, 0x22, 0x05]) is False
    )
    assert receiver.total_packets == 1
    assert receiver.logged_packets == 0

    # Page 82 from matching ID -> logged once, duplicate suppressed
    pkt82 = [82, 0xFF, 0x34, 0x00, 0x00, 0x00, 0x00, 0x23, 0x80, 0x39, 0x30, 0x22, 0x05]
    assert receiver.handle_packet(pkt82) is True
    assert receiver.handle_packet(pkt82) is False
    assert receiver.total_packets == 3
    assert receiver.logged_packets == 1
    assert "page 82(0x52)=2" in receiver.format_summary()

    # Flush file handler and verify log contents
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_text = log_file.read_text(encoding="utf-8")
    assert "52 FF 34 00 00 00 00 23" in log_text
    assert "LeftShifter" in log_text


def test_ant_logger_page79_and_tx_payload_builders(tmp_path: Path) -> None:
    # Page 79: Byte 1 = Component ID (0x23 -> RearDerailleur / 3), Byte 2 = SW Supp (0x37=55), Byte 3 = SW Main (0x14=20)
    page79_rd = [0x4F, 0x23, 0x37, 0x14, 0x57, 0x1E, 0xE1, 0x3B]
    summary79 = ant_logger.decode_page_summary(page79_rd, device_type=0x22)
    assert "comp=2(RearDerailleur)/3" in summary79
    assert "sw=20.55" in summary79
    assert "serial=0x3BE11E57" in summary79

    args = ant_logger.parse_args(
        [
            "--ant-id",
            "4111",
            "--request-page",
            "82",
            "--desc1",
            "0xFF,0x03,0x33",
            "--tx-resp",
            "0x04",
            "--send-hex",
            "46 FF FF 43 FF 04 52 01",
        ]
    )
    payloads = ant_logger.build_tx_payloads_from_args(args)
    assert payloads == [
        [0x46, 0xFF, 0xFF, 0xFF, 0xFF, 0x04, 0x52, 0x01],
        [0x46, 0xFF, 0xFF, 0x03, 0xFF, 0x04, 0x52, 0x01],
        [0x46, 0xFF, 0xFF, 0x33, 0xFF, 0x04, 0x52, 0x01],
        [0x46, 0xFF, 0xFF, 0x43, 0xFF, 0x04, 0x52, 0x01],
    ]

    # Verify ACK send path with bounded retry
    log_file = tmp_path / "tx.log"
    pkt_logger = ant_logger.configure_logging(log_file, debug=False)
    receiver = ant_logger.AntBroadcastLogger(
        ant_id=4111,
        send_mode="ack",
        send_retries=2,
        packet_logger=pkt_logger,
    )
    mock_channel = MagicMock()
    mock_channel.id = 0
    receiver.channel = mock_channel
    assert receiver.send_payload(payloads[0]) is True
    mock_channel._ant.send_acknowledged_data.assert_called_once_with(0, payloads[0])
    mock_channel.wait_for_event.assert_called_once()
