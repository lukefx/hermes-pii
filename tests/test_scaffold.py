"""Smoke tests for the directory-plugin entry point and registration."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ScaffoldTests(unittest.TestCase):
    def test_directory_plugin_loads_and_registers_documented_callbacks(self):
        entrypoint = ROOT / "__init__.py"
        self.assertTrue(entrypoint.is_file(), "Directory plugin entry point is missing")
        name = "hermes_pii_directory_test"
        spec = importlib.util.spec_from_file_location(
            name, entrypoint, submodule_search_locations=[str(ROOT)]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            self.assertTrue(callable(module.register))
            from test_plugin import PluginContext
            context = PluginContext()
            self.assertIsNone(module.register(context))
            self.assertIn("pre_llm_call", context.hooks)
            self.assertIn("llm_request", context.middleware)
        finally:
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]


if __name__ == "__main__":
    unittest.main()
