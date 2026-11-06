"""Small in-process capacity gate for CPU-heavy solver routes."""
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock

from app.hardening.errors import ApiError

# 423 (not 503): HTTP 503 is reserved for the shared rate-limit engine's kill switch ("demo paused"), so the frontend can never mistake
# a busy solver for a paused demo. The error code stays "solver_busy" and the body still carries retry_after_seconds.
SOLVER_BUSY_STATUS = 423


class SolverCapacity:
    """Non-blocking semaphore with observable active count.

    This is intentionally per process. It protects a small hosted backend from
    running multiple OR-Tools solves in the same worker at once; a multi-worker
    deployment must set the worker count with this per-process limit in mind.
    """

    def __init__(self, limit: int, retry_after_seconds: int):
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds
        self._sem = BoundedSemaphore(limit)
        self._lock = Lock()
        self._active = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @contextmanager
    def claim(self):
        if not self._sem.acquire(blocking=False):
            raise ApiError(
                SOLVER_BUSY_STATUS,
                "solver_busy",
                "The itinerary solver is busy. Please try again shortly; your request was not started.",
                {"retry_after_seconds": self.retry_after_seconds, "active_solves": self.active, "capacity": self.limit},
                {"Retry-After": str(self.retry_after_seconds)},
            )
        with self._lock:
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
            self._sem.release()
