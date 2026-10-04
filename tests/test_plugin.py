"""Synthetic provider boundary: real Presidio, no remote LLM calls."""
from copy import deepcopy
import unittest

from hermes_pii import register


class PluginContext:
    def __init__(self):
        self.hooks = {}
        self.middleware = {}

    def get_config(self, key, default=None):
        return default

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_middleware(self, name, callback):
        self.middleware[name] = callback


class PluginTests(unittest.TestCase):
    def test_hook_and_middleware_remove_pii_from_chat_provider_request(self):
        ctx = PluginContext()
        register(ctx)
        self.assertIn("pre_llm_call", ctx.hooks)
        self.assertIn("llm_request", ctx.middleware)
        text = "Mi chiamo Mario Rossi, email mario.rossi@example.com."
        request = {"model": "local-test", "stream": True, "messages": [
            {"role": "system", "content": "Rispondi in italiano."},
            {"role": "user", "content": text},
        ]}
        original = deepcopy(request)
        # Returning redacted text here would APPEND it, leaving the raw prompt.
        self.assertIsNone(ctx.hooks["pre_llm_call"](user_message=text, session_id="synthetic", future_field=True))
        update = ctx.middleware["llm_request"](request=request, session_id="synthetic")
        sent = []
        def local_provider(**kwargs):
            sent.append(kwargs)
            return {"content": "Risposta di test"}
        result = local_provider(**update["request"])
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
        sent = ctx.middleware["llm_request"](request=request)["request"]
        self.assertNotIn("private@example.com", sent["instructions"])
        self.assertNotIn("user@example.com", sent["input"][0]["content"][0]["text"])
        self.assertNotIn("user@example.com", sent["input"][1]["output"])
        self.assertEqual(sent["input"][1]["call_id"], "call_private@example.com")


    def test_tool_arguments_keep_json_keys_and_protocol_identifiers(self):
        import json
        ctx = PluginContext()
        register(ctx)
        arguments = json.dumps({"email": "private@example.com", "nested": ["other@example.com"], "count": 3})
        request = {"messages": [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_123", "type": "function", "function": {"name": "send_email", "arguments": arguments}},
        ]}], "input": [{"type": "function_call", "call_id": "call_123", "name": "send_email", "arguments": arguments}]}
        sent = ctx.middleware["llm_request"](request=request)["request"]
        chat_call = sent["messages"][0]["tool_calls"][0]
        self.assertNotIn("private@example.com", chat_call["function"]["arguments"])
        self.assertNotIn("other@example.com", chat_call["function"]["arguments"])
        parsed = json.loads(chat_call["function"]["arguments"])
        self.assertEqual(set(parsed), {"email", "nested", "count"})
        self.assertEqual(parsed["count"], 3)
        self.assertEqual(chat_call["id"], "call_123")
        self.assertEqual(chat_call["function"]["name"], "send_email")
        self.assertNotIn("private@example.com", sent["input"][0]["arguments"])


    def test_missing_model_passes_original_text_and_retries_without_logging_pii(self):
        import socket
        from unittest.mock import patch
        from hermes_pii.plugin import PiiPlugin
        text = "Contact test@example.com."
        request = {"messages": [{"role": "user", "content": text}]}
        plugin = PiiPlugin(model_name="absent_model_for_offline_test")
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                for _ in range(2):
                    sent = plugin.llm_request(request=request)
                    self.assertEqual(sent["request"], request)
                    self.assertEqual(sent["source"], "hermes-pii")
                    self.assertEqual(
                        sent["reason"],
                        "Local PII filtering with local attachment sanitization",
                    )
        self.assertEqual(len(logs.output), 2)
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
