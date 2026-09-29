"""
Microsoft-defined Bluetooth HCI extension for the emulated Windows controller.

Every real Windows radio implements Microsoft's vendor HCI extension, whose
advertisement monitors let the stack track LE device presence in the
controller, independently of host scanning. The emulated controller had none
of it, so Windows took its software fallbacks. This adds the advertisement
monitoring subset (Read_Supported_Features, LE_Monitor_Advertisement v1/v2,
LE_Cancel_Monitor_Advertisement, LE_Set_Advertisement_Filter_Enable and the
LE_Monitor_Device_Event), following "Microsoft-defined Bluetooth HCI commands
and events" in the Windows driver docs.

Windows learns the vendor opcode from the radio devnode's ``VsMsftOpCode``
registry value; see :func:`vs_msft_opcode_registry_script`.

EXPERIMENT: enabled by BLEAK_WINVHCI_VARIANT=msft, to see whether the ~40s
Device Association Service hangs on CI depend on the missing extension.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import struct

from bumble import hci
from winvhci.bumble_compat import WindowsCompatController

logger = logging.getLogger(__name__)

#: Any opcode in the vendor OGF (0x3F) works; Windows reads it from the registry.
MSFT_OPCODE = 0xFC1E

#: Constant bytes at the start of every Microsoft-defined event (event code
#: 0xFF), reported by Read_Supported_Features so the stack can tell them apart.
EVENT_PREFIX = b"MS"

# Supported_features bits (see the spec's Read_Supported_Features table).
FEATURE_RSSI_MONITOR_LE_ADV = 0x04
FEATURE_ADV_MONITOR_LE_LEGACY = 0x08
FEATURE_ADV_MONITOR_CONCURRENT = 0x20
SUPPORTED_FEATURES = (
    FEATURE_RSSI_MONITOR_LE_ADV | FEATURE_ADV_MONITOR_LE_LEGACY | FEATURE_ADV_MONITOR_CONCURRENT
)

# Subcommand opcodes.
SUB_READ_SUPPORTED_FEATURES = 0x00
SUB_MONITOR_RSSI = 0x01
SUB_CANCEL_MONITOR_RSSI = 0x02
SUB_LE_MONITOR_ADVERTISEMENT = 0x03
SUB_LE_CANCEL_MONITOR_ADVERTISEMENT = 0x04
SUB_LE_SET_ADVERTISEMENT_FILTER_ENABLE = 0x05
SUB_LE_MONITOR_ADVERTISEMENT_V2 = 0x0F

# Microsoft event codes.
EVENT_LE_MONITOR_DEVICE = 0x02

# Condition types.
CONDITION_PATTERN = 0x01
CONDITION_UUID = 0x02
CONDITION_IRK = 0x03
CONDITION_ADDRESS = 0x04

# Monitor_options bits (v2).
OPTION_PEER_ADDRESS = 0x01
OPTION_PEER_IRK = 0x02
OPTION_DIRECTED_PEER_ADDRESS = 0x04
OPTION_DIRECTED_PEER_IRK = 0x08
OPTION_DIRECTED_ANY = 0x10
OPTION_ANY = 0x20

STATUS_SUCCESS = 0x00
STATUS_UNSUPPORTED_FEATURE = 0x11
STATUS_INVALID_PARAMETERS = 0x12
STATUS_COMMAND_DISALLOWED = 0x0C

#: Used when a monitor gives a reserved (0) RSSI_threshold_low_time_interval.
DEFAULT_LOST_INTERVAL = 4.0

# AD types carrying service UUID lists, by UUID size in bytes.
UUID_AD_TYPES = {2: (0x02, 0x03), 4: (0x04, 0x05), 16: (0x06, 0x07)}


@dataclasses.dataclass
class Monitor:
    handle: int
    condition_type: int
    patterns: list[tuple[int, int, bytes]]  # (ad_type, start, pattern)
    uuid: bytes
    address: tuple[int, bytes] | None  # (type, 6 bytes LE) from the condition
    options: int
    peer_address: tuple[int, bytes] | None  # from v2 Monitor_options bit 0
    sampling_period: int
    lost_interval: float


def ad_structures(data: bytes):
    """Yield (ad_type, value) for each AD structure in advertising data."""
    i = 0
    while i + 1 < len(data):
        length = data[i]
        if length == 0 or i + 1 + length > len(data):
            return
        yield data[i + 1], bytes(data[i + 2 : i + 1 + length])
        i += 1 + length


def vs_msft_opcode_registry_script(opcode: int = MSFT_OPCODE) -> str:
    """PowerShell that sets VsMsftOpCode on every winvhci radio devnode.

    The value lives in the devnode's hardware key ("Device Parameters"),
    which PnP creates the first time a radio is enumerated and keeps
    afterwards, since the driver reuses one instance ID. Prints how many
    devnodes were set, so a caller can tell when there is none yet.
    """
    return f"""
$n = 0
foreach ($d in Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\WINVHCI\\RADIO' -ErrorAction SilentlyContinue) {{
    $dp = Join-Path $d.PSPath 'Device Parameters'
    if (Test-Path $dp) {{ Set-ItemProperty $dp -Name VsMsftOpCode -Value {opcode} -Type DWord; $n++ }}
}}
$n
"""


class MsftExtensionMixin:
    """Mixin for a bumble Controller adding Microsoft's advertisement monitors.

    Put it before the controller class in the bases so that on_hci_command and
    on_advertising_pdu are seen first.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._msft_monitors: dict[int, Monitor] = {}
        self._msft_next_handle = 0
        self._msft_filter_enabled = False
        # (handle, address_type, address_bytes) -> lost timer
        self._msft_tracked: dict[tuple[int, int, bytes], asyncio.TimerHandle] = {}

    # ---- commands ---------------------------------------------------------

    def on_hci_command(self, command):
        if command.op_code != MSFT_OPCODE:
            return super().on_hci_command(command)
        params = bytes(command.parameters)
        sub = params[0] if params else None
        logger.info("MSFT command 0x%02X params=%s", sub if sub is not None else -1, params.hex())
        try:
            payload = self._msft_dispatch(sub, params[1:])
        except Exception:
            logger.exception("MSFT command 0x%s failed", params.hex())
            payload = bytes([STATUS_INVALID_PARAMETERS, sub or 0])
        logger.info("MSFT reply %s", payload.hex())
        # _command_complete sends a raw Command Complete for unmodelled commands
        # and returns None, which is what an async handler must return.
        return self._command_complete(command, payload)

    def _msft_dispatch(self, sub: int | None, p: bytes) -> bytes:
        if sub == SUB_READ_SUPPORTED_FEATURES:
            return (
                bytes([STATUS_SUCCESS, sub])
                + struct.pack("<Q", SUPPORTED_FEATURES)
                + bytes([len(EVENT_PREFIX)])
                + EVENT_PREFIX
            )
        if sub in (SUB_LE_MONITOR_ADVERTISEMENT, SUB_LE_MONITOR_ADVERTISEMENT_V2):
            return self._msft_monitor_advertisement(sub, p)
        if sub == SUB_LE_CANCEL_MONITOR_ADVERTISEMENT:
            handle = p[0]
            self._msft_monitors.pop(handle, None)
            for key in [k for k in self._msft_tracked if k[0] == handle]:
                self._msft_tracked.pop(key).cancel()
            logger.info("MSFT monitor %d cancelled", handle)
            return bytes([STATUS_SUCCESS, sub])
        if sub == SUB_LE_SET_ADVERTISEMENT_FILTER_ENABLE:
            # The spec allows rejecting a redundant enable/disable, but Windows
            # sends "disable" right after Read_Supported_Features on a machine
            # with no monitors yet, and a rejection there stopped it from
            # installing any monitor afterwards (seen on CI). Real controllers
            # evidently accept it; do the same.
            enable = bool(p[0])
            self._msft_filter_enabled = enable
            logger.info("MSFT advertisement filters %s", "enabled" if enable else "disabled")
            return bytes([STATUS_SUCCESS, sub])
        if sub in (SUB_MONITOR_RSSI, SUB_CANCEL_MONITOR_RSSI):
            return bytes([STATUS_UNSUPPORTED_FEATURE, sub])
        return bytes([STATUS_UNSUPPORTED_FEATURE, sub or 0])

    def _msft_monitor_advertisement(self, sub: int, p: bytes) -> bytes:
        # Common head: RSSI_threshold_high, RSSI_threshold_low,
        # RSSI_threshold_low_time_interval, RSSI_sampling_period.
        _high, _low, low_interval, sampling = p[0], p[1], p[2], p[3]
        i = 4
        options = OPTION_ANY
        peer_address = None
        if sub == SUB_LE_MONITOR_ADVERTISEMENT_V2:
            options = p[i]
            _report_options = p[i + 1]
            peer = (p[i + 8], bytes(p[i + 2 : i + 8]))
            irk = p[i + 9 : i + 25]
            i += 25
            if options & (OPTION_PEER_IRK | OPTION_DIRECTED_PEER_IRK):
                if irk == bytes(16):
                    return bytes([STATUS_INVALID_PARAMETERS, sub])
                # Address resolution is not emulated.
                return bytes([STATUS_UNSUPPORTED_FEATURE, sub])
            if options == 0:
                return bytes([STATUS_INVALID_PARAMETERS, sub])
            if options & (OPTION_PEER_ADDRESS | OPTION_DIRECTED_PEER_ADDRESS):
                peer_address = peer

        condition_type = p[i]
        i += 1
        patterns: list[tuple[int, int, bytes]] = []
        uuid = b""
        address = None
        if condition_type == CONDITION_PATTERN:
            count = p[i]
            i += 1
            for _ in range(count):
                length = p[i]
                ad_type, start = p[i + 1], p[i + 2]
                patterns.append((ad_type, start, bytes(p[i + 3 : i + 1 + length])))
                i += 1 + length
        elif condition_type == CONDITION_UUID:
            size = {1: 2, 2: 4, 3: 16}[p[i]]
            uuid = bytes(p[i + 1 : i + 1 + size])
        elif condition_type == CONDITION_ADDRESS:
            if options & (OPTION_PEER_ADDRESS | OPTION_PEER_IRK | OPTION_DIRECTED_PEER_ADDRESS | OPTION_DIRECTED_PEER_IRK):
                return bytes([STATUS_INVALID_PARAMETERS, sub])
            address = (p[i], bytes(p[i + 1 : i + 7]))
        elif condition_type == CONDITION_IRK:
            return bytes([STATUS_UNSUPPORTED_FEATURE, sub])
        else:
            return bytes([STATUS_INVALID_PARAMETERS, sub])

        handle = self._msft_next_handle
        self._msft_next_handle = (self._msft_next_handle + 1) & 0xFF
        self._msft_monitors[handle] = Monitor(
            handle=handle,
            condition_type=condition_type,
            patterns=patterns,
            uuid=uuid,
            address=address,
            options=options,
            peer_address=peer_address,
            sampling_period=sampling,
            lost_interval=float(low_interval) if low_interval else DEFAULT_LOST_INTERVAL,
        )
        logger.info(
            "MSFT monitor %d: condition %d patterns=%s uuid=%s address=%s options=0x%02X sampling=%d",
            handle, condition_type, patterns, uuid.hex(), address, options, sampling,
        )
        return bytes([STATUS_SUCCESS, sub, handle])

    # ---- matching ---------------------------------------------------------

    @staticmethod
    def _msft_address_of(pdu) -> tuple[int, bytes]:
        addr = pdu.advertiser_address
        # bumble Address: address_bytes are the 6 octets in HCI (little-endian) order.
        return (1 if addr.is_random else 0), bytes(addr.address_bytes)

    def _msft_matches(self, m: Monitor, pdu) -> bool:
        addr = self._msft_address_of(pdu)
        # Directed-only options never match: bumble's legacy PDUs carry no TargetA.
        if not (m.options & (OPTION_ANY | OPTION_PEER_ADDRESS)):
            return False
        if m.peer_address is not None and addr != m.peer_address:
            return False
        data = bytes(pdu.data)
        if m.condition_type == CONDITION_ADDRESS:
            return addr == m.address
        if m.condition_type == CONDITION_UUID:
            wanted = UUID_AD_TYPES[len(m.uuid)]
            for ad_type, value in ad_structures(data):
                if ad_type in wanted:
                    for j in range(0, len(value) - len(m.uuid) + 1, len(m.uuid)):
                        if value[j : j + len(m.uuid)] == m.uuid:
                            return True
            return False
        if m.condition_type == CONDITION_PATTERN:
            for ad_type, start, pattern in m.patterns:
                for t, value in ad_structures(data):
                    if t == ad_type and value[start : start + len(pattern)] == pattern:
                        return True
            return False
        return False

    def _msft_send_device_event(self, handle: int, addr: tuple[int, bytes], state: int) -> None:
        params = EVENT_PREFIX + bytes([EVENT_LE_MONITOR_DEVICE, addr[0]]) + addr[1] + bytes([handle, state])
        logger.info("MSFT Monitor_Device_Event handle=%d addr=%s state=%d", handle, addr[1][::-1].hex(":"), state)
        self.send_hci_packet(hci.HCI_Event(params, event_code=hci.HCI_VENDOR_EVENT))

    def _msft_on_match(self, m: Monitor, addr: tuple[int, bytes]) -> None:
        key = (m.handle, addr[0], addr[1])
        loop = asyncio.get_running_loop()
        timer = self._msft_tracked.pop(key, None)
        if timer is None:
            self._msft_send_device_event(m.handle, addr, 1)
        else:
            timer.cancel()
        self._msft_tracked[key] = loop.call_later(m.lost_interval, self._msft_on_lost, key)

    def _msft_on_lost(self, key: tuple[int, int, bytes]) -> None:
        if self._msft_tracked.pop(key, None) is not None and key[0] in self._msft_monitors:
            self._msft_send_device_event(key[0], (key[1], key[2]), 0)

    def on_advertising_pdu(self, pdu) -> None:
        matches = [m for m in self._msft_monitors.values() if self._msft_matches(m, pdu)]
        addr = self._msft_address_of(pdu)
        for m in matches:
            self._msft_on_match(m, addr)

        # With filters enabled only monitored advertisements reach the host,
        # and a monitor with sampling period 0xFF asks for none at all. The
        # base class reports only while le_scan_enable is set, so gate on it;
        # it still handles pending connections either way.
        if self._msft_filter_enabled:
            propagate = any(m.sampling_period != 0xFF for m in matches)
        else:
            propagate = True
        saved = self.le_scan_enable
        if not propagate:
            self.le_scan_enable = False
        try:
            super().on_advertising_pdu(pdu)
        finally:
            self.le_scan_enable = saved


class MsftWindowsCompatController(MsftExtensionMixin, WindowsCompatController):
    """WindowsCompatController with Microsoft's advertisement monitors."""
