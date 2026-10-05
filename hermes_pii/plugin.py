"""Offline preparation and explicit, fail-open provider request rewriting."""
import logging
from threading import Lock

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
            updated = list(content)
            for index, part in enumerate(content):
                if not isinstance(part, dict) or part.get("type") not in ("text", "input_text", "output_text"):
                    continue
                if isinstance(part.get("text"), str):
                    updated[index] = {**part, "text": self._redact_text(part["text"])}
            return updated
        return content

    def _redact_message(self, message):
        logger.debug("Redacting message: %s", message)
        if message.get("role") != "user" or message.get("type", "message") != "message":
            return message
        if "content" not in message:
            return message
        return {**message, "content": self._redact_content(message["content"])}

    def llm_request(self, request, **kwargs):
        """Copy only rewritten branches, preserving shared input even after Hermes' shallow-copy fallback."""
        updated = dict(request)
        # A string Responses input is a user prompt; instructions are preserved.
        if isinstance(updated.get("input"), str):
            updated["input"] = self._redact_text(updated["input"])
        for key in ("messages", "input"):
            messages = updated.get(key)
            if isinstance(messages, list):
                updated[key] = [
                    self._redact_message(message) if isinstance(message, dict) else message
                    for message in messages
                ]
        return {
            "request": updated,
            "source": "hermes-pii",
            "reason": "Local PII filtering (fail-open)",
        }


def register(ctx):
    """Register callbacks without importing NLP libraries or loading a model."""
    if hasattr(ctx, "get_config"):
        get_setting = ctx.get_config
    else:
        from hermes_cli.config import load_config
        settings = (load_config() or {}).get("plugins", {}).get("entries", {}).get("hermes-pii", {}).get("settings", {})
        get_setting = settings.get
    plugin = PiiPlugin(
        language=get_setting("language", "en"),
        model_name=get_setting("model_name", "en_core_web_sm"),
        score_threshold=get_setting("score_threshold", 0.4),
    )
    ctx.register_hook("pre_llm_call", plugin.pre_llm_call)
    ctx.register_middleware("llm_request", plugin.llm_request)
