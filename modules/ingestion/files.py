import codecs
from pathlib import Path
from typing import BinaryIO

SUPPORTED = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"PK\x03\x04",
    ),
    ".json": ("application/json", None),
    ".csv": ("text/csv", None),
    ".txt": ("text/plain", None),
    ".md": ("text/markdown", None),
    ".markdown": ("text/markdown", None),
}


def validate_upload(
    filename: str | None, declared_mime: str | None, file: BinaryIO
) -> tuple[str, str, str]:
    """Validate extension, declared type, signature, archive structure, and UTF-8; rewind before returning."""
    extension = Path(filename or "").suffix.lower()
    supported = SUPPORTED.get(extension)
    if supported is None:
        raise ValueError("Unsupported file extension")
    mime, signature = supported
    declared_type = (declared_mime or "").partition(";")[0].strip().lower()
    if declared_type not in ("", "application/octet-stream", mime):
        raise ValueError("File extension and MIME type do not match")
    header = file.read(8)
    file.seek(0)
    if signature is not None and not header.startswith(signature):
        raise ValueError("File signature does not match its extension")
    if extension in {".docx"}:
        import zipfile

        try:
            with zipfile.ZipFile(file) as archive:
                names = set(archive.namelist())
            file.seek(0)
        except zipfile.BadZipFile as exc:
            raise ValueError("Invalid DOCX file") from exc
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise ValueError("Invalid DOCX file")
    elif extension in {".txt", ".md", ".markdown", ".csv", ".json"}:
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            while block := file.read(1024 * 1024):
                decoder.decode(block)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ValueError("Text files must use UTF-8 encoding") from exc
        finally:
            file.seek(0)
    return extension, mime, Path(filename or "upload").name
