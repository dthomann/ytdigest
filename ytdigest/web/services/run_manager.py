"""Background run management for the web UI."""
from __future__ import annotations

import logging
import threading

from ... import db, pipeline
from ...config import Config
from ...run_lock import RunInProgressError

logger = logging.getLogger("ytdigest.web.run_manager")


class RunManager:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._state = "idle"
        self._run_id: int | None = None
        self._message: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def run_id(self) -> int | None:
        with self._lock:
            return self._run_id

    @property
    def message(self) -> str | None:
        with self._lock:
            return self._message

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._state == "running":
                return False, "A run is already in progress"

        def _run():
            conn = db.connect(self.config.db_path)
            try:
                result = pipeline.run_pipeline(conn, self.config)
                with self._lock:
                    self._state = "finished"
                    self._run_id = result.run_id
                    if result.notes:
                        preview = "; ".join(result.notes[:5])
                        if len(result.notes) > 5:
                            preview += f" (+{len(result.notes) - 5} more)"
                        self._message = f"Run #{result.run_id} {result.status}: {preview}"
                    else:
                        self._message = f"Run #{result.run_id} finished ({result.status})"
            except RunInProgressError:
                with self._lock:
                    self._state = "error"
                    self._message = "A run is already in progress"
            except Exception as exc:
                logger.exception("web-triggered run failed")
                with self._lock:
                    self._state = "error"
                    self._message = str(exc)
            finally:
                conn.close()

        with self._lock:
            self._state = "running"
            self._run_id = None
            self._message = "Pipeline running…"
            self._thread = threading.Thread(target=_run, daemon=True)
            self._thread.start()
        return True, "Run started"

    def reset(self) -> None:
        with self._lock:
            if self._state != "running":
                self._state = "idle"
                self._run_id = None
                self._message = None
