"""The per-investigation Grafana query cache.

A live run sent this LogQL twice, 27 seconds apart, byte for byte:

    {service_name="render_worker"} | deployment="local" | level="error"  now-1h

The free tier allows 20 model calls per model per DAY, so one duplicate is 5%
of a day's budget spent re-reading a result already in the transcript.
"""

from __future__ import annotations

import unittest

from agent.query_cache import (
    CACHE_MARKER,
    CACHE_STATE_KEY,
    CACHEABLE_TOOLS,
    MAX_ENTRIES,
    after_tool,
    before_tool,
    cache_key,
)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Ctx:
    """Stands in for ADK's ToolContext; only .state is used."""
    def __init__(self) -> None:
        self.state: dict = {}


LOGQL = {
    "logql": '{service_name="render_worker"} | deployment="local" | level="error"',
    "startRfc3339": "now-1h",
    "endRfc3339": "now",
    "limit": 50,
}
RESULT = {"lines": ["CUDA error: out of memory", "..."]}


class TestRepeatedQueryIsServedFromCache(unittest.TestCase):
    def test_the_exact_duplicate_from_the_live_run(self) -> None:
        tool, ctx = _Tool("query_loki_logs"), _Ctx()
        # First send: nothing cached, so the real tool must run.
        self.assertIsNone(before_tool(tool, LOGQL, ctx))
        after_tool(tool, LOGQL, ctx, RESULT)
        # Second, identical send: served without touching Grafana.
        served = before_tool(tool, LOGQL, ctx)
        self.assertIsNotNone(served, "duplicate query was not cached")
        self.assertEqual(served["lines"], RESULT["lines"])
        self.assertEqual(served[CACHE_MARKER], "hit")

    def test_argument_order_does_not_defeat_it(self) -> None:
        tool, ctx = _Tool("query_loki_logs"), _Ctx()
        after_tool(tool, LOGQL, ctx, RESULT)
        reordered = dict(reversed(list(LOGQL.items())))
        self.assertIsNotNone(before_tool(tool, reordered, ctx))

    def test_a_different_time_range_is_a_different_query(self) -> None:
        tool, ctx = _Tool("query_loki_logs"), _Ctx()
        after_tool(tool, LOGQL, ctx, RESULT)
        widened = dict(LOGQL, startRfc3339="now-6h")
        self.assertIsNone(before_tool(tool, widened, ctx),
                          "widening the window must reach Grafana, not the cache")

    def test_a_different_query_string_is_a_different_query(self) -> None:
        tool, ctx = _Tool("query_loki_logs"), _Ctx()
        after_tool(tool, LOGQL, ctx, RESULT)
        other = dict(LOGQL, logql='{service_name="stage_control"}')
        self.assertIsNone(before_tool(tool, other, ctx))

    def test_same_args_on_a_different_tool_is_a_different_query(self) -> None:
        ctx = _Ctx()
        after_tool(_Tool("query_loki_logs"), LOGQL, ctx, RESULT)
        self.assertIsNone(before_tool(_Tool("query_loki_stats"), LOGQL, ctx))


class TestReasoningToolsAreNeverCached(unittest.TestCase):
    """The trace tools are the investigation's writes. Serving a cached
    'already recorded' would silently drop a hypothesis or a rejection."""

    def test_write_tools_always_execute(self) -> None:
        for name in ("propose_hypothesis", "reject_hypothesis",
                     "record_evidence", "update_confidence",
                     "record_finding", "conclude"):
            with self.subTest(name):
                tool, ctx = _Tool(name), _Ctx()
                args = {"statement": "x", "entity": "node_12"}
                self.assertNotIn(name, CACHEABLE_TOOLS)
                after_tool(tool, args, ctx, {"ok": True})
                self.assertEqual(ctx.state.get(CACHE_STATE_KEY, {}), {},
                                 f"{name} must never be cached")
                self.assertIsNone(before_tool(tool, args, ctx))


class TestScoping(unittest.TestCase):
    def test_cache_does_not_cross_investigations(self) -> None:
        tool = _Tool("query_prometheus")
        a, b = _Ctx(), _Ctx()
        after_tool(tool, {"expr": "up"}, a, {"v": 1})
        # A second investigation has its own state, so it must miss.
        self.assertIsNone(before_tool(tool, {"expr": "up"}, b))

    def test_a_served_response_is_not_re_stored(self) -> None:
        tool, ctx = _Tool("query_prometheus"), _Ctx()
        after_tool(tool, {"expr": "up"}, ctx, {"v": 1})
        served = before_tool(tool, {"expr": "up"}, ctx)
        before = dict(ctx.state[CACHE_STATE_KEY])
        after_tool(tool, {"expr": "up"}, ctx, served)
        self.assertEqual(ctx.state[CACHE_STATE_KEY], before)

    def test_entries_are_bounded(self) -> None:
        tool, ctx = _Tool("query_prometheus"), _Ctx()
        for i in range(MAX_ENTRIES + 10):
            after_tool(tool, {"expr": f"metric_{i}"}, ctx, {"v": i})
        self.assertLessEqual(len(ctx.state[CACHE_STATE_KEY]), MAX_ENTRIES)

    def test_original_response_is_left_alone(self) -> None:
        # after_tool returns None so ADK keeps its own response object.
        tool, ctx = _Tool("query_prometheus"), _Ctx()
        self.assertIsNone(after_tool(tool, {"expr": "up"}, ctx, {"v": 1}))


class TestKey(unittest.TestCase):
    def test_key_is_stable_and_distinguishes_tools(self) -> None:
        self.assertEqual(cache_key("t", {"a": 1}), cache_key("t", {"a": 1}))
        self.assertNotEqual(cache_key("t", {"a": 1}), cache_key("u", {"a": 1}))

    def test_unserialisable_args_do_not_raise(self) -> None:
        cache_key("t", {"when": object()})


if __name__ == "__main__":
    unittest.main()


class TestInvestigatorIsWired(unittest.TestCase):
    """A cache nothing calls saves nothing."""

    def test_callbacks_are_attached_to_the_investigation_step(self) -> None:
        from agent.investigator import build_investigation_step
        from agent.query_cache import after_tool as at, before_tool as bt
        step = build_investigation_step()
        self.assertIs(step.before_tool_callback, bt)
        self.assertIs(step.after_tool_callback, at)


class TestWideningIsMandatory(unittest.TestCase):
    """Run A read instant values, saw 0.97, did not classify that as a "ramp",
    and so never widened -- while Run 1, on the same fault, went to now-6h and
    found the cause. The instruction gated a requirement on the model's own
    subjective reading of the data, which is why the two runs diverged."""

    def setUp(self) -> None:
        from agent.investigator import INSTRUCTION
        self.text = INSTRUCTION

    def test_widening_is_stated_as_a_requirement_not_a_permission(self) -> None:
        self.assertIn("MUST NOT call conclude", self.text)
        self.assertNotIn("YOU MAY WIDEN", self.text)

    def test_it_is_not_gated_on_the_data_looking_like_a_ramp(self) -> None:
        # The exact conditional that failed in Run A.
        self.assertNotIn('If you see a slow ramp or a resource climbing', self.text)
        self.assertIn('looks like" a ramp', self.text)

    def test_the_satisfying_query_is_spelled_out(self) -> None:
        self.assertIn("stage_control", self.text)
        self.assertIn("now-6h", self.text)
        # Run A stopped at now-1h; the prompt must say why that is not enough.
        self.assertIn('NOT "now-1h"', self.text)

    def test_error_logs_are_ruled_out_as_a_substitute(self) -> None:
        # The cause is an INFO line, so Run A's error-level sweep could not
        # have found it however many times it ran.
        self.assertIn("INFO", self.text)
        self.assertIn('level="error" is guaranteed to miss it', self.text)

    def test_repeat_queries_are_discouraged_in_the_prompt_too(self) -> None:
        # Belt and braces: the cache makes a repeat free, the prompt makes it
        # unnecessary. Neither alone is worth relying on.
        self.assertIn("Never send the same query twice", self.text)
