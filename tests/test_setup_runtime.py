"""Private runtime installation without touching the active profile."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import json
import hashlib

SOURCE = Path(__file__).parents[1] / "hermes_pii" / "setup_runtime.py"


def load_setup():
    spec = importlib.util.spec_from_file_location("pii_setup_under_test", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_generation(generation, language, model):
    python = generation / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    (python.parent.parent / "pyvenv.cfg").write_text("include-system-site-packages = false")
    return str(python), model


class RuntimeSetupTests(unittest.TestCase):
    def test_arbitrary_remote_model_is_rejected_before_creating_runtime(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            with self.assertRaisesRegex(ValueError, "model"):
                setup.install_runtime(runtime, "en", "https://example.org/model.whl")
            self.assertFalse(runtime.exists())

    def test_publishes_private_generation_and_reuses_healthy_install(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            with patch.object(setup, "_create_generation", side_effect=fake_generation) as create:
                with patch.object(setup, "_health_check", return_value=None):
                    receipt = setup.install_runtime(runtime, "en", "en_core_web_sm")
                    self.assertTrue(receipt["ready"])
                    self.assertEqual(receipt["schema"], 1)
                    self.assertTrue(Path(receipt["python"]).is_relative_to(runtime.resolve()))
                    self.assertEqual(json.loads((runtime / "current.json").read_text()), receipt)
                    self.assertEqual(setup.inspect_runtime(runtime, "en", "en_core_web_sm"), receipt)
                    self.assertEqual(setup.install_runtime(runtime, "en", "en_core_web_sm"), receipt)
                    self.assertEqual(create.call_count, 1)
                    self.assertIsNone(setup.inspect_runtime(runtime, "it", "it_core_news_sm"))
                    with patch.object(setup, "_create_generation", side_effect=RuntimeError("failure")):
                        with self.assertRaises(RuntimeError):
                            setup.install_runtime(runtime, "it", "it_core_news_sm")
                    self.assertEqual(json.loads((runtime / "current.json").read_text()), receipt)
                    self.assertEqual(len(list((runtime / "generations").iterdir())), 1)

    def test_existing_local_model_is_copied_and_changed_source_invalidates_receipt(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "source-model"
            model.mkdir()
            (model / "meta.json").write_text('{"lang":"it"}')
            (model / "config.cfg").write_text("original")
            runtime = root / "runtime"
            def build(generation, language, selection):
                python = generation / "venv" / "bin" / "python"
                python.parent.mkdir(parents=True)
                python.touch()
                (python.parent.parent / "pyvenv.cfg").write_text("include-system-site-packages = false")
                return str(python), setup._install_model([], str(python), generation, selection)
            with patch.object(setup, "_create_generation", side_effect=build):
                with patch.object(setup, "_health_check", return_value=None):
                    receipt = setup.install_runtime(runtime, "it", str(model))
                    copied = Path(receipt["model_name"])
                    self.assertNotEqual(copied, model)
                    self.assertEqual((copied / "config.cfg").read_text(), "original")
                    (model / "config.cfg").write_text("modified")
                    self.assertIsNone(setup.inspect_runtime(runtime, "it", str(model)))
                    with self.assertRaisesRegex(ValueError, "language"):
                        setup.install_runtime(root / "other", "en", str(model))

    def test_receipt_rejects_a_generation_symlink_outside_runtime(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            external = root / "outside"
            python = external / "venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            (python.parent.parent / "pyvenv.cfg").touch()
            generations = runtime / "generations"
            generations.mkdir(parents=True)
            (generations / "unsafe").symlink_to(external, target_is_directory=True)
            receipt = {"schema":1, "ready":True, "language":"en", "configured_model":"en_core_web_sm",
                       "model_name":"en_core_web_sm", "score_threshold":0.4,
                       "python":str(generations / "unsafe/venv/bin/python"),
                       "model_sha256":setup.MODEL_PINS["en_core_web_sm"][1],
                       "requirements_sha256":setup._requirements_sha256()}
            (runtime / "current.json").write_text(json.dumps(receipt))
            with patch.object(setup, "_health_check", return_value=None):
                self.assertIsNone(setup.inspect_runtime(runtime, "en", "en_core_web_sm"))

    def test_existing_receipt_with_invalid_threshold_is_not_ready(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            generation = runtime / "generations" / "existing"
            python = generation / "venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            (python.parent.parent / "pyvenv.cfg").touch()
            receipt = {"schema":1, "ready":True, "language":"en", "configured_model":"en_core_web_sm",
                       "model_name":"en_core_web_sm", "score_threshold":float("nan"),
                       "python":str(python), "model_sha256":setup.MODEL_PINS["en_core_web_sm"][1],
                       "requirements_sha256":setup._requirements_sha256()}
            (runtime / "current.json").write_text(json.dumps(receipt))
            with patch.object(setup, "_health_check", return_value=None):
                self.assertIsNone(setup.inspect_runtime(runtime, "en", "en_core_web_sm"))

    def test_lock_changes_invalidate_receipt_and_failed_publish_keeps_previous(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            lock = Path(directory) / "requirements.txt"
            lock.write_text("first lock")
            with patch.object(setup, "REQUIREMENTS", lock), patch.object(setup, "_create_generation", side_effect=fake_generation):
                with patch.object(setup, "_health_check", return_value=None):
                    receipt = setup.install_runtime(runtime, "en", "en_core_web_sm")
                    self.assertEqual(receipt["requirements_sha256"], hashlib.sha256(lock.read_bytes()).hexdigest())
                    lock.write_text("changed lock")
                    self.assertIsNone(setup.inspect_runtime(runtime, "en", "en_core_web_sm"))
                    with patch.object(setup.os, "replace", side_effect=OSError("publication failed")):
                        with self.assertRaises(OSError):
                            setup.install_runtime(runtime, "en", "en_core_web_sm")
                    self.assertEqual(json.loads((runtime / "current.json").read_text()), receipt)
                    self.assertEqual(len(list((runtime / "generations").iterdir())), 1)
                    self.assertEqual(list(runtime.glob(".current-*.json")), [])

    def test_health_failure_or_missing_interpreter_invalidates_receipt(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with patch.object(setup, "_create_generation", side_effect=fake_generation):
                with patch.object(setup, "_health_check", return_value=None):
                    receipt = setup.install_runtime(runtime, "en", "en_core_web_sm")
                with patch.object(setup, "_health_check", side_effect=RuntimeError("broken")):
                    self.assertIsNone(setup.inspect_runtime(runtime, "en", "en_core_web_sm"))
                Path(receipt["python"]).unlink()
                with patch.object(setup, "_health_check", return_value=None) as health:
                    self.assertIsNone(setup.inspect_runtime(runtime, "en", "en_core_web_sm"))
                    health.assert_not_called()

    def test_official_wheel_install_uses_fixed_url_hash_and_no_dependencies(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            generation = Path(directory)
            for model, (language, digest) in setup.MODEL_PINS.items():
                with self.subTest(model=model), patch.object(setup, "_run") as run:
                    self.assertEqual(setup._install_model(["uv"], "/private/python", generation, model), model)
                    lock = (generation / "model-requirements.txt").read_text()
                    self.assertIn(f"{model}-3.8.0/{model}-3.8.0-py3-none-any.whl", lock)
                    self.assertIn(f"--hash=sha256:{digest}", lock)
                    command = run.call_args.args[0]
                    self.assertIn("--require-hashes", command)
                    self.assertIn("--no-deps", command)
                    self.assertEqual(command[command.index("--python") + 1], "/private/python")

    def test_real_subprocess_errors_never_expose_captured_output(self):
        setup = load_setup()
        with self.assertRaises(RuntimeError) as caught:
            setup._run([sys.executable, "-I", "-c", "import sys; print('sensitive-input'); sys.exit(1)"])
        self.assertNotIn("sensitive-input", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_missing_and_malformed_receipts_are_not_ready(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            self.assertIsNone(setup.inspect_runtime(runtime, "en", "en_core_web_sm"))
            for content in ("invalid json", "[]", "{}", '{"ready":true}'):
                with self.subTest(content=content):
                    (runtime / "current.json").write_text(content)
                    self.assertIsNone(setup.inspect_runtime(runtime, "en", "en_core_web_sm"))

    def test_invalid_settings_are_rejected_without_writing_anything(self):
        setup = load_setup()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            selections = (("it", "en_core_web_sm", 0.4), ("en", "it_core_news_sm", 0.4),
                          ("en", "relative/model", 0.4), ("en", "en_core_web_sm", float("nan")),
                          ("en", "en_core_web_sm", True), ("en", "en_core_web_sm", 2))
            for language, model, threshold in selections:
                with self.subTest(language=language, model=model, threshold=threshold), self.assertRaises(ValueError):
                    setup.install_runtime(runtime, language, model, threshold)
            self.assertFalse(runtime.exists())

    def test_subprocesses_do_not_inherit_python_or_install_target_settings(self):
        setup = load_setup()
        with patch.dict("os.environ", {"PYTHONPATH":"secret", "PYTHONHOME":"secret", "VIRTUAL_ENV":"secret", "PIP_TARGET":"secret", "UV_PROJECT_ENVIRONMENT":"secret"}):
            environment = setup._clean_env()
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_TARGET", "UV_PROJECT_ENVIRONMENT"):
            self.assertNotIn(key, environment)


