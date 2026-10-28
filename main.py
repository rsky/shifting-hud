import argparse
import json
import logging
import sys
from pathlib import Path

from openant.devices.common import BatteryData, BatteryStatus
from openant.devices.shift import ShiftData, ShiftingSystemID
from PySide6.QtWidgets import QApplication

from ant_worker import AntShiftingWorker
from hud_window import INVALID_GEAR_FRONT, AntShiftingBridge, ShiftingHudWindow
from settings import load_settings

_logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ANT+ Shifting HUD Desktop Application")
    parser.add_argument(
        "-s",
        "--settings",
        type=Path,
        default=None,
        help="Path to settings.json (defaults to ./settings.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode with sample shift & battery data (no ANT+ hardware required)",
    )
    return parser.parse_args(argv)


def apply_demo_data(window: ShiftingHudWindow) -> None:
    """Inject sample shift and battery telemetry into the HUD window."""
    teeth = window.settings.teeth
    is_front_single = teeth is not None and teeth.front is not None and len(teeth.front) == 1

    total_rear = len(teeth.rear) if (teeth and teeth.rear) else 12
    gear_rear = min(5, total_rear)

    if is_front_single:
        gear_front = INVALID_GEAR_FRONT
        total_front = 0
    else:
        total_front = len(teeth.front) if (teeth and teeth.front) else 2
        gear_front = 2

    window.update_shift_data(
        ShiftData(
            gear_front=gear_front,
            total_front=total_front,
            gear_rear=gear_rear,
            total_rear=total_rear,
        )
    )
    window.update_battery_data(BatteryData(battery_id=ShiftingSystemID.LeftShifter.value, status=BatteryStatus.Good))
    window.update_battery_data(BatteryData(battery_id=ShiftingSystemID.RightShifter.value, status=BatteryStatus.Good))
    if not is_front_single:
        window.update_battery_data(
            BatteryData(battery_id=ShiftingSystemID.FrontDerailleur.value, status=BatteryStatus.New)
        )
    window.update_battery_data(BatteryData(battery_id=ShiftingSystemID.RearDerailleur.value, status=BatteryStatus.Ok))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        settings = load_settings(args.settings)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        _logger.error(f"Failed to load settings: {e}")
        return 1

    app = QApplication.instance() or QApplication(sys.argv)
    window = ShiftingHudWindow(settings)

    if args.demo:
        apply_demo_data(window)
        window.show()
        return app.exec()

    bridge = AntShiftingBridge()
    bridge.shift_data_received.connect(window.update_shift_data)
    bridge.battery_data_received.connect(window.update_battery_data)
    bridge.connection_lost.connect(window.reset_display)

    worker = AntShiftingWorker(
        ant_id=settings.ant_id,
        on_shift=bridge.shift_data_received.emit,
        on_battery=bridge.battery_data_received.emit,
        on_connection_lost=bridge.connection_lost.emit,
    )
    app.aboutToQuit.connect(worker.stop)

    window.show()
    worker.start()
    try:
        return app.exec()
    finally:
        worker.stop()


if __name__ == "__main__":
    sys.exit(main())
