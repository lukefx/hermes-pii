"""Private stdio runtime tests; all sensitive fixtures are synthetic."""
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO, StringIO
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from typing import Any
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_pii.worker import RuntimeWorker


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
SCRATCH = Path.home() / ".hermes" / "cache" / "scratch"
REAL_POPEN = subprocess.Popen
FAKE_WORKER = r'''
import json
import sys
import time
mode = sys.argv[1]
if mode.startswith("tree_"):
    import os
    import subprocess
    converter = "\n".join([
        "import json, os, signal, sys, tempfile, time",
        "from pathlib import Path",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        "directory = tempfile.mkdtemp(prefix='synthetic-office-')",
        "Path(directory, 'raw-document.txt').write_text('synthetic-sensitive-document')",
        "Path(sys.argv[1]).write_text(json.dumps({'pid': os.getpid(), 'group': os.getpgrp(), 'directory': directory, 'env': [os.environ.get(name) for name in ('TMPDIR', 'TMP', 'TEMP')]}))",
        "time.sleep(30)",
    ])
    for line in sys.stdin:
        request = json.loads(line)
        child = subprocess.Popen([sys.executable, '-I', '-u', '-c', converter, request['part']['marker']])
        if mode == "tree_exit":
            time.sleep(0.1)
            sys.exit()
        if mode == "tree_close":
            print(json.dumps({'ok': True, 'result': {'part': {}, 'detector_error': False}}), flush=True)
        else:
            time.sleep(30)
    sys.exit()
if mode == "blocked_write":
    time.sleep(20)
    sys.exit()
for line in sys.stdin:
    request = json.loads(line)
    if mode == "eof":
        sys.exit()
    if mode == "timeout":
        time.sleep(20)
    if mode == "midline":
        sys.stdout.write('{"ok": true, "result": "secret"}')
        sys.stdout.flush()
        time.sleep(20)
    invalid = {
        "invalid_json": 'synthetic-secret\n',
        "partial_eof": '{"ok":true,"result":"secret"}',
        "not_object": '[]\n',
        "missing_result": '{"ok":true}\n',
        "worker_error": '{"ok":false,"error":"synthetic-secret"}\n',
        "wrong_ok": '{"ok":1,"result":"secret"}\n',
        "wrong_redact": '{"ok":true,"result":{"text":"secret"}}\n',
        "wrong_spans": '{"ok":true,"result":[[0,999,"EMAIL"]]}\n',
        "wrong_envelope": '{"ok":true,"result":{"part":{},"detector_error":"no"}}\n',
        "missing_metadata": '{"ok":true,"result":{"part":{}}}\n',
        "wrong_part": '{"ok":true,"result":{"part":["secret"],"detector_error":false}}\n',
    }
    if mode in invalid:
        sys.stdout.write(invalid[mode])
        sys.stdout.flush()
        if mode == "partial_eof":
            sys.exit()
        continue
    time.sleep(0.002)
    results = {"redact": request.get("text"), "detect_spans": [[0, 1, "PERSON"]],
               "sanitize_part": {"part": request.get("part"), "detector_error": True}}
    print(json.dumps({"ok": True, "result": results[request["op"]]}), flush=True)
'''


class ClientProtocolTests(unittest.TestCase):
    def setUp(self):
        from hermes_pii.runtime_client import RuntimeClient

        self.client_class = RuntimeClient
        SCRATCH.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(temporary.cleanup)
        self.script = Path(temporary.name) / "fake_worker.py"
        self.script.write_text(FAKE_WORKER)

    def client(self, *modes, timeout: float | None=None):
        choices = iter(modes or ("valid",))

        def start(command, **kwargs):
            return REAL_POPEN([sys.executable, "-I", "-u", str(self.script), next(choices)], **kwargs)

        patcher = patch("hermes_pii.runtime_client.subprocess.Popen", side_effect=start)
        self.addCleanup(patcher.stop)
        self.spawn = patcher.start()
        options = {} if timeout is None else {"timeout": timeout}
        client = self.client_class(PYTHON, "en", "en_core_web_sm", **options)
        self.addCleanup(client.close)
        return client

    def tree_record(self):
        marker = self.script.with_name("converter.json")
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        record = json.loads(marker.read_text())

        def cleanup():
            try:
                os.kill(record["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
            shutil.rmtree(record["directory"], ignore_errors=True)

        self.addCleanup(cleanup)
        return record

    def assert_tree_cleaned(self, record):
        self.assertFalse(Path(record["directory"]).exists(), "sensitive converter files survived")
        with REAL_POPEN(["ps", "-o", "stat=", "-p", str(record["pid"])],
                        stdout=subprocess.PIPE, text=True) as probe:
            status = probe.communicate(timeout=2)[0].strip()
        self.assertTrue(not status or status.startswith("Z"), "converter descendant is still live")
        self.assertNotEqual(record["group"], os.getpgrp())
        self.assertEqual(len(set(record["env"])), 1)
        self.assertIsNotNone(record["env"][0])
        self.assertFalse(Path(record["env"][0]).exists(), "worker-owned root survived")

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_attachment_timeout_removes_sensitive_converter_tree_and_recovers(self):
        client = self.client("tree_timeout", "valid", timeout=0.4)
        with self.assertRaises(RuntimeError):
            client.sanitize_part({"marker": str(self.script.with_name("converter.json"))})
        record = self.tree_record()
        self.assert_tree_cleaned(record)
        self.assertEqual(client.redact("recovered"), "recovered")

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_close_cleans_converter_after_worker_has_already_exited(self):
        client = self.client("tree_close")
        client.sanitize_part({"marker": str(self.script.with_name("converter.json"))})
        record = self.tree_record()
        process = client._process
        assert process is not None
        process.terminate()
        process.wait(timeout=2)
        client.close()
        self.assert_tree_cleaned(record)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_close_interrupts_inflight_attachment_before_request_timeout(self):
        client = self.client("tree_timeout", timeout=3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(client.sanitize_part, {"marker": str(self.script.with_name("converter.json"))})
            record = self.tree_record()
            start = time.monotonic()
            client.close()
            self.assertLess(time.monotonic() - start, 1.5)
            with self.assertRaises(RuntimeError):
                request.result(timeout=1)
        self.assert_tree_cleaned(record)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_keyboard_interrupt_cleans_converter_and_allows_next_request(self):
        client = self.client("tree_timeout", "valid")
        records = []

        def cancel(process, request):
            process.stdin.write((json.dumps(request) + "\n").encode())
            records.append(self.tree_record())
            raise KeyboardInterrupt

        with patch.object(client, "_exchange", side_effect=cancel):
            with self.assertRaises(KeyboardInterrupt):
                client.sanitize_part({"marker": str(self.script.with_name("converter.json"))})
        self.assert_tree_cleaned(records[0])
        self.assertEqual(client.redact("recovered"), "recovered")

    def test_attachment_envelope_reaches_caller(self):
        client = self.client()
        part = {"type": "input_image", "image_url": "data:image/png;base64,AA=="}
        self.assertEqual(client.sanitize_part(part, language="it"), {"part": part, "detector_error": True})

    def test_launch_is_lazy_isolated_and_cleans_environment(self):
        client = self.client()
        self.spawn.assert_not_called()
        with patch.dict(os.environ, {"PYTHONPATH": "secret", "PYTHONHOME": "secret", "VIRTUAL_ENV": "secret"}):
            self.assertEqual(client.redact("test"), "test")
        args, kwargs = self.spawn.call_args
        self.assertEqual(args[0], [str(PYTHON.absolute()), "-I", "-u", str(ROOT / "hermes_pii" / "worker.py")])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            self.assertNotIn(name, kwargs["env"])

    def test_invalid_responses_are_safe_and_next_request_restarts(self):
        cases = {
            "invalid_json": "redact", "not_object": "redact", "missing_result": "redact",
            "worker_error": "redact", "wrong_ok": "redact", "wrong_redact": "redact",
            "wrong_spans": "detect_spans", "wrong_envelope": "sanitize_part",
            "missing_metadata": "sanitize_part", "wrong_part": "sanitize_part",
            "partial_eof": "redact", "eof": "redact",
        }
        for mode, method in cases.items():
            with self.subTest(mode=mode):
                client = self.client(mode, "valid")
                value = {} if method == "sanitize_part" else "synthetic-secret"
                with self.assertRaises(RuntimeError) as caught:
                    getattr(client, method)(value)
                self.assertNotIn("synthetic-secret", "".join(traceback.format_exception(caught.exception)))
                self.assertIsNone(client._process)
                self.assertFalse(Path(self.spawn.call_args.kwargs["env"]["TMPDIR"]).exists())
                self.assertEqual(client.redact("recovered"), "recovered")
                client.close()

    def test_timeouts_include_midline_response_and_blocked_write(self):
        for mode in ("timeout", "midline", "blocked_write"):
            with self.subTest(mode=mode):
                client = self.client(mode, "valid", timeout=0.15)
                text = "x" * (8 * 1024 * 1024) if mode == "blocked_write" else "synthetic-secret"
                start = time.monotonic()
                with self.assertRaises(RuntimeError):
                    client.redact(text)
                self.assertLess(time.monotonic() - start, 2)
                client.timeout = 1
                self.assertEqual(client.redact("recovered"), "recovered")
                client.close()

    def test_attachment_sized_response_is_read_in_chunks(self):
        client = self.client(timeout=1.5)
        text = "x" * (8 * 1024 * 1024)
        self.assertEqual(client.redact(text), text)

    def test_concurrent_requests_stay_paired_and_reuse_worker(self):
        client = self.client()
        values = [f"item-{index}" for index in range(24)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(client.redact, values)), values)
        self.assertEqual(self.spawn.call_count, 1)

    def test_close_reaps_worker_and_rejects_reuse(self):
        client = self.client()
        client.redact("test")
        process = client._process
        assert process is not None and process.stdin is not None and process.stdout is not None
        client.close()
        client.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            client.redact("secret")

    def test_attachment_default_covers_pipeline_but_explicit_timeouts_are_retained(self):
        from types import SimpleNamespace

        for timeout, expected in ((None, 640), (120, 120), (0.2, 0.2)):
            with self.subTest(timeout=timeout):
                client = self.client(timeout=timeout)
                events = []

                def event_factory():
                    event = Mock(wraps=threading.Event())
                    events.append(event)
                    return event

                facade = SimpleNamespace(Event=event_factory, Thread=threading.Thread)
                with patch("hermes_pii.runtime_client.threading", facade):
                    self.assertEqual(client.redact("test"), "test")
                    client.sanitize_part({})
                events[0].wait.assert_called_once_with(120 if timeout is None else timeout)
                events[1].wait.assert_called_once_with(expected)
                client.close()

    def test_timeout_must_be_finite_positive_number(self):
        invalid: list[Any] = [None, 0, -1, float("nan"), float("inf"), "secret"]
        for timeout in invalid:
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout"):
                    self.client_class(PYTHON, "en", "en_core_web_sm", timeout=timeout)

    def test_spawn_exception_is_static_without_original_traceback(self):
        from hermes_pii.runtime_client import RuntimeClient

        client = RuntimeClient(PYTHON, "en", "en_core_web_sm")
        self.addCleanup(client.close)
        with patch("hermes_pii.runtime_client.subprocess.Popen", side_effect=OSError("synthetic-secret")):
            with self.assertRaises(RuntimeError) as caught:
                client.redact("synthetic-secret")
        self.assertNotIn("synthetic-secret", "".join(traceback.format_exception(caught.exception)))


class WorkerProtocolTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX SIGTERM")
    def test_sigterm_unwinds_owned_temp_context_even_if_tempfile_was_cached(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory)
            owned = root / "owned"
            owned.mkdir()
            ready, unwound = root / "ready", root / "unwound"
            probe = root / "probe.py"
            probe.write_text(
                "import os, sys, tempfile, time\n"
                "from pathlib import Path\n"
                "tempfile.gettempdir()\n"
                f"os.environ['TMPDIR'] = {str(owned)!r}\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from hermes_pii.worker import RuntimeWorker, main\n"
                "def handle(self, request):\n"
                "    with tempfile.TemporaryDirectory() as directory:\n"
                "        Path(directory, 'raw-document.txt').write_text('synthetic-sensitive-document')\n"
                f"        Path({str(ready)!r}).write_text(directory)\n"
                "        try: time.sleep(20)\n"
                f"        finally: Path({str(unwound)!r}).write_text('unwound')\n"
                "RuntimeWorker.handle = handle\n"
                "main()\n"
            )
            with REAL_POPEN([sys.executable, "-I", "-u", str(probe)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as process:
                assert process.stdin is not None
                process.stdin.write(b'{}\n')
                process.stdin.flush()
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                process.terminate()
                process.wait(timeout=2)
            actual = Path(ready.read_text())
            self.addCleanup(shutil.rmtree, actual, True)
            self.assertTrue(unwound.exists(), "SIGTERM did not unwind temporary contexts")
            self.assertEqual(actual.parent, owned)
            self.assertFalse(actual.exists())

    def test_detector_is_loaded_once_and_stdout_noise_cannot_corrupt_protocol(self):
        from types import SimpleNamespace
        from hermes_pii.worker import _response

        redactor = Mock(language="en")
        redactor.redact.return_value = "[EMAIL_ADDRESS]"
        factory = Mock(return_value=redactor)
        worker = RuntimeWorker()
        request = {"op": "redact", "text": "alice@example.com", "config": {"language": "en", "model_name": "local"}}
        with patch.dict(sys.modules, {"hermes_pii.redactor": SimpleNamespace(PresidioRedactor=factory)}):
            self.assertEqual(worker.handle(request), "[EMAIL_ADDRESS]")
            self.assertEqual(worker.handle(request), "[EMAIL_ADDRESS]")
        factory.assert_called_once_with(language="en", model_name="local")

        def noisy_handle(request):
            print("synthetic-secret")
            return "[EMAIL_ADDRESS]"

        with patch.object(worker, "handle", side_effect=noisy_handle), patch("sys.stderr", StringIO()):
            self.assertEqual(json.loads(_response(worker, "{}")), {"ok": True, "result": "[EMAIL_ADDRESS]"})

    def test_serialization_and_detector_failures_return_only_static_errors(self):
        from hermes_pii.worker import _response

        worker = Mock()
        worker.handle.side_effect = [ValueError("synthetic-secret"), object()]
        expected = {"ok": False, "error": "PII runtime request failed"}
        self.assertEqual(json.loads(_response(worker, "not-json synthetic-secret")), expected)
        self.assertEqual(json.loads(_response(worker, "{}")), expected)
        self.assertEqual(json.loads(_response(worker, "{}")), expected)

    def test_invalid_requests_rejected_before_detector_initialization(self):
        requests = [None, [], {}, {"op": "unknown"}, {"op": "redact", "text": {}},
                    {"op": "detect_spans", "text": 1}, {"op": "sanitize_part", "part": []}]
        for request in requests:
            with self.subTest(request=request):
                worker = RuntimeWorker()
                with self.assertRaises(ValueError):
                    worker.handle(request)
                self.assertIsNone(worker._redactor)

    def test_worker_disables_standard_socket_networking(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            probe = Path(directory) / "probe.py"
            probe.write_text(
                "import sys, socket\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from hermes_pii.worker import _disable_network\n"
                "_disable_network()\n"
                "blocked = 0\n"
                "for attempt in (lambda: socket.socket(), lambda: socket.getaddrinfo('localhost', 9), "
                "lambda: socket.create_connection(('127.0.0.1', 9)), lambda: socket.socketpair()):\n"
                "    try: attempt()\n"
                "    except OSError: blocked += 1\n"
                "assert blocked == 4, blocked\n"
                "print('network blocked')\n"
            )
            result = subprocess.run([sys.executable, "-I", str(probe)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "network blocked")


class WorkerAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.worker = RuntimeWorker()
        self.detector = Mock()
        self.worker._redactor = self.detector
        self.detector.detect_spans.return_value = []
        self.request = {"op": "sanitize_part", "part": {"type": "input_image", "image_url": "data:image/png;base64,AA=="}, "language": "it"}

    def test_detector_failure_survives_sanitizer_exception_swallowing(self):
        self.detector.detect_spans.side_effect = ValueError("synthetic-secret")

        def image(part, *, detect_spans, language):
            detect_spans("synthetic-secret")

        with self.assertLogs("hermes_pii.attachments", level="WARNING") as logs:
            with patch("hermes_pii.attachments._sanitize_image_part", side_effect=image):
                result = self.worker.handle(self.request)
        self.assertNotIn("synthetic-secret", " ".join(logs.output))
        assert isinstance(result, dict)
        self.assertIs(result["detector_error"], True)
        self.assertIn("omitted", result["part"]["text"])

    def test_non_detector_attachment_failure_is_not_detector_failure(self):
        with self.assertLogs("hermes_pii.attachments", level="WARNING"):
            with patch("hermes_pii.attachments._sanitize_image_part", side_effect=ValueError("bad image")):
                result = self.worker.handle(self.request)
        assert isinstance(result, dict)
        self.assertIs(result["detector_error"], False)
        self.assertIn("omitted", result["part"]["text"])

    def test_office_page_list_and_language_pass_through(self):
        pages = [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]
        with patch("hermes_pii.attachments.sanitize_content_part", return_value=pages) as sanitizer:
            result = self.worker.handle(self.request)
        self.assertEqual(result, {"part": pages, "detector_error": False})
        self.assertEqual(sanitizer.call_args.kwargs["language"], "it")


class RealRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_real_worker_sanitizes_image_pixels(self):
        from PIL import Image, ImageDraw, ImageFont
        from hermes_pii.runtime_client import RuntimeClient

        image = Image.new("RGB", (900, 110), "white")
        ImageDraw.Draw(image).text((20, 20), "alice@example.com", fill="black", font=ImageFont.load_default(size=40))
        output = BytesIO()
        image.save(output, format="PNG")
        original = output.getvalue()
        part = {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(original).decode("ascii")}
        client = RuntimeClient(PYTHON, "en", "en_core_web_sm")
        self.addCleanup(client.close)
        result = client.sanitize_part(part)
        self.assertFalse(result["detector_error"])
        self.assertEqual(result["part"]["type"], "input_image")
        safe = base64.b64decode(result["part"]["image_url"].split(",", 1)[1])
        with Image.open(BytesIO(safe)) as redacted:
            self.assertGreater(redacted.histogram()[0], image.histogram()[0])
        self.assertEqual(base64.b64decode(part["image_url"].split(",", 1)[1]), original)

    def test_client_works_in_host_with_site_packages_disabled(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            probe = Path(directory) / "host.py"
            probe.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from hermes_pii.runtime_client import RuntimeClient\n"
                f"client = RuntimeClient({str(PYTHON)!r}, 'en', 'en_core_web_sm')\n"
                "print(client.redact('alice@example.com'))\n"
                "assert not any(name in sys.modules for name in ('spacy', 'PIL', 'presidio_analyzer'))\n"
                "client.close()\n"
            )
            result = subprocess.run([sys.executable, "-I", "-S", str(probe)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("alice@example.com", result.stdout)
        self.assertIn("EMAIL_ADDRESS", result.stdout)

    def test_uninspectable_real_attachment_omits_without_detector_failure(self):
        from hermes_pii.runtime_client import RuntimeClient

        client = RuntimeClient(PYTHON, "en", "en_core_web_sm")
        self.addCleanup(client.close)
        result = client.sanitize_part({"type": "input_image", "image_url": "data:image/png;base64,AA=="})
        assert isinstance(result, dict)
        self.assertIs(result["detector_error"], False)
        self.assertIn("omitted", result["part"]["text"])

    def test_missing_model_fails_safely_then_recovers_with_local_model(self):
        from hermes_pii.runtime_client import RuntimeClient

        client = RuntimeClient(PYTHON, "en", "synthetic-secret-missing-model")
        self.addCleanup(client.close)
        with self.assertRaises(RuntimeError) as caught:
            client.redact("alice@example.com")
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("synthetic-secret", rendered)
        self.assertNotIn("alice@example.com", rendered)
        client.model_name = "en_core_web_sm"
        self.assertNotIn("alice@example.com", client.redact("alice@example.com"))

    def test_worker_exits_cleanly_on_stdin_eof(self):
        result = subprocess.run([str(PYTHON), "-I", "-u", str(ROOT / "hermes_pii" / "worker.py")],
                                input="not-json synthetic-secret\n", text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"ok": False, "error": "PII runtime request failed"})
        self.assertEqual(result.stderr, "")

    def test_persistent_real_worker_redacts_and_returns_spans(self):
        from hermes_pii.runtime_client import RuntimeClient

        client = RuntimeClient(PYTHON, "en", "en_core_web_sm")
        self.addCleanup(client.close)
        text = "Email alice@example.com today."
        self.assertNotIn("alice@example.com", client.redact(text))
        process = client._process
        assert process is not None
        spans = client.detect_spans(text)
        self.assertTrue(any(text[start:end] == "alice@example.com" for start, end, _ in spans))
        self.assertIs(client._process, process)
        self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
