"""File lock to prevent concurrent pipeline runs (CLI, systemd timer, web UI)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


class RunInProgressError(Exception):
    pass


@contextmanager
def run_lock(lock_path: Path):
    """Exclusive non-blocking lock. Raises RunInProgressError if already held."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunInProgressError("A run is already in progress") from exc
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
