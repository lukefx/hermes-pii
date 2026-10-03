# Design

## Responsibilities

- Package dependencies install the default English spaCy model during installation.
- Registration only installs callbacks; it does not import spaCy or load models.
- `pre_llm_call` prepares the detector, without analyzing or appending user text.
- `llm_request` rewrites a provider-bound copy using an explicit field whitelist.
- `PresidioRedactor` owns offline detection and serialized inference.
- `masking.py` replaces full overlapping-span unions with entity-type placeholders.

The plugin locks lazy construction; the redactor locks inference. There is no
permanent failure flag, recovery mapping, or request/result cache.

## Model loading

The default English model requirement and its `[tool.uv.sources]` wheel URL are declared
in `pyproject.toml` and pinned in `uv.lock`. Hermes PM keeps both in member snapshots.
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

This policy deliberately allows raw PII to reach a provider on detector errors.

## Contracts retained

- The preparation hook returns `None`; returning text would append it to the original prompt.
- Middleware returns `{"request": ...}` without changing original history.
- Protocol keys, roles, tool names, call IDs, schemas, and non-text fields are preserved.
- String values inside JSON tool arguments are filtered recursively; numeric values are preserved.
- Invalid detector offsets raise; overlapping spans are fully covered.
- No presidio-anonymizer dependency: its cryptography constraint conflicts with
  Hermes' security pin, and placeholder replacement needs no encryption operator.

## Limits

- The optional Italian model has a non-commercial license; language accuracy needs benchmarking.
- Multimodal content, unknown provider fields, auxiliary calls, and bypassing paths need separate coverage.
- There is no recovery map, mandatory egress barrier, or anonymity guarantee.
- Redacted historical tool arguments may affect replay continuity.

## Future work

- An appropriately licensed Italian model and synthetic IT/CH accuracy benchmarks.
- Recording HTTP-provider tests across streaming and auxiliary paths.
- A separate fail-closed architecture if confidentiality becomes a requirement.
