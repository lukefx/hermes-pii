"""Real plugin registration/dispatch with synthetic data and a loopback HTTP recorder."""
from copy import deepcopy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from hermes_cli import middleware
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest

from hermes_pii import register


class RecordingTransportTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        received = self.received

        class Recorder(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"source": "synthetic-local-recorder", "ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.home = self.enterContext(tempfile.TemporaryDirectory(
            prefix="hermes-pii-recording-", dir=os.environ.get("TMPDIR"),
        ))
        connect = socket.socket.connect

        def loopback_only(sock, address):
            if address[0] not in {"127.0.0.1", "::1"}:
                raise AssertionError("Non-loopback networking is forbidden in this test")
            return connect(sock, address)

        self.enterContext(patch.object(socket.socket, "connect", loopback_only))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def make_plugin(self, policy):
        manager = PluginManager(scope_key=self.home)
        manifest = PluginManifest(name="hermes-pii", path=str(Path(__file__).resolve().parents[1]))
        settings = {"error_policy": policy, "language": "en", "model_name": "en_core_web_sm"}
        config = {"plugins": {"entries": {"hermes-pii": {"settings": settings}}}}
        import sys
        from hermes_pii.runtime_client import RuntimeClient
        client = RuntimeClient(os.environ.get("HERMES_PII_TEST_RUNTIME_PYTHON", sys.executable), "en", "en_core_web_sm")
        self.addCleanup(client.close)
        with patch("hermes_cli.plugins.load_config_readonly", return_value=config):
            with patch("hermes_pii.cli.make_runtime", return_value=client):
                register(PluginContext(manifest, manager))
        self.assertNotIn("llm_request", manager._middleware)
        self.enterContext(patch("hermes_cli.plugins._delivery_manager", return_value=manager))
        return getattr(manager._middleware["llm_execution"][0], "__self__")

    def send(self, request):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("POST", "/synthetic-provider", body=json.dumps(request),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            connection.close()

    @staticmethod
    def request(field, stream):
        output = '{ "content": "Contact John Smith at john.smith@example.com", "count": 1, "ok": true }'
        if field == "input":
            messages = [
                {"role": "user", "content": "Use the synthetic tool result."},
                {"type": "function_call_output", "call_id": "synthetic-id", "output": output},
            ]
        else:
            messages = [
                {"role": "user", "content": "Use the synthetic tool result."},
                {"role": "tool", "tool_call_id": "synthetic-id", "content": output},
            ]
        return {"model": "synthetic-local-model", field: messages, "stream": stream}

    def verify_success(self, policy):
        self.make_plugin(policy)
        for field, stream in itertools.product(("messages", "input"), (False, True)):
            with self.subTest(field=field, stream=stream, policy=policy):
                request = self.request(field, stream)
                original = deepcopy(request)
                result = middleware.run_llm_execution_middleware(request, self.send)
                self.assertEqual(result, {"source": "synthetic-local-recorder", "ok": True})
                wire = self.received[-1]
                self.assertNotIn("John Smith", repr(wire))
                self.assertNotIn("john.smith@example.com", repr(wire))
                self.assertIn("<EMAIL_ADDRESS>", repr(wire))
                output = wire[field][-1]["output" if field == "input" else "content"]
                self.assertEqual(json.loads(output)["count"], 1)
                self.assertIs(json.loads(output)["ok"], True)
                self.assertEqual(wire[field][-1].get("call_id", wire[field][-1].get("tool_call_id")), "synthetic-id")
                self.assertEqual(wire["stream"], stream)
                self.assertEqual(request, original)

    def test_permissive_success_filters_real_http_payloads(self):
        self.verify_success("permissive")

    @unittest.skipUnless(hasattr(middleware, "MiddlewareAbort"), "requires the isolated strict-host extension")
    def test_strict_success_filters_real_http_payloads(self):
        self.verify_success("strict")

    @unittest.skipUnless(hasattr(middleware, "MiddlewareAbort"), "requires the isolated strict-host extension")
    def test_strict_detector_failure_sends_no_http_request(self):
        plugin = self.make_plugin("strict")
        for field, stream in itertools.product(("messages", "input"), (False, True)):
            with self.subTest(field=field, stream=stream):
                request = self.request(field, stream)
                original = deepcopy(request)
                with patch.object(plugin, "_get_redactor", side_effect=OSError("private@example.com")):
                    with self.assertRaises(getattr(middleware, "MiddlewareAbort")) as raised:
                        middleware.run_llm_execution_middleware(request, self.send)
                self.assertEqual(raised.exception.code, "pii_inspection_failed")
                self.assertNotIn("private@example.com", str(raised.exception))
                self.assertEqual(self.received, [])
                self.assertEqual(request, original)

    def test_permissive_detector_failure_preserves_real_http_payload(self):
        plugin = self.make_plugin("permissive")
        request = self.request("messages", False)
        original = deepcopy(request)
        with patch.object(plugin, "_get_redactor", side_effect=OSError("private@example.com")):
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                middleware.run_llm_execution_middleware(request, self.send)
        self.assertEqual(self.received, [original])
        self.assertEqual(request, original)
        self.assertIn("unfiltered", repr(logs.output))
        self.assertNotIn("private@example.com", repr(logs.output))


if __name__ == "__main__":
    unittest.main()
