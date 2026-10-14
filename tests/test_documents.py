"""Offline document storage and generation checks."""

import io
import zipfile

import pytest

from jev_ultrafast import documents


def test_created_docx_is_valid_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "write_with_model", lambda instruction, source="": "Título\nPrimeiro parágrafo")
    store = documents.DocumentStore(tmp_path)
    created = store.create("Relatório.docx", "Crie um relatório")
    name, data = documents.DocumentStore(tmp_path).get(created["id"])
    assert name == "Relatório.docx"
    assert documents.docx_text(data) == "Título\nPrimeiro parágrafo"
    assert documents.DocumentStore(tmp_path).list()[0]["id"] == created["id"]


def test_revision_preserves_original(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "write_with_model", lambda instruction, source="": f"Revisto: {source}")
    store = documents.DocumentStore(tmp_path)
    original = store.save("Texto.docx", documents.make_docx("Original"), "upload")
    revision = store.revise(original["id"], "Revise o texto")
    assert revision["parent_id"] == original["id"]
    assert documents.docx_text(store.get(original["id"])[1]) == "Original"
    assert documents.docx_text(store.get(revision["id"])[1]) == "Revisto: Original"


@pytest.mark.parametrize("name,data", [
    ("../../secreto.docx", documents.make_docx("x")),
    ("planilha.xlsx", documents.make_docx("x")),
    ("macro.docm", documents.make_docx("x")),
    ("texto.docx", b"not a zip"),
])
def test_invalid_upload_rejected(tmp_path, name, data):
    with pytest.raises(ValueError):
        documents.DocumentStore(tmp_path).save(name, data, "upload")


def test_oversized_uncompressed_document_rejected(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "x" * (documents.MAX_UNCOMPRESSED_BYTES + 1))
    with pytest.raises(ValueError):
        documents.DocumentStore(tmp_path).save("large.docx", output.getvalue(), "upload")


def test_deletion_removes_file_and_metadata(tmp_path):
    store = documents.DocumentStore(tmp_path)
    created = store.save("Texto.docx", documents.make_docx("Olá"), "upload")
    store.delete(created["id"])
    assert store.list() == []
    with pytest.raises(ValueError):
        store.get(created["id"])
