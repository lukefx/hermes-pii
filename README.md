# hermes-pii

Local PII detection for Hermes Agent using `<PERSON>` and spaCy. No cloud service
or LLM is used for detection. This is a POC, not a confidentiality guarantee.

## Installation

Enable the plugin first so Hermes provisions its managed Python environment,
then install the default English model, `en_core_web_sm` 3.8.0, separately in
that environment before starting a chat. For a local checkout and the default
Hermes home (`~/.hermes`):

```bash
mkdir -p "$HOME/.hermes/plugins"
# Run from the root of this checkout; skip if this link already exists.
ln -s "$PWD" "$HOME/.hermes/plugins/hermes-pii"
env -u UV_NATIVE_TLS UV_SYSTEM_CERTS=true hermes plugins enable hermes-pii
hermes plugins list --plain --no-bundled
```

Hermes admits
named PyPI dependencies only; the model wheel is hosted on GitHub and cannot be
installed through the plugin dependency installer. Use the interpreter from
Hermes' current managed environment (replace the path below). With the managed
layout under `~/.hermes`, its path is recorded in
`~/.hermes/installs/<install-id>/facts.json` at
`packages.venv.environment`; append `/bin/python`. Select the record for the
Hermes installation you launch, rather than an old environment directory.

```bash
env -u UV_NATIVE_TLS UV_SYSTEM_CERTS=true uv pip install --python /path/to/hermes/environment/bin/python https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
/path/to/hermes/environment/bin/python -c "import spacy; spacy.load('en_core_web_sm')"
/path/to/hermes/environment/bin/python -c "from hermes_pii.redactor import PresidioRedactor; result = PresidioRedactor().redact('Contact John Smith at john.smith@example.com.'); print(result); assert 'John Smith' not in result and 'john.smith@example.com' not in result"
```

Restart Hermes after provisioning the model. Enabling plugins or updating Hermes
can create a new managed environment; check its current interpreter and repeat
model provisioning if needed. A successful plugin enable does not verify that
the NLP model is installed. The certificate environment variables above use the
system trust store for `uv` without disabling TLS verification.

The plugin does not download models at runtime. If the model is missing,
redaction fails open and original text is sent to the provider.

For development:

```bash
uv venv --python 3.12 .venv
uv sync --frozen
# Install the host separately for tests using agent.context_references.
uv pip install --python .venv/bin/python 'hermes-agent>=0.19.0'
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

### Local enable fails with a workspace dependency error

If `hermes plugins enable hermes-pii` reports
`hermes-agent is included as a workspace member, but is missing an entry in tool.uv.sources`,
update this checkout and retry the enable command. Older revisions included
`hermes-agent` in the development dependency group. Hermes copies directory
plugins into its own `uv` workspace, where that declaration conflicts with the
host package. The dependency has been removed from the plugin metadata;
development and CI install Hermes separately for the integration tests that use
its context-reference preprocessing. Do not add `hermes-agent` back to this
plugin's dependency groups or add a workspace-only source to the standalone
checkout. Admission failure leaves the active environment and plugin enable
configuration unchanged.

**Model licenses:** `en_core_web_sm` is MIT. The optional `it_core_news_sm` model
is CC BY-NC-SA 3.0; verify that its terms fit your use. The spaCy library has a
separate license.

## Request flow

1. `pre_llm_call` prepares one cached detector. It does not analyze or append user text.
2. `llm_request` redacts user prompt text and tool-result text, and sanitizes
   supported image parts in those messages in a copy of the provider request.
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

### Keep the Italian model across managed environment rebuilds

Models installed only into a managed virtual environment can disappear when
Hermes creates a new environment. For a persistent local model, install the
model wheel without dependencies into plugin data, then configure spaCy's model
data directory directly. The plugin's managed environment supplies spaCy:

```bash
env -u UV_NATIVE_TLS UV_SYSTEM_CERTS=true uv pip install --no-deps \
  --target "$HOME/.hermes/plugin-data/hermes-pii/models" \
  https://github.com/explosion/spacy-models/releases/download/it_core_news_sm-3.8.0/it_core_news_sm-3.8.0-py3-none-any.whl
hermes config set plugins.entries.hermes-pii.settings.language it
hermes config set plugins.entries.hermes-pii.settings.model_name \
  "$HOME/.hermes/plugin-data/hermes-pii/models/it_core_news_sm/it_core_news_sm-3.8.0"
```

For a named profile, use that profile's plugin-data directory and run configuration
commands with `hermes -p <profile>`. Fully restart the desktop backend and gateway
after updating plugin code or configuration; opening a new chat alone does not
reload Python modules or settings held by an existing backend process.

From this checkout, test a new CLI session against the real configured provider:

```bash
hermes chat --cli --oneshot -Q --max-turns 4 --run-budget 90 \
  -q 'Read tests/fixtures/pii-italian.pdf with read_file and quote its complete text. Do not use other tools.'
```

The answer should contain `<PERSON>` and `<PHONE_NUMBER>` rather than the original
name and phone. An omission notice means inspection failed; it does not prove
successful detection. This CLI check calls the configured LLM and requires its
credentials. Use a new chat when retesting the desktop flow, since previous
assistant messages can already contain the unfiltered values. Original tool
previews remain local and may still show the source text.

## Supported payloads

- Chat Completions text in messages with `role: user`.
- Responses string input and text in input messages with `role: user`.
- Tool-result text in Chat `role: tool` (and legacy `role: function`) messages
  and Responses `function_call_output` items, including PDF text extracted by
  Hermes' `read_file`. JSON results are parsed and string values are filtered;
  JSON keys, numbers, booleans, and null values retain their original values.
- Inline/base64 raster images in Chat and Responses user messages and tool results. Tesseract OCR
  runs locally; detected text is covered with black rectangles before the image is sent.
- PDFs attached through Hermes' `pdf.attach` flow, which renders pages to images,
  are covered by the same image path.
- Word `.docx` and Excel `.xlsx` attachments are converted locally to PDF pages,
  rasterized, and filtered through the same image/OCR path. Supported inputs are
  inline/base64 Chat `file` and Responses `input_file` parts with a `filename`,
  or explicit `@file:` references in user messages to files inside the Hermes
  process working directory. Other file text is covered when Hermes inlines it
  into a user message.

**PDF file references and `pdf.attach` use different paths.** Hermes' desktop
`file.attach` flow can submit a local file reference and let the agent call
`read_file`. Hermes extracts the PDF as text and returns it as a tool result.
The plugin filters that text before the next provider call. Word and Excel text
returned by tools follows the same path. In contrast, `pdf.attach` renders pages
as images, which pass through local OCR and pixel redaction. Neither path edits
the source PDF: local tool output and history retain the original content.
Tool-result text is never expanded as a local `@file:` reference.

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

System/developer prompts, Responses instructions, assistant messages, and
tool-call arguments are preserved and never analyzed. Messages without a
supported role or a Responses `function_call_output` type are also preserved.
Protocol keys, roles, tool names, call IDs, schemas, metadata, and unlisted
provider fields are preserved. Within JSON tool-result bodies, string values
are filtered while keys and non-string scalar values are preserved.
Names, places, organizations, email, phones, IBANs, cards, and IP addresses are
recognized in supported text and OCR output; accuracy and false positives depend on context and OCR quality.
An OCR false negative can leave PII in an image, so this remains a best-effort
filter rather than a confidentiality guarantee.

## Failure policy

**User text is fail-open:** a detector/import/model error returns the original text,
including any PII. Every later operation retries; there is no permanent failure
latch. **Tool-result text is fail-closed:** if local detection fails, the text
is replaced with an omission notice and later calls retry. **Attachments are
fail-closed when inspection errors:** the affected part
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

To check the desktop PDF file-read path with real Hermes extraction and the
Italian NLP model, provision `it_core_news_sm` in Hermes' current environment
and run from this checkout (adjust both paths for your installation):

```bash
HERMES_PII_PDF_SMOKE=1 PYTHONPATH="$HOME/.hermes/hermes-agent:$PWD" \
  /path/to/hermes/environment/bin/python -m unittest discover -s tests -p test_tool_results.py -v
```

For the persistent model directory above, also set
`HERMES_PII_ITALIAN_MODEL="$HOME/.hermes/plugin-data/hermes-pii/models/it_core_news_sm/it_core_news_sm-3.8.0"`
when invoking the smoke test.

The bundled synthetic PDF contains `Ciao sono Mario e il numero di telefono è
0797770099.` The smoke test reads it through Hermes' `read_file`, filters both
Chat and Responses tool-result payloads, and asserts that the provider receives
`<PERSON>` and `<PHONE_NUMBER>` instead of the original values. No remote LLM
is called; socket connections are forbidden during detection.

Tests cover real offline text detection, provider-payload rewriting, protocol
preservation, synthetic image redaction, Office rendering and reference handling, unsupported-media omission, preparation
without duplicate analysis, sanitized failures, and transient-error recovery.
The text integration test uses a preinstalled model and forbids socket connections
during redaction. Synthetic OCR tests do not contact Tesseract or a remote LLM.
