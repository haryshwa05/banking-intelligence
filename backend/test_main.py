from io import BytesIO
from contextlib import closing

import fitz
from fastapi.testclient import TestClient

import main


def configure_test_storage(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "metadata.db")
    monkeypatch.setattr(main, "UPLOADS_DIR", tmp_path / "uploads")
    main.initialise_storage()


def make_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A native PDF text extraction test.")
    result = document.tobytes()
    document.close()
    return result


def test_native_pdf_upload_has_a_queued_extraction(monkeypatch, tmp_path) -> None:
    configure_test_storage(monkeypatch, tmp_path)
    client = TestClient(main.app)
    response = client.post("/uploads", files={"file": ("native.pdf", make_pdf(), "application/pdf")})

    assert response.status_code == 201
    uploaded = response.json()
    assert uploaded["extractionStatus"] == "queued"

    text, page_count, ocr_pages = main.extractor.extract_pdf(tmp_path / "uploads" / f"{uploaded['id']}.pdf")
    main.complete_job(uploaded["id"], text, page_count, ocr_pages)
    extraction = client.get(f"/uploads/{uploaded['id']}/extraction")

    assert extraction.status_code == 200
    assert extraction.json()["status"] == "completed"
    assert "native PDF text extraction test" in extraction.json()["text"]
    assert extraction.json()["ocrPageCount"] == 0
    assert client.get(f"/uploads/{uploaded['id']}/preview").status_code == 200


def test_office_file_is_stored_but_not_extracted(monkeypatch, tmp_path) -> None:
    configure_test_storage(monkeypatch, tmp_path)
    client = TestClient(main.app)
    response = client.post("/uploads", files={"file": ("notes.docx", BytesIO(b"document"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})

    assert response.status_code == 201
    assert response.json()["extractionStatus"] == "unsupported"


def test_upload_can_be_deleted(monkeypatch, tmp_path) -> None:
    configure_test_storage(monkeypatch, tmp_path)
    client = TestClient(main.app)
    uploaded = client.post("/uploads", files={"file": ("notes.txt", b"remove me", "text/plain")}).json()

    response = client.delete(f"/uploads/{uploaded['id']}")

    assert response.status_code == 204
    assert client.get("/uploads").json() == []
    assert not (tmp_path / "uploads" / f"{uploaded['id']}.txt").exists()


def add_indexed_chunk(upload_id: str, filename: str, chunk_id: str, text: str) -> None:
    with closing(main.get_connection()) as connection:
        connection.execute(
            "INSERT INTO uploads (id, original_name, stored_name, mime_type, size_bytes, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (upload_id, filename, f"{upload_id}.pdf", "application/pdf", 1, "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO extractions (upload_id, status, extracted_text, created_at, updated_at) VALUES (?, 'completed', ?, ?, ?)",
            (upload_id, text, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        connection.execute("INSERT INTO document_indexes (upload_id, status) VALUES (?, 'ready')", (upload_id,))
        connection.execute(
            "INSERT INTO chunks (id, upload_id, page_number, ordinal, text, content_hash) VALUES (?, ?, 1, 0, ?, 'hash')",
            (chunk_id, upload_id, text),
        )
        connection.execute("INSERT INTO chunk_fts (chunk_id, text) VALUES (?, ?)", (chunk_id, text))
        connection.commit()


def test_strict_retrieval_rejects_irrelevant_vendor_chunk(monkeypatch, tmp_path) -> None:
    configure_test_storage(monkeypatch, tmp_path)
    add_indexed_chunk("loan", "loan.pdf", "loan:0", "Tony Stark identity number SYN-ID-928374")
    add_indexed_chunk("vendor", "vendor.pdf", "vendor:0", "Pinnacle Freight statement of account with a balance")

    class FakeCollection:
        def query(self, **_kwargs):
            return {"ids": [["vendor:0", "loan:0"]]}

    monkeypatch.setattr(main.rag, "_collection_for", lambda: FakeCollection())
    monkeypatch.setattr(main.rag, "_embed", lambda _texts: [[0.0]])
    monkeypatch.setattr(
        main.rag,
        "_rerank",
        lambda _question, rows: [(row, 0.99 if row["id"] == "loan:0" else 0.02) for row in rows],
    )
    with closing(main.get_connection()) as connection:
        result = main.rag.retrieve(connection, "What is Tony's ID number?", None)

    assert [row["id"] for row in result] == ["loan:0"]


def test_question_returns_only_claude_selected_citations(monkeypatch, tmp_path) -> None:
    configure_test_storage(monkeypatch, tmp_path)
    add_indexed_chunk("loan", "loan.pdf", "loan:0", "Tony Stark identity number SYN-ID-928374")
    add_indexed_chunk("identity", "identity.png", "identity:0", "Tony Stark National Identity Card")
    with closing(main.get_connection()) as connection:
        chunks = connection.execute(
            "SELECT chunks.*, uploads.original_name FROM chunks JOIN uploads ON uploads.id = chunks.upload_id ORDER BY chunks.id"
        ).fetchall()
    monkeypatch.setattr(main.rag, "retrieve", lambda *_args: chunks)
    monkeypatch.setattr(main.rag, "answer", lambda *_args: ("Tony's ID is SYN-ID-928374.", ["loan:0"]))

    response = TestClient(main.app).post("/questions", json={"question": "What is Tony's ID number?"})

    assert response.status_code == 200
    assert response.json()["answer"] == "Tony's ID is SYN-ID-928374."
    assert response.json()["sources"] == [{"documentId": "loan", "filename": "loan.pdf", "pageNumber": 1, "chunkId": "loan:0"}]


def test_citation_sources_show_a_document_page_once() -> None:
    rows = [
        {"id": "fact:1", "document_id": "loan", "original_name": "loan.pdf", "page_number": 1},
        {"id": "fact:2", "document_id": "loan", "original_name": "loan.pdf", "page_number": 1},
        {"id": "fact:3", "document_id": "loan", "original_name": "loan.pdf", "page_number": 2},
        {"id": "fact:4", "document_id": "pay", "original_name": "pay.pdf", "page_number": 1},
    ]

    sources = main.citation_sources(rows, "document_id")

    assert sources == [
        {"documentId": "loan", "filename": "loan.pdf", "pageNumber": 1, "chunkId": "fact:1"},
        {"documentId": "loan", "filename": "loan.pdf", "pageNumber": 2, "chunkId": "fact:3"},
        {"documentId": "pay", "filename": "pay.pdf", "pageNumber": 1, "chunkId": "fact:4"},
    ]


def test_question_without_evidence_does_not_call_claude(monkeypatch, tmp_path) -> None:
    configure_test_storage(monkeypatch, tmp_path)
    monkeypatch.setattr(main.rag, "retrieve", lambda *_args: [])
    monkeypatch.setattr(main.rag, "answer", lambda *_args: (_ for _ in ()).throw(AssertionError("Claude should not be called")))

    response = TestClient(main.app).post("/questions", json={"question": "What is Tony's ID number?"})

    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert "strong enough evidence" in response.json()["answer"]
