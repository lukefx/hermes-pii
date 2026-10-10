"""Regression checks for the Hermes cryptography dependency conflict."""
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DependencyTests(unittest.TestCase):
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
