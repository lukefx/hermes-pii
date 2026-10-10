"""Synthetic image and opaque-media tests for the outgoing provider boundary."""
import base64
from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from hermes_pii.plugin import PiiPlugin


class FakeRedactor:
    def detect_spans(self, text):
        return [(0, len(text), "EMAIL_ADDRESS")]


def white_png():
    output = BytesIO()
    Image.new("RGB", (120, 60), "white").save(output, format="PNG")
    return output.getvalue()


class AttachmentTests(unittest.TestCase):
    def test_data_image_is_redacted_without_mutating_original_request(self):
        from hermes_pii.attachments import OCRWord

        encoded = base64.b64encode(white_png()).decode("ascii")
        request = {
            "model": "local-test",
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    "detail": "high",
                }],
            }],
        }
        original = deepcopy(request)
        plugin = PiiPlugin()
        with (
            patch.object(plugin, "_get_redactor", return_value=FakeRedactor()),
            patch(
                "hermes_pii.attachments._ocr_words",
                return_value=("alice@example.com", [OCRWord(0, 17, 10, 10, 100, 30)]),
            ),
        ):
            sent = plugin.llm_request(request=request)["request"]

        sent_part = sent["messages"][0]["content"][0]
        self.assertEqual(sent_part["type"], "image_url")
        self.assertEqual(sent_part["detail"], "high")
        self.assertTrue(sent_part["image_url"]["url"].startswith("data:image/png;base64,"))
        sanitized = base64.b64decode(sent_part["image_url"]["url"].split(",", 1)[1])
        with Image.open(BytesIO(sanitized)) as image:
            self.assertEqual(image.getpixel((50, 20)), (0, 0, 0))
            self.assertEqual(image.getpixel((110, 50)), (255, 255, 255))
        self.assertEqual(request, original)
        self.assertNotIn(encoded, repr(sent))

    def test_uninspectable_images_and_opaque_media_are_omitted(self):
        plugin = PiiPlugin()
        request = {"input": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": "https://files.example/private.png"},
                {"type": "input_file", "file_id": "secret-file-id"},
                {"type": "input_audio", "data": "private-audio-payload"},
            ],
        }]}

        sent = plugin.llm_request(request=request)["request"]
        parts = sent["input"][0]["content"]
        self.assertEqual([part["type"] for part in parts], ["text", "text", "text"])
        self.assertNotIn("files.example", repr(sent))
        self.assertNotIn("secret-file-id", repr(sent))
        self.assertNotIn("private-audio-payload", repr(sent))

    def test_non_user_attachments_are_preserved_without_inspection(self):
        plugin = PiiPlugin()
        messages = [
            {"role": role, "content": [{"type": "input_image", "image_url": "opaque"}]}
            for role in ("system", "developer", "assistant", "tool")
        ] + [{"content": [{"type": "input_file", "file_id": "opaque"}]}]
        for field in ("messages", "input"):
            with self.subTest(field=field):
                with patch("hermes_pii.plugin.sanitize_content_part") as sanitize:
                    sent = plugin.llm_request({field: messages})["request"]
                sanitize.assert_not_called()
                self.assertEqual(sent[field], messages)

    def test_detector_initialization_error_omits_image(self):
        from hermes_pii.attachments import OCRWord

        encoded = base64.b64encode(white_png()).decode("ascii")
        request = {"messages": [{"role": "user", "content": [{
            "type": "input_image",
            "image_url": f"data:image/png;base64,{encoded}",
        }]}]}
        plugin = PiiPlugin()
        with (
            patch.object(plugin, "_get_redactor", side_effect=RuntimeError("synthetic failure")),
            patch(
                "hermes_pii.attachments._ocr_words",
                return_value=("alice@example.com", [OCRWord(0, 17, 10, 10, 100, 30)]),
            ),
        ):
            sent = plugin.llm_request(request=request)["request"]

        part = sent["messages"][0]["content"][0]
        self.assertEqual(part["type"], "text")
        self.assertIn("could not inspect it locally", part["text"])
        self.assertNotIn(encoded, repr(sent))


if __name__ == "__main__":
    unittest.main()
