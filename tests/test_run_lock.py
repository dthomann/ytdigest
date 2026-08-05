import threading

import pytest

from ytdigest.run_lock import RunInProgressError, run_lock


def test_run_lock_exclusive(tmp_path):
    lock_path = tmp_path / ".run.lock"
    with run_lock(lock_path):
        with pytest.raises(RunInProgressError):
            with run_lock(lock_path):
                pass


def test_run_lock_released_after_exit(tmp_path):
    lock_path = tmp_path / ".run.lock"
    with run_lock(lock_path):
        pass
    with run_lock(lock_path):
        pass


def test_run_lock_blocks_concurrent_thread(tmp_path):
    lock_path = tmp_path / ".run.lock"
    got_error = threading.Event()

    def try_acquire():
        try:
            with run_lock(lock_path):
                pass
        except RunInProgressError:
            got_error.set()

    with run_lock(lock_path):
        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=1)
    assert got_error.is_set()
