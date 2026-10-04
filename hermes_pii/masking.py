"""Replace detector spans without encryption dependencies or recovery mappings."""

STRUCTURED_ENTITIES = frozenset({
    "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "CREDIT_CARD", "IP_ADDRESS",
})


def detected_span_groups(text, detections):
    """Return merged ``(start, end, entity_type)`` spans, preferring structured PII."""
    groups = []
    for detection in sorted(detections, key=lambda item: (item.start, -item.end)):
        if not (
            type(detection.start) is int
            and type(detection.end) is int
            and 0 <= detection.start < detection.end <= len(text)
        ):
            raise ValueError("Invalid detector span offsets")
        rank = (detection.entity_type in STRUCTURED_ENTITIES, detection.score, detection.entity_type)
        if groups and detection.start < groups[-1][1]:
            group = groups[-1]
            group[1] = max(group[1], detection.end)
            if rank > group[3]:
                group[2], group[3] = detection.entity_type, rank
        else:
            groups.append([detection.start, detection.end, detection.entity_type, rank])
    return [(start, end, entity_type) for start, end, entity_type, _ in groups]


def replace_detected_spans(text, detections):
    """Mask the full union of overlaps; prefer structured-PII placeholder types."""
    pieces = []
    cursor = 0
    for start, end, entity_type in detected_span_groups(text, detections):
        pieces.extend((text[cursor:start], f"<{entity_type}>"))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)
