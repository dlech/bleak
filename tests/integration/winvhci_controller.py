import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    if sys.platform != "win32":
        assert False, "This is only available on Windows"


import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from bumble.controller import Controller
from bumble.link import LocalLink
from bumble.transport.common import Transport
from winrt.windows.devices.bluetooth import BluetoothAdapter
from winvhci.bumble_compat import (
    WindowsCompatController,
    WindowsCompatLink,
    apply_dual_mode,
)
from winvhci.device import VhciStats
from winvhci.transport import open_winvhci_transport

from tests.integration import winvhci_diagnostics

# The address the virtual controller reports as its own. Windows connects as
# central using this as its public identity address, which is what
# WindowsCompatLink exists to route correctly.
#
# It is also how the adapter is identified below, serving the same purpose as
# the manufacturer ID the BlueZ equivalent matches on: it distinguishes our
# controller from any real Bluetooth hardware on the machine.
WINVHCI_CONTROLLER_ADDRESS = "F0:F1:F2:F3:F4:F5"

logger = logging.getLogger(__name__)


def _address_to_int(address: str) -> int:
    """Convert "AA:BB:CC:DD:EE:FF" to the integer WinRT reports."""
    return int(address.replace(":", ""), 16)


async def wait_for_adapter_to_go(address: str, timeout: float = 90.0) -> None:
    """Wait until no Bluetooth adapter reports our controller's address.

    Needed BEFORE creating a radio, and not optional. get_default_async keeps
    returning the previous radio's adapter until Windows has finished removing
    it, so a new adapter cannot become the default while the old one is still
    there. Skipping this and merely waiting for a different device id below
    does not work: it waits out the full timeout and then raises, which was
    measured at 56 errors per run against none with this in place.

    Timing out is an error. An earlier version logged a warning and started
    anyway, on the theory that a developer machine might have a real adapter
    and that refusing to run would be worse. That reasoning does not hold: the
    wait returns as soon as it sees any adapter whose address is not ours, so
    the only way to reach the timeout is for OUR previous radio to still be
    there. Starting in that state does not fail cleanly - it fails several
    tests later as ERROR_FILE_NOT_FOUND out of GetGattServicesAsync, or
    ERROR_NOT_FOUND out of get_radio_async, both of which read as Bleak bugs
    and cost a great deal of time to trace back to here.

    The timeout is ninety seconds because removal has a long tail, and the tail
    is Windows being patient rather than anything being broken. Three
    instrumented runs measured 16 waits each, with maxima of 34.2s, 34.2s and
    34.3s - a constant, not scatter. Ninety seconds is about two and a half
    times that: margin for a tail three runs could not see, without making a
    genuinely stuck radio take minutes to report itself.

    What produces 34s is not known, and two attractive explanations have been
    tested and failed:

    - The LE connection link supervision timeout, documented as capped at
      0x0C80 in 10ms units - 32 seconds - which matches 34s almost too well.
      But dropping the controller with a live LE link took 8.7s and 8.8s,
      against 11.3s and 2.7s after disconnecting first. No difference, and
      nothing near 34s, so the resemblance is coincidence.
    - Accumulated stale devnodes slowing PnP. Purging 1428 of them changed
      nothing.

    Microsoft documents no PnP removal timeout and nothing about BthPort's
    behavior when a radio vanishes, so 34s stands as a measurement without an
    explanation. There are well known reports of Windows dropping BLE links at
    around 30s - noble-uwp#40, esp32-snippets#1096 - but those concern
    CONNECTIONS, both threads are unresolved, and the test above rules that
    mechanism out here. Note also that no standalone reproduction has ever produced
    it: dozens of open/close cycles, with and without connections, all clear in
    under 12s. Only full suite runs reach 34s, so whatever causes it needs the
    sustained load and is not reachable by a small script.

    A virtual radio vanishes the moment its client closes the handle, which no
    real radio ever does; a physical adapter that stops answering has gone out
    of range or lost power, and the stack is built to retry that case rather
    than tear down at once. So it keeps working on a radio the driver has
    already marked missing, and PnP cannot finish the removal until it stops.
    Caught in the act at a 30s timeout: PnP still reported the devnode OK, WinRT
    still reported RadioState.ON, and bthserv still held a handle on
    \\Device\\BTHMS_RFCOMM - down from two handles to one, a stack partway
    through giving up. Two seconds after a fast teardown those handles are gone.

    The handle really is closed by then. The control device is exclusive and the
    next radio opens successfully moments later, which could not happen while
    the previous handle was open.

    Do not shorten this on the strength of a short measurement. Timing 27 idle
    open/close cycles and 12 with a full GATT connect gave a maximum of 8.2s,
    which made 30s look generous - but that sampled only the body of the
    distribution. Instrumented suite runs then hit 30s two or three times per
    run. The limit has to outlast the stack's retry period, not the median.
    """
    wanted = _address_to_int(address)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        adapter = await BluetoothAdapter.get_default_async()
        if (
            adapter is None  # pyright: ignore[reportUnnecessaryComparison]
            or adapter.bluetooth_address != wanted
        ):
            return
        # A cached entry for a radio that is already gone counts as gone.
        # Without this the wait burns its full timeout whenever Windows is slow
        # to forget, which is exactly when it is least helpful.
        if not await _adapter_is_live(adapter):
            return
        await asyncio.sleep(0.25)

    raise TimeoutError(
        f"a Bluetooth adapter at {address} was still present after "
        f"{timeout:.0f}s. That is the radio from the previous test, whose "
        f"handle is closed but whose devnode Windows has not finished "
        f"removing. Well past the observed tail, so treat it as stuck rather "
        f"than slow."
    )


async def _adapter_is_live(adapter: BluetoothAdapter) -> bool:
    """Whether an adapter actually backs a radio that still exists.

    get_default_async can return a CACHED adapter for a radio Windows has
    already removed, and it does not always catch up quickly - a stale entry
    was observed surviving longer than a 30 s wait. Nothing about the adapter
    object itself gives that away: the address and device id are exactly what
    they were when the radio was real.

    Using it is what gives it away, and this asks the same question Bleak's
    scanner asks a moment later:

        radio = await adapter.get_radio_async()
        OSError: [WinError -2147023728] Element not found.

    which is ERROR_NOT_FOUND for the radio behind the adapter. Better to find
    that here, while still waiting, than to have every scanner test fail at a
    line that looks like a Bleak problem.
    """
    try:
        radio = await adapter.get_radio_async()
    except OSError as error:
        logger.debug("adapter %s is stale: %s", adapter.device_id, error)
        return False
    return radio is not None  # pyright: ignore[reportUnnecessaryComparison]


async def wait_for_adapter(address: str, timeout: float = 30.0) -> BluetoothAdapter:
    """
    Wait for Windows to bring up a Bluetooth adapter for OUR radio.

    This is the counterpart of the BlueZ path's InterfacesAdded wait, and it
    polls rather than subscribing because there is no "adapter added" event on
    BluetoothAdapter; a DeviceWatcher would be a great deal more machinery for
    a fixture that runs once.

    Matching on address alone is sound ONLY because wait_for_adapter_to_go runs
    first and is now correct. That ordering is the whole safety argument, so do
    not reorder or skip it.

    This used to take an `exclude_device_id` and require a different device id,
    because an address-only match was satisfied instantly by the previous
    test's adapter and the fixture then ran against a radio being torn down -
    54 errors in a back-to-back run. That fix addressed the symptom. The cause
    was that the wait for the old radio to go gave up after 30s, in the middle
    of the Windows Bluetooth stack retrying a radio it thought had gone out of
    range, so the old adapter was frequently still there when this ran.

    With that wait given three minutes, the old adapter is genuinely gone
    before this starts, and there is nothing left for an address match to
    confuse it with. The identity check also no longer WORKS: winvhci now uses
    a constant PnP instance ID so that Windows reuses one devnode instead of
    creating a permanent new one per radio, which means consecutive radios
    share a device id and "wait for a different one" waits forever.

    Measured timings, for anyone tempted to tune this: a new adapter appears
    about 0.3 s after the radio is created, and the old one usually vanishes
    the instant the handle closes, though it can linger a few seconds.

    There is deliberately no power-on step. BlueZ needs one, and it doubles as
    the signal that the adapter is fully configured. Windows brings the radio up
    on its own as BthPort initializes.
    """
    wanted = _address_to_int(address)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_seen: int | None = None

    while loop.time() < deadline:
        adapter = await BluetoothAdapter.get_default_async()

        # The projection types this as non-optional and the documentation does
        # not say what happens when no adapter is present, so this guard is
        # belt-and-braces rather than something the API promises. It is worth
        # having: a machine with no Bluetooth at all is exactly the case this
        # has to fail cleanly on, and an AttributeError from inside a poll loop
        # would be a much worse way to find out.
        if adapter is None:  # pyright: ignore[reportUnnecessaryComparison]
            await asyncio.sleep(0.25)
            continue

        if adapter.bluetooth_address == wanted:
            if await _adapter_is_live(adapter):
                logger.info("adapter %s is up as %s", address, adapter.device_id)
                return adapter
            # A stale cache entry: get_default_async can return an adapter for
            # a radio that is already gone, and using it is the only way to
            # tell.
            await asyncio.sleep(0.25)
            continue

        # A real radio on a developer machine. Worth recording, because
        # otherwise every test fails against the wrong adapter for no visible
        # reason.
        last_seen = adapter.bluetooth_address
        await asyncio.sleep(0.25)

    if last_seen is not None:
        raise RuntimeError(
            f"the default Bluetooth adapter is "
            f"{last_seen:012X}, not the virtual controller's {wanted:012X}. "
            f"Another Bluetooth adapter is present and Windows prefers it."
        )
    raise RuntimeError(
        f"no new Bluetooth adapter appeared within {timeout}s. The winvhci "
        f"driver may not be installed, or Windows did not finish bringing the "
        f"radio up - see https://github.com/dlech/windows-vhci-driver"
    )


def read_stats(hci_transport: Transport) -> "VhciStats | None":
    """The driver's packet counters, or None if they cannot be read.

    `device` belongs to winvhci's own Transport subclass rather than to
    bumble's, so it is fetched defensively. And the CLIENT having a stats()
    method does not mean the installed DRIVER implements the IOCTL behind it:
    those are versioned separately - the client comes from the pinned git
    tag, the driver from a release the workflow installs. A 1.1.1 client
    against a 1.0.2 driver turned every teardown into an
    ERROR_INVALID_FUNCTION error, which is a far worse outcome than not
    knowing the drop count.
    """
    device = getattr(hci_transport, "device", None)
    stats = getattr(device, "stats", None)
    if stats is None:
        logger.info("winvhci client has no stats support")
        return None
    try:
        return stats()
    except Exception:
        logger.warning(
            "could not read winvhci stats. The installed driver is probably "
            "older than the winvhci client.",
            exc_info=True,
        )
        return None


def check_for_packet_loss(
    hci_transport: Transport, baseline: "VhciStats | None"
) -> None:
    """
    Fail if the driver lost a packet while the tests were running.

    Worth doing explicitly because loss here is otherwise invisible: a dropped
    advertising report is indistinguishable from a device that was simply not
    advertising, so it surfaces as a flaky discovery test somewhere else
    entirely rather than as itself. The driver counts what it discards, and
    reading the counters turns "the test is flaky" into "the driver dropped
    four packets".

    Read before the transport closes, since the counters live behind the device
    handle and closing it is what destroys the radio.
    """
    s = read_stats(hci_transport)
    if s is None or baseline is None:
        return
    logger.info(
        "winvhci: %d packets to the stack, %d to the client, peak depths %d/%d/%d",
        s.writes_total,
        s.queued_to_user_total,
        s.host_to_ctrl_peak,
        s.pending_event_peak,
        s.pending_data_peak,
    )

    # Only allocation failures are a defect. The driver documents the split:
    # "a client that vanished mid-flight is expected, a failed allocation is
    # not". DropsNoClient counts packets the Bluetooth stack produced after the
    # handle closed, which is exactly what every teardown here does - the radio
    # is destroyed by closing the device, and BthPort does not stop the instant
    # that happens.
    #
    # Asserting on it against zero was wrong twice over: it flags an expected
    # race as a defect, and the counters are cumulative for the life of the
    # device node, so every module also inherited every earlier module's
    # teardown. The result was a tidy 1, 2, 3, 4, 5 climb across modules and 16
    # spurious errors.
    lost = s.drops_alloc_failed - baseline.drops_alloc_failed
    vanished = s.drops_no_client - baseline.drops_no_client

    if vanished:
        logger.info(
            "winvhci discarded %d packet(s) with no client attached, which is "
            "the ordinary teardown race rather than loss during the tests",
            vanished,
        )

    if lost:
        raise AssertionError(
            f"the winvhci driver lost {lost} packet(s) to failed allocations. "
            f"Any discovery or notification failure in this run is suspect."
        )


@contextlib.asynccontextmanager
async def open_winvhci_bluetooth_controller_link() -> AsyncGenerator[LocalLink, None]:
    """
    Open a local link (virtual RF connection) to a bumble Bluetooth controller
    that is connected to the Windows Bluetooth stack through the winvhci driver.
    """
    # Wait for the previous test's radio to actually be gone before creating
    # ours. Every transport this suite opens gives its controller the same
    # address, so until the old one has gone there is no way to tell the two
    # apart - and this is the only step that establishes that, which is why it
    # comes first and why it is allowed three minutes.
    await wait_for_adapter_to_go(WINVHCI_CONTROLLER_ADDRESS)

    # Opening the device is what creates the radio, and closing it is what
    # destroys it - the radio's lifetime is this handle's lifetime. So the
    # transport being an async context manager is not incidental tidiness; it is
    # what guarantees a failed test cannot leave a radio behind for the next one.
    async with await open_winvhci_transport() as hci_transport:
        # WindowsCompatLink rather than LocalLink: bumble's LocalLink assumes an
        # LE packet's source is the sending controller's random address, which
        # does not hold for Windows connecting as central with its public
        # identity address, so LE ACL data would never arrive.
        link = WindowsCompatLink()

        # WindowsCompatController rather than Controller, for three reasons that
        # each show up as the Windows stack simply stopping mid-bring-up. See
        # winvhci.bumble_compat for the detail; briefly, bumble does not
        # implement the BR/EDR configuration commands Windows sends, and one
        # "Unknown HCI Command" reply makes BthPort restart initialization
        # forever.
        windows_controller = WindowsCompatController(
            "BLEAK-TEST-WINVHCI",
            host_source=hci_transport.source,
            host_sink=hci_transport.sink,
            link=link,
            public_address=WINVHCI_CONTROLLER_ADDRESS,
        )

        # Bumble's controller reports itself LE-only, and Windows stops dead
        # after Read_Local_Supported_Features when it sees that.
        apply_dual_mode(windows_controller)

        await wait_for_adapter(WINVHCI_CONTROLLER_ADDRESS)

        # Baseline AFTER bring-up: the counters are cumulative for the life
        # of the device node, so without this each module inherits every
        # earlier module's teardown.
        baseline = read_stats(hci_transport)

        # So a GATT failure deep inside Bleak can read the driver's counters.
        # Bleak knows nothing about the transport underneath it, and the most
        # useful counter at that moment is radios_alive: it says whether a
        # previous radio was still being removed when discovery failed.
        winvhci_diagnostics.set_current_transport(hci_transport)
        try:
            yield link
        finally:
            winvhci_diagnostics.set_current_transport(None)
            check_for_packet_loss(hci_transport, baseline)


@contextlib.asynccontextmanager
async def open_transport_with_winvhci() -> AsyncGenerator[Transport, None]:
    """
    Create a bumble HCI Transport connected to Windows via the winvhci driver
    and connect a Bluetooth controller for a peripheral device to it.
    """
    # Windows GATT discovery fails intermittently in ways Bleak's own retry
    # cannot see, and the cause is not yet known. This records the machine's
    # state when it happens; it changes no behavior, and only does work on a
    # failure. Installed here rather than in conftest so it exists only on the
    # winvhci path.
    winvhci_diagnostics.install()

    async with open_winvhci_bluetooth_controller_link() as local_link:
        peripheral_controller = Controller("BLEAK-TEST-PERIPHERAL", link=local_link)
        yield Transport(peripheral_controller, peripheral_controller)
