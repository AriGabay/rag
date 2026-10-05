"""Small helpers shared by integration tests."""

from __future__ import annotations

import io

from pypdf import PdfWriter


def tiny_pdf(tag: str) -> bytes:
    """A valid one-page PDF whose bytes differ per ``tag``."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": tag})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def upload(client, files: list[tuple[str, bytes]], group_id=None, document_id=None):
    data = {}
    if group_id:
        data["group_id"] = str(group_id)
    if document_id:
        data["document_id"] = str(document_id)
    return client.post(
        "/api/documents",
        files=[("files", (name, content, "application/octet-stream")) for name, content in files],
        data=data,
    )
