"""Standard-library-only client for a persistent, private Python worker."""
from __future__ import annotations

import atexit
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time


class _DefaultTimeout(float):
    """Keep the public 120-second default distinct from an explicit timeout."""


_DEFAULT_TIMEOUT = _DefaultTimeout(120)
# NLP startup plus conversion, PDF rendering, and OCR for twenty pages.
_ATTACHMENT_TIMEOUT = 120 + 60 + 60 + 20 * 20


def _read_response(stream):
    """Use raw chunks, not FileIO.readline's per-byte reads for large media."""
    chunks = bytearray()
    while len(chunks) < 128 * 1024 * 1024:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        chunks.extend(chunk)
        if b"\n" in chunk:
            break
    if not chunks.endswith(b"\n"):
        raise ValueError("incomplete worker response")
    return chunks


def _exchange_line(process, request, result, done):
    """Keep even blocked writes and partial reads inside the timeout boundary."""
    try:
        payload = memoryview((json.dumps(request) + "\n").encode("utf-8"))
        while payload:
            written = process.stdin.write(payload)
            if not written:
                raise OSError("worker pipe closed")
            payload = payload[written:]
        response = json.loads(_read_response(process.stdout))
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError("invalid worker response")
        result.append(response["result"])
    except Exception:
        # Never move raw detector exceptions, child output or input into the host.
        pass
    finally:
        done.set()


def _valid_span(span, length):
    if not isinstance(span, list) or len(span) != 3:
        return False
    start, end, entity = span
    return (type(start) is int and type(end) is int
            and 0 <= start < end <= length
            and isinstance(entity, str) and bool(entity))


def _valid_attachment(result):
    if not isinstance(result, dict) or type(result.get("detector_error")) is not bool:
        return False
    part = result.get("part")
    return isinstance(part, dict) or (
        isinstance(part, list) and all(isinstance(item, dict) for item in part)
    )


def _validate_result(operation, arguments, result):
    if operation == "redact":
        valid = isinstance(result, str)
    elif operation == "detect_spans":
        valid = isinstance(result, list) and all(
            _valid_span(span, len(arguments["text"])) for span in result
        )
    else:
        valid = _valid_attachment(result)
    if not valid:
        raise ValueError("invalid worker result")


def _signal_tree(process, sig):
    """The PID is also the session/group ID we created, even after worker exit."""
    process.poll()
    try:
        if os.name == "posix":
            # Never signal the host's group, including in injected test launchers.
            if process.pid != os.getpgrp():
                os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _tree_alive(process):
    process.poll()  # Reap the worker before checking its possibly surviving group.
    if os.name != "posix":
        return process.returncode is None
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can report EPERM while a killed orphan group is being reaped.
        return True


def _wait_tree(process, timeout=0.2):
    deadline = time.monotonic() + timeout
    while _tree_alive(process) and time.monotonic() < deadline:
        time.sleep(0.01)


def _stop_tree(process):
    _signal_tree(process, signal.SIGTERM)
    _wait_tree(process)
    # Do not gate this on worker.poll(): converters can outlive their parent.
    if _tree_alive(process):
        _signal_tree(process, signal.SIGKILL)
        _wait_tree(process)
    process.wait(timeout=0.2)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


class RuntimeClient:
    """Run local PII primitives without importing NLP into the host process."""

    def __init__(self, python: str | Path, language: str, model_name: str,
                 score_threshold=0.4, timeout: float=_DEFAULT_TIMEOUT):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("PII runtime timeout must be a finite positive number")
        self.python = str(Path(python).absolute())
        self.language = language
        self.model_name = str(model_name)
        self.score_threshold = score_threshold
        self.timeout = timeout
        self._process = None
        self._temporary = None
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._closed = False
        atexit.register(self.close)

    def redact(self, text: str) -> str:
        return self._call("redact", text=text)

    def detect_spans(self, text: str) -> list:
        return self._call("detect_spans", text=text)

    def sanitize_part(self, part, language=None) -> dict:
        """Return sanitized media and detector-failure metadata for host policy."""
        return self._call("sanitize_part", part=part, language=language)

    def _start(self):
        environment = os.environ.copy()
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            environment.pop(name, None)
        self._temporary = tempfile.mkdtemp(prefix="hermes-pii-worker-")
        for name in ("TMPDIR", "TMP", "TEMP"):
            environment[name] = self._temporary
        worker = Path(__file__).with_name("worker.py").resolve()
        self._process = subprocess.Popen(
            [self.python, "-I", "-u", str(worker)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=environment, bufsize=0,
            start_new_session=(os.name == "posix"),
        )
        return self._process

    def _call(self, operation, **arguments):
        with self._lock:
            try:
                with self._state_lock:
                    if self._closed:
                        raise RuntimeError("PII runtime is closed")
                    process = self._process or self._start()
                request = {"op": operation, **arguments, "config": {
                    "language": self.language, "model_name": self.model_name,
                    "score_threshold": self.score_threshold,
                }}
                result = self._exchange(process, request)
                _validate_result(operation, arguments, result)
                return result
            except BaseException as error:
                self._stop()
                if not isinstance(error, Exception):
                    raise
                message = "PII runtime is closed" if self._closed else "PII runtime request failed"
                raise RuntimeError(message) from None

    def _exchange(self, process, request):
        result = []
        done = threading.Event()

        threading.Thread(target=_exchange_line,
                         args=(process, request, result, done), daemon=True).start()
        timeout = self.timeout
        if timeout is _DEFAULT_TIMEOUT and request["op"] == "sanitize_part":
            timeout = _ATTACHMENT_TIMEOUT
        if not done.wait(timeout) or not result:
            raise RuntimeError("worker exchange failed")
        return result[0]

    def _stop(self):
        with self._state_lock:
            process, self._process = self._process, None
            temporary, self._temporary = self._temporary, None
            try:
                if process is not None:
                    _stop_tree(process)
            finally:
                if temporary is not None:
                    shutil.rmtree(temporary)

    def close(self):
        """Cancel in-flight work, stop the worker and reject further requests."""
        with self._state_lock:
            self._closed = True
            self._stop()
        atexit.unregister(self.close)
