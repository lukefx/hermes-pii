# hermes-pii

Local PII detection for Hermes Agent using `<PERSON>` and spaCy. No cloud service
or LLM is used for detection. This is a POC, not a confidentiality guarantee.

## Installation

Install the default English model, `en_core_web_sm` 3.8.0, separately in the
Python environment used by Hermes before enabling the plugin. Hermes admits
named PyPI dependencies only; the model wheel is hosted on GitHub and cannot be
installed through the plugin dependency installer. Use the interpreter from
Hermes' managed environment (replace the path below):

```bash
uv pip install --python /path/to/hermes/environment/bin/python https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
/path/to/hermes/environment/bin/python -c "import spacy; spacy.load('en_core_web_sm')"
```

The plugin does not download models at runtime. If the model is missing,
redaction fails open and original text is sent to the provider.

For development:

```bash
uv venv --python 3.12 .venv
uv sync --frozen
uv pip install --python .venv/bin/python https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
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
2. `llm_request` redacts user prompt text and sanitizes supported image parts in
   user messages in a copy of the provider request.
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

The default model requires separate provisioning. To use Italian, first provision
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

- Chat Completions text in messages with `role: user`.
- Responses string input and text in input messages with `role: user`.
- Inline/base64 raster images in Chat and Responses user messages. Tesseract OCR
  runs locally; detected text is covered with black rectangles before the image is sent.
- PDFs attached through Hermes' `pdf.attach` flow, which renders pages to images,
  are covered by the same image path.
- Word `.docx` and Excel `.xlsx` attachments are converted locally to PDF pages,
  rasterized, and filtered through the same image/OCR path. Supported inputs are
  inline/base64 Chat `file` and Responses `input_file` parts with a `filename`,
  or explicit `@file:` references in user messages to files inside the Hermes
  process working directory. Other file text is covered when Hermes inlines it
  into a user message.

Images must be inline base64 data that the plugin can inspect locally. Remote
image URLs, opaque file IDs, unsupported files, audio, and video parts are
replaced with an omission notice. Tesseract and trained data for the configured language must be installed
and visible on the Hermes process' `PATH`; `pytesseract` alone does not install
the Tesseract executable. On macOS, install the PDF renderer and English OCR
runtime with `brew install poppler tesseract`; install `tesseract-lang` as well
for languages beyond English. Image inspection is limited to 50 MiB and 40
megapixels, with a 20-second OCR timeout per image. Install these tools before
enabling the plugin, then restart Hermes.

### Word and Excel

Office files follow the same visual path as PDF attachments:

```text
DOCX/XLSX → LibreOffice PDF → Poppler PNG pages → Tesseract + Presidio → blacked-out images
```

No additional Python packages are required for Office support. Install
LibreOffice in addition to the Poppler and Tesseract system tools already used
for PDF/image processing. `uv sync` and Hermes' plugin dependency installer do
not install these programs. All three commands,
`soffice`, `pdftoppm`, and `tesseract`, must be available on the Hermes process'
`PATH`. On macOS:

```bash
brew install --cask libreoffice
brew install poppler tesseract
# If soffice is not already on PATH:
export PATH="/Applications/LibreOffice.app/Contents/MacOS:$PATH"
```

On Ubuntu:

```bash
sudo apt-get install libreoffice-writer libreoffice-calc poppler-utils tesseract-ocr tesseract-ocr-eng fonts-dejavu-core
```

Verify the commands from the same environment used to launch Hermes:

```bash
soffice --version
pdftoppm -v
tesseract --version
tesseract --list-langs
```

The OCR language data must match the plugin's configured language (`eng` for the
default English configuration). Install additional Tesseract language data when
using another language, alongside the corresponding spaCy model described above.

Restart Hermes, then attach a synthetic Office file or reference it in a user
message, for example `Summarize @file:"documents/test.docx"`. Local references
are restricted to the process working directory and its descendants, including
after resolving symlinks. Place uploaded files in that workspace; references
outside it are omitted. The plugin replaces Hermes' binary-file preview stub
with filtered pages so it does not ask the model to read the original.

The provider receives only raster pages. Original Office files and local
conversation history remain unchanged; no editable anonymized Office copy is
produced. A model that accepts image input is required. Excel follows its print
layout: hidden sheets, cells outside print areas, comments, metadata, and other
content not rendered by LibreOffice are not sent. This is a visual preview,
not structured spreadsheet extraction or inspection of every stored field.

Office input is limited to 20 MiB, 2,000 ZIP members, 100 MiB of expanded package
content, and 20 pages. At most four Office references are expanded per text
part. Conversion and rasterization each have a 60-second timeout; the existing
20-second OCR timeout applies per page. Oversized documents are omitted in
full, rather than silently truncated. A conversion or OCR failure on any page
omits the entire Office attachment. Temporary documents, PDFs, and page images
are deleted after processing.

Only `.docx` and `.xlsx` are supported in this version. Legacy `.doc`/`.xls`,
macro-enabled, encrypted, externally linked, and embedded-object packages are
unsupported. Conversion uses an isolated LibreOffice profile with macro
security set to its highest level, macro execution disabled, and active
OLE/DDE content disabled. It is not an operating-system sandbox.
LibreOffice's [command-line conversion documentation](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)
describes the conversion interface used here.

For a named test profile, list profiles with `hermes profile list`. If needed,
link this checkout into that profile's plugin directory as described above.
Then run `hermes -p <profile> plugins enable hermes-pii` and
`hermes -p <profile> chat --tui`. Attach the PDF in the TUI. Ask Hermes to
transcribe a synthetic email in it; the expected result is that it cannot read
the blacked-out address. Check `hermes -p <profile> plugins list` to confirm the
plugin is enabled in that profile.

System/developer prompts, Responses instructions, assistant messages, tool-call
arguments, and tool outputs are preserved and never analyzed. Messages without
an explicit user role are also preserved. Keys, roles, tool names, call IDs,
numeric values, schemas, metadata, and unlisted provider fields are preserved.
Names, places, organizations, email, phones, IBANs, cards, and IP addresses are
recognized in supported text and OCR output; accuracy and false positives depend on context and OCR quality.
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

To include a real DOCX/XLSX conversion and OCR check, install the system tools
above and run:

```bash
HERMES_PII_OFFICE_SMOKE=1 uv run --no-sync python -m unittest discover -s tests -v
```

CI runs this check on Python 3.12; other matrix entries run the unit suite.

Tests cover real offline text detection, provider-payload rewriting, protocol
preservation, synthetic image redaction, Office rendering and reference handling, unsupported-media omission, preparation
without duplicate analysis, sanitized failures, and transient-error recovery.
The text integration test uses a preinstalled model and forbids socket connections
during redaction. Synthetic OCR tests do not contact Tesseract or a remote LLM.
