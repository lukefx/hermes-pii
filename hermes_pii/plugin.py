"""Prepare the local detector and rewrite outgoing text and attachment content."""
from copy import deepcopy
import json
import logging
from threading import Lock

from .attachments import is_media_content_part, sanitize_content_part

logger = logging.getLogger(__name__)


def _warn(error):
    logger.warning(
        "PII filtering failed (%s); original text is passed through. Future calls retry.",
        type(error).__name__,
    )


class PiiPlugin:
    """Cache one detector; keep failure handling local to each operation."""

    def __init__(self, language="en", model_name="en_core_web_sm", score_threshold=0.4):
        self._settings = (language, model_name, score_threshold)
        self._redactor = None
        self._lock = Lock()

    def _get_redactor(self):
        with self._lock:
            if self._redactor is None:
                from .redactor import PresidioRedactor
                self._redactor = PresidioRedactor(*self._settings)
            return self._redactor

    def pre_llm_call(self, user_message="", **kwargs):
        """Warm the local detector without analyzing or appending user text."""
        try:
            self._get_redactor()
        except Exception as error:
            _warn(error)

    def _redact_text(self, text):
        if not text:
            return text
        try:
            return self._get_redactor().redact(text)
        except Exception as error:
            _warn(error)
            return text

    def _redact_content(self, content):
        if isinstance(content, str):
            return self._redact_text(content)
        if isinstance(content, list):
            for index, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                if is_media_content_part(part):
                    content[index] = sanitize_content_part(
                        part,
                        detect_spans=lambda text: self._get_redactor().detect_spans(text),
                        language=self._settings[0],
                    )
                    continue
                if (
                    part.get("type") in ("text", "input_text", "output_text")
                    and isinstance(part.get("text"), str)
                ):
                    part["text"] = self._redact_text(part["text"])
        return content

    def _redact_json_values(self, value):
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, list):
            return [self._redact_json_values(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_json_values(item) for key, item in value.items()}
        return value

    def _redact_arguments(self, arguments):
        if not isinstance(arguments, str):
            return arguments
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return self._redact_text(arguments)
        result = self._redact_json_values(parsed)
        return arguments if result == parsed else json.dumps(result, ensure_ascii=False)

    def _redact_message(self, message):
        if "content" in message:
            message["content"] = self._redact_content(message["content"])
        if message.get("type") == "function_call_output" and "output" in message:
            message["output"] = self._redact_content(message["output"])
        if message.get("type") == "function_call" and "arguments" in message:
            message["arguments"] = self._redact_arguments(message["arguments"])
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            if "arguments" in function:
                function["arguments"] = self._redact_arguments(function["arguments"])

    def llm_request(self, request, **kwargs):
        """Rewrite supported text and media fields in an outgoing request copy."""
        updated = deepcopy(request)
        for key in ("instructions", "input"):
            if isinstance(updated.get(key), str):
                updated[key] = self._redact_text(updated[key])
        for key in ("messages", "input"):
            messages = updated.get(key)
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        self._redact_message(message)
        return {
            "request": updated,
            "source": "hermes-pii",
            "reason": "Local PII filtering with local attachment sanitization",
        }


def register(ctx):
    """Register callbacks without importing NLP libraries or loading a model."""
    plugin = PiiPlugin(
        language=ctx.get_config("language", default="en"),
        model_name=ctx.get_config("model_name", default="en_core_web_sm"),
        score_threshold=ctx.get_config("score_threshold", default=0.4),
    )
    ctx.register_hook("pre_llm_call", plugin.pre_llm_call)
    ctx.register_middleware("llm_request", plugin.llm_request)
