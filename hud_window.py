from __future__ import annotations

from pathlib import Path
from typing import Optional

from openant.devices.common import BatteryData, BatteryStatus
from openant.devices.shift import ShiftData, ShiftingSystemID
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from settings import AppSettings

INVALID_GEAR_FRONT = 7  # 0b111 (3 bits)
INVALID_GEAR_REAR = 31  # 0b11111 (5 bits)

BATTERY_COMPONENT_KEYS = {
    ShiftingSystemID.LeftShifter: "L",
    ShiftingSystemID.RightShifter: "R",
    ShiftingSystemID.FrontDerailleur: "FD",
    ShiftingSystemID.RearDerailleur: "RD",
}

INITIAL_VALUE = "--"

ICONS_DIR = Path(__file__).resolve().parent / "icons"
BATTERY_ICON_CROP_RECT = QRect(0, 8, 48, 32)
BATTERY_ICON_SIZE = QSize(24, 16)
BATTERY_ICON_DPR = 2.0

# Battery icons are from Akar Icons (https://akaricons.com/), licensed under MIT License.
BATTERY_ICON_FILES: dict[BatteryStatus, str] = {
    BatteryStatus.New: "akar-icons--battery-full@2x.png",
    BatteryStatus.Good: "akar-icons--battery-full@2x.png",
    BatteryStatus.Ok: "akar-icons--battery-medium@2x.png",
    BatteryStatus.Low: "akar-icons--battery-low@2x.png",
    BatteryStatus.Critical: "akar-icons--battery-empty@2x.png",
    BatteryStatus.Charging: "akar-icons--battery-charging@2x.png",
}


def load_battery_icons(icons_dir: Path = ICONS_DIR) -> dict[BatteryStatus, QPixmap]:
    """Load and crop @2x icons (48x48 -> 48x32) with devicePixelRatio=2.0 (logical 24x16)."""
    pixmap_cache: dict[str, QPixmap] = {}
    icons: dict[BatteryStatus, QPixmap] = {}
    for status, filename in BATTERY_ICON_FILES.items():
        if filename not in pixmap_cache:
            img = QImage(str(icons_dir / filename))
            if not img.isNull():
                cropped = img.copy(BATTERY_ICON_CROP_RECT)
                pixmap = QPixmap.fromImage(cropped)
                pixmap.setDevicePixelRatio(BATTERY_ICON_DPR)
                pixmap_cache[filename] = pixmap
        if filename in pixmap_cache:
            icons[status] = pixmap_cache[filename]
    return icons


def format_gear_text(gear: int, total: int, invalid_value: int) -> tuple[str, Optional[int]]:
    """
    Format gear text as '<current>/<total>' or '--' when unknown/invalid.
    Returns (formatted_text, valid_current_gear_or_None).
    """
    if 1 <= gear < invalid_value and 1 <= total < invalid_value and gear <= total:
        return f"{gear}/{total}", gear
    return INITIAL_VALUE, None


class AntShiftingBridge(QObject):
    """Qt Signal bridge for thread-safe UI updates from the openant worker thread."""

    shift_data_received = Signal(object)  # ShiftData
    battery_data_received = Signal(object)  # BatteryData
    connection_lost = Signal()


class ShiftingHudWindow(QWidget):
    """
    Frameless, always-on-top, rounded-corner translucent HUD window with opaque text.
    """

    CORNER_RADIUS = 16
    MAIN_VALUE_MIN_WIDTH = 104
    SUB_VALUE_MIN_WIDTH = BATTERY_ICON_SIZE.width()

    BG_COLOR = QColor(18, 20, 26, 88)
    BORDER_COLOR = QColor(255, 255, 255, 45)

    FONT_FAMILY = '"Avenir Next", "Segoe UI", sans-serif'

    MAIN_TITLE_STYLE = (
        "color: rgba(185, 195, 210, 255);"
        f"font-family: {FONT_FAMILY};"
        "font-size: 14px;"
        "font-weight: 700;"
        "letter-spacing: 1px;"
        "background: transparent;"
    )
    MAIN_VALUE_STYLE = (
        "color: rgba(255, 255, 255, 255);"
        f"font-family: {FONT_FAMILY};"
        "font-size: 32px;"
        "font-weight: 900;"
        "background: transparent;"
    )
    SUB_TITLE_STYLE = (
        "color: rgba(170, 180, 195, 255);"
        f"font-family: {FONT_FAMILY};"
        "font-size: 14px;"
        "font-weight: 700;"
        "background: transparent;"
    )
    SUB_VALUE_STYLE = (
        "color: rgba(255, 255, 255, 255);"
        f"font-family: {FONT_FAMILY};"
        "font-size: 14px;"
        "font-weight: 700;"
        "background: transparent;"
    )
    DIVIDER_STYLE = "background-color: rgba(255, 255, 255, 40); border: none;"

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.settings = settings
        self._drag_offset: Optional[QPoint] = None
        self._battery_icons = load_battery_icons()
        self.battery_statuses: dict[str, Optional[BatteryStatus]] = {
            key: None for key in BATTERY_COMPONENT_KEYS.values()
        }

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

        self.front_block, self.front_value_label = self._create_main_metric_block("FRONT", INITIAL_VALUE)
        self.rear_block, self.rear_value_label = self._create_main_metric_block("REAR", INITIAL_VALUE)
        self.ratio_block, self.ratio_value_label = self._create_main_metric_block("RATIO", INITIAL_VALUE)
        self.ratio_block.setVisible(False)

        main_layout.addWidget(self.front_block)
        main_layout.addWidget(self.rear_block)
        main_layout.addWidget(self.ratio_block)

        # Subtle divider line between main and sub content
        divider = QFrame(self)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFixedHeight(1)
        divider.setStyleSheet(self.DIVIDER_STYLE)

        # --- Sub Content (Batteries: L, R, FD, RD) ---
        sub_layout = QHBoxLayout()
        sub_layout.setContentsMargins(0, 2, 0, 0)
        sub_layout.setSpacing(16)

        self.battery_value_labels: dict[str, QLabel] = {}
        for key in BATTERY_COMPONENT_KEYS.values():
            block, val_label = self._create_sub_metric_block(key, INITIAL_VALUE)
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
        title_label.setStyleSheet(self.MAIN_TITLE_STYLE)

        value_label = QLabel(initial_value, container)
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value_label.setMinimumWidth(self.MAIN_VALUE_MIN_WIDTH)
        value_label.setStyleSheet(self.MAIN_VALUE_STYLE)

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
        title_label.setStyleSheet(self.SUB_TITLE_STYLE)

        value_label = QLabel(initial_value, container)
        value_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        value_label.setMinimumWidth(self.SUB_VALUE_MIN_WIDTH)
        value_label.setStyleSheet(self.SUB_VALUE_STYLE)

        layout.addWidget(title_label)
        layout.addWidget(value_label)
        return container, value_label

    def _set_battery_label(self, component_key: str, status: Optional[BatteryStatus]) -> None:
        label = self.battery_value_labels[component_key]
        pixmap = self._battery_icons.get(status) if status is not None else None
        if pixmap is not None and not pixmap.isNull():
            label.setPixmap(pixmap)
        else:
            label.setText(INITIAL_VALUE)

    def _init_shortcuts(self) -> None:
        for key in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(self.close)

    def reset_display(self) -> None:
        """Reset all HUD metrics to '--' when the connection is lost."""
        changed = False
        if self.front_value_label.text() != INITIAL_VALUE or self.front_block.isHidden():
            self.front_value_label.setText(INITIAL_VALUE)
            self.front_block.setVisible(True)
            changed = True
        if self.rear_value_label.text() != INITIAL_VALUE:
            self.rear_value_label.setText(INITIAL_VALUE)
            changed = True
        if self.ratio_value_label.text() != INITIAL_VALUE or not self.ratio_block.isHidden():
            self.ratio_value_label.setText(INITIAL_VALUE)
            self.ratio_block.setVisible(False)
            changed = True
        for key, status in self.battery_statuses.items():
            if status is not None:
                self.battery_statuses[key] = None
                self._set_battery_label(key, None)
                changed = True
        if changed:
            self.adjustSize()
            self.update()

    def update_shift_data(self, data: ShiftData) -> None:
        """Update FRONT, REAR, and optional GEAR RATIO from ShiftData (only redraws on change)."""
        front_text, valid_front = format_gear_text(data.gear_front, data.total_front, INVALID_GEAR_FRONT)
        rear_text, valid_rear = format_gear_text(data.gear_rear, data.total_rear, INVALID_GEAR_REAR)

        # Hide FRONT block when FD is absent (1x setup: front unknown while rear is valid)
        front_visible = not (valid_front is None and valid_rear is not None)

        ratio = self.settings.calculate_gear_ratio(valid_front, valid_rear)
        ratio_visible = ratio is not None
        ratio_text = f"{ratio:.2f}" if ratio is not None else INITIAL_VALUE

        changed = False
        if self.front_value_label.text() != front_text:
            self.front_value_label.setText(front_text)
            changed = True
        if self.front_block.isHidden() == front_visible:
            self.front_block.setVisible(front_visible)
            changed = True
        if self.rear_value_label.text() != rear_text:
            self.rear_value_label.setText(rear_text)
            changed = True
        if self.ratio_value_label.text() != ratio_text:
            self.ratio_value_label.setText(ratio_text)
            changed = True
        if self.ratio_block.isHidden() == ratio_visible:
            self.ratio_block.setVisible(ratio_visible)
            changed = True

        if changed:
            self.adjustSize()
            self.update()

    def update_battery_data(self, data: BatteryData) -> None:
        """Update L, R, FD, RD battery display from BatteryData (only redraws on change)."""
        system_id = ShiftingSystemID(data.battery_id) if data.battery_id != 0xFF else ShiftingSystemID.Unknown
        component_key = BATTERY_COMPONENT_KEYS.get(system_id)
        if component_key is None:
            return

        if self.battery_statuses.get(component_key) == data.status:
            return

        self.battery_statuses[component_key] = data.status
        self._set_battery_label(component_key, data.status)
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
