"""Standard-library-only installer for private NLP runtime generations.

Installation never changes the host interpreter. Only a healthy generation is
published; existing generations are retained for workers already using them.
"""
from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import subprocess
import uuid

PACKAGE_DIR = Path(__file__).resolve().parent
REQUIREMENTS = PACKAGE_DIR / "runtime-requirements.txt"
MODEL_PINS = {
    "en_core_web_sm": ("en", "1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"),
    "it_core_news_sm": ("it", "3f617bf9a8ae0418953cf1fbf014e10272684c4229e882a7fd748b637d0100bf"),
}


def _validate_selection(language, model_name):
    if not isinstance(model_name, str) or not isinstance(language, str) or not language:
        raise ValueError("A language and model selection are required")
    if model_name in MODEL_PINS:
        if MODEL_PINS[model_name][0] != language:
            raise ValueError("The configured language does not match the model")
        return
    directory = Path(model_name)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("Select a supported model package or an existing absolute model directory")
    try:
        metadata = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("The local model directory needs valid spaCy metadata") from None
    if not isinstance(metadata, dict) or metadata.get("lang") != language:
        raise ValueError("The configured language does not match the local model")


def _model_fingerprint(model_name):
    if model_name in MODEL_PINS:
        return MODEL_PINS[model_name][1]
    digest = hashlib.sha256()
    root = Path(model_name)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("The local model directory must not contain symbolic links")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\x00")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            digest.update(b"\x00")
    return digest.hexdigest()



def _requirements_sha256():
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _clean_env():
    blocked = {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
    return {key: value for key, value in os.environ.items()
            if key not in blocked and not key.startswith(("UV_", "PIP_"))}


def _run(command, *, timeout=900):
    try:
        return subprocess.run(command, env=_clean_env(), check=True,
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        # Never surface subprocess output, arbitrary paths, or environment secrets.
        raise RuntimeError("Private runtime setup or health check failed; the previous installation is unchanged") from None


def _uv():
    executable = shutil.which("uv")
    if not executable:
        raise RuntimeError("uv is required to install the private runtime")
    return [executable, "--system-certs", "--no-config"]


def _install_model(uv, python, generation, model_name):
    if model_name not in MODEL_PINS:
        target = generation / "model"
        shutil.copytree(model_name, target)
        return str(target)
    digest = MODEL_PINS[model_name][1]
    wheel = f"{model_name}-3.8.0-py3-none-any.whl"
    url = f"https://github.com/explosion/spacy-models/releases/download/{model_name}-3.8.0/{wheel}"
    model_lock = generation / "model-requirements.txt"
    model_lock.write_text(f"{model_name} @ {url} --hash=sha256:{digest}\n", encoding="utf-8")
    _run([*uv, "pip", "install", "--python", python, "--no-deps", "--require-hashes",
          "--only-binary", ":all:", "-r", str(model_lock)])
    return model_name


def _create_generation(generation, language, model_name):
    uv = _uv()
    venv = generation / "venv"
    _run([*uv, "venv", "--python", "3.12", str(venv)])
    python = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    _run([*uv, "pip", "sync", "--python", python, "--require-hashes", "--only-binary", ":all:",
          "--index-url", "https://pypi.org/simple", str(REQUIREMENTS)])
    return python, _install_model(uv, python, generation, model_name)


HEALTH_SCRIPT = r'''
import sys, types, socket
from pathlib import Path
assert sys.version_info[:2] == (3, 12)
assert sys.prefix != sys.base_prefix

def deny_network(*args, **kwargs):
    raise RuntimeError("Network is disabled during runtime health checks")
socket.socket.connect = deny_network
socket.create_connection = deny_network
import spacy, PIL, pytesseract
model, language, root, threshold = sys.argv[1:]
nlp = spacy.load(model)
assert nlp.lang == language, "Language/model mismatch"
package = types.ModuleType("hermes_pii")
package.__path__ = [root]
sys.modules["hermes_pii"] = package
from hermes_pii.redactor import PresidioRedactor
redactor = PresidioRedactor(language, model, float(threshold))
result = redactor.redact("Contact example.person@example.com")
assert "example.person@example.com" not in result
assert "<EMAIL_ADDRESS>" in result
'''


def _health_check(python, language, model_name, score_threshold):
    _run([python, "-I", "-c", HEALTH_SCRIPT, model_name, language,
          str(PACKAGE_DIR), str(score_threshold)], timeout=120)


def _validate_threshold(score_threshold):
    if type(score_threshold) not in (int, float) or not math.isfinite(score_threshold) or not 0 <= score_threshold <= 1:
        raise ValueError("score_threshold must be between zero and one")


def _receipt_matches(receipt, runtime_dir, language, model_name):
    _validate_threshold(receipt.get("score_threshold"))
    expected = {"schema": 1, "ready": True, "language": language,
                "configured_model": model_name, "requirements_sha256": _requirements_sha256()}
    if any(receipt.get(key) != value for key, value in expected.items()):
        return False
    python = Path(receipt.get("python", ""))
    if not python.is_absolute() or not python.is_file():
        return False
    venv = python.parent.parent
    generation = venv.parent
    if generation.resolve().parent != (runtime_dir / "generations").resolve():
        return False
    if venv.resolve().parent != generation.resolve():
        return False
    if not (venv / "pyvenv.cfg").is_file():
        return False
    effective = model_name if model_name in MODEL_PINS else str(generation / "model")
    if receipt.get("model_name") != effective:
        return False
    fingerprint = _model_fingerprint(model_name)
    if receipt.get("model_sha256") != fingerprint:
        return False
    return model_name in MODEL_PINS or _model_fingerprint(effective) == fingerprint


def inspect_runtime(runtime_dir, language, model_name):
    """Return a matching, healthy receipt, or None without installing anything."""
    runtime_dir = Path(runtime_dir).resolve()
    try:
        _validate_selection(language, model_name)
        receipt = json.loads((runtime_dir / "current.json").read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or not _receipt_matches(receipt, runtime_dir, language, model_name):
            return None
        _health_check(receipt["python"], language, receipt["model_name"], receipt["score_threshold"])
        return receipt
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None


def _publish(runtime_dir, receipt):
    temporary = runtime_dir / f".current-{uuid.uuid4().hex}.json"
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, runtime_dir / "current.json")
    finally:
        temporary.unlink(missing_ok=True)


def install_runtime(runtime_dir, language, model_name, score_threshold=0.4):
    """Install pinned Python 3.12 dependencies and publish after offline checks."""
    _validate_selection(language, model_name)
    _validate_threshold(score_threshold)
    runtime_dir = Path(runtime_dir).resolve()
    existing = inspect_runtime(runtime_dir, language, model_name)
    if existing is not None and existing["score_threshold"] == score_threshold:
        return existing
    generation = runtime_dir / "generations" / uuid.uuid4().hex
    generation.mkdir(parents=True, mode=0o700)
    try:
        python, effective_model = _create_generation(generation, language, model_name)
        _health_check(python, language, effective_model, score_threshold)
        receipt = {"schema": 1, "ready": True, "python": python, "model_name": effective_model,
                   "configured_model": model_name, "language": language,
                   "model_sha256": _model_fingerprint(model_name),
                   "requirements_sha256": _requirements_sha256(), "score_threshold": score_threshold}
        _publish(runtime_dir, receipt)
        return receipt
    except BaseException:
        shutil.rmtree(generation, ignore_errors=True)
        raise
