"""Preparation, retry, and concurrency checks without remote providers."""
import socket
import unittest
from unittest.mock import patch

from hermes_pii.plugin import PiiPlugin


class LifecycleTests(unittest.TestCase):
    def test_hook_prepares_once_without_analyzing_user_text(self):
        plugin = PiiPlugin()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            with patch("hermes_pii.plugin.PiiPlugin._get_redactor", wraps=plugin._get_redactor) as prepare:
                self.assertIsNone(plugin.pre_llm_call(user_message="Explain how Python works."))
                self.assertEqual(prepare.call_count, 1)
            detector = plugin._get_redactor()
            with patch.object(detector, "redact", wraps=detector.redact) as analyze:
                plugin.pre_llm_call(user_message="Explain how Python works.")
                sent = plugin.llm_request(request={"messages": [
                    {"role": "user", "content": "Explain how Python works."},
                ]})
                self.assertEqual(analyze.call_count, 1)
        self.assertEqual(sent["request"]["messages"][0]["content"], "Explain how Python works.")

    def test_concurrent_requests_share_one_detector(self):
        from concurrent.futures import ThreadPoolExecutor
        from hermes_pii.redactor import PresidioRedactor

        plugin = PiiPlugin()
        request = {"messages": [{"role": "user", "content": "Explain how Python works."}]}
        with patch("hermes_pii.redactor.PresidioRedactor", wraps=PresidioRedactor) as factory:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(plugin.llm_request, request=request) for _ in range(8)]
                for future in futures:
                    self.assertEqual(future.result(timeout=10)["request"], request)
        self.assertEqual(factory.call_count, 1)

    def test_transient_failure_preserves_text_and_next_call_recovers(self):
        text = "Email alice@example.com."
        plugin = PiiPlugin()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            detector = plugin._get_redactor()
            real_redact = detector.redact
            calls = 0

            def fail_once(value):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("Synthetic detector failure")
                return real_redact(value)

            request = {"messages": [{"role": "user", "content": text}]}
            with patch.object(detector, "redact", side_effect=fail_once):
                with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                    first = plugin.llm_request(request=request)
                second = plugin.llm_request(request=request)
        self.assertEqual(first["request"], request)
        self.assertNotIn("alice@example.com", repr(second["request"]))
        self.assertEqual(request["messages"][0]["content"], text)
        self.assertNotIn(text, repr(logs.output))
        self.assertIn("original text", logs.output[0])
        self.assertNotIn("until process restart", logs.output[0])


if __name__ == "__main__":
    unittest.main()
