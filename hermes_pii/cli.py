"""Dependency-light native commands for explicit local PII provisioning."""
from pathlib import Path
import json


def runtime_directory():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "plugin-data" / "hermes-pii" / "runtime"


def read_settings(ctx):
    defaults = {"language": "en", "model_name": "en_core_web_sm", "score_threshold": 0.4,
                "error_policy": "strict"}
    return {key: ctx.get_config(key, default) for key, default in defaults.items()}


def setup_parser(parser):
    commands = parser.add_subparsers(dest="pii_command", required=True)
    commands.add_parser("install", help="Build an isolated runtime and provision the configured model")
    commands.add_parser("status", help="Report runtime readiness and strict-host compatibility")


def make_handler(settings_provider):
    def handle(args):
        import sys
        try:
            settings = settings_provider()
            if args.pii_command == "status":
                return print_status(settings)
            from .setup_runtime import install_runtime
            receipt = install_runtime(runtime_directory(), settings["language"], settings["model_name"],
                                      settings["score_threshold"])
            print(json.dumps({"ready": receipt["ready"], "language": receipt["language"],
                              "model_name": receipt["configured_model"]}))
            return 0
        except Exception as error:
            print(f"PII command failed ({type(error).__name__})", file=sys.stderr)
            return 1
    return handle


def print_status(settings):
    from .setup_runtime import inspect_runtime
    from .plugin import _require_abort_support
    receipt = inspect_runtime(runtime_directory(), settings["language"], settings["model_name"])
    try:
        _require_abort_support()
        supported = True
    except RuntimeError:
        supported = False
    ready = False
    if receipt is not None:
        client = _runtime_from_receipt(settings, receipt)
        try:
            probe = client.redact("Contact hermes-pii-check@example.com.")
            ready = "hermes-pii-check@example.com" not in probe and "<EMAIL_ADDRESS>" in probe
        except Exception:
            ready = False
        finally:
            client.close()
    print(json.dumps({"ready": ready, "language": settings["language"],
                      "model_name": settings["model_name"], "error_policy": settings["error_policy"],
                      "abort_supported": supported,
                      "compatible": settings["error_policy"] == "permissive" or supported}))
    return 0 if ready else 1


def make_runtime(settings):
    from .setup_runtime import inspect_runtime
    receipt = inspect_runtime(runtime_directory(), settings["language"], settings["model_name"])
    if receipt is None:
        raise RuntimeError("PII runtime is not provisioned for these settings; run hermes pii install")
    return _runtime_from_receipt(settings, receipt)


def _runtime_from_receipt(settings, receipt):
    from .runtime_client import RuntimeClient
    return RuntimeClient(receipt["python"], settings["language"], receipt["model_name"],
                         score_threshold=settings["score_threshold"])


def register_command(ctx):
    ctx.register_cli_command(name="pii", help="Install and inspect the isolated local PII runtime",
                             setup_fn=setup_parser, handler_fn=make_handler(lambda: read_settings(ctx)))
