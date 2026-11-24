"""Cross-platform advisory file locks (stdlib only).

The ledger writes one file per task. Two agents racing on the same task file would
otherwise lose an update (read-modify-write with no lock). We serialise those
critical sections with an OS advisory lock on a sidecar file:

  POSIX  -> ``fcntl.flock``
  Windows -> ``msvcrt.locking``

Both are advisory, which is fine: the only contenders are our own processes. Locks
live under ``.orchestra/locks/`` (never in ``ledger/``) so they cannot be committed
and cannot show up in a diff. See docs/03 section 3 and ADR 0004.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_TIMEOUT = 15.0
DEFAULT_INTERVAL = 0.02


class LockTimeout(RuntimeError):
    """Raised when a lock could not be taken within the timeout."""


if os.name == "nt":  # pragma: no cover - exercised on Windows CI, see tests
    import msvcrt

    def _try_lock(handle) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def advisory_lock(path: Path, timeout: float = DEFAULT_TIMEOUT, interval: float = DEFAULT_INTERVAL) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the duration of the block."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not lock {path} within {timeout}s")
            time.sleep(interval)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()