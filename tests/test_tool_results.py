"""Check document tool results at the outgoing provider boundary."""
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from hermes_pii.plugin import PiiPlugin

TEXT = "Ciao sono Mario e il numero di telefono è 0797770099."


class FakeRedactor:
    def redact(self, text):
        return text.replace("Mario", "<PERSON>").replace("0797770099", "<PHONE_NUMBER>")


class ToolResultTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PiiPlugin()
        self.plugin._redactor = FakeRedactor()

    def test_tool_text_and_json_are_filtered_without_changing_protocol_or_history(self):
        payload = {"content": TEXT, "total_lines": 1, "extracted_document": True,
                   "nested": [{"0797770099": TEXT}], "file_size": 1400, "error": None}
        for field, message, content_key in (
            ("messages", {"role": "tool", "tool_call_id": "0797770099", "name": "read_file"}, "content"),
            ("messages", {"role": "function", "name": "read_file"}, "content"),
            ("input", {"type": "function_call_output", "call_id": "0797770099", "status": "completed"}, "output"),
        ):
            for content in (TEXT, json.dumps(payload, ensure_ascii=False),
                            [{"type": "text", "text": TEXT, "cache_control": {"type": "ephemeral"}}]):
                with self.subTest(field=field, content=content):
                    request = {field: [{**message, content_key: content}]}
                    original = deepcopy(request)
                    sent = self.plugin.llm_request(request)["request"][field][0]
                    for key, value in message.items():
                        self.assertEqual(sent[key], value)
                    self.assertEqual(request, original)
                    if isinstance(content, str) and content.startswith("{"):
                        parsed = json.loads(sent[content_key])
                        self.assertEqual(parsed["total_lines"], 1)
                        self.assertIs(parsed["extracted_document"], True)
                        self.assertIsNone(parsed["error"])
                        self.assertIn("0797770099", parsed["nested"][0])
                        filtered = parsed["content"]
                    elif isinstance(content, list):
                        filtered = sent[content_key][0]["text"]
                        self.assertEqual(sent[content_key][0]["cache_control"], content[0]["cache_control"])
                    else:
                        filtered = sent[content_key]
                    self.assertNotIn("Mario", filtered)
                    self.assertNotIn("0797770099", filtered)
                    self.assertIn("<PERSON>", filtered)
                    self.assertIn("<PHONE_NUMBER>", filtered)

    def test_tool_result_failure_is_omitted_without_logging_content(self):
        with patch.object(self.plugin, "_get_redactor", side_effect=RuntimeError(TEXT)):
            with self.assertLogs("hermes_pii.plugin", level="WARNING") as logs:
                sent = self.plugin.llm_request({"messages": [{"role": "tool", "content": TEXT}]})
        self.assertNotIn(TEXT, repr(sent))
        self.assertIn("Tool result omitted", repr(sent))
        self.assertNotIn(TEXT, str(logs.output))
        recovered = self.plugin.llm_request({"messages": [{"role": "tool", "content": TEXT}]})
        self.assertIn("<PHONE_NUMBER>", repr(recovered))

    def test_unchanged_json_retains_its_original_formatting(self):
        content = '{ "total_lines": 1, "content": "Nothing sensitive here" }'
        sent = self.plugin.llm_request({"messages": [{"role": "tool", "content": content}]})
        self.assertEqual(sent["request"]["messages"][0]["content"], content)

    def test_tool_text_does_not_expand_local_file_references(self):
        with patch("hermes_pii.plugin.expand_office_references") as expand:
            self.plugin.llm_request({"messages": [{"role": "tool", "content": '@file:"private.docx"'}]})
        expand.assert_not_called()

    @unittest.skipUnless(os.environ.get("HERMES_PII_PDF_SMOKE") == "1",
                         "requires Hermes document extraction and the Italian NLP model")
    def test_real_italian_pdf_read_is_filtered_before_provider_call(self):
        from tools.file_tools import read_file_tool

        pdf = Path(__file__).parent / "fixtures" / "pii-italian.pdf"
        payload = read_file_tool(str(pdf.resolve()), task_id="hermes-pii-pdf-smoke")
        extracted = json.loads(payload)
        self.assertTrue(extracted["extracted_document"])
        self.assertIn(TEXT, extracted["content"])
        for field, message in (
            ("messages", {"role": "tool", "tool_call_id": "pdf-read", "content": payload}),
            ("input", {"type": "function_call_output", "call_id": "pdf-read", "output": payload}),
        ):
            with self.subTest(field=field):
                request = {field: [message]}
                original = deepcopy(request)
                plugin = PiiPlugin(language="it", model_name="it_core_news_sm")
                with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
                    sent = plugin.llm_request(request)["request"]
                received = []
                def provider(**kwargs):
                    received.append(kwargs)
                provider(**sent)
                self.assertNotIn("Mario", repr(received))
                self.assertNotIn("0797770099", repr(received))
                self.assertIn("<PERSON>", repr(received))
                self.assertIn("<PHONE_NUMBER>", repr(received))
                self.assertEqual(request, original)
