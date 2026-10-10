"""Render local Office documents to pages; never send the original package."""
from io import BytesIO
from pathlib import Path, PurePosixPath
import subprocess
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZipFile

OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx"})
MAX_OFFICE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_OFFICE_PAGES = 20
_CONVERSION_TIMEOUT = 60
_RENDER_TIMEOUT = 60
_MAX_PAGE_BYTES = 20 * 1024 * 1024


def _validate_package(raw, extension):
    """Bound ZIP expansion and reject active/linked content before conversion."""
    if extension not in OFFICE_EXTENSIONS or len(raw) > MAX_OFFICE_BYTES:
        raise ValueError("unsupported or oversized Office file")
    with ZipFile(BytesIO(raw)) as package:
        members = package.infolist()
        if len(members) > 2000 or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
            raise ValueError("Office package exceeds expansion limits")
        names = [item.filename for item in members]
        if len(set(names)) != len(names) or any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts for name in names):
            raise ValueError("invalid Office package paths")
        required = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
        if required not in package.namelist():
            raise ValueError("Office package does not match its extension")
        for item in members:
            name = item.filename.lower()
            if item.flag_bits & 1 or name.endswith("vbaproject.bin") or "/embeddings/" in name:
                raise ValueError("encrypted or embedded active content is unsupported")
            if name.endswith(".rels"):
                root = ElementTree.fromstring(package.read(item))
                if any(node.get("TargetMode", "").lower() == "external" for node in root):
                    raise ValueError("external Office relationships are unsupported")


def _run(argv, timeout):
    # Converter output can contain document text or paths; discard it entirely.
    subprocess.run(
        argv, check=True, timeout=timeout, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def render_office_pages(raw, extension):
    """Yield bounded PNG pages from DOCX/XLSX, cleaning up even after OCR errors."""
    _validate_package(raw, extension)
    with TemporaryDirectory(prefix="hermes-pii-office-") as directory:
        root = Path(directory)
        source = root / ("document" + extension)
        source.write_bytes(raw)
        profile = root / "profile"
        profile.mkdir()
        (profile / "user").mkdir()
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
            '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>'
            '</item></oor:items>', encoding="utf-8",
        )
        _run([
            "soffice", "-env:UserInstallation=" + profile.as_uri(),
            "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(root), str(source),
        ], _CONVERSION_TIMEOUT)
        pdf = root / "document.pdf"
        if not pdf.is_file() or pdf.stat().st_size > MAX_EXPANDED_BYTES:
            raise ValueError("Office conversion produced no bounded PDF")
        # Render one extra page so an oversized document is rejected, never truncated.
        _run([
            "pdftoppm", "-png", "-r", "150", "-scale-to", "2400",
            "-f", "1", "-l", str(MAX_OFFICE_PAGES + 1), str(pdf), str(root / "page"),
        ], _RENDER_TIMEOUT)
        pages = sorted(root.glob("page-*.png"), key=lambda path: int(path.stem.split("-")[-1]))
        if not pages or len(pages) > MAX_OFFICE_PAGES:
            raise ValueError("Office document has no pages or exceeds the page limit")
        for page in pages:
            if page.stat().st_size > _MAX_PAGE_BYTES:
                raise ValueError("Office page exceeds the size limit")
            yield page.read_bytes()
