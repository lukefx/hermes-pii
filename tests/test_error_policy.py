"""An explicitly permissive filter must preserve tool availability on errors."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from hermes_pii.plugin import PiiPlugin


class ErrorPolicyTests(unittest.TestCase):
    def test_permissive_mode_preserves_tool_output_and_reports_degraded_filtering(self):
        plugin = PiiPlugin(error_policy="permissive")
        request = {"messages": [{"role": "tool", "tool_call_id": "synthetic-id",
                                 "content": '{"content":"TOOL_OUTPUT_OK","count":1}'}]}
        original = deepcopy(request)
        with patch.object(plugin, "_get_redactor", side_effect=OSError("private@example.com")):
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                update = plugin.llm_request(request)
        self.assertEqual(update["request"], original)
        self.assertEqual(request, original)
        self.assertIn("unfiltered", update["reason"])
        self.assertNotIn("private@example.com", repr(logs.output))
        self.assertIn("unfiltered", repr(logs.output))

    def test_repeated_warnings_are_suppressed_without_disabling_retries(self):
        from types import SimpleNamespace
        plugin = PiiPlugin(error_policy="permissive")
        request = {"messages": [{"role": "tool", "content": "Email synthetic@example.com"}]}
        fixed = SimpleNamespace(redact=lambda value: value.replace("synthetic@example.com", "<EMAIL_ADDRESS>"))
        with patch.object(plugin, "_get_redactor", side_effect=[OSError(), OSError(), fixed]) as get:
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                plugin.llm_request(request)
                plugin.llm_request(request)
                recovered = plugin.llm_request(request)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(get.call_count, 3)
        self.assertNotIn("synthetic@example.com", repr(recovered))
        self.assertNotIn("unfiltered", recovered["reason"])
        with patch.object(plugin, "_get_redactor", side_effect=OSError()):
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                plugin.llm_request(request)
        self.assertEqual(len(logs.output), 1)

    def test_strict_failure_marks_the_request_for_abort_instead_of_omitting_tool_evidence(self):
        plugin = PiiPlugin()
        request = {"messages": [{"role": "tool", "tool_call_id": "synthetic-id",
                                 "content": "TOOL_OUTPUT_OK private@example.com"}]}
        original = deepcopy(request)
        with patch.object(plugin, "_get_redactor", side_effect=OSError("private@example.com")):
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                update = plugin.llm_request(request)
        self.assertTrue(update["request"].get("_hermes_pii_blocked"))
        self.assertNotIn("private@example.com", repr(update))
        self.assertNotIn("Tool result omitted", repr(update))
        self.assertIn("blocked", update["reason"])
        self.assertNotIn("private@example.com", repr(logs.output))
        self.assertEqual(request, original)

    def test_attachment_detector_failure_cannot_hide_behind_an_omission_in_strict_mode(self):
        from types import SimpleNamespace
        plugin = PiiPlugin()
        plugin._redactor = SimpleNamespace(detect_spans=lambda text: (_ for _ in ()).throw(OSError("synthetic")))
        request = {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "synthetic"}},
        ]}]}

        def sanitizer(part, *, detect_spans, **kwargs):
            try:
                detect_spans("synthetic")
            except OSError:
                return {"type": "text", "text": "Attachment omitted"}
            self.fail("The injected detector must fail")

        with patch("hermes_pii.plugin.sanitize_content_part", side_effect=sanitizer):
            with self.assertLogs("hermes_pii.plugin", level="WARNING"):
                result = plugin.llm_request(request)
        self.assertEqual(result["request"], {"_hermes_pii_blocked": True})

    def test_execution_guard_aborts_before_the_provider_on_detector_failure(self):
        from unittest.mock import Mock

        class PolicyAbort(Exception):
            def __init__(self, message, *, code):
                super().__init__(message)
                self.code = code

        plugin = PiiPlugin()
        provider = Mock()
        request = {"messages": [{"role": "tool", "content": "TOOL_OUTPUT_OK private@example.com"}]}
        with patch("hermes_pii.plugin._require_abort_support", return_value=PolicyAbort, create=True):
            with patch.object(plugin, "_get_redactor", side_effect=OSError("private@example.com")):
                with self.assertLogs("hermes_pii.plugin", level="WARNING"):
                    with self.assertRaises(PolicyAbort) as raised:
                        plugin.llm_execution(request, provider)
        provider.assert_not_called()
        self.assertEqual(raised.exception.code, "pii_inspection_failed")
        self.assertNotIn("private@example.com", str(raised.exception))

    def test_strict_registration_refuses_hosts_without_the_abort_contract(self):
        from hermes_pii import register
        from test_plugin import PluginContext
        context = PluginContext()
        context.get_config = lambda key, default=None: default
        with patch("hermes_cli.middleware.MiddlewareAbort", None, create=True):
            with self.assertRaisesRegex(RuntimeError, "MiddlewareAbort"):
                register(context)
        self.assertEqual(context.hooks, {})
        self.assertEqual(context.middleware, {})

    def test_unexpected_filter_errors_cannot_fail_open_at_execution(self):
        from unittest.mock import Mock

        class PolicyAbort(Exception):
            def __init__(self, message, *, code):
                super().__init__(message)
                self.code = code

        plugin = PiiPlugin()
        provider = Mock()
        with patch("hermes_pii.plugin._require_abort_support", return_value=PolicyAbort):
            with patch.object(plugin, "llm_request", side_effect=RuntimeError("private@example.com")):
                with self.assertRaises(PolicyAbort) as raised:
                    plugin.llm_execution({"messages": []}, provider)
        provider.assert_not_called()
        self.assertNotIn("private@example.com", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
