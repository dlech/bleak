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
from winvhci.transport import open_winvhci_transport

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


async def wait_for_adapter_to_go(address: str, timeout: float = 30.0) -> None:
    """Wait until no Bluetooth adapter reports our controller's address.

    Needed BEFORE creating a radio, and not optional. get_default_async keeps
    returning the previous radio's adapter until Windows has finished removing
    it, so a new adapter cannot become the default while the old one is still
    there. Skipping this and merely waiting for a different device id below
    does not work: it waits out the full timeout and then raises, which was
    measured at 56 errors per run against none with this in place.

    Not an error if the wait times out. A developer machine may have a real
    adapter, and refusing to run at all would be worse than running with a
    warning - the tests themselves will say soon enough.
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

    logger.warning(
        "an adapter at %s is still present after %.0fs; starting anyway",
        address,
        timeout,
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


async def current_adapter_id(address: str) -> str | None:
    """The device id of the adapter currently at `address`, if any.

    Captured before a new radio is created, so :func:`wait_for_adapter` can
    tell the new one from whatever was there before.
    """
    adapter = await BluetoothAdapter.get_default_async()
    if adapter is None:  # pyright: ignore[reportUnnecessaryComparison]
        return None
    if adapter.bluetooth_address != _address_to_int(address):
        return None
    return adapter.device_id


async def wait_for_adapter(
    address: str, exclude_device_id: str | None = None, timeout: float = 30.0
) -> BluetoothAdapter:
    """
    Wait for Windows to bring up a Bluetooth adapter for OUR radio.

    This is the counterpart of the BlueZ path's InterfacesAdded wait, and it
    polls rather than subscribing because there is no "adapter added" event on
    BluetoothAdapter; a DeviceWatcher would be a great deal more machinery for
    a fixture that runs once.

    `exclude_device_id` is what makes it correct across repeated use, and
    matching on address alone was a real bug rather than a theoretical one.
    Every transport this suite opens gives its controller the same address, so
    a stale adapter from the previous test satisfied an address-only match
    instantly - and the fixture then ran against a radio that was being torn
    down. One module alone always passed; the whole suite, run twice back to
    back with no changes, gave 54 errors and then none.

    The device id is what identifies a radio instance, and it does change:

        \\\\?\\winvhci#radio#1&79f5d87&1a&97#{92383b0e-...}
        \\\\?\\winvhci#radio#1&79f5d87&1a&98#{92383b0e-...}

    This works WITH wait_for_adapter_to_go and does not replace it, which is
    worth stating because replacing it was tried and was much worse - 56 errors
    a run against none. A new adapter cannot become the default while the
    previous one is still present, so excluding the old id without first
    waiting for it to go just waits out the timeout and raises.

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
            if adapter.device_id != exclude_device_id and await _adapter_is_live(
                adapter
            ):
                logger.info("adapter %s is up as %s", address, adapter.device_id)
                return adapter
            # Either the one that was already here, or a stale cache entry.
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


def read_stats(hci_transport: Transport):
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


def check_for_packet_loss(hci_transport: Transport, baseline) -> None:
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
    # Which adapter, if any, is already here - captured BEFORE the radio is
    # created, so the wait below can tell the new one from a leftover.
    #
    # Every transport this suite opens gives its controller the same address,
    # and closing a handle destroys its radio without Windows finishing with it
    # immediately. So an address-only match could be satisfied by the previous
    # test's adapter, and the fixture would run against a radio being torn
    # down.
    previous_adapter_id = await current_adapter_id(WINVHCI_CONTROLLER_ADDRESS)

    # And wait for it to actually go. A new adapter cannot become the
    # default while the previous one is still present, so the identity check
    # below cannot stand on its own - measured, at 56 errors a run.
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

        await wait_for_adapter(
            WINVHCI_CONTROLLER_ADDRESS, exclude_device_id=previous_adapter_id
        )

        # Baseline AFTER bring-up: the counters are cumulative for the life
        # of the device node, so without this each module inherits every
        # earlier module's teardown.
        baseline = read_stats(hci_transport)
        try:
            yield link
        finally:
            check_for_packet_loss(hci_transport, baseline)


@contextlib.asynccontextmanager
async def open_transport_with_winvhci() -> AsyncGenerator[Transport, None]:
    """
    Create a bumble HCI Transport connected to Windows via the winvhci driver
    and connect a Bluetooth controller for a peripheral device to it.
    """
    async with open_winvhci_bluetooth_controller_link() as local_link:
        peripheral_controller = Controller("BLEAK-TEST-PERIPHERAL", link=local_link)
        yield Transport(peripheral_controller, peripheral_controller)
