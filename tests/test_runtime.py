"""StageRunner start-up behaviour.

Two properties matter for a deployment where the host injects secrets for us
and nobody is watching the container logs:

* a missing credential must not take the whole app down, and
* concurrent ``start()`` calls must produce exactly one emitter thread.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from api.runtime import StageRunner


class TestStartFailureIsNotFatal(unittest.TestCase):
    """A bad credential degrades the demo; it does not abort the lifespan."""

    def test_start_records_error_instead_of_raising(self) -> None:
        runner = StageRunner()
        boom = RuntimeError("Missing required environment variables: X")

        with patch("api.runtime.StageEmitter", side_effect=boom):
            runner.start()  # must not raise -- the lifespan depends on it

        self.assertFalse(runner.running)
        self.assertIsNotNone(runner.start_error)
        self.assertIn("Missing required environment variables", runner.start_error)

        # The routers must stay answerable rather than blowing up.
        self.assertEqual(
            sorted(runner.fault_state()), sorted(runner.fault_state())
        )
        with self.assertRaises(RuntimeError):
            runner.start_fault("genlock_loss")

        # And shutting down a runner that never started is a no-op.
        runner.stop()

    def test_error_clears_on_a_later_successful_start(self) -> None:
        runner = StageRunner()
        with patch("api.runtime.StageEmitter", side_effect=RuntimeError("no creds")):
            runner.start()
        self.assertIsNotNone(runner.start_error)

        with patch("api.runtime.StageEmitter"):
            runner.start()
        try:
            self.assertTrue(runner.running)
            self.assertIsNone(runner.start_error)
        finally:
            runner.stop()


class TestDoubleStartGuard(unittest.TestCase):
    """Two emitters would write the same series -- Mimir sees out-of-order
    samples rather than an obvious failure, so the guard is load-bearing."""

    def test_concurrent_starts_create_one_thread(self) -> None:
        runner = StageRunner()
        created = []

        def make_emitter():
            created.append(1)
            return MagicMock()

        gate = threading.Barrier(8)

        def racer() -> None:
            gate.wait()
            runner.start()

        with patch("api.runtime.StageEmitter", side_effect=make_emitter):
            threads = [threading.Thread(target=racer) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        try:
            self.assertEqual(len(created), 1, "started more than one emitter")
            self.assertTrue(runner.running)
        finally:
            runner.stop()


if __name__ == "__main__":
    unittest.main()
