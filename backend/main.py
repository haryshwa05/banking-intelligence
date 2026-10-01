from __future__ import annotations

import mimetypes
import json
import sqlite3
import threading
import uuid
import logging
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

from extraction import TextExtractor
from entity_resolution import EntityResolver
from facts import FactEngine
from rag import RagEngine
import chat_history

UPLOADS_DIR = APP_DIR / "uploads"
DATABASE_PATH = APP_DIR / "upload_metadata.db"
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_PDF_PAGES = 100

app = FastAPI(title="Document Intelligence API")
logger = logging.getLogger("document-library")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

extractor = TextExtractor()
rag = RagEngine()
facts = FactEngine(rag.embed_texts, rag.rerank_texts)
entities = EntityResolver()
worker_event = threading.Event()
worker_started = False
worker_start_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def is_extractable(mime_type: str) -> bool:
    return mime_type == "application/pdf" or mime_type.startswith("image/")


def citation_sources(rows: list[sqlite3.Row], document_id_key: str) -> list[dict[str, object]]:
    """Create display citations, once per document page, in evidence order."""
    sources: list[dict[str, object]] = []
    seen_pages: set[tuple[str, int]] = set()
    for row in rows:
        document_id = str(row[document_id_key])
        page_number = int(row["page_number"])
        page_key = (document_id, page_number)
        if page_key in seen_pages:
            continue
        seen_pages.add(page_key)
        sources.append(
            {
                "documentId": document_id,
                "filename": row["original_name"],
                "pageNumber": page_number,
                "chunkId": row["id"],
            }
        )
    return sources


def initialise_storage() -> None:
    UPLOADS_DIR.mkdir(exist_ok=True)
    with closing(get_connection()) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY, original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
                mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, uploaded_at TEXT NOT NULL
            )
            """
        )
        rag.initialise(connection)
        facts.initialise(connection)
        entities.initialise(connection)
        chat_history.initialise(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS extractions (
                upload_id TEXT PRIMARY KEY, status TEXT NOT NULL, extracted_text TEXT,
                page_count INTEGER, ocr_page_count INTEGER, error_message TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
                FOREIGN KEY (upload_id) REFERENCES uploads(id)
            )
            """
        )
        now = utc_now()
        connection.execute(
            """
            INSERT OR IGNORE INTO extractions (upload_id, status, created_at, updated_at)
            SELECT id,
                   CASE WHEN mime_type = 'application/pdf' OR mime_type LIKE 'image/%' THEN 'queued' ELSE 'unsupported' END,
                   ?, ?
            FROM uploads
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO document_indexes (upload_id, status)
            SELECT upload_id, 'queued' FROM extractions WHERE status = 'completed'
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO document_facts (upload_id, status, extraction_version)
            SELECT upload_id, 'queued', 'generic-facts-v1' FROM extractions WHERE status = 'completed'
            """
        )
        connection.execute(
            """INSERT OR IGNORE INTO document_entity_links(document_id, entity_id, status, confidence, reason, resolved_at)
            SELECT document_facts.upload_id, NULL, 'queued', NULL, NULL, NULL
            FROM document_facts WHERE document_facts.status='ready'"""
        )
        connection.commit()


def recover_interrupted_jobs() -> None:
    with closing(get_connection()) as connection:
        connection.execute(
            "UPDATE extractions SET status = 'queued', updated_at = ? WHERE status = 'processing'",
            (utc_now(),),
        )
        connection.execute("UPDATE document_entity_links SET status='queued', confidence=NULL, reason=NULL, resolved_at=NULL WHERE status='processing'")
        connection.execute(
            "UPDATE document_facts SET status='queued', error_message=NULL WHERE status IN ('processing', 'failed')"
        )
        connection.execute(
            """UPDATE document_facts SET status='queued', error_message=NULL
            WHERE status='ready' AND NOT EXISTS (SELECT 1 FROM facts WHERE facts.document_id=document_facts.upload_id)"""
        )
        connection.execute("UPDATE document_concepts SET status='queued', error_message=NULL WHERE status='processing'")
        connection.execute(
            """
            UPDATE document_indexes SET status = 'queued', error_message = NULL
            WHERE status IN ('indexing', 'failed')
              AND upload_id IN (SELECT upload_id FROM extractions WHERE status = 'completed')
            """
        )
        connection.commit()


def document_payload(row: sqlite3.Row) -> dict[str, str | int | None]:
    return {
        "id": row["id"], "name": row["original_name"], "type": row["mime_type"],
        "sizeBytes": row["size_bytes"], "uploadedAt": row["uploaded_at"],
        "url": f"/uploads/{row['id']}/file", "extractionStatus": row["extraction_status"],
        "indexStatus": row["index_status"] if "index_status" in row.keys() else None,
        "factStatus": row["fact_status"] if "fact_status" in row.keys() else None,
        "entityId": row["entity_id"] if "entity_id" in row.keys() else None,
        "entityName": row["entity_name"] if "entity_name" in row.keys() else None,
        "entityStatus": row["entity_status"] if "entity_status" in row.keys() else None,
        "entityReason": row["entity_reason"] if "entity_reason" in row.keys() else None,
    }


def get_upload(upload_id: str) -> sqlite3.Row:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT uploads.*, extractions.status AS extraction_status, document_indexes.status AS index_status, document_facts.status AS fact_status,
                   document_entity_links.entity_id, document_entity_links.status AS entity_status,
                   document_entity_links.reason AS entity_reason, entities.display_name AS entity_name
            FROM uploads JOIN extractions ON extractions.upload_id = uploads.id
            LEFT JOIN document_indexes ON document_indexes.upload_id = uploads.id
            LEFT JOIN document_facts ON document_facts.upload_id = uploads.id
            LEFT JOIN document_entity_links ON document_entity_links.document_id = uploads.id
            LEFT JOIN entities ON entities.id = document_entity_links.entity_id WHERE uploads.id = ?
            """, (upload_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return row


def enqueue_processing() -> None:
    worker_event.set()


def claim_next_job() -> sqlite3.Row | None:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT uploads.*, extractions.status AS extraction_status
            FROM uploads JOIN extractions ON extractions.upload_id = uploads.id
            WHERE extractions.status = 'queued' ORDER BY uploads.uploaded_at ASC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            "UPDATE extractions SET status = 'processing', updated_at = ?, error_message = NULL WHERE upload_id = ? AND status = 'queued'",
            (utc_now(), row["id"]),
        )
        connection.commit()
        return row if cursor.rowcount else None


def complete_job(upload_id: str, text: str, page_count: int, ocr_page_count: int) -> None:
    now = utc_now()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE extractions SET status = 'completed', extracted_text = ?, page_count = ?, ocr_page_count = ?,
            error_message = NULL, updated_at = ?, completed_at = ? WHERE upload_id = ?
            """, (text, page_count, ocr_page_count, now, now, upload_id),
        )
        connection.execute(
            """
            INSERT INTO document_indexes (upload_id, status, error_message, indexed_at)
            VALUES (?, 'queued', NULL, NULL)
            ON CONFLICT(upload_id) DO UPDATE SET
                status = 'queued', error_message = NULL, indexed_at = NULL
            """,
            (upload_id,),
        )
        facts.queue_document(connection, upload_id)
        connection.commit()


def process_pending_indexes(upload_id: str | None = None) -> None:
    filter_sql = " AND extractions.upload_id = ?" if upload_id else ""
    params = (upload_id,) if upload_id else ()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """ 
            SELECT extractions.upload_id, extractions.extracted_text
            FROM extractions JOIN document_indexes ON document_indexes.upload_id = extractions.upload_id
            WHERE extractions.status = 'completed' AND document_indexes.status = 'queued'
            """ + filter_sql + " ORDER BY extractions.completed_at ASC",
            params,
        ).fetchall()
    for row in rows:
        try:
            with closing(get_connection()) as connection:
                rag.index_document(connection, row["upload_id"], row["extracted_text"], utc_now())
                connection.commit()
        except Exception as error:
            with closing(get_connection()) as connection:
                rag.mark_failed(connection, row["upload_id"], error)
                connection.commit()


def process_pending_facts(upload_id: str | None = None) -> None:
    filter_sql = " AND extractions.upload_id = ?" if upload_id else ""
    params = (upload_id,) if upload_id else ()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT extractions.upload_id, extractions.extracted_text
            FROM extractions JOIN document_facts ON document_facts.upload_id = extractions.upload_id
            WHERE extractions.status='completed' AND document_facts.status='queued'
            """ + filter_sql + " ORDER BY extractions.completed_at ASC",
            params,
        ).fetchall()
    for row in rows:
        try:
            with closing(get_connection()) as connection:
                facts.extract_document(connection, row["upload_id"], row["extracted_text"], utc_now())
                connection.commit()
        except Exception as error:
            with closing(get_connection()) as connection:
                facts.mark_failed(connection, row["upload_id"], error)
                connection.commit()


def process_pending_entity_resolution(upload_id: str | None = None) -> None:
    filter_sql = " AND document_facts.upload_id = ?" if upload_id else ""
    params = (upload_id,) if upload_id else ()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """SELECT document_facts.upload_id FROM document_facts
            LEFT JOIN document_entity_links ON document_entity_links.document_id=document_facts.upload_id
            WHERE document_facts.status='ready' AND (document_entity_links.status IS NULL OR document_entity_links.status='queued')"""
            + filter_sql + " ORDER BY document_facts.upload_id",
            params,
        ).fetchall()
    for row in rows:
        with closing(get_connection()) as connection:
            entities.resolve_document(connection, row["upload_id"])
            connection.commit()


def process_pending_concept_mapping(upload_id: str | None = None) -> None:
    filter_sql = " AND document_concepts.upload_id = ?" if upload_id else ""
    params = (upload_id,) if upload_id else ()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            "SELECT upload_id FROM document_concepts WHERE status='queued'" + filter_sql + " ORDER BY upload_id",
            params,
        ).fetchall()
    for row in rows:
        try:
            with closing(get_connection()) as connection:
                facts.map_document_concepts(connection, row["upload_id"], utc_now())
                connection.commit()
        except Exception as error:
            with closing(get_connection()) as connection:
                facts.mark_concept_mapping_failed(connection, row["upload_id"], error)
                connection.commit()


def fail_job(upload_id: str, error: Exception) -> None:
    with closing(get_connection()) as connection:
        connection.execute(
            "UPDATE extractions SET status = 'failed', error_message = ?, updated_at = ? WHERE upload_id = ?",
            (str(error)[:500], utc_now(), upload_id),
        )
        connection.commit()


def worker_loop() -> None:
    while True:
        worker_event.wait(timeout=10)
        worker_event.clear()
        # Complete any interrupted post-extraction work before taking a new file.
        # There is intentionally only one worker, so a document moves through
        # extraction, indexing, and fact ingestion without competing for models.
        process_pending_indexes()
        process_pending_facts()
        process_pending_concept_mapping()
        process_pending_entity_resolution()
        while job := claim_next_job():
            try:
                path = UPLOADS_DIR / job["stored_name"]
                if not path.is_file():
                    raise FileNotFoundError("Stored file is missing.")
                if job["mime_type"] == "application/pdf":
                    text, page_count, ocr_page_count = extractor.extract_pdf(path)
                else:
                    text, page_count, ocr_page_count = extractor.extract_image(path)
                complete_job(job["id"], text, page_count, ocr_page_count)
                process_pending_indexes(job["id"])
                process_pending_facts(job["id"])
                process_pending_concept_mapping(job["id"])
                process_pending_entity_resolution(job["id"])
            except Exception as error:
                fail_job(job["id"], error)


def start_worker() -> None:
    global worker_started
    with worker_start_lock:
        if not worker_started:
            threading.Thread(target=worker_loop, name="document-extraction-worker", daemon=True).start()
            worker_started = True
    enqueue_processing()


def warm_rag_models() -> None:
    """Load the local embedding and reranking models once per API worker.

    Keeping this at startup makes readiness explicit and prevents the first
    document question from paying the model-initialisation cost.
    """
    logger.info("Loading local embedding and reranking models...")
    rag.embed_texts(["Document intelligence startup warmup."])
    rag.rerank_texts("startup warmup", ["Document intelligence startup warmup."])
    logger.info("Local embedding and reranking models are ready.")


@app.on_event("startup")
def startup() -> None:
    initialise_storage()
    recover_interrupted_jobs()
    warm_rag_models()
    start_worker()


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Document Library API"}


@app.get("/uploads")
def list_uploads() -> list[dict[str, str | int | None]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT uploads.*, extractions.status AS extraction_status, document_indexes.status AS index_status, document_facts.status AS fact_status,
                   document_entity_links.entity_id, document_entity_links.status AS entity_status,
                   document_entity_links.reason AS entity_reason, entities.display_name AS entity_name
            FROM uploads JOIN extractions ON extractions.upload_id = uploads.id
            LEFT JOIN document_indexes ON document_indexes.upload_id = uploads.id
            LEFT JOIN document_facts ON document_facts.upload_id = uploads.id
            LEFT JOIN document_entity_links ON document_entity_links.document_id = uploads.id
            LEFT JOIN entities ON entities.id = document_entity_links.entity_id ORDER BY uploads.uploaded_at DESC
            """
        ).fetchall()
    return [document_payload(row) for row in rows]


@app.get("/entities")
def list_entities() -> list[dict[str, object]]:
    with closing(get_connection()) as connection:
        return entities.list_entities(connection)


@app.post("/entities", status_code=201)
def create_entity(payload: dict[str, object]) -> dict[str, object]:
    name = payload.get("name")
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 160:
        raise HTTPException(status_code=400, detail="Customer name must be between 2 and 160 characters.")
    with closing(get_connection()) as connection:
        entity_id = entities.create_entity(connection, name.strip())
        connection.commit()
    return {"id": entity_id, "name": name.strip(), "documentCount": 0}


@app.post("/uploads", status_code=201)
def upload_file(file: UploadFile = File(...)) -> dict[str, str | int | None]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required.")

    upload_id = str(uuid.uuid4())
    original_name = Path(file.filename).name
    extension = Path(original_name).suffix.lower()
    destination = UPLOADS_DIR / f"{upload_id}{extension}"
    mime_type = file.content_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    size_bytes = 0
    try:
        with destination.open("wb") as destination_file:
            while chunk := file.file.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="Files must be 50 MB or smaller.")
                destination_file.write(chunk)
        if mime_type == "application/pdf":
            try:
                page_count = extractor.pdf_page_count(destination)
            except Exception as error:
                raise HTTPException(status_code=400, detail="The PDF could not be read.") from error
            if page_count > MAX_PDF_PAGES:
                raise HTTPException(status_code=413, detail="PDFs must contain 100 pages or fewer.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()

    uploaded_at = utc_now()
    extraction_status = "queued" if is_extractable(mime_type) else "unsupported"
    with closing(get_connection()) as connection:
        connection.execute(
            "INSERT INTO uploads (id, original_name, stored_name, mime_type, size_bytes, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (upload_id, original_name, destination.name, mime_type, size_bytes, uploaded_at),
        )
        connection.execute(
            "INSERT INTO extractions (upload_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (upload_id, extraction_status, uploaded_at, uploaded_at),
        )
        connection.commit()
    if extraction_status == "queued":
        enqueue_processing()
    return {
        "id": upload_id, "name": original_name, "type": mime_type, "sizeBytes": size_bytes,
        "uploadedAt": uploaded_at, "url": f"/uploads/{upload_id}/file", "extractionStatus": extraction_status, "indexStatus": None, "factStatus": None,
        "entityId": None, "entityName": None, "entityStatus": None, "entityReason": None,
    }


@app.get("/uploads/{upload_id}/extraction")
def get_extraction(upload_id: str) -> dict[str, object | None]:
    upload = get_upload(upload_id)
    with closing(get_connection()) as connection:
        extraction = connection.execute("SELECT * FROM extractions WHERE upload_id = ?", (upload_id,)).fetchone()
    if extraction is None:
        raise HTTPException(status_code=404, detail="Extraction record not found.")
    return {
        "document": document_payload(upload), "status": extraction["status"],
        "text": extraction["extracted_text"] if extraction["status"] == "completed" else None,
        "pageCount": extraction["page_count"], "ocrPageCount": extraction["ocr_page_count"],
        "error": extraction["error_message"], "completedAt": extraction["completed_at"],
    }


@app.put("/uploads/{upload_id}/entity")
def assign_upload_entity(upload_id: str, payload: dict[str, object]) -> dict[str, str | int | None]:
    entity_id = payload.get("entityId")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise HTTPException(status_code=400, detail="A customer ID is required.")
    upload = get_upload(upload_id)
    if upload["entity_status"] in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="Automatic customer resolution is in progress. Try again shortly.")
    with closing(get_connection()) as connection:
        current = connection.execute("SELECT status FROM document_entity_links WHERE document_id=?", (upload_id,)).fetchone()
        if current is not None and current["status"] == "processing":
            raise HTTPException(status_code=409, detail="Customer resolution is still processing. Try again shortly.")
        try:
            entities.assign_document(connection, upload_id, entity_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        connection.commit()
    return document_payload(get_upload(upload_id))


@app.post("/uploads/{upload_id}/extraction/retry")
def retry_extraction(upload_id: str) -> dict[str, str]:
    upload = get_upload(upload_id)
    if not is_extractable(upload["mime_type"]):
        raise HTTPException(status_code=400, detail="Text extraction is only available for PDFs and images.")
    with closing(get_connection()) as connection:
        row = connection.execute("SELECT status FROM extractions WHERE upload_id = ?", (upload_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Extraction record not found.")
        if row["status"] != "failed":
            raise HTTPException(status_code=409, detail="Only failed extractions can be retried.")
        connection.execute(
            """
            UPDATE extractions SET status = 'queued', extracted_text = NULL, page_count = NULL, ocr_page_count = NULL,
            error_message = NULL, completed_at = NULL, updated_at = ? WHERE upload_id = ?
            """, (utc_now(), upload_id),
        )
        connection.commit()
    enqueue_processing()
    return {"status": "queued"}


@app.post("/uploads/{upload_id}/facts/retry")
def retry_fact_extraction(upload_id: str) -> dict[str, str]:
    upload = get_upload(upload_id)
    if not is_extractable(upload["mime_type"]):
        raise HTTPException(status_code=400, detail="Fact extraction is only available for PDFs and images.")
    with closing(get_connection()) as connection:
        extraction = connection.execute("SELECT status FROM extractions WHERE upload_id = ?", (upload_id,)).fetchone()
        if extraction is None or extraction["status"] != "completed":
            raise HTTPException(status_code=409, detail="Text extraction must finish before facts can be retried.")
        facts.queue_document(connection, upload_id)
        connection.commit()
    enqueue_processing()
    return {"status": "queued"}


@app.delete("/uploads/{upload_id}", status_code=204, response_class=Response)
def delete_upload(upload_id: str) -> Response:
    upload = get_upload(upload_id)
    file_path = UPLOADS_DIR / upload["stored_name"]
    try:
        file_path.unlink(missing_ok=True)
    except OSError as error:
        raise HTTPException(status_code=409, detail="The file is currently being processed. Try deleting it again shortly.") from error

    with closing(get_connection()) as connection:
        rag.delete_document(connection, upload_id)
        facts.delete_document(connection, upload_id)
        entities.delete_document(connection, upload_id)
        connection.execute("DELETE FROM extractions WHERE upload_id = ?", (upload_id,))
        connection.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        connection.commit()
    return Response(status_code=204)


def _prepare_question(payload: dict[str, object], original_question: str | None = None) -> tuple[dict[str, object] | None, list[sqlite3.Row], dict[str, object]]:
    question = str(payload.get("question", "")).strip()
    raw_question = original_question or question
    document_ids = payload.get("documentIds")
    entity_id = payload.get("entityId")
    if not question:
        raise HTTPException(status_code=400, detail="A question is required.")
    if document_ids is not None and entity_id is not None:
        raise HTTPException(status_code=400, detail="Choose either documents or a customer, not both.")
    if document_ids is not None and (not isinstance(document_ids, list) or not document_ids or not all(isinstance(item, str) and item.strip() for item in document_ids)):
        raise HTTPException(status_code=400, detail="documentIds must contain at least one document ID.")
    if entity_id is not None and (not isinstance(entity_id, str) or not entity_id.strip()):
        raise HTTPException(status_code=400, detail="entityId must be a customer ID.")
    trace: dict[str, object] = {"question": raw_question, "documentScope": "all completed documents"}
    if raw_question != question:
        trace["standaloneQuestion"] = question
    with closing(get_connection()) as connection:
        if document_ids is not None:
            document_ids = list(dict.fromkeys(document_ids))
            placeholders = ",".join("?" for _ in document_ids)
            found = {row[0] for row in connection.execute(f"SELECT id FROM uploads WHERE id IN ({placeholders})", document_ids).fetchall()}
            if found != set(document_ids):
                raise HTTPException(status_code=404, detail="One or more selected documents no longer exist.")
            entity_scope: dict[str, object] = {"mode": "selected-documents", "documentIds": document_ids}
            trace["documentScope"] = document_ids
        elif entity_id is not None:
            document_ids = entities.linked_document_ids(connection, entity_id)
            if document_ids is None:
                raise HTTPException(status_code=404, detail="Customer not found.")
            entity_scope = {"mode": "selected-customer", "entityId": entity_id, "documentIds": document_ids}
            trace["documentScope"] = f"customer: {entity_id}"
        else:
            # An explicitly named customer in the current turn must take
            # precedence over a subject inferred from earlier chat history.
            direct_scope = entities.question_scope(connection, raw_question)
            entity_scope = direct_scope if direct_scope.get("mode") in {"entity", "ambiguous"} else entities.question_scope(connection, question)
            if entity_scope.get("mode") == "entity":
                document_ids = entity_scope["documentIds"]
                trace["documentScope"] = f"customer: {entity_scope['entityName']}"
    trace["entityScope"] = entity_scope
    if entity_scope.get("mode") == "ambiguous":
        trace["route"] = "ambiguous-customer"
        return {
            "answer": "More than one customer matches this name. Choose a customer or specific documents before asking.",
            "sources": [], "mode": "entity", "debug": trace,
        }, [], trace
    if entity_scope.get("mode") in {"entity", "selected-customer"} and not document_ids:
        trace["route"] = "entity-no-linked-documents"
        return {
            "answer": "This customer has no linked documents to answer from yet.",
            "sources": [], "mode": "entity", "debug": trace,
        }, [], trace
    try:
        with closing(get_connection()) as connection:
            structured_answer, structured_trace = facts.answer_with_trace(connection, question, document_ids)
        trace["structuredFacts"] = structured_trace
        if structured_answer is not None:
            answer, fact_rows = structured_answer
            trace["route"] = "structured-facts"
            trace["finalEvidenceIds"] = [row["id"] for row in fact_rows]
            return {
                "answer": answer,
                "sources": citation_sources(fact_rows, "document_id"),
                "mode": "structured", "debug": trace,
            }, [], trace
        with closing(get_connection()) as connection:
            chunks, rag_trace = rag.retrieve_with_trace(connection, question, document_ids)
        trace["rag"] = rag_trace
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="Document search is not ready. Check the local RAG dependencies and wait for indexing to finish.",
        ) from error
    if not chunks:
        trace["route"] = "no-evidence"
        return {"answer": "I could not find strong enough evidence in the indexed documents.", "sources": [], "mode": "rag", "debug": trace}, [], trace
    trace["claudeInputEvidenceIds"] = [row["id"] for row in chunks]
    return None, chunks, trace


def _finish_rag(answer: str, cited_chunk_ids: list[str], chunks: list[sqlite3.Row], trace: dict[str, object]) -> dict[str, object]:
    chunks_by_id = {row["id"]: row for row in chunks}
    valid_ids = list(dict.fromkeys(chunk_id for chunk_id in cited_chunk_ids if chunk_id in chunks_by_id))
    trace["route"] = "strict-rag"
    trace["claudeValidatedCitationIds"] = valid_ids
    return {
        "answer": answer,
        "sources": citation_sources([chunks_by_id[chunk_id] for chunk_id in valid_ids], "upload_id"),
        "mode": "rag", "debug": trace,
    }


@app.post("/questions")
def ask_question(payload: dict[str, object]) -> dict[str, object]:
    prepared, chunks, trace = _prepare_question(payload)
    if prepared is not None:
        return prepared
    try:
        answer, cited_chunk_ids = rag.answer(str(payload["question"]), chunks)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return _finish_rag(answer, cited_chunk_ids, chunks, trace)


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _conversation(connection: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return row


class ChatCancelled(Exception):
    pass


def _turn_is_pending(connection: sqlite3.Connection, conversation_id: str, turn_id: str, attempt_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM chat_messages WHERE conversation_id=? AND turn_id=? AND role='user' AND status='pending' AND attempt_id=?",
        (conversation_id, turn_id, attempt_id),
    ).fetchone() is not None


@app.post("/conversations", status_code=201)
def create_conversation(payload: dict[str, object]) -> dict[str, object]:
    scope_mode = payload.get("scopeMode", "all")
    entity_id = payload.get("entityId")
    document_ids = payload.get("documentIds")
    if scope_mode not in {"all", "customer", "documents"}:
        raise HTTPException(status_code=400, detail="Invalid conversation scope.")
    if scope_mode == "customer" and (not isinstance(entity_id, str) or not entity_id.strip()):
        raise HTTPException(status_code=400, detail="Choose a customer for this chat.")
    if scope_mode == "documents" and (not isinstance(document_ids, list) or not document_ids or not all(isinstance(item, str) and item.strip() for item in document_ids)):
        raise HTTPException(status_code=400, detail="Choose at least one document for this chat.")
    if scope_mode != "customer" and entity_id is not None or scope_mode != "documents" and document_ids is not None:
        raise HTTPException(status_code=400, detail="The supplied scope does not match the scope mode.")
    document_ids = list(dict.fromkeys(document_ids)) if scope_mode == "documents" else []
    with closing(get_connection()) as connection:
        if scope_mode == "customer" and connection.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            found = {row[0] for row in connection.execute(f"SELECT id FROM uploads WHERE id IN ({placeholders})", document_ids)}
            if found != set(document_ids):
                raise HTTPException(status_code=404, detail="One or more selected documents no longer exist.")
        conversation_id = chat_history.new_id()
        timestamp = utc_now()
        connection.execute(
            "INSERT INTO conversations(id, title, scope_mode, entity_id, document_ids, created_at, updated_at) VALUES (?, 'New chat', ?, ?, ?, ?, ?)",
            (conversation_id, scope_mode, entity_id if scope_mode == "customer" else None, json.dumps(document_ids), timestamp, timestamp),
        )
        connection.commit()
        return chat_history.conversation_payload(_conversation(connection, conversation_id))


@app.get("/conversations")
def list_conversations(limit: int = 50, offset: int = 0) -> list[dict[str, object]]:
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(status_code=400, detail="Invalid conversation page.")
    with closing(get_connection()) as connection:
        rows = connection.execute("SELECT * FROM conversations ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [chat_history.conversation_payload(row) for row in rows]


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, object]:
    with closing(get_connection()) as connection:
        conversation = _conversation(connection, conversation_id)
        messages = connection.execute(
            """SELECT m.* FROM chat_messages AS m
            JOIN chat_messages AS u ON u.conversation_id=m.conversation_id
              AND u.turn_id=m.turn_id AND u.role='user'
            WHERE m.conversation_id=?
            ORDER BY u.created_at, u.id, CASE m.role WHEN 'user' THEN 0 ELSE 1 END""",
            (conversation_id,),
        ).fetchall()
        return {**chat_history.conversation_payload(conversation), "messages": [chat_history.message_payload(row) for row in messages]}


@app.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: dict[str, object]) -> dict[str, object]:
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        raise HTTPException(status_code=400, detail="Title must be between 1 and 120 characters.")
    with closing(get_connection()) as connection:
        _conversation(connection, conversation_id)
        connection.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?", (title.strip(), utc_now(), conversation_id))
        connection.commit()
        return chat_history.conversation_payload(_conversation(connection, conversation_id))


@app.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str) -> Response:
    with closing(get_connection()) as connection:
        _conversation(connection, conversation_id)
        connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        connection.commit()
    return Response(status_code=204)


@app.post("/conversations/{conversation_id}/messages/stream")
def stream_conversation_message(conversation_id: str, payload: dict[str, object]) -> StreamingResponse:
    question = payload.get("question")
    turn_id = payload.get("turnId") or chat_history.new_id()
    attempt_id = payload.get("attemptId") or chat_history.new_id()
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise HTTPException(status_code=400, detail="Question must be between 1 and 4000 characters.")
    if not isinstance(turn_id, str) or not turn_id.strip() or len(turn_id) > 100:
        raise HTTPException(status_code=400, detail="Invalid turn ID.")
    if not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 100:
        raise HTTPException(status_code=400, detail="Invalid attempt ID.")
    question = question.strip()
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        conversation = _conversation(connection, conversation_id)
        pending = connection.execute(
            "SELECT turn_id FROM chat_messages WHERE conversation_id=? AND role='user' AND status='pending'",
            (conversation_id,),
        ).fetchone()
        if pending is not None:
            raise HTTPException(status_code=409, detail="Another answer is already in progress in this chat.")
        existing = connection.execute(
            "SELECT * FROM chat_messages WHERE conversation_id=? AND turn_id=? AND role='user'",
            (conversation_id, turn_id),
        ).fetchone()
        if existing and (existing["content"] != question or existing["status"] == "pending"):
            raise HTTPException(status_code=409, detail="This turn is already in progress or has different text.")
        if existing and existing["status"] == "completed":
            answer = connection.execute(
                "SELECT * FROM chat_messages WHERE conversation_id=? AND turn_id=? AND role='assistant'",
                (conversation_id, turn_id),
            ).fetchone()
            if answer:
                result = chat_history.message_payload(answer)
                return StreamingResponse(iter([_sse("final", {"message": result, "conversation": chat_history.conversation_payload(conversation)})]), media_type="text/event-stream")
        if existing:
            connection.execute("UPDATE chat_messages SET status='pending', attempt_id=? WHERE id=?", (attempt_id, existing["id"]))
        else:
            connection.execute(
                "INSERT INTO chat_messages(id, conversation_id, turn_id, role, content, status, attempt_id, created_at) VALUES (?, ?, ?, 'user', ?, 'pending', ?, ?)",
                (chat_history.new_id(), conversation_id, turn_id, question, attempt_id, utc_now()),
            )
        connection.commit()
        scope = chat_history.conversation_payload(conversation)

    def events():
        finished = False
        try:
            yield _sse("status", {"stage": "context", "label": "Understanding question"})
            with closing(get_connection()) as connection:
                current = _conversation(connection, conversation_id)
                effective_question, context_trace = chat_history.context_and_question(connection, current, question)
                if not _turn_is_pending(connection, conversation_id, turn_id, attempt_id):
                    raise ChatCancelled()
            yield _sse("status", {"stage": "retrieval", "label": "Finding document evidence"})
            ask_payload: dict[str, object] = {"question": effective_question}
            if scope["scopeMode"] == "customer":
                ask_payload["entityId"] = scope["entityId"]
            elif scope["scopeMode"] == "documents":
                ask_payload["documentIds"] = scope["documentIds"]
            prepared, chunks, trace = _prepare_question(ask_payload, question)
            trace["conversationContext"] = context_trace
            with closing(get_connection()) as connection:
                if not _turn_is_pending(connection, conversation_id, turn_id, attempt_id):
                    raise ChatCancelled()
            yield _sse("status", {"stage": "answer", "label": "Writing answer"})
            if prepared is not None:
                result = prepared
                # Structured results are calculated and verified before they
                # can be shown. RAG generation below streams actual model text.
                for start in range(0, len(str(result["answer"])), 80):
                    with closing(get_connection()) as connection:
                        if not _turn_is_pending(connection, conversation_id, turn_id, attempt_id):
                            raise ChatCancelled()
                    yield _sse("delta", {"text": str(result["answer"])[start:start + 80]})
            else:
                answer = ""
                citations: list[str] = []
                delta_count = 0
                for item in rag.answer_stream(effective_question, chunks):
                    if item["type"] == "delta":
                        delta_count += 1
                        if delta_count % 15 == 0:
                            with closing(get_connection()) as connection:
                                if not _turn_is_pending(connection, conversation_id, turn_id, attempt_id):
                                    raise ChatCancelled()
                        yield _sse("delta", {"text": item["text"]})
                    elif item["type"] == "final":
                        answer = item["answer"]
                        citations = item["citedChunkIds"]
                if not answer:
                    raise RuntimeError("Claude did not produce a grounded answer.")
                result = _finish_rag(answer, citations, chunks, trace)
            timestamp = utc_now()
            with closing(get_connection()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not _turn_is_pending(connection, conversation_id, turn_id, attempt_id):
                    raise ChatCancelled()
                connection.execute(
                    "INSERT OR REPLACE INTO chat_messages(id, conversation_id, turn_id, role, content, status, sources_json, mode, debug_json, created_at) VALUES (?, ?, ?, 'assistant', ?, 'completed', ?, ?, ?, ?)",
                    (chat_history.new_id(), conversation_id, turn_id, result["answer"], json.dumps(result["sources"]), result["mode"], json.dumps(result["debug"]), timestamp),
                )
                connection.execute(
                    "UPDATE chat_messages SET status='completed' WHERE conversation_id=? AND turn_id=? AND role='user' AND attempt_id=?",
                    (conversation_id, turn_id, attempt_id),
                )
                connection.execute(
                    "UPDATE conversations SET title=CASE WHEN title='New chat' THEN ? ELSE title END, updated_at=? WHERE id=?",
                    (question[:70], timestamp, conversation_id),
                )
                connection.commit()
                saved = connection.execute(
                    "SELECT * FROM chat_messages WHERE conversation_id=? AND turn_id=? AND role='assistant'",
                    (conversation_id, turn_id),
                ).fetchone()
                updated = _conversation(connection, conversation_id)
            finished = True
            yield _sse("final", {"message": chat_history.message_payload(saved), "conversation": chat_history.conversation_payload(updated)})
        except GeneratorExit:
            raise
        except Exception as error:
            if isinstance(error, ChatCancelled):
                logger.info("Conversation turn was stopped")
            elif isinstance(error, HTTPException):
                logger.warning("Conversation turn rejected: %s", error.detail)
            else:
                logger.exception("Conversation turn failed")
            with closing(get_connection()) as connection:
                connection.execute(
                    "UPDATE chat_messages SET status='failed' WHERE conversation_id=? AND turn_id=? AND role='user' AND attempt_id=? AND status='pending'",
                    (conversation_id, turn_id, attempt_id),
                )
                connection.commit()
            finished = True
            detail = "Response stopped." if isinstance(error, ChatCancelled) else error.detail if isinstance(error, HTTPException) else str(error) if isinstance(error, RuntimeError) else "The question could not be answered."
            yield _sse("error", {"detail": detail})
        finally:
            if not finished:
                with closing(get_connection()) as connection:
                    connection.execute(
                        "UPDATE chat_messages SET status='interrupted' WHERE conversation_id=? AND turn_id=? AND role='user' AND attempt_id=? AND status='pending'",
                        (conversation_id, turn_id, attempt_id),
                    )
                    connection.commit()

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/conversations/{conversation_id}/turns/{turn_id}/cancel")
def cancel_conversation_turn(conversation_id: str, turn_id: str, payload: dict[str, object]) -> dict[str, str]:
    attempt_id = payload.get("attemptId")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise HTTPException(status_code=400, detail="An attempt ID is required.")
    with closing(get_connection()) as connection:
        _conversation(connection, conversation_id)
        cursor = connection.execute(
            "UPDATE chat_messages SET status='interrupted' WHERE conversation_id=? AND turn_id=? AND role='user' AND status='pending' AND attempt_id=?",
            (conversation_id, turn_id, attempt_id),
        )
        connection.commit()
    return {"status": "interrupted" if cursor.rowcount else "unchanged"}


@app.get("/uploads/{upload_id}/facts")
def get_document_facts(upload_id: str) -> dict[str, object]:
    get_upload(upload_id)
    with closing(get_connection()) as connection:
        status = connection.execute("SELECT status, error_message FROM document_facts WHERE upload_id = ?", (upload_id,)).fetchone()
        rows = connection.execute("SELECT * FROM facts WHERE document_id = ? ORDER BY page_number, id", (upload_id,)).fetchall()
    return {"status": status["status"] if status else "unavailable", "error": status["error_message"] if status else None, "facts": [dict(row) for row in rows]}


@app.get("/uploads/{upload_id}/preview")
def preview_upload(upload_id: str) -> FileResponse:
    upload = get_upload(upload_id)
    if not is_extractable(upload["mime_type"]):
        raise HTTPException(status_code=415, detail="In-app preview is available for PDFs and images.")
    file_path = UPLOADS_DIR / upload["stored_name"]
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Stored file is missing.")
    return FileResponse(file_path, media_type=upload["mime_type"])


@app.get("/uploads/{upload_id}/file")
def download_upload(upload_id: str) -> FileResponse:
    upload = get_upload(upload_id)
    file_path = UPLOADS_DIR / upload["stored_name"]
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Stored file is missing.")
    return FileResponse(file_path, media_type=upload["mime_type"], filename=upload["original_name"])
