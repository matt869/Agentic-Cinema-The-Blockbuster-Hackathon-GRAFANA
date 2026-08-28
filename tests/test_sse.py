"""The investigation stream.

Phase 6's acceptance is that events arrive *as they happen*, not batched at
the end -- that property is what makes the demo watchable, and it is easy to
lose to buffering without noticing.

These run a real uvicorn server in this process and read the stream over real
HTTP. That matters twice over: it exercises the actual network path a browser
uses, and because the server shares this process it also shares the
``event_bus`` singleton, so the test can publish into a live stream.

(``httpx.ASGITransport`` cannot be used here -- it buffers the whole response,
which both hides the property under test and deadlocks on an endless stream.)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest

import httpx
import uvicorn
from fastapi import FastAPI

from api.routes_stream import router as stream_router
from api.runtime import EventBus, event_bus

# Only the stream router: no lifespan, so no simulator starts and no
# telemetry is emitted by the test suite.
_app = FastAPI()
_app.include_router(stream_router)   # router already carries /stream


class _Server:
    """A real uvicorn server on an ephemeral port, in a daemon thread."""

    def __init__(self) -> None:
        config = uvicorn.Config(_app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> tuple[str, asyncio.AbstractEventLoop]:
        self.thread.start()
        for _ in range(200):
            if self.server.started and self.server.servers:
                break
            time.sleep(0.05)
        else:  # pragma: no cover
            raise RuntimeError("uvicorn did not start")
        sock = self.server.servers[0]
        port = sock.sockets[0].getsockname()[1]
        # The bus hands events to asyncio queues, so a publish from this
        # thread must be marshalled onto the server's loop.
        return f"http://127.0.0.1:{port}", sock.get_loop()

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class TestEventBus(unittest.TestCase):
    def test_replays_history_to_a_late_subscriber(self) -> None:
        bus = EventBus()
        bus.publish("inv", {"type": "started"})
        bus.publish("inv", {"type": "hypothesis"})
        self.assertEqual([e["type"] for e in bus.history("inv")],
                         ["started", "hypothesis"])

    def test_stamps_every_event_with_a_time(self) -> None:
        bus = EventBus()
        bus.publish("inv", {"type": "x"})
        self.assertIn("ts", bus.history("inv")[0])

    def test_reading_an_unknown_id_allocates_nothing(self) -> None:
        """Regression: defaultdict created a buffer for every bad id."""
        bus = EventBus()
        self.assertEqual(bus.history("never-existed"), [])
        self.assertNotIn("never-existed", bus._history)

    def test_history_is_capped_per_investigation(self) -> None:
        bus = EventBus()
        for i in range(bus.MAX_HISTORY + 200):
            bus.publish("inv", {"type": "x", "n": i})
        self.assertEqual(len(bus.history("inv")), bus.MAX_HISTORY)

    def test_old_investigations_are_evicted(self) -> None:
        """Regression: every investigation was retained forever."""
        bus = EventBus()
        for i in range(bus.MAX_INVESTIGATIONS + 15):
            bus.publish(f"inv{i}", {"type": "started"})
        self.assertEqual(len(bus._history), bus.MAX_INVESTIGATIONS)


class TestStreamRoute(unittest.TestCase):
    GAP = 0.25

    def _read(self, base: str, iid: str, limit: int) -> list[tuple[float, dict]]:
        got: list[tuple[float, dict]] = []
        with httpx.stream("GET", f"{base}/stream/investigation/{iid}",
                          timeout=30) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                got.append((time.monotonic(), event))
                if event.get("type") == "complete" or len(got) >= limit:
                    break
        return got

    def test_events_arrive_incrementally_not_batched(self) -> None:
        iid = f"sse-{time.time_ns()}"

        with _Server() as (base, loop):
            def publish_later() -> None:
                for kind in ("started", "hypothesis", "query",
                             "rejected", "complete"):
                    time.sleep(self.GAP)
                    loop.call_soon_threadsafe(
                        event_bus.publish, iid, {"type": kind}
                    )

            writer = threading.Thread(target=publish_later, daemon=True)
            writer.start()
            received = self._read(base, iid, limit=5)
            writer.join(timeout=10)

        self.assertEqual([e["type"] for _, e in received],
                         ["started", "hypothesis", "query", "rejected", "complete"])

        arrivals = [t for t, _ in received]
        spread = arrivals[-1] - arrivals[0]
        self.assertGreater(
            spread, self.GAP * 2,
            f"all events arrived within {spread:.3f}s -- that is a batched response",
        )

    def test_late_subscriber_gets_the_whole_trace(self) -> None:
        """Opening the UI mid-investigation must not lose earlier steps."""
        iid = f"sse-late-{time.time_ns()}"
        for kind in ("started", "hypothesis", "rejected", "complete"):
            event_bus.publish(iid, {"type": kind})
        with _Server() as (base, _loop):
            received = self._read(base, iid, limit=4)
        self.assertEqual([e["type"] for _, e in received],
                         ["started", "hypothesis", "rejected", "complete"])


if __name__ == "__main__":
    unittest.main()
