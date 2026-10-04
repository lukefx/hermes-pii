"""Sanitize multimodal request parts before Hermes sends them to a provider."""
from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass
from io import BytesIO

logger = logging.getLogger(__name__)

_IMAGE_TYPES = frozenset({"image_url", "input_image", "image"})
_UNSUPPORTED_MEDIA_TYPES = frozenset({
    "audio", "audio_url", "input_audio", "input_file", "input_video", "video",
    "video_url", "file", "file_url",
})
_MEDIA_TYPES = _IMAGE_TYPES | _UNSUPPORTED_MEDIA_TYPES
_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)
_OMITTED_ATTACHMENT = (
    "[Attachment omitted because Hermes PII could not inspect it locally.]"
)
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000
_OCR_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class OCRWord:
    text_start: int
    text_end: int
    left: int
    top: int
    right: int
    bottom: int


def is_media_content_part(part):
    """Return whether a provider content part carries supported or opaque media."""
    return isinstance(part, dict) and part.get("type") in _MEDIA_TYPES


def sanitize_content_part(part, *, detect_spans, language="en"):
    """Redact raster image PII; replace opaque or unsupported media parts with safe text."""
    part_type = part.get("type")
    if part_type in _UNSUPPORTED_MEDIA_TYPES:
        logger.warning("PII attachment omitted because local sanitization is unavailable")
        return {"type": "text", "text": _OMITTED_ATTACHMENT}
    if part_type not in _IMAGE_TYPES:
        return part

    try:
        updated, inspected = _sanitize_image_part(part, detect_spans=detect_spans, language=language)
        if inspected:
            return updated
    except Exception as error:
        logger.warning(
            "PII image attachment omitted after local inspection failed (%s)",
            type(error).__name__,
        )
    else:
        logger.warning("PII image attachment omitted because its data was not locally inspectable")
    return {"type": "text", "text": _OMITTED_ATTACHMENT}


def _sanitize_image_part(part, *, detect_spans, language):
    part_type = part["type"]
    if part_type == "image":
        source = part.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            return part, False
        mime = source.get("media_type")
        encoded = source.get("data")
        if not isinstance(mime, str) or not mime.startswith("image/") or not isinstance(encoded, str):
            return part, False
        raw = _decode_base64(encoded)
        safe, changed = sanitize_image_bytes(raw, detect_spans=detect_spans, language=language)
        if not changed:
            return part, True
        updated = dict(part)
        updated_source = dict(source)
        updated_source["media_type"] = "image/png"
        updated_source["data"] = base64.b64encode(safe).decode("ascii")
        updated["source"] = updated_source
        return updated, True

    image_spec = part.get("image_url")
    if isinstance(image_spec, dict):
        url = image_spec.get("url")
    else:
        url = image_spec
    if not isinstance(url, str):
        return part, False
    match = _DATA_URL_RE.fullmatch(url)
    if not match:
        return part, False
    raw = _decode_base64(match.group(2))
    safe, changed = sanitize_image_bytes(raw, detect_spans=detect_spans, language=language)
    if not changed:
        return part, True

    updated = dict(part)
    data_url = "data:image/png;base64," + base64.b64encode(safe).decode("ascii")
    if isinstance(image_spec, dict):
        updated_spec = dict(image_spec)
        updated_spec["url"] = data_url
        updated["image_url"] = updated_spec
    else:
        updated["image_url"] = data_url
    return updated, True


def sanitize_image_bytes(raw, *, detect_spans, language="en"):
    """Black out OCR boxes for every detected span; return ``(PNG bytes, changed)``."""
    from PIL import Image, ImageDraw, ImageOps

    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError("image exceeds local inspection size limit")
    with Image.open(BytesIO(raw)) as opened:
        if getattr(opened, "n_frames", 1) != 1:
            raise ValueError("animated images require frame-by-frame sanitization")
        if opened.width * opened.height > _MAX_IMAGE_PIXELS:
            raise ValueError("image exceeds local inspection pixel limit")
        image = ImageOps.exif_transpose(opened).convert("RGB")

    text, words = _ocr_words(image, language)
    spans = detect_spans(text)
    if not spans:
        return raw, False

    draw = ImageDraw.Draw(image)
    margin = max(2, round(min(image.size) * 0.002))
    redacted = 0
    for start, end, _entity_type in spans:
        matching = [word for word in words if word.text_start < end and word.text_end > start]
        if not matching:
            raise ValueError("PII span did not map to OCR word boxes")
        left = max(0, min(word.left for word in matching) - margin)
        top = max(0, min(word.top for word in matching) - margin)
        right = min(image.width - 1, max(word.right for word in matching) + margin)
        bottom = min(image.height - 1, max(word.bottom for word in matching) + margin)
        draw.rectangle((left, top, right, bottom), fill="black")
        redacted += 1

    if not redacted:
        return raw, False
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), True


def _decode_base64(encoded):
    if len(encoded) > ((_MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("image exceeds local inspection size limit")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("invalid image data") from error


def _ocr_words(image, language):
    import pytesseract
    from pytesseract import Output

    lang = {"en": "eng", "it": "ita", "de": "deu", "fr": "fra", "es": "spa"}.get(
        language, language
    )
    data = pytesseract.image_to_data(
        image,
        lang=lang,
        output_type=Output.DICT,
        timeout=_OCR_TIMEOUT_SECONDS,
    )
    words = []
    pieces = []
    cursor = 0
    for index, raw_text in enumerate(data.get("text", [])):
        token = str(raw_text or "").strip()
        if not token:
            continue
        left, top, width, height = (
            int(data[key][index]) for key in ("left", "top", "width", "height")
        )
        if width <= 0 or height <= 0:
            continue
        if pieces:
            pieces.append(" ")
            cursor += 1
        start = cursor
        pieces.append(token)
        cursor += len(token)
        words.append(OCRWord(start, cursor, left, top, left + width, top + height))
    return "".join(pieces), words
