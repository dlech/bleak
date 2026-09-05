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


@contextlib.asynccontextmanager
async def open_winvhci_bluetooth_controller_link() -> AsyncGenerator[LocalLink, None]:
    """
    Open a local link (virtual RF connection) to a bumble Bluetooth controller
    that is connected to the Windows Bluetooth stack through the winvhci driver.
    """
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

        yield link


@contextlib.asynccontextmanager
async def open_transport_with_winvhci() -> AsyncGenerator[Transport, None]:
    """
    Create a bumble HCI Transport connected to Windows via the winvhci driver
    and connect a Bluetooth controller for a peripheral device to it.
    """
    async with open_winvhci_bluetooth_controller_link() as local_link:
        peripheral_controller = Controller("BLEAK-TEST-PERIPHERAL", link=local_link)
        yield Transport(peripheral_controller, peripheral_controller)
