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


async def wait_for_adapter(address: str, timeout: float = 15.0) -> BluetoothAdapter:
    """
    Wait for Windows to bring up a Bluetooth adapter for our controller.

    This is the counterpart of the BlueZ path's InterfacesAdded wait, and it
    polls rather than subscribing because there is no "adapter added" event on
    BluetoothAdapter; a DeviceWatcher would be a great deal more machinery for
    a fixture that runs once.

    There is deliberately no power-on step. BlueZ needs one, and it doubles as
    the signal that the adapter is fully configured. Windows brings the radio up
    on its own as BthPort initialises, so what has to be waited for is only that
    it finished - and get_default_async() returning our address is that.
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
            logger.info("adapter %s is up", address)
            return adapter

        # A real radio on a developer machine, or a stale one. Worth recording,
        # because otherwise every test fails against the wrong adapter for no
        # visible reason.
        last_seen = adapter.bluetooth_address
        await asyncio.sleep(0.25)

    if last_seen is not None:
        raise RuntimeError(
            f"the default Bluetooth adapter is "
            f"{last_seen:012X}, not the virtual controller's {wanted:012X}. "
            f"Another Bluetooth adapter is present and Windows prefers it."
        )
    raise RuntimeError(
        f"no Bluetooth adapter appeared within {timeout}s. The winvhci driver "
        f"may not be installed, or Windows did not finish bringing the radio up "
        f"- see https://github.com/dlech/windows-vhci-driver"
    )


async def wait_for_adapter_to_go(address: str, timeout: float = 30.0) -> None:
    """Wait until no Bluetooth adapter reports our controller's address.

    The counterpart of :func:`wait_for_adapter`, and needed for the same reason
    that function cannot stand alone: it matches on address, and every module
    in this suite uses the same one, so it cannot tell a freshly created radio
    from the previous module's radio still being removed.

    Not an error if the wait times out. A developer machine could have a real
    adapter, and refusing to run at all would be a worse outcome than running
    with a warning - the tests themselves will say soon enough.
    """
    wanted = _address_to_int(address)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        adapter = await BluetoothAdapter.get_default_async()
        if (
            adapter is None or adapter.bluetooth_address != wanted
        ):  # pyright: ignore[reportUnnecessaryComparison]
            return
        await asyncio.sleep(0.25)

    logger.warning(
        "an adapter at %s is still present after %.0fs; starting anyway",
        address,
        timeout,
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
    # Wait for the PREVIOUS module's radio to finish going away before asking
    # for a new one.
    #
    # Closing the device handle destroys the radio, but Windows does not finish
    # with it immediately: removing the node unloads BthPort's whole stack above
    # it, which takes several seconds. Every module in this suite opens its own
    # transport, so without this a module can start while the last one's radio
    # is still being torn down.
    #
    # wait_for_adapter cannot catch that on its own, and this is the part worth
    # spelling out: it matches on the controller's address, and every module
    # uses the SAME address. A stale adapter therefore satisfies it instantly,
    # so the fixture proceeds against a radio that is on its way out, and the
    # tests fail in whatever way that particular moment happens to produce.
    # Running one module alone always passed; running the suite gave anything
    # from 0 to 54 errors on identical invocations.
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
        # "Unknown HCI Command" reply makes BthPort restart initialisation
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
