"""Polling helper shared by failure inject/recover steps."""

import time


def wait_until(predicate, timeout_s: float, *, interval_s: float = 1.0) -> bool:
    """Poll ``predicate`` until it returns true or ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - daemons may be restarting
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)
