import pathlib
import magic
from fastapi import HTTPException

ALLOWED_SUFFIXES = {".pdf", ".docx", ".pptx", ".txt", ".md", ".png", ".jpg", ".jpeg"}

ALLOWED_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

def guard_file(file_bytes: bytes, filename: str) -> pathlib.Path:
    suffix = pathlib.Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension {suffix!r}."
        )

    mime = magic.from_buffer(file_bytes, mime=True)
    expected_mime = ALLOWED_MIME[suffix]
    

    if not mime.startswith(expected_mime.split("/")[0]):
        raise HTTPException(
            status_code=415,
            detail=f"File content type ({mime}) does not match extension '{suffix}'."
        )

    return pathlib.Path(filename)