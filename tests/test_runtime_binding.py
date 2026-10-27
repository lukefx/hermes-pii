"""The registered host filter must use the private runtime, not host NLP packages."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from hermes_pii.plugin import PiiPlugin


class RuntimeBindingTests(unittest.TestCase):
    def test_text_filter_uses_one_injected_runtime(self):
        client = SimpleNamespace(redact=lambda text: text.replace("hermes-pii-check@example.com", "<EMAIL_ADDRESS>")
                                 .replace("private@example.com", "<EMAIL_ADDRESS>"))
        factory = Mock(return_value=client)
        plugin = PiiPlugin(error_policy="permissive", runtime_factory=factory)
        original: dict = {"messages": [{"role": "user", "content": "Email private@example.com"}]}
        opaque = object()
        original["metadata"] = opaque
        result = plugin.llm_request(original)["request"]
        self.assertIn("<EMAIL_ADDRESS>", result["messages"][0]["content"])
        self.assertEqual(original["messages"][0]["content"], "Email private@example.com")
        self.assertIs(result["metadata"], opaque)
        self.assertIsNone(plugin.pre_llm_call())
        factory.assert_called_once_with()


    def test_rejected_runtime_is_closed_before_retry(self):
        for failure in (RuntimeError("synthetic failure"), "unfiltered@example.com"):
            with self.subTest(failure=type(failure).__name__):
                rejected = Mock()
                if isinstance(failure, Exception):
                    rejected.redact.side_effect = failure
                else:
                    rejected.redact.return_value = failure
                recovered = Mock()
                recovered.redact.return_value = "<EMAIL_ADDRESS>"
                factory = Mock(side_effect=[rejected, recovered])
                plugin = PiiPlugin(runtime_factory=factory)
                self.assertFalse(plugin.check_health()["ready"])
                rejected.close.assert_called_once_with()
                self.assertTrue(plugin.check_health()["ready"])
                recovered.close.assert_not_called()
                self.assertEqual(factory.call_count, 2)

    def test_worker_attachment_detector_failure_blocks_strict_request(self):
        client = SimpleNamespace(
            redact=lambda text: "<EMAIL_ADDRESS>",
            sanitize_part=Mock(return_value={"part": {"type": "text", "text": "Attachment omitted"},
                                             "detector_error": True}),
        )
        plugin = PiiPlugin(runtime_factory=lambda: client)
        request = {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
        ]}]}
        with self.assertLogs("hermes_pii.plugin", level="WARNING"):
            result = plugin.llm_request(request)
        self.assertEqual(result["request"], {"_hermes_pii_blocked": True})
        client.sanitize_part.assert_called_once()


    def test_office_reference_conversion_uses_the_private_runtime(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        client = SimpleNamespace(redact=lambda text: "<EMAIL_ADDRESS>",
                                 sanitize_part=Mock(return_value={
                                     "part": [{"type": "image_url", "image_url": {"url": "synthetic-safe-image"}}],
                                     "detector_error": False}))
        plugin = PiiPlugin(runtime_factory=lambda: client)
        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as directory:
            (Path(directory) / "report.docx").write_bytes(b"synthetic-private-document")
            with patch("hermes_pii.office_references.Path.cwd", return_value=Path(directory)):
                result = plugin._redact_user_text("Inspect @file:report.docx")
        client.sanitize_part.assert_called_once()
        self.assertIn("synthetic-safe-image", repr(result))


if __name__ == "__main__":
    unittest.main()
