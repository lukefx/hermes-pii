# Design

## Responsibilities

- A stdlib-only bootstrap registers `hermes pii install` and `hermes pii status`
  independently of NLP readiness. The manifest declares `python_runtime: external`;
  the host-facing package has no runtime dependencies.
- `setup_runtime.py` explicitly builds a profile-scoped Python 3.12 environment,
  synchronizes hash-locked requirements, verifies the configured model, and
  publishes a healthy generation through an atomic receipt. Failed installation
  preserves the previous generation. No request path performs provisioning.
- Registration installs filtering callbacks only when the selected policy is
  supported. It does not import spaCy or load models.
- `pre_llm_call` prepares the detector, without analyzing or appending user text.
- `llm_execution` filters a provider-bound copy using an explicit field whitelist,
  then invokes `next_call` only when the configured policy permits it.
- `RuntimeClient` owns a persistent private-Python subprocess and serializes
  bounded JSON-line IPC. EOF, timeout, malformed responses, and process failures
  invalidate that child; later operations can start a replacement.
- `worker.py` owns heavy imports and attachment processing. It returns explicit
  detector-failure metadata even when an attachment sanitizer catches an error.
- `PresidioRedactor` owns offline detection and serialized inference inside the worker.
- `masking.py` replaces full overlapping-span unions with entity-type placeholders.
- `attachments.py` routes media parts and sanitizes raster pixels using injected detection.
- `office.py` validates DOCX/XLSX packages, converts them to PDF using an isolated
  LibreOffice profile, and yields bounded PNG pages using Poppler.
- `office_references.py` resolves explicit user Office references inside the process
  workspace and replaces Hermes' binary preview stubs with filtered image parts.

The plugin locks lazy construction; the redactor locks inference. There is no
permanent failure flag, recovery mapping, or request/result cache.

## Model loading

`hermes pii install` accepts the pinned official `en_core_web_sm` and
`it_core_news_sm` 3.8.0 wheels with reviewed SHA-256 hashes, or an existing absolute
model-data directory copied into the generation. Arbitrary URLs and mismatched
language/model configurations are rejected. The private environment, not Hermes'
managed environment, contains spaCy, Presidio, Pillow, and pytesseract.

The worker calls `spacy.load()` on local files and supplies the installed pipeline
to Presidio, avoiding its stock loader's download path. Email validation uses a
bundled suffix snapshot with remote URLs and disk caching disabled. Runtime
subprocess environments clear `PYTHONPATH`, `PYTHONHOME`, and `VIRTUAL_ENV`;
worker stderr is discarded and raw model/document errors are not logged.

The English model is the default. Italian requires explicit language/model
configuration and has a non-commercial model license. `uv` may download Python,
packages, and the selected wheel only during the explicit installation command.
Tesseract, Poppler, LibreOffice, and OCR language data remain separately installed
system prerequisites; the installer does not invoke a system package manager.

No `nlp.initialize()` or training is needed for a pretrained model. Missing
models raise in the redactor and trigger the explicit strict/permissive policy.

## Failure policy

Strict is the default and requires the host's public `MiddlewareAbort` exception.
Registration checks this feature before adding filtering callbacks. Unsupported
strict hosts retain only the native installer/status CLI, emit a safe warning,
and remain unprotected; they never silently become permissive. Runtime readiness
and strict-host compatibility are reported separately. The execution middleware
transforms a request copy; detector errors produce an internal safe
blocked marker, which is converted to the terminal host signal before any
provider execution. The marker is never passed to `next_call`. No ordinary
exception is relied upon to enforce a stop; ordinary host middleware failures
remain fail-open. See [Strict host integration](strict-host.md).

Permissive mode must be selected explicitly. Detector errors then preserve the
affected text, including PII, and warn that filtering is degraded. Each later
operation retries. Context-local failure tracking prevents one request from
contaminating another. Identical warnings are suppressed until a successful
inspection; warnings contain only the exception class, never input, exception
messages, or tracebacks.

Attachments omit the affected part on unsupported input/conversion errors. A
detector failure inside an attachment sanitizer is also tracked for strict
enforcement. Originals are never sent as an attachment fallback, even in
permissive mode. OCR false negatives can still leave PII visible.

## Contracts retained

- The preparation hook returns `None`; returning text would append it to the original prompt.
- The internal request helper returns `{"request": ...}` without changing history;
  the registered execution middleware passes only filtered requests downstream.
- Protocol keys, roles, tool names, call IDs, schemas, and unlisted fields are preserved.
- Supported user attachment parts may be replaced by images or an omission notice.
- System prompts and tool-call arguments are preserved; supported user and tool-result text is filtered.
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
- Wider enforcement coverage if confidentiality becomes a requirement; the scoped
  strict abort contract is not a universal egress barrier.
