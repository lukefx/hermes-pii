# hermes-pii

Local PII detection for Hermes Agent using <PERSON> and spaCy. No cloud service
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

For the existing directory-plugin symlink in a Hermes profile, enable the plugin
through Hermes so dependency admission provisions its managed environment:

```bash
env -u UV_NATIVE_TLS UV_SYSTEM_CERTS=true hermes plugins enable hermes-pii
```

Restart Hermes after installing, updating code, or changing settings. The project
`.venv` is separate from Hermes' managed environment. Do not install the plugin
both as a directory plugin and as an independently discovered pip entry point.

**Model licenses:** `en_core_web_sm` is MIT. The optional `it_core_news_sm` model
is CC BY-NC-SA 3.0; verify that its terms fit your use. The spaCy library has a
separate license.

## Request flow

1. `pre_llm_call` prepares one cached detector. It does not analyze or append user text.
2. `llm_request` redacts supported text in a copy of the provider request.
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

Keys, roles, tool names, call IDs, numeric values, schemas, and non-text parts
are preserved. Images, audio, asset URLs, metadata, and unlisted provider fields
are not filtered. Names, places, organizations, email, phones, IBANs, cards, and
IP addresses are recognized; accuracy and false positives depend on context.

## Failure policy

**Fail-open:** a detector/import/model error returns the original text, including
any PII. Every later operation retries; there is no permanent failure latch.
Warnings contain only the exception class, never input, exception messages, or
tracebacks. The middleware's reason describes attempted fail-open filtering,
not a guarantee that every field was redacted.

Local history retains original values. No recovery mapping or value restoration
is implemented. Auxiliary calls, multimodal inputs, and middleware-bypassing paths
have not been verified end-to-end. This plugin must not be treated as a mandatory
privacy barrier. Use only synthetic data for development and smoke tests.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
uv pip check --python .venv/bin/python
```

Tests cover real offline detection, provider-payload rewriting, protocol preservation,
preparation without duplicate analysis, sanitized failures, and transient-error recovery.
The integration test uses a preinstalled model and forbids socket connections during
redaction. No remote LLM is contacted.
