"""OTLP endpoint construction.

Every value here is a shape a hosting dashboard has actually produced from
someone pasting into a text field. They matter because the string is
concatenated into a URL, and the Grafana gateway answers a malformed one with
404 per batch -- which the SDK logs and swallows, so the process keeps serving
while exporting nothing. Measured against the live gateway: the correct path
returns 200 (metrics) / 204 (logs); a single trailing space returns 404.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from simulator.otlp_client import load_credentials

GOOD = "https://otlp-gateway-prod-ap-southeast-1.grafana.net/otlp"
WANT = GOOD  # what load_credentials must always hand back


def _env(endpoint: str) -> dict[str, str]:
    return {
        "GRAFANA_OTLP_ENDPOINT": endpoint,
        "GRAFANA_OTLP_INSTANCE_ID": "123456",
        "GRAFANA_OTLP_TOKEN": "glc_test",
    }


class TestEndpointNormalisation(unittest.TestCase):
    def _endpoint(self, raw: str) -> str:
        with patch.dict(os.environ, _env(raw), clear=False), \
             patch("simulator.otlp_client.load_dotenv"):
            return load_credentials()["endpoint"]

    def test_already_correct_is_unchanged(self) -> None:
        self.assertEqual(self._endpoint(GOOD), WANT)

    def test_trailing_newline(self) -> None:
        # The classic paste-into-a-web-field result.
        self.assertEqual(self._endpoint(GOOD + "\n"), WANT)

    def test_trailing_space(self) -> None:
        # Verified to 404 against the real gateway if it survives.
        self.assertEqual(self._endpoint(GOOD + " "), WANT)

    def test_leading_and_trailing_whitespace(self) -> None:
        self.assertEqual(self._endpoint("  " + GOOD + "\t\n"), WANT)

    def test_double_quoted(self) -> None:
        self.assertEqual(self._endpoint(f'"{GOOD}"'), WANT)

    def test_single_quoted(self) -> None:
        self.assertEqual(self._endpoint(f"'{GOOD}'"), WANT)

    def test_quoted_with_whitespace_outside(self) -> None:
        self.assertEqual(self._endpoint(f'  "{GOOD}"  \n'), WANT)

    def test_trailing_slash(self) -> None:
        self.assertEqual(self._endpoint(GOOD + "/"), WANT)

    def test_base_url_without_otlp_segment_gets_it(self) -> None:
        base = "https://otlp-gateway-prod-ap-southeast-1.grafana.net"
        self.assertEqual(self._endpoint(base), WANT)

    def test_otlp_segment_is_not_doubled(self) -> None:
        # Doubling produces /otlp/otlp/v1/metrics, which 404s.
        self.assertEqual(self._endpoint(GOOD), WANT)
        self.assertNotIn("/otlp/otlp", self._endpoint(GOOD))

    def test_signal_paths_build_correctly(self) -> None:
        e = self._endpoint(GOOD + "\n")
        self.assertEqual(f"{e}/v1/metrics",
                         f"{WANT}/v1/metrics")
        self.assertEqual(f"{e}/v1/logs", f"{WANT}/v1/logs")


class TestCredentialsAreCleaned(unittest.TestCase):
    def test_id_and_token_are_stripped(self) -> None:
        env = _env(GOOD)
        env["GRAFANA_OTLP_INSTANCE_ID"] = " 123456\n"
        env["GRAFANA_OTLP_TOKEN"] = '"glc_test"\n'
        with patch.dict(os.environ, env, clear=False), \
             patch("simulator.otlp_client.load_dotenv"):
            c = load_credentials()
        # A trailing newline in the token corrupts the Basic auth header.
        self.assertEqual(c["instance_id"], "123456")
        self.assertEqual(c["token"], "glc_test")


class TestWhitespaceOnlyIsMissing(unittest.TestCase):
    def test_blank_variable_is_reported_as_missing(self) -> None:
        env = _env(GOOD)
        env["GRAFANA_OTLP_TOKEN"] = "   "
        with patch.dict(os.environ, env, clear=False), \
             patch("simulator.otlp_client.load_dotenv"):
            with self.assertRaises(RuntimeError) as ctx:
                load_credentials()
        # Naming it beats 404ing forever against a gateway that cannot say why.
        self.assertIn("GRAFANA_OTLP_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class TestSignalPathIsStripped(unittest.TestCase):
    """Grafana's quickstart shows the full metrics URL, so people paste it.

    Appending /otlp to a URL that already ends in a signal path produced
    .../otlp/v1/metrics/otlp/v1/metrics -- a 404, and a worse one than the
    input deserved.
    """

    def _endpoint(self, raw: str) -> str:
        with patch.dict(os.environ, _env(raw), clear=False), \
             patch("simulator.otlp_client.load_dotenv"):
            return load_credentials()["endpoint"]

    def test_full_metrics_url(self) -> None:
        self.assertEqual(self._endpoint(GOOD + "/v1/metrics"), WANT)

    def test_full_logs_url(self) -> None:
        self.assertEqual(self._endpoint(GOOD + "/v1/logs"), WANT)

    def test_full_traces_url(self) -> None:
        self.assertEqual(self._endpoint(GOOD + "/v1/traces"), WANT)

    def test_full_url_with_trailing_slash(self) -> None:
        self.assertEqual(self._endpoint(GOOD + "/v1/metrics/"), WANT)

    def test_full_url_quoted_and_padded(self) -> None:
        self.assertEqual(self._endpoint(f'  "{GOOD}/v1/metrics"\n'), WANT)

    def test_never_doubles_otlp(self) -> None:
        for raw in (GOOD, GOOD + "/v1/metrics", GOOD + "/", GOOD + "\n"):
            self.assertNotIn("/otlp/otlp", self._endpoint(raw))


class TestHealthReportsResolvedEndpoint(unittest.TestCase):
    """/health must show the URL in use, so a 404 is diagnosable remotely."""

    def test_health_exposes_endpoint_but_no_credentials(self) -> None:
        from api.main import health
        env = _env(GOOD + "\n")
        with patch.dict(os.environ, env, clear=False):
            h = health()
        self.assertEqual(h["otlp_endpoint"], f"{WANT}/v1/metrics")
        blob = repr(h)
        self.assertNotIn("glc_", blob, "token must never appear")
        self.assertNotIn("123456", blob, "instance id must never appear")
