from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Callable, Optional

from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.common import BatteryData
from openant.devices.shift import ShiftData, Shifting
from openant.easy.node import Node

_logger = logging.getLogger(__name__)

# ANT+ Shifting Profile (Device Type 0x22 / 34)
SHIFTING_DEVICE_TYPE = 0x22  # 34
DEFAULT_INACTIVITY_TIMEOUT = 30.0


class HudShiftingDevice(Shifting):  # type: ignore[misc]
    """
    ANT+ Shifting device (Device Type 0x22 / 34) subclass that safely decodes
    Page 1 (Shift System Status) and Common Page 82 (Battery Status).
    """

    def __init__(
        self,
        node: Node,
        device_id: int = 0,
        name: str = "shifting",
        trans_type: int = 0,
        on_shift_callback: Optional[Callable[[ShiftData], None]] = None,
        on_battery_callback: Optional[Callable[[BatteryData], None]] = None,
    ):
        self._on_shift_callback = on_shift_callback
        self._on_battery_callback = on_battery_callback
        self.last_rx_time: Optional[float] = None
        super().__init__(
            node=node,
            device_id=device_id,
            name=name,
            trans_type=trans_type,
        )
        # Ensure all 16 4-bit battery IDs (0..15) can be indexed without IndexError
        self.data["batteries"] = [BatteryData() for _ in range(16)]

    def _on_data(self, data: list[int]) -> None:
        self.last_rx_time = time.monotonic()
        super()._on_data(data)

    def on_device_data(self, page: int, page_name: str, data: ShiftData) -> None:
        if page == 0x01 and isinstance(data, ShiftData) and self._on_shift_callback is not None:
            self._on_shift_callback(dataclasses.replace(data))

    def on_battery(self, data: BatteryData) -> None:
        if isinstance(data, BatteryData) and self._on_battery_callback is not None:
            self._on_battery_callback(dataclasses.replace(data))

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


class AntShiftingWorker:
    """Background worker managing the openant Node, Shifting profile channel, and inactivity watchdog."""

    def __init__(
        self,
        ant_id: int,
        on_shift: Callable[[ShiftData], None],
        on_battery: Callable[[BatteryData], None],
        on_connection_lost: Optional[Callable[[], None]] = None,
        inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT,
    ):
        self.ant_id = ant_id
        self._on_shift = on_shift
        self._on_battery = on_battery
        self._on_connection_lost = on_connection_lost
        self._inactivity_timeout = inactivity_timeout
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._node: Optional[Node] = None
        self._device: Optional[HudShiftingDevice] = None
        self._lock = threading.Lock()
        self._last_error_msg: Optional[str] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AntShiftingWorker", daemon=True)
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="AntShiftingWatchdog", daemon=True)
        self._thread.start()
        self._watchdog_thread.start()

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
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=1.0)

    def check_inactivity(self, now: Optional[float] = None) -> bool:
        """
        Check if data reception has timed out due to sensor sleep or range loss.
        Returns True if inactivity timeout was triggered.
        """
        if self._inactivity_timeout <= 0:
            return False

        with self._lock:
            device = self._device

        if device is None or device.last_rx_time is None:
            return False

        current_time = time.monotonic() if now is None else now
        if current_time - device.last_rx_time >= self._inactivity_timeout:
            device.last_rx_time = None
            _logger.info("ANT+ Shifting sensor inactivity timeout; resetting HUD display")
            if self._on_connection_lost is not None:
                self._on_connection_lost()
            return True
        return False

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(timeout=1.0):
            self.check_inactivity()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            node: Optional[Node] = None
            device: Optional[HudShiftingDevice] = None
            connected = False
            try:
                node = Node()
                with self._lock:
                    if self._stop_event.is_set():
                        node.stop()
                        return
                    self._node = node

                node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
                device = HudShiftingDevice(
                    node,
                    device_id=self.ant_id,
                    on_shift_callback=self._on_shift,
                    on_battery_callback=self._on_battery,
                )

                with self._lock:
                    if self._stop_event.is_set():
                        device.close_channel()
                        node.stop()
                        return
                    self._device = device

                self._last_error_msg = None
                connected = True
                _logger.info(
                    f"ANT+ Shifting listener started (device_type=0x{SHIFTING_DEVICE_TYPE:02x}, antID={self.ant_id})"
                )
                node.start()
            except Exception as e:
                if not self._stop_event.is_set():
                    err_msg = f"{type(e).__name__}: {e}"
                    if err_msg != self._last_error_msg:
                        _logger.warning(f"ANT+ connection unavailable or interrupted ({err_msg}); retrying...")
                        self._last_error_msg = err_msg
                    else:
                        _logger.debug(f"ANT+ retry failed ({err_msg}); retrying...")
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
                if connected and not self._stop_event.is_set() and self._on_connection_lost is not None:
                    self._on_connection_lost()

            if not self._stop_event.is_set():
                self._stop_event.wait(timeout=3.0)
