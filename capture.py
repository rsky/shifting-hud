from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from hud_window import ShiftingHudWindow
from main import apply_demo_data
from settings import load_settings

_logger = logging.getLogger(__name__)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture HUD screenshot with demo/dummy data")
    parser.add_argument(
        "-s",
        "--settings",
        type=Path,
        default=None,
        help="Path to settings.json (defaults to ./settings.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("screenshot.png"),
        help="Output image file path (default: screenshot.png)",
    )
    return parser.parse_args(argv)


def capture_screenshot(window: ShiftingHudWindow, output_path: Path) -> bool:
    """Render the HUD window into a translucent ARGB32 PNG image."""
    app = QApplication.instance()
    if app:
        app.processEvents()

    image = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    window.render(image, QPoint(0, 0))

    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    return image.save(str(output_path))


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        settings = load_settings(args.settings)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        _logger.error(f"Failed to load settings: {e}")
        return 1

    app = QApplication.instance() or QApplication(sys.argv)
    window = ShiftingHudWindow(settings)
    apply_demo_data(window)
    window.show()
    app.processEvents()

    out_file = args.output
    if capture_screenshot(window, out_file):
        print(f"Screenshot successfully saved to: {out_file.resolve()}")
        window.close()
        return 0
    else:
        _logger.error(f"Failed to save image to {out_file}")
        window.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
