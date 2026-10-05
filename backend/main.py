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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

from extraction import TextExtractor
from entity_resolution import EntityResolver
from facts import FactEngine
from rag import RagEngine
import agents
import chat_history
import knowledge
import office
import tables

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


def document_kind(upload: sqlite3.Row) -> str | None:
    return office.document_kind(upload["original_name"], upload["mime_type"])


def is_extractable(upload: sqlite3.Row) -> bool:
    return document_kind(upload) is not None


def is_previewable(mime_type: str) -> bool:
    return mime_type == "application/pdf" or mime_type.startswith("image/")


def page_labels(connection: sqlite3.Connection, document_ids: list[str]) -> dict[str, list[str]]:
    """Human-readable page names ("Section 2", "Sheet Loans · Rows 1–50") per document."""
    if not document_ids:
        return {}
    placeholders = ",".join("?" for _ in document_ids)
    rows = connection.execute(
        f"SELECT upload_id, page_labels FROM extractions WHERE upload_id IN ({placeholders}) AND page_labels IS NOT NULL",
        document_ids,
    ).fetchall()
    return {row["upload_id"]: json.loads(row["page_labels"]) for row in rows}


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
    if sources:
        with closing(get_connection()) as connection:
            labels = page_labels(connection, list({source["documentId"] for source in sources}))
        for source in sources:
            document_labels = labels.get(str(source["documentId"]), [])
            if 0 < int(source["pageNumber"]) <= len(document_labels):
                source["pageLabel"] = document_labels[int(source["pageNumber"]) - 1]
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
        knowledge.initialise(connection)
        agents.initialise(connection)
        tables.initialise(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS extractions (
                upload_id TEXT PRIMARY KEY, status TEXT NOT NULL, extracted_text TEXT,
                page_count INTEGER, ocr_page_count INTEGER, error_message TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
                page_labels TEXT,
                FOREIGN KEY (upload_id) REFERENCES uploads(id)
            )
            """
        )
        if "page_labels" not in {row[1] for row in connection.execute("PRAGMA table_info(extractions)")}:
            connection.execute("ALTER TABLE extractions ADD COLUMN page_labels TEXT")
        now = utc_now()
        uploads = connection.execute(
            "SELECT uploads.*, extractions.status AS extraction_status FROM uploads LEFT JOIN extractions ON extractions.upload_id=uploads.id"
        ).fetchall()
        for upload in uploads:
            supported = is_extractable(upload)
            if upload["extraction_status"] is None:
                connection.execute(
                    "INSERT INTO extractions (upload_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (upload["id"], "queued" if supported else "unsupported", now, now),
                )
            elif upload["extraction_status"] == "unsupported" and supported:
                # Files stored before their format was supported are read now.
                connection.execute("UPDATE extractions SET status='queued', updated_at=? WHERE upload_id=?", (now, upload["id"]))
        # Spreadsheets are analysed from their rows, not from extracted facts.
        connection.execute(
            """
            INSERT OR IGNORE INTO document_facts (upload_id, status, extraction_version)
            SELECT upload_id, 'not_applicable', 'generic-facts-v1' FROM document_tables GROUP BY upload_id
            """
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
            FROM document_facts WHERE document_facts.status='ready'
              AND NOT EXISTS (SELECT 1 FROM document_spaces WHERE document_spaces.document_id=document_facts.upload_id)"""
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
        "documentKind": document_kind(row),
        "sizeBytes": row["size_bytes"], "uploadedAt": row["uploaded_at"],
        "url": f"/uploads/{row['id']}/file", "extractionStatus": row["extraction_status"],
        "indexStatus": row["index_status"] if "index_status" in row.keys() else None,
        "factStatus": row["fact_status"] if "fact_status" in row.keys() else None,
        "entityId": row["entity_id"] if "entity_id" in row.keys() else None,
        "entityName": row["entity_name"] if "entity_name" in row.keys() else None,
        "entityStatus": row["entity_status"] if "entity_status" in row.keys() else None,
        "entityReason": row["entity_reason"] if "entity_reason" in row.keys() else None,
        "knowledgeKind": (row["space_kind"] if "space_kind" in row.keys() else None) or knowledge.ENTITY,
        "spaceId": row["space_id"] if "space_id" in row.keys() else None,
        "spaceName": row["space_name"] if "space_name" in row.keys() else None,
    }


DOCUMENT_SELECT = """
    SELECT uploads.*, extractions.status AS extraction_status, document_indexes.status AS index_status, document_facts.status AS fact_status,
           document_entity_links.entity_id, document_entity_links.status AS entity_status,
           document_entity_links.reason AS entity_reason, entities.display_name AS entity_name,
           knowledge_spaces.id AS space_id, knowledge_spaces.name AS space_name, knowledge_spaces.kind AS space_kind
    FROM uploads JOIN extractions ON extractions.upload_id = uploads.id
    LEFT JOIN document_indexes ON document_indexes.upload_id = uploads.id
    LEFT JOIN document_facts ON document_facts.upload_id = uploads.id
    LEFT JOIN document_entity_links ON document_entity_links.document_id = uploads.id
    LEFT JOIN entities ON entities.id = document_entity_links.entity_id
    LEFT JOIN document_spaces ON document_spaces.document_id = uploads.id
    LEFT JOIN knowledge_spaces ON knowledge_spaces.id = document_spaces.space_id
"""


def get_upload(upload_id: str) -> sqlite3.Row:
    with closing(get_connection()) as connection:
        row = connection.execute(DOCUMENT_SELECT + " WHERE uploads.id = ?", (upload_id,)).fetchone()
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


def complete_job(
    upload_id: str, text: str, page_count: int, ocr_page_count: int,
    labels: list[str] | None = None, table_profiles: list[dict[str, object]] | None = None,
) -> None:
    now = utc_now()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE extractions SET status = 'completed', extracted_text = ?, page_count = ?, ocr_page_count = ?,
            page_labels = ?, error_message = NULL, updated_at = ?, completed_at = ? WHERE upload_id = ?
            """, (text, page_count, ocr_page_count, json.dumps(labels) if labels else None, now, now, upload_id),
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
        if table_profiles is not None:
            # A spreadsheet's rows are queried exactly; per-page fact
            # extraction would send every row block to Claude for nothing.
            tables.store_profiles(connection, upload_id, table_profiles)
            connection.execute(
                """INSERT INTO document_facts (upload_id, status, extraction_version) VALUES (?, 'not_applicable', ?)
                ON CONFLICT(upload_id) DO UPDATE SET status='not_applicable', error_message=NULL""",
                (upload_id, "generic-facts-v1"),
            )
        else:
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
            WHERE document_facts.status='ready' AND (document_entity_links.status IS NULL OR document_entity_links.status='queued')
              AND NOT EXISTS (SELECT 1 FROM document_spaces WHERE document_spaces.document_id=document_facts.upload_id)"""
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
            process_job(job)


def extract_job(job: sqlite3.Row) -> None:
    """Read one claimed upload and record its text, page labels and table profiles."""
    path = UPLOADS_DIR / job["stored_name"]
    if not path.is_file():
        raise FileNotFoundError("Stored file is missing.")
    kind = document_kind(job)
    labels: list[str] | None = None
    table_profiles: list[dict[str, object]] | None = None
    if kind == office.PDF:
        text, page_count, ocr_page_count = extractor.extract_pdf(path)
    elif kind == office.IMAGE:
        text, page_count, ocr_page_count = extractor.extract_image(path)
    elif kind == office.SPREADSHEET:
        sheets = tables.load_sheets(path)
        if not sheets:
            raise ValueError("The spreadsheet has no readable rows.")
        text, labels, table_profiles = tables.sheet_text(job["original_name"], sheets)
        page_count, ocr_page_count = len(labels), 0
        tables.remember(job["id"], sheets)
    elif kind == office.WORD:
        text, page_count, labels = office.extract_word(path)
        ocr_page_count = 0
    elif kind == office.TEXT:
        text, page_count, labels = office.extract_plain_text(path)
        ocr_page_count = 0
    else:
        raise ValueError("This file type cannot be read.")
    complete_job(job["id"], text, page_count, ocr_page_count, labels, table_profiles)


def process_job(job: sqlite3.Row) -> None:
    try:
        extract_job(job)
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
        rows = connection.execute(DOCUMENT_SELECT + " ORDER BY uploads.uploaded_at DESC").fetchall()
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


@app.patch("/entities/{entity_id}")
def rename_entity(entity_id: str, payload: dict[str, object]) -> dict[str, object]:
    name = payload.get("name")
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 160:
        raise HTTPException(status_code=400, detail="Customer name must be between 2 and 160 characters.")
    with closing(get_connection()) as connection:
        if connection.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        connection.execute("UPDATE entities SET display_name=?, updated_at=? WHERE id=?", (name.strip(), utc_now(), entity_id))
        connection.commit()
        return next(item for item in entities.list_entities(connection) if item["id"] == entity_id)


@app.delete("/entities/{entity_id}")
def delete_entity(entity_id: str) -> dict[str, int]:
    """Delete a customer record. Its documents are kept but become unassigned,
    and chats bound to this customer are deleted because they cannot be answered."""
    with closing(get_connection()) as connection:
        if connection.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        if connection.execute("SELECT 1 FROM document_entity_links WHERE entity_id=? AND status='processing'", (entity_id,)).fetchone():
            raise HTTPException(status_code=409, detail="Customer resolution is still processing. Try again shortly.")
        unlinked = connection.execute(
            """UPDATE document_entity_links SET entity_id=NULL, status='needs_review', confidence=NULL,
            reason='Its customer was deleted. Assign a customer or file it into a shared space.', resolved_at=?
            WHERE entity_id=?""",
            (utc_now(), entity_id),
        ).rowcount
        chats = connection.execute("DELETE FROM conversations WHERE entity_id=?", (entity_id,)).rowcount
        connection.execute("DELETE FROM entity_identifiers WHERE entity_id=?", (entity_id,))
        connection.execute("DELETE FROM entities WHERE id=?", (entity_id,))
        connection.commit()
    return {"unassignedDocuments": unlinked, "deletedChats": chats}


@app.delete("/uploads/{upload_id}/entity")
def unassign_upload_entity(upload_id: str) -> dict[str, str | int | None]:
    """Remove a document from its customer without deleting the document."""
    upload = get_upload(upload_id)
    if upload["entity_status"] in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="Customer resolution is in progress. Try again shortly.")
    with closing(get_connection()) as connection:
        connection.execute("DELETE FROM entity_identifiers WHERE source_document_id=?", (upload_id,))
        connection.execute(
            """UPDATE document_entity_links SET entity_id=NULL, status='needs_review', confidence=NULL,
            reason='Removed from its customer by a reviewer.', resolved_at=? WHERE document_id=?""",
            (utc_now(), upload_id),
        )
        connection.commit()
    return document_payload(get_upload(upload_id))


@app.post("/uploads", status_code=201)
def upload_file(file: UploadFile = File(...), spaceId: str | None = Form(None)) -> dict[str, str | int | None]:
    """Upload a document as customer knowledge, or file it straight into a shared space."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="A file is required.")
    space_id = spaceId.strip() if isinstance(spaceId, str) and spaceId.strip() else None
    if space_id is not None:
        with closing(get_connection()) as connection:
            if knowledge.get_space(connection, space_id) is None:
                raise HTTPException(status_code=404, detail="Knowledge space not found.")

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
    extraction_status = "queued" if office.document_kind(original_name, mime_type) else "unsupported"
    with closing(get_connection()) as connection:
        connection.execute(
            "INSERT INTO uploads (id, original_name, stored_name, mime_type, size_bytes, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (upload_id, original_name, destination.name, mime_type, size_bytes, uploaded_at),
        )
        connection.execute(
            "INSERT INTO extractions (upload_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (upload_id, extraction_status, uploaded_at, uploaded_at),
        )
        if space_id is not None:
            knowledge.file_into_space(connection, upload_id, space_id)
        connection.commit()
    if extraction_status == "queued":
        enqueue_processing()
    return document_payload(get_upload(upload_id))


@app.get("/uploads/{upload_id}/extraction")
def get_extraction(upload_id: str) -> dict[str, object | None]:
    upload = get_upload(upload_id)
    with closing(get_connection()) as connection:
        extraction = connection.execute("SELECT * FROM extractions WHERE upload_id = ?", (upload_id,)).fetchone()
        table_rows = connection.execute(
            "SELECT sheet, row_count, columns_json FROM document_tables WHERE upload_id=? ORDER BY position", (upload_id,)
        ).fetchall()
    if extraction is None:
        raise HTTPException(status_code=404, detail="Extraction record not found.")
    table_summaries = [
        {"sheet": row["sheet"], "rowCount": row["row_count"],
         "columns": [{"name": column["name"], "type": column["type"]} for column in json.loads(row["columns_json"])]}
        for row in table_rows
    ] or None
    return {
        "document": document_payload(upload), "status": extraction["status"],
        "text": extraction["extracted_text"] if extraction["status"] == "completed" else None,
        "pageCount": extraction["page_count"], "ocrPageCount": extraction["ocr_page_count"],
        "error": extraction["error_message"], "completedAt": extraction["completed_at"],
        "pageLabels": json.loads(extraction["page_labels"]) if extraction["page_labels"] else None,
        "tables": table_summaries,
    }


@app.put("/uploads/{upload_id}/entity")
def assign_upload_entity(upload_id: str, payload: dict[str, object]) -> dict[str, str | int | None]:
    entity_id = payload.get("entityId")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise HTTPException(status_code=400, detail="A customer ID is required.")
    upload = get_upload(upload_id)
    if upload["space_id"] is not None:
        raise HTTPException(status_code=409, detail=f"This document is filed as shared knowledge in {upload['space_name']}. Move it to customer knowledge before assigning a customer.")
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
    if not is_extractable(upload):
        raise HTTPException(status_code=400, detail="Text extraction is available for PDFs, images, spreadsheets, Word and text files.")
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
    if not is_extractable(upload):
        raise HTTPException(status_code=400, detail="Fact extraction is not available for this file type.")
    if document_kind(upload) == office.SPREADSHEET:
        raise HTTPException(status_code=400, detail="Spreadsheets are analysed directly from their rows, not from extracted facts.")
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
        knowledge.delete_document(connection, upload_id)
        tables.delete_document(connection, upload_id)
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
    return _answer_from_scope(question, document_ids, trace)


STRUCTURED_CAPABILITIES = {"fact_lookup", "calculations", "policy_evaluation", "customer_profile"}


def _answer_from_scope(
    question: str, document_ids: list[str] | None, trace: dict[str, object], capabilities: set[str] | None = None,
    model: str = agents.DEFAULT_MODEL,
) -> tuple[dict[str, object] | None, list[sqlite3.Row], dict[str, object]]:
    """Run spreadsheet analysis, structured facts, then strict RAG, over an already-authorised scope.

    ``capabilities`` is an agent's allowed tool set; ``None`` keeps the legacy
    unrestricted behaviour. An empty list scope must never reach retrieval,
    because the engines treat a missing scope as the whole library.
    """
    if document_ids is not None and not document_ids:
        raise ValueError("An empty document scope cannot be searched.")
    if capabilities is None or "table_analysis" in capabilities:
        try:
            with closing(get_connection()) as connection:
                table_result, table_trace = tables.answer(connection, question, document_ids, UPLOADS_DIR, model)
        except Exception as error:
            # Spreadsheet analysis is an extra route; its failure must not
            # block passage search over the same scope.
            logger.exception("Spreadsheet analysis failed")
            table_result, table_trace = None, {"reason": f"Spreadsheet analysis failed: {type(error).__name__}."}
        trace["tables"] = table_trace
        if table_result is not None:
            trace["route"] = "table-analysis"
            trace["finalEvidenceIds"] = [source["chunkId"] for source in table_result["sources"]]
            return {**table_result, "mode": "table", "debug": trace}, [], trace
    try:
        structured_answer = None
        if capabilities is None or capabilities & STRUCTURED_CAPABILITIES:
            with closing(get_connection()) as connection:
                structured_answer, structured_trace = facts.answer_with_trace(connection, question, document_ids, capabilities=capabilities)
            trace["structuredFacts"] = structured_trace
        else:
            trace["structuredFacts"] = {"reason": "This agent has no structured-fact capabilities."}
        if structured_answer is not None:
            answer, fact_rows = structured_answer
            trace["route"] = "structured-facts"
            trace["finalEvidenceIds"] = [row["id"] for row in fact_rows]
            return {
                "answer": answer,
                "sources": citation_sources(fact_rows, "document_id"),
                "mode": "structured", "debug": trace,
            }, [], trace
        if capabilities is not None and "document_search" not in capabilities:
            trace["route"] = "capability-not-enabled"
            return {
                "answer": "I could not answer this from verified facts, and this agent is not permitted to search document passages. "
                          "An administrator can enable \"Search document passages\" in the Agent Builder.",
                "sources": [], "mode": "agent", "debug": trace,
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


def _agent_context(connection: sqlite3.Connection, agent_id: str, entity_id: str | None) -> tuple[sqlite3.Row, dict[str, object]]:
    agent = agents.get_agent(connection, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="This chat's agent no longer exists.")
    try:
        return agent, agents.resolve_knowledge(connection, agent, entity_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _knowledge_summary(context: dict[str, object]) -> dict[str, object]:
    """What an answer was allowed to read, as shown to the user and stored with it."""
    return {
        "agent": context["agent"], "entity": context["entity"],
        "sources": [
            {"id": source["id"], "kind": source["kind"], "name": source["name"], "documentCount": source["documentCount"]}
            for source in context["sources"]
        ],
        "documentCount": len(context["documentIds"]),
    }


def _prepare_agent_question(
    question: str, raw_question: str, agent: sqlite3.Row, context: dict[str, object],
) -> tuple[dict[str, object] | None, list[sqlite3.Row], dict[str, object]]:
    """Answer within an agent's governed knowledge for one customer context.

    The document scope is exactly the resolved knowledge; nothing in the
    question can widen it. A question that names a different customer is
    refused, rather than silently answered from this chat's customer.
    """
    entity = context["entity"]
    trace: dict[str, object] = {
        "question": raw_question,
        "agent": {"id": agent["id"], "name": agent["name"], "capabilities": json.loads(agent["capabilities"])},
        "knowledge": _knowledge_summary(context),
        "documentScope": [source["name"] for source in context["sources"]],
    }
    if raw_question != question:
        trace["standaloneQuestion"] = question
    with closing(get_connection()) as connection:
        named = entities.question_scope(connection, raw_question)
    if named.get("mode") == "entity" and named.get("entityId") != (entity or {}).get("id"):
        trace["route"] = "other-customer-refused"
        current = f"{entity['name']}" if entity else "shared knowledge only (no customer)"
        return {
            "answer": f"This chat is scoped to {current}, so I can't use {named['entityName']}'s documents here. "
                      f"Start a new {agent['name']} chat with {named['entityName']} selected.",
            "sources": [], "mode": "agent", "debug": trace,
        }, [], trace
    document_ids = list(context["documentIds"])
    if not document_ids:
        trace["route"] = "no-knowledge-documents"
        where = f"{entity['name']}'s documents or " if entity else ""
        return {
            "answer": f"There are no documents in {where}this agent's knowledge spaces yet, so I have nothing to answer from.",
            "sources": [], "mode": "agent", "debug": trace,
        }, [], trace
    capabilities = set(json.loads(agent["capabilities"]))
    if entity and "consistency_check" in capabilities and agents.is_consistency_question(raw_question):
        with closing(get_connection()) as connection:
            conflicts, compared, evidence = agents.consistency_check(connection, list(context["entityDocumentIds"]))
        trace["route"] = "consistency-check"
        trace["consistency"] = {
            "comparedConceptCount": compared,
            "conflicts": [{"concept": item["concept"], "factIds": [row["id"] for row in item["variants"]]} for item in conflicts],
        }
        trace["finalEvidenceIds"] = [row["id"] for row in evidence]
        return {
            "answer": agents.describe_conflicts(conflicts, compared, entity["name"]),
            "sources": citation_sources(evidence, "document_id"), "mode": "consistency", "debug": trace,
        }, [], trace
    return _answer_from_scope(question, document_ids, trace, capabilities, agent["model"])


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
    agent_id = payload.get("agentId")
    if agent_id is not None:
        return _create_agent_conversation(agent_id, payload)
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


def _create_agent_conversation(agent_id: object, payload: dict[str, object]) -> dict[str, object]:
    entity_id = payload.get("entityId")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise HTTPException(status_code=400, detail="agentId must be an agent ID.")
    if entity_id is not None and (not isinstance(entity_id, str) or not entity_id.strip()):
        raise HTTPException(status_code=400, detail="entityId must be a customer ID.")
    if payload.get("documentIds") is not None or payload.get("scopeMode") not in (None, "agent"):
        raise HTTPException(status_code=400, detail="Agent chats use the agent's knowledge access, not a manual scope.")
    with closing(get_connection()) as connection:
        if agents.get_agent(connection, agent_id) is None:
            raise HTTPException(status_code=404, detail="Agent not found.")
        _agent_context(connection, agent_id, entity_id)  # validates the customer context
        conversation_id = chat_history.new_id()
        timestamp = utc_now()
        connection.execute(
            """INSERT INTO conversations(id, title, scope_mode, entity_id, document_ids, agent_id, created_at, updated_at)
            VALUES (?, 'New chat', 'agent', ?, '[]', ?, ?, ?)""",
            (conversation_id, entity_id, agent_id, timestamp, timestamp),
        )
        connection.commit()
        return chat_history.conversation_payload(_conversation(connection, conversation_id))


@app.get("/conversations")
def list_conversations(limit: int = 50, offset: int = 0, agentId: str | None = None) -> list[dict[str, object]]:
    """List chats, optionally for one agent (``agentId=none`` lists pre-agent chats)."""
    if limit < 1 or limit > 100 or offset < 0:
        raise HTTPException(status_code=400, detail="Invalid conversation page.")
    where, params = "", []
    if agentId == "none":
        where = " WHERE agent_id IS NULL"
    elif agentId:
        where, params = " WHERE agent_id=?", [agentId]
    with closing(get_connection()) as connection:
        rows = connection.execute(
            "SELECT * FROM conversations" + where + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [chat_history.conversation_payload(row) for row in rows]


@app.delete("/conversations")
def delete_conversations(agentId: str) -> dict[str, int]:
    """Delete every chat of one agent; ``agentId=none`` deletes the pre-agent chats."""
    with closing(get_connection()) as connection:
        if agentId == "none":
            deleted = connection.execute("DELETE FROM conversations WHERE agent_id IS NULL").rowcount
        else:
            deleted = connection.execute("DELETE FROM conversations WHERE agent_id=?", (agentId,)).rowcount
        connection.commit()
    return {"deletedChats": deleted}


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
        detail = {**chat_history.conversation_payload(conversation), "messages": [chat_history.message_payload(row) for row in messages]}
        if conversation["agent_id"]:
            try:
                _, context = _agent_context(connection, conversation["agent_id"], conversation["entity_id"])
                detail["knowledge"] = _knowledge_summary(context)
            except HTTPException as error:
                detail["knowledge"] = None
                detail["knowledgeError"] = error.detail
        return detail


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
            agent_row: sqlite3.Row | None = None
            knowledge_used: dict[str, object] | None = None
            if scope["agentId"]:
                with closing(get_connection()) as connection:
                    agent_row, agent_context = _agent_context(connection, scope["agentId"], scope["entityId"])
                knowledge_used = _knowledge_summary(agent_context)
                yield _sse("status", {"stage": "retrieval", "label": f"Searching {', '.join(source['name'] for source in agent_context['sources'])}"})
                prepared, chunks, trace = _prepare_agent_question(effective_question, question, agent_row, agent_context)
            else:
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
                answer_options = {"model": agent_row["model"], "instructions": agent_row["instructions"]} if agent_row is not None else {}
                for item in rag.answer_stream(effective_question, chunks, **answer_options):
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
                    "INSERT OR REPLACE INTO chat_messages(id, conversation_id, turn_id, role, content, status, sources_json, mode, debug_json, context_json, table_json, created_at) VALUES (?, ?, ?, 'assistant', ?, 'completed', ?, ?, ?, ?, ?, ?)",
                    (chat_history.new_id(), conversation_id, turn_id, result["answer"], json.dumps(result["sources"]), result["mode"], json.dumps(result["debug"]),
                     json.dumps(knowledge_used) if knowledge_used else None, json.dumps(result["table"]) if result.get("table") else None, timestamp),
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
    if not is_previewable(upload["mime_type"]):
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


# --- Knowledge spaces -------------------------------------------------------

def _space_text(payload: dict[str, object], key: str, minimum: int, maximum: int, label: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise HTTPException(status_code=400, detail=f"{label} must be between {minimum} and {maximum} characters.")
    return value.strip()


@app.get("/knowledge/spaces")
def list_knowledge_spaces() -> list[dict[str, object]]:
    with closing(get_connection()) as connection:
        return knowledge.list_spaces(connection)


@app.post("/knowledge/spaces", status_code=201)
def create_knowledge_space(payload: dict[str, object]) -> dict[str, object]:
    name = _space_text(payload, "name", 2, 80, "Space name")
    description = _space_text(payload, "description", 0, 400, "Description")
    kind = payload.get("kind")
    with closing(get_connection()) as connection:
        try:
            space_id = knowledge.create_space(connection, str(kind), name, description)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        connection.commit()
        return knowledge.space_payload(knowledge.get_space(connection, space_id))


@app.patch("/knowledge/spaces/{space_id}")
def update_knowledge_space(space_id: str, payload: dict[str, object]) -> dict[str, object]:
    name = _space_text(payload, "name", 2, 80, "Space name")
    description = _space_text(payload, "description", 0, 400, "Description")
    with closing(get_connection()) as connection:
        if knowledge.get_space(connection, space_id) is None:
            raise HTTPException(status_code=404, detail="Knowledge space not found.")
        connection.execute("UPDATE knowledge_spaces SET name=?, description=?, updated_at=? WHERE id=?", (name, description, utc_now(), space_id))
        connection.commit()
        return knowledge.space_payload(knowledge.get_space(connection, space_id))


@app.delete("/knowledge/spaces/{space_id}", status_code=204, response_class=Response)
def delete_knowledge_space(space_id: str, moveDocuments: bool = False) -> Response:
    """Delete a shared space. With ``moveDocuments`` its documents return to
    customer knowledge (and customer detection runs again); otherwise a space
    that still holds documents is not deleted."""
    with closing(get_connection()) as connection:
        space = knowledge.get_space(connection, space_id)
        if space is None:
            raise HTTPException(status_code=404, detail="Knowledge space not found.")
        if space["document_count"] and not moveDocuments:
            raise HTTPException(status_code=409, detail=f"Move or delete the {space['document_count']} document(s) in {space['name']} first.")
        for document_id in knowledge.space_document_ids(connection, [space_id]):
            knowledge.move_to_entity_knowledge(connection, document_id)
        agents.remove_space_from_agents(connection, space_id)
        connection.execute("DELETE FROM knowledge_spaces WHERE id=?", (space_id,))
        connection.commit()
    enqueue_processing()
    return Response(status_code=204)


@app.put("/uploads/{upload_id}/knowledge")
def file_upload_knowledge(upload_id: str, payload: dict[str, object]) -> dict[str, str | int | None]:
    """File a document into a shared space, or (spaceId null) return it to customer knowledge."""
    upload = get_upload(upload_id)
    space_id = payload.get("spaceId")
    if space_id is not None and (not isinstance(space_id, str) or not space_id.strip()):
        raise HTTPException(status_code=400, detail="spaceId must be a knowledge space ID or null.")
    if upload["entity_status"] == "processing":
        raise HTTPException(status_code=409, detail="Customer resolution is still processing. Try again shortly.")
    with closing(get_connection()) as connection:
        if space_id is None:
            if upload["space_id"] is not None:
                knowledge.move_to_entity_knowledge(connection, upload_id)
        else:
            try:
                knowledge.file_into_space(connection, upload_id, space_id)
            except ValueError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
        connection.commit()
    enqueue_processing()
    return document_payload(get_upload(upload_id))


# --- Agents -----------------------------------------------------------------

@app.get("/agents/catalog")
def agent_catalog() -> dict[str, object]:
    return agents.catalog()


@app.get("/agents")
def list_agents() -> list[dict[str, object]]:
    with closing(get_connection()) as connection:
        return agents.list_agents(connection)


@app.post("/agents", status_code=201)
def create_agent(payload: dict[str, object]) -> dict[str, object]:
    with closing(get_connection()) as connection:
        try:
            definition = agents.validate(connection, payload)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        agent_id = agents.create_agent(connection, definition)
        connection.commit()
        return agents.agent_payload(agents.get_agent(connection, agent_id))


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str) -> dict[str, object]:
    with closing(get_connection()) as connection:
        agent = agents.get_agent(connection, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found.")
        return agents.agent_payload(agent)


@app.put("/agents/{agent_id}")
def update_agent(agent_id: str, payload: dict[str, object]) -> dict[str, object]:
    with closing(get_connection()) as connection:
        if agents.get_agent(connection, agent_id) is None:
            raise HTTPException(status_code=404, detail="Agent not found.")
        try:
            definition = agents.validate(connection, payload, agent_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        agents.update_agent(connection, agent_id, definition)
        connection.commit()
        return agents.agent_payload(agents.get_agent(connection, agent_id))


@app.delete("/agents/{agent_id}", status_code=204, response_class=Response)
def delete_agent(agent_id: str) -> Response:
    with closing(get_connection()) as connection:
        agent = agents.get_agent(connection, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found.")
        # The agent's chats cannot be answered without it; remove them with it.
        connection.execute("DELETE FROM conversations WHERE agent_id=?", (agent_id,))
        connection.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        connection.commit()
    return Response(status_code=204)


@app.get("/agents/{agent_id}/knowledge")
def preview_agent_knowledge(agent_id: str, entityId: str | None = None) -> dict[str, object]:
    """Show exactly which knowledge an agent would use for a customer, before chatting."""
    with closing(get_connection()) as connection:
        _, context = _agent_context(connection, agent_id, entityId or None)
        return _knowledge_summary(context)
