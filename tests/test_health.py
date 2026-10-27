"""Detector readiness must be observable without sending any user content."""
import unittest
from unittest.mock import patch

from hermes_pii.plugin import PiiPlugin


class HealthTests(unittest.TestCase):
    def test_missing_model_reports_safe_failure_and_can_recover(self):
        plugin = PiiPlugin()
        with patch.object(plugin, "_get_redactor", side_effect=OSError("private@example.com")):
            status = plugin.check_health()
        self.assertEqual(status, {"ready": False, "error_type": "OSError"})
        self.assertNotIn("private@example.com", repr(status))
        with patch.object(plugin, "_get_redactor", return_value=object()):
            self.assertEqual(plugin.check_health(), {"ready": True, "error_type": None})

    def test_self_check_rejects_a_detector_that_passes_the_probe_through(self):
        from types import SimpleNamespace
        plugin = PiiPlugin()
        broken = SimpleNamespace(redact=lambda text: text)
        fixed = SimpleNamespace(redact=lambda text: "<EMAIL_ADDRESS>")
        with patch("hermes_pii.redactor.PresidioRedactor", side_effect=[broken, fixed]):
            self.assertEqual(plugin.check_health(), {"ready": False, "error_type": "RuntimeError"})
            self.assertEqual(plugin.check_health(), {"ready": True, "error_type": None})

    def test_health_command_returns_nonzero_with_a_safe_json_report(self):
        import contextlib
        import io
        import json
        from hermes_pii.health import main
        output = io.StringIO()
        with patch.object(PiiPlugin, "_get_redactor", side_effect=OSError("private@example.com")):
            with contextlib.redirect_stdout(output):
                code = main(["--model-name", "missing", "--language", "it"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"ready": False, "error_type": "OSError"})

    def test_health_can_require_the_host_abort_contract(self):
        import contextlib
        import io
        import json
        from hermes_pii.health import main
        output = io.StringIO()
        with patch.object(PiiPlugin, "_get_redactor", return_value=object()):
            with patch("hermes_cli.middleware.MiddlewareAbort", None, create=True):
                with contextlib.redirect_stdout(output):
                    code = main(["--require-abort"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "ready": False, "error_type": "RuntimeError", "abort_supported": False,
        })


if __name__ == "__main__":
    unittest.main()
