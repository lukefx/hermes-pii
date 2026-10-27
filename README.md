# hermes-pii

Local PII detection for Hermes Agent using Presidio Analyzer and spaCy. Detection
runs on your machine, in a private Python environment. No cloud detector or LLM
is used. This is a POC, not a confidentiality guarantee.

## Installation

```bash
hermes plugins install hermes-pii
hermes config set plugins.entries.hermes-pii.settings.language en
hermes config set plugins.entries.hermes-pii.settings.model_name en_core_web_sm
hermes pii install
hermes pii status
```

The native `pii` command requires an enabled plugin. Accept the normal installer's
enable prompt; if you deliberately installed it disabled, run
`hermes plugins enable hermes-pii` first. Registering the plugin does not import
NLP packages or load a model. No chat or external LLM call is needed for setup.

**Release prerequisite:** a bare plugin name resolves to the reviewed catalog
commit, not this working tree. Publishing a new revision and updating the
catalog pin are separate maintainer actions; otherwise the first command still
installs the previously cataloged implementation.

`hermes pii install` creates a Python 3.12 environment beneath the active
profile's `plugin-data/hermes-pii/runtime` directory, synchronizes hash-locked
Python requirements, verifies and installs the selected official model wheel,
and checks synthetic offline redaction before publishing an atomic runtime
receipt. Hermes' own Python environment receives no NLP dependencies. The
plugin declares `python_runtime: external` and has no package runtime dependencies.
`uv` must be available to the installer; it may provision Python during this
explicit command. Network access is needed for uncached installation only.

Supported packaged models are `en_core_web_sm` and `it_core_news_sm`, both
3.8.0, downloaded from the official spaCy GitHub releases with SHA-256 pins.
Alternatively, configure an existing absolute spaCy model-data directory. It is
copied into a generation and checked against the selected language. Arbitrary
model URLs and language/model mismatches are rejected. There are no model or
package downloads during request processing.

Repeated installation reuses an identical healthy runtime. Failed provisioning
does not replace the previous working receipt. Generations are profile-local;
Hermes environment rebuilds do not delete the private environment or model.
Restart Hermes after changing settings or reprovisioning a running session.

**Strict-host compatibility is separate from runtime readiness.** The default
`strict` policy requires `hermes_cli.middleware.MiddlewareAbort`. On an
unsupported host registration exposes only the installer/status CLI and emits a
warning: it does **not** register a functioning privacy filter or silently switch
to permissive mode. `status` reports readiness and compatibility separately;
a ready model does not establish active protection. Keep the plugin disabled in
your regular profile until host compatibility and detector readiness are
verified. Use a disposable `HERMES_HOME` for setup experiments on unsupported
hosts. See [Strict host integration](docs/strict-host.md).

For corporate system certificate trust, retain TLS verification:

```bash
env -u UV_NATIVE_TLS UV_SYSTEM_CERTS=true hermes pii install
```

### Development checkout

Use a directory link instead of pretending the catalog contains uncommitted code:

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
ln -s "$PWD" "${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-pii"
hermes plugins enable hermes-pii
```

Skip the link if it already exists, and use a disposable `HERMES_HOME` for tests.
Do not install both a directory plugin and a separate pip-discovered entry point.
The project environment is development-only, not the environment used by the
native installed filter:

```bash
uv sync --frozen
# Install host integration-test dependencies into the development environment only.
uv pip install --python .venv/bin/python 'hermes-agent>=0.19.0'
uv pip install --python .venv/bin/python https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

**Model licenses:** `en_core_web_sm` is MIT. `it_core_news_sm` is CC BY-NC-SA 3.0;
verify that its non-commercial terms fit your use. spaCy has a separate license.

## Settings

Settings live under `plugins.entries.hermes-pii.settings`. Change them with
`hermes config set`. CLI commands read the current configuration; a running chat
captures its settings at registration and must be restarted after changes.

| Key | Default | Purpose |
|---|---|---|
| `language` | `en` | Language supported by the selected model |
| `model_name` | `en_core_web_sm` | Pinned packaged model or existing absolute model-data directory |
| `score_threshold` | `0.4` | Presidio confidence threshold, from 0 to 1 |
| `error_policy` | `strict` | `strict` blocks detector errors on compatible hosts; `permissive` explicitly sends unfiltered text on failure |

For Italian, subject to the model's non-commercial license:

```bash
hermes config set plugins.entries.hermes-pii.settings.language it
hermes config set plugins.entries.hermes-pii.settings.model_name it_core_news_sm
hermes pii install
hermes pii status
```

Set language and model together. After a language/model change, the old receipt
is not used for the new configuration: rerun `hermes pii install`. Named profiles
have separate settings and runtimes; use the same `hermes -p <profile>` prefix
for installation, configuration, provisioning, and status commands.

## Request flow and local data

1. The stdlib-only bootstrap registers the native `pii` CLI before NLP provisioning.
2. On compatible hosts, `pre_llm_call` prepares one cached private worker and a
   synthetic readiness check. It never analyzes or appends the user's prompt.
3. `llm_execution` rewrites a selective copy of supported provider fields
   immediately before `next_call`. The private Python worker performs detection,
   masking, image OCR, and Office conversion over local stdio IPC.
4. Strict detector failures are converted to the host's terminal policy-abort
   signal. Successful inspection continues normal execution. Original history
   and source attachments are unchanged.

`llm_request()` remains an internal transformation helper, not registered request
middleware. Ordinary request-middleware exceptions fail open in Hermes; strict
enforcement belongs to the verified execution contract instead. Registration
failure or a disabled plugin is not an egress barrier.

The parent and worker exchange raw supported text and attachments through pipes
on the same machine, not a network service. There is no remote detection fallback.
Worker stderr is discarded; warnings contain exception classes, never payloads,
exception messages, or tracebacks. No recovery mapping or result cache is kept.
Temporary Office files, rendered PDFs, and page images are cleaned after use.
The parent owns each worker's temporary root and, on POSIX, its process group;
timeout or closure cancels the worker and converter/OCR descendants before
removing that root. Default IPC deadlines are 120 seconds for text and 640 seconds
for attachments, including model startup and the supported per-page OCR budget.
Explicit client deadlines are honored unchanged.
Private environments, copied model data, and installation receipts remain on disk.
This process separation is not an operating-system sandbox.

Models load from installed local files with `spacy.load()`; no training or
`nlp.initialize()` is required. Email recognition uses the bundled public-suffix
snapshot with remote refresh and caching disabled. Masking covers the complete
union of overlapping detected spans. Failed worker operations can be retried on
later calls; there is no permanent detector-failure latch.

## Supported payloads

- Chat Completions text in `role: user` messages.
- Responses string input and text in `role: user` input messages.
- Tool-result text in Chat `role: tool` and legacy `role: function` messages,
  and Responses `function_call_output` items. JSON result string values are
  filtered; keys, numbers, booleans, and null values are preserved.
- Inline/base64 raster images in user messages and tool results. Tesseract OCR
  runs locally; detected text is covered with black rectangles before sending.
- PDF pages rendered to images by Hermes' `pdf.attach` path use the image filter.
- Inline/base64 Word `.docx` and Excel `.xlsx` attachments, and explicit user
  `@file:` references inside the Hermes process working directory, are converted
  to filtered page images. Tool-result text is never expanded as a local reference.

**PDF file references and `pdf.attach` differ.** A desktop `file.attach` reference
may lead to `read_file` extracting PDF text. The next tool-result payload is
filtered as text. `pdf.attach` renders images for OCR and pixel masking. Neither
path modifies the source PDF, and local history/tool previews retain originals.

System/developer prompts, Responses instructions, assistant messages, and tool-call
arguments are preserved and never analyzed. Protocol keys, roles, tool names,
call IDs, schemas, metadata, unlisted fields, and unsupported message roles are
preserved. Recognition of names, places, organizations, email, phones, IBANs,
cards, and IP addresses depends on language, context, thresholds, and OCR quality.
False positives and false negatives remain possible.

Remote image URLs, opaque file IDs, unsupported files, audio, and video are
replaced with an omission notice. Original attachments are never a failure
fallback, including in permissive mode. Image inspection is limited to 50 MiB
and 40 megapixels; OCR has a 20-second timeout per image.

### Image and Office prerequisites

`hermes pii install` provisions Python packages and the NLP model. It does not
install operating-system executables or OCR language data. `pytesseract` does
not include Tesseract. Install required tools separately and make them visible
on the environment's `PATH` before restarting Hermes:

```bash
# macOS: image/PDF support; add tesseract-lang for languages beyond English.
brew install poppler tesseract
# Office support:
brew install --cask libreoffice
# If soffice is not already on PATH:
export PATH="/Applications/LibreOffice.app/Contents/MacOS:$PATH"
```

On Ubuntu:

```bash
sudo apt-get install libreoffice-writer libreoffice-calc poppler-utils tesseract-ocr tesseract-ocr-eng fonts-dejavu-core
```

Check the actual launch environment with `soffice --version`, `pdftoppm -v`,
`tesseract --version`, and `tesseract --list-langs`. OCR data must match the plugin
language (`eng` for English, `ita` for Italian); provision the spaCy model separately
with `hermes pii install`.

Office processing follows this visual path:

```text
DOCX/XLSX → LibreOffice PDF → Poppler PNG pages → Tesseract + Presidio → blacked-out images
```

Only raster pages reach the provider, so an image-capable model is required.
No editable anonymized Office copy is produced. Excel follows its print layout:
hidden sheets, cells outside print areas, comments, metadata, and other content
not rendered by LibreOffice are not sent. This is not structured spreadsheet
extraction or inspection of every stored field.

Office inputs are limited to 20 MiB, 2,000 ZIP members, 100 MiB of expanded
package content, and 20 pages. At most four references are expanded per text
part. Conversion and rasterization each have a 60-second timeout, plus OCR's
20-second timeout per page. Oversized documents are omitted in full, never
silently truncated. A conversion/OCR failure on any page omits the entire
attachment. Local references remain inside the working directory after symlink
resolution; references outside it are omitted. Hermes' binary preview stub is
replaced so it does not instruct the model to read the original.

Only `.docx` and `.xlsx` are supported. Legacy `.doc`/`.xls`, macro-enabled,
encrypted, externally linked, and embedded-object packages are unsupported.
LibreOffice runs with an isolated profile, maximum macro security, macro
execution disabled, and active OLE/DDE content disabled. It is not an OS sandbox.
See [LibreOffice conversion options](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html).

## Failure policy

**Strict (default, compatible host required):** detector/import/model/IPC errors
abort the turn locally before the affected provider attempt. A safe visible error
replaces silent discarding of tool evidence. Earlier successful provider/tool calls
are not undone. A subsequent turn retries; no permanent failure latch is set.

**Permissive (explicit):** affected text passes through, including any PII, and a
safe warning states that filtering is degraded. Choose this only when
availability matters more than confidentiality:

```bash
hermes config set plugins.entries.hermes-pii.settings.error_policy permissive
```

Restart after changing policy. Identical warnings are suppressed until a
successful inspection without suppressing recovery attempts. Unsupported media
and conversion errors retain the omission policy. A detector failure caught
inside attachment inspection is also tracked for strict enforcement; it cannot
silently become a successful inspection. OCR can miss PII without raising.

Auxiliary calls and middleware-bypassing paths have not all been verified
end-to-end. The plugin is not a mandatory privacy barrier. Use synthetic data for
smoke tests; an LLM echoing placeholders is not wire-level evidence of coverage.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
cccc hermes_pii tests --max-cognitive 19 --max-cyclomatic 19 --table
uv pip check --python .venv/bin/python
hermes pii status
```

Host contract validation is separate: `hermes plugins doctor . --ci` and
`hermes plugins validate . --json` can report unavailable strict middleware on an
unsupported host, even though its bootstrap CLI works. Do not treat successful
runtime installation as proof of strict host support.

For actual DOCX/XLSX conversion and OCR, install the system tools and run:

```bash
HERMES_PII_OFFICE_SMOKE=1 uv run --no-sync python -m unittest discover -s tests -v
```

For the optional desktop PDF-read path, use the development environment with
Hermes integration-test dependencies and an existing Italian model-data directory:

```bash
HERMES_PII_PDF_SMOKE=1 HERMES_PII_ITALIAN_MODEL=/absolute/path/to/Italian-model-data \
  uv run --no-sync python -m unittest discover -s tests -p test_tool_results.py -v
```

The bundled PDF contains synthetic Italian names and phone data. The test reads
it with Hermes' `read_file`, checks Chat and Responses tool-result payloads, and
forbids socket connections during in-process detection. No remote LLM is called.
A parent-process socket mock does not constrain a subprocess: worker isolation
and IPC behavior require their own tests. The suite also exercises a real local
worker, timeout/EOF/malformed responses, recovery, concurrency, attachment-failure
metadata, recording HTTP transports, original-payload preservation, and installer
transaction/validation behavior.
