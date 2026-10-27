from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QApplication

from ant_worker import AntShiftingWorker
from hud_window import AntShiftingBridge, ShiftingHudWindow
from settings import load_settings

_logger = logging.getLogger(__name__)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ANT+ Shifting (Di2 / AXS) HUD Desktop Application")
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
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
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

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
