import argparse
import json
import logging
import signal
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from openant.base.message import Message
from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.common import BatteryStatus
from openant.devices.shift import ShiftingSystemID
from openant.easy.channel import Channel
from openant.easy.exception import TransferFailedException
from openant.easy.node import Node

from ant_worker import (
    EXTENDED_HEADER_MIN_LEN,
    INVALID_GEAR_FRONT,
    INVALID_GEAR_REAR,
    SHIFTING_DEVICE_TYPE,
    STANDARD_PAYLOAD_LEN,
    apply_usb_fixes,
    build_page70_payload,
    close_channel_safely,
)
from settings import load_settings

_logger = logging.getLogger(__name__)

ANTPLUS_RF_FREQ = 57  # 2457 MHz
POWER_METER_DEVICE_TYPE = 0x0B  # 11
DEFAULT_SHIFTING_PERIOD = 8192
DEFAULT_POWER_METER_PERIOD = 8182

DEVICE_TYPE_PRESETS: dict[str, tuple[int, int]] = {
    "shifting": (SHIFTING_DEVICE_TYPE, DEFAULT_SHIFTING_PERIOD),
    "power": (POWER_METER_DEVICE_TYPE, DEFAULT_POWER_METER_PERIOD),
    "any": (0, DEFAULT_SHIFTING_PERIOD),
}

_BATTERY_STATUS_NAMES: dict[int, str] = {s.value: s.name for s in BatteryStatus}
_SHIFTING_COMPONENT_NAMES: dict[int, str] = {c.value: c.name for c in ShiftingSystemID}


def parse_device_type(value: str) -> tuple[int, int]:
    """Parse device type preset name or integer (decimal or 0x-prefixed hex) into (device_type, default_period)."""
    lower = value.strip().lower()
    if lower in DEVICE_TYPE_PRESETS:
        return DEVICE_TYPE_PRESETS[lower]
    try:
        device_type = int(lower, 0)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"Invalid device type '{value}'. Use 'shifting', 'power', 'any', or an integer (e.g. 34 or 0x22)."
        ) from e
    if not (0 <= device_type <= 0xFF):
        raise argparse.ArgumentTypeError(f"Device type must be between 0 and 255, got {device_type}")
    default_period = DEFAULT_POWER_METER_PERIOD if device_type == POWER_METER_DEVICE_TYPE else DEFAULT_SHIFTING_PERIOD
    return device_type, default_period


def parse_byte(value: str) -> int:
    """Parse a single byte from decimal or 0x-prefixed hex."""
    try:
        parsed = int(value.strip(), 0)
    except ValueError as e:
        raise ValueError(f"Invalid byte value '{value}'") from e
    if not (0 <= parsed <= 0xFF):
        raise ValueError(f"Byte value must be between 0 and 255, got {parsed}")
    return parsed


def parse_byte_list(value: str) -> list[int]:
    """Parse a comma-separated list of byte values (decimal or 0x-prefixed hex)."""
    tokens = [tok for tok in value.replace(" ", ",").split(",") if tok]
    if not tokens:
        raise ValueError("Byte list cannot be empty")
    return [parse_byte(tok) for tok in tokens]


def parse_hex_payload(value: str) -> list[int]:
    """Parse an 8-byte hex payload such as '46 FF FF 03 FF 04 52 01' or '0x46,0xFF,...'."""
    tokens = [tok for tok in value.replace(",", " ").split() if tok]
    if len(tokens) != STANDARD_PAYLOAD_LEN:
        raise ValueError(f"Payload must contain exactly {STANDARD_PAYLOAD_LEN} bytes, got {len(tokens)}: '{value}'")
    payload: list[int] = []
    for tok in tokens:
        cleaned = tok[2:] if tok.lower().startswith("0x") else tok
        try:
            b = int(cleaned, 16)
        except ValueError as e:
            raise ValueError(f"Invalid hex byte '{tok}' in payload '{value}'") from e
        if not (0 <= b <= 0xFF):
            raise ValueError(f"Byte out of range (0x00..0xFF): '{tok}'")
        payload.append(b)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log raw ANT+ broadcast packets for a target ANT+ ID to console and log file."
    )
    parser.add_argument(
        "--ant-id",
        type=int,
        default=None,
        help="Target ANT+ device ID (0 for wildcard). Defaults to antID from settings.json if available, else 0.",
    )
    parser.add_argument(
        "-s",
        "--settings",
        type=Path,
        default=None,
        help="Path to settings.json (used when --ant-id is omitted; defaults to ./settings.json)",
    )
    parser.add_argument(
        "--device-type",
        type=str,
        default="shifting",
        help="ANT+ device type: 'shifting' (34/0x22), 'power' (11/0x0B), 'any' (0), or integer (default: shifting)",
    )
    parser.add_argument(
        "--period",
        type=int,
        default=None,
        help="ANT+ channel period in 1/32768s ticks (default: 8192 for shifting, 8182 for power)",
    )
    parser.add_argument(
        "-o",
        "--log-file",
        type=Path,
        default=None,
        help="Path to output log file (default: ant_log_<ant_id>_<timestamp>.log)",
    )
    parser.add_argument(
        "--ignore-page1",
        action="store_true",
        help="Suppress output for Page 1 (Shift System Status) packets",
    )
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="Suppress consecutive packets with an identical 8-byte payload",
    )
    parser.add_argument(
        "--request-page",
        type=str,
        default=None,
        help="Send Page 70 (Request Data Page) for the given page number (e.g. 82 or 0x52) after first packet",
    )
    parser.add_argument(
        "--desc1",
        type=str,
        default="0xFF",
        help="Descriptor Byte 1 for --request-page; comma-separated for multiple requests (e.g. 0xFF,0x03,0x04,0x33,0x43)",
    )
    parser.add_argument(
        "--desc2",
        type=str,
        default="0xFF",
        help="Descriptor Byte 2 for --request-page (default: 0xFF)",
    )
    parser.add_argument(
        "--tx-resp",
        type=str,
        default="0x04",
        help="Requested Transmission Response byte (Byte 5) for --request-page (default: 0x04)",
    )
    parser.add_argument(
        "--cmd-type",
        type=str,
        default="0x01",
        help="Command Type byte (Byte 7) for --request-page (default: 0x01)",
    )
    parser.add_argument(
        "--send-hex",
        action="append",
        default=[],
        help="Send an explicit 8-byte hex payload after first packet (repeatable, e.g. '46 FF FF 03 FF 04 52 01')",
    )
    parser.add_argument(
        "--send-mode",
        choices=("ack", "broadcast"),
        default="ack",
        help="Transmission mode for --request-page / --send-hex (default: ack)",
    )
    parser.add_argument(
        "--send-interval",
        type=float,
        default=5.0,
        help="Seconds to wait between multiple TX payloads (default: 5.0)",
    )
    parser.add_argument(
        "--send-retries",
        type=int,
        default=3,
        help="Maximum retry attempts per payload in 'ack' mode (default: 3)",
    )
    parser.add_argument(
        "--background-scan",
        action="store_true",
        help="Open channel with ext_assign=0x01 (ANT Background Scanning; RX-only, disables ACK TX)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging including openant internals",
    )
    return parser.parse_args(argv)


def resolve_ant_id(ant_id_arg: int | None, settings_path: Path | None) -> int:
    """Resolve target ANT+ ID from CLI arg or settings.json."""
    if ant_id_arg is not None:
        if not (0 <= ant_id_arg <= 0xFFFF):
            raise ValueError(f"--ant-id must be between 0 and 65535, got {ant_id_arg}")
        return ant_id_arg

    candidate = settings_path if settings_path is not None else Path("settings.json")
    if candidate.exists():
        settings = load_settings(candidate)
        return settings.ant_id
    if settings_path is not None:
        raise FileNotFoundError(f"Settings file not found: {settings_path}")
    return 0


def parse_extended_header(data: list[int]) -> tuple[int, int, int] | None:
    """Extract (device_id, device_type, trans_type) from ANT extended message bytes if present."""
    if len(data) >= EXTENDED_HEADER_MIN_LEN:
        rx_device_id = data[9] | (data[10] << 8)
        rx_device_type = data[11]
        rx_trans_type = data[12]
        return rx_device_id, rx_device_type, rx_trans_type
    return None


def decode_page_summary(payload: list[int], device_type: int) -> str:
    """Return a concise human-readable summary for known ANT+ data pages."""
    if len(payload) < STANDARD_PAYLOAD_LEN:
        return f"Short payload ({len(payload)} bytes)"

    page = payload[0]

    # Shifting Profile pages (0x01 - 0x04)
    if device_type in (SHIFTING_DEVICE_TYPE, 0):
        if page == 0x01:
            event_count = payload[1]
            raw_rear = payload[3] & 0x1F
            raw_front = (payload[3] & 0xE0) >> 5
            total_rear = payload[4] & 0x1F
            total_front = (payload[4] & 0xE0) >> 5
            rear_str = "--" if raw_rear >= INVALID_GEAR_REAR else f"{raw_rear + 1}/{total_rear}"
            front_str = "--" if raw_front >= INVALID_GEAR_FRONT else f"{raw_front + 1}/{total_front}"
            return f"ShiftStatus event={event_count} front={front_str} rear={rear_str}"
        if page == 0x02:
            return f"ShiftFunctionEvents event={payload[1]}"
        if page == 0x03:
            return "ShiftFunctionConfig"
        if page == 0x04:
            return f"ShiftTrim event={payload[1]} rear={payload[4]}/{payload[2]} front={payload[5]}/{payload[3]}"

    # Power Meter pages
    if device_type in (POWER_METER_DEVICE_TYPE, 0) and page == 0x10:
        event_count = payload[1]
        cadence = payload[3]
        power = payload[6] | (payload[7] << 8)
        return f"StandardPower event={event_count} power={power}W cadence={cadence}rpm"

    # Common Data Pages
    if page == 70:  # 0x46 Request Data Page
        req_page = payload[6]
        return (
            f"RequestDataPage req_page={req_page}(0x{req_page:02X}) "
            f"desc1=0x{payload[3]:02X} desc2=0x{payload[4]:02X} "
            f"tx_resp=0x{payload[5]:02X} cmd=0x{payload[7]:02X}"
        )
    if page == 71:  # 0x47 Command Status
        return f"CommandStatus last_cmd=0x{payload[1]:02X} seq={payload[2]} status=0x{payload[3]:02X}"
    if page == 78:  # 0x4E Multi-Component System Manufacturer's Info
        comp_id = (payload[2] >> 4) & 0x0F
        num_comp = payload[2] & 0x0F
        hw_rev = payload[3]
        mfg_id = payload[4] | (payload[5] << 8)
        model_num = payload[6] | (payload[7] << 8)
        comp_name = _SHIFTING_COMPONENT_NAMES.get(comp_id, "Unknown")
        return f"MultiCompMfg comp={comp_id}({comp_name})/{num_comp} hw={hw_rev} mfg={mfg_id} model={model_num}"
    if page == 79:  # 0x4F Multi-Component System Product Info
        comp_id = (payload[1] >> 4) & 0x0F
        num_comp = payload[1] & 0x0F
        sw_supp = payload[2]
        sw_main = payload[3]
        serial = payload[4] | (payload[5] << 8) | (payload[6] << 16) | (payload[7] << 24)
        comp_name = _SHIFTING_COMPONENT_NAMES.get(comp_id, "Unknown")
        return f"MultiCompProduct comp={comp_id}({comp_name})/{num_comp} sw={sw_main}.{sw_supp} serial=0x{serial:08X}"
    if page == 80:  # 0x50 Manufacturer's Information
        hw_rev = payload[3]
        mfg_id = payload[4] | (payload[5] << 8)
        model_num = payload[6] | (payload[7] << 8)
        return f"MfgInfo hw={hw_rev} mfg={mfg_id} model={model_num}"
    if page == 81:  # 0x51 Product Information
        sw_supp = payload[2]
        sw_main = payload[3]
        serial = payload[4] | (payload[5] << 8) | (payload[6] << 16) | (payload[7] << 24)
        return f"ProductInfo sw={sw_main}.{sw_supp} serial=0x{serial:08X}"
    if page == 82:  # 0x52 Battery Status
        batt_ident = payload[2]
        num_batts = batt_ident & 0x0F
        batt_id = (batt_ident >> 4) & 0x0F
        op_time_raw = payload[3] | (payload[4] << 8) | (payload[5] << 16)
        frac_v = payload[6] / 256.0
        desc = payload[7]
        coarse_v = desc & 0x0F
        status_code = (desc >> 4) & 0x07
        resolution = 2 if (desc & 0x80) else 16
        op_time_s = op_time_raw * resolution
        status_name = _BATTERY_STATUS_NAMES.get(status_code, f"Unknown({status_code})")
        volt_str = "N/A" if coarse_v == 0x0F else f"{coarse_v + frac_v:.2f}V"
        if batt_ident == 0xFF:
            id_str = "single(0xFF)"
        else:
            comp_name = _SHIFTING_COMPONENT_NAMES.get(batt_id, "Unknown")
            id_str = f"id={batt_id}({comp_name})/{num_batts}"
        return f"BatteryStatus {id_str} status={status_name}({status_code}) voltage={volt_str} op_time={op_time_s}s"
    if page == 84:  # 0x54 Subfield Data
        return "SubfieldData"
    if 0xF0 <= page <= 0xFF:
        return f"ManufacturerSpecific(0x{page:02X})"

    return f"OtherPage({page}/0x{page:02X})"


def format_packet_line(
    data: list[int],
    kind: str,
    device_type: int,
    fallback_ant_id: int = 0,
) -> str:
    """Format a received ANT+ packet into a single readable log line."""
    payload = data[:STANDARD_PAYLOAD_LEN]
    page = payload[0] if payload else -1
    hex_payload = " ".join(f"{b:02X}" for b in payload)
    ext = parse_extended_header(data)
    if ext is not None:
        rx_id, rx_type, rx_tt = ext
        src = f"id={rx_id:05d} type=0x{rx_type:02X} tt=0x{rx_tt:02X}"
        effective_type = rx_type if rx_type != 0 else device_type
    else:
        src = f"id={fallback_ant_id:05d} type=0x{device_type:02X} tt=--"
        effective_type = device_type

    summary = decode_page_summary(payload, effective_type)
    extra = ""
    if len(data) > STANDARD_PAYLOAD_LEN and ext is None:
        extra_hex = " ".join(f"{b:02X}" for b in data[STANDARD_PAYLOAD_LEN:])
        extra = f" extra=[{extra_hex}]"
    return f"[{kind:<9}] {src} | page={page:3d} (0x{page:02X}) | {hex_payload}{extra} | {summary}"


class AntBroadcastLogger:
    """ANT+ receiver that logs all packets from a target device ID and optionally sends test TX payloads."""

    def __init__(
        self,
        ant_id: int,
        device_type: int = SHIFTING_DEVICE_TYPE,
        period: int = DEFAULT_SHIFTING_PERIOD,
        ignore_page1: bool = False,
        dedup: bool = False,
        background_scan: bool = False,
        tx_payloads: list[list[int]] | None = None,
        send_mode: str = "ack",
        send_interval: float = 5.0,
        send_retries: int = 3,
        packet_logger: logging.Logger | None = None,
    ):
        self.ant_id = ant_id
        self.device_type = device_type
        self.period = period
        self.ignore_page1 = ignore_page1
        self.dedup = dedup
        self.background_scan = background_scan
        self.tx_payloads: list[list[int]] = tx_payloads or []
        self.send_mode = send_mode
        self.send_interval = max(0.0, send_interval)
        self.send_retries = max(1, send_retries)
        self._packet_logger = packet_logger or _logger
        self._last_payload: tuple[int, ...] | None = None
        self._tx_started: bool = False
        self._stop_event = threading.Event()
        self._tx_thread: threading.Thread | None = None
        self.page_counts: Counter[int] = Counter()
        self.total_packets: int = 0
        self.logged_packets: int = 0
        self.node: Node | None = None
        self.channel: Any = None

    def send_payload(self, payload: list[int]) -> bool:
        """Send one 8-byte payload over the active channel with bounded retries in ACK mode."""
        channel = self.channel
        if channel is None:
            self._packet_logger.warning("[TX-FAIL  ] Channel is not open")
            return False

        hex_payload = " ".join(f"{b:02X}" for b in payload)
        summary = decode_page_summary(payload, self.device_type)

        if self.send_mode == "broadcast":
            self._packet_logger.info(f"[TX-BCAST ] {hex_payload} | {summary}")
            try:
                channel.send_broadcast_data(payload)
                self._packet_logger.info(f"[TX-OK    ] Broadcast sent: {hex_payload}")
                return True
            except Exception as e:
                self._packet_logger.warning(f"[TX-FAIL  ] Broadcast failed ({type(e).__name__}: {e}): {hex_payload}")
                return False

        # ACK mode: call low-level _ant.send_acknowledged_data + wait_for_event directly
        # to avoid openant.Channel.send_acknowledged_data's unbounded recursion on TransferFailedException.
        for attempt in range(1, self.send_retries + 1):
            if self._stop_event.is_set():
                return False
            self._packet_logger.info(f"[TX-ACK   ] attempt {attempt}/{self.send_retries} | {hex_payload} | {summary}")
            try:
                channel._ant.send_acknowledged_data(channel.id, payload)
                channel.wait_for_event([Message.Code.EVENT_TRANSFER_TX_COMPLETED])
                self._packet_logger.info(
                    f"[TX-OK    ] ACK received (attempt {attempt}/{self.send_retries}) | {hex_payload}"
                )
                return True
            except TransferFailedException as e:
                self._packet_logger.warning(
                    f"[TX-RETRY ] Transfer failed (attempt {attempt}/{self.send_retries}: {e}) | {hex_payload}"
                )
                if attempt < self.send_retries and self._stop_event.wait(timeout=0.5):
                    return False
            except Exception as e:
                self._packet_logger.warning(f"[TX-FAIL  ] ACK error ({type(e).__name__}: {e}) | {hex_payload}")
                return False

        self._packet_logger.warning(f"[TX-FAIL  ] ACK failed after {self.send_retries} attempts | {hex_payload}")
        return False

    def _start_tx_worker(self) -> None:
        if self._tx_started or not self.tx_payloads:
            return
        self._tx_started = True

        def _worker() -> None:
            self._packet_logger.info(
                f"[TX-PLAN  ] Starting TX sequence ({len(self.tx_payloads)} payload(s), "
                f"mode={self.send_mode}, interval={self.send_interval:.1f}s, retries={self.send_retries})"
            )
            for idx, payload in enumerate(self.tx_payloads):
                if self._stop_event.is_set():
                    break
                if idx > 0 and self._stop_event.wait(timeout=self.send_interval):
                    break
                self.send_payload(payload)
            self._packet_logger.info("[TX-PLAN  ] TX sequence finished; continuing to listen...")

        self._tx_thread = threading.Thread(target=_worker, name="AntLoggerTxWorker", daemon=True)
        self._tx_thread.start()

    def handle_packet(self, data: list[int], kind: str = "BROADCAST") -> bool:
        """Process and log one received packet. Returns True if logged."""
        if not data:
            return False

        ext = parse_extended_header(data)
        if self.ant_id != 0 and ext is not None:
            rx_id = ext[0]
            if rx_id != 0 and rx_id != self.ant_id:
                return False

        payload = data[:STANDARD_PAYLOAD_LEN]
        page = payload[0]
        self.total_packets += 1
        self.page_counts[page] += 1

        if self.tx_payloads and not self._tx_started:
            self._start_tx_worker()

        if self.ignore_page1 and page == 0x01:
            return False

        payload_key = tuple(payload)
        if self.dedup and payload_key == self._last_payload:
            return False
        self._last_payload = payload_key

        line = format_packet_line(data, kind=kind, device_type=self.device_type, fallback_ant_id=self.ant_id)
        self._packet_logger.info(line)
        self.logged_packets += 1
        return True

    def start(self) -> None:
        apply_usb_fixes()
        self._stop_event.clear()
        self.node = Node()
        self.node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)

        ext_assign = 0x01 if self.background_scan else None
        channel = self.node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE, 0x00, ext_assign)
        self.channel = channel
        channel.on_broadcast_data = lambda data: self.handle_packet(data, "BROADCAST")
        channel.on_burst_data = lambda data: self.handle_packet(data, "BURST")
        channel.on_acknowledge = lambda data: self.handle_packet(data, "ACK")
        channel.set_search_timeout(0xFF)
        channel.set_id(self.ant_id, self.device_type, 0)
        channel.enable_extended_messages(1)
        channel.set_period(self.period)
        channel.set_rf_freq(ANTPLUS_RF_FREQ)
        channel.open()

        self._packet_logger.info(
            f"Listening on ANT+ (ant_id={self.ant_id}, device_type=0x{self.device_type:02X}, "
            f"period={self.period}, ext_assign={ext_assign}, ignore_page1={self.ignore_page1}, "
            f"dedup={self.dedup}, tx_payloads={len(self.tx_payloads)})... Press Ctrl+C to stop."
        )
        self.node.start()

    def stop(self) -> None:
        self._stop_event.set()
        channel = self.channel
        node = self.node
        self.channel = None
        self.node = None

        close_channel_safely(node, channel)

        if node is not None:
            try:
                node.stop()
            except Exception as e:
                _logger.debug(f"Error stopping node: {e}")

        if self._tx_thread is not None and self._tx_thread.is_alive():
            self._tx_thread.join(timeout=1.0)

    def format_summary(self) -> str:
        if not self.page_counts:
            return "Summary: 0 packets received."
        breakdown = ", ".join(f"page {page}(0x{page:02X})={count}" for page, count in sorted(self.page_counts.items()))
        return f"Summary: total={self.total_packets}, logged={self.logged_packets} | {breakdown}"


def configure_logging(log_file: Path, debug: bool) -> logging.Logger:
    """Configure console + file logging and return the packet logger."""
    if log_file.parent != Path("."):
        log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=root_level, handlers=[console_handler, file_handler], force=True)

    packet_logger = logging.getLogger("ant_logger.packets")
    packet_logger.setLevel(logging.INFO)
    return packet_logger


def default_log_file_path(ant_id: int) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(f"ant_log_{ant_id}_{ts}.log")


def build_tx_payloads_from_args(args: argparse.Namespace) -> list[list[int]]:
    """Build the ordered list of 8-byte TX payloads from CLI options."""
    payloads: list[list[int]] = []
    if args.request_page is not None:
        req_page = parse_byte(args.request_page)
        desc1_list = parse_byte_list(args.desc1)
        desc2 = parse_byte(args.desc2)
        tx_resp = parse_byte(args.tx_resp)
        cmd_type = parse_byte(args.cmd_type)
        for desc1 in desc1_list:
            payloads.append(
                build_page70_payload(
                    requested_page=req_page,
                    desc1=desc1,
                    desc2=desc2,
                    tx_resp=tx_resp,
                    cmd_type=cmd_type,
                )
            )
    for raw_hex in args.send_hex:
        payloads.append(parse_hex_payload(raw_hex))
    return payloads


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        device_type, default_period = parse_device_type(args.device_type)
        ant_id = resolve_ant_id(args.ant_id, args.settings)
        tx_payloads = build_tx_payloads_from_args(args)
    except (OSError, json.JSONDecodeError, ValueError, argparse.ArgumentTypeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    period = args.period if args.period is not None else default_period
    log_file = args.log_file if args.log_file is not None else default_log_file_path(ant_id)
    packet_logger = configure_logging(log_file, debug=args.debug)
    packet_logger.info(f"Logging ANT+ packets to: {log_file.resolve()}")

    receiver = AntBroadcastLogger(
        ant_id=ant_id,
        device_type=device_type,
        period=period,
        ignore_page1=args.ignore_page1,
        dedup=args.dedup,
        background_scan=args.background_scan,
        tx_payloads=tx_payloads,
        send_mode=args.send_mode,
        send_interval=args.send_interval,
        send_retries=args.send_retries,
        packet_logger=packet_logger,
    )

    def _handle_sigint(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    prev_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        receiver.start()
    except KeyboardInterrupt:
        packet_logger.info("Stopping ANT+ logger...")
    except Exception as e:
        packet_logger.error(f"ANT+ logger error ({type(e).__name__}): {e}")
        return 1
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        receiver.stop()
        packet_logger.info(receiver.format_summary())

    return 0


if __name__ == "__main__":
    sys.exit(main())
