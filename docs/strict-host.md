# Strict host integration

## Compatibility requirement

The default `strict` policy requires the public class
`hermes_cli.middleware.MiddlewareAbort` and the corresponding terminal handler
in Hermes' generic conversation loop. This is a host extension, not an API
provided by every released Hermes build. Checking for the class is a feature
gate, not proof that a third-party host correctly implements its semantics.

The plugin refuses unsupported strict registration before adding callbacks.
A host may catch registration failures and run without that plugin; therefore a
failed plugin load is **not** privacy enforcement. Check discovery, successful
registration, detector readiness, and the running process independently. Do not
enable this development revision in the production app until the paired host
change has been reviewed and installed.

## Why execution middleware

Ordinary request/execution middleware exceptions are fail-open in the inspected
host. Raising `RuntimeError`, returning an invented abort dictionary, or using
`BaseException` is not a supported terminal policy stop.

The plugin registers its `llm_execution` callback, rather than request middleware.
It filters a request copy immediately before `next_call`. On strict inspection
failure, it raises:

```python
MiddlewareAbort(
    "Hermes PII blocked this turn because local inspection failed. "
    "No provider request was sent for this attempt. Check the configured NLP model.",
    code="pii_inspection_failed",
)
```

The message/code are constants. Detector input, exception messages, and
tracebacks must not be attached to the signal or its user-visible report.
The internal blocked marker contains no original request and never reaches
`next_call`. Successful filtering preserves protocol fields and original history.

The host must propagate this signal through nested middleware and terminate the
turn before retries, provider fallback, verification nudges, or summarization.
An ordinary exception must retain its existing host behavior. Failure after a
tool has run does not undo that tool or earlier successful provider calls.

Cleanup failures must not hide the terminal policy result. The paired host runs
spinner, callback, history and status cleanup independently and reports only
static `cleanup_errors` step codes. If storage fails, persistence cannot be
guaranteed, but the fixed local refusal still returns without model recovery.

## Offline preflight

After provisioning the configured private runtime, use the profile-aware native
command:

```bash
hermes pii status
```

`ready` reports a synthetic email check through the private worker.
`abort_supported` reports the selected host's terminal abort capability, and
`compatible` evaluates that capability against the configured failure policy.
Require both `ready` and `compatible` before enabling filtering. A zero exit code
confirms runtime readiness only; it does not certify strict compatibility,
registration, detection accuracy, or complete egress coverage.

The command reads profile settings and does not download models or call an LLM.
Keep NLP packages out of the host environment. The legacy
`python -m hermes_pii.health` entry point is an in-process development diagnostic
for an NLP-enabled interpreter, not a preflight for the dependency-light host.

If deliberately choosing `permissive`, explicitly set
`plugins.entries.hermes-pii.settings.error_policy` to `permissive` before
restarting. Detector errors then allow unfiltered text. Unsupported attachments
are still omitted, not forwarded in their original form.

## Required regression coverage

- Missing model, imports and detector analysis failure; safe health output.
- Strict initial-request and post-tool denial: no affected provider execution.
- No retry/fallback/stop-gate continuation after a policy denial.
- Streaming/non-streaming, Chat Completions/Responses, local visible failure.
- Real local recording transport for successful filtering and denied attempts.
- Explicit permissive passthrough with warnings, retries and later recovery.
- Structured tool results, identifiers, JSON values and unchanged local history.
- Nested middleware propagation without reclassifying ordinary host exceptions.

## Limits

The host extension targets the generic conversation loop. `codex_app_server`
bypasses that loop; auxiliary calls, subagents, compression, titles, approvals,
plugin-originated requests and alternate endpoints require separate verification.
System/developer text, assistant history, instructions, tool-call arguments,
protocol identifiers and unlisted fields remain outside this plugin's filtering
whitelist. Other middleware can also change requests after inspection; this is
not an OS-enforced egress boundary. Successful local inspection can miss PII.
