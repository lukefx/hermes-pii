# Design

## Responsibilities

- Users provision the default English spaCy model in the Hermes environment.
- Registration only installs callbacks; it does not import spaCy or load models.
- `pre_llm_call` prepares the detector, without analyzing or appending user text.
- `llm_request` rewrites a provider-bound copy using an explicit field whitelist.
- `PresidioRedactor` owns offline detection and serialized inference.
- `masking.py` replaces full overlapping-span unions with entity-type placeholders.
- `attachments.py` routes media parts and sanitizes raster pixels using injected detection.
- `office.py` validates DOCX/XLSX packages, converts them to PDF using an isolated
  LibreOffice profile, and yields bounded PNG pages using Poppler.
- `office_references.py` resolves explicit user Office references inside the process
  workspace and replaces Hermes' binary preview stubs with filtered image parts.

The plugin locks lazy construction; the redactor locks inference. There is no
permanent failure flag, recovery mapping, or request/result cache.

## Model loading

The default English model is provisioned separately from its GitHub release wheel.
Hermes' dependency installer does not resolve `[tool.uv.sources]`, and the model
has no PyPI releases. Package dependencies therefore exclude the model.
Runtime code calls `spacy.load()` and supplies the installed pipeline to Presidio,
avoiding its stock loader's download path. Email validation uses a bundled
suffix snapshot with remote URLs and disk caching disabled. Custom models must
already be installed or provided through a local model directory. The optional
Italian model is not installed by default.

No `nlp.initialize()` or training is needed for a pretrained model. Missing
models raise in the redactor and trigger the plugin's fail-open fallback.

## Failure policy

Detector or initialization errors return original text. Each later operation
retries. Warnings contain only the exception class, never input, exception
messages, or tracebacks. Middleware metadata describes attempted fail-open
filtering rather than claiming successful redaction.

This policy deliberately allows raw prompt text to reach a provider on detector
errors. Attachments instead omit the affected part on inspection errors. Office
files reuse the existing image sanitizer and detector callback; the originals
are never sent as a fallback. OCR false negatives can still leave PII visible.

## Contracts retained

- The preparation hook returns `None`; returning text would append it to the original prompt.
- Middleware returns `{"request": ...}` without changing original history.
- Protocol keys, roles, tool names, call IDs, schemas, and unlisted fields are preserved.
- Supported user attachment parts may be replaced by images or an omission notice.
- System prompts, tool outputs and tool-call arguments are preserved; only user text is filtered.
- Invalid detector offsets raise; overlapping spans are fully covered.
- No presidio-anonymizer dependency: its cryptography constraint conflicts with
  Hermes' security pin, and placeholder replacement needs no encryption operator.

## Limits

- The optional Italian model has a non-commercial license; language accuracy needs benchmarking.
- Word/Excel use rendered pages, not editable document rewriting or structured
  spreadsheet extraction. Original archives, metadata, macros, and non-rendered
  content never accompany the filtered pages.
- Office package validation bounds ZIP expansion and rejects external relationships,
  embedded objects, encrypted entries, and VBA payloads before conversion.
- One Office attachment is replaced atomically: conversion, page count, or OCR
  failures omit the whole document, including previously processed pages.
- Office references use the process working directory; remote/container-only files
  and files outside it cannot be read by this local middleware.
- Unknown provider fields, auxiliary calls, and bypassing paths need separate coverage.
- There is no recovery map, mandatory egress barrier, or anonymity guarantee.

## Future work

- An appropriately licensed Italian model and synthetic IT/CH accuracy benchmarks.
- Recording HTTP-provider tests across streaming and auxiliary paths.
- A separate fail-closed architecture if confidentiality becomes a requirement.
