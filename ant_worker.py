import dataclasses
import errno
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from openant.base.message import Message
from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.common import BatteryData, BatteryStatus
from openant.devices.shift import ShiftData, Shifting, ShiftingSystemID
from openant.easy.node import Node

_logger = logging.getLogger(__name__)

# ANT+ Shifting Profile (Device Type 0x22 / 34)
SHIFTING_DEVICE_TYPE = 0x22  # 34
INVALID_GEAR_FRONT = 7  # 0b111 (3 bits)
INVALID_GEAR_REAR = 31  # 0b11111 (5 bits)
SHIFT_STATUS_PAGE = 0x01
BATTERY_STATUS_PAGE = 82  # 0x52 Common Data Page
STANDARD_PAYLOAD_LEN = 8
EXTENDED_HEADER_MIN_LEN = 13
MAX_BATTERY_SLOTS = 16
DEFAULT_INACTIVITY_TIMEOUT = 30.0
DEFAULT_DISCOVERY_POLL_INTERVAL = 1.0
DEFAULT_RETRY_DELAY = 3.0


class OpenantTimeoutWarningFilter(logging.Filter):
    """
    Filter out false-positive USB read timeout warnings logged by openant on Windows.
    When debug logging is disabled, timeout warnings from openant are dropped.
    When debug logging is enabled, they are downgraded to DEBUG level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.WARNING:
            msg = record.getMessage()
            if "timeout error" in msg.lower() or "_usb_reap_async" in msg.lower():
                logger = logging.getLogger(record.name)
                if not logger.isEnabledFor(logging.DEBUG):
                    return False
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
        return True


_usb_fixes_applied = False


def patch_pyusb_libusb0_timeout() -> bool:
    """
    Patch pyusb's libusb0 backend (_check function) so that Windows libusb-win32
    read timeouts (such as [_usb_reap_async] timeout error) properly raise
    usb.core.USBTimeoutError instead of generic usb.core.USBError.
    This prevents openant from treating normal read timeouts as unexpected errors.
    """
    try:
        import usb.backend.libusb0
        import usb.core
    except ImportError:
        return False

    orig_check = getattr(usb.backend.libusb0, "_check", None)
    if orig_check is None or getattr(orig_check, "_is_timeout_patched", False):
        return True

    def _patched_check(ret: Any) -> Any:
        lib = getattr(usb.backend.libusb0, "_lib", None)
        if ret is None:
            errmsg = lib.usb_strerror() if lib is not None else ""
        else:
            if hasattr(ret, "value"):
                ret = ret.value
            if ret < 0:
                errmsg = lib.usb_strerror() if lib is not None else ""
                if hasattr(errmsg, "lower") and errmsg.lower() == "no error":
                    errmsg = os.strerror(-ret)
            else:
                return ret

        is_timeout = False
        timeout_errno = getattr(errno, "ETIMEDOUT", 110)
        # Windows / MinGW / POSIX timeout errno checks (110 = Linux, 116 = MinGW/libusb0, 138 = MSVC)
        if ret is not None and (-ret == timeout_errno or -ret in (110, 116, 138)):
            is_timeout = True
        elif errmsg:
            err_bytes = errmsg if isinstance(errmsg, bytes) else str(errmsg).encode("latin1", errors="ignore")
            if b"timeout" in err_bytes.lower() or b"_usb_reap_async" in err_bytes.lower():
                is_timeout = True

        if is_timeout:
            raise usb.core.USBTimeoutError(errmsg, ret, -ret if ret is not None else timeout_errno)
        raise usb.core.USBError(errmsg, ret)

    _patched_check._is_timeout_patched = True  # type: ignore[attr-defined]
    usb.backend.libusb0._check = _patched_check
    return True


def apply_usb_fixes() -> None:
    """Apply PyUSB libusb0 timeout normalization and openant logging filter."""
    global _usb_fixes_applied
    if _usb_fixes_applied:
        return
    _usb_fixes_applied = True

    patch_pyusb_libusb0_timeout()

    openant_logger = logging.getLogger("openant.base.ant")
    if not any(isinstance(f, OpenantTimeoutWarningFilter) for f in openant_logger.filters):
        openant_logger.addFilter(OpenantTimeoutWarningFilter())


apply_usb_fixes()



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
        on_shift_callback: Callable[[ShiftData], None] | None = None,
        on_battery_callback: Callable[[BatteryData], None] | None = None,
    ):
        self._on_shift_callback = on_shift_callback
        self._on_battery_callback = on_battery_callback
        self.last_rx_time: float | None = None
        self.device_id: int = device_id
        self.trans_type: int = trans_type
        self._attached: bool = False
        super().__init__(
            node=node,
            device_id=device_id,
            name=name,
            trans_type=trans_type,
        )
        # Ensure all 16 4-bit battery IDs (0..15) can be indexed without IndexError
        self.data["batteries"] = [BatteryData() for _ in range(MAX_BATTERY_SLOTS)]

    def _accept_packet(self, data: list[int]) -> bool:
        """
        Inspect extended header (when present) to discover or filter the device ID
        without triggering openant's channel close/reopen race condition.
        """
        if len(data) >= EXTENDED_HEADER_MIN_LEN:
            rx_device_id = data[9] | (data[10] << 8)
            rx_device_type = data[11]
            rx_trans_type = data[12]

            if self.device_id == 0:
                if rx_device_id == 0:
                    return False
                self.device_id = rx_device_id
                self.trans_type = rx_trans_type
                self._attached = True
                _logger.info(f"Device ID #{rx_device_id:05} of type {rx_device_type}:{rx_trans_type} attached: {self}")
                return True

            if rx_device_id != 0 and rx_device_id != self.device_id:
                _logger.debug(f"Ignoring packet from Device ID #{rx_device_id:05} (expected #{self.device_id:05})")
                return False

            if not self._attached:
                self._attached = True
                _logger.info(f"Device ID #{rx_device_id:05} of type {rx_device_type}:{rx_trans_type} attached: {self}")

        return self.device_id != 0

    def _on_data(self, data: list[int]) -> None:
        if not self._accept_packet(data):
            return

        if data and data[0] == BATTERY_STATUS_PAGE:
            _logger.debug(f"Raw Page 82 (Battery) packet: {[f'0x{b:02X}' for b in data]}")

        if STANDARD_PAYLOAD_LEN < len(data) < EXTENDED_HEADER_MIN_LEN:
            # Truncate malformed extended payload (< 13 bytes) to 8 bytes so super()._on_data won't IndexError
            data = data[:STANDARD_PAYLOAD_LEN]

        self.last_rx_time = time.monotonic()
        super()._on_data(data)

    def close_channel(self) -> None:
        """Safely close and remove the device channel without triggering CHANNEL_IN_WRONG_STATE."""
        if not hasattr(self, "channel") or self.channel is None:
            return
        try:
            if self.node is not None and self.channel in self.node.channels:
                self.channel.close()
                if getattr(self.channel._ant, "_running", False):
                    try:
                        self.channel.wait_for_event([Message.Code.EVENT_CHANNEL_CLOSED])
                    except Exception:
                        pass
                    try:
                        self.channel._unassign()
                    except Exception:
                        pass
                if self.channel in self.node.channels:
                    self.node.channels.remove(self.channel)
        except Exception as e:
            _logger.debug(f"Error closing channel: {e}")

    def on_device_data(self, page: int, page_name: str, data: ShiftData) -> None:
        if page == SHIFT_STATUS_PAGE and isinstance(data, ShiftData) and self._on_shift_callback is not None:
            self._on_shift_callback(dataclasses.replace(data))

    def on_battery(self, data: BatteryData) -> None:
        if not isinstance(data, BatteryData):
            return

        system_id = ShiftingSystemID(data.battery_id) if data.battery_id != 0xFF else ShiftingSystemID.Unknown
        status_name = data.status.name if hasattr(data.status, "name") else str(data.status)
        voltage_str = (
            f"{data.voltage_coarse + data.voltage_fractional:.2f}V"
            if (data.voltage_coarse is not None and data.voltage_fractional is not None)
            else "N/A"
        )
        _logger.debug(
            f"Battery update: ID={data.battery_id} ({system_id.name}), status={status_name}, "
            f"voltage={voltage_str}, operating_time={data.operating_time}s"
        )

        if _logger.isEnabledFor(logging.DEBUG):
            # Dump all battery slots (0..15) that have reported data
            active_slots: list[str] = []
            batteries: list[BatteryData] = self.data.get("batteries", [])
            for idx, b in enumerate(batteries):
                b_status = getattr(b, "status", None)
                if b_status is not None and b_status != BatteryStatus.Invalid:
                    b_sys = ShiftingSystemID(idx) if idx < 12 else ShiftingSystemID.Unknown
                    b_status_str = b_status.name if hasattr(b_status, "name") else str(b_status)
                    b_volt = (
                        f"{b.voltage_coarse + b.voltage_fractional:.2f}V"
                        if (
                            getattr(b, "voltage_coarse", None) is not None
                            and getattr(b, "voltage_fractional", None) is not None
                        )
                        else "N/A"
                    )
                    active_slots.append(f"[Slot {idx} ({b_sys.name}): {b_status_str}, {b_volt}]")

            if active_slots:
                _logger.debug(f"All active battery states ({len(active_slots)}): {', '.join(active_slots)}")
            else:
                _logger.debug("All active battery states: none recorded yet")

        if self._on_battery_callback is not None:
            self._on_battery_callback(dataclasses.replace(data))

    def on_data(self, data: list[int]) -> None:
        if not data:
            return

        page = data[0]
        _logger.debug(f"{self} on_data page={page:#04x}: {data}")

        # Main Data Page 1 (0x01): Shift System Status
        if page == SHIFT_STATUS_PAGE and len(data) >= STANDARD_PAYLOAD_LEN:
            self._event_count[0][0] = self._event_count[0][1]
            self._event_count[0][1] = data[1]

            raw_gear_rear = data[3] & 0x1F
            raw_gear_front = (data[3] & 0xE0) >> 5

            shift: ShiftData = self.data["shift"]
            # ANT+ Shifting profile transmits 0-indexed gear positions (0 = innermost gear).
            # Convert to 1-based gear numbers (1 = innermost) used throughout the application.
            shift.gear_rear = raw_gear_rear + 1 if raw_gear_rear < INVALID_GEAR_REAR else INVALID_GEAR_REAR
            shift.gear_front = raw_gear_front + 1 if raw_gear_front < INVALID_GEAR_FRONT else INVALID_GEAR_FRONT
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
    """Background worker managing the openant Node, Shifting profile channel, discovery polling, and inactivity watchdog."""

    def __init__(
        self,
        ant_id: int,
        on_shift: Callable[[ShiftData], None],
        on_battery: Callable[[BatteryData], None],
        on_connection_lost: Callable[[], None] | None = None,
        inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT,
        discovery_poll_interval: float = DEFAULT_DISCOVERY_POLL_INTERVAL,
    ):
        apply_usb_fixes()
        self.ant_id = ant_id
        self._active_ant_id = ant_id
        self._on_shift = on_shift
        self._on_battery = on_battery
        self._on_connection_lost = on_connection_lost
        self._inactivity_timeout = inactivity_timeout
        self._discovery_poll_interval = discovery_poll_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._node: Node | None = None
        self._device: HudShiftingDevice | None = None
        self._lock = threading.Lock()
        self._last_error_msg: str | None = None

    @property
    def active_ant_id(self) -> int:
        with self._lock:
            return self._active_ant_id

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="AntShiftingWorker", daemon=True)
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="AntShiftingWatchdog", daemon=True)
        self._thread.start()
        self._watchdog_thread.start()

    def _close_session(
        self,
        device: HudShiftingDevice | None = None,
        node: Node | None = None,
    ) -> None:
        with self._lock:
            target_device = device if device is not None else self._device
            target_node = node if node is not None else self._node
            self._device = None
            self._node = None

        if target_device is not None:
            try:
                target_device.close_channel()
            except Exception as e:
                _logger.debug(f"Error closing channel: {e}")

        if target_node is not None:
            try:
                target_node.stop()
            except Exception as e:
                _logger.debug(f"Error stopping node: {e}")

    def stop(self) -> None:
        self._stop_event.set()
        self._close_session()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=1.0)

    def poll_discovery(self) -> int | None:
        """
        Poll whether a Shifting device ID has been discovered when started with antID=0.
        Returns the discovered device ID when newly locked, or None otherwise.
        """
        with self._lock:
            if self._active_ant_id != 0:
                return self._active_ant_id
            device = self._device
            if device is None:
                return None
            discovered_id = int(device.device_id)
            if discovered_id != 0:
                self._active_ant_id = discovered_id
                _logger.info(f"ANT+ Shifting device discovered (antID={discovered_id}); continuing reception")
                return discovered_id

        _logger.debug("Polling for ANT+ Shifting device (antID=0)...")
        return None

    def check_inactivity(self, now: float | None = None) -> bool:
        """
        Check if data reception has timed out due to sensor sleep or range loss.
        Returns True if inactivity timeout was triggered.
        """
        if self._inactivity_timeout <= 0:
            return False

        with self._lock:
            device = self._device
            node = self._node

        if device is None or device.last_rx_time is None:
            return False

        current_time = time.monotonic() if now is None else now
        if current_time - device.last_rx_time >= self._inactivity_timeout:
            device.last_rx_time = None
            _logger.info("ANT+ Shifting sensor inactivity timeout; resetting HUD display and reconnecting")
            if self._on_connection_lost is not None:
                self._on_connection_lost()
            if node is not None:
                try:
                    node.stop()
                except Exception as e:
                    _logger.debug(f"Error stopping node on inactivity: {e}")
            return True
        return False

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(timeout=self._discovery_poll_interval):
            if self.active_ant_id == 0:
                self.poll_discovery()
            self.check_inactivity()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            node: Node | None = None
            device: HudShiftingDevice | None = None
            connected = False
            try:
                node = Node()
                with self._lock:
                    if self._stop_event.is_set():
                        node.stop()
                        return
                    self._node = node
                    effective_ant_id = self._active_ant_id

                node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
                device = HudShiftingDevice(
                    node,
                    device_id=effective_ant_id,
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
                if effective_ant_id == 0:
                    _logger.info(
                        f"ANT+ Shifting listener started in discovery mode (device_type=0x{SHIFTING_DEVICE_TYPE:02x}, antID=0); polling every {self._discovery_poll_interval:.1f}s..."
                    )
                else:
                    _logger.info(
                        f"ANT+ Shifting listener started (device_type=0x{SHIFTING_DEVICE_TYPE:02x}, antID={effective_ant_id})"
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
                if self.active_ant_id == 0:
                    self.poll_discovery()
                self._close_session(device, node)
                if connected and not self._stop_event.is_set() and self._on_connection_lost is not None:
                    self._on_connection_lost()

            if not self._stop_event.is_set():
                self._stop_event.wait(timeout=DEFAULT_RETRY_DELAY)
