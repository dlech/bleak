import contextlib
import functools
import sys
import threading
from collections.abc import Callable
from typing import AsyncGenerator, ParamSpec, TypeVar

import pytest
from bumble import data_types
from bumble.core import AdvertisingData, DataType
from bumble.device import Device, DeviceConfiguration
from bumble.gatt import Service
from bumble.hci import Address
from bumble.transport import open_transport
from bumble.transport.common import Transport

from bleak import BleakScanner
from bleak.backends import _utils
from bleak.backends.device import BLEDevice


@pytest.fixture
async def hci_transport(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[Transport, None]:
    """Create a bumble HCI Transport."""
    async with create_hci_transport(request) as hci_transport:
        yield hci_transport


@contextlib.asynccontextmanager
async def create_hci_transport(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[Transport, None]:
    """Create a bumble HCI Transport."""
    hci_transport_name: str | None = request.config.getoption("--bleak-hci-transport")
    # --bleak-bluez-vhci is the old, Linux-only spelling of --bleak-vhci. Both
    # mean the same thing now: give the OS's own Bluetooth stack an adapter
    # through whatever virtual HCI mechanism that OS provides.
    vhci_enabled: bool = bool(
        request.config.getoption("--bleak-vhci")
        or request.config.getoption("--bleak-bluez-vhci")
    )

    if hci_transport_name is not None and vhci_enabled:
        raise pytest.UsageError(
            "Cannot use --bleak-hci-transport and --bleak-vhci together"
        )
    elif vhci_enabled:
        # Imported inside the branch: each implementation depends on packages
        # that only install on its own platform - dbus-fast on Linux, winrt and
        # winvhci on Windows.
        #
        # Each branch does its own `async with`, rather than binding a common
        # alias and using it once below. The tidier-looking version does not
        # type check on a THIRD platform: mypy narrows sys.platform, so on macOS
        # both imports are unreachable and the alias is never bound.
        if sys.platform == "linux":
            from tests.integration.bluez_controller import (
                open_transport_with_bluez_vhci,
            )

            async with open_transport_with_bluez_vhci() as hci_transport:
                yield hci_transport
        elif sys.platform == "win32":
            from tests.integration.winvhci_controller import open_transport_with_winvhci

            async with open_transport_with_winvhci() as hci_transport:
                yield hci_transport
        else:
            pytest.skip(f"--bleak-vhci is not supported on {sys.platform}")
    elif hci_transport_name is not None:
        async with await open_transport(hci_transport_name) as hci_transport:
            yield hci_transport
    else:
        pytest.skip(
            "No HCI transport provided (use --bleak-hci-transport or --bleak-vhci)"
        )


@pytest.fixture
async def bumble_peripheral(hci_transport: Transport) -> Device:
    """Create a BLE peripheral device with bumble."""
    return create_bumble_peripheral(hci_transport)


def generate_peripheral_address() -> Address:
    """A fresh address for a test peripheral.

    UNIQUE per peripheral, deliberately: the services differ between test
    modules and between runs, and reusing an address invites the OS to answer
    from a stale GATT cache. That is why this is generated rather than fixed.

    PUBLIC rather than random static, and that part is Windows-specific. The
    symptoms it addresses were discovery returning nothing for a peer that was
    advertising, and connecting by address failing with

        FileNotFoundError: [WinError -2147024894] The system cannot find the
        file specified

    which is Windows saying it has no record of the device rather than
    anything about a file. The theory is that Windows keys its record of a
    device on address *and* address type and does not retain a random one the
    way it retains a public one, so a peer that only ever appears at a random
    address stays unknown to it. BlueZ shows neither symptom, which is what
    makes an OS-level difference the likely explanation rather than anything
    in the peripheral or the driver.

    Calling that a theory rather than a fact is deliberate: it is inferred
    from the symptoms and from BlueZ being unaffected, not from Windows
    documentation saying so.

    A simulated peripheral claiming a public address it does not own is fine:
    nothing here is on air, and the address is thrown away with the test.
    """
    return Address(
        str(Address.generate_static_address()), Address.PUBLIC_DEVICE_ADDRESS
    )


def create_bumble_peripheral(hci_transport: Transport) -> Device:
    """Create a BLE peripheral device with bumble."""
    config = DeviceConfiguration(
        name="Bleak",
        address=generate_peripheral_address(),
        advertising_interval_min=200,
        advertising_interval_max=200,
    )
    return Device.from_config_with_hci(
        config,
        hci_transport.source,
        hci_transport.sink,
    )


def add_default_advertising_data(
    bumble_peripheral: Device,
    additional_adv_data: list[DataType] | None = None,
) -> None:
    """Add default advertising data to bumble peripheral."""
    adv_data: list[DataType] = [
        data_types.Flags(
            AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
            | AdvertisingData.Flags.BR_EDR_NOT_SUPPORTED
        ),
        data_types.CompleteLocalName(bumble_peripheral.name),
    ]
    if additional_adv_data:
        adv_data.extend(additional_adv_data)
    bumble_peripheral.advertising_data = bytes(AdvertisingData(adv_data))


async def configure_and_power_on_bumble_peripheral(
    bumble_peripheral: Device,
    additional_adv_data: list[DataType] | None = None,
    services: list[Service] | None = None,
) -> None:
    """Configure and power on the bumble peripheral."""
    add_default_advertising_data(bumble_peripheral, additional_adv_data)
    if services:
        bumble_peripheral.add_services(services)
    await bumble_peripheral.power_on()
    await bumble_peripheral.start_advertising()


#: How many times :func:`find_ble_device` restarts the scan before giving up.
#:
#: One scan is not evidence of absence. Windows in particular can answer a
#: discovery request out of a cache that a background scanner fills on its own
#: schedule, returning nothing while the peer is advertising perfectly well -
#: which is why bleak's own examples restart their scans periodically. A single
#: attempt made every integration test on Windows fail at setup with "failed to
#: discover device, is Bumble working?", and Bumble was working.
#:
#: Restarting is not the same as scanning for longer: the point is to make the
#: backend begin a fresh scan, not to wait longer on one that may never have
#: really started.
FIND_DEVICE_ATTEMPTS = 3


async def find_ble_device(bumble_peripheral: Device) -> BLEDevice:
    """Find the BLE device corresponding to the bumble peripheral."""
    for attempt in range(1, FIND_DEVICE_ATTEMPTS + 1):
        device = await BleakScanner.find_device_by_name(bumble_peripheral.name)
        if device is not None:
            return device

        if attempt < FIND_DEVICE_ATTEMPTS:
            print(
                f"scan {attempt}/{FIND_DEVICE_ATTEMPTS} did not find "
                f"{bumble_peripheral.name!r}; restarting the scan",
                flush=True,
            )

    raise RuntimeError(
        f"failed to discover device in {FIND_DEVICE_ATTEMPTS} scans, is Bumble working?"
    )


_P = ParamSpec("_P")
_TReturn = TypeVar("_TReturn")


def enable_coverage(func: Callable[_P, _TReturn]) -> Callable[_P, _TReturn]:
    """
    Enable coverage tracing on a non-Python-created thread, if not already enabled.
    (https://github.com/nedbat/coveragepy/issues/686)
    """

    @functools.wraps(func)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _TReturn:
        if sys.gettrace() is None and (trace_hook := threading.gettrace()):
            sys.settrace(trace_hook)
        return func(*args, **kwargs)

    return wrapped


# Patch external thread callbacks to enable coverage tracing.
# This must be done at module import time, before any backend code is imported,
# to ensure the patch is in place when callbacks are created.
# Callbacks from external (non-Python) threads don't have coverage tracing enabled
# by default. This patch wraps such callbacks (e.g., CoreBluetooth dispatch queue
# threads, WinRT callback threads) to enable coverage tracing.
_utils.external_thread_callback = enable_coverage
