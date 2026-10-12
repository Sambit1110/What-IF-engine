"""Named-pipe fixtures that cannot hang the test run."""

import os
import threading
import unittest
from pathlib import Path

FIFO_SUPPORTED = hasattr(os, "mkfifo")


def make_fifo(path: Path) -> None:
    """Create a named pipe, or skip the test where that is impossible."""
    try:
        os.mkfifo(path)
    except OSError as exc:
        raise unittest.SkipTest(f"cannot create a named pipe here: {exc}")


def call_with_timeout(function, timeout: float = 10.0):
    """
    Run function in a daemon thread and fail, instead of hanging, if it
    blocks (e.g. opening a named pipe that has no writer).
    """
    outcome: dict[str, object] = {}

    def target():
        try:
            outcome["value"] = function()
        except BaseException as exc:  # re-raised in the test thread
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise AssertionError(
            f"did not finish within {timeout:g}s (blocked on a named pipe?)"
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]
