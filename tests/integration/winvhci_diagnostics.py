"""Capture the machine's state when Windows GATT discovery fails.

Why this exists
---------------
``BleakClientWinRT._get_services`` fails intermittently on Windows in three
different ways, all from the same WinRT call:

    FileNotFoundError                 the async operation completed with ERROR
    bare AssertionError               it completed but get_results() returned None
    BleakCharacteristicNotFoundError  it succeeded and returned an incomplete tree

Bleak's own retry in ``_get_services`` cannot see any of them: it tests
``result.status == UNREACHABLE`` on a completed operation, while the first two
escape from inside ``FutureLike.result()`` before that check is reached.

The cause is not known. What is known is that it is far easier to reproduce in
CI than on a development machine - roughly two runs in five against one in
twenty - so the cheapest way to collect evidence is to have every CI run
contribute a sample rather than to sit and wait for one locally.

What it records
---------------
On each failure: the exception and traceback, the driver's own counters
(including ``radios_alive``, which says whether a previous radio is still being
removed), and what PnP reports about winvhci devices at that instant. Written
to a file so it survives the run and can be uploaded as an artifact.

Two things it deliberately does NOT do:

- It does not retry. Whether a retry would even succeed is untested, and
  measuring is the job here.
- It does not run anything expensive on the event loop. An earlier attempt
  called a subprocess synchronously from a failure path and blocked the loop
  long enough to break thirty unrelated tests. The PnP query goes through a
  thread.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    if sys.platform != "win32":
        assert False, "This is only available on Windows"


import asyncio
import datetime
import logging
import os
import subprocess
import traceback
from typing import Any

logger = logging.getLogger(__name__)

#: Where to write. The CI workflow uploads this path as an artifact.
DIAGNOSTICS_PATH = os.environ.get(
    "WINVHCI_GATT_DIAGNOSTICS", "winvhci-gatt-diagnostics.log"
)

#: The transport currently in use, so a failure can reach the driver's
#: counters. GATT discovery happens deep inside Bleak, which knows nothing
#: about the transport underneath it, so the link fixture parks it here.
_current_transport: Any = None

_installed = False

#: How many times per run the PnP state may be queried. Bounded because the
#: query costs about a second and a failure is not always a test failure.
MAX_PNP_QUERIES = 3
_pnp_queries = 0


def set_current_transport(transport: Any) -> None:
    """Record the transport a failure should read counters from."""
    global _current_transport
    _current_transport = transport


def _pnp_state() -> str:
    """What PnP says about winvhci devices. Runs in a thread, never inline."""
    script = (
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -like 'WINVHCI*' } | "
        "ForEach-Object { $_.Status + ' ' + $_.InstanceId }"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as error:  # noqa: BLE001 - diagnostics must not raise
        return f"<pnp query failed: {error}>"
    out = (done.stdout or "").strip()
    return out.replace("\n", " | ") if out else "<no winvhci device present>"


def _driver_stats() -> str:
    stats = getattr(_current_transport, "device", None)
    read = getattr(stats, "stats", None)
    if read is None:
        return "<no transport parked, or client has no stats support>"
    try:
        s = read()
    except Exception as error:  # noqa: BLE001
        return f"<stats read failed: {error}>"
    return (
        f"radios_alive={s.radios_alive} writes={s.writes_total} "
        f"to_client={s.queued_to_user_total} drops={s.total_drops} "
        f"no_radio={s.writes_no_radio}"
    )


async def _record(test_id: str, error: BaseException) -> None:
    global _pnp_queries

    stamp = datetime.datetime.now().isoformat(timespec="milliseconds")

    # The driver counters come from an IOCTL and cost microseconds, so they are
    # always read. The PnP query shells out to PowerShell and costs about a
    # second, and that is only safe a bounded number of times.
    #
    # Bleak's connect path has a branch that deliberately swallows a
    # _get_services failure - it awaits a cancelled task and ignores OSError -
    # so a failure here is not always a test failure. Spending a second in a
    # path Bleak meant to discard silently would perturb the very timing under
    # investigation, which is a mistake already made twice on this problem.
    stats = await asyncio.to_thread(_driver_stats)

    if _pnp_queries < MAX_PNP_QUERIES:
        _pnp_queries += 1
        pnp = await asyncio.to_thread(_pnp_state)
    else:
        pnp = f"<skipped, already queried {MAX_PNP_QUERIES} times this run>"

    with open(DIAGNOSTICS_PATH, "a", encoding="utf-8") as handle:
        handle.write(
            f"\n===== {stamp} {test_id} =====\n"
            f"  {type(error).__name__}: {error}\n"
            f"  driver: {stats}\n"
            f"  pnp:    {pnp}\n"
            f"{''.join(traceback.format_exception(error))}"
        )
        handle.flush()


def install() -> None:
    """Wrap ``_get_services`` so failures are recorded. Idempotent."""
    global _installed
    if _installed:
        return

    try:
        from bleak.backends.winrt.client import BleakClientWinRT
    except Exception:  # noqa: BLE001 - not Windows, nothing to instrument
        return

    # Reaching into a private method on purpose. There is no hook for this, and
    # the alternative - wrapping every call site in the test suite - would miss
    # the ones inside Bleak's own connect path, which is where the failures
    # happen. getattr rather than attribute access so the private use does not
    # need a suppression comment that black keeps moving off the offending line.
    original: Any = getattr(BleakClientWinRT, "_get_services")

    async def recording_get_services(self, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return await original(self, **kwargs)
        except Exception as error:  # noqa: BLE001 - the exception IS the data
            # Broad on purpose: the three known symptoms share no base class,
            # and narrowing risks missing a fourth before the set is known.
            try:
                await _record(
                    os.environ.get("PYTEST_CURRENT_TEST", "<unknown test>"), error
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record GATT diagnostics")
            raise

    BleakClientWinRT._get_services = recording_get_services  # type: ignore[method-assign]
    _installed = True
    logger.info("winvhci GATT diagnostics active, writing %s", DIAGNOSTICS_PATH)
