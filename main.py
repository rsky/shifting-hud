from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from openant.base.driver import DriverException
from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.common import BatteryData, BatteryStatus, DeviceType
from openant.devices.shift import ShiftData, Shifting, ShiftingSystemID
from openant.easy.exception import AntException
from openant.easy.node import Node
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_logger = logging.getLogger(__name__)

# ANT+ Shifting Profile (Device Type 0x22 / 34) constants
SHIFTING_DEVICE_TYPE = 0x22  # 34
INVALID_GEAR_FRONT = 7  # 0b111 (3 bits)
INVALID_GEAR_REAR = 31  # 0b11111 (5 bits)

VALID_BATTERY_STATUSES = {
    BatteryStatus.New,
    BatteryStatus.Good,
    BatteryStatus.Ok,
    BatteryStatus.Low,
    BatteryStatus.Critical,
    BatteryStatus.Charging,
}

BATTERY_COMPONENT_KEYS = {
    ShiftingSystemID.LeftShifter: "L",
    ShiftingSystemID.RightShifter: "R",
    ShiftingSystemID.FrontDerailleur: "FD",
    ShiftingSystemID.RearDerailleur: "RD",
}


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

        Returns None if teeth are not configured, gears are invalid, or IndexError occurs.
        """
        if self.teeth is None or not self.teeth.front or not self.teeth.rear:
            return None
        if gear_front is None or gear_rear is None:
            return None

        front_index = gear_front - 1
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
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_and_parse_settings(raw: Any) -> AppSettings:
    """Validate settings dict against settings.schema.json rules and return AppSettings."""
    if not isinstance(raw, dict):
        raise ValueError("settings.json must be a JSON object")

    if "antID" not in raw:
        raise ValueError("Missing required property 'antID' in settings.json")

    ant_id_raw = raw["antID"]
    if not _is_valid_number(ant_id_raw) or int(ant_id_raw) != ant_id_raw or ant_id_raw < 0:
        raise ValueError("'antID' must be a non-negative integer")

    teeth_settings: Optional[TeethSettings] = None
    if "teeth" in raw and raw["teeth"] is not None:
        teeth_raw = raw["teeth"]
        if not isinstance(teeth_raw, dict):
            raise ValueError("'teeth' must be an object")

        front_list: Optional[list[float]] = None
        rear_list: Optional[list[float]] = None

        if "front" in teeth_raw and teeth_raw["front"] is not None:
            if not isinstance(teeth_raw["front"], list) or not all(_is_valid_number(x) for x in teeth_raw["front"]):
                raise ValueError("'teeth.front' must be an array of numbers")
            front_list = [float(x) for x in teeth_raw["front"]]

        if "rear" in teeth_raw and teeth_raw["rear"] is not None:
            if not isinstance(teeth_raw["rear"], list) or not all(_is_valid_number(x) for x in teeth_raw["rear"]):
                raise ValueError("'teeth.rear' must be an array of numbers")
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
    """Load and validate settings.json."""
    resolved_path = resolve_settings_path(path)

    with resolved_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return validate_and_parse_settings(data)


def format_gear_text(gear: int, total: int, invalid_value: int) -> tuple[str, Optional[int]]:
    """
    Format gear text as '<current>/<total>' or '-' when unknown/invalid.
    Returns (formatted_text, valid_current_gear_or_None).
    """
    if 1 <= gear < invalid_value and 1 <= total < invalid_value and gear <= total:
        return f"{gear}/{total}", gear
    return "-", None


def format_battery_status(status: BatteryStatus) -> str:
    """Format BatteryStatus enum to display string or '-'."""
    if status in VALID_BATTERY_STATUSES:
        return str(status.name)
    return "-"


class HudShiftingDevice(Shifting):  # type: ignore[misc]
    """
    ANT+ Shifting device (Device Type 0x22 / 34) subclass that safely decodes
    Page 1 (Shift System Status), Page 4 (Trim), and Common Page 82 (Battery Status).
    """

    def __init__(
        self,
        node: Node,
        device_id: int = 0,
        name: str = "shifting",
        trans_type: int = 0,
    ):
        assert DeviceType.Shifting.value == SHIFTING_DEVICE_TYPE
        super().__init__(
            node=node,
            device_id=device_id,
            name=name,
            trans_type=trans_type,
        )
        # Ensure all 16 4-bit battery IDs (0..15) can be indexed without IndexError
        self.data["batteries"] = [BatteryData() for _ in range(16)]

    def on_data(self, data: list[int]) -> None:
        if not data:
            return

        page = data[0]
        _logger.debug(f"{self} on_data page={page:#04x}: {data}")

        # Main Data Page 1 (0x01): Shift System Status
        if page == 0x01 and len(data) >= 8:
            self._event_count[0][0] = self._event_count[0][1]
            self._event_count[0][1] = data[1]

            shift: ShiftData = self.data["shift"]
            shift.gear_rear = data[3] & 0x1F
            shift.gear_front = (data[3] & 0xE0) >> 5
            shift.total_rear = data[4] & 0x1F
            shift.total_front = (data[4] & 0xE0) >> 5
            shift.invalid_inboard_rear = data[5] & 0x0F
            shift.invalid_outboard_rear = (data[5] & 0xF0) >> 4
            shift.invalid_inboard_front = data[6] & 0x0F
            shift.invalid_outboard_front = (data[6] & 0xF0) >> 4
            shift.shift_failure_rear = data[7] & 0x0F
            shift.shift_failure_front = (data[7] & 0xF0) >> 4

            self.on_device_data(page, "shift_system_status", shift)

        # Data Page 4 (0x04): TrimAdjust (preserve existing gear_front / gear_rear)
        elif page == 0x04 and len(data) >= 6:
            self._event_count[2][0] = self._event_count[2][1]
            self._event_count[2][1] = data[1]

            shift = self.data["shift"]
            shift.max_trim_rear = data[2]
            shift.max_trim_front = data[3]
            shift.current_trim_rear = data[4]
            shift.current_trim_front = data[5]

        elif page in (0x02, 0x03):
            try:
                super().on_data(data)
            except IndexError:
                _logger.debug("Ignored truncated page %#04x data: %s", page, data)


class AntShiftingBridge(QObject):
    """Qt Signal bridge for thread-safe UI updates from the openant worker thread."""

    shift_data_received = Signal(object)  # ShiftData
    battery_data_received = Signal(object)  # BatteryData


class AntShiftingWorker:
    """Background worker managing the openant Node and Shifting profile channel."""

    def __init__(self, ant_id: int, bridge: AntShiftingBridge):
        self.ant_id = ant_id
        self.bridge = bridge
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._node: Optional[Node] = None
        self._device: Optional[HudShiftingDevice] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AntShiftingWorker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            device = self._device
            node = self._node
            self._device = None
            self._node = None

        if device is not None:
            try:
                device.close_channel()
            except Exception as e:
                _logger.debug(f"Error closing channel: {e}")

        if node is not None:
            try:
                node.stop()
            except Exception as e:
                _logger.debug(f"Error stopping node: {e}")

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            node: Optional[Node] = None
            device: Optional[HudShiftingDevice] = None
            try:
                node = Node()
                node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
                device = HudShiftingDevice(node, device_id=self.ant_id)

                device.on_device_data = self._on_device_data
                device.on_battery = self._on_battery

                with self._lock:
                    if self._stop_event.is_set():
                        device.close_channel()
                        node.stop()
                        return
                    self._node = node
                    self._device = device

                _logger.info(
                    f"ANT+ Shifting listener started (device_type=0x{SHIFTING_DEVICE_TYPE:02x}, antID={self.ant_id})"
                )
                node.start()
            except (DriverException, AntException, Exception) as e:
                if not self._stop_event.is_set():
                    _logger.warning(f"ANT+ connection unavailable or interrupted ({e}); retrying...")
            finally:
                with self._lock:
                    self._device = None
                    self._node = None
                if device is not None:
                    try:
                        device.close_channel()
                    except Exception:
                        pass
                if node is not None:
                    try:
                        node.stop()
                    except Exception:
                        pass

            if not self._stop_event.is_set():
                self._stop_event.wait(timeout=3.0)

    def _on_device_data(self, page: int, page_name: str, data: ShiftData) -> None:
        if page == 0x01 and isinstance(data, ShiftData):
            self.bridge.shift_data_received.emit(data)

    def _on_battery(self, data: BatteryData) -> None:
        if isinstance(data, BatteryData):
            self.bridge.battery_data_received.emit(data)


class ShiftingHudWindow(QWidget):
    """
    Frameless, always-on-top, rounded-corner translucent HUD window with opaque text.
    """

    CORNER_RADIUS = 16
    BG_COLOR = QColor(18, 20, 26, 185)
    BORDER_COLOR = QColor(255, 255, 255, 45)

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.settings = settings
        self._drag_offset: Optional[QPoint] = None

        self.setWindowTitle("Shifting HUD")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._init_ui()
        self._init_shortcuts()

    def _init_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(20, 14, 20, 14)
        root_layout.setSpacing(8)
        root_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        # --- Main Content (FRONT, REAR, optional GEAR RATIO) ---
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(24)

        self.front_block, self.front_value_label = self._create_main_metric_block("FRONT", "-")
        self.rear_block, self.rear_value_label = self._create_main_metric_block("REAR", "-")
        self.ratio_block, self.ratio_value_label = self._create_main_metric_block("RATIO", "-")
        self.ratio_block.setVisible(False)

        main_layout.addWidget(self.front_block)
        main_layout.addWidget(self.rear_block)
        main_layout.addWidget(self.ratio_block)

        # Subtle divider line between main and sub content
        divider = QFrame(self)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: rgba(255, 255, 255, 40); border: none;")

        # --- Sub Content (Batteries: L, R, FD, RD) ---
        sub_layout = QHBoxLayout()
        sub_layout.setContentsMargins(0, 2, 0, 0)
        sub_layout.setSpacing(16)

        self.battery_value_labels: dict[str, QLabel] = {}
        for key in ("L", "R", "FD", "RD"):
            block, val_label = self._create_sub_metric_block(key, "-")
            self.battery_value_labels[key] = val_label
            sub_layout.addWidget(block)

        root_layout.addLayout(main_layout)
        root_layout.addWidget(divider)
        root_layout.addLayout(sub_layout)

    def _create_main_metric_block(self, title: str, initial_value: str) -> tuple[QWidget, QLabel]:
        container = QWidget(self)
        container.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(2)

        title_label = QLabel(title, container)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet(
            "color: rgba(185, 195, 210, 255);"
            "font-size: 11px;"
            "font-weight: 700;"
            "letter-spacing: 1px;"
            "background: transparent;"
        )

        value_label = QLabel(initial_value, container)
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value_label.setMinimumWidth(68)
        value_label.setStyleSheet(
            "color: rgba(255, 255, 255, 255);font-size: 28px;font-weight: 800;background: transparent;"
        )

        layout.addWidget(title_label)
        layout.addWidget(value_label)
        return container, value_label

    def _create_sub_metric_block(self, title: str, initial_value: str) -> tuple[QWidget, QLabel]:
        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(4)

        title_label = QLabel(f"{title}:", container)
        title_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_label.setStyleSheet(
            "color: rgba(170, 180, 195, 255);font-size: 11px;font-weight: 700;background: transparent;"
        )

        value_label = QLabel(initial_value, container)
        value_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        value_label.setMinimumWidth(28)
        value_label.setStyleSheet(
            "color: rgba(255, 255, 255, 255);font-size: 11px;font-weight: 600;background: transparent;"
        )

        layout.addWidget(title_label)
        layout.addWidget(value_label)
        return container, value_label

    def _init_shortcuts(self) -> None:
        for key in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(self.close)

    def update_shift_data(self, data: ShiftData) -> None:
        """Update FRONT, REAR, and optional GEAR RATIO from ShiftData."""
        front_text, valid_front = format_gear_text(data.gear_front, data.total_front, INVALID_GEAR_FRONT)
        rear_text, valid_rear = format_gear_text(data.gear_rear, data.total_rear, INVALID_GEAR_REAR)

        self.front_value_label.setText(front_text)
        self.rear_value_label.setText(rear_text)

        ratio = self.settings.calculate_gear_ratio(valid_front, valid_rear)
        if ratio is not None:
            self.ratio_value_label.setText(f"{ratio:.2f}")
            self.ratio_block.setVisible(True)
        else:
            self.ratio_value_label.setText("-")
            self.ratio_block.setVisible(False)

        self.adjustSize()
        self.update()

    def update_battery_data(self, data: BatteryData) -> None:
        """Update L, R, FD, RD battery display from BatteryData."""
        system_id = ShiftingSystemID(data.battery_id) if data.battery_id != 0xFF else ShiftingSystemID.Unknown
        component_key = BATTERY_COMPONENT_KEYS.get(system_id)
        if component_key is None:
            return

        status_text = format_battery_status(data.status)
        self.battery_value_labels[component_key].setText(status_text)
        self.adjustSize()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(self.BG_COLOR)
        painter.setPen(QPen(self.BORDER_COLOR, 1.2))
        painter.drawRoundedRect(rect, self.CORNER_RADIUS, self.CORNER_RADIUS)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        menu = QMenu(self)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)
        menu.exec(event.globalPos())


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

    settings = load_settings(args.settings)

    app = QApplication.instance() or QApplication(sys.argv)
    window = ShiftingHudWindow(settings)

    bridge = AntShiftingBridge()
    bridge.shift_data_received.connect(window.update_shift_data)
    bridge.battery_data_received.connect(window.update_battery_data)

    worker = AntShiftingWorker(ant_id=settings.ant_id, bridge=bridge)
    app.aboutToQuit.connect(worker.stop)

    window.show()
    worker.start()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
