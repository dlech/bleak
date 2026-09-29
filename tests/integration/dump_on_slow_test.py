"""
Debug aid, Windows only: minidump the Device Association Service, dasHost.exe,
the Bluetooth Support Service and this process when a test runs too long.

The flaky winvhci failures are one WinRT call hanging ~40s inside DAS's
BluetoothLE provider (System event 3503, no HCI traffic meanwhile). ETW shows
where the hang is but not what it waits on; thread stacks do. Enabled by
setting BLEAK_DUMP_ON_SLOW_TEST to a directory; the threshold is
BLEAK_DUMP_THRESHOLD seconds (default 15).
"""

import csv
import io
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import pytest

logger = logging.getLogger(__name__)

DUMP_DIR = os.environ.get("BLEAK_DUMP_ON_SLOW_TEST")
THRESHOLD = float(os.environ.get("BLEAK_DUMP_THRESHOLD", "15"))
ENABLED = bool(DUMP_DIR) and sys.platform == "win32"

_dumped_tests: set[str] = set()
_dump_threads: list[threading.Thread] = []


def _tasklist(*args: str) -> list[dict[str, str]]:
    out = subprocess.run(
        ["tasklist", "/fo", "csv", *args], capture_output=True, text=True, timeout=30
    ).stdout
    return list(csv.DictReader(io.StringIO(out)))


def _service_pid(name: str) -> int | None:
    # tasklist /svc lists the services hosted by each process.
    for row in _tasklist("/svc", "/fi", f"SERVICES eq {name}"):
        pid = row.get("PID", "")
        if pid.isdigit():
            return int(pid)
    return None


def _process_pids(image: str) -> list[int]:
    return [
        int(row["PID"])
        for row in _tasklist("/fi", f"IMAGENAME eq {image}")
        if row.get("PID", "").isdigit()
    ]


def _minidump(pid: int, path: str) -> None:
    # comsvcs.dll's MiniDump export: no extra tooling needed on a runner. It
    # writes nothing without the "full" argument.
    args = [
        "rundll32.exe",
        r"C:\Windows\System32\comsvcs.dll,MiniDump",
        str(pid),
        path,
        "full",
    ]
    subprocess.run(args, timeout=180, check=False)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    logger.warning("dumped pid %d to %s (%d bytes)", pid, path, size)


def _dump_all(nodeid: str) -> None:
    assert DUMP_DIR
    os.makedirs(DUMP_DIR, exist_ok=True)
    tag = f"{time.strftime('%H%M%S')}-" + "".join(
        c if c.isalnum() else "_" for c in nodeid
    )[-60:]
    logger.warning("test %s exceeded %.0fs; dumping processes", nodeid, THRESHOLD)
    targets: list[tuple[str, int | None]] = [
        ("das", _service_pid("DeviceAssociationService")),
        ("bthserv", _service_pid("bthserv")),
        ("python", os.getpid()),
    ]
    targets += [("dashost", pid) for pid in _process_pids("dasHost.exe")]
    for label, pid in targets:
        if pid is None:
            logger.warning("no pid for %s", label)
            continue
        try:
            _minidump(pid, os.path.join(DUMP_DIR, f"{tag}-{label}-{pid}.dmp"))
        except Exception:
            logger.exception("dumping %s (pid %d) failed", label, pid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Iterator[None]:
    if not ENABLED or item.nodeid in _dumped_tests:
        yield
        return

    def on_timeout() -> None:
        _dumped_tests.add(item.nodeid)
        _dump_all(item.nodeid)

    # Covers setup and teardown too: module fixtures connect as well.
    timer = threading.Timer(THRESHOLD, on_timeout)
    timer.daemon = True
    _dump_threads.append(timer)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def pytest_sessionfinish() -> None:
    # A dump in flight when the run ends must be allowed to finish; a daemon
    # thread would die with the interpreter.
    for t in _dump_threads:
        if t.is_alive():
            t.join(timeout=300)
