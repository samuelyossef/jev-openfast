"""Local Office files managed independently from browser conversations."""

import io
import os
import re
import sqlite3
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from .model import CHAT_URL, openrouter_key, post_json

ROOT = Path(__file__).resolve().parent.parent / "artifacts" / "documents"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
PARTS = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def check_office_file(name: str, data: bytes) -> tuple[str, str]:
    if not isinstance(name, str) or not name.strip() or len(name) > 180:
        raise ValueError("Nome de arquivo inválido.")
    clean = Path(name.replace("\\", "/")).name.strip()
    extension = Path(clean).suffix.lower()
    if clean != name or extension not in PARTS or not 0 < len(data) <= MAX_FILE_BYTES:
        raise ValueError("Envie um DOCX, XLSX ou PPTX válido de até 8 MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (len(entries) > 4000 or sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES
                    or "[Content_Types].xml" not in archive.namelist() or PARTS[extension] not in archive.namelist()):
                raise ValueError("Arquivo Office inválido ou grande demais.")
            ElementTree.fromstring(archive.read(PARTS[extension]))
    except (zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, KeyError) as error:
        raise ValueError("Arquivo Office inválido.") from error
    return clean, extension


def make_docx(text: str) -> bytes:
    paragraphs = []
    for line in text.splitlines()[:250]:
        safe = escape(line[:4000])
        paragraphs.append(f'<w:p><w:r><w:t xml:space="preserve">{safe}</w:t></w:r></w:p>')
    body = "".join(paragraphs) or "<w:p/>"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>'
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" '
                         'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/word/document.xml" '
                         'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                         '</Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" '
                         'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                         'Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                         f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}'
                         '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
                         '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
                         '</w:sectPr></w:body></w:document>')
    return output.getvalue()


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    lines = []
    for paragraph in root.iter(f"{{{WORD_NS}}}p"):
        line = "".join(node.text or "" for node in paragraph.iter(f"{{{WORD_NS}}}t"))
        lines.append(line)
    return "\n".join(lines)[:24000]


def preview_text(name: str, data: bytes) -> str:
    """A local, text-only inspection; it does not claim Office layout fidelity."""
    kind = Path(name).suffix.lower()
    if kind == ".docx":
        return docx_text(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if kind == ".pptx":
            slides = sorted((part for part in archive.namelist()
                             if re.fullmatch(r"ppt/slides/slide\d+\.xml", part)),
                            key=lambda part: int(re.search(r"slide(\d+)", part).group(1)))
            output = []
            for index, part in enumerate(slides[:100], start=1):
                root = ElementTree.fromstring(archive.read(part))
                words = [node.text or "" for node in root.iter()
                         if node.tag == "{http://schemas.openxmlformats.org/drawingml/2006/main}t"]
                output.append(f"Slide {index}\n" + "\n".join(words))
            return "\n\n".join(output)[:24000]
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                shared.append("".join(node.text or "" for node in item.iter()
                                      if node.tag == "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
        sheets = sorted(part for part in archive.namelist()
                        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", part))
        output = []
        for index, part in enumerate(sheets[:30], start=1):
            root = ElementTree.fromstring(archive.read(part))
            rows = []
            for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                values = []
                for cell in row.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                    value = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                    raw = value.text if value is not None else ""
                    if cell.get("t") == "s" and raw and raw.isdecimal() and int(raw) < len(shared):
                        raw = shared[int(raw)]
                    values.append(raw or "")
                rows.append("\t".join(values))
                if len(rows) >= 500:
                    break
            output.append(f"Planilha {index}\n" + "\n".join(rows))
        return "\n\n".join(output)[:24000]


def write_with_model(instruction: str, source: str = "") -> str:
    prompt = ("Produza apenas o conteúdo final de um documento DOCX em português. "
              "Use texto simples, títulos e parágrafos. Não use cercas de código. "
              "Não invente fatos, números ou fontes. Limite a 12000 caracteres.\n"
              f"Pedido: {instruction[:2000]}")
    if source:
        prompt += f"\nDocumento atual para revisar:\n{source[:24000]}"
    result = post_json(CHAT_URL, openrouter_key(), {
        "model": os.environ.get("TEXT_MODEL", "inception/mercury-2.5"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 3500,
    })
    try:
        content = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError, TypeError) as error:
        raise RuntimeError("A IA não retornou um documento válido.") from error
    if not content:
        raise RuntimeError("A IA retornou um documento vazio.")
    return content[:12000]


class DocumentStore:
    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.database = self.root / "documents.sqlite3"

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE IF NOT EXISTS documents ("
                           "id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, origin TEXT NOT NULL, "
                           "created_at INTEGER NOT NULL, parent_id TEXT)")
        return connection

    def list(self):
        with self._connect() as db:
            rows = db.execute("SELECT id, name, kind, origin, created_at, parent_id FROM documents "
                              "ORDER BY created_at DESC").fetchall()
        return [dict(zip(("id", "name", "kind", "origin", "created_at", "parent_id"), row, strict=True))
                for row in rows]

    def save(self, name: str, data: bytes, origin: str, parent_id: str | None = None):
        name, extension = check_office_file(name, data)
        identifier = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.root / f"{identifier}{extension}")
            with self._connect() as db:
                db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                           (identifier, name, extension.removeprefix("."), origin, time.time_ns() // 1_000_000,
                            parent_id))
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            (self.root / f"{identifier}{extension}").unlink(missing_ok=True)
            raise
        return {"id": identifier, "name": name, "kind": extension.removeprefix("."), "origin": origin,
                "parent_id": parent_id}

    def get(self, identifier: str):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Documento inválido.")
        with self._connect() as db:
            row = db.execute("SELECT name, kind FROM documents WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Documento não encontrado.")
        return row[0], (self.root / f"{identifier}.{row[1]}").read_bytes()

    def delete(self, identifier: str):
        name, _ = self.get(identifier)
        with self._connect() as db:
            db.execute("DELETE FROM documents WHERE id=?", (identifier,))
        (self.root / f"{identifier}{Path(name).suffix.lower()}").unlink(missing_ok=True)

    def create(self, name: str, instruction: str):
        if not isinstance(instruction, str) or not 2 <= len(instruction.strip()) <= 2000:
            raise ValueError("Descreva o documento em até 2000 caracteres.")
        if not isinstance(name, str) or not name.lower().endswith(".docx"):
            raise ValueError("O documento criado pela IA deve ter extensão .docx.")
        return self.save(name, make_docx(write_with_model(instruction)), "ai")

    def revise(self, identifier: str, instruction: str):
        if not isinstance(instruction, str) or not 2 <= len(instruction.strip()) <= 2000:
            raise ValueError("Descreva a alteração em até 2000 caracteres.")
        name, data = self.get(identifier)
        if Path(name).suffix.lower() != ".docx":
            raise ValueError("A revisão pela IA está disponível para DOCX.")
        revised = write_with_model(instruction, docx_text(data))
        return self.save(name, make_docx(revised), "ai_revision", identifier)
