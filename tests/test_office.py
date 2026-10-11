"""Office rendering, user references, and fail-closed provider boundary checks."""
import base64
from copy import deepcopy
from io import BytesIO
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from hermes_pii.attachments import OCRWord
from hermes_pii.office import _validate_package, render_office_pages
from hermes_pii.plugin import PiiPlugin


def office_package(extension, text="alice@example.com", extra=None):
    """Build minimal real OOXML documents using synthetic content only."""
    if extension == ".docx":
        main = "word/document.xml"
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        document = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Contact: ' + text + '</w:t></w:r></w:p>'
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:body></w:document>'
        )
        files = {main: document}
    else:
        main = "xl/workbook.xml"
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
        files = {
            main: '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Contacts" sheetId="1" r:id="rId1"/></sheets></workbook>',
            'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
            'xl/worksheets/sheet1.xml': '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cols><col min="1" max="1" width="35" customWidth="1"/></cols><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Contact: ' + text + '</t></is></c></row>'
            '</sheetData></worksheet>',
        }
    files['[Content_Types].xml'] = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/' + main + '" ContentType="' + content_type + '"/>'
        + ('<Override PartName="/xl/worksheets/sheet1.xml" '
           'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' if extension == '.xlsx' else '')
        + '</Types>'
    )
    files['_rels/.rels'] = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="' + main + '"/></Relationships>'
    )
    files.update(extra or {})
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as package:
        for name, value in files.items():
            package.writestr(name, value)
    return output.getvalue()


def white_page():
    output = BytesIO()
    Image.new("RGB", (120, 60), "white").save(output, format="PNG")
    return output.getvalue()


class FakeRedactor:
    def detect_spans(self, text):
        return [(0, len(text), "EMAIL_ADDRESS")] if text else []

    def redact(self, text):
        return text.replace("alice@example.com", "<EMAIL_ADDRESS>")


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.plugin = PiiPlugin()
        self.plugin._redactor = FakeRedactor()

    def test_inline_office_pages_are_blackened_in_chat_and_responses(self):
        for extension in (".docx", ".xlsx"):
            for responses in (False, True):
                with self.subTest(extension=extension, responses=responses):
                    raw = office_package(extension)
                    encoded = base64.b64encode(raw).decode("ascii")
                    spec = {"filename": "alice@example.com" + extension, "file_data": "data:application/octet-stream;base64," + encoded if responses else encoded}
                    part = {"type": "input_file", **spec} if responses else {"type": "file", "file": spec}
                    field = "input" if responses else "messages"
                    request = {field: [{"role": "user", "content": [part]}]}
                    original = deepcopy(request)
                    with (
                        patch("hermes_pii.attachments.render_office_pages", side_effect=lambda *args: (page for page in [white_page(), white_page()])) as render,
                        patch("hermes_pii.attachments._ocr_words", return_value=("alice@example.com", [OCRWord(0, 17, 10, 10, 100, 30)])),
                        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
                    ):
                        sent = self.plugin.llm_request(request)["request"]
                    render.assert_called_once_with(raw, extension)
                    pages = sent[field][0]["content"]
                    self.assertEqual(len(pages), 2)
                    for page in pages:
                        self.assertEqual(page["type"], "input_image" if responses else "image_url")
                        url = page["image_url"] if responses else page["image_url"]["url"]
                        with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
                            self.assertEqual(image.getpixel((50, 20)), (0, 0, 0))
                            self.assertEqual(image.getpixel((110, 50)), (255, 255, 255))
                    self.assertEqual(request, original)
                    self.assertNotIn(encoded, repr(sent))
                    self.assertNotIn("alice@example.com", repr(sent))

    def test_conversion_or_later_page_failure_omits_whole_document(self):
        def broken_pages(raw, extension):
            yield white_page()
            raise subprocess.TimeoutExpired("converter", 60)

        part = {"type": "input_file", "filename": "test.docx", "file_data": base64.b64encode(office_package(".docx")).decode("ascii")}
        for failure in (broken_pages, FileNotFoundError("private path")):
            with self.subTest(failure=type(failure).__name__):
                with (
                    patch("hermes_pii.attachments.render_office_pages", side_effect=failure),
                    patch("hermes_pii.attachments._ocr_words", return_value=("", [])),
                    self.assertLogs("hermes_pii.attachments", level="WARNING") as logs,
                ):
                    sent = self.plugin.llm_request({"input": [{"role": "user", "content": [part]}]})["request"]
                content = sent["input"][0]["content"]
                self.assertEqual(len(content), 1)
                self.assertEqual(content[0]["type"], "input_text")
                self.assertIn("Attachment omitted", content[0]["text"])
                self.assertNotIn("private path", repr(logs.output))

    def test_later_ocr_failure_omits_all_pages_and_closes_renderer(self):
        closed = []

        def pages(raw, extension):
            try:
                yield white_page()
                yield white_page()
            finally:
                closed.append(True)

        part = {"type": "input_file", "filename": "test.xlsx", "file_data": base64.b64encode(office_package(".xlsx")).decode("ascii")}
        with (
            patch("hermes_pii.attachments.render_office_pages", side_effect=pages),
            patch("hermes_pii.attachments._ocr_words", side_effect=[("", []), RuntimeError("private contents")]),
        ):
            sent = self.plugin.llm_request({"input": [{"role": "user", "content": [part]}]})["request"]
        self.assertEqual(closed, [True])
        self.assertEqual(len(sent["input"][0]["content"]), 1)
        self.assertIn("Attachment omitted", repr(sent))
        self.assertNotIn("input_image", repr(sent))

    def test_opaque_legacy_invalid_and_oversized_payloads_never_render(self):
        parts = [
            {"type": "input_file", "file_id": "opaque"},
            {"type": "input_file", "filename": "legacy.xls", "file_data": "YWJj"},
            {"type": "input_file", "filename": "bad.docx", "file_data": "bad base64!"},
            {"type": "input_file", "filename": "bad.xlsx", "file_data": "data:application/octet-stream,plain"},
            {"type": "input_file", "filename": "large.docx", "file_data": base64.b64encode(b"x" * 11).decode("ascii")},
        ]
        with patch("hermes_pii.attachments.MAX_OFFICE_BYTES", 10), patch("hermes_pii.attachments.render_office_pages") as render:
            sent = self.plugin.llm_request({"input": [{"role": "user", "content": parts}]})["request"]
        render.assert_not_called()
        self.assertTrue(all("Attachment omitted" in part["text"] for part in sent["input"][0]["content"]))

    def test_office_references_replace_hermes_binary_stub_and_preserve_original(self):
        from agent.context_references import preprocess_context_references

        with TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "synthetic file.docx"
            raw = office_package(".docx")
            path.write_bytes(raw)
            ref = '@file:"' + str(path.relative_to(Path.cwd())) + '"'
            expanded = preprocess_context_references("Summarize " + ref, cwd=Path.cwd(), context_length=10000).message
            for text in ("Summarize " + ref, expanded):
                for responses in (False, True):
                    with self.subTest(expanded=text == expanded, responses=responses):
                        request = {"input": text} if responses else {"messages": [{"role": "user", "content": text}]}
                        original = deepcopy(request)
                        with (
                            patch("hermes_pii.attachments.render_office_pages", side_effect=lambda *args: (page for page in [white_page()])),
                            patch("hermes_pii.attachments._ocr_words", return_value=("", [])),
                        ):
                            sent = self.plugin.llm_request(request)["request"]
                        field = "input" if responses else "messages"
                        self.assertTrue(any(part["type"] == ("input_image" if responses else "image_url") for part in sent[field][0]["content"]))
                        self.assertNotIn("available on disk", repr(sent))
                        self.assertNotIn("@file:", repr(sent))
                        self.assertEqual(request, original)
                        self.assertEqual(path.read_bytes(), raw)

    def test_outside_workspace_and_symlink_references_are_omitted(self):
        with TemporaryDirectory() as outside, TemporaryDirectory(dir=Path.cwd()) as inside:
            source = Path(outside) / "private.docx"
            source.write_bytes(office_package(".docx"))
            link = Path(inside) / "link.docx"
            link.symlink_to(source)
            for path in (source, link):
                with patch("hermes_pii.attachments.render_office_pages") as render:
                    sent = self.plugin.llm_request({"input": '@file:"' + str(path) + '"'})["request"]
                render.assert_not_called()
                self.assertIn("Attachment omitted", repr(sent))
                self.assertNotIn(str(path), repr(sent))

    def test_active_linked_corrupt_and_expanding_packages_are_rejected(self):
        external = '<Relationships><Relationship TargetMode="External" Target="https://example.com"/></Relationships>'
        for extra in ({"word/vbaProject.bin": "macro"}, {"word/embeddings/item.bin": "object"}, {"word/_rels/document.xml.rels": external}):
            with self.subTest(extra=list(extra)):
                with self.assertRaises(ValueError):
                    _validate_package(office_package(".docx", extra=extra), ".docx")
        with patch("hermes_pii.office.MAX_EXPANDED_BYTES", 10), self.assertRaises(ValueError):
            _validate_package(office_package(".docx"), ".docx")
        with patch("hermes_pii.office._run") as run, self.assertRaises(Exception):
            list(render_office_pages(b"not a ZIP", ".docx"))
        run.assert_not_called()

    @unittest.skipUnless(os.environ.get("HERMES_PII_OFFICE_SMOKE") == "1", "requires LibreOffice, Poppler, Tesseract, and the NLP model")
    def test_real_office_conversion_and_ocr_redaction(self):
        from hermes_pii.redactor import PresidioRedactor
        import pytesseract

        plugin = PiiPlugin()
        plugin._redactor = PresidioRedactor()
        for extension in (".docx", ".xlsx"):
            with self.subTest(extension=extension):
                pages = list(render_office_pages(office_package(extension), extension))
                self.assertEqual(len(pages), 1)
                before = pytesseract.image_to_string(Image.open(BytesIO(pages[0])))
                part = {"type": "input_file", "filename": "test" + extension,
                        "file_data": base64.b64encode(office_package(extension)).decode("ascii")}
                sent = plugin.llm_request({"input": [{"role": "user", "content": [part]}]})["request"]
                image_part = sent["input"][0]["content"][0]
                self.assertEqual(image_part["type"], "input_image")
                safe = base64.b64decode(image_part["image_url"].split(",", 1)[1])
                after = pytesseract.image_to_string(Image.open(BytesIO(safe)))
                self.assertIn("alice@example.com", before)
                self.assertNotIn("alice@example.com", after)

    def test_renderer_uses_isolated_profile_orders_pages_and_cleans_up(self):
        roots = []

        def run(argv, timeout):
            if argv[0] == "soffice":
                root = Path(argv[argv.index("--outdir") + 1])
                roots.append(root)
                profile = (root / "profile/user/registrymodifications.xcu").read_text()
                self.assertIn("<value>3</value>", profile)
                self.assertIn('oor:name="DisableMacrosExecution"', profile)
                self.assertIn('oor:name="DisableActiveContent"', profile)
                (root / "document.pdf").write_bytes(b"synthetic PDF")
            else:
                root = Path(argv[-1]).parent
                (root / "page-2.png").write_bytes(b"second")
                (root / "page-1.png").write_bytes(b"first")

        with patch("hermes_pii.office._run", side_effect=run):
            self.assertEqual(list(render_office_pages(office_package(".docx"), ".docx")), [b"first", b"second"])
        self.assertFalse(roots[0].exists())
        with patch("hermes_pii.office.MAX_OFFICE_PAGES", 1), patch("hermes_pii.office._run", side_effect=run), self.assertRaises(ValueError):
            list(render_office_pages(office_package(".docx"), ".docx"))
        self.assertFalse(roots[-1].exists())


if __name__ == "__main__":
    unittest.main()
