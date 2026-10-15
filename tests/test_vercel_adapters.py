"""Focused checks for the file-based Vercel function adapters."""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import DashboardHandler  # noqa: E402
from api.health import handler as HealthHandler  # noqa: E402
from api.simulation import handler as SimulationHandler  # noqa: E402


class VercelAdapterTests(unittest.TestCase):
    def test_entrypoints_are_base_http_handler_subclasses(self):
        self.assertTrue(issubclass(SimulationHandler, DashboardHandler))
        self.assertTrue(issubclass(HealthHandler, DashboardHandler))

    def test_config_uses_file_based_python_functions(self):
        config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))

        self.assertEqual(config["$schema"], "https://openapi.vercel.sh/vercel.json")
        self.assertEqual(config["functions"], {"api/*.py": {}})
        self.assertNotIn("builds", config)


class VercelHttpAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.health_server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        cls.simulation_server = ThreadingHTTPServer(("127.0.0.1", 0), SimulationHandler)
        cls.health_thread = threading.Thread(
            target=cls.health_server.serve_forever, daemon=True
        )
        cls.simulation_thread = threading.Thread(
            target=cls.simulation_server.serve_forever, daemon=True
        )
        cls.health_thread.start()
        cls.simulation_thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.health_server, cls.simulation_server):
            server.shutdown()
            server.server_close()
        for thread in (cls.health_thread, cls.simulation_thread):
            thread.join(timeout=3)

    @staticmethod
    def request(server, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={"Content-Type": "application/json"} if body else {},
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_health_adapter_returns_health_payload(self):
        status, body = self.request(self.health_server, "GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"ok": True, "service": "telecom-energy-prototype"},
        )

    def test_simulation_adapter_returns_existing_browser_contract(self):
        request_body = json.dumps(
            {"days": 1, "cities": 1, "towers_per_city": 1, "seed": 7}
        ).encode("utf-8")
        status, body = self.request(
            self.simulation_server, "POST", "/api/simulation", request_body
        )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("summary", payload)
        self.assertIn("replay", payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
