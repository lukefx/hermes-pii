"""Synthetic provider boundary: real Presidio, no remote LLM calls."""
from copy import deepcopy
import unittest

from hermes_pii import register


class PluginContext:
    def __init__(self):
        self.hooks = {}
        self.middleware = {}

    def get_config(self, key, default=None):
        # Legacy-host fixtures opt in explicitly; strict compatibility is tested separately.
        return "permissive" if key == "error_policy" else default

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_middleware(self, name, callback):
        self.middleware[name] = callback


class PluginTests(unittest.TestCase):
    def test_user_pii_is_not_logged_at_debug_level(self):
        from unittest.mock import patch
        from hermes_pii.plugin import PiiPlugin

        plugin = PiiPlugin()
        text = "Contact private@example.com"
        request = {"messages": [{"role": "user", "content": text}]}
        with self.assertNoLogs("hermes_pii.plugin", level="DEBUG"):
            with patch.object(plugin, "_redact_text", return_value="<EMAIL_ADDRESS>"):
                result = plugin.llm_request(request)["request"]
        self.assertEqual(result["messages"][0]["content"], "<EMAIL_ADDRESS>")

    def test_selective_copy_preserves_nested_input_and_skips_opaque_fields(self):
        from unittest.mock import patch
        from hermes_pii.plugin import PiiPlugin

        class Opaque:
            def __deepcopy__(self, memo):
                raise AssertionError("Unfiltered fields must not be copied")

        asset = {"type": "image_url", "image_url": {"url": "opaque"}}
        text = {"type": "text", "text": "private@example.com"}
        function = {"name": "send", "arguments": '{"email":"private@example.com"}'}
        message = {"role": "user", "content": [text, asset], "tool_calls": [{"id": "1", "function": function}]}
        metadata = Opaque()
        request = {"messages": [message], "metadata": metadata}
        plugin = PiiPlugin()
        with patch.object(plugin, "_redact_text", side_effect=lambda value: value.replace("private@example.com", "<EMAIL_ADDRESS>")):
            result = plugin.llm_request(request)["request"]
        self.assertEqual(text["text"], "private@example.com")
        self.assertEqual(function["arguments"], '{"email":"private@example.com"}')
        self.assertIs(result["metadata"], metadata)
        self.assertEqual(asset["image_url"]["url"], "opaque")
        self.assertIn("Attachment omitted", result["messages"][0]["content"][1]["text"])
        self.assertEqual(result["messages"][0]["content"][0]["text"], "<EMAIL_ADDRESS>")
        self.assertEqual(result["messages"][0]["tool_calls"][0]["function"]["arguments"], function["arguments"])

    def test_hook_and_middleware_remove_pii_from_chat_provider_request(self):
        ctx = PluginContext()
        register(ctx)
        self.assertIn("pre_llm_call", ctx.hooks)
        self.assertIn("llm_execution", ctx.middleware)
        text = "Mi chiamo Mario Rossi, email mario.rossi@example.com."
        request = {"model": "local-test", "stream": True, "messages": [
            {"role": "system", "content": "Rispondi in italiano."},
            {"role": "user", "content": text},
        ]}
        original = deepcopy(request)
        # Returning redacted text here would APPEND it, leaving the raw prompt.
        self.assertIsNone(ctx.hooks["pre_llm_call"](user_message=text, session_id="synthetic", future_field=True))
        sent = []
        def local_provider(kwargs):
            sent.append(kwargs)
            return {"content": "Risposta di test"}
        result = ctx.middleware["llm_execution"](request, local_provider, session_id="synthetic")
        self.assertEqual(result["content"], "Risposta di test")
        self.assertNotIn("Mario Rossi", repr(sent))
        self.assertNotIn("mario.rossi@example.com", repr(sent))
        self.assertIn("<EMAIL_ADDRESS>", repr(sent))
        self.assertEqual(request, original)
        self.assertEqual(sent[0]["model"], "local-test")
        self.assertTrue(sent[0]["stream"])


    def test_responses_api_text_is_redacted(self):
        ctx = PluginContext()
        register(ctx)
        request = {
            "model": "local-test",
            "instructions": "Invia una copia a private@example.com.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Email user@example.com"}]},
                {"type": "function_call_output", "call_id": "call_private@example.com", "output": "Contatto user@example.com"},
            ],
        }
        sent = ctx.middleware["llm_execution"](request, lambda filtered: filtered)
        self.assertEqual(sent["instructions"], request["instructions"])
        self.assertNotIn("user@example.com", sent["input"][0]["content"][0]["text"])
        self.assertNotIn("user@example.com", sent["input"][1]["output"])
        self.assertIn("<EMAIL_ADDRESS>", sent["input"][1]["output"])
        self.assertEqual(sent["input"][1]["call_id"], "call_private@example.com")


    def test_instructions_assistant_messages_and_tool_arguments_are_preserved(self):
        from unittest.mock import patch
        from hermes_pii.plugin import PiiPlugin
        plugin = PiiPlugin()
        text = "Contact private@example.com"
        excluded = [
            {"role": role, "content": text}
            for role in ("system", "developer", "assistant")
        ] + [
            {"content": text},
            {"type": "function_call", "arguments": text},
        ]
        for field in ("messages", "input"):
            with self.subTest(field=field):
                user = {"role": "user", "content": text}
                request = {field: excluded + [user], "instructions": text}
                with patch.object(plugin, "_redact_text", return_value="<EMAIL_ADDRESS>") as redact:
                    result = plugin.llm_request(request)["request"]
                redact.assert_called_once_with(text)
                self.assertEqual(result[field][:-1], excluded)
                self.assertEqual(result[field][-1]["content"], "<EMAIL_ADDRESS>")
                self.assertEqual(result["instructions"], text)
                self.assertEqual(user["content"], text)
        with patch.object(plugin, "_redact_text", return_value="<EMAIL_ADDRESS>") as redact:
            result = plugin.llm_request({"input": text, "instructions": text})["request"]
        redact.assert_called_once_with(text)
        self.assertEqual(result["input"], "<EMAIL_ADDRESS>")
        self.assertEqual(result["instructions"], text)


    def test_missing_model_passes_original_text_and_retries_without_logging_pii(self):
        import socket
        from unittest.mock import patch
        from hermes_pii.plugin import PiiPlugin
        text = "Contact test@example.com."
        request = {"messages": [{"role": "user", "content": text}]}
        plugin = PiiPlugin(model_name="absent_model_for_offline_test", error_policy="permissive")
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                for _ in range(2):
                    sent = plugin.llm_request(request=request)
                    self.assertEqual(sent["request"], request)
                    self.assertEqual(sent["source"], "hermes-pii")
                    self.assertEqual(
                        sent["reason"],
                        "WARNING: permissive PII policy sent unfiltered text",
                    )
        self.assertEqual(len(logs.output), 1)
        for record in logs.records:
            self.assertIsNone(record.exc_info)
            self.assertIn("OSError", record.getMessage())
            self.assertNotIn(text, record.getMessage())
            self.assertNotIn("absent_model_for_offline_test", record.getMessage())
        self.assertEqual(request["messages"][0]["content"], text)

    def test_missing_model_does_not_make_preparation_raise(self):
        from hermes_pii.plugin import PiiPlugin
        plugin = PiiPlugin(model_name="absent_model_for_offline_test")
        with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
            self.assertIsNone(plugin.pre_llm_call(user_message="A harmless test prompt."))
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("A harmless test prompt", logs.output[0])


if __name__ == "__main__":
    unittest.main()
