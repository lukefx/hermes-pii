"""Offline preparation and explicit provider request rewriting."""
import json
import logging
from contextvars import ContextVar
from importlib import import_module
from threading import Lock
from typing import Callable

from .attachments import _OMITTED_ATTACHMENT, is_media_content_part, sanitize_content_part
from .office_references import expand_office_references

logger = logging.getLogger(__name__)
_failures: ContextVar[list[str] | None] = ContextVar("hermes_pii_failures", default=None)


def _require_abort_support() -> Callable[..., Exception]:
    try:
        MiddlewareAbort = getattr(import_module("hermes_cli.middleware"), "MiddlewareAbort")
    except (ImportError, AttributeError):
        raise RuntimeError("Strict PII mode requires Hermes MiddlewareAbort support; plugin not registered") from None
    if not isinstance(MiddlewareAbort, type) or not issubclass(MiddlewareAbort, Exception):
        raise RuntimeError("Strict PII mode requires Hermes MiddlewareAbort support; plugin not registered")
    return MiddlewareAbort


def _verify_redactor(candidate):
    try:
        probe = candidate.redact("Contact hermes-pii-check@example.com.")
        if "hermes-pii-check@example.com" in probe or "<EMAIL_ADDRESS>" not in probe:
            raise RuntimeError("PII detector self-check failed")
    except Exception:
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        raise


class PiiPlugin:
    """Cache one detector; keep failure handling local to each operation."""

    def __init__(self, language="en", model_name="en_core_web_sm", score_threshold=0.4, error_policy="strict", runtime_factory=None):
        if error_policy not in {"strict", "permissive"}:
            raise ValueError("error_policy must be strict or permissive")
        self._settings = (language, model_name, score_threshold)
        self._runtime_factory = runtime_factory
        self._error_policy = error_policy
        self._redactor = None
        self._lock = Lock()
        self._warned = set()

    def _warn_failure(self, error):
        error_type = type(error).__name__
        failures = _failures.get()
        if failures is not None:
            failures.append(error_type)
        with self._lock:
            if error_type in self._warned:
                return
            self._warned.add(error_type)
        policy = "strict policy blocks this request" if self._error_policy == "strict" else "permissive policy sends unfiltered text"
        logger.warning("PII protection degraded (%s); %s", error_type, policy)

    def _get_redactor(self):
        with self._lock:
            if self._redactor is None:
                if self._runtime_factory is None:
                    from .redactor import PresidioRedactor
                    candidate = PresidioRedactor(*self._settings)
                else:
                    candidate = self._runtime_factory()
                _verify_redactor(candidate)
                self._redactor = candidate
            return self._redactor

    def check_health(self):
        """Report detector readiness without exposing exception messages or input."""
        try:
            self._get_redactor()
        except Exception as error:
            return {"ready": False, "error_type": type(error).__name__}
        return {"ready": True, "error_type": None}

    def pre_llm_call(self, user_message="", **kwargs):
        """Warm the local detector without analyzing or appending user text."""
        try:
            self._get_redactor()
        except Exception as error:
            self._warn_failure(error)

    def _redact_text(self, text):
        if not text:
            return text
        try:
            return self._get_redactor().redact(text)
        except Exception as error:
            if self._error_policy == "strict":
                raise
            self._warn_failure(error)
            return text

    def _redact_user_text(self, text, *, responses=False):
        expanded = expand_office_references(
            text, detect_spans=self._detect_spans,
            language=self._settings[0], responses=responses, sanitize=self._sanitize_part,
        )
        if isinstance(expanded, str):
            return self._redact_text(expanded)
        for part in expanded:
            if part["type"] in {"text", "input_text"}:
                part["text"] = self._redact_text(part["text"])
        return expanded

    def _detect_spans(self, text):
        try:
            return self._get_redactor().detect_spans(text)
        except Exception as error:
            self._warn_failure(error)
            raise

    def _redact_tool_text(self, text):
        """Filter tool text, retaining JSON keys and numeric/boolean metadata."""
        if not text:
            return text
        try:
            detector = self._get_redactor()

            def redact_values(value):
                if isinstance(value, str):
                    return detector.redact(value)
                if isinstance(value, list):
                    return [redact_values(item) for item in value]
                if isinstance(value, dict):
                    return {key: redact_values(item) for key, item in value.items()}
                return value

            try:
                payload = json.loads(text)
            except ValueError:
                return detector.redact(text)
            if not isinstance(payload, (dict, list)):
                return detector.redact(text)
            filtered = redact_values(payload)
            return json.dumps(filtered, ensure_ascii=False) if filtered != payload else text
        except Exception as error:
            if self._error_policy == "strict":
                raise
            self._warn_failure(error)
            return text

    def _sanitize_part(self, part, **kwargs):
        if self._runtime_factory is None:
            return sanitize_content_part(part, detect_spans=self._detect_spans, language=self._settings[0])
        try:
            result = getattr(self._get_redactor(), "sanitize_part")(part, language=self._settings[0])
            if result["detector_error"]:
                self._warn_failure(RuntimeError())
            return result["part"]
        except Exception as error:
            self._warn_failure(error)
            text_type = "input_text" if part.get("type") == "input_file" else "text"
            return {"type": text_type, "text": _OMITTED_ATTACHMENT}

    def _redact_content(self, content, *, responses=False, tool_output=False):
        redact_text = self._redact_tool_text if tool_output else lambda text: self._redact_user_text(text, responses=responses)
        if isinstance(content, str):
            return redact_text(content)
        if isinstance(content, list):
            updated = []
            for part in content:
                result = part
                if isinstance(part, dict) and is_media_content_part(part):
                    result = self._sanitize_part(part)
                elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
                    text = redact_text(part["text"])
                    result = text if isinstance(text, list) else {**part, "text": text}
                updated.extend(result if isinstance(result, list) and result is not part else [result])
            return updated
        return content

    def _redact_message(self, message, *, responses=False):
        if responses and message.get("type") == "function_call_output":
            if "output" not in message:
                return message
            return {**message, "output": self._redact_content(message["output"], responses=True, tool_output=True)}
        role = message.get("role")
        if role not in {"user", "tool", "function"} or message.get("type", "message") != "message":
            return message
        if "content" not in message:
            return message
        return {**message, "content": self._redact_content(
            message["content"], responses=responses, tool_output=role in {"tool", "function"},
        )}

    def llm_request(self, request, **kwargs):
        failures = []
        token = _failures.set(failures)
        try:
            updated = self._rewrite_request(request)
        except Exception as error:
            if self._error_policy != "strict":
                raise
            self._warn_failure(error)
            updated = {"_hermes_pii_blocked": True}
        finally:
            _failures.reset(token)
        if failures and self._error_policy == "strict":
            updated = {"_hermes_pii_blocked": True}
        if not failures:
            with self._lock:
                self._warned.clear()
        return {
            "request": updated,
            "source": "hermes-pii",
            "reason": ("PII inspection failed; request blocked locally" if updated.get("_hermes_pii_blocked")
                       else "WARNING: permissive PII policy sent unfiltered text" if failures
                       else "Local PII filtering with local attachment sanitization"),
        }

    def llm_execution(self, request, next_call, **kwargs):
        """Inspect at the execution boundary, where policy aborts cannot fail open."""
        abort_type = _require_abort_support() if self._error_policy == "strict" else None
        try:
            filtered = self.llm_request(request)["request"]
        except Exception:
            if abort_type is None:
                raise
            filtered = {"_hermes_pii_blocked": True}
        if abort_type is not None and filtered.get("_hermes_pii_blocked"):
            raise abort_type(
                "Hermes PII blocked this turn because local inspection failed. "
                "No provider request was sent for this attempt. Check the configured NLP model.",
                code="pii_inspection_failed",
            ) from None
        return next_call(filtered)

    def _rewrite_request(self, request):
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
        return updated


def register(ctx):
    """Register callbacks without importing NLP libraries or loading a model."""
    if hasattr(ctx, "get_config"):
        get_setting = ctx.get_config
    else:
        from hermes_cli.config import load_config
        settings = (load_config() or {}).get("plugins", {}).get("entries", {}).get("hermes-pii", {}).get("settings", {})
        get_setting = settings.get
    runtime_factory = None
    if hasattr(ctx, "register_cli_command"):
        from .cli import make_runtime, read_settings, register_command
        register_command(ctx)
        settings = read_settings(ctx)
        runtime_factory = lambda: make_runtime(settings)
    plugin = PiiPlugin(
        language=get_setting("language", "en"),
        model_name=get_setting("model_name", "en_core_web_sm"),
        score_threshold=get_setting("score_threshold", 0.4),
        error_policy=get_setting("error_policy", "strict"),
        runtime_factory=runtime_factory,
    )
    if plugin._error_policy == "strict":
        try:
            _require_abort_support()
        except RuntimeError:
            if not hasattr(ctx, "register_cli_command"):
                raise
            logger.warning("PII CLI available; filtering not registered because this host lacks MiddlewareAbort support")
            return
    ctx.register_hook("pre_llm_call", plugin.pre_llm_call)
    ctx.register_middleware("llm_execution", plugin.llm_execution)
