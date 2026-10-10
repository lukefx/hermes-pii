"""Resolve explicit user Office references inside the Hermes process workspace."""
import base64
import logging
from pathlib import Path
import re

from .attachments import _OMITTED_ATTACHMENT, sanitize_content_part
from .office import MAX_OFFICE_BYTES, OFFICE_EXTENSIONS

logger = logging.getLogger(__name__)
_REFERENCE = re.compile(r'''(?<![\w/])@file:(?:"([^"\n]+)"|'([^'\n]+)'|`([^`\n]+)`|([^\s]+))''')
_MAX_REFERENCES = 4


def expand_office_references(text, *, detect_spans, language, responses=False):
    """Return text unchanged, or text/image parts replacing Office refs and binary stubs."""
    matches = [
        match for match in _REFERENCE.finditer(text)
        if Path(next(value for value in match.groups() if value is not None).rstrip(",;!?")).suffix.lower()
        in OFFICE_EXTENSIONS
    ]
    if not matches:
        return text
    parts, cursor = [], 0
    text_type = "input_text" if responses else "text"
    for index, match in enumerate(matches):
        start, end = match.span()
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start] == "📎 ":
            # Hermes' @file expansion produces a one-line binary stub instructing
            # tools to read the original. Replace that entire stub with safe pages.
            start = line_start
            end = text.find("\n", end)
            if end == -1:
                end = len(text)
        if start < cursor:
            continue
        parts.append({"type": text_type, "text": text[cursor:start]})
        try:
            if index >= _MAX_REFERENCES:
                raise ValueError("too many Office references")
            target = next(value for value in match.groups() if value is not None).rstrip(",;!?")
            workspace = Path.cwd().resolve()
            path = (workspace / target).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                raise ValueError("Office reference is outside the workspace or not a file")
            with path.open("rb") as source:
                raw = source.read(MAX_OFFICE_BYTES + 1)
            if len(raw) > MAX_OFFICE_BYTES:
                raise ValueError("Office file exceeds the size limit")
            spec = {"filename": "document" + path.suffix.lower(), "file_data": base64.b64encode(raw).decode("ascii")}
            part = {"type": "input_file", **spec} if responses else {"type": "file", "file": spec}
            result = sanitize_content_part(part, detect_spans=detect_spans, language=language)
            parts.extend(result if isinstance(result, list) else [result])
        except Exception as error:
            logger.warning("PII Office reference omitted (%s)", type(error).__name__)
            parts.append({"type": text_type, "text": _OMITTED_ATTACHMENT})
        cursor = end
    parts.append({"type": text_type, "text": text[cursor:]})
    return parts
