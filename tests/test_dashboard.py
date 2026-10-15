"""Contract checks for the local browser-dashboard API adapter."""

from __future__ import annotations

import pathlib
import sys
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import DashboardHandler, run_simulation  # noqa: E402


class DashboardContractTests(unittest.TestCase):
    def test_response_contains_replay_evaluation_and_energy_comparisons(self):
        payload = run_simulation(
            {"days": 2, "cities": 2, "towers_per_city": 2, "seed": 7}
        )

        self.assertIn("replay", payload)
        self.assertIn("evaluation", payload)
        self.assertIn("detector", payload)
        self.assertIn("calibration", payload)
        self.assertIn("coverage", payload)
        self.assertIn("operations", payload)
        self.assertFalse(payload["scenario"]["enabled"])
        self.assertEqual(len(payload["replay"]), 48)
        self.assertEqual(len(payload["replay"][0]["towers"]), 4)
        self.assertEqual(payload["evaluation"]["interval_minutes"], 60)
        self.assertEqual(payload["evaluation"]["timezone"], "Asia/Kolkata")
        self.assertGreater(payload["summary"]["always_on_energy_kwh"], 0.0)
        self.assertGreater(payload["summary"]["scheduled_energy_kwh"], 0.0)
        self.assertGreater(payload["summary"]["adaptive_energy_kwh"], 0.0)
        self.assertTrue(payload["operations"]["simulation_only"])
        self.assertIn("fault_detector", payload["operations"])
        self.assertEqual(len(payload["coverage"]["cities"]["City-01"]["latest"]["cells"]), 25)
        self.assertEqual(len(payload["coverage"]["timeline"][0]["cell_statuses"]), 25)
        self.assertTrue(set(payload["coverage"]["timeline"][0]["cell_statuses"]) <= {"c", "b", "u"})

        tower = payload["replay"][0]["towers"][0]
        for key in (
            "tower_id",
            "active_users",
            "demand_mbps",
            "forecast_mbps",
            "active_capacity_mbps",
            "adaptive_power_kw",
            "mode",
            "controller_reason",
        ):
            self.assertIn(key, tower)

    def test_surge_demo_is_exposed_for_replay_selection(self):
        payload = run_simulation(
            {
                "days": 3,
                "cities": 1,
                "towers_per_city": 1,
                "seed": 4,
                "event_probability": 0.0,
                "anomaly_probability": 0.0,
                "surge_demo": True,
            }
        )

        scenario = payload["scenario"]
        self.assertTrue(scenario["enabled"])
        self.assertEqual(scenario["tower_id"], "city-01-T01")
        self.assertEqual(scenario["duration_intervals"], 2)
        scenario_frames = [
            frame
            for frame in payload["replay"]
            if frame["timestamp"] == scenario["timestamp"]
        ]
        self.assertEqual(len(scenario_frames), 1)
        self.assertTrue(scenario_frames[0]["towers"][0]["spike"])
        self.assertGreater(payload["detector"]["alert_count"], 0)

    def test_fault_probability_returns_fault_metrics_and_operations(self):
        payload = run_simulation(
            {"days": 1, "cities": 1, "towers_per_city": 1, "seed": 8, "fault_probability": 1.0}
        )
        self.assertGreater(payload["operations"]["fault_detector"]["detected_fault_count"], 0)
        self.assertGreater(payload["operations"]["summary"]["incident_count"], 0)
        self.assertTrue(all(item["simulation_only"] for item in payload["operations"]["dispatches"]))
        self.assertTrue(any(tower["fault"] for frame in payload["replay"] for tower in frame["towers"]))


class DashboardHttpTests(unittest.TestCase):
    """Exercise the same HTTP paths the browser uses."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def test_static_assets_and_health_support_get_and_head(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"GridPulse", body)
        self.assertIn(b"Intelligence For Industries", body)
        self.assertIn(b'href="lab.html"', body)
        self.assertIn(b'id="case-studies"', body)
        self.assertIn(b"21.6%", body)
        self.assertIn(b"09:00", body)
        self.assertIn(b"City-03", body)
        self.assertTrue(any(key.lower() == "content-type" and "text/html" in value for key, value in headers))

        status, _, body = self.request("GET", "/lab.html")
        self.assertEqual(status, 200)
        self.assertIn(b"GridPulse Lab", body)
        self.assertIn(b"src=\"lab.js\"", body)
        self.assertIn(b"cloudfront.net", body)
        self.assertIn(b"lab-bg-pattern", body)
        self.assertIn(b"playback-start", body)
        self.assertIn(b"deploy-help-team", body)

        status, _, body = self.request("GET", "/lab.js")
        self.assertEqual(status, 200)
        self.assertIn(b'fetch("api/simulation"', body)
        self.assertIn(b"requestAnimationFrame", body)
        self.assertIn(b"does not contact or direct real people", body)

        status, _, body = self.request("GET", "/lab.css")
        self.assertEqual(status, 200)
        self.assertIn(b"grayscale(1)", body)
        self.assertIn(b"background: #000", body)

        status, _, body = self.request("HEAD", "/lab.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

        status, headers, body = self.request("HEAD", "/dashboard.js")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertTrue(any(key.lower() == "content-length" and int(value) > 0 for key, value in headers))

        status, headers, body = self.request("GET", "/main.js")
        self.assertEqual(status, 200)
        self.assertIn(b"IntersectionObserver", body)
        self.assertIn(b'window.location.href = "lab.html"', body)
        self.assertTrue(any(key.lower() == "content-type" and "javascript" in value for key, value in headers))

        status, headers, body = self.request("GET", "/assets/logo.webp")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"RIFF"))
        self.assertTrue(any(key.lower() == "content-type" and "image/webp" in value for key, value in headers))

        status, headers, body = self.request("GET", "/fonts/GeistPixel-Circle.woff2")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"wOF2"))
        self.assertTrue(any(key.lower() == "content-type" and "font/woff2" in value for key, value in headers))

        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)

    def test_options_and_invalid_nonfinite_json_are_controlled(self):
        status, headers, body = self.request("OPTIONS", "/api/simulation")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertTrue(any(key.lower() == "allow" and "POST" in value for key, value in headers))

        status, _, body = self.request(
            "POST",
            "/api/simulation",
            body=b'{"days": Infinity}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"non-finite JSON", body)

        status, _, _ = self.request("GET", "/assets/%2e%2e/dashboard.py")
        self.assertEqual(status, 404)

    def test_valid_post_returns_browser_contract(self):
        body = json.dumps({"days": 1, "cities": 1, "towers_per_city": 1, "seed": 7}).encode()
        status, headers, response_body = self.request(
            "POST",
            "/api/simulation",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(key.lower() == "content-type" and "application/json" in value for key, value in headers))
        payload = json.loads(response_body)
        self.assertIn("replay", payload)
        self.assertIn("summary", payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
