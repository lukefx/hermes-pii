"""The native installer command must be available before NLP provisioning."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from hermes_pii import register
from test_plugin import PluginContext


class BootstrapContext(PluginContext):
    def __init__(self):
        super().__init__()
        self.commands = {}

    def get_config(self, key, default=None):
        return default

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kwargs):
        self.commands[name] = (setup_fn, handler_fn)


class CliTests(unittest.TestCase):
    def test_strict_host_without_abort_still_exposes_install_command(self):
        context = BootstrapContext()
        with patch("hermes_pii.plugin._require_abort_support", side_effect=RuntimeError("unsupported")):
            register(context)
        setup, handler = context.commands["pii"]
        parser = argparse.ArgumentParser()
        setup(parser)
        self.assertEqual(parser.parse_args(["install"]).pii_command, "install")
        self.assertTrue(callable(handler))
        self.assertEqual(context.hooks, {})
        self.assertEqual(context.middleware, {})


    def test_native_registration_uses_private_runtime_without_eager_start(self):
        context = BootstrapContext()
        context.get_config = lambda key, default=None: "permissive" if key == "error_policy" else default
        client = Mock()
        client.redact.side_effect = lambda text: text.replace("hermes-pii-check@example.com", "<EMAIL_ADDRESS>")
        with patch("hermes_pii.cli.make_runtime", return_value=client) as factory:
            register(context)
            factory.assert_not_called()
            context.hooks["pre_llm_call"]()
            factory.assert_called_once()
        self.assertIn("pii", context.commands)
        self.assertIn("llm_execution", context.middleware)


    def test_status_checks_the_private_installation_only_once(self):
        from hermes_pii.cli import print_status

        settings = {"language": "en", "model_name": "en_core_web_sm", "score_threshold": 0.4,
                    "error_policy": "strict"}
        receipt = {"python": "/synthetic-private-python", "model_name": "en_core_web_sm"}
        client = Mock()
        client.redact.return_value = "<EMAIL_ADDRESS>"
        output = io.StringIO()
        with patch("hermes_pii.setup_runtime.inspect_runtime", return_value=receipt) as inspect:
            with patch("hermes_pii.runtime_client.RuntimeClient", return_value=client) as factory:
                with patch("hermes_pii.plugin._require_abort_support", side_effect=RuntimeError("unsupported")):
                    with contextlib.redirect_stdout(output):
                        result = print_status(settings)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["compatible"], False)
        inspect.assert_called_once()
        factory.assert_called_once_with("/synthetic-private-python", "en", "en_core_web_sm", score_threshold=0.4)
        client.close.assert_called_once_with()

    def test_install_failure_prints_only_exception_class(self):
        import sys
        from types import SimpleNamespace
        from hermes_pii.cli import make_handler

        settings = {"language": "en", "model_name": "en_core_web_sm", "score_threshold": 0.4}
        installer = Mock(side_effect=RuntimeError("private@example.com"))
        module = SimpleNamespace(install_runtime=installer)
        output = io.StringIO()
        with patch.dict(sys.modules, {"hermes_pii.setup_runtime": module}):
            with patch("hermes_pii.cli.runtime_directory", return_value=Path("/synthetic-runtime")):
                with contextlib.redirect_stderr(output):
                    result = make_handler(lambda: settings)(argparse.Namespace(pii_command="install"))
        self.assertEqual(result, 1)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("private@example.com", output.getvalue())


if __name__ == "__main__":
    unittest.main()
