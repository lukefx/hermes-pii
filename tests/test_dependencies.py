"""Regression checks for dependencies admitted into Hermes' workspace."""
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DependencyTests(unittest.TestCase):
    def test_native_install_does_not_add_nlp_packages_to_hermes(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(metadata["project"]["dependencies"], [])
        self.assertIn("python_runtime: external", (ROOT / "plugin.yaml").read_text())

    def test_bootstrap_registers_cli_in_standard_library_only_process(self):
        import subprocess
        import sys
        code = """
import argparse
import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from hermes_pii import register
commands = {}
context = SimpleNamespace(get_config=lambda key, default=None: default,
                          register_cli_command=lambda **command: commands.update({command['name']: command}))
register(context)
parser = argparse.ArgumentParser()
commands['pii']['setup_fn'](parser)
assert parser.parse_args(['install']).pii_command == 'install'
assert not {'spacy', 'presidio_analyzer', 'PIL', 'pytesseract'}.intersection(sys.modules)
print('stdlib bootstrap verified')
"""
        result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdlib bootstrap verified", result.stdout)

    def test_plugin_does_not_depend_on_its_workspace_host(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        requirements = list(metadata["project"]["dependencies"])
        for group in metadata.get("dependency-groups", {}).values():
            requirements.extend(item for item in group if isinstance(item, str))
        self.assertFalse(any(item.startswith("hermes-agent") for item in requirements))

    def test_default_model_requires_manual_provisioning(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertFalse(any("en-core-web-sm" in item for item in metadata["project"]["dependencies"]))
        self.assertNotIn("uv", metadata.get("tool", {}))

    def test_plugin_does_not_require_presidio_anonymizer(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        requirements = metadata["project"]["dependencies"]
        self.assertFalse(any("presidio-anonymizer" in item for item in requirements))


if __name__ == "__main__":
    unittest.main()
