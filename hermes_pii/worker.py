"""Private JSON-lines worker. Launch directly with the managed Python -I -u."""
from __future__ import annotations

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import signal
import sys
import tempfile


def _network_disabled(*args, **kwargs):
    raise OSError("PII worker networking is disabled")


def _disable_network():
    """Block standard socket networking in this private process, not the host.

    This is defense in depth for offline libraries, not an OS-level sandbox.
    """
    import socket

    # Keep a class (rather than a function) so importing ssl still works.
    class OfflineSocket(socket.socket):
        def __new__(cls, *args, **kwargs):
            return _network_disabled()

    for name in ("connect", "connect_ex", "bind", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, _network_disabled)
    socket.socket = OfflineSocket
    for name in ("create_connection", "create_server", "socketpair", "fromfd",
                 "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, _network_disabled)


def _validate_request(request):
    if not isinstance(request, dict):
        raise ValueError("invalid worker request")
    operation = request.get("op")
    if operation in ("redact", "detect_spans"):
        valid = isinstance(request.get("text"), str)
    elif operation == "sanitize_part":
        valid = isinstance(request.get("part"), dict)
    else:
        valid = False
    if not valid:
        raise ValueError("invalid worker request")


class RuntimeWorker:
    """Keep the expensive redactor alive for the lifetime of one private pipe."""

    def __init__(self):
        self._redactor = None

    def handle(self, request):
        _validate_request(request)
        if self._redactor is None:
            from hermes_pii.redactor import PresidioRedactor
            self._redactor = PresidioRedactor(**request["config"])
        operation = request["op"]
        if operation == "redact":
            return self._redactor.redact(request["text"])
        if operation == "detect_spans":
            return self._redactor.detect_spans(request["text"])
        if operation == "sanitize_part":
            language = request.get("language") or self._redactor.language
            return self._sanitize_part(request["part"], language)
        raise ValueError("unsupported operation")

    def _sanitize_part(self, part, language):
        from hermes_pii.attachments import sanitize_content_part

        detector_error = False
        redactor = self._redactor
        assert redactor is not None

        def detect(text):
            nonlocal detector_error
            try:
                return redactor.detect_spans(text)
            except Exception:
                detector_error = True
                raise

        filtered = sanitize_content_part(part, detect_spans=detect, language=language)
        return {"part": filtered, "detector_error": detector_error}


def _response(worker, line):
    try:
        with redirect_stdout(sys.stderr):
            result = worker.handle(json.loads(line))
        return json.dumps({"ok": True, "result": result})
    except Exception:
        return json.dumps({"ok": False, "error": "PII runtime request failed"})


def _cancel(signum, frame):
    """Unwind Python temporary contexts; the parent bounds this with SIGKILL."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)


def main():
    # Reset cached tempfile state before any NLP/converter/OCR initialization.
    if os.environ.get("TMPDIR"):
        tempfile.tempdir = os.environ["TMPDIR"]
    signal.signal(signal.SIGTERM, _cancel)
    # Only this isolated child imports the checkout's package source.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    _disable_network()
    worker = RuntimeWorker()
    output = sys.stdout
    for line in sys.stdin:
        output.write(_response(worker, line) + "\n")
        output.flush()


if __name__ == "__main__":
    main()
