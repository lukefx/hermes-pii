# hermes-pii

Local PII detection for Hermes Agent using `<PERSON>` and spaCy. No cloud service
or LLM is used for detection. This is a POC, not a confidentiality guarantee.

## Installation

The default English model, `en_core_web_sm` 3.8.0, is a declared package
dependency. Installing the plugin also installs the model; no separate download
command or spaCy training/initialization step is required.

The wheel URL is declared in `[tool.uv.sources]`. Use Hermes/uv rather than bare
`pip`: Hermes PM preserves the named requirement, whereas direct-URL requirements
are omitted during dependency admission.

For development:

```bash
uv venv --python 3.12 .venv
uv sync --frozen
```

For a directory-plugin symlink in a Hermes profile, enable the plugin through
Hermes so dependency admission provisions its managed environment:

```bash
env -u UV_NATIVE_TLS UV_SYSTEM_CERTS=true hermes -p <profile> plugins enable hermes-pii
```

Named profiles have separate plugin directories. Check the target profile with
`hermes -p <profile> plugins list`. If it does not show `hermes-pii`, link this
checkout into that profile's `$HERMES_HOME/plugins/hermes-pii` before enabling
it. Restart Hermes after installing, updating code, or changing settings. The
project `.venv` is separate from Hermes' managed environment. Do not install the
plugin both as a directory plugin and as an independently discovered pip entry
point.

**Model licenses:** `en_core_web_sm` is MIT. The optional `it_core_news_sm` model
is CC BY-NC-SA 3.0; verify that its terms fit your use. The spaCy library has a
separate license.

## Request flow

1. `pre_llm_call` prepares one cached detector. It does not analyze or append user text.
2. `llm_request` redacts supported text and sanitizes supported image parts in a
   copy of the provider request.
3. Hermes continues its normal execution; original conversation history is unchanged.

Models load from installed packages or local directories. Runtime loading and
analysis never download models. Email recognition uses a bundled, offline suffix
snapshot. Overlapping detections are replaced across their full union.

## Settings

Settings live under `plugins.entries.hermes-pii.settings` and are read once at
registration. Change them with `hermes config set`, then restart Hermes.

| Key | Default | Purpose |
|---|---|---|
| `language` | `en` | Language supported by the selected model |
| `model_name` | `en_core_web_sm` | Installed model name or local model directory |
| `score_threshold` | `0.4` | <PERSON> detection threshold |

The default model is installed automatically. To use Italian, first provision
`it_core_news_sm` 3.8.0 in the same Hermes environment. The plugin does not
install or download it at runtime. Then set both Italian settings from the
command line:

```bash
hermes config set plugins.entries.hermes-pii.settings.language it
hermes config set plugins.entries.hermes-pii.settings.model_name it_core_news_sm
```

Restart Hermes after changing settings. If the selected model is not installed,
initialization fails and the plugin's fail-open policy passes original text
through. Additional/custom models must be provisioned separately before use.

## Supported payloads

- Chat Completions message content and text parts.
- Responses instructions, string/message-list input, text parts, and function outputs.
- JSON string values in Chat/Responses tool-call arguments.
- Inline/base64 raster images in Chat and Responses content. Tesseract OCR runs
  locally; detected text is covered with black rectangles before the image is sent.
- PDFs attached through Hermes' `pdf.attach` flow, which renders pages to images,
  are covered by the same image path. Files read through Hermes' `@file` context
  flow are covered after their extracted text enters the provider request.

Images must be inline base64 data that the plugin can inspect locally. Remote
image URLs and opaque file, audio, and video parts are replaced with an omission
notice. Tesseract and trained data for the configured language must be installed
and visible on the Hermes process' `PATH`; `pytesseract` alone does not install
the Tesseract executable. On macOS, install the PDF renderer and English OCR
runtime with `brew install poppler tesseract`; install `tesseract-lang` as well
for languages beyond English. Image inspection is limited to 50 MiB and 40
megapixels, with a 20-second OCR timeout per image. Install these tools before
enabling the plugin, then restart Hermes.

For a named test profile, list profiles with `hermes profile list`. If needed,
link this checkout into that profile's plugin directory as described above.
Then run `hermes -p <profile> plugins enable hermes-pii` and
`hermes -p <profile> chat --tui`. Attach the PDF in the TUI. Ask Hermes to
transcribe a synthetic email in it; the expected result is that it cannot read
the blacked-out address. Check `hermes -p <profile> plugins list` to confirm the
plugin is enabled in that profile.

Keys, roles, tool names, call IDs, numeric values, schemas, and ordinary
non-sensitive protocol fields are preserved. Names, places, organizations,
email, phones, IBANs, cards, and IP addresses are recognized in supported text
and OCR output; accuracy and false positives depend on context and OCR quality.
An OCR false negative can leave PII in an image, so this remains a best-effort
filter rather than a confidentiality guarantee.

## Failure policy

**Text is fail-open:** a detector/import/model error returns the original text,
including any PII. Every later operation retries; there is no permanent failure
latch. **Attachments are fail-closed when inspection errors:** the affected part
is replaced with an omission notice. Warnings contain only the exception class,
never input, exception messages, or tracebacks. OCR can miss PII without raising
an error, so attachment sanitization is not a guarantee that every image was
fully redacted.

Local history retains original values. No recovery mapping or value restoration
is implemented. Auxiliary calls and middleware-bypassing paths have not been
verified end-to-end. This plugin must not be treated as a mandatory privacy
barrier. Use only synthetic data for development and smoke tests.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
uv pip check --python .venv/bin/python
```

Tests cover real offline text detection, provider-payload rewriting, protocol
preservation, synthetic image redaction, unsupported-media omission, preparation
without duplicate analysis, sanitized failures, and transient-error recovery.
The text integration test uses a preinstalled model and forbids socket connections
during redaction. Synthetic OCR tests do not contact Tesseract or a remote LLM.
