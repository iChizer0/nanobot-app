"""Readiness-gate regression tests; no device or third-party packages needed."""

from __future__ import annotations

import http.client
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

kit = types.ModuleType("kit")
kit_app = types.ModuleType("kit.app")
kit_config = types.ModuleType("kit.config")


class _App:
    def setup(self, config):
        pass


kit_app.App = _App
kit_app.run_app = lambda app: None
kit_config.appdata_root = lambda: "/tmp"
sys.modules.setdefault("kit", kit)
sys.modules.setdefault("kit.app", kit_app)
sys.modules.setdefault("kit.config", kit_config)

import app as wrapper  # noqa: E402


class GateTests(unittest.TestCase):
    def setUp(self):
        self.server = wrapper.ThreadingHTTPServer(
            ("127.0.0.1", 0), wrapper._GateHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, host="camera.example"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", "/", headers={"Host": host})
            response = connection.getresponse()
            body = response.read().decode()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def test_waiting_page_disables_cache_and_retries_with_a_fresh_url(self):
        with patch.object(wrapper, "_probe_status", return_value=None):
            status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn("http-equiv=refresh", body)
        self.assertIn("window.location.replace", body)
        self.assertIn("searchParams.set('retry'", body)

    def test_probe_status_uses_a_real_http_response(self):
        class StatusHandler(wrapper.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = wrapper.ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(wrapper._probe_status(port), 204)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertIsNone(wrapper._probe_status(port))

    def test_gateway_5xx_is_not_ready_and_does_not_probe_the_relay(self):
        with patch.object(wrapper, "_probe_status", return_value=503) as probe:
            status, _, _ = self.request()
        self.assertEqual(status, 200)
        probe.assert_called_once_with(wrapper.WEBUI_PORT)

    def test_ready_gateway_uses_http_when_the_relay_is_unavailable(self):
        def probe(port, *, tls=False):
            return None if tls else 200

        with patch.object(wrapper, "_probe_status", side_effect=probe):
            status, headers, _ = self.request()
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://camera.example:18765/")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_ready_gateway_and_relay_use_https(self):
        def probe(port, *, tls=False):
            return 204 if tls else 200

        with patch.object(wrapper, "_probe_status", side_effect=probe):
            status, headers, _ = self.request()
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://camera.example:19765/")

    def test_broken_relay_falls_back_to_the_ready_direct_webui(self):
        def probe(port, *, tls=False):
            return 502 if tls else 200

        with patch.object(wrapper, "_probe_status", side_effect=probe):
            status, headers, _ = self.request()
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "http://camera.example:18765/")

    def test_bracketed_ipv6_host_without_a_port_is_preserved(self):
        with patch.object(wrapper, "_probe_status", return_value=200):
            status, headers, _ = self.request(host="[::1]")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://[::1]:19765/")

    def test_bracketed_ipv6_host_with_a_port_drops_only_the_port(self):
        with patch.object(wrapper, "_probe_status", return_value=200):
            status, headers, _ = self.request(host="[::1]:18766")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://[::1]:19765/")


if __name__ == "__main__":
    unittest.main()
