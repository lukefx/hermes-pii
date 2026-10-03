"""Regression checks for the Hermes cryptography dependency conflict."""
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DependencyTests(unittest.TestCase):
    def test_default_model_is_an_installation_dependency(self):
        from packaging.requirements import Requirement
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        requirements = [Requirement(item) for item in metadata["project"]["dependencies"]]
        models = [item for item in requirements if item.name == "en-core-web-sm"]
        self.assertEqual(len(models), 1, "The installer must provision the default NLP model")
        self.assertIsNone(models[0].url, "Hermes PM drops direct-URL requirements")
        self.assertEqual(str(models[0].specifier), "==3.8.0")
        self.assertEqual(
            metadata["tool"]["uv"]["sources"]["en-core-web-sm"]["url"],
            "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl",
        )

    def test_plugin_does_not_require_presidio_anonymizer(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        requirements = metadata["project"]["dependencies"]
        self.assertFalse(any("presidio-anonymizer" in item for item in requirements))


if __name__ == "__main__":
    unittest.main()
