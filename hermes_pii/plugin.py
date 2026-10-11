"""Offline preparation and explicit, fail-open provider request rewriting."""
import logging
from threading import Lock

from .attachments import is_media_content_part, sanitize_content_part
from .office_references import expand_office_references

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

    def _redact_user_text(self, text, *, responses=False):
        expanded = expand_office_references(
            text, detect_spans=lambda value: self._get_redactor().detect_spans(value),
            language=self._settings[0], responses=responses,
        )
        if isinstance(expanded, str):
            return self._redact_text(expanded)
        for part in expanded:
            if part["type"] in {"text", "input_text"}:
                part["text"] = self._redact_text(part["text"])
        return expanded

    def _redact_content(self, content, *, responses=False):
        if isinstance(content, str):
            return self._redact_user_text(content, responses=responses)
        if isinstance(content, list):
            updated = []
            for part in content:
                result = part
                if isinstance(part, dict) and is_media_content_part(part):
                    result = sanitize_content_part(
                        part,
                        detect_spans=lambda text: self._get_redactor().detect_spans(text),
                        language=self._settings[0],
                    )
                elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
                    text = self._redact_user_text(part["text"], responses=responses)
                    result = text if isinstance(text, list) else {**part, "text": text}
                updated.extend(result if isinstance(result, list) and result is not part else [result])
            return updated
        return content

    def _redact_message(self, message, *, responses=False):
        if message.get("role") != "user" or message.get("type", "message") != "message":
            return message
        if "content" not in message:
            return message
        return {**message, "content": self._redact_content(message["content"], responses=responses)}

    def llm_request(self, request, **kwargs):
        """Copy only rewritten branches, preserving shared input even after Hermes' shallow-copy fallback."""
        updated = dict(request)
        for key in ("messages", "input"):
            messages = updated.get(key)
            if isinstance(messages, list):
                updated[key] = [
                    self._redact_message(message, responses=key == "input") if isinstance(message, dict) else message
                    for message in messages
                ]
        # Responses string input becomes a user message only when Office pages are attached.
        if isinstance(updated.get("input"), str):
            content = self._redact_user_text(updated["input"], responses=True)
            updated["input"] = [{"role": "user", "content": content}] if isinstance(content, list) else content
        return {
            "request": updated,
            "source": "hermes-pii",
            "reason": "Local PII filtering with local attachment sanitization",
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
